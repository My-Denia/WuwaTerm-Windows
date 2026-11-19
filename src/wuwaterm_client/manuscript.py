"""Read and safely save the public Web's manuscript workfiles (v1, v2, v3 and v4).

Reports and decisions in v1/v2 files, and the document-mode choices in them, stay
historical data (Core ADR 0015, unchanged): this module does not grant them runtime
trust, and a presentation layer must request a new review.

v3/v4 segment records are the one exception.  A saved segment result is reused only
when the content hashes, the request inputs and the live basis all match (public
Web ``contracts/manuscript-v3/SPEC.md``, "Design note").  v4 extends v3 with dense
caps (128 findings, choices and submitted decisions, document-mode choices
included) and structured ``not_evaluated`` ``reason`` codes while keeping v1, v2
and v3 byte-identical when their tighter caps suffice.  This module only parses
and writes: it computes no hashes or digests, so whether a record is current is
decided by the caller.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic import CATEGORIES, REVIEW_VERSION, valid_semantic_report

WORKFILE_FORMAT = "wuwaterm-manuscript-v1"
WORKFILE_FORMAT_V2 = "wuwaterm-manuscript-v2"
WORKFILE_FORMAT_V3 = "wuwaterm-manuscript-v3"
WORKFILE_FORMAT_V4 = "wuwaterm-manuscript-v4"
MAX_WORKFILE_BYTES = 1_048_576  # v1 and v2
MAX_V3_BYTES = 46_137_344  # 44 MiB (v3 and v4)
MAX_V3_TEXT_SCALARS = 46_137_344  # source, target and the semantic texts of a v3/v4 file
MAX_CHOICES = 32
MAX_V4_CHOICES = 128
MAX_HISTORY = 2
MAX_SEMANTIC_DECISIONS = 128
MAX_SEMANTIC_HISTORY = 2
MAX_SEMANTIC_TEXTS = 16
MAX_HISTORY_SEGMENTS = 64
MAX_V3_ITEMS = 512  # array items, any array of a v3/v4 file (v1/v2: 128)
MAX_SEGMENTS = 512
MAX_SEGMENT_CHOICE_HISTORY = 512
MAX_SEGMENT_CHOICES = 32  # per segment record in v3
MAX_RESULT_FINDINGS = 32
MAX_V4_FINDINGS = 128
MAX_FINDING_CANDIDATES = 8
MAX_OFFERED = 8
MAX_CORE_SCALARS = 2000  # a done or stale record's cores are 1 to 2,000 scalars
REASON_VALUES = (
    "not_a_term",
    "ordinary_word",
    "no_target_region",
    "found_outside_region",
    "ambiguous_repeat",
)
SEGMENTATION_VERSION = "segmentation-v1"
ALIGNMENT_AUTOMATIC = "automatic"
_HEX = re.compile(r"^[a-f0-9]{64}$")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_TOP = {"format", "source", "target", "direction", "alignments", "choices", "history"}
_TOP_V2 = _TOP | {"semantic_decisions", "semantic_texts", "semantic_history"}
_V2_PART = ("source", "target", "direction", "alignments", "choices", "history",
            "semantic_decisions", "semantic_texts", "semantic_history")
_TOP_V3 = _TOP_V2 | {"segments", "segment_choice_history", "progress"}
_RECORD_KEYS = {"index", "segmentation_version", "source_start", "source_end", "target_start", "target_end",
                "source_hash", "target_hash", "status", "direction", "alignment_input", "basis", "result", "choices"}
_RESULT_KEYS = {"truncated", "evaluated", "not_evaluated", "findings"}
_FINDING_KEYS = {"verdict", "source", "target", "candidates", "candidates_truncated"}
_CANDIDATE_KEYS = {"candidate_id", "zh", "en", "category"}
_SEGMENT_CHOICE_KEYS = {"source_span", "choice", "candidate", "offered", "confirmed"}
_HISTORY_CHOICE_KEYS = {"source_hash", "source_span", "choice", "candidate", "offered"}
_DECISION_KEYS = {"issue_id", "fingerprint", "category", "segment_index", "source_revision",
                  "target_revision", "source_span", "target_span", "decision", "applied_text", "basis"}
_VERDICTS = {"verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated"}

_MSG_NOT_JSON = "无法读取 JSON 稿件文件。"
_MSG_TOO_LARGE = "稿件文件超过大小上限（v1/v2 为 1 MiB，v3 为 44 MiB）；请把稿件拆成较小的几份。"
_MSG_TOO_MANY_ENTRIES = "稿件文件条目过多；超过 512 段的稿件请拆成几份保存，选择历史过多时请先移除部分记录。"
_MSG_SEGMENT_INVALID = "稿件的分段记录无效；当前稿件未被替换。"
_MSG_SEGMENT_ORDER = "稿件的分段顺序或位置无效；当前稿件未被替换。"
_MSG_SEGMENT_ANCHOR = "稿件的逐段术语选择位置无效；当前稿件未被替换。"
_MSG_PROGRESS = "稿件的进度记录无效；当前稿件未被替换。"
_MSG_INVALID_CONTENT = "稿件内容、选择或位置依据无效；当前稿件未被替换。"
_MSG_DUPLICATE_CHOICES = "稿件包含同一位置的重复选择，未导入。"


class WorkfileError(ValueError):
    """A workfile cannot be loaded or saved without losing valid data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def empty_workfile() -> dict[str, Any]:
    return {"format": WORKFILE_FORMAT, "source": "", "target": "", "direction": "en",
            "alignments": None, "choices": [], "history": []}


def _keys(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and value.keys() == fields


def _text(value: Any, maximum: int = 2000, empty: bool = True) -> bool:
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and _LONE_SURROGATE.search(value) is None)


def _int(value: Any, *, safe: bool = False) -> bool:
    # JSON numbers such as 1.0 satisfy JavaScript Number.isInteger as well.
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and (not isinstance(value, float) or math.isfinite(value))
            and value == int(value) and (not safe or abs(value) <= 9_007_199_254_740_991))


def _hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _direction(value: Any) -> bool:
    return value in ("en", "zh")


def _span(span: Any, content: str) -> bool:
    if not _keys(span, {"start", "end", "text"}):
        return False
    start, end = span["start"], span["end"]
    return (_int(start) and _int(end) and start >= 0 and end > start
            and _text(span["text"], 2000, False) and end <= len(content)
            and content[int(start):int(end)] == span["text"])


def _alignments(items: Any, source: str, target: str) -> bool:
    if items is None:
        return True
    if not isinstance(items, list) or len(items) > 64:
        return False
    source_end = target_end = 0
    for item in items:
        if (not _keys(item, {"source", "target"}) or not _span(item["source"], source)
                or (item["target"] is not None and not _span(item["target"], target))
                or item["source"]["start"] < source_end):
            return False
        source_end = item["source"]["end"]
        if item["target"] is not None:
            if item["target"]["start"] < target_end:
                return False
            target_end = item["target"]["end"]
    return True


def _dictionary(value: Any, version: str) -> bool:
    fields = {"schema_version", "source_commit", "term_count"}
    if version == "review-v2":
        fields.add("revision")
    return (_keys(value, fields)
            and (value["schema_version"] is None or _text(value["schema_version"], 4096))
            and (value["source_commit"] is None or _text(value["source_commit"], 4096))
            and _int(value["term_count"], safe=True) and value["term_count"] >= 0
            and (version != "review-v2" or _hex(value["revision"])))


def _candidate(value: Any, version: str) -> bool:
    fields = {"zh", "en", "category", "sources"}
    if version == "review-v2":
        fields.add("candidate_id")
    if not _keys(value, fields):
        return False
    sources = value["sources"]
    return (_text(value["zh"], 2000, False) and _text(value["en"], 2000, False)
            and _text(value["category"], 256, False) and isinstance(sources, list)
            and len(sources) <= 8 and all(_keys(s, {"source_file", "source_id"})
                and _text(s["source_file"], 4096, False) and _text(s["source_id"], 1024, False)
                for s in sources)
            and (version != "review-v2" or _hex(value["candidate_id"])))


def _mention(span: dict[str, Any]) -> str:
    return f'{int(span["start"])}:{int(span["end"])}:{span["text"]}'


def valid_report(value: Any, source: str, target: str, *, v4: bool = False) -> bool:
    """Match the public Web's serializable report schema, not its trust state."""
    if not isinstance(value, dict):
        return False
    fields = {"request_id", "source_revision", "target_revision", "rule_version", "dictionary",
              "coverage", "findings", "truncated"}
    if "matcher_revision" in value:
        fields.add("matcher_revision")
    if not _keys(value, fields):
        return False
    version, coverage, findings = value["rule_version"], value["coverage"], value["findings"]
    max_findings = MAX_V4_FINDINGS if v4 else MAX_RESULT_FINDINGS
    if (not _text(value["request_id"], 256, False) or not _hex(value["source_revision"])
            or not _hex(value["target_revision"]) or version not in ("review-v1", "review-v2")
            or not _dictionary(value["dictionary"], version)
            or ("matcher_revision" in value and not _hex(value["matcher_revision"]))
            or not _keys(coverage, {"evaluated", "not_evaluated", "rules"})
            or not _int(coverage["evaluated"], safe=True) or coverage["evaluated"] < 0
            or not _int(coverage["not_evaluated"], safe=True) or coverage["not_evaluated"] < 0
            or not isinstance(coverage["rules"], list) or len(coverage["rules"]) > 16
            or not all(_text(r, 256, False) for r in coverage["rules"])
            or not isinstance(value["truncated"], bool) or not isinstance(findings, list)
            or len(findings) > max_findings):
        return False
    base_finding_keys = {"id", "rule_id", "verdict", "source_span", "target_span",
                         "candidates", "candidates_truncated"}
    ids: set[str] = set()
    for finding in findings:
        if v4 and isinstance(finding, dict) and "reason" in finding:
            if (not _keys(finding, base_finding_keys | {"reason"})
                    or finding["verdict"] != "not_evaluated"
                    or not isinstance(finding["reason"], str)
                    or finding["reason"] not in REASON_VALUES):
                return False
        elif not _keys(finding, base_finding_keys):
            return False
        source_span, target_span, candidates = finding["source_span"], finding["target_span"], finding["candidates"]
        if (not _text(finding["id"], 8192, False) or finding["id"] in ids
                or not _text(finding["rule_id"], 256, False)
                or not isinstance(finding["verdict"], str) or finding["verdict"] not in _VERDICTS
                or not _span(source_span, source) or finding["id"] != _mention(source_span)
                or (target_span is not None and not _span(target_span, target))
                or not isinstance(finding["candidates_truncated"], bool) or not isinstance(candidates, list)
                or len(candidates) > 8 or not all(_candidate(c, version) for c in candidates)):
            return False
        ids.add(finding["id"])
    return True


def _basis(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    fields = {"rule_version", "dictionary"}
    if "matcher_revision" in value:
        fields.add("matcher_revision")
    return (_keys(value, fields) and value["rule_version"] == "review-v2"
            and _dictionary(value["dictionary"], "review-v2")
            and ("matcher_revision" not in value or _hex(value["matcher_revision"])))


def _choice(value: Any) -> bool:
    if not _keys(value, {"source", "direction", "source_span", "scope", "choice", "candidate", "basis"}):
        return False
    if (not _text(value["source"]) or not _direction(value["direction"])
            or not _span(value["source_span"], value["source"]) or not _text(value["scope"], 20000)
            or not _basis(value["basis"])):
        return False
    candidate = value["candidate"]
    if value["choice"] == "not_a_term":
        return candidate is None
    return (value["choice"] == "official_pair" and _keys(candidate, {"candidate_id", "zh", "en", "category"})
            and _hex(candidate["candidate_id"]) and _text(candidate["zh"], 2000, False)
            and _text(candidate["en"], 2000, False) and _text(candidate["category"], 256, False))


def _resolutions(value: Any, *, max_choices: int = MAX_CHOICES) -> bool:
    if not isinstance(value, list) or len(value) > max_choices:
        return False
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or not _text(item.get("mention_id"), 8192, False):
            return False
        if item["mention_id"] in ids:
            return False
        ids.add(item["mention_id"])
        if item.get("choice") == "not_a_term":
            if not _keys(item, {"mention_id", "choice"}):
                return False
        elif (item.get("choice") != "official_pair" or not _keys(item, {"mention_id", "choice", "candidate_id"})
              or not _hex(item["candidate_id"])):
            return False
    return True


def _snapshot(value: Any, *, v4: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    fields = {"source", "target", "direction", "alignments", "report"}
    if "resolutions" in value:
        fields.add("resolutions")
    max_choices = MAX_V4_CHOICES if v4 else MAX_CHOICES
    return (_keys(value, fields) and _text(value["source"]) and _text(value["target"])
            and _direction(value["direction"]) and _alignments(value["alignments"], value["source"], value["target"])
            and valid_report(value["report"], value["source"], value["target"], v4=v4)
            and ("resolutions" not in value or _resolutions(value["resolutions"], max_choices=max_choices)))


def _strict_int(value: Any) -> bool:
    return type(value) is int


def _hex32(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def _decision_span(span: Any) -> bool:
    return (_keys(span, {"start", "end", "text"}) and _strict_int(span["start"])
            and _strict_int(span["end"]) and 0 <= span["start"] < span["end"]
            and _text(span["text"], 2000, False) and span["end"] - span["start"] == len(span["text"]))


def _decision(value: Any) -> bool:
    """A recorded semantic decision (Web docs/manuscript-v2.md). Structure only:
    spans are historical and are checked against old texts when they are used."""
    if not _keys(value, _DECISION_KEYS):
        return False
    basis, kind, applied = value["basis"], value["decision"], value["applied_text"]
    if not (_hex32(value["issue_id"]) and _hex32(value["fingerprint"])
            and isinstance(value["category"], str) and value["category"] in CATEGORIES
            and _strict_int(value["segment_index"]) and value["segment_index"] >= 0
            and _hex(value["source_revision"]) and _hex(value["target_revision"])
            and isinstance(kind, str) and kind in ("accepted", "adjusted", "ignored")
            and _keys(basis, {"review_version", "term_basis"})
            and basis["review_version"] == REVIEW_VERSION
            and _keys(basis["term_basis"], {"dictionary_revision", "matcher_revision"})
            and _hex(basis["term_basis"]["dictionary_revision"])
            and _hex(basis["term_basis"]["matcher_revision"])):
        return False
    if kind == "ignored":
        if applied is not None:
            return False
    elif not _text(applied, 2000):
        # An accepted suggestion may be a deletion, so the text may be empty.
        return False
    spans = (value["source_span"], value["target_span"])
    if spans[0] is None and spans[1] is None:
        return False
    return all(span is None or _decision_span(span) for span in spans)


def occurrence_key(decision: Mapping[str, Any]) -> str:
    """One decision per reported occurrence (Web ``occurrenceKey``)."""
    def bounds(span):
        return None if span is None else [span["start"], span["end"]]
    return json.dumps([decision["category"], decision["source_revision"], decision["target_revision"],
                       bounds(decision["source_span"]), bounds(decision["target_span"])], separators=(",", ":"))


def _semantic_text(value: Any, text_limit: int) -> bool:
    return _keys(value, {"revision", "text"}) and _hex(value["revision"]) and _text(value["text"], text_limit)


def _history_segment(item: Any, source: str, target: str) -> bool:
    if not _keys(item, {"segment_index", "source_start", "source_end", "target_start", "target_end", "report"}):
        return False
    a, b, c, d = (item[k] for k in ("source_start", "source_end", "target_start", "target_end"))
    return (_strict_int(item["segment_index"]) and item["segment_index"] >= 0
            and all(_strict_int(x) for x in (a, b, c, d)) and 0 <= a <= b <= len(source) and 0 <= c <= d <= len(target)
            and valid_semantic_report(item["report"], source[a:b], target[c:d]))


def _semantic_snapshot(value: Any, text_limit: int) -> bool:
    """One semantic run over a whole manuscript: untrusted reference data."""
    return (_keys(value, {"source", "target", "direction", "segments"})
            and _text(value["source"], text_limit) and _text(value["target"], text_limit)
            and _direction(value["direction"]) and isinstance(value["segments"], list)
            and len(value["segments"]) <= MAX_HISTORY_SEGMENTS
            and all(_history_segment(item, value["source"], value["target"]) for item in value["segments"]))


def recover_span(old: str, span: Mapping[str, Any], current: str) -> dict[str, Any] | None:
    """Web ``recoverSpan``: relocate only inside an identical, unique, complete line block."""
    start, end, value = span["start"], span["end"], span["text"]
    if not (0 <= start < end <= len(old) and old[start:end] == value):
        return None
    if old == current:
        return {"start": start, "end": end, "text": value}
    old_blocks, new_blocks = old.split("\n"), current.split("\n")
    offset = 0
    for block in old_blocks:
        block_end = offset + len(block)
        if offset <= start and block_end >= end:
            if old_blocks.count(block) != 1 or new_blocks.count(block) != 1:
                return None
            index = new_blocks.index(block)
            moved = sum(len(b) + 1 for b in new_blocks[:index]) + start - offset
            recovered = {"start": moved, "end": moved + end - start, "text": value}
            return recovered if current[moved:moved + end - start] == value else None
        offset = block_end + 1
    return None


def _recovered_mention(choice: dict[str, Any], source: str) -> str | None:
    old, span = choice["source"], choice["source_span"]
    if old == source:
        return _mention(span)
    old_blocks, new_blocks = old.split("\n"), source.split("\n")
    start = 0
    for block in old_blocks:
        end = start + len(block)
        if start <= span["start"] and end >= span["end"]:
            if old_blocks.count(block) != 1 or new_blocks.count(block) != 1:
                return None
            idx = new_blocks.index(block)
            recovered_start = sum(len(b) + 1 for b in new_blocks[:idx]) + int(span["start"]) - start
            return f'{recovered_start}:{recovered_start + int(span["end"] - span["start"])}:{span["text"]}'
        start = end + 1
    return None


def choice_conflict_groups(payload: Mapping[str, Any]) -> list[list[int]]:
    """Group historical choices that now recover to the same source position.

    Choices in a loaded workfile have already been validated. Changes to the
    current source can make formerly distinct records collide; keep their
    indices so a caller can let the user choose what to retain.
    """
    groups: dict[str, list[int]] = {}
    for index, choice in enumerate(payload["choices"]):
        mention = _recovered_mention(choice, payload["source"])
        if mention is not None:
            groups.setdefault(mention, []).append(index)
    return [indices for indices in groups.values() if len(indices) > 1]


def _safe_tree(value: Any, depth: int = 0, items: int = 128) -> None:
    if depth > 16:
        raise WorkfileError("workfile_too_deep", "稿件文件嵌套过深。")
    if isinstance(value, list):
        if len(value) > items:
            raise WorkfileError("workfile_too_many_entries", _MSG_TOO_MANY_ENTRIES)
        for child in value:
            _safe_tree(child, depth + 1, items)
    elif isinstance(value, dict):
        if len(value) > 32:
            raise WorkfileError("workfile_too_many_fields", "稿件文件字段过多。")
        for key, child in value.items():
            if key in ("__proto__", "constructor", "prototype"):
                raise WorkfileError("workfile_forbidden_field", "稿件文件含不支持的字段。")
            _safe_tree(child, depth + 1, items)


def _too_large() -> WorkfileError:
    return WorkfileError("workfile_too_large", _MSG_TOO_LARGE)


def _too_many() -> WorkfileError:
    return WorkfileError("workfile_too_many_entries", _MSG_TOO_MANY_ENTRIES)


def _segment_invalid() -> WorkfileError:
    return WorkfileError("workfile_segment_invalid", _MSG_SEGMENT_INVALID)


def _check_legacy_parts(value: dict[str, Any], v2: bool, text_limit: int, *, v4: bool = False) -> None:
    """The v1 part, and the v2 part when ``v2``, with the v2/v4 checks and codes."""
    source, target, choices, history = value["source"], value["target"], value["choices"], value["history"]
    max_choices = MAX_V4_CHOICES if v4 else MAX_CHOICES
    if (not _text(source, text_limit) or not _text(target, text_limit)
            or not _direction(value["direction"]) or not _alignments(value["alignments"], source, target)
            or not isinstance(choices, list) or len(choices) > max_choices or not all(_choice(c) for c in choices)
            or not isinstance(history, list) or len(history) > MAX_HISTORY
            or not all(_snapshot(h, v4=v4) for h in history)):
        raise WorkfileError("workfile_invalid_content", _MSG_INVALID_CONTENT)
    original = [(c["source"], c["direction"], _mention(c["source_span"])) for c in choices]
    if len(set(original)) != len(original) or choice_conflict_groups(value):
        raise WorkfileError("workfile_duplicate_choices", _MSG_DUPLICATE_CHOICES)
    if v2:
        decisions, texts, snapshots = value["semantic_decisions"], value["semantic_texts"], value["semantic_history"]
        if (not isinstance(decisions, list) or len(decisions) > MAX_SEMANTIC_DECISIONS
                or not all(_decision(d) for d in decisions)
                or not isinstance(texts, list) or len(texts) > MAX_SEMANTIC_TEXTS
                or not all(_semantic_text(t, text_limit) for t in texts)
                or len({t["revision"] for t in texts}) != len(texts)
                or not isinstance(snapshots, list) or len(snapshots) > MAX_SEMANTIC_HISTORY
                or not all(_semantic_snapshot(h, text_limit) for h in snapshots)):
            raise WorkfileError("workfile_invalid_content", _MSG_INVALID_CONTENT)
        keys = [occurrence_key(d) for d in decisions]
        if len(set(keys)) != len(keys):
            raise WorkfileError("workfile_duplicate_choices", _MSG_DUPLICATE_CHOICES)


def _normalize_integral_numbers(root: Any) -> None:
    """Integral JSON numbers such as 3.0 read as 3, as they do in JavaScript."""
    stack = [root]
    while stack:
        node = stack.pop()
        for key, child in list(node.items() if isinstance(node, dict) else enumerate(node)):
            if isinstance(child, float) and child.is_integer():
                node[key] = int(child)
            elif isinstance(child, (dict, list)):
                stack.append(child)


# ---- v3 (public Web contracts/manuscript-v3/SPEC.md, "Reading" steps 3 to 10) ----

def _nonempty(value: Any, maximum: int) -> bool:
    return _text(value, maximum, False)


def _pair_shape(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 2 and _int(value[0], safe=True)
            and _int(value[1], safe=True) and 0 <= value[0] < value[1])


def _candidate_shape(value: Any) -> bool:
    return (_keys(value, _CANDIDATE_KEYS) and _hex(value["candidate_id"]) and _nonempty(value["zh"], 2000)
            and _nonempty(value["en"], 2000) and _nonempty(value["category"], 256))


def _choice_span_shape(span: Any) -> bool:
    return (_keys(span, {"start", "end", "text"}) and _int(span["start"], safe=True) and _int(span["end"], safe=True)
            and span["start"] >= 0 and span["end"] > span["start"] and _nonempty(span["text"], 2000)
            and span["end"] - span["start"] == len(span["text"]))


def _basis_v3(value: Any) -> bool:
    """A v3 basis: nothing is null or missing."""
    dictionary = value.get("dictionary") if isinstance(value, dict) else None
    return (_keys(value, {"rule_version", "dictionary", "matcher_revision"}) and value["rule_version"] == "review-v2"
            and _keys(dictionary, {"schema_version", "source_commit", "term_count", "revision"})
            and _nonempty(dictionary["schema_version"], 4096) and _nonempty(dictionary["source_commit"], 4096)
            and _int(dictionary["term_count"], safe=True) and dictionary["term_count"] >= 0
            and _hex(dictionary["revision"]) and _hex(value["matcher_revision"]))


def _check_result_shape(result: Any, *, v4: bool = False) -> None:
    if not (_keys(result, _RESULT_KEYS) and isinstance(result["truncated"], bool)
            and _int(result["evaluated"], safe=True) and result["evaluated"] >= 0
            and _int(result["not_evaluated"], safe=True) and result["not_evaluated"] >= 0
            and isinstance(result["findings"], list)):
        raise _segment_invalid()
    max_findings = MAX_V4_FINDINGS if v4 else MAX_RESULT_FINDINGS
    if len(result["findings"]) > max_findings:
        raise _too_many()
    for finding in result["findings"]:
        if v4 and isinstance(finding, dict) and "reason" in finding:
            valid_keys_and_verdict = (
                _keys(finding, _FINDING_KEYS | {"reason"})
                and finding["verdict"] == "not_evaluated"
                and isinstance(finding["reason"], str)
                and finding["reason"] in REASON_VALUES
            )
        else:
            valid_keys_and_verdict = (
                _keys(finding, _FINDING_KEYS)
                and isinstance(finding["verdict"], str)
                and finding["verdict"] in _VERDICTS
            )
        if not (valid_keys_and_verdict
                and _pair_shape(finding["source"]) and (finding["target"] is None or _pair_shape(finding["target"]))
                and isinstance(finding["candidates_truncated"], bool) and isinstance(finding["candidates"], list)):
            raise _segment_invalid()
        candidates = finding["candidates"]
        if len(candidates) > MAX_FINDING_CANDIDATES:
            raise _too_many()
        if (not all(_candidate_shape(c) for c in candidates)
                or len({c["candidate_id"] for c in candidates}) != len(candidates)):
            raise _segment_invalid()


def _check_choice_shape(choice: Any, history: bool) -> None:
    """A record choice (step 6) or a history choice (step 9)."""
    if (not _keys(choice, _HISTORY_CHOICE_KEYS if history else _SEGMENT_CHOICE_KEYS)
            or (history and not _hex(choice["source_hash"])) or not _choice_span_shape(choice["source_span"])):
        raise _segment_invalid()
    if choice["choice"] == "official_pair":
        if not _candidate_shape(choice["candidate"]):
            raise _segment_invalid()
    elif choice["choice"] != "not_a_term" or choice["candidate"] is not None:
        raise _segment_invalid()
    offered = choice["offered"]
    if not isinstance(offered, list):
        raise _segment_invalid()
    if len(offered) > MAX_OFFERED:
        raise _too_many()
    ascending = all(_hex(item) and (i == 0 or offered[i - 1] < item) for i, item in enumerate(offered))
    if (len(offered) != 0) if choice["choice"] == "official_pair" else not ascending:
        raise _segment_invalid()
    if not history and not isinstance(choice["confirmed"], bool):
        raise _segment_invalid()


def _check_record_shape(record: Any, source_length: int, target_length: int, *, v4: bool = False) -> None:
    if not _keys(record, _RECORD_KEYS):
        raise _segment_invalid()

    def within(start: Any, end: Any, length: int) -> bool:
        return _int(start, safe=True) and _int(end, safe=True) and 0 <= start <= end <= length

    if (not _int(record["index"], safe=True) or record["segmentation_version"] != SEGMENTATION_VERSION
            or not within(record["source_start"], record["source_end"], source_length)
            or not within(record["target_start"], record["target_end"], target_length)
            or not _hex(record["source_hash"]) or not _hex(record["target_hash"])
            or not (isinstance(record["status"], str) and record["status"] in ("pending", "done", "stale"))):
        raise _segment_invalid()
    # direction, alignment_input, basis and result are null exactly when the record is pending.
    live = record["status"] != "pending"
    if any((record[key] is None) == live for key in ("direction", "alignment_input", "basis", "result")):
        raise _segment_invalid()
    if live and (not _direction(record["direction"])
                 or not (record["alignment_input"] == ALIGNMENT_AUTOMATIC or _hex(record["alignment_input"]))):
        raise _segment_invalid()
    if record["basis"] is not None and not _basis_v3(record["basis"]):
        raise _segment_invalid()
    if record["result"] is not None:
        _check_result_shape(record["result"], v4=v4)
    choices = record["choices"]
    if not isinstance(choices, list):
        raise _segment_invalid()
    max_choices = MAX_V4_CHOICES if v4 else MAX_SEGMENT_CHOICES
    if len(choices) > max_choices:
        raise _too_many()
    for choice in choices:
        _check_choice_shape(choice, False)
    if not live and any(choice["confirmed"] for choice in choices):
        raise _segment_invalid()


def _check_record_content(record: dict[str, Any], source: str, target: str) -> None:
    source_core = source[record["source_start"]:record["source_end"]]
    target_core = target[record["target_start"]:record["target_end"]]
    live = record["status"] != "pending"
    findings = record["result"]["findings"] if live else []
    if live:
        if not (1 <= len(source_core) <= MAX_CORE_SCALARS and 1 <= len(target_core) <= MAX_CORE_SCALARS):
            raise _segment_invalid()
        seen: set[tuple[int, int]] = set()
        for finding in findings:
            span = (finding["source"][0], finding["source"][1])
            if (span[1] > len(source_core) or (finding["target"] is not None and finding["target"][1] > len(target_core))
                    or span in seen):
                raise _segment_invalid()
            seen.add(span)
    anchor = WorkfileError("workfile_segment_anchor_invalid", _MSG_SEGMENT_ANCHOR)
    for choice in record["choices"]:
        span = choice["source_span"]
        start, end = span["start"], span["end"]
        if end > len(source_core) or source_core[start:end] != span["text"]:
            raise anchor
        if not live or not choice["confirmed"]:
            continue
        finding = next((f for f in findings if f["source"][0] == start and f["source"][1] == end), None)
        if finding is None:
            raise anchor
        if choice["choice"] == "official_pair":
            if choice["candidate"] not in finding["candidates"]:
                raise anchor
        elif sorted(c["candidate_id"] for c in finding["candidates"]) != choice["offered"]:
            raise anchor
    places = [(c["source_span"]["start"], c["source_span"]["end"]) for c in record["choices"]]
    if len(set(places)) != len(places):
        raise WorkfileError("workfile_duplicate_choices", _MSG_DUPLICATE_CHOICES)


def _parse_v3(value: dict[str, Any], *, v4: bool = False) -> dict[str, Any]:
    _safe_tree(value, items=MAX_V3_ITEMS)  # step 3
    if not _keys(value, _TOP_V3):  # step 4
        raise WorkfileError("workfile_unsupported_format", "不支持此稿件格式或版本。")
    # Step 5: what lib parseWorkfile does to the v1/v2 part, including the 128-item array cap.
    _safe_tree({"format": WORKFILE_FORMAT_V2, **{key: value[key] for key in _V2_PART}})
    _check_legacy_parts(value, True, MAX_V3_TEXT_SCALARS, v4=v4)
    _normalize_integral_numbers(value)
    segments, source, target = value["segments"], value["source"], value["target"]
    if not isinstance(segments, list):
        raise _segment_invalid()
    for record in segments:  # step 6
        _check_record_shape(record, len(source), len(target), v4=v4)
    for position, record in enumerate(segments):  # step 7
        if record["index"] != position or (position and (
                record["source_start"] < segments[position - 1]["source_end"]
                or record["target_start"] < segments[position - 1]["target_end"])):
            raise WorkfileError("workfile_segment_order", _MSG_SEGMENT_ORDER)
    for record in segments:  # step 8
        _check_record_content(record, source, target)
    if not isinstance(value["segment_choice_history"], list):  # step 9
        raise _segment_invalid()
    for choice in value["segment_choice_history"]:
        _check_choice_shape(choice, True)
    progress = value["progress"]  # step 10
    invalid = WorkfileError("workfile_progress_invalid", _MSG_PROGRESS)
    if not _keys(progress, {"current", "completed"}):
        raise invalid
    current, completed = progress["current"], progress["completed"]
    if current is not None and not (_int(current, safe=True) and 0 <= current < len(segments)):
        raise invalid
    if not isinstance(completed, list) or not all(
            _int(n, safe=True) and (i == 0 or completed[i - 1] < n) and 0 <= n < len(segments)
            and segments[n]["status"] in ("done", "stale") for i, n in enumerate(completed)):
        raise invalid
    return value


def parse_workfile(content: str) -> dict[str, Any]:
    """Read a v1, v2, v3 or v4 workfile."""
    try:
        size = len(content.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise WorkfileError("workfile_not_json", _MSG_NOT_JSON) from exc
    if size > MAX_V3_BYTES:
        raise _too_large()
    try:
        value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        parsed = True
    except (ValueError, RecursionError):
        value, parsed = None, False
    if parsed and isinstance(value, dict) and value.get("format") in (WORKFILE_FORMAT_V3, WORKFILE_FORMAT_V4):
        return _parse_v3(value, v4=value.get("format") == WORKFILE_FORMAT_V4)
    # Every other input gets the v2 reader's result, starting with its own 1 MiB check.
    if size > MAX_WORKFILE_BYTES:
        raise _too_large()
    if not parsed:
        raise WorkfileError("workfile_not_json", _MSG_NOT_JSON)
    _safe_tree(value)
    v2 = isinstance(value, dict) and value.get("format") == WORKFILE_FORMAT_V2
    if not _keys(value, _TOP_V2 if v2 else _TOP) or value["format"] not in (WORKFILE_FORMAT, WORKFILE_FORMAT_V2):
        raise WorkfileError("workfile_unsupported_format", "不支持此稿件格式或版本。")
    _check_legacy_parts(value, v2, MAX_WORKFILE_BYTES)
    # Validation accepts integral JSON numbers such as 1200.0 (JavaScript
    # compatibility); offsets are used for slicing, so they become int here.
    for items in [value["alignments"]] + [snapshot["alignments"] for snapshot in value["history"]]:
        for item in items or ():
            for span in (item["source"], item["target"]):
                if span is not None:
                    span["start"], span["end"] = int(span["start"]), int(span["end"])
    return value


def _semantic_texts_needed(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Web's writer keeps only texts an ``ignored`` decision references, once each."""
    needed = set()
    for decision in payload.get("semantic_decisions") or []:
        if decision["decision"] == "ignored":
            needed.update((decision["source_revision"], decision["target_revision"]))
    seen: set[str] = set()
    out = []
    for entry in payload.get("semantic_texts") or []:
        if entry["revision"] in needed and entry["revision"] not in seen:
            seen.add(entry["revision"])
            out.append({"revision": entry["revision"], "text": entry["text"]})
    return out


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _minify(value: Mapping[str, Any]) -> str:
    """The canonical bytes of a v3 file: minified, no trailing newline (SPEC "Canonical bytes")."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


# Every dict of a v3 part is rebuilt in the SPEC's key order, so the order of the
# input never leaks into the output.

def _order_span(span: Mapping[str, Any]) -> dict[str, Any]:
    return {"start": span["start"], "end": span["end"], "text": span["text"]}


def _order_basis(basis: Mapping[str, Any]) -> dict[str, Any]:
    dictionary = basis["dictionary"]
    return {"rule_version": basis["rule_version"],
            "dictionary": {"schema_version": dictionary["schema_version"], "source_commit": dictionary["source_commit"],
                           "term_count": dictionary["term_count"], "revision": dictionary["revision"]},
            "matcher_revision": basis["matcher_revision"]}


def _order_candidate(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {"candidate_id": candidate["candidate_id"], "zh": candidate["zh"], "en": candidate["en"],
            "category": candidate["category"]}


def _order_choice(choice: Mapping[str, Any]) -> dict[str, Any]:
    return {"source_span": _order_span(choice["source_span"]), "choice": choice["choice"],
            "candidate": _order_candidate(choice["candidate"]), "offered": list(choice["offered"]),
            "confirmed": choice["confirmed"]}


def _order_history_choice(choice: Mapping[str, Any]) -> dict[str, Any]:
    return {"source_hash": choice["source_hash"], "source_span": _order_span(choice["source_span"]),
            "choice": choice["choice"], "candidate": _order_candidate(choice["candidate"]),
            "offered": list(choice["offered"])}


def _order_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    target = finding["target"]
    out: dict[str, Any] = {
        "verdict": finding["verdict"], "source": list(finding["source"]),
        "target": None if target is None else list(target),
        "candidates": [_order_candidate(c) for c in finding["candidates"]],
        "candidates_truncated": finding["candidates_truncated"],
    }
    if "reason" in finding:
        out["reason"] = finding["reason"]
    return out


def _order_result(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {"truncated": result["truncated"], "evaluated": result["evaluated"],
            "not_evaluated": result["not_evaluated"], "findings": [_order_finding(f) for f in result["findings"]]}


def _order_record(record: Mapping[str, Any]) -> dict[str, Any]:
    basis = record["basis"]
    # A pending record writes its choices unconfirmed (SPEC "Writing"); in the session they may still hold
    # their confirmed values until the re-check after an edit of the target alone (SPEC "Runtime" step 6).
    choices = [_order_choice(c) for c in record["choices"]]
    if record["status"] == "pending":
        choices = [{**c, "confirmed": False} for c in choices]
    return {"index": record["index"], "segmentation_version": record["segmentation_version"],
            "source_start": record["source_start"], "source_end": record["source_end"],
            "target_start": record["target_start"], "target_end": record["target_end"],
            "source_hash": record["source_hash"], "target_hash": record["target_hash"], "status": record["status"],
            "direction": record["direction"], "alignment_input": record["alignment_input"],
            "basis": None if basis is None else _order_basis(basis), "result": _order_result(record["result"]),
            "choices": choices}


def _v1_v2_content(payload: Mapping[str, Any], value: dict[str, Any]) -> str:
    """v1 bytes without semantic data; otherwise v2, dropping the oldest semantic
    history snapshot until the file fits 1 MiB. Decisions are never dropped."""
    decisions = list(payload.get("semantic_decisions") or [])
    snapshots = list(payload.get("semantic_history") or [])[-MAX_SEMANTIC_HISTORY:]
    if not decisions and not snapshots:
        return _dump({"format": WORKFILE_FORMAT, **value})
    texts = _semantic_texts_needed(payload)

    def v2(history: list) -> str:
        return _dump({"format": WORKFILE_FORMAT_V2, **value, "semantic_decisions": decisions,
                      "semantic_texts": texts, "semantic_history": history})
    content = v2(snapshots)
    while snapshots and len(content.encode("utf-8")) > MAX_WORKFILE_BYTES:
        snapshots.pop(0)
        content = v2(snapshots)
    return content


def _order_document_history(history: list[Any]) -> list[Any]:
    """Preserve the input key order of document-mode v4 findings (SPEC.md: the
    writer keeps the order it was given); no reordering is performed."""
    return list(history)


def _v1v2_legacy_fits(value: Mapping[str, Any]) -> bool:
    choices, history = value["choices"], value["history"]
    return (
        isinstance(choices, list)
        and len(choices) <= MAX_CHOICES
        and isinstance(history, list)
        and all(
            isinstance(h, Mapping)
            and len(h.get("resolutions") or ()) <= MAX_CHOICES
            and isinstance(h.get("report"), Mapping)
            and isinstance(h["report"].get("findings"), list)
            and len(h["report"]["findings"]) <= MAX_RESULT_FINDINGS
            and all(isinstance(f, Mapping) and "reason" not in f for f in h["report"]["findings"])
            for h in history
        )
    )


def _v3_content(payload: Mapping[str, Any], value: dict[str, Any]) -> str:
    """Minified canonical v3 or v4 bytes (lowest format that holds the payload).
    Only the oldest semantic history snapshots are dropped, and only while the
    file is over 44 MiB; a file still too large fails."""
    decisions = list(payload.get("semantic_decisions") or [])
    snapshots = list(payload.get("semantic_history") or [])[-MAX_SEMANTIC_HISTORY:]
    texts = _semantic_texts_needed(payload)
    try:
        records = [_order_record(r) for r in payload.get("segments") or []]
    except (KeyError, TypeError) as exc:
        raise _segment_invalid() from exc
    try:
        entries = [_order_history_choice(c) for c in payload.get("segment_choice_history") or []]
    except (KeyError, TypeError) as exc:
        raise _segment_invalid() from exc
    try:
        progress = payload.get("progress")
        if progress is None:
            progress = {"current": None, "completed": []}
        ordered = {"current": progress["current"], "completed": list(progress["completed"])}
    except (KeyError, TypeError) as exc:
        raise WorkfileError("workfile_progress_invalid", _MSG_PROGRESS) from exc

    v3_compatible = _v1v2_legacy_fits(value) and all(
        len(r["choices"]) <= MAX_SEGMENT_CHOICES
        and (
            r["result"] is None
            or (
                len(r["result"]["findings"]) <= MAX_RESULT_FINDINGS
                and all("reason" not in f for f in r["result"]["findings"])
            )
        )
        for r in records
    )
    fmt = WORKFILE_FORMAT_V3 if v3_compatible else WORKFILE_FORMAT_V4
    if fmt == WORKFILE_FORMAT_V4 and isinstance(value.get("history"), list):
        value = {**value, "history": _order_document_history(value["history"])}

    def write(history: list) -> str:
        return _minify({"format": fmt, **value, "semantic_decisions": decisions,
                        "semantic_texts": texts, "semantic_history": history, "segments": records,
                        "segment_choice_history": entries, "progress": ordered})
    content = write(snapshots)
    size = len(content.encode("utf-8"))
    while snapshots and size > MAX_V3_BYTES:
        snapshots.pop(0)
        content = write(snapshots)
        size = len(content.encode("utf-8"))
    if size > MAX_V3_BYTES:
        raise _too_large()
    return content


def serialize_workfile(payload: Mapping[str, Any]) -> str:
    """The lowest format that holds the payload (3-tier rule: v1/v2 -> v3 -> v4).

    1) ``v1v2_compatible`` (no segment records/history, <= 32 choices/findings/
       resolutions, no ``reason``) and UTF-8 bytes <= 1 MiB -> pretty v1 or v2;
    2) ``v3_compatible`` (all counts <= 32, no ``reason``) -> minified v3;
    3) Otherwise -> minified v4 (up to 44 MiB).
    The output is validated through ``parse_workfile``."""
    if not isinstance(payload, Mapping):
        raise WorkfileError("workfile_invalid_content", "稿件内容无效。")
    try:
        value = {key: payload[key] for key in ("source", "target", "direction", "alignments", "choices")}
        value["history"] = list(payload["history"])[-MAX_HISTORY:]
        if (not payload.get("segments") and not payload.get("segment_choice_history")
                and _v1v2_legacy_fits(value)):
            content = _v1_v2_content(payload, value)
            if len(content.encode("utf-8")) <= MAX_WORKFILE_BYTES:
                parse_workfile(content)
                return content
        content = _v3_content(payload, value)
        parse_workfile(content)
        return content
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, WorkfileError):
            raise
        raise WorkfileError("workfile_invalid_content", "稿件内容无效，无法保存。") from exc


def semantic_payload_fits(payload: Mapping[str, Any]) -> bool:
    """Whether the payload can be saved: valid and within the file limit (44 MiB in v3 and v4)."""
    try:
        serialize_workfile(payload)
    except WorkfileError:
        return False
    return True


def load_workfile(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as file:
        data = file.read(MAX_V3_BYTES + 1)
    if len(data) > MAX_V3_BYTES:
        raise _too_large()
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkfileError("workfile_not_json", _MSG_NOT_JSON) from exc
    return parse_workfile(content)


def save_workfile(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Flush a validated workfile, then replace the destination in one step."""
    content = serialize_workfile(payload).encode("utf-8")
    destination = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
