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
from dataclasses import dataclass, field, replace
from typing import Any

from . import manuscript

# The editable keys of a draft, in the order a draft is written.
DRAFT_KEYS = ("source", "target", "direction", "alignments", "choices")
# The keys a review's answer depends on: a change to one makes the report stale.
REVIEW_KEYS = DRAFT_KEYS
# The v3 long-mode parts. Their values are never changed in place, so copies of
# the payload share them instead of copying hundreds of records on every keystroke.
PART_KEYS = ("segments", "segment_choice_history", "progress")
# How many steps undo keeps (contracts/workbench-state/edit-transactions.json R8).
HISTORY_LIMIT = 50
# A one-scalar edit on the same side within this many milliseconds of the last
# one joins its undo step (R8).
TYPING_COALESCE_MS = 1200
# The longest span an alignment entry may hold, in scalars (R6).
MAX_SPAN_SCALARS = 2000
# The session list of invalidated alignments keeps the latest entries, at most
# the 64 an alignment list can hold.
MAX_SESSION_INVALIDATIONS = 64
# Why an edit transaction took an entry out of the alignment list: the edit
# crosses one of its span boundaries (or replaces the whole span together with
# text outside it), deletes its source text, a target fill matched more than
# one untranslated entry, or an edit inside the span grew it past the limit.
INVALIDATION_REASONS = ("crossed_boundary", "source_emptied", "fill_ambiguous", "span_too_long")
# What an edit did to the explicit alignments, for the workbench's notice:
# none of them is left, or some were invalidated and the others kept.
NOTICE_ALL = "all"
NOTICE_PARTIAL = "partial"

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
    ``alignment_notice``: ``NOTICE_ALL`` when the edit left none of the
    previous explicit alignments, ``NOTICE_PARTIAL`` when it invalidated some
    and kept the others, else None. ``invalidated`` and ``emptied_targets``
    count the entries it took out and the entries whose target it emptied
    (they stay, with ``target: null``); ``kept`` counts the entries left.
    """

    committed: bool = False
    semantic_cleared: bool = False
    review_inputs_changed: bool = False
    alignment_notice: str | None = None
    invalidated: int = 0
    emptied_targets: int = 0
    kept: int = 0


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
    that step, and the host's own view state (opaque here).

    ``invalidations`` is the session list of alignments edits had invalidated
    by then, so stepping back restores every entry a later edit took out.
    ``serial`` names the step; a typing run joins the step it opened only
    while that step is still the newest one.
    """

    value: dict[str, Any]
    session: dict[str, Any]
    view: dict[str, Any] | None = None
    invalidations: tuple[dict[str, Any], ...] = ()
    serial: int = 0


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
        self._serial = 0

    def record(self, entry: HistoryEntry) -> int:
        """Push ``entry`` as a new step; a new step clears the redo steps.
        Returns the step's serial."""
        self._serial += 1
        self.undo = (self.undo + [replace(entry, serial=self._serial)])[-self.limit:]
        self.redo = []
        return self._serial

    def drop_redo(self) -> None:
        """An edit joined the newest step: what was undone cannot come back."""
        self.redo = []

    def top_serial(self) -> int | None:
        """The serial of the newest undo step, if there is one."""
        return self.undo[-1].serial if self.undo else None

    def step(self, *, undo: bool, current: HistoryEntry) -> HistoryEntry | None:
        source = self.undo if undo else self.redo
        if not source:
            return None
        entry = source.pop()
        target = self.redo if undo else self.undo
        self._serial += 1
        target[:] = (target + [replace(current, serial=self._serial)])[-self.limit:]
        return entry

    def clear(self) -> None:
        self.undo, self.redo = [], []

    def can_undo(self) -> bool:
        return bool(self.undo)

    def can_redo(self) -> bool:
        return bool(self.redo)


# -- edit transactions (contracts/workbench-state/edit-transactions.json) -----
#
# An edit transaction is one change of the source or the target text and what
# it does to the explicit alignments: entries outside the change are kept or
# shifted, an entry holding it is resized, and only the entries it crosses, or
# whose source text it deletes, leave the list. A target span whose text the
# change deletes becomes ``target: None`` and keeps its source anchor, so the
# segment can be translated again. Offsets are Unicode scalars (Python str
# indexes), the same numbers the Web counts with ``Array.from``.


@dataclass(frozen=True)
class AlignmentRemap:
    """The alignments one transaction keeps, and what it took out.

    ``invalidated`` holds ``{"original_index", "reason", "entry"}`` per entry
    that left the list (``entry`` as it was before the edit); ``emptied_targets``
    holds ``{"original_index", "entry"}`` per entry whose target the edit
    deleted. ``fallback_all`` is the safety net: only malformed input clears
    every alignment.
    """

    alignments: list[dict[str, Any]] | None
    invalidated: tuple[dict[str, Any], ...] = field(default=())
    emptied_targets: tuple[dict[str, Any], ...] = field(default=())
    fallback_all: bool = False

    @property
    def touched(self) -> bool:
        """Whether the edit invalidated or emptied any entry."""
        return bool(self.invalidated or self.emptied_targets or self.fallback_all)

    def notice(self, previous: list | None) -> str | None:
        """``NOTICE_ALL`` when the edit left none of ``previous`` explicit
        alignments, ``NOTICE_PARTIAL`` when it invalidated only some, else None."""
        if not previous:
            return None
        if self.alignments is not None and not self.alignments:
            return NOTICE_ALL
        return NOTICE_PARTIAL if self.invalidated else None


def _copy_span(span: Mapping[str, Any]) -> dict[str, Any]:
    return {"start": span["start"], "end": span["end"], "text": span["text"]}


def _copy_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    target = entry["target"]
    return {"source": _copy_span(entry["source"]), "target": None if target is None else _copy_span(target)}


def _slice_span(text: str, start: int, end: int) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text[start:end]}


def _unchanged(alignments: list | None) -> AlignmentRemap:
    return AlignmentRemap(alignments)


def _fallback_all(invalidated: tuple = ()) -> AlignmentRemap:
    return AlignmentRemap([], tuple(invalidated), (), True)


_CHUNK = 4096


def _common_prefix(a: str, b: str, limit: int) -> int:
    """The length of the longest common prefix of ``a`` and ``b``, at most ``limit``.

    Compares whole chunks first, so a keystroke in a long manuscript costs a
    few C-level slice comparisons instead of a Python loop over every scalar.
    """
    at = 0
    while at < limit:
        step = min(_CHUNK, limit - at)
        if a[at:at + step] == b[at:at + step]:
            at += step
            continue
        while at < limit and a[at] == b[at]:
            at += 1
        return at
    return at


def _common_suffix(a: str, b: str, limit: int) -> int:
    """The length of the longest common suffix of ``a`` and ``b``, at most ``limit``."""
    at = 0
    la, lb = len(a), len(b)
    while at < limit:
        step = min(_CHUNK, limit - at)
        if a[la - at - step:la - at] == b[lb - at - step:lb - at]:
            at += step
            continue
        while at < limit and a[la - 1 - at] == b[lb - 1 - at]:
            at += 1
        return at
    return at


def compute_minimal_splice(before: str, after: str) -> tuple[int, int, int] | None:
    """Minimal ``(start, end, replacement_len)`` scalar splice transforming ``before`` to ``after``.

    The splice rule of the contract: the longest common prefix first, then the
    longest common suffix of the rest. The Web counts the prefix in UTF-16
    units and backs off a split surrogate pair; on well-formed text that is
    exactly the scalar prefix and suffix taken here.
    """
    if before == after:
        return None
    prefix = _common_prefix(before, after, min(len(before), len(after)))
    suffix = _common_suffix(before, after, min(len(before), len(after)) - prefix)
    return prefix, len(before) - suffix, len(after) - suffix - prefix


def narrow_splice(before: str, after: str, start: int, end: int, replacement_len: int) -> tuple[int, int, int] | None:
    """The minimal splice inside a reported edit range.

    An editor can report a wider range than the text it changed (Qt may report
    a whole block). Trimming what the old and the new text share inside that
    range, prefix first, keeps the edit where the owner made it without letting
    unchanged text cross an alignment boundary. Returns None when nothing changed.
    """
    if (not (0 <= start <= end <= len(before)) or replacement_len < 0
            or len(after) - replacement_len != len(before) - (end - start)
            or before[:start] != after[:start] or before[end:] != after[start + replacement_len:]):
        # The report does not describe this change: judge the texts themselves.
        return compute_minimal_splice(before, after)
    removed = before[start:end]
    inserted = after[start:start + replacement_len]
    if removed == inserted:
        return None
    prefix = _common_prefix(removed, inserted, min(len(removed), len(inserted)))
    suffix = _common_suffix(removed, inserted, min(len(removed), len(inserted)) - prefix)
    return start + prefix, end - suffix, replacement_len - prefix - suffix


def _place_span(span: Mapping[str, Any], next_text: str, start: int, end: int,
                length: int) -> tuple[str, dict[str, Any] | None]:
    """Where one non-empty span lands after ``[start:end]`` became ``length`` scalars.

    ``kept`` when it ends at or before the splice (R1; an insertion at its end
    stays outside, R5), ``shifted`` when it starts at or after the splice end
    (R1, R5), ``resized`` when the splice lies inside it and leaves text (R2),
    ``emptied`` when its text is deleted (an inside edit that leaves nothing, or
    a pure deletion that covers it) and ``crossed`` otherwise (R3).
    """
    delta = length - (end - start)
    a, b = span["start"], span["end"]
    if b <= start:
        return "kept", _copy_span(span)
    if a >= end:
        return "shifted", _slice_span(next_text, a + delta, b + delta)
    if a <= start and end <= b:
        return ("resized", _slice_span(next_text, a, b + delta)) if b + delta > a else ("emptied", None)
    if start <= a and b <= end and length == 0:
        return "emptied", None
    return "crossed", None


def _over_limit(span: Mapping[str, Any] | None) -> bool:
    return span is not None and len(span["text"]) > MAX_SPAN_SCALARS


def _settle(alignments: list, kept: list[tuple[int, dict]], invalidated: list[dict],
            emptied: list[dict], next_source: str, next_target: str) -> AlignmentRemap:
    """R6 on the entries an edit kept: one whose span grew past the span limit
    leaves the list alone (``span_too_long``); a list that still fails
    validation falls back to clearing every alignment."""
    every = list(invalidated)
    survivors = []
    for index, entry in kept:
        if _over_limit(entry["source"]) or _over_limit(entry["target"]):
            every.append({"original_index": index, "reason": "span_too_long",
                          "entry": _copy_entry(alignments[index])})
        else:
            survivors.append((index, entry))
    every.sort(key=lambda item: item["original_index"])
    result = [entry for _index, entry in survivors]
    if not manuscript._alignments(result, next_source, next_target):
        return _fallback_all(tuple(every))
    live = {index for index, _entry in survivors}
    return AlignmentRemap(result, tuple(every), tuple(item for item in emptied if item["original_index"] in live))


def remap_splice(alignments: list | None, side: str, next_source: str, next_target: str,
                 start: int, end: int, length: int, *, fill: tuple[int, dict] | None = None,
                 ambiguous: tuple[int, ...] = ()) -> AlignmentRemap:
    """The edit-transaction rules for one scalar splice on ``side``: ``[start:end]``
    of the old side text replaced by ``length`` scalars, giving ``next_source``
    and ``next_target``.

    ``fill`` places the one untranslated entry a target fill matched (its
    index and its new target span); ``ambiguous`` lists the candidates of an
    ambiguous fill, which are invalidated.
    """
    if alignments is None:
        return _unchanged(None)
    if not alignments:
        return _unchanged([])
    if side not in ("source", "target"):
        return _fallback_all()
    next_text = next_source if side == "source" else next_target
    if (any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end, length))
            or start < 0 or end < start or length < 0 or start + length > len(next_text)):
        return _fallback_all()
    kept: list[tuple[int, dict]] = []
    invalidated: list[dict] = []
    emptied: list[dict] = []
    for index, entry in enumerate(alignments):
        if fill is not None and fill[0] == index:
            kept.append((index, {"source": _copy_span(entry["source"]), "target": fill[1]}))
            continue
        if index in ambiguous:
            invalidated.append({"original_index": index, "reason": "fill_ambiguous", "entry": _copy_entry(entry)})
            continue
        span = entry["source"] if side == "source" else entry["target"]
        if span is None:
            kept.append((index, _copy_entry(entry)))
            continue
        kind, placed = _place_span(span, next_text, start, end, length)
        if placed is None:
            if kind == "crossed":
                invalidated.append({"original_index": index, "reason": "crossed_boundary", "entry": _copy_entry(entry)})
            elif side == "source":
                invalidated.append({"original_index": index, "reason": "source_emptied", "entry": _copy_entry(entry)})
            else:
                emptied.append({"original_index": index, "entry": _copy_entry(entry)})
                kept.append((index, {"source": _copy_span(entry["source"]), "target": None}))
        elif side == "source":
            target = entry["target"]
            kept.append((index, {"source": placed, "target": None if target is None else _copy_span(target)}))
        else:
            kept.append((index, {"source": _copy_span(entry["source"]), "target": placed}))
    return _settle(alignments, kept, invalidated, emptied, next_source, next_target)


def remap_for_free_edit(alignments: list | None, side: str, old_source: str, old_target: str,
                        new_source: str, new_target: str) -> AlignmentRemap:
    """Alignments after a free edit on ``side``: the minimal scalar splice
    between the old and the new side text goes through the transaction rules."""
    if alignments is None:
        return _unchanged(None)
    if not alignments:
        return _unchanged([])
    if side not in ("source", "target"):
        return _fallback_all()
    splice = compute_minimal_splice(old_source if side == "source" else old_target,
                                    new_source if side == "source" else new_target)
    if splice is None:
        kept = [(index, _copy_entry(entry)) for index, entry in enumerate(alignments)]
        return _settle(alignments, kept, [], [], new_source, new_target)
    return remap_splice(alignments, side, new_source, new_target, *splice)


def remap_for_edit(alignments: list | None, side: str, old_source: str, old_target: str,
                   new_source: str, new_target: str,
                   edit: tuple[int, int, int] | None = None) -> AlignmentRemap:
    """Alignments after an edit of ``side`` the editor reported as ``edit``
    (``(start, old_end, new_length)``), narrowed to the text it changed; a free
    edit when there is no report."""
    if edit is None:
        return remap_for_free_edit(alignments, side, old_source, old_target, new_source, new_target)
    before = old_source if side == "source" else old_target
    after = new_source if side == "source" else new_target
    splice = narrow_splice(before, after, *edit)
    if splice is None:
        return remap_for_free_edit(alignments, side, old_source, old_target, new_source, new_target)
    if alignments is None:
        return _unchanged(None)
    return remap_splice(alignments, side, new_source, new_target, *splice)


def remap_for_target_fill(alignments: list | None, source: str, next_target: str,
                          pair_source_start: int, pair_source_end: int, splice_pos: int,
                          core_start: int, core_end: int, delta: int) -> AlignmentRemap:
    """Alignments after a target fill: ``delta`` scalars inserted at
    ``splice_pos``, whose core ``[core_start:core_end]`` is the translation of
    the one untranslated (``target: None``) entry inside the pair source region.

    Two or more such entries make the fill ambiguous: only they are
    invalidated (``fill_ambiguous``) and the others shift. With none, the fill
    is a plain insertion.
    """
    if alignments is None:
        return _unchanged(None)
    if not alignments:
        return _unchanged([])
    if (splice_pos < 0 or delta < 0 or core_start < splice_pos or core_end <= core_start
            or core_end > splice_pos + delta):
        return _fallback_all()
    matching = tuple(index for index, entry in enumerate(alignments)
                     if entry["target"] is None and entry["source"]["start"] >= pair_source_start
                     and entry["source"]["end"] <= pair_source_end)
    fill = (matching[0], _slice_span(next_target, core_start, core_end)) if len(matching) == 1 else None
    return remap_splice(alignments, "target", source, next_target, splice_pos, splice_pos, delta,
                        fill=fill, ambiguous=matching if len(matching) > 1 else ())


def build_target_fill_transaction(
    source: str,
    raw_target: str,
    alignments: list | None,
    plan: Sequence[tuple[int, int, str, int, int]],
    *,
    whitespace: frozenset[str] = frozenset(),
    separate_paragraphs: bool = False,
) -> tuple[str, list[tuple[str, int, int, int]], AlignmentRemap]:
    """Build one undoable batch target-fill transaction with reverse-order per-segment edits.

    Each item in ``plan`` is ``(start, end, text, source_start, source_end)``.
    Alignments are remapped forward in document order, while ``edits`` are
    emitted in reverse order so each offset stays in the coordinates of the
    preceding text and intermediate reviewed segments are never invalidated.
    """
    ordered = sorted(plan, key=lambda item: (item[0], item[1], item[3], item[4]))
    current = raw_target
    working_alignments = alignments
    shift = 0
    invalidated: list[dict[str, Any]] = []
    emptied: list[dict[str, Any]] = []
    applied: list[tuple[int, int, int]] = []
    for start, end, text, source_start, source_end in ordered:
        position = start + shift
        if separate_paragraphs:
            left = "\n\n" if position > 0 and current[position - 1] not in whitespace else ""
            right = "\n\n" if position < len(current) and current[position] not in whitespace else ""
        else:
            left = right = ""
        inserted = left + text + right
        filled = current[:position] + inserted + current[end + shift:]
        fill_start = position + len(left)
        remap = remap_for_target_fill(
            working_alignments,
            source,
            filled,
            source_start,
            source_end,
            position,
            fill_start,
            fill_start + len(text),
            len(inserted),
        )
        working_alignments = remap.alignments
        invalidated.extend(remap.invalidated)
        emptied.extend(remap.emptied_targets)
        current = filled
        shift += len(inserted) - (end - start)
        applied.append((start, end, len(inserted)))
    edits = [("target", start, end, length) for start, end, length in reversed(applied)]
    return current, edits, AlignmentRemap(working_alignments, tuple(invalidated), tuple(emptied))


def apply_edit_transaction(draft: Mapping[str, Any], tx: Mapping[str, Any]) -> tuple[dict[str, Any], AlignmentRemap]:
    """One edit transaction on a draft (``source``, ``target``, ``direction``,
    ``alignments``): the next draft and what the transaction kept, invalidated
    and emptied. A direction change still clears every explicit alignment (R7).

    ``tx`` is one of the contract's kinds: ``{"kind": "edit", "side", "text"}``,
    ``{"kind": "splice", "side", "start", "end", "replacement"}``,
    ``{"kind": "fill", "pair_source", "at", "insert", "core"}`` or
    ``{"kind": "direction", "direction"}``.
    """
    kind = tx["kind"]
    if kind == "direction":
        remap = _unchanged(None if draft["alignments"] is None else [])
        return {**draft, "direction": tx["direction"], "alignments": remap.alignments}, remap
    if kind in ("edit", "splice"):
        side = tx["side"]
        if side not in ("source", "target"):
            raise ValueError("edit transaction side must be source or target")
        if kind == "edit":
            text = tx["text"]
        else:
            old = draft[side]
            if not 0 <= tx["start"] <= tx["end"] <= len(old):
                raise ValueError("edit transaction splice is outside the text")
            text = old[:tx["start"]] + tx["replacement"] + old[tx["end"]:]
        next_source = text if side == "source" else draft["source"]
        next_target = text if side == "target" else draft["target"]
        if kind == "edit":
            remap = remap_for_free_edit(draft["alignments"], side, draft["source"], draft["target"],
                                        next_source, next_target)
        else:
            remap = remap_splice(draft["alignments"], side, next_source, next_target,
                                 tx["start"], tx["end"], len(tx["replacement"]))
        return {**draft, "source": next_source, "target": next_target, "alignments": remap.alignments}, remap
    if kind == "fill":
        old = draft["target"]
        at = tx["at"]
        if not 0 <= at <= len(old):
            raise ValueError("edit transaction fill is outside the text")
        next_target = old[:at] + tx["insert"] + old[at:]
        remap = remap_for_target_fill(draft["alignments"], draft["source"], next_target,
                                      tx["pair_source"]["start"], tx["pair_source"]["end"], at,
                                      tx["core"]["start"], tx["core"]["end"], len(tx["insert"]))
        return {**draft, "target": next_target, "alignments": remap.alignments}, remap
    raise ValueError("unknown edit transaction kind")


def append_invalidations(current: list[dict] | tuple, added: tuple[dict, ...] | list[dict]) -> list[dict]:
    """The session list of invalidated alignments with ``added`` appended,
    keeping the latest ``MAX_SESSION_INVALIDATIONS``."""
    return ([*current, *added][-MAX_SESSION_INVALIDATIONS:]) if added else list(current)


def valid_invalidation(item: Any) -> bool:
    """Whether ``item`` is one entry of the session list as it is kept and
    written to the recovery draft."""
    if not isinstance(item, Mapping) or set(item) != {"original_index", "reason", "entry"}:
        return False
    index, entry = item["original_index"], item["entry"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 64:
        return False
    if item["reason"] not in INVALIDATION_REASONS or not isinstance(entry, Mapping) or set(entry) != {"source", "target"}:
        return False

    def span(value: Any) -> bool:
        return (isinstance(value, Mapping) and set(value) == {"start", "end", "text"}
                and all(isinstance(value[key], int) and not isinstance(value[key], bool) for key in ("start", "end"))
                and 0 <= value["start"] < value["end"] and isinstance(value["text"], str))

    return span(entry["source"]) and (entry["target"] is None or span(entry["target"]))


# Compatibility names: the kept alignments only.

def remap_alignments_for_side_edit(alignments: list[dict[str, Any]] | None, side: str, old_source: str,
                                   old_target: str, new_source: str, new_target: str,
                                   edit: tuple[int, int, int] | None = None) -> list[dict[str, Any]] | None:
    """The alignments ``remap_for_edit`` keeps."""
    return remap_for_edit(alignments, side, old_source, old_target, new_source, new_target, edit).alignments


def remap_alignments_for_target_splice(alignments: list[dict[str, Any]] | None, source: str, next_target: str,
                                       start: int, end: int, replacement_len: int) -> list[dict[str, Any]] | None:
    """The alignments ``remap_splice`` keeps after a target splice."""
    return remap_splice(alignments, "target", source, next_target, start, end, replacement_len).alignments
