"""Layer 1 of the layered review: deterministic structure checks.

Core ``POST /v1/structure-checks`` (``structure-check-v1``, ADR 0022) checks
paired segments for an empty target, numbers, placeholders and markup tags, and
paragraph and line layout. No model is called and the same texts always give
the same findings. This module holds the wire rules (strict request and
response checks, request batching), the rule by which a Layer 1 finding takes
precedence over a semantic (Layer 2) issue that describes the same break, and
the session state of Layer 1 (results kept by the content hashes of the two
cores they were made for). It mirrors the public Web ``lib/structure-check.js``
and ``lib/structure-layer.ts``.

Nothing here is stored. Structure findings never enter a manuscript file of
any version, an export, the undo history or the recovery draft (manuscript-v5
SPEC "Structure checks are not stored"): the state lives on the document
controller only, and a result describes exactly the texts it was made for, so
it disappears from view as soon as either changes.

Every length and offset is in Unicode scalars (Python ``str`` indices), as
Core counts. Pure Python: no Qt, no HTTP.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .segmentation import STATUS_READY, STATUS_UNTRANSLATED

CAPABILITY_STRUCTURE_CHECK = "structure-check-v1"
STRUCTURE_CHECK_VERSION = "structure-check-v1"
# Every rule_id Core can return, in Core's order (findings are sorted by rule, then offset).
STRUCTURE_RULES = (
    "empty_target", "paragraph_count_mismatch", "line_count_mismatch", "placeholder_missing",
    "placeholder_unexpected", "tag_unbalanced", "number_missing", "number_unexpected",
)
# The checks a response lists as run.
STRUCTURE_CHECKS = ("empty_target", "numbers", "placeholders", "paragraphs")
MAX_SEGMENTS = 64
MAX_SIDE_SCALARS = 2000
MAX_TOTAL_SCALARS = 20_000
MAX_FINDINGS = 16
# Core's default body limit is 64 KiB, and Chinese text at the scalar limits is
# larger than that, so a batch is also cut by its serialized size. It stays
# under 60 KiB so the operator's 64 KiB cap is never the one that answers.
BATCH_BYTES = 61_440

# Every wire object's key set, named as Core's OpenAPI names it; a contract test
# compares them with the pinned Core ``docs/api/openapi.json``.
WIRE_KEYS: dict[str, frozenset[str]] = {
    "StructureCheckRequestBody": frozenset({"check_version", "segments"}),
    "StructureCheckSegmentBody": frozenset({"segment_id", "source", "target"}),
    "StructureCheckResponseBody": frozenset({"request_id", "check_version", "checks", "segments"}),
    "StructureSegmentResultBody": frozenset({"index", "segment_id", "findings", "omitted_findings", "layout"}),
    "StructureFindingBody": frozenset({"rule_id", "source_span", "target_span"}),
    "StructureLayoutBody": frozenset({"source_paragraphs", "target_paragraphs", "source_lines", "target_lines"}),
    "StructureSpanBody": frozenset({"start", "end", "text"}),
}
_SEGMENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_CONTROL = re.compile("[\u0000-\u001f\u007f]")


def _keys(value: Any, keys: frozenset[str]) -> bool:
    return isinstance(value, dict) and value.keys() == keys


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 9_007_199_254_740_991


def encode(body: Mapping[str, Any]) -> bytes:
    """The exact request bytes: compact UTF-8 JSON, as the batch limit counts them."""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def valid_request(value: Any) -> bool:
    """A request Core accepts: exactly ``check_version`` and ``segments``; 1 to
    64 segments of ``source``, ``target`` and an optional ``segment_id`` (unique
    within the request); each side at most 2,000 scalars and 20,000 across the
    request, with no isolated surrogate."""
    if (not _keys(value, WIRE_KEYS["StructureCheckRequestBody"])
            or value["check_version"] != STRUCTURE_CHECK_VERSION):
        return False
    segments = value["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        return False
    ids: set[str] = set()
    total = 0
    for segment in segments:
        keys = WIRE_KEYS["StructureCheckSegmentBody"] if isinstance(segment, dict) and "segment_id" in segment \
            else frozenset({"source", "target"})
        if not _keys(segment, keys):
            return False
        for side in (segment["source"], segment["target"]):
            if not isinstance(side, str) or _LONE_SURROGATE.search(side) or len(side) > MAX_SIDE_SCALARS:
                return False
            total += len(side)
        segment_id = segment.get("segment_id")
        if segment_id is not None:
            if not isinstance(segment_id, str) or not _SEGMENT_ID.fullmatch(segment_id) or segment_id in ids:
                return False
            ids.add(segment_id)
    return total <= MAX_TOTAL_SCALARS


def _span(span: Any, side: str) -> bool:
    if span is None:
        return True
    return (_keys(span, WIRE_KEYS["StructureSpanBody"]) and _count(span["start"]) and _count(span["end"])
            and span["start"] < span["end"] <= len(side) and isinstance(span["text"], str)
            and side[span["start"]:span["end"]] == span["text"])


def valid_response(body: Any, request: Mapping[str, Any]) -> bool:
    """Core's answer to ``request``, strictly: the four top-level keys, the
    checks it ran (known names, no repeats), and one segment per request
    segment in request order with its index, its echoed ``segment_id`` (or
    null), at most 16 findings of a known rule whose spans are the exact text of
    the submitted side, ``omitted_findings`` and the four layout counts."""
    if not _keys(body, WIRE_KEYS["StructureCheckResponseBody"]):
        return False
    request_id = body["request_id"]
    if (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256
            or _CONTROL.search(request_id)):
        return False
    checks = body["checks"]
    if (body["check_version"] != STRUCTURE_CHECK_VERSION or not isinstance(checks, list)
            or len(set(map(str, checks))) != len(checks)
            or not all(isinstance(name, str) and name in STRUCTURE_CHECKS for name in checks)):
        return False
    sent = request["segments"]
    segments = body["segments"]
    if not isinstance(segments, list) or len(segments) != len(sent):
        return False
    for index, (segment, item) in enumerate(zip(segments, sent, strict=True)):
        if not _keys(segment, WIRE_KEYS["StructureSegmentResultBody"]):
            return False
        if (segment["index"] != index or type(segment["index"]) is not int
                or segment["segment_id"] != item.get("segment_id")
                or not _count(segment["omitted_findings"])):
            return False
        layout = segment["layout"]
        if not _keys(layout, WIRE_KEYS["StructureLayoutBody"]) or not all(_count(v) for v in layout.values()):
            return False
        findings = segment["findings"]
        if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
            return False
        for finding in findings:
            if (not _keys(finding, WIRE_KEYS["StructureFindingBody"])
                    or not isinstance(finding["rule_id"], str) or finding["rule_id"] not in STRUCTURE_RULES
                    or not _span(finding["source_span"], item["source"])
                    or not _span(finding["target_span"], item["target"])):
                return False
    return True


def batches(items: Iterable[tuple[str, str, str]], max_bytes: int = BATCH_BYTES) -> tuple[list[dict], list[str]]:
    """Request bodies for ``(segment_id, source, target)`` items, in order.

    Each batch holds at most 64 segments and 20,000 scalars, and its JSON stays
    under ``max_bytes`` UTF-8 bytes. An item with a side over 2,000 scalars, or
    alone over the byte cap, cannot be sent and is returned in ``skipped``."""
    out: list[dict] = []
    skipped: list[str] = []
    current: list[dict] = []
    scalars = 0
    # The compact encoding of a batch is the empty body plus each segment's own
    # bytes and one comma between two segments, so sizes add up exactly.
    empty = len(encode({"check_version": STRUCTURE_CHECK_VERSION, "segments": []}))
    used = empty

    for segment_id, source, target in items:
        segment = {"segment_id": segment_id, "source": source, "target": target}
        length = len(source) + len(target)
        own = len(encode(segment))
        if len(source) > MAX_SIDE_SCALARS or len(target) > MAX_SIDE_SCALARS or empty + own > max_bytes:
            skipped.append(segment_id)
            continue
        if (len(current) >= MAX_SEGMENTS or scalars + length > MAX_TOTAL_SCALARS
                or used + own + (1 if current else 0) > max_bytes):
            out.append({"check_version": STRUCTURE_CHECK_VERSION, "segments": current})
            current, scalars, used = [], 0, empty
        used += own + (1 if current else 0)
        current.append(segment)
        scalars += length
    if current:
        out.append({"check_version": STRUCTURE_CHECK_VERSION, "segments": current})
    return out, skipped


# ---- Layer 1 wins an overlap (ADR 0022) ------------------------------------

# The semantic categories a structure rule can restate, and the side on which
# their spans must meet. ``empty_target`` restates an omission anywhere in its
# segment; layout counts and unbalanced tags restate no semantic category.
PRECEDENCE: dict[str, tuple[tuple[str, ...], str | None]] = {
    "empty_target": (("omission",), None),
    "number_missing": (("quantity_change", "omission"), "source"),
    "number_unexpected": (("quantity_change", "unsupported_addition"), "target"),
    "placeholder_missing": (("omission",), "source"),
    "placeholder_unexpected": (("unsupported_addition",), "target"),
}


def _meet(a: Mapping | None, b: Mapping | None) -> bool:
    return a is not None and b is not None and a["start"] < b["end"] and b["start"] < a["end"]


def covers(category: str, source_span: Mapping | None, target_span: Mapping | None,
           finding: Mapping[str, Any]) -> bool:
    """Whether a structure finding of the same segment describes the break of a
    semantic issue (both with whole-text spans), so the Layer 1 finding is shown
    and the issue is not raised again. A category the rule restates is
    required; except for ``empty_target``, the spans must also overlap on the
    rule's side."""
    rule = PRECEDENCE.get(finding["rule_id"])
    if rule is None or category not in rule[0]:
        return False
    if rule[1] is None:
        return True
    if rule[1] == "source":
        return _meet(finding["source_span"], source_span)
    return _meet(finding["target_span"], target_span)


# ---- session state ---------------------------------------------------------

# The pair statuses Layer 1 checks: a ready pair, and an untranslated one (its
# empty target is the finding).
CHECKED_STATUSES = (STATUS_READY, STATUS_UNTRANSLATED)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key_of(source_core: str, target_core: str) -> tuple[str, str]:
    """A result's key: the content hashes of the two cores it was made for."""
    return _hash(source_core), _hash(target_core)


@dataclass(frozen=True)
class Item:
    """One segment a check sends: its request ``segment_id``, its pair index,
    the key its result is stored under, and its cores and their offsets."""

    segment_id: str
    index: int
    key: tuple[str, str]
    source: str
    target: str


def checkable(pair: Any, source: str) -> bool:
    return pair.status in CHECKED_STATUSES and bool(source[pair.source.core_start:pair.source.core_end].strip())


def items(pairs: Sequence[Any], source: str, target: str) -> list[Item]:
    """The segments of the current pairing a structure check sends."""
    out = []
    for pair in pairs:
        if not checkable(pair, source):
            continue
        s_core = source[pair.source.core_start:pair.source.core_end]
        t_core = target[pair.target.core_start:pair.target.core_end]
        out.append(Item(f"s{pair.index}", pair.index, key_of(s_core, t_core), s_core, t_core))
    return out


def plan(pairs: Sequence[Any], source: str, target: str) -> tuple[list[Item], list[dict], list[str]]:
    """The items of the current pairing, their request batches and the
    ``segment_id`` of every item that cannot be sent."""
    found = items(pairs, source, target)
    planned, skipped = batches((item.segment_id, item.source, item.target) for item in found)
    return found, planned, skipped


@dataclass
class State:
    """Layer 1 of one document session: never saved anywhere.

    ``phase``: idle, running, done or failed. ``results``: per item key, the
    findings (core-relative spans), the count Core left out and the layout
    counts. ``skipped``: how many items of the last run could not be sent.
    ``error``: ``(message, request_id)`` of a failed run."""

    phase: str = "idle"
    results: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    skipped: int = 0
    checks: tuple[str, ...] = ()
    error: tuple[str, str | None] | None = None


def store(state: State, batch: Mapping[str, Any], response: Mapping[str, Any],
          keys: Mapping[str, tuple[str, str]]) -> None:
    """Keep a validated response's results under the keys of the items sent."""
    state.checks = tuple(response["checks"])
    for sent, segment in zip(batch["segments"], response["segments"], strict=True):
        state.results[keys[sent["segment_id"]]] = {
            "findings": [{"rule_id": f["rule_id"],
                          "source_span": None if f["source_span"] is None else dict(f["source_span"]),
                          "target_span": None if f["target_span"] is None else dict(f["target_span"])}
                         for f in segment["findings"]],
            "omitted": segment["omitted_findings"],
            "layout": dict(segment["layout"]),
        }


def _shift(span: Mapping | None, by: int) -> dict | None:
    return None if span is None else {"start": span["start"] + by, "end": span["end"] + by, "text": span["text"]}


@dataclass(frozen=True)
class View:
    """A segment's Layer 1 result for the text as it is now, with whole-text
    spans, or ``result`` None when it has none for these exact cores."""

    index: int
    checkable: bool
    result: dict[str, Any] | None

    @property
    def flagged(self) -> bool:
        return self.result is not None and bool(self.result["findings"] or self.result["omitted"])

    @property
    def layout_differs(self) -> bool:
        if self.result is None:
            return False
        layout = self.result["layout"]
        return (layout["source_paragraphs"] != layout["target_paragraphs"]
                or layout["source_lines"] != layout["target_lines"])


def views(pairs: Sequence[Any], source: str, target: str, state: State) -> list[View]:
    """The Layer 1 view of every segment of the current pairing, in order."""
    out = []
    for pair in pairs:
        ok = checkable(pair, source)
        result = None
        if ok and state.results:
            s_core = source[pair.source.core_start:pair.source.core_end]
            t_core = target[pair.target.core_start:pair.target.core_end]
            stored = state.results.get(key_of(s_core, t_core))
            if stored is not None:
                result = {**stored, "findings": [
                    {"rule_id": f["rule_id"], "source_span": _shift(f["source_span"], pair.source.core_start),
                     "target_span": _shift(f["target_span"], pair.target.core_start)}
                    for f in stored["findings"]]}
        out.append(View(pair.index, ok, result))
    return out


@dataclass(frozen=True)
class Summary:
    checked: int
    with_findings: int
    findings: int
    omitted: int
    unchecked: int


def summary(views_now: Sequence[View]) -> Summary:
    """Counts for the Layer 1 summary: checked segments, segments with a
    finding, findings shown, findings Core left out, and unchecked ones."""
    checked = [v for v in views_now if v.result is not None]
    return Summary(
        checked=len(checked),
        with_findings=sum(1 for v in checked if v.flagged),
        findings=sum(len(v.result["findings"]) for v in checked),  # type: ignore[index]
        omitted=sum(v.result["omitted"] for v in checked),  # type: ignore[index]
        unchecked=sum(1 for v in views_now if v.checkable and v.result is None),
    )


def covered_by(category: str, source_span: Mapping | None, target_span: Mapping | None,
               view: View | None) -> bool:
    """Whether a Layer 1 finding of the issue's segment already describes it.
    Only results for the current cores count, since a view holds no other."""
    return view is not None and view.result is not None and any(
        covers(category, source_span, target_span, finding) for finding in view.result["findings"])
