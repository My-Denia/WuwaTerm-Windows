"""Pure-Python workspace I/O: open/save/import/export, recovery drafts, dirty baseline.

Holds no Qt and no HTTP dependencies so every persistence, import validation,
recovery snapshot, and Markdown report projection rule can be tested headlessly.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import markdown_report as mdr
from .. import segment_records as sr
from .. import strings
from ..document_edits import copy_payload, normalize_semantic
from ..manuscript import empty_workfile, parse_workfile, save_workfile, serialize_workfile
from ..segmentation import STATUS_READY
from .review_coordinator import offset_span

MAX_IMPORT_BYTES = 1_048_576
TEXT_SUFFIXES = (".txt", ".md")


class WorkspaceState:
    """Tracks the open manuscript's disk path and saved baseline payload."""

    def __init__(self, initial_payload: Mapping[str, Any] | None = None) -> None:
        self.path: Path | None = None
        self.saved_payload: dict[str, Any] = copy_payload(dict(initial_payload)) if initial_payload is not None else empty_workfile()

    def is_dirty(self, payload: Mapping[str, Any]) -> bool:
        return payload != self.saved_payload

    def title(self) -> str:
        return self.path.name if self.path is not None else strings.MANUSCRIPT_UNTITLED

    def mark_saved(self, path: Path, payload: Mapping[str, Any]) -> None:
        self.path = path
        self.saved_payload = copy_payload(dict(payload))

    def mark_opened(self, path: Path, payload: Mapping[str, Any]) -> None:
        self.path = path
        self.saved_payload = copy_payload(dict(payload))

    def mark_new(self, payload: Mapping[str, Any]) -> None:
        self.path = None
        self.saved_payload = copy_payload(dict(payload))

    def mark_recovered(self, source_path: str | None) -> None:
        self.path = Path(source_path) if source_path else None
        self.saved_payload = empty_workfile()


def atomic_write_bytes(destination: Path, content: bytes) -> None:
    """Write ``content`` to ``destination`` via a sibling temp file and atomic replace."""
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def has_recovery_content(payload: Mapping[str, Any]) -> bool:
    return any(bool(payload.get(k)) for k in ("source", "target", "choices", "history", "semantic_decisions"))


def prepare_recovery_snapshot(
    payload: Mapping[str, Any],
    semantic_texts_fn: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> dict[str, Any]:
    snapshot = copy_payload(dict(payload))
    if snapshot.get("semantic_decisions") or snapshot.get("semantic_history"):
        snapshot.setdefault("semantic_history", [])
        snapshot["semantic_texts"] = semantic_texts_fn(snapshot)
        normalize_semantic(snapshot)
    return snapshot


def is_report_file(path: Path) -> bool:
    """Whether ``path`` starts with the Markdown report export header."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return mdr.is_report_export(head.decode("utf-8", errors="replace"))


def read_import_text(path: Path) -> tuple[str | None, str | None]:
    """Read a dropped/imported plain text file. Returns ``(text, error_message)``."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_IMPORT_BYTES + 1)
    except OSError as exc:
        return None, strings.IMPORT_READ_FAILED.format(reason=exc.strerror or type(exc).__name__)
    if len(data) > MAX_IMPORT_BYTES:
        return None, strings.IMPORT_TOO_LARGE
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, strings.IMPORT_NOT_UTF8
    if mdr.is_report_export(text):
        return None, strings.MANUSCRIPT_REPORT_NOT_MANUSCRIPT
    return text, None


def build_prose_export(payload: Mapping[str, Any]) -> bytes:
    source_text = payload["source"]
    target_text = payload["target"]
    sections = [source_text]
    if target_text:
        sections.append(f"---\n\n{target_text}")
    return ("\n\n".join(sections)).encode("utf-8")


def save_manuscript_file(
    path: Path,
    payload: dict[str, Any],
    *,
    segment_cursor: int | None,
) -> tuple[dict[str, Any], sr.Parts, tuple[str, ...]]:
    """Save ``payload`` to ``path`` and return normalized post-save metadata.

    Raises ``WorkfileError`` or ``OSError`` on failure without mutating ``payload``.
    """
    parts = sr.parts_of(payload)
    to_write = payload
    if parts.segments and segment_cursor is not None and 0 <= segment_cursor < len(parts.segments):
        to_write = sr.with_parts(payload, sr.set_current(parts, segment_cursor))
    save_workfile(path, to_write)
    written = parse_workfile(serialize_workfile(to_write))
    adopted_parts, features = sr.adopt_file_features(to_write, written.get("review_features") or ())
    return written, adopted_parts, features


def build_report_input(
    *,
    payload: Mapping[str, Any],
    review_state: str,
    session_snapshots: Sequence[dict[str, Any]],
    long_mode: bool,
    records: Sequence[dict[str, Any]] | None,
    term_state_fn: Callable[[Mapping[str, Any]], str],
    issues: Sequence[Any],
    decisions: Sequence[Any],
    issue_state_fn: Callable[[Any], str],
    pairs: Sequence[Any],
    session_result_for: Callable[[Any, str, str], Any],
    is_session_choice: Callable[[Mapping[str, Any] | None], bool],
) -> dict[str, Any]:
    """Assemble the pure-Python Markdown report projection matching the Web contract."""
    source, target, direction = payload["source"], payload["target"], payload["direction"]

    def snapshot(entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        return {
            **entry,
            "resolutions": entry.get("resolutions"),
            "trusted": any(entry == own for own in session_snapshots),
        }

    history = [] if long_mode else payload["history"]
    latest = snapshot(history[-1] if history else None)
    previous = snapshot(history[-2] if len(history) > 1 else None)
    current = bool(
        latest
        and latest["trusted"]
        and review_state == "current"
        and latest["source"] == source
        and latest["target"] == target
        and latest["direction"] == direction
        and latest["alignments"] == payload["alignments"]
    )
    currency = "none" if latest is None else "current" if current else "stale" if latest["trusted"] else "imported"
    draft = {"source": source, "target": target, "direction": direction, "alignments": payload["alignments"]}

    segment_terms = None
    if records is not None:
        segment_terms = []
        for index, record in enumerate(records):
            state = term_state_fn(record)
            if record["result"] is None and not record["choices"]:
                continue
            findings = []
            if record["result"] is not None:
                for finding in sr.expand_result(record, source, target)["findings"]:
                    exported_finding: dict[str, Any] = {
                        "verdict": finding["verdict"],
                        "source_span": offset_span(finding["source_span"], record["source_start"]),
                        "target_span": offset_span(finding["target_span"], record["target_start"]),
                        "candidates": [{"zh": c["zh"], "en": c["en"]} for c in finding["candidates"]],
                        "candidates_truncated": finding["candidates_truncated"] is True,
                    }
                    if "reason" in finding:
                        exported_finding["reason"] = finding["reason"]
                    findings.append(exported_finding)
            choices = [
                {**choice, "source_span": offset_span(choice["source_span"], record["source_start"])}
                for choice in record["choices"]
            ]
            truncated = record["result"] is not None and record["result"]["truncated"] is True
            segment_terms.append({
                "index": index,
                "state": state,
                "stale": state != "current",
                "truncated": truncated,
                "findings": findings,
                "choices": choices,
            })

    semantic_part = None
    if issues or decisions:
        statuses = {"needs_reconfirm": "needs_reconfirmation", "ignored_carried": "ignored"}
        items = []
        for issue in issues:
            raw = issue.issue
            state = issue_state_fn(issue)
            items.append({
                "issue_id": raw["id"],
                "category": raw["category"],
                "confidence": raw["confidence"],
                "segment_index": raw.get("segment_index"),
                "source_excerpt": raw.get("source_excerpt"),
                "target_excerpt": raw.get("target_excerpt"),
                "rationale": raw["rationale"],
                "suggestion": raw["suggestion"]["replacement"] if raw.get("suggestion") else None,
                "suggestion_withheld": raw.get("suggestion_withheld"),
                "status": statuses.get(state, state),
            })
        ready = [pair for pair in pairs if pair.status == STATUS_READY]
        reviewed = sum(
            1
            for pair in ready
            if (result := session_result_for(pair, source, target)) is not None and result.report is not None
        )
        semantic_part = {
            "items": items,
            "resolved": sum(1 for item in items if item["status"] in ("accepted", "adjusted", "ignored")),
            "segments": {"total": len(ready), "reviewed": reviewed},
        }

    if long_mode:
        rec_list = records or []
        if not any(record["result"] is not None for record in rec_list):
            currency = "none"
        elif all(pair.status == STATUS_READY for pair in pairs) and all(term_state_fn(r) == "current" for r in rec_list):
            currency = "current"
        else:
            currency = "stale"

    return {
        "draft": draft,
        "latest": latest,
        "previous": previous,
        "currency": currency,
        "choices": payload["choices"],
        "reconciled": mdr.reconcile_choices(payload["choices"], draft, latest, own=is_session_choice),
        "comparison": mdr.compare_reports(previous, latest) if latest and previous else None,
        "semantic": semantic_part,
        "segment_terms": segment_terms,
        "segment_choice_history": len(payload.get("segment_choice_history") or []),
    }


def render_report_markdown(report_input: Mapping[str, Any]) -> str:
    return mdr.render(dict(report_input))
