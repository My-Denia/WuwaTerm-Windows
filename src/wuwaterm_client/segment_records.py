"""Resumable long-manuscript term review: the v3 segment records at runtime.

The contract is the public Web ``contracts/manuscript-v3/SPEC.md``; quoted
section names refer to it. Pure Python: no Qt, no HTTP.

State. The manuscript payload carries the v3 parts ``segments``,
``segment_choice_history`` and ``progress`` in their file shapes: record
ranges are whole-document scalar positions of the cores, and choice spans are
relative to the record's source core. The three keys exist only while there
are records or history entries (``with_parts``), so a v1/v2 draft stays
byte-identical. Every function here treats these values as immutable. It
returns new lists and dicts and shares the records it did not change, so an
undo snapshot can hold them by reference. The session's current basis
(``None`` until a review-v2 response supplies one) and its failures are
runtime state and are never saved.

SEGMENT RECORD REUSE RULE (the one place it is stated; SPEC "Design note" and
"Runtime" steps 3 and 5). A saved segment result is used without a new
request only when

1. its record is ``done`` (a ``stale`` record is never reused);
2. the content hashes of the current source and target cores equal the
   record's ``source_hash`` and ``target_hash``;
3. the record's ``direction`` and ``alignment_input`` equal what a request for
   these cores would send now; and
4. its basis is the same as the session's current basis in all six values.

1 to 3 make a *reuse candidate* (``reuse_candidate``); with 4 the result is
*current* (``is_current``). Before the session has a basis nothing is current:
a candidate shows as saved, no resolutions are sent, and the first check
(``next_index``) is the probe that supplies the basis. Every other record is
checked again (``plan_request``, ``apply_response``).

A record is ``done`` only while its result was computed with exactly its
confirmed choices as resolutions. When the confirmed choices change after the
result, the record becomes ``stale``. Unconfirmed choices are never sent and
never change the status.

Order of use: ``apply_edit`` for every replacement the editor performed, then
``normalize`` against the current pairing before planning, display or save.
The planner and the request functions expect normalized parts: one record per
pair, in order.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from . import manuscript
from .manuscript import (ALIGNMENT_AUTOMATIC, MAX_CORE_SCALARS, MAX_SEGMENT_CHOICE_HISTORY, MAX_SEGMENT_CHOICES,
                         MAX_SEGMENTS, SEGMENTATION_VERSION, WorkfileError)
from .segmentation import STATUS_READY, pair_is_sendable
from .semantic_session import _shift

LONG_MODE_SCALARS = 2000  # long mode: either side has more scalars than this
RULE_VERSION = "review-v2"
TERM_PAIR_RULE = "review.term_pair"
COVERAGE_RULES = ("review.term_pair", "review.sentence_meaning")
OFFICIAL_PAIR = "official_pair"
NOT_A_TERM = "not_a_term"
_PART_KEYS = ("segments", "segment_choice_history", "progress")


class Parts(NamedTuple):
    """The v3 long-mode parts of a payload, in their file shapes."""

    segments: list
    history: list   # segment_choice_history, oldest first
    progress: dict


def _empty_progress() -> dict:
    return {"current": None, "completed": []}


def parts_of(payload: Mapping[str, Any]) -> Parts:
    return Parts(payload.get("segments") or [], payload.get("segment_choice_history") or [],
                 payload.get("progress") or _empty_progress())


def with_parts(payload: Mapping[str, Any], parts: Parts) -> dict:
    """A shallow copy of ``payload`` that carries ``parts``. Without records and
    history entries the three keys are removed, leaving a plain v1/v2 draft."""
    value = dict(payload)
    if parts.segments or parts.history:
        value.update(segments=list(parts.segments), segment_choice_history=list(parts.history),
                     progress=parts.progress if parts.segments else _empty_progress())
    else:
        for key in _PART_KEYS:
            value.pop(key, None)
    return value


def long_mode(source: str, target: str) -> bool:
    return len(source) > LONG_MODE_SCALARS or len(target) > LONG_MODE_SCALARS


# ---- content and request inputs (SPEC "Terms", "Segment request") ----

def content_hash(core: str) -> str:
    """Lowercase hex SHA-256 of the UTF-8 core: Core ``text_revision``."""
    return hashlib.sha256(core.encode("utf-8")).hexdigest()


def alignment_list(alignments: Sequence[Mapping] | None, source: str, target: str,
                   source_core: tuple[int, int], target_core: tuple[int, int]) -> list | None:
    """The ``alignments`` a segment request sends for these cores; ``None`` (omit it,
    automatic) when the manuscript's alignments are ``None``. Each alignment is
    intersected with the cores and rebased to them, its text is the core slice,
    and one whose source, or non-null target, misses its core is left out. An
    explicit list, even ``[]``, never becomes automatic."""
    if alignments is None:
        return None

    def clip(span: Mapping, core: tuple[int, int], text: str) -> dict | None:
        start, end = max(span["start"], core[0]), min(span["end"], core[1])
        if start >= end:
            return None
        return {"start": start - core[0], "end": end - core[0], "text": text[start:end]}

    out = []
    for item in alignments:
        clipped_source = clip(item["source"], source_core, source)
        clipped_target = None if item["target"] is None else clip(item["target"], target_core, target)
        if clipped_source is None or (item["target"] is not None and clipped_target is None):
            continue
        out.append({"source": clipped_source, "target": clipped_target})
    return out


def _digest(items: list) -> str:
    return content_hash(json.dumps(items, ensure_ascii=False, separators=(",", ":")))


def alignment_input(alignments: Sequence[Mapping] | None, source: str, target: str,
                    source_core: tuple[int, int], target_core: tuple[int, int]) -> str:
    """``"automatic"``, or the SHA-256 of the minified ``alignment_list``."""
    items = alignment_list(alignments, source, target, source_core, target_core)
    return ALIGNMENT_AUTOMATIC if items is None else _digest(items)


def _cores(record: Mapping) -> tuple[tuple[int, int], tuple[int, int]]:
    return (record["source_start"], record["source_end"]), (record["target_start"], record["target_end"])


def _input_now(record: Mapping, payload: Mapping) -> str:
    return alignment_input(payload["alignments"], payload["source"], payload["target"], *_cores(record))


# ---- basis (SPEC "Terms") ----

def basis_of(report: Any) -> dict | None:
    """The v3 basis of a review-v2 response, or ``None`` when it has none: another
    rule version, no ``matcher_revision``, a null ``schema_version`` or
    ``source_commit``, or any other value a v3 basis does not allow."""
    if (not isinstance(report, Mapping) or report.get("rule_version") != RULE_VERSION
            or "matcher_revision" not in report or not isinstance(report.get("dictionary"), Mapping)):
        return None
    dictionary = report["dictionary"]
    basis = {"rule_version": RULE_VERSION,
             "dictionary": {key: dictionary.get(key) for key in ("schema_version", "source_commit", "term_count", "revision")},
             "matcher_revision": report["matcher_revision"]}
    return basis if manuscript._basis_v3(basis) else None


def _basis_key(basis: Mapping) -> tuple:
    dictionary = basis["dictionary"]
    return (basis["rule_version"], dictionary["schema_version"], dictionary["source_commit"],
            dictionary["term_count"], dictionary["revision"], basis["matcher_revision"])


def same_basis(a: Mapping | None, b: Mapping | None) -> bool:
    """All six values equal; ``None`` is never the same as anything."""
    return a is not None and b is not None and _basis_key(a) == _basis_key(b)


# ---- compact result (SPEC "Compact term result") ----

def compact_result(report: Mapping, source_core: str, target_core: str) -> dict | None:
    """The compact result of a review-v2 response for exactly these cores, or ``None``
    when it cannot be stored: no v3 basis, a finding whose rule id is not
    ``review.term_pair``, coverage rules other than the review-v2 constant, a
    count over its cap, a core outside 1 to 2,000 scalars, or anything else the
    reader would reject."""
    if (basis_of(report) is None or not 1 <= len(source_core) <= MAX_CORE_SCALARS
            or not 1 <= len(target_core) <= MAX_CORE_SCALARS):
        return None
    try:
        if list(report["coverage"]["rules"]) != list(COVERAGE_RULES):
            return None
        findings = []
        for finding in report["findings"]:
            source_span, target_span = finding["source_span"], finding["target_span"]
            if finding["rule_id"] != TERM_PAIR_RULE or source_span is None:
                return None
            findings.append({
                "verdict": finding["verdict"], "source": [source_span["start"], source_span["end"]],
                "target": None if target_span is None else [target_span["start"], target_span["end"]],
                "candidates": [{"candidate_id": c["candidate_id"], "zh": c["zh"], "en": c["en"],
                                "category": c["category"]} for c in finding["candidates"]],
                "candidates_truncated": finding["candidates_truncated"]})
        result = {"truncated": report["truncated"], "evaluated": report["coverage"]["evaluated"],
                  "not_evaluated": report["coverage"]["not_evaluated"], "findings": findings}
        manuscript._check_result_shape(result)
    except (KeyError, TypeError, WorkfileError):
        return None
    seen: set[tuple[int, int]] = set()
    for finding in findings:
        (start, end), target = finding["source"], finding["target"]
        if end > len(source_core) or (target is not None and target[1] > len(target_core)) or (start, end) in seen:
            return None
        seen.add((start, end))
    return result


def expand_result(record: Mapping, source: str, target: str) -> dict:
    """A done or stale record as a review-v2 report for the existing renderers. Spans
    are relative to the cores. What the compact form dropped is rebuilt from the
    record (SPEC "What is dropped"): no request id, no candidate sources. The
    request id is ``None``, so ``manuscript.valid_report`` does not accept it."""
    (a, b), (c, d) = _cores(record)
    source_core, target_core = source[a:b], target[c:d]
    basis, result = record["basis"], record["result"]

    def span(pair: list | None, core: str) -> dict | None:
        return None if pair is None else {"start": pair[0], "end": pair[1], "text": core[pair[0]:pair[1]]}

    findings = []
    for finding in result["findings"]:
        source_span = span(finding["source"], source_core)
        findings.append({
            "id": mention_id(source_span), "verdict": finding["verdict"], "rule_id": TERM_PAIR_RULE,
            "source_span": source_span, "target_span": span(finding["target"], target_core),
            "candidates": [{"zh": x["zh"], "en": x["en"], "category": x["category"], "sources": [],
                            "candidate_id": x["candidate_id"]} for x in finding["candidates"]],
            "candidates_truncated": finding["candidates_truncated"]})
    return {"request_id": None, "source_revision": record["source_hash"], "target_revision": record["target_hash"],
            "rule_version": basis["rule_version"], "matcher_revision": basis["matcher_revision"],
            "dictionary": dict(basis["dictionary"]),
            "coverage": {"evaluated": result["evaluated"], "not_evaluated": result["not_evaluated"],
                         "rules": list(COVERAGE_RULES)},
            "findings": findings, "truncated": result["truncated"]}


# ---- choices (SPEC "Segment choice", "Runtime" steps 6 and 7) ----

def mention_id(span: Mapping) -> str:
    """``start:end:text``, the review-v2 finding id of a source span."""
    return f'{span["start"]}:{span["end"]}:{span["text"]}'


def _place(choice: Mapping) -> tuple[int, int]:
    return choice["source_span"]["start"], choice["source_span"]["end"]


def _resolution(choice: Mapping) -> dict:
    item = {"mention_id": mention_id(choice["source_span"]), "choice": choice["choice"]}
    if choice["choice"] == OFFICIAL_PAIR:
        item["candidate_id"] = choice["candidate"]["candidate_id"]
    return item


def resolutions_for(record: Mapping) -> list[dict]:
    """The record's confirmed choices as review-v2 resolutions, in Core's shape."""
    return [_resolution(choice) for choice in record["choices"] if choice["confirmed"]]


def resolution_context(source_hash: str, basis: Mapping) -> dict:
    return {"source_revision": source_hash, "rule_version": basis["rule_version"],
            "dictionary_revision": basis["dictionary"]["revision"], "matcher_revision": basis["matcher_revision"]}


def _finding_at(result: Mapping | None, start: int, end: int) -> dict | None:
    if result is None:
        return None
    return next((f for f in result["findings"] if f["source"][0] == start and f["source"][1] == end), None)


def _choice_holds(choice: Mapping, result: Mapping) -> bool:
    """Step 7: the finding at the same span still offers the chosen candidate
    (all four fields), or for ``not_a_term`` the same candidate ids as a set."""
    finding = _finding_at(result, *_place(choice))
    if finding is None:
        return False
    if choice["choice"] == OFFICIAL_PAIR:
        return choice["candidate"] in finding["candidates"]
    return {c["candidate_id"] for c in finding["candidates"]} == set(choice["offered"])


def _unconfirmed(choice: dict) -> dict:
    return {**choice, "confirmed": False} if choice["confirmed"] else choice


def _replace(parts: Parts, index: int, record: dict) -> Parts:
    segments = list(parts.segments)
    segments[index] = record
    return parts._replace(segments=segments)


def choose(payload: Mapping[str, Any], index: int, start: int, end: int, choice: str,
           candidate_id: str | None = None) -> Parts:
    """Make or confirm the user's choice at a finding of record ``index``'s current
    result (``start``/``end`` relative to the source core). ``official_pair``
    takes a candidate of that finding; ``not_a_term`` offers the finding's
    candidate ids. A changed confirmed choice makes a done record stale."""
    parts = parts_of(payload)
    record = parts.segments[index]
    finding = _finding_at(record["result"], start, end)
    if finding is None:
        raise ValueError("the record's result has no finding at this span")
    if choice == OFFICIAL_PAIR:
        candidate = next((c for c in finding["candidates"] if c["candidate_id"] == candidate_id), None)
        if candidate is None:
            raise ValueError("candidate_id is not a candidate of this finding")
        candidate, offered = dict(candidate), []
    elif choice == NOT_A_TERM and candidate_id is None:
        candidate, offered = None, sorted(c["candidate_id"] for c in finding["candidates"])
    else:
        raise ValueError(f"not a choice: {choice!r}")
    core_start = record["source_start"]
    new = {"source_span": {"start": start, "end": end, "text": payload["source"][core_start + start:core_start + end]},
           "choice": choice, "candidate": candidate, "offered": offered, "confirmed": True}
    return _set_choice(parts, index, (start, end), new)


def unchoose(payload: Mapping[str, Any], index: int, start: int, end: int) -> Parts:
    """Remove the user's choice at this span; removing a confirmed one makes a done record stale."""
    return _set_choice(parts_of(payload), index, (start, end), None)


def _set_choice(parts: Parts, index: int, place: tuple[int, int], new: dict | None) -> Parts:
    record = parts.segments[index]
    old = next((c for c in record["choices"] if _place(c) == place), None)
    if old == new:
        return parts
    if old is None:
        choices = [*record["choices"], new]
    elif new is None:
        choices = [c for c in record["choices"] if c is not old]
    else:
        choices = [new if c is old else c for c in record["choices"]]
    if len(choices) > MAX_SEGMENT_CHOICES:
        raise manuscript._too_many()
    changed = (old is not None and old["confirmed"]) or (new is not None and new["confirmed"])
    status = "stale" if record["status"] == "done" and changed else record["status"]
    return _replace(parts, index, {**record, "choices": choices, "status": status})


# ---- reuse and planning (SPEC "Runtime" steps 3 to 5) ----

def reuse_candidate(record: Mapping, payload: Mapping[str, Any]) -> bool:
    """Rule items 1 to 3 (step 3), recomputed from the current text."""
    if record["status"] != "done" or record["direction"] != payload["direction"]:
        return False
    (a, b), (c, d) = _cores(record)
    return (content_hash(payload["source"][a:b]) == record["source_hash"]
            and content_hash(payload["target"][c:d]) == record["target_hash"]
            and _input_now(record, payload) == record["alignment_input"])


def is_current(record: Mapping, payload: Mapping[str, Any], basis: Mapping | None) -> bool:
    """The whole reuse rule (step 5); never while the session has no basis."""
    return same_basis(record["basis"], basis) and reuse_candidate(record, payload)


def needs_check(record: Mapping, payload: Mapping[str, Any], basis: Mapping | None) -> bool:
    """Without a basis yet: not a reuse candidate. With one: not current."""
    return not (is_current(record, payload, basis) if basis is not None else reuse_candidate(record, payload))


def next_index(payload: Mapping[str, Any], pairs: Sequence, basis: Mapping | None,
               skip: Collection[int] = ()) -> int | None:
    """The next segment to check among the sendable pairs not in ``skip``.

    With a basis: the first segment that needs a check. Without one (step 4,
    the probe): the segment at ``progress.current`` if it needs a check, else
    the first that does, else ``progress.current`` or the first sendable
    segment, because the probe must still supply the basis."""
    parts = parts_of(payload)
    # Text typed right at a core's end leaves the record's range as it was, so
    # only equal ranges show that the records follow the current pairing.
    if len(parts.segments) != len(pairs) or any(
            _cores(r) != ((p.source.core_start, p.source.core_end), (p.target.core_start, p.target.core_end))
            for r, p in zip(parts.segments, pairs, strict=True)):
        raise ValueError("normalize the parts to the current pairing first")
    source, target = payload["source"], payload["target"]

    def sendable(i: int) -> bool:
        return i not in skip and pair_is_sendable(pairs[i], source, target)

    def needing(i: int) -> bool:
        return sendable(i) and needs_check(parts.segments[i], payload, basis)

    current = parts.progress["current"]
    if basis is None and current is not None and needing(current):
        return current
    first = next((i for i in range(len(pairs)) if needing(i)), None)
    if first is not None or basis is not None:
        return first
    if current is not None and sendable(current):
        return current
    return next((i for i in range(len(pairs)) if sendable(i)), None)


@dataclass(frozen=True)
class Request:
    """One segment term request (SPEC "Segment request"). Pass ``source``,
    ``target``, ``direction``, ``alignments``, ``resolutions`` and
    ``resolution_context`` to ``ApiClient.review_pair``."""

    index: int
    source: str                    # the source core
    target: str                    # the target core
    direction: str
    alignments: list | None        # None: omitted (automatic)
    alignment_input: str
    resolutions: list              # [] when none are sent
    resolution_context: dict | None
    realign: bool                  # step 6, second case: realign every choice against the response


def plan_request(payload: Mapping[str, Any], index: int, basis: Mapping | None, *,
                 allow_resolutions: bool = True) -> Request:
    """Step 6. The record's confirmed choices go out as resolutions when its source
    hash, direction and basis are unchanged; otherwise none go out and the
    response realigns every choice. Before the session has a basis, nothing
    goes out (the probe).

    If Core rejects a request that carried resolutions (HTTP 400,
    ``invalid_request``), plan again once with ``allow_resolutions=False``. A
    rejection after that is a failure of the segment. Per segment and run the
    caller sends at most three requests: the planned one, that fallback, and
    one more when ``Applied.again``."""
    record = parts_of(payload).segments[index]
    (a, b), (c, d) = _cores(record)
    source_core, target_core = payload["source"][a:b], payload["target"][c:d]
    items = alignment_list(payload["alignments"], payload["source"], payload["target"], (a, b), (c, d))
    source_hash = content_hash(source_core)
    carry = (allow_resolutions and basis is not None and record["status"] != "pending"
             and record["source_hash"] == source_hash and record["direction"] == payload["direction"]
             and same_basis(record["basis"], basis))
    resolutions = resolutions_for(record) if carry else []
    return Request(index=index, source=source_core, target=target_core, direction=payload["direction"],
                   alignments=items, alignment_input=ALIGNMENT_AUTOMATIC if items is None else _digest(items),
                   resolutions=resolutions,
                   resolution_context=resolution_context(source_hash, basis) if resolutions else None,
                   realign=not carry)


@dataclass(frozen=True)
class Applied:
    """What a response did.

    ``outcome``: ``"stored"`` (the record holds the result), ``"current"`` (the
    record was already current: the probe only supplied the basis),
    ``"not_storable"`` (a failed check: the record keeps what it had) or
    ``"discarded"`` (the record changed while the request was in flight; it is
    checked again later). ``again``: send ``plan_request`` for this index once
    more now, because the confirmed choices differ from the resolutions the
    result was computed with."""

    parts: Parts
    basis: dict | None
    outcome: str
    again: bool = False


def _matches(record: Mapping, payload: Mapping[str, Any], request: Request) -> bool:
    (a, b), (c, d) = _cores(record)
    return (payload["direction"] == request.direction and payload["source"][a:b] == request.source
            and payload["target"][c:d] == request.target and _input_now(record, payload) == request.alignment_input)


def apply_response(payload: Mapping[str, Any], request: Request, report: Mapping, basis: Mapping | None) -> Applied:
    """Steps 4 to 7 for one review-v2 response; ``basis`` is the session's current one.

    The response's basis becomes current when it differs, and every done record
    under another basis becomes stale (a finished mark stays). A response
    without a v3 basis leaves the session without one, and every done record
    becomes stale. Then the record at ``request.index``, if it still has the
    sent cores and inputs, and is not already current, takes the compact
    result: choices are kept (confirmed) or unconfirmed by step 7, never
    dropped, and the record is done only when its confirmed choices are
    exactly the resolutions that were sent."""
    new_basis = basis_of(report)
    current = basis if same_basis(basis, new_basis) else new_basis
    parts = parts_of(payload)
    segments = list(parts.segments)
    if not same_basis(basis, new_basis):
        for i, record in enumerate(segments):
            if record["status"] == "done" and not same_basis(record["basis"], current):
                segments[i] = {**record, "status": "stale"}
    parts = parts._replace(segments=segments)
    if request.index >= len(segments) or not _matches(segments[request.index], payload, request):
        return Applied(parts, current, "discarded")
    record = segments[request.index]
    if is_current(record, payload, current):
        return Applied(parts, current, "current")
    result = compact_result(report, request.source, request.target)
    if result is None:
        if record["status"] == "done":
            parts = _replace(parts, request.index, {**record, "status": "stale"})
        return Applied(parts, current, "not_storable")
    choices = []
    for choice in record["choices"]:
        kept = (request.realign or choice["confirmed"]) and _choice_holds(choice, result)
        choices.append(choice if choice["confirmed"] == kept else {**choice, "confirmed": kept})
    sent = sorted(json.dumps(r, sort_keys=True) for r in request.resolutions)
    confirmed = sorted(json.dumps(_resolution(c), sort_keys=True) for c in choices if c["confirmed"])
    status = "done" if confirmed == sent else "stale"
    stored = {**record, "source_hash": content_hash(request.source), "target_hash": content_hash(request.target),
              "status": status, "direction": request.direction, "alignment_input": request.alignment_input,
              "basis": new_basis, "result": result, "choices": choices}
    return Applied(_replace(parts, request.index, stored), current, "stored", again=status == "stale")


# ---- edits (SPEC "Writing", "Choices") ----

def _history_entry(choice: Mapping, source_hash: str) -> dict:
    return {"source_hash": source_hash, "source_span": choice["source_span"], "choice": choice["choice"],
            "candidate": choice["candidate"], "offered": choice["offered"]}


def _pending(record: Mapping, choices: list, **changes: Any) -> dict:
    return {**record, **changes, "status": "pending", "direction": None, "alignment_input": None, "basis": None,
            "result": None, "choices": [_unconfirmed(c) for c in choices]}


def apply_edit(parts: Parts, before: str, side: str, start: int, old_end: int, new_len: int) -> Parts:
    """One replacement the editor performed, as ``Session.apply_edit`` receives it;
    ``before`` is that side's text before the edit.

    Records the edit does not touch shift (``semantic_session._shift``). A
    record whose core it hits becomes pending and covers the edited range;
    its index leaves ``progress.completed``. Its choices become unconfirmed.
    On the source side, a choice whose span the edit overlaps moves to the
    history with the pre-edit core's hash, and the others keep their place.
    The ranges are provisional until ``normalize``."""
    new_end = start + new_len
    delta = new_end - old_end
    first, last = ("source_start", "source_end") if side == "source" else ("target_start", "target_end")
    segments, moved, hit_indexes = [], [], set()
    for index, record in enumerate(parts.segments):
        a, b = record[first], record[last]
        na, nb, hit = _shift(a, b, start, old_end, new_end)
        if not hit:
            segments.append(record if (na, nb) == (a, b) else {**record, first: na, last: nb})
            continue
        hit_indexes.add(index)
        na, nb = min(a, start), max(b, old_end) + delta
        choices = []
        for choice in record["choices"]:
            if side == "source":
                span = choice["source_span"]
                cs, ce, overlapped = _shift(a + span["start"], a + span["end"], start, old_end, new_end)
                if overlapped:
                    moved.append(_history_entry(choice, content_hash(before[a:b])))
                    continue
                if (cs - na, ce - na) != (span["start"], span["end"]):
                    choice = {**choice, "source_span": {"start": cs - na, "end": ce - na, "text": span["text"]}}
            choices.append(choice)
        segments.append(_pending(record, choices, **{first: na, last: nb}))
    if not hit_indexes:
        return parts._replace(segments=segments)
    completed = [i for i in parts.progress["completed"] if i not in hit_indexes]
    progress = parts.progress if len(completed) == len(parts.progress["completed"]) else {
        "current": parts.progress["current"], "completed": completed}
    return Parts(segments, [*parts.history, *moved] if moved else parts.history, progress)


# ---- normalization (SPEC "Writing", "Runtime" step 2) ----

def _check_history(history: list, check_limits: bool) -> None:
    if check_limits and len(history) > MAX_SEGMENT_CHOICE_HISTORY:
        raise manuscript._too_many()


def without_records(payload: Mapping[str, Any], *, check_limits: bool = True) -> Parts:
    """Leave long mode, or save without records by the user's choice: every choice
    moves to the history with its record's source core hash."""
    parts, source = parts_of(payload), payload["source"]
    moved = [_history_entry(choice, content_hash(source[r["source_start"]:r["source_end"]]))
             for r in parts.segments for choice in r["choices"]]
    history = [*parts.history, *moved] if moved else parts.history
    _check_history(history, check_limits)
    if not parts.segments and parts.progress == _empty_progress():
        return parts
    return Parts([], history, _empty_progress())


def _new_record(index: int, core: tuple, hashes: tuple[str, str]) -> dict:
    (a, b), (c, d) = core
    return {"index": index, "segmentation_version": SEGMENTATION_VERSION, "source_start": a, "source_end": b,
            "target_start": c, "target_end": d, "source_hash": hashes[0], "target_hash": hashes[1],
            "status": "pending", "direction": None, "alignment_input": None, "basis": None, "result": None,
            "choices": []}


def normalize(payload: Mapping[str, Any], pairs: Sequence, basis: Mapping | None = None, *,
              check_limits: bool = True) -> Parts:
    """The parts mapped to ``pairs``, the current segmentation-v1 pairing of the payload.

    In long mode there is one record per pair, in order. Records map by position
    when the counts and all core ranges are equal; otherwise each pair takes
    the first unused record with both hashes equal (kept), or else with the
    source hash equal (choices kept unconfirmed, no result), and every other
    pair gets a new pending record. Then: a changed core, a changed direction or
    a pair that is not ready makes a record pending; a changed alignment input,
    or a basis that differs from ``basis`` when one is given, makes a done
    record stale. A choice that no longer reads the same at its place, or whose
    record found no pair, moves by absolute position into the pair whose
    source core holds it and reads the same, unconfirmed; failing that, it goes
    to the history. ``progress`` follows the mapping. Outside long mode, see
    ``without_records``.

    Idempotent: normalizing a normalized payload returns equal values, with the
    unchanged records shared. With ``check_limits`` (the default), more than
    512 records or history entries raise ``WorkfileError``
    ``workfile_too_many_entries``; without it they are kept, and saving fails
    with that code."""
    source, target, direction = payload["source"], payload["target"], payload["direction"]
    if not long_mode(source, target):
        return without_records(payload, check_limits=check_limits)
    if check_limits and len(pairs) > MAX_SEGMENTS:
        raise manuscript._too_many()
    parts = parts_of(payload)
    old = parts.segments
    cores = [((p.source.core_start, p.source.core_end), (p.target.core_start, p.target.core_end)) for p in pairs]
    hashes = [(content_hash(source[a:b]), content_hash(target[c:d])) for (a, b), (c, d) in cores]
    count = len(pairs)
    match: list[int | None] = [None] * count
    if len(old) == count and all(_cores(r) == core for r, core in zip(old, cores, strict=True)):
        match = list(range(count))
    else:
        # A pending record has no result, so its identity is what its range holds now.
        def keys(record: Mapping) -> tuple[str, str]:
            if record["status"] != "pending":
                return record["source_hash"], record["target_hash"]
            (a, b), (c, d) = _cores(record)
            return content_hash(source[a:b]), content_hash(target[c:d])
        both: dict[tuple, deque] = {}
        by_source: dict[str, deque] = {}
        for i, record in enumerate(old):
            key = keys(record)
            both.setdefault(key, deque()).append(i)
            by_source.setdefault(key[0], deque()).append(i)
        used: set[int] = set()

        def take(queue: deque | None) -> int | None:
            while queue and queue[0] in used:
                queue.popleft()
            return queue.popleft() if queue else None
        # Every segment first looks for both hashes; only those still unmatched
        # then look for the source hash.
        for table, key in ((both, lambda h: h), (by_source, lambda h: h[0])):
            for j in range(count):
                if match[j] is None:
                    i = take(table.get(key(hashes[j])))
                    if i is not None:
                        used.add(i)
                        match[j] = i

    pool: list[tuple[Mapping, dict]] = []   # (record it was in, choice), placed by absolute position below
    records: list[dict] = []
    for j, i in enumerate(match):
        core, now = cores[j], hashes[j]
        if i is None:
            records.append(_new_record(j, core, now))
            continue
        record = old[i]
        (a, b), (c, d) = core
        choices, lost_confirmed = [], False
        for choice in record["choices"]:
            span = choice["source_span"]
            if source[a + span["start"]:a + span["end"]] == span["text"] and span["end"] <= b - a:
                choices.append(choice)
            else:
                pool.append((record, choice))
                lost_confirmed = lost_confirmed or choice["confirmed"]
        status = record["status"]
        same_content = status != "pending" and (record["source_hash"], record["target_hash"]) == now
        if not same_content or pairs[j].status != STATUS_READY or record["direction"] != direction:
            if status != "pending" or len(choices) != len(record["choices"]):
                record = _pending(record, choices)
        else:
            if status == "done" and (lost_confirmed or (basis is not None and not same_basis(record["basis"], basis))
                                     or alignment_input(payload["alignments"], source, target, core[0], core[1])
                                     != record["alignment_input"]):
                status = "stale"
            if status != record["status"] or len(choices) != len(record["choices"]):
                record = {**record, "status": status, "choices": choices}
        updates = {"index": j, "source_start": a, "source_end": b, "target_start": c, "target_end": d,
                   "source_hash": now[0], "target_hash": now[1]}
        if any(record[key] != value for key, value in updates.items()):
            record = {**record, **updates}
        records.append(record)

    # Choices of records no pair took join those that no longer read the same where they were.
    taken = {i for i in match if i is not None}
    pool.extend((record, choice) for i, record in enumerate(old) if i not in taken for choice in record["choices"])
    moved = []
    if pool:
        starts = [core[0][0] for core in cores]
        for record, choice in pool:
            span = choice["source_span"]
            begin, end = record["source_start"] + span["start"], record["source_start"] + span["end"]
            j = bisect.bisect_right(starts, begin) - 1
            placed = False
            if j >= 0 and cores[j][0][0] <= begin and end <= cores[j][0][1] and source[begin:end] == span["text"]:
                into = records[j]
                place = (begin - cores[j][0][0], end - cores[j][0][0])
                if len(into["choices"]) < MAX_SEGMENT_CHOICES and all(_place(c) != place for c in into["choices"]):
                    relocated = {**choice, "source_span": {"start": place[0], "end": place[1], "text": span["text"]},
                                 "confirmed": False}
                    records[j] = {**into, "choices": [*into["choices"], relocated]}
                    placed = True
            if not placed:
                moved.append(_history_entry(choice, content_hash(
                    source[record["source_start"]:record["source_end"]])))
    history = [*parts.history, *moved] if moved else parts.history
    _check_history(history, check_limits)
    return Parts(_shared(old, records), history, _map_progress(parts.progress, old, match, records, cores))


def _shared(old: list, records: list) -> list:
    if len(old) == len(records) and all(a is b for a, b in zip(old, records, strict=True)):
        return old
    return records


def _map_progress(progress: Mapping, old: list, match: list, records: list, cores: list) -> dict:
    new_index = {i: j for j, i in enumerate(match) if i is not None}
    completed = sorted(new_index[i] for i in progress["completed"]
                       if i in new_index and records[new_index[i]]["status"] in ("done", "stale"))
    current = progress["current"]
    if current is not None:
        if current in new_index:
            current = new_index[current]
        elif current < len(old) and records:
            # The record went away: stay at the pair at or after its old place.
            begin = old[current]["source_start"]
            current = next((j for j, core in enumerate(cores) if core[0][1] > begin), len(records) - 1)
        else:
            current = None
    value = {"current": current, "completed": completed}
    return progress if value == progress else value


# ---- history and progress (SPEC "Runtime" step 8, "Progress") ----

def restorable(payload: Mapping[str, Any]) -> list[tuple[int, int]]:
    """``(history position, record index)`` pairs that ``restore`` accepts: the
    record's source core has the entry's hash, the span reads the same there,
    and the record has no choice at that span."""
    parts, source = parts_of(payload), payload["source"]
    by_hash: dict[str, list[int]] = {}
    for i, record in enumerate(parts.segments):
        by_hash.setdefault(content_hash(source[record["source_start"]:record["source_end"]]), []).append(i)
    out = []
    for position, entry in enumerate(parts.history):
        span = entry["source_span"]
        for i in by_hash.get(entry["source_hash"], ()):
            record = parts.segments[i]
            core = source[record["source_start"]:record["source_end"]]
            if (span["end"] <= len(core) and core[span["start"]:span["end"]] == span["text"]
                    and all(_place(c) != (span["start"], span["end"]) for c in record["choices"])):
                out.append((position, i))
    return out


def restore(payload: Mapping[str, Any], position: int, index: int) -> Parts:
    """Move a history entry into record ``index`` as an unconfirmed choice."""
    if (position, index) not in restorable(payload):
        raise ValueError("this history entry cannot be restored into this record")
    parts = parts_of(payload)
    entry, record = parts.history[position], parts.segments[index]
    if len(record["choices"]) >= MAX_SEGMENT_CHOICES:
        raise manuscript._too_many()
    choice = {"source_span": entry["source_span"], "choice": entry["choice"], "candidate": entry["candidate"],
              "offered": entry["offered"], "confirmed": False}
    parts = _replace(parts, index, {**record, "choices": [*record["choices"], choice]})
    return parts._replace(history=[e for k, e in enumerate(parts.history) if k != position])


def set_current(parts: Parts, index: int | None) -> Parts:
    if index is not None and not 0 <= index < len(parts.segments):
        raise ValueError("no such segment")
    if index == parts.progress["current"]:
        return parts
    return parts._replace(progress={"current": index, "completed": parts.progress["completed"]})


def set_finished(parts: Parts, index: int, finished: bool) -> Parts:
    """Mark or unmark a finished segment; only a done or stale record can be finished."""
    completed = set(parts.progress["completed"])
    if finished and parts.segments[index]["status"] not in ("done", "stale"):
        raise ValueError("a pending segment cannot be finished")
    if (index in completed) == finished:
        return parts
    completed.symmetric_difference_update({index})
    return parts._replace(progress={"current": parts.progress["current"], "completed": sorted(completed)})
