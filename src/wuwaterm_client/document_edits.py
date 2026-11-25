"""The workbench document's editable state: drafts, history and edit results.

Pure Python: no Qt, no HTTP, standard library only. ``DocumentRunController``
owns one document and composes these pieces with the runs; the commands
themselves are methods there, because an edit must commit through the same
payload and the same semantic session a run does.

A *draft* is what undo and redo restore: the editable keys of the manuscript,
the semantic decisions, and - by reference, as the payload holds them - the
long-mode parts (``segment_records``). Reports, the saved baseline and the
path are deliberately not part of it: undoing an edit must never erase a
report added afterwards or undo a save.

The Qt half keeps nothing of this: it hands its own view state (caret, focus,
scroll) in as an opaque value, gets it back on undo or redo, and renders what
an :class:`EditOutcome` says changed.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import manuscript

# The editable keys of a draft, in the order a draft is written.
DRAFT_KEYS = ("source", "target", "direction", "alignments", "choices")
# The keys a review's answer depends on: a change to one makes the report stale.
REVIEW_KEYS = DRAFT_KEYS
# The v3 long-mode parts. Their values are never changed in place, so copies of
# the payload share them instead of copying hundreds of records on every keystroke.
PART_KEYS = ("segments", "segment_choice_history", "progress")
# How many steps undo keeps.
HISTORY_LIMIT = 20

# Why a command changed nothing. The host turns these into the workbench's own
# wording; nothing here is a message.
REASON_NOT_APPLICABLE = "not_applicable"  # the command does not apply right now
REASON_RECONFIRM = "reconfirm"  # the basis or the span is no longer executable
REASON_STALE = "stale"  # the span moved while the command was being applied
REASON_REJECTED = "rejected"  # the editor cannot display the result
REASON_FULL = "full"  # the manuscript's decision or size cap would be exceeded
REASON_FAILED = "failed"  # the segment records refused the change


def copy_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy that shares the immutable long-mode parts."""
    return {key: item if key in PART_KEYS else copy.deepcopy(item) for key, item in value.items()}


def normalize_semantic(payload: dict) -> dict:
    """Semantic keys are present together, and only with semantic data.

    A manuscript without semantic data stays byte-for-byte a v1 workfile, so
    an untouched draft never looks edited because of an empty list.
    """
    decisions = payload.get("semantic_decisions") or []
    history = payload.get("semantic_history") or []
    if decisions or history:
        payload["semantic_decisions"], payload["semantic_history"] = decisions, history
        payload["semantic_texts"] = payload.get("semantic_texts") or []
    else:
        for key in ("semantic_decisions", "semantic_texts", "semantic_history"):
            payload.pop(key, None)
    return payload


def draft_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The undoable draft of ``payload``: editable keys and decisions copied,
    the long-mode parts shared by reference."""
    value = {key: payload[key] for key in DRAFT_KEYS}
    # Decisions are part of the draft, so undo and redo take them back too.
    value["semantic_decisions"] = payload.get("semantic_decisions", [])
    value = copy.deepcopy(value)
    for key in PART_KEYS:
        if key in payload:
            value[key] = payload[key]
    return value


def restore_draft_into(payload: dict[str, Any], value: Mapping[str, Any]) -> None:
    """Write ``value`` into ``payload`` in place, normalized as the payload
    holds semantic keys."""
    value = copy_payload(dict(value))
    decisions = value.pop("semantic_decisions", [])
    for key in PART_KEYS:
        payload.pop(key, None)
    payload.update(value)
    payload["semantic_decisions"] = decisions
    payload.setdefault("semantic_history", [])
    payload.setdefault("semantic_texts", [])
    normalize_semantic(payload)


def merge_draft(before: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    """``value`` laid over ``before``: keys the draft does not carry keep their
    current value, and a draft without long-mode parts leaves long mode."""
    merged: dict[str, Any] = {**before, **copy_payload(dict(value))}
    for key in PART_KEYS:
        if key not in value:
            merged.pop(key, None)
    return merged


@dataclass(frozen=True)
class EditOutcome:
    """What a committed edit means to the host.

    ``committed`` False: nothing changed and nothing is to be shown.
    ``semantic_cleared``: the semantic results of the previous direction were
    dropped, so the host says so. ``review_inputs_changed``: something a
    report was computed from changed, so the listed report is stale.
    """

    committed: bool = False
    semantic_cleared: bool = False
    review_inputs_changed: bool = False


@dataclass(frozen=True)
class CommandOutcome:
    """The result of one document command: whether it committed and why not.

    ``edit`` is the commit it made, when it made one, so the host applies the
    same effects (refresh, stale report) an edit by hand does.
    """

    committed: bool = False
    reason: str = ""
    edit: EditOutcome | None = None

    @property
    def changed(self) -> bool:
        """Whether the document itself changed (a command can commit without
        changing anything, when the records already held that state)."""
        return self.edit is not None and self.edit.committed

    def __bool__(self) -> bool:
        return self.committed


@dataclass(frozen=True)
class HistoryEntry:
    """One step of the undo/redo history: the draft, the semantic session at
    that step, and the host's own view state (opaque here)."""

    value: dict[str, Any]
    session: dict[str, Any]
    view: dict[str, Any] | None = None


@dataclass(frozen=True)
class HistoryOutcome:
    """One undo or redo: the step the host restores, and what it refreshes."""

    entry: HistoryEntry
    edit: EditOutcome


class EditHistory:
    """The undo and redo stacks of one document, bounded to ``limit`` steps.

    ``record`` is what an edit does; ``step`` is what undo and redo do, and it
    records the state it leaves on the other stack, so both directions keep
    working after a mix of edits, undos and redos.
    """

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self.limit = limit
        self.undo: list[HistoryEntry] = []
        self.redo: list[HistoryEntry] = []

    def record(self, entry: HistoryEntry) -> None:
        self.undo = (self.undo + [entry])[-self.limit:]
        self.redo = []

    def step(self, *, undo: bool, current: HistoryEntry) -> HistoryEntry | None:
        source = self.undo if undo else self.redo
        if not source:
            return None
        entry = source.pop()
        target = self.redo if undo else self.undo
        target[:] = (target + [current])[-self.limit:]
        return entry

    def clear(self) -> None:
        self.undo, self.redo = [], []

    def can_undo(self) -> bool:
        return bool(self.undo)

    def can_redo(self) -> bool:
        return bool(self.redo)


def compute_minimal_splice(before: str, after: str) -> tuple[int, int, int] | None:
    """Minimal ``(start, end, replacement_len)`` scalar splice transforming ``before`` to ``after``."""
    if before == after:
        return None
    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    rem_before = len(before) - prefix
    rem_after = len(after) - prefix
    while suffix < rem_before and suffix < rem_after and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]:
        suffix += 1
    return prefix, len(before) - suffix, len(after) - suffix - prefix


def _remap_span_after_splice(
    span: dict[str, Any],
    new_text: str,
    start: int,
    end: int,
    delta: int,
    *,
    allow_empty: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """Remap one alignment span after splicing ``[start:end]`` by ``delta`` scalars.

    Returns ``(ok, mapped_span)``. ``ok`` is ``False`` when the splice crosses
    a span boundary, goes out of bounds, or empties a non-nullable span.
    """
    ss, se = int(span["start"]), int(span["end"])
    if se <= start:
        return True, copy.deepcopy(span)
    if ss >= end:
        ns, ne = ss + delta, se + delta
        if ns < 0 or ne > len(new_text) or ne <= ns:
            return False, None
        return True, {"start": ns, "end": ne, "text": new_text[ns:ne]}
    if ss <= start and end <= se:
        ne = se + delta
        if ne <= ss:
            return (True, None) if allow_empty else (False, None)
        if ne > len(new_text):
            return False, None
        return True, {"start": ss, "end": ne, "text": new_text[ss:ne]}
    return False, None


def remap_alignments_for_target_splice(
    alignments: list[dict[str, Any]] | None,
    source: str,
    next_target: str,
    start: int,
    end: int,
    replacement_len: int,
    *,
    fill_source_span: tuple[int, int] | None = None,
    fill_target_span: tuple[int, int] | None = None,
    fill_pair_source_span: tuple[int, int] | None = None,
) -> list[dict[str, Any]] | None:
    """Remap explicit ``alignments`` after splicing ``target[start:end]`` to ``replacement_len`` scalars.

    Preserves automatic correspondence (``None``), fills an explicit
    ``target: null`` region when requested, shifts trailing regions by the
    length delta, resizes an enclosing region (for localized edits or single-
    segment re-translation), and falls back to ``[]`` if the splice crosses a
    region boundary, leaves a requested fill unmatched, or fails schema
    validation.
    """
    if alignments is None:
        return None
    if not alignments:
        return []
    delta = replacement_len - (end - start)
    remapped: list[dict[str, Any]] = []
    matched_fills = 0
    for region in alignments:
        s_span = copy.deepcopy(region["source"])
        t_span = region["target"]
        if t_span is None:
            matches_fill = (
                fill_source_span is not None
                and fill_target_span is not None
                and (
                    (s_span["start"], s_span["end"]) == fill_source_span
                    or (
                        fill_pair_source_span is not None
                        and fill_pair_source_span[0] <= s_span["start"]
                        and s_span["end"] <= fill_pair_source_span[1]
                    )
                )
            )
            if matches_fill:
                assert fill_target_span is not None
                fs, fe = fill_target_span
                new_t = {"start": fs, "end": fe, "text": next_target[fs:fe]} if fe > fs else None
                matched_fills += 1
            else:
                new_t = None
        else:
            ok, new_t = _remap_span_after_splice(t_span, next_target, start, end, delta, allow_empty=True)
            if not ok:
                return []
        remapped.append({"source": s_span, "target": new_t})
    if fill_source_span is not None and matched_fills != 1:
        return []
    return remapped if manuscript._alignments(remapped, source, next_target) else []


def remap_alignments_for_side_edit(
    alignments: list[dict[str, Any]] | None,
    side: str,
    old_source: str,
    old_target: str,
    new_source: str,
    new_target: str,
    edit: tuple[int, int, int] | None = None,
) -> list[dict[str, Any]] | None:
    """Remap explicit ``alignments`` after editing either ``source`` or ``target``.

    Uses ``edit`` (``(start, end, replacement_len)``) when available or derives
    the minimal splice between the old and new side text. Preserves ``None`` and
    ``[]``, shifts trailing spans, resizes enclosing spans, and falls back to
    ``[]`` on boundary-crossing edits, empty source collapse, or invalid result.
    """
    if alignments is None:
        return None
    if not alignments:
        return []
    if side not in ("source", "target"):
        return []
    before_text = old_source if side == "source" else old_target
    after_text = new_source if side == "source" else new_target
    splice = edit if edit is not None else compute_minimal_splice(before_text, after_text)
    if splice is None:
        copied = copy.deepcopy(alignments)
        return copied if manuscript._alignments(copied, new_source, new_target) else []
    start, end, replacement_len = splice
    if side == "target":
        return remap_alignments_for_target_splice(alignments, new_source, new_target, start, end, replacement_len)
    delta = replacement_len - (end - start)
    remapped: list[dict[str, Any]] = []
    for region in alignments:
        ok, new_s = _remap_span_after_splice(region["source"], new_source, start, end, delta, allow_empty=False)
        if not ok or new_s is None:
            return []
        remapped.append({"source": new_s, "target": copy.deepcopy(region["target"])})
    return remapped if manuscript._alignments(remapped, new_source, new_target) else []
