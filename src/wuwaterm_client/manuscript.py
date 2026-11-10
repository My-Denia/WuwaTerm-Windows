"""Read and safely save the public Web's v1 manuscript workfiles.

Reports and decisions in imported files are historical data.  This module does
not grant them runtime trust; a presentation layer must request a new review.
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
MAX_WORKFILE_BYTES = 1_048_576
MAX_CHOICES = 32
MAX_HISTORY = 2
MAX_SEMANTIC_DECISIONS = 128
MAX_SEMANTIC_HISTORY = 2
MAX_SEMANTIC_TEXTS = 16
MAX_HISTORY_SEGMENTS = 64
_HEX = re.compile(r"^[a-f0-9]{64}$")
_TOP = {"format", "source", "target", "direction", "alignments", "choices", "history"}
_TOP_V2 = _TOP | {"semantic_decisions", "semantic_texts", "semantic_history"}
_DECISION_KEYS = {"issue_id", "fingerprint", "category", "segment_index", "source_revision",
                  "target_revision", "source_span", "target_span", "decision", "applied_text", "basis"}
_VERDICTS = {"verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated"}


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
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


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


def valid_report(value: Any, source: str, target: str) -> bool:
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
            or len(findings) > 32):
        return False
    ids: set[str] = set()
    for finding in findings:
        if not _keys(finding, {"id", "rule_id", "verdict", "source_span", "target_span",
                               "candidates", "candidates_truncated"}):
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


def _resolutions(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > MAX_CHOICES:
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


def _snapshot(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    fields = {"source", "target", "direction", "alignments", "report"}
    if "resolutions" in value:
        fields.add("resolutions")
    return (_keys(value, fields) and _text(value["source"]) and _text(value["target"])
            and _direction(value["direction"]) and _alignments(value["alignments"], value["source"], value["target"])
            and valid_report(value["report"], value["source"], value["target"])
            and ("resolutions" not in value or _resolutions(value["resolutions"])))


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


def _semantic_text(value: Any) -> bool:
    return _keys(value, {"revision", "text"}) and _hex(value["revision"]) and _text(value["text"], MAX_WORKFILE_BYTES)


def _history_segment(item: Any, source: str, target: str) -> bool:
    if not _keys(item, {"segment_index", "source_start", "source_end", "target_start", "target_end", "report"}):
        return False
    a, b, c, d = (item[k] for k in ("source_start", "source_end", "target_start", "target_end"))
    return (_strict_int(item["segment_index"]) and item["segment_index"] >= 0
            and all(_strict_int(x) for x in (a, b, c, d)) and 0 <= a <= b <= len(source) and 0 <= c <= d <= len(target)
            and valid_semantic_report(item["report"], source[a:b], target[c:d]))


def _semantic_snapshot(value: Any) -> bool:
    """One semantic run over a whole manuscript: untrusted reference data."""
    return (_keys(value, {"source", "target", "direction", "segments"})
            and _text(value["source"], MAX_WORKFILE_BYTES) and _text(value["target"], MAX_WORKFILE_BYTES)
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


def _safe_tree(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise WorkfileError("workfile_too_deep", "稿件文件嵌套过深。")
    if isinstance(value, list):
        if len(value) > 128:
            raise WorkfileError("workfile_too_many_entries", "稿件文件条目过多。")
        for child in value:
            _safe_tree(child, depth + 1)
    elif isinstance(value, dict):
        if len(value) > 32:
            raise WorkfileError("workfile_too_many_fields", "稿件文件字段过多。")
        for key, child in value.items():
            if key in ("__proto__", "constructor", "prototype"):
                raise WorkfileError("workfile_forbidden_field", "稿件文件含不支持的字段。")
            _safe_tree(child, depth + 1)


def parse_workfile(content: str) -> dict[str, Any]:
    try:
        size = len(content.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise WorkfileError("workfile_not_json", "无法读取 JSON 稿件文件。") from exc
    if size > MAX_WORKFILE_BYTES:
        raise WorkfileError("workfile_too_large", "稿件文件不能超过 1 MiB。")
    try:
        value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as exc:
        raise WorkfileError("workfile_not_json", "无法读取 JSON 稿件文件。") from exc
    _safe_tree(value)
    v2 = isinstance(value, dict) and value.get("format") == WORKFILE_FORMAT_V2
    if not _keys(value, _TOP_V2 if v2 else _TOP) or value["format"] not in (WORKFILE_FORMAT, WORKFILE_FORMAT_V2):
        raise WorkfileError("workfile_unsupported_format", "不支持此稿件格式或版本。")
    source, target, choices, history = value["source"], value["target"], value["choices"], value["history"]
    if (not _text(source, MAX_WORKFILE_BYTES) or not _text(target, MAX_WORKFILE_BYTES)
            or not _direction(value["direction"]) or not _alignments(value["alignments"], source, target)
            or not isinstance(choices, list) or len(choices) > MAX_CHOICES or not all(_choice(c) for c in choices)
            or not isinstance(history, list) or len(history) > MAX_HISTORY or not all(_snapshot(h) for h in history)):
        raise WorkfileError("workfile_invalid_content", "稿件内容、选择或位置依据无效；当前稿件未被替换。")
    original = [(c["source"], c["direction"], _mention(c["source_span"])) for c in choices]
    if len(set(original)) != len(original) or choice_conflict_groups(value):
        raise WorkfileError("workfile_duplicate_choices", "稿件包含同一位置的重复选择，未导入。")
    if v2:
        decisions, texts, snapshots = value["semantic_decisions"], value["semantic_texts"], value["semantic_history"]
        if (not isinstance(decisions, list) or len(decisions) > MAX_SEMANTIC_DECISIONS
                or not all(_decision(d) for d in decisions)
                or not isinstance(texts, list) or len(texts) > MAX_SEMANTIC_TEXTS
                or not all(_semantic_text(t) for t in texts)
                or len({t["revision"] for t in texts}) != len(texts)
                or not isinstance(snapshots, list) or len(snapshots) > MAX_SEMANTIC_HISTORY
                or not all(_semantic_snapshot(h) for h in snapshots)):
            raise WorkfileError("workfile_invalid_content", "稿件内容、选择或位置依据无效；当前稿件未被替换。")
        keys = [occurrence_key(d) for d in decisions]
        if len(set(keys)) != len(keys):
            raise WorkfileError("workfile_duplicate_choices", "稿件包含同一位置的重复选择，未导入。")
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


def serialize_workfile(payload: Mapping[str, Any]) -> str:
    """v1 bytes without semantic data; otherwise v2, dropping the oldest
    semantic history snapshot until the file fits. Decisions are never dropped."""
    if not isinstance(payload, Mapping):
        raise WorkfileError("workfile_invalid_content", "稿件内容无效。")
    try:
        value = {key: payload[key] for key in ("source", "target", "direction", "alignments", "choices")}
        value["history"] = list(payload["history"])[-MAX_HISTORY:]
        decisions = list(payload.get("semantic_decisions") or [])
        snapshots = list(payload.get("semantic_history") or [])[-MAX_SEMANTIC_HISTORY:]
        if decisions or snapshots:
            texts = _semantic_texts_needed(payload)

            def v2(history: list) -> str:
                return _dump({"format": WORKFILE_FORMAT_V2, **value, "semantic_decisions": decisions,
                              "semantic_texts": texts, "semantic_history": history})
            content = v2(snapshots)
            while snapshots and len(content.encode("utf-8")) > MAX_WORKFILE_BYTES:
                snapshots.pop(0)
                content = v2(snapshots)
        else:
            content = _dump({"format": WORKFILE_FORMAT, **value})
        parse_workfile(content)
        return content
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, WorkfileError):
            raise
        raise WorkfileError("workfile_invalid_content", "稿件内容无效，无法保存。") from exc


def semantic_payload_fits(payload: Mapping[str, Any]) -> bool:
    """Whether the payload can be saved: valid and within the 1 MiB file limit."""
    try:
        serialize_workfile(payload)
    except WorkfileError:
        return False
    return True


def load_workfile(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as file:
        data = file.read(MAX_WORKFILE_BYTES + 1)
    if len(data) > MAX_WORKFILE_BYTES:
        raise WorkfileError("workfile_too_large", "稿件文件不能超过 1 MiB。")
    try:
        return parse_workfile(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise WorkfileError("workfile_not_json", "无法读取 JSON 稿件文件。") from exc


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
