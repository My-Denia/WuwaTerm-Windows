"""Resumable long-manuscript term review: the v3 to v5 segment records at runtime.

The contract is the public Web ``contracts/manuscript-v3/SPEC.md``; v4 raises the
same records' caps without changing their shape
(``contracts/manuscript-v4/SPEC.md``), v5 adds review features and gap-tolerant
records (``contracts/manuscript-v5/SPEC.md``), and quoted section names refer
to v3. Pure Python: no Qt, no HTTP.

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
4. its basis is the same as the session's current basis in all six values;
5. its review features (v5 SPEC "Review features") equal the session's: the
   ones the next request asks Core for.

1 to 3 make a *reuse candidate* (``reuse_candidate``); with 4 and 5 the result
is *current* (``is_current``). Before the session has a basis, or while the
session's features are unknown (``None``: the service capabilities are not
known yet), nothing is current: a candidate whose features do not contradict
the session shows as saved, no resolutions are sent, and the first check
(``next_index``) is the probe that supplies the basis. Every other record is
checked again (``plan_request``, ``apply_response``).

A record's features are its runtime ``features`` key, set when a response is
stored (never written: the manuscript writer turns them into the file's
``review_features``); a record read from a file has that file's
``review_features`` (the payload key of that name), and a v1 to v4 file has
none. So a v4 file opened against a service that lists
``ambiguous-mention-v1`` is checked again once, segment by segment.

A record is ``done`` only while its result was computed with exactly its
confirmed choices as resolutions. When the confirmed choices change after the
result, the record becomes ``stale``. Unconfirmed choices are never sent and
never change the status.

After an edit of the target alone (SPEC "Runtime" step 6, last paragraph) a
record loses its result, but its choices keep their ``confirmed`` values until
the re-check, and the record remembers what its result was computed for
(``_Prior``, under the record key ``prior``). The re-check then confirms only
what was confirmed before, and only while it still matches its findings: a
choice that was unconfirmed before the edit stays unconfirmed even when it
matches. These are runtime values: ``manuscript._order_record`` never writes
``prior`` and writes the choices of a pending record unconfirmed. Any other
change that makes a record pending (a source edit, a new direction) leaves its
choices unconfirmed, and the re-check realigns them by step 7: a kept choice
becomes confirmed.

Order of use: ``apply_edit`` for every replacement the editor performed, then
``normalize`` against the current pairing before planning, display or save.
The planner and the request functions expect normalized parts: one record per
pair, in order.
"""

from __future__ import annotations

import bisect
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, NamedTuple

from . import manuscript, strings
from .manuscript import (ALIGNMENT_AUTOMATIC, MAX_CORE_SCALARS, MAX_SEGMENT_CHOICE_HISTORY, MAX_SEGMENTS,
                         MAX_V4_CHOICES, REVIEW_FEATURE_VALUES, SEGMENTATION_VERSION, WorkfileError,
                         is_ambiguous_mention, record_features)
from .segmentation import STATUS_READY, _sentence_pieces, pair_is_sendable
from . import semantic_session as sessions
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


def review_feature_set(tokens: Any) -> tuple[str, ...]:
    """The sorted, known review features of a list (``REVIEW_FEATURE_VALUES``);
    anything else is dropped."""
    return tuple(sorted({t for t in tokens or () if isinstance(t, str) and t in REVIEW_FEATURE_VALUES}))


def session_review_features(capabilities: Collection[str] | None) -> tuple[str, ...] | None:
    """The review features a session asks Core for: ``ambiguous-mention-v1``
    when the service lists it; ``None`` while the capabilities are unknown."""
    return None if capabilities is None else review_feature_set(capabilities)


def features_of(record: Mapping, payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The features a record's result was made with (see the module docstring)."""
    return review_feature_set(record_features(record, payload.get("review_features") or ()))


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
        # The lists themselves, so an unchanged part keeps its identity.
        value.update(segments=parts.segments, segment_choice_history=parts.history,
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


def _overlap(a: int, b: int, c: int, d: int) -> int:
    """Shared scalars of the spans ``[a, b)`` and ``[c, d)``; 0 when disjoint."""
    return max(0, min(b, d) - max(a, c))


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
    count over its cap (128), a core outside 1 to 2,000 scalars, or anything
    else the v4 reader would reject."""
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
            item: dict[str, Any] = {
                "verdict": finding["verdict"], "source": [source_span["start"], source_span["end"]],
                "target": None if target_span is None else [target_span["start"], target_span["end"]],
                "candidates": [{"candidate_id": c["candidate_id"], "zh": c["zh"], "en": c["en"],
                                "category": c["category"]} for c in finding["candidates"]],
                "candidates_truncated": finding["candidates_truncated"],
            }
            if "reason" in finding:
                item["reason"] = finding["reason"]
            findings.append(item)
        result = {"truncated": report["truncated"], "evaluated": report["coverage"]["evaluated"],
                  "not_evaluated": report["coverage"]["not_evaluated"], "findings": findings}
        # v5 limits: an ``ambiguous_mention`` finding is stored only because the
        # request asked for it (the API client refuses it otherwise).
        manuscript._check_result_shape(result, v5=True)
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
        item: dict[str, Any] = {
            "id": mention_id(source_span), "verdict": finding["verdict"], "rule_id": TERM_PAIR_RULE,
            "source_span": source_span, "target_span": span(finding["target"], target_core),
            "candidates": [{"zh": x["zh"], "en": x["en"], "category": x["category"], "sources": [],
                            "candidate_id": x["candidate_id"]} for x in finding["candidates"]],
            "candidates_truncated": finding["candidates_truncated"],
        }
        if "reason" in finding:
            item["reason"] = finding["reason"]
        findings.append(item)
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
    if len(choices) > MAX_V4_CHOICES:
        raise manuscript._too_many()
    changed = (old is not None and old["confirmed"]) or (new is not None and new["confirmed"])
    status = "stale" if record["status"] == "done" and changed else record["status"]
    return _replace(parts, index, {**record, "choices": choices, "status": status})


# ---- reuse and planning (SPEC "Runtime" steps 3 to 5) ----

def content_current(record: Mapping, payload: Mapping[str, Any]) -> bool:
    """Whether a result describes the current text and direction at its cores."""
    source, target = payload["source"], payload["target"]
    (a, b), (c, d) = _cores(record)
    return (record["direction"] == payload["direction"]
            and 0 <= a <= b <= len(source) and 0 <= c <= d <= len(target)
            and content_hash(source[a:b]) == record["source_hash"]
            and content_hash(target[c:d]) == record["target_hash"])


def reuse_candidate(record: Mapping, payload: Mapping[str, Any]) -> bool:
    """Rule items 1 to 3 (step 3), recomputed from the current text."""
    return (record["status"] == "done" and content_current(record, payload)
            and _input_now(record, payload) == record["alignment_input"])


@dataclass(frozen=True)
class RecordView:
    """A result's freshness and whether it can be displayed or needs a check."""

    state: str  # pending, saved, current, stale
    content_current: bool

    @property
    def needs_check(self) -> bool:
        return self.state in ("pending", "stale")


def record_view(record: Mapping, payload: Mapping[str, Any], basis: Mapping | None,
                features: Sequence[str] | None = ()) -> RecordView:
    """Derive the same freshness for display, grouping and review planning.
    ``features``: the session's review features, ``None`` while unknown."""
    if record["result"] is None:
        return RecordView("pending", False)
    content = content_current(record, payload)
    if (record["status"] != "done" or not content
            or _input_now(record, payload) != record["alignment_input"]):
        return RecordView("stale", content)
    if features is not None and features_of(record, payload) != review_feature_set(features):
        return RecordView("stale", content)
    if basis is None or features is None:
        return RecordView("saved", content)
    return RecordView("current" if same_basis(record["basis"], basis) else "stale", content)


class RecordViews:
    """Cache derived views while immutable records and their inputs are unchanged."""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._views: dict[int, tuple[Mapping, RecordView]] = {}

    def get(self, record: Mapping, payload: Mapping[str, Any], basis: Mapping | None,
            features: Sequence[str] | None = ()) -> RecordView:
        key = (payload["source"], payload["target"], payload["alignments"], payload["direction"], basis,
               None if features is None else tuple(features), payload.get("review_features"))
        if (self._key is None or any(a is not b for a, b in zip(self._key[:5], key[:5]))
                or self._key[3] != key[3] or self._key[5:] != key[5:]):
            self._key = key
            self._views.clear()
        hit = self._views.get(id(record))
        if hit is None or hit[0] is not record:
            hit = record, record_view(record, payload, basis, features)
            self._views[id(record)] = hit
        return hit[1]


def is_current(record: Mapping, payload: Mapping[str, Any], basis: Mapping | None,
               features: Sequence[str] | None = ()) -> bool:
    """The whole reuse rule (step 5); never while the session has no basis."""
    return record_view(record, payload, basis, features).state == "current"


def needs_check(record: Mapping, payload: Mapping[str, Any], basis: Mapping | None,
                features: Sequence[str] | None = ()) -> bool:
    """Without a basis yet: not a reuse candidate. With one: not current."""
    return record_view(record, payload, basis, features).needs_check


@dataclass(frozen=True)
class TermPlan:
    due: tuple[int, ...]
    probe: int | None
    order: tuple[int, ...]


def term_plan(payload: Mapping[str, Any], pairs: Sequence, basis: Mapping | None,
              skip: Collection[int] = (), views: RecordViews | None = None,
              features: Sequence[str] | None = ()) -> TermPlan:
    """Plan sendable term checks and the single first live-basis probe."""
    parts = parts_of(payload)
    if len(parts.segments) != len(pairs) or any(
            _cores(r) != ((p.source.core_start, p.source.core_end), (p.target.core_start, p.target.core_end))
            for r, p in zip(parts.segments, pairs, strict=True)):
        raise ValueError("normalize the parts to the current pairing first")
    source, target = payload["source"], payload["target"]
    views = views if views is not None else RecordViews()
    ready = [i for i, pair in enumerate(pairs) if i not in skip and pair_is_sendable(pair, source, target)]
    due = tuple(i for i in ready if views.get(parts.segments[i], payload, basis, features).needs_check)
    probe = None
    if basis is None and ready:
        current = parts.progress["current"]
        probe = (current if current in due else due[0] if due
                 else current if current in ready else ready[0])
    order = due if probe is None else (probe, *(i for i in due if i != probe))
    return TermPlan(due, probe, order)


def next_index(payload: Mapping[str, Any], pairs: Sequence, basis: Mapping | None,
               skip: Collection[int] = (), features: Sequence[str] | None = ()) -> int | None:
    """The next segment to check among the sendable pairs not in ``skip``.

    With a basis: the first segment that needs a check. Without one (step 4,
    the probe): the segment at ``progress.current`` if it needs a check, else
    the first that does, else ``progress.current`` or the first sendable
    segment, because the probe must still supply the basis."""
    plan = term_plan(payload, pairs, basis, skip, features=features)
    return plan.order[0] if plan.order else None


def pair_key(pair: Any, source: str, target: str) -> tuple:
    """Occurrence and text version, distinguishing identical paragraphs."""
    a, b = pair.source.core_start, pair.source.core_end
    c, d = pair.target.core_start, pair.target.core_end
    return a, b, c, d, source[a:b], target[c:d]


@dataclass(frozen=True)
class NextReview:
    index: int
    term: bool
    semantic: bool


def next_review(payload: Mapping[str, Any], pairs: Sequence, term_basis: Mapping | None,
                session: sessions.Session, semantic_basis: sessions.Basis, *, terms: bool,
                attempted: Collection[tuple] = (), views: RecordViews | None = None,
                features: Sequence[str] | None = ()) -> NextReview | None:
    """Combine term and semantic work, giving the first term-basis probe priority."""
    source, target = payload["source"], payload["target"]
    skip = {i for i, pair in enumerate(pairs) if pair_key(pair, source, target) in attempted}
    views = views if views is not None else RecordViews()
    plan = term_plan(payload, pairs, term_basis, skip, views, features) if terms else None
    term = plan.order[0] if plan and plan.order else None
    if plan is not None and plan.probe is not None:
        index = plan.probe
    else:
        semantic = next((i for i, pair in enumerate(pairs) if i not in skip and pair.status == STATUS_READY
                         and semantic_basis.needs(session.result_for(pair, source, target))), None)
        indexes = [i for i in (term, semantic) if i is not None]
        if not indexes:
            return None
        index = min(indexes)
    need_term = terms and (term_basis is None or views.get(
        parts_of(payload).segments[index], payload, term_basis, features).needs_check)
    need_semantic = semantic_basis.needs(session.result_for(pairs[index], source, target))
    return NextReview(index, need_term, need_semantic)


def review_status(pair: Any, term: str, payload: Mapping[str, Any], session: sessions.Session,
                  semantic_basis: sessions.Basis, failure: str | None) -> str:
    """Combine term, semantic and failure state for one segment."""
    if pair.status != STATUS_READY:
        return pair.status
    result = session.result_for(pair, payload["source"], payload["target"])
    if term == "current" and not semantic_basis.needs(result):
        return "reviewed"
    if failure is not None:
        return failure
    return term if term in ("saved", "stale", "failed") else "pending"


def choice_reconfirmable(record: Mapping, choice: Mapping, basis: Mapping | None) -> bool:
    """An unconfirmed choice can be renewed only against a matching finding."""
    if choice["confirmed"] or basis is None:
        return False
    span = choice["source_span"]
    finding = _finding_at(record["result"], span["start"], span["end"])
    return finding is not None and (choice["choice"] == NOT_A_TERM or choice["candidate"] in finding["candidates"])


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
    features: tuple[str, ...] = ()  # the review features asked for; stored with the result


def plan_request(payload: Mapping[str, Any], index: int, basis: Mapping | None, *,
                 allow_resolutions: bool = True, features: Sequence[str] | None = ()) -> Request:
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
                   realign=not carry, features=review_feature_set(features))


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
    if is_current(record, payload, current, request.features):
        return Applied(parts, current, "current")
    result = compact_result(report, request.source, request.target)
    if result is None:
        if record["status"] == "done":
            parts = _replace(parts, request.index, {**record, "status": "stale"})
        return Applied(parts, current, "not_storable")
    prior = record.get(PRIOR)
    target_only = (prior is not None and prior.source_hash == content_hash(request.source)
                   and prior.direction == request.direction and same_basis(prior.basis, basis))
    choices = []
    for choice in record["choices"]:
        # Step 6, last paragraph: after a target-only edit nothing becomes confirmed that was not.
        confirmable = choice["confirmed"] if target_only else request.realign or choice["confirmed"]
        kept = confirmable and _choice_holds(choice, result)
        choices.append(choice if choice["confirmed"] == kept else {**choice, "confirmed": kept})
    sent = sorted(json.dumps(r, sort_keys=True) for r in request.resolutions)
    confirmed = sorted(json.dumps(_resolution(c), sort_keys=True) for c in choices if c["confirmed"])
    status = "done" if confirmed == sent else "stale"
    stored = {**record, "source_hash": content_hash(request.source), "target_hash": content_hash(request.target),
              "status": status, "direction": request.direction, "alignment_input": request.alignment_input,
              "basis": new_basis, "result": result, "choices": choices}
    stored.pop(PRIOR, None)
    # A record states its features only where they are not the file's own, so
    # memory and the written file stay alike in the usual case.
    stored.pop("features", None)
    if request.features != review_feature_set(payload.get("review_features") or ()):
        stored["features"] = request.features
    return Applied(_replace(parts, request.index, stored), current, "stored", again=status == "stale")


# ---- edits (SPEC "Writing", "Choices") ----

def _history_entry(choice: Mapping, source_hash: str) -> dict:
    return {"source_hash": source_hash, "source_span": choice["source_span"], "choice": choice["choice"],
            "candidate": choice["candidate"], "offered": choice["offered"]}


class _Prior(NamedTuple):
    """What a record's result was computed for, before an edit of the target alone dropped it."""

    source_hash: str
    direction: str
    basis: dict


PRIOR = "prior"   # runtime only: ``manuscript._order_record`` writes named keys, never this one


def _pending(record: Mapping, choices: list, *, target_only: bool = False, **changes: Any) -> dict:
    """The record without its result. ``target_only``: only the target changed, so
    the choices keep their confirmed values and the record keeps (or takes)
    its ``prior``; otherwise the choices become unconfirmed and no ``prior``
    is kept."""
    prior = None
    if target_only:
        prior = record.get(PRIOR) or (None if record["status"] == "pending" else _Prior(
            record["source_hash"], record["direction"], record["basis"]))
    out = {**{k: v for k, v in record.items() if k != PRIOR}, **changes, "status": "pending", "direction": None,
           "alignment_input": None, "basis": None, "result": None,
           "choices": list(choices) if target_only else [_unconfirmed(c) for c in choices]}
    if prior is not None:
        out[PRIOR] = prior
    return out


def apply_edit(parts: Parts, before: str, side: str, start: int, old_end: int, new_len: int) -> Parts:
    """One replacement the editor performed, as ``Session.apply_edit`` receives it;
    ``before`` is that side's text before the edit.

    Records the edit does not touch shift (``semantic_session._shift``). A
    record whose core it hits becomes pending and covers the edited range;
    its index leaves ``progress.completed``. On the target side its choices keep
    their confirmed values (see the module docstring); on the source side they
    become unconfirmed.
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
        segments.append(_pending(record, choices, target_only=side == "target", **{first: na, last: nb}))
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


def _new_record(index: int, core: tuple, hashes: tuple[str, str], version: str = SEGMENTATION_VERSION) -> dict:
    (a, b), (c, d) = core
    return {"index": index, "segmentation_version": version, "source_start": a, "source_end": b,
            "target_start": c, "target_end": d, "source_hash": hashes[0], "target_hash": hashes[1],
            "status": "pending", "direction": None, "alignment_input": None, "basis": None, "result": None,
            "choices": []}


def normalize(payload: Mapping[str, Any], pairs: Sequence, basis: Mapping | None = None, *,
              check_limits: bool = True, version: str = SEGMENTATION_VERSION) -> Parts:
    """The parts mapped to ``pairs``, the current pairing of the payload, and
    labelled ``version`` (``segmentation-v1``, or ``segmentation-v1-gaps`` when
    explicit alignments leave text uncovered: v5 SPEC "Gap-tolerant segment
    records"). The label says how the ranges were cut; it never decides reuse.

    In long mode there is one record per pair, in order. Records map by position
    when the counts and all core ranges are equal; otherwise matching runs in
    three passes (see ``rank`` in the body): (1) exact source occurrence
    (``source_start``, ``source_end``, ``source_hash``), (2) globally best-ranked
    ``(old, pair)`` pairs with both hashes equal (kept), and (3) globally
    best-ranked pairs with only the source hash equal (choices kept unconfirmed,
    no result); every other pair gets a new pending record. Then:
    a changed core, a changed direction or a pair that is not ready makes a
    record pending; a changed alignment input, or a basis that differs from
    ``basis`` when one is given, makes a done record stale. A choice that no
    longer reads the same at its place, or whose record found no pair, moves by
    absolute position into the pair whose source core holds it and reads the
    same, unconfirmed; failing that, it goes to the history. ``progress``
    follows the mapping. Outside long mode, see ``without_records``.

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
        old_keys = [keys(record) for record in old]
        old_cores = [_cores(record) for record in old]
        used: set[int] = set()
        # Pass 1: exact source occurrence (source_start, source_end, source_hash).
        for j in range(count):
            sa, sb = cores[j][0]
            sh = hashes[j][0]
            for i, (rc, rk) in enumerate(zip(old_cores, old_keys, strict=True)):
                if i not in used and rc[0] == (sa, sb) and rk[0] == sh:
                    match[j] = i
                    used.add(i)
                    break

        def rank(i: int, j: int) -> tuple[int, int, int, int, int]:
            """How well record ``i`` explains pair ``j`` (highest 5-tuple wins).

            Equal hashes make records interchangeable by content, so stored
            order cannot tell duplicate paragraphs apart: positions still can.
            Pass 2 and Pass 3 rank every candidate ``(i, j)`` globally by source
            overlap, target overlap, negative source start delta, ``-j``, and
            ``-i``, falling back to earliest-pair / earliest-record order only
            when all positional signals tie."""
            (a, b), (c, d) = old_cores[i]
            (pa, pb), (pc, pd) = cores[j]
            return _overlap(a, b, pa, pb), _overlap(c, d, pc, pd), -abs(a - pa), -j, -i

        # Pass 2: both hashes equal (global best pair); Pass 3: source hash equal only.
        for pred in (lambda i, j: old_keys[i] == hashes[j], lambda i, j: old_keys[i][0] == hashes[j][0]):
            candidates = [
                (rank(i, j), j, i)
                for j in range(count) if match[j] is None
                for i in range(len(old)) if i not in used and pred(i, j)
            ]
            candidates.sort(key=lambda item: item[0], reverse=True)
            for _, j, i in candidates:
                if match[j] is None and i not in used:
                    match[j] = i
                    used.add(i)

    pool: list[tuple[Mapping, dict]] = []   # (record it was in, choice), placed by absolute position below
    records: list[dict] = []
    for j, i in enumerate(match):
        core, now = cores[j], hashes[j]
        if i is None:
            records.append(_new_record(j, core, now, version))
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
            prior = record.get(PRIOR)
            # A new direction or source core ends what a pending record remembers (it is pending, unconfirmed).
            gone = prior is not None and (prior.direction != direction or prior.source_hash != now[0])
            if status != "pending" or len(choices) != len(record["choices"]) or gone:
                only_target = (status != "pending" and record["direction"] == direction
                               and record["source_hash"] == now[0] and record["target_hash"] != now[1])
                record = _pending(record, choices, target_only=only_target)
        else:
            if status == "done" and (lost_confirmed or (basis is not None and not same_basis(record["basis"], basis))
                                     or alignment_input(payload["alignments"], source, target, core[0], core[1])
                                     != record["alignment_input"]):
                status = "stale"
            if status != record["status"] or len(choices) != len(record["choices"]):
                record = {**record, "status": status, "choices": choices}
        updates = {"index": j, "segmentation_version": version, "source_start": a, "source_end": b,
                   "target_start": c, "target_end": d, "source_hash": now[0], "target_hash": now[1]}
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
                if len(into["choices"]) < MAX_V4_CHOICES and all(_place(c) != place for c in into["choices"]):
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


def carry_results(parts: Parts, later: Parts) -> Parts:
    """Undo or redo: ``parts`` is the restored snapshot, ``later`` what it replaces.

    A restored record without a done result takes the done result of a later
    record with the same stored hashes and the same choices, so a check made
    after the snapshot is not lost. Call ``normalize`` afterwards: it compares
    the hashes with the current cores again, so a carried result is kept only
    where the content really is the one it was computed for."""
    done: dict[tuple[str, str], list[dict]] = {}
    for record in later.segments:
        if record["status"] == "done":
            done.setdefault((record["source_hash"], record["target_hash"]), []).append(record)
    if not done:
        return parts
    segments = []
    for record in parts.segments:
        if record["status"] != "done":
            match = next((r for r in done.get((record["source_hash"], record["target_hash"]), ())
                          if r["choices"] == record["choices"]), None)
            if match is not None:
                record = {**match, **{key: record[key] for key in (
                    "index", "source_start", "source_end", "target_start", "target_end")}}
        segments.append(record)
    return parts._replace(segments=_shared(parts.segments, segments))


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
    if len(record["choices"]) >= MAX_V4_CHOICES:
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


# ---- term occurrence grouping & per-occurrence context --------------------

_FLAT_WS = re.compile(r"[\r\n\t]+")
_CONTEXT_WINDOW = 34
_GROUP_STATUS_PRIORITY = {
    "inconsistent": 0,
    "pending": 1,
    "not_evaluated": 2,
    "confirmed": 3,
}
_VERDICT_LABELS = {
    "verified_constraint": strings.MANUSCRIPT_VERDICT_VERIFIED,
    "confirmed_conflict": strings.MANUSCRIPT_VERDICT_CONFLICT,
    "needs_review": strings.MANUSCRIPT_VERDICT_NEEDS_REVIEW,
    "not_evaluated": strings.MANUSCRIPT_VERDICT_NOT_EVALUATED,
}


def localized_not_evaluated_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    return strings.MANUSCRIPT_NOT_EVALUATED_REASON.get(reason, reason)


def localized_not_evaluated_detail(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason in strings.MANUSCRIPT_NOT_EVALUATED_DETAIL:
        return strings.MANUSCRIPT_NOT_EVALUATED_DETAIL[reason]
    return strings.MANUSCRIPT_NOT_EVALUATED_UNKNOWN.format(reason=reason)


def localized_verdict_label(verdict: str, reason: str | None = None) -> str:
    if verdict == "not_evaluated" and reason:
        return strings.MANUSCRIPT_VERDICT_NOT_EVALUATED_REASON.format(
            reason=localized_not_evaluated_reason(reason)
        )
    return _VERDICT_LABELS.get(verdict, verdict)


def _en_singular_forms(word: str) -> set[str]:
    w = word.lower()
    forms = {w}
    if len(w) > 3 and w.endswith("ies"):
        forms.add(w[:-3] + "y")
    if len(w) > 3 and w.endswith("ves"):
        forms.add(w[:-3] + "f")
        forms.add(w[:-3] + "fe")
    if len(w) > 2 and w.endswith("es"):
        forms.add(w[:-2])
    if len(w) > 1 and w.endswith("s") and not w.endswith("ss"):
        forms.add(w[:-1])
    return forms


def _english_surface_tokens(surface: str) -> tuple[tuple[str, ...], frozenset[str]] | None:
    if (not surface
            or not any(ch.isascii() and ch.isalpha() for ch in surface)
            or any(ord(ch) >= 0x3000 for ch in surface)):
        return None
    words = surface.strip().split()
    if not words:
        return None
    prefix = tuple(w.lower() for w in words[:-1])
    return prefix, frozenset(_en_singular_forms(words[-1]))


def _attention(choice: Mapping[str, Any] | None, freshness: str, verdict: str) -> str:
    """What one place still needs, as Web's ``attention`` (``lib/long-review.js``):
    ``settled``, ``pending``, ``conflict``, ``not_evaluated`` or ``ok``.

    A place is decided when it has a confirmed choice and its segment is not
    stale; a place whose choice waits for the next check, or that sits in a
    stale segment, needs a check; a conflict needs a decision. ``ok`` is a
    verified place with nothing to do.

    ``saved`` (``_record_freshness``: a reopened result whose basis this session
    has not checked against the live server yet) is never ``settled`` or ``ok``:
    nothing about a saved result is verified here, so every saved place needs
    that check. A conflict it records still reads as a conflict, because a
    recorded conflict is a decision to make, never a settled place.
    """
    if freshness == "saved":
        return "conflict" if verdict == "confirmed_conflict" else "pending"
    if choice is not None and choice.get("confirmed") and freshness != "stale":
        return "settled"
    if choice is not None and not choice.get("confirmed"):
        return "pending"
    if freshness == "stale":
        return "pending"
    if verdict == "confirmed_conflict":
        return "conflict"
    if verdict == "needs_review":
        return "pending"
    if verdict == "not_evaluated":
        return "not_evaluated"
    return "ok"


def _needs_action(occurrence: Mapping[str, Any]) -> bool:
    return occurrence["attention"] in ("pending", "conflict")


def _unchecked_holding(
    surfaces: Sequence[str],
    unchecked: Sequence[tuple[int, str]],
    direction: str,
) -> list[int]:
    """The 1-based numbers of the unchecked segments whose source core holds one
    of these spellings: a substring for a non-English source, a whole word
    (case-insensitive, plural included) for an English one. Web
    ``uncheckedHolding``."""
    forms = [name for name in (surface.strip() for surface in surfaces) if name]
    if not forms or not unchecked:
        return []
    english = direction == "zh"
    if english:
        pattern = re.compile(
            r"(?<![^\W\d_])(?:" + "|".join(re.escape(name) for name in forms) + r")(?:es|s)?(?![^\W\d_])",
            re.IGNORECASE,
        )
        return sorted(index + 1 for index, core in unchecked if pattern.search(core))
    return sorted(index + 1 for index, core in unchecked if any(name in core for name in forms))


def group_matches_filter(group: Mapping[str, Any], key: str) -> bool:
    """Whether a status filter shows this group (Web ``groupMatch``).

    A filter shows a group that *holds a place in that state*, whatever the
    group's own status is: an ``inconsistent`` group with a ``needs_review``
    place is also pending work, so the ``pending`` filter must not hide it. A
    group that waits for a check of a segment without a result is pending even
    though none of its known places is.
    """
    if key == "all":
        return True
    if key == "inconsistent":
        return bool(group.get("inconsistent", group["status"] == "inconsistent"))
    if key == "pending":
        return group["pending_count"] > 0 or bool(group["unverified_segments"])
    if key == "not_evaluated":
        return group["not_evaluated_count"] > 0
    if key == "confirmed":
        return group["status"] == "confirmed"
    return True


def _context_snippet(
    core_text: str,
    hit: tuple[int, int] | None,
    *,
    source_core: str = "",
    source_hit: tuple[int, int] | None = None,
    window: int = _CONTEXT_WINDOW,
) -> str:
    """Compact ~28–40 scalar snippet around ``hit``, or paired sentence excerpt when ``hit`` is None."""
    if not core_text:
        return ""
    if hit is not None:
        a, b = hit
        a = max(0, min(a, len(core_text)))
        b = max(a, min(b, len(core_text)))
        hit_len = b - a
        pad = max(6, (window - hit_len) // 2)
        left = max(0, a - pad)
        right = min(len(core_text), b + pad)
        if left == 0:
            right = min(len(core_text), max(right, window))
        elif right == len(core_text):
            left = max(0, min(left, len(core_text) - window))
        # Avoid slicing ASCII words in half at the outer edges while keeping the hit intact.
        if left > 0 and core_text[left - 1].isascii() and core_text[left - 1].isalnum():
            while left < a and core_text[left].isascii() and core_text[left].isalnum():
                left += 1
        if right < len(core_text) and core_text[right].isascii() and core_text[right].isalnum():
            while right > b and core_text[right - 1].isascii() and core_text[right - 1].isalnum():
                right -= 1
        body = _FLAT_WS.sub(" ", core_text[left:right]).strip()
        return ("…" if left > 0 else "") + body + ("…" if right < len(core_text) else "")

    stripped = core_text.strip()
    if not stripped:
        return ""
    seg_start, seg_end = 0, len(core_text)
    if source_core and source_hit is not None:
        s_sents = _sentence_pieces(source_core, 0, len(source_core))
        t_sents = _sentence_pieces(core_text, 0, len(core_text))
        if s_sents and t_sents:
            if len(s_sents) == len(t_sents):
                s_idx = next(
                    (k for k, (sa, sb) in enumerate(s_sents) if sa <= source_hit[0] < sb),
                    len(s_sents) - 1,
                )
                seg_start, seg_end = t_sents[s_idx]
            else:
                target_pos = min(
                    len(core_text) - 1,
                    int((source_hit[0] / max(1, len(source_core))) * len(core_text)),
                )
                seg_start, seg_end = next(
                    ((ta, tb) for ta, tb in t_sents if ta <= target_pos < tb),
                    t_sents[-1],
                )
    excerpt_raw = _FLAT_WS.sub(" ", core_text[seg_start:seg_end]).strip()
    if not excerpt_raw:
        excerpt_raw = _FLAT_WS.sub(" ", stripped).strip()
        seg_start, seg_end = 0, len(core_text)
    clipped_left = seg_start > 0
    clipped_right = seg_end < len(core_text)
    if len(excerpt_raw) > window:
        cut = excerpt_raw[:window]
        if excerpt_raw[window].isascii() and excerpt_raw[window].isalnum() and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        excerpt_raw = cut.rstrip()
        clipped_right = True
    return ("…" if clipped_left else "") + excerpt_raw + ("…" if clipped_right else "")


def _record_freshness(
    record: Mapping,
    source: str,
    target: str,
    direction: str,
    alignments: Sequence[Mapping] | None,
    basis: Mapping | None,
    features: Sequence[str] | None = (),
    file_features: Sequence[str] | None = None,
) -> str | None:
    """``current``, ``saved`` or ``stale`` when the record's content hashes and
    direction match the current cores; ``None`` when pending or content-diverged."""
    view = record_view(record, {"source": source, "target": target, "direction": direction,
                                "alignments": alignments, "review_features": file_features}, basis, features)
    return view.state if view.content_current else None


def _canonical_group_surface(surfaces: Sequence[str], candidates: Sequence[Mapping]) -> str:
    if len(surfaces) == 1 or not _english_surface_tokens(surfaces[0]):
        return surfaces[0]
    candidate_en = [c["en"] for c in candidates if isinstance(c.get("en"), str) and c["en"]]
    for wanted in candidate_en:
        if wanted in surfaces:
            return wanted
    lower_map = {s.lower(): s for s in surfaces}
    for wanted in candidate_en:
        if wanted.lower() in lower_map:
            return wanted
    return min(surfaces, key=lambda s: (len(s), surfaces.index(s)))


def group_term_occurrences(
    records: Sequence[Mapping],
    pairs: Sequence,
    source: str,
    target: str,
    direction: str,
    *,
    alignments: Sequence[Mapping] | None = None,
    basis: Mapping | None = None,
    features: Sequence[str] | None = (),
    file_features: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate findings across non-pending content-current (current/saved/stale)
    segment records by source surface (merging English singular/plural surfaces
    sharing candidate sets), with per-occurrence spans, ~28–40 scalar context
    snippets, rendering distribution and group status.

    Ambiguous one-character names (``ambiguous_mention``) are no place in any
    group and take no ``finding_index``: they are not evaluated and not a
    problem until the user resolves them, so a caller lists them apart, after
    the groups. ``features`` is the session's review
    feature set (``None`` while unknown), ``file_features`` the
    ``review_features`` of the file the records were read from.

    A ready segment with no usable record yet is not a place in any group, but
    its source core may hold one of a group's spellings: ``unverified_segments``
    lists it, and the group is never ``confirmed`` while one stands, so a header
    cannot claim the term is settled across a manuscript that still owes a check.

    Saved results (a reopened manuscript, before the session has checked the live
    basis) are listed as usual, with ``saved_count`` saying how many places are
    such results. They are never settled: every saved place counts as pending, so
    the group is never ``confirmed`` either.
    """
    raw_groups: list[dict[str, Any]] = []
    finding_index = 0
    checked: set[int] = set()

    for fallback_idx, record in enumerate(records or ()):
        seg_index = record.get("index", fallback_idx)
        if pairs and 0 <= seg_index < len(pairs) and getattr(pairs[seg_index], "status", STATUS_READY) != STATUS_READY:
            continue
        freshness = _record_freshness(record, source, target, direction, alignments, basis, features,
                                      file_features)
        if freshness is None:
            continue
        checked.add(seg_index)
        (s_core_start, s_core_end), (t_core_start, t_core_end) = _cores(record)
        source_core = source[s_core_start:s_core_end]
        target_core = target[t_core_start:t_core_end]
        expanded = expand_result(record, source, target)
        choices_by_place = {_place(c): c for c in record.get("choices") or ()}

        for finding in expanded["findings"]:
            if is_ambiguous_mention(finding):
                continue
            core_s = finding["source_span"]
            core_t = finding["target_span"]
            doc_s = {
                "start": s_core_start + core_s["start"],
                "end": s_core_start + core_s["end"],
                "text": core_s["text"],
            }
            doc_t = None if core_t is None else {
                "start": t_core_start + core_t["start"],
                "end": t_core_start + core_t["end"],
                "text": core_t["text"],
            }
            verdict = finding["verdict"]
            reason = finding.get("reason") if verdict == "not_evaluated" else None
            reason_label = localized_not_evaluated_reason(reason)
            verdict_label = localized_verdict_label(verdict, reason)
            actual_translation = doc_t["text"] if doc_t is not None else None
            choice = choices_by_place.get((core_s["start"], core_s["end"]))
            choice_state = (
                "none" if choice is None else "confirmed" if choice.get("confirmed") else "unconfirmed"
            )
            source_ctx = _context_snippet(source_core, (core_s["start"], core_s["end"]))
            target_ctx = _context_snippet(
                target_core,
                None if core_t is None else (core_t["start"], core_t["end"]),
                source_core=source_core,
                source_hit=(core_s["start"], core_s["end"]),
            )
            candidates = finding["candidates"]
            cand_ids = frozenset(c["candidate_id"] for c in candidates if "candidate_id" in c)
            surface = core_s["text"]
            en_tokens = _english_surface_tokens(surface)

            occ = {
                "finding_index": finding_index,
                "segment_index": seg_index,
                "segment_number": seg_index + 1,
                "surface": surface,
                "core_source_span": core_s,
                "core_target_span": core_t,
                "source_span": doc_s,
                "target_span": doc_t,
                "source_context": source_ctx,
                "target_context": target_ctx,
                "actual_translation": actual_translation,
                "verdict": verdict,
                "reason": reason,
                "reason_label": reason_label,
                "verdict_label": verdict_label,
                "freshness": freshness,
                "choice": choice,
                "choice_state": choice_state,
                "candidates": candidates,
                "candidates_truncated": finding["candidates_truncated"],
                "attention": _attention(choice, freshness, verdict),
            }
            finding_index += 1

            matched_group = None
            for grp in raw_groups:
                if surface in grp["surfaces_set"]:
                    matched_group = grp
                    break
                if (en_tokens is not None
                        and grp["en_prefix"] is not None
                        and cand_ids
                        and cand_ids == grp["cand_ids"]
                        and en_tokens[0] == grp["en_prefix"]
                        and bool(en_tokens[1] & grp["en_forms"])):
                    matched_group = grp
                    break

            if matched_group is None:
                raw_groups.append({
                    "surfaces": [surface],
                    "surfaces_set": {surface},
                    "cand_ids": cand_ids,
                    "en_prefix": None if en_tokens is None else en_tokens[0],
                    "en_forms": set() if en_tokens is None else set(en_tokens[1]),
                    "candidates": candidates,
                    "occurrences": [occ],
                })
            else:
                if surface not in matched_group["surfaces_set"]:
                    matched_group["surfaces"].append(surface)
                    matched_group["surfaces_set"].add(surface)
                if en_tokens is not None:
                    matched_group["en_forms"].update(en_tokens[1])
                if not matched_group["candidates"] and candidates:
                    matched_group["candidates"] = candidates
                matched_group["occurrences"].append(occ)

    # A ready segment whose record is missing, pending or content-diverged has no
    # place in a group yet; Web's `unchecked`, narrowed to these spellings per group.
    unchecked: list[tuple[int, str]] = []
    for pair in pairs or ():
        index = getattr(pair, "index", None)
        if index is None or index in checked:
            continue
        if getattr(pair, "status", STATUS_READY) != STATUS_READY:
            continue
        region = getattr(pair, "source", None)
        start, end = getattr(region, "core_start", None), getattr(region, "core_end", None)
        if start is None or end is None or not 0 <= start <= end <= len(source):
            continue
        unchecked.append((index, source[start:end]))

    groups: list[dict[str, Any]] = []
    for order_idx, grp in enumerate(raw_groups):
        occurrences = grp["occurrences"]
        counts: Counter[str] = Counter()
        first_seen: dict[str, int] = {}
        for pos, occ in enumerate(occurrences):
            if occ["actual_translation"] is not None:
                label = occ["actual_translation"]
            elif occ["verdict"] == "not_evaluated":
                label = occ["verdict_label"]
            else:
                label = strings.MANUSCRIPT_TERM_TARGET_MISS
            counts[label] += 1
            first_seen.setdefault(label, pos)

        distribution = sorted(counts.items(), key=lambda item: (-item[1], first_seen[item[0]]))
        distribution_text = " · ".join(
            strings.MANUSCRIPT_TERM_DISTRIBUTION_ITEM.format(label=lbl, count=cnt)
            for lbl, cnt in distribution
        )

        eval_occs = [o for o in occurrences if o["verdict"] != "not_evaluated"]
        # Derive status from the same _attention-based counts Web uses, so
        # status and group_matches_filter can never disagree or drop a group
        # under any filter.
        has_conflict = any(o["attention"] == "conflict" for o in occurrences)
        distinct_renderings = len({o["actual_translation"] for o in eval_occs})
        undecided_evaluated = any(
            not (o.get("choice") or {}).get("confirmed") for o in eval_occs
        )
        inconsistent = has_conflict or (distinct_renderings >= 2 and undecided_evaluated)

        pending_count = sum(1 for o in occurrences if _needs_action(o))
        not_evaluated_count = sum(1 for o in occurrences if o["attention"] == "not_evaluated")
        saved_count = sum(1 for o in occurrences if o["freshness"] == "saved")
        unverified = _unchecked_holding(grp["surfaces"], unchecked, direction)

        if inconsistent:
            status = "inconsistent"
        elif pending_count > 0 or saved_count > 0 or bool(unverified):
            # A saved result is never settled: its basis is not the live one.
            status = "pending"
        elif not_evaluated_count > 0:
            status = "not_evaluated"
        else:
            status = "confirmed"

        clean_occurrences = []
        for occ in occurrences:
            item = dict(occ)
            item["group_status"] = status
            clean_occurrences.append(item)

        seg_indices = sorted({o["segment_index"] for o in clean_occurrences})
        groups.append({
            "surface": _canonical_group_surface(grp["surfaces"], grp["candidates"]),
            "surfaces": list(grp["surfaces"]),
            "count": len(clean_occurrences),
            "segment_indices": seg_indices,
            "segments": [idx + 1 for idx in seg_indices],
            "distribution": distribution,
            "distribution_text": distribution_text,
            "status": status,
            "inconsistent": inconsistent,
            "pending_count": pending_count,
            "saved_count": saved_count,
            "not_evaluated_count": not_evaluated_count,
            "confirmed_count": len(occurrences) - pending_count - not_evaluated_count,
            "unverified_segments": unverified,
            "candidates": grp["candidates"],
            "occurrences": clean_occurrences,
            "_order": order_idx,
        })

    groups.sort(key=lambda g: (_GROUP_STATUS_PRIORITY.get(g["status"], 99), g["_order"]))
    for g in groups:
        g.pop("_order", None)
    return groups


def adopt_file_features(payload: Mapping[str, Any], features: Sequence[str]) -> tuple[Parts, tuple[str, ...]]:
    """After a save: the file's ``review_features`` become the payload's, and a
    record whose own features equal them no longer states them."""
    file_features = review_feature_set(features)
    parts = parts_of(payload)
    segments = [{k: v for k, v in record.items() if k != "features"}
                if "features" in record and review_feature_set(record["features"]) == file_features else record
                for record in parts.segments]
    return parts._replace(segments=_shared(parts.segments, segments)), file_features
