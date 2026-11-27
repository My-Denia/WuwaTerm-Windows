"""Pure-Python manuscript edit store: payload, revision, history, and transactions.

Owns the editable manuscript draft, undo/redo history, typing coalescing,
input-method composition grouping, alignment invalidations, segment record
normalization, term choices, and document-mode choices. Holds no Qt and no HTTP
dependencies.
"""
from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .. import markdown_report as mdr
from .. import segment_records as sr
from .. import semantic_session as sessions
from ..document_edits import (
    PART_KEYS,
    REASON_FAILED,
    REASON_FULL,
    REASON_NOT_APPLICABLE,
    REVIEW_KEYS,
    TYPING_COALESCE_MS,
    AlignmentRemap,
    CommandOutcome,
    EditHistory,
    EditOutcome,
    HistoryEntry,
    HistoryOutcome,
    append_invalidations,
    compute_minimal_splice,
    draft_of,
    merge_draft,
    narrow_splice,
    remap_for_edit,
    remap_for_free_edit,
    restore_draft_into,
    valid_invalidation,
)
from ..manuscript import (
    MAX_V4_CHOICES,
    WorkfileError,
    empty_workfile,
    recover_span,
    sanitize_duplicate_choices,
)
from ..segmentation import SEGMENTATION_VERSION
from .alignment_state import AlignmentState


class DocumentEditStore:
    """Transactional editing, undo/redo history, and choice mutations for a manuscript."""

    def __init__(
        self,
        alignment: AlignmentState,
        session: sessions.Session,
        *,
        editor_state_provider: Callable[[], dict[str, Any]],
        on_direction_changed: Callable[[], bool],
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._alignment = alignment
        self._session = session
        self._editor_state_provider = editor_state_provider
        self._on_direction_changed = on_direction_changed
        self.clock: Callable[[], float] = clock or (lambda: time.monotonic() * 1000.0)

        self.payload: dict[str, Any] = empty_workfile()
        self.history = EditHistory()
        self.document_revision = 0
        self.alignment_invalidations: list[dict[str, Any]] = []
        self._typing: tuple[str, float, int] | None = None
        self._composition: tuple[str, dict[str, Any], list[dict[str, Any]]] | None = None
        self.decision_cache: sessions.DecisionIndex | None = None
        self.records_seen: tuple[Any, ...] | None = None
        self.session_choices: list[dict[str, Any]] = []
        self.trusted_snapshots: list[dict[str, Any]] = []

    # -- snapshots & normalization ------------------------------------------

    def decisions(self) -> list[dict[str, Any]]:
        return self.payload.get("semantic_decisions", [])

    def decision_index(self) -> sessions.DecisionIndex:
        decisions = self.decisions()
        if self.decision_cache is None or self.decision_cache.decisions is not decisions:
            self.decision_cache = sessions.DecisionIndex(decisions)
        return self.decision_cache

    def draft_snapshot(self) -> dict[str, Any]:
        return draft_of(self.payload)

    def restore_draft(self, value: dict[str, Any]) -> None:
        restore_draft_into(self.payload, value)

    def put_parts(self, parts: sr.Parts) -> None:
        """Hold ``parts`` in the payload, in place; not an undo step of its own."""
        value = sr.with_parts(self.payload, parts)
        for key in PART_KEYS:
            if key in value:
                self.payload[key] = value[key]
            else:
                self.payload.pop(key, None)

    def records(self, term_basis: dict[str, Any] | None) -> list[dict[str, Any]] | None:
        """Normalize and return long-mode segment records, or None in document mode."""
        payload = self.payload
        long = self._alignment.long_now(payload)
        if not long and not payload.get("segments"):
            return None
        pairs = self._alignment.current_pairs(payload) if long else []

        def now() -> tuple[Any, ...]:
            return (
                payload["source"],
                payload["target"],
                payload["alignments"],
                term_basis,
                pairs,
                *(payload.get(key) for key in PART_KEYS),
            )

        seen = self.records_seen
        if seen is None or seen[-1] != payload["direction"] or any(a is not b for a, b in zip(seen, now())):
            self.put_parts(
                sr.normalize(
                    payload,
                    pairs,
                    term_basis,
                    check_limits=False,
                    version=self._alignment.pairs_version if long else SEGMENTATION_VERSION,
                )
            )
            self.records_seen = (*now(), payload["direction"])
        return payload.get("segments") or [] if long else None

    def reset_session_tracking(self) -> None:
        self.records_seen = None
        self.session_choices = []
        self.trusted_snapshots = []

    # -- text edits & undo/redo transactions --------------------------------

    def _record_step(self, before: dict[str, Any], view_state: dict[str, Any] | None) -> int:
        return self.history.record(
            HistoryEntry(
                before,
                self._session.snapshot(),
                self._editor_state_provider() if view_state is None else view_state,
                tuple(self.alignment_invalidations),
            )
        )

    def _apply_text_edits(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        edits: list[tuple[Any, ...]] | None,
        exempt: sessions.LiveIssue | None,
    ) -> None:
        if edits is None:
            edits = [
                (side, 0, len(before[side]), len(after[side]))
                for side in ("source", "target")
                if before[side] != after[side]
            ]
        parts = sr.parts_of(self.payload) if "segments" in self.payload else None
        for side, start, old_end, new_length in edits:
            self._session.apply_edit(side, start, old_end, new_length, exempt=exempt)
            if parts is not None:
                parts = sr.apply_edit(parts, before[side], side, start, old_end, new_length)
        if parts is not None:
            self.put_parts(parts)

    def _commit_edit(
        self,
        before: dict[str, Any],
        merged: dict[str, Any],
        edits: list[tuple[Any, ...]] | None,
        exempt: sessions.LiveIssue | None,
        remap: AlignmentRemap | None,
        reset: bool,
        base_invalidations: list[dict[str, Any]],
    ) -> EditOutcome:
        self.restore_draft(merged)
        self.document_revision += 1
        cleared = False
        if before["direction"] != merged["direction"]:
            cleared = bool(self._on_direction_changed())
        elif edits is not None or before["source"] != merged["source"] or before["target"] != merged["target"]:
            self._apply_text_edits(before, merged, edits, exempt)
        if reset:
            self.alignment_invalidations = []
        else:
            self.alignment_invalidations = append_invalidations(
                base_invalidations,
                remap.invalidated if remap is not None else (),
            )
        kept = len(merged["alignments"] or ())
        if remap is None:
            return EditOutcome(True, cleared, any(before[key] != merged[key] for key in REVIEW_KEYS), kept=kept)
        return EditOutcome(
            True,
            cleared,
            any(before[key] != merged[key] for key in REVIEW_KEYS),
            alignment_notice=remap.notice(before["alignments"]),
            invalidated=len(remap.invalidated),
            emptied_targets=len(remap.emptied_targets),
            kept=kept,
        )

    def apply_edit(
        self,
        value: dict[str, Any],
        *,
        view_state: dict[str, Any] | None = None,
        edits: list[tuple[Any, ...]] | None = None,
        exempt: sessions.LiveIssue | None = None,
        remap: AlignmentRemap | None = None,
        alignment_reset: bool = False,
    ) -> EditOutcome:
        before = self.draft_snapshot()
        merged = merge_draft(before, value)
        if merged == before:
            return EditOutcome()
        self._typing = None
        self._composition = None
        self._record_step(before, view_state)
        return self._commit_edit(
            before,
            merged,
            edits,
            exempt,
            remap,
            alignment_reset or before["direction"] != merged["direction"],
            self.alignment_invalidations,
        )

    def edit_text(
        self,
        side: str,
        text: str,
        *,
        edit: tuple[int, int, int] | None = None,
        composing: bool = False,
        view_state: dict[str, Any] | None = None,
        exempt: sessions.LiveIssue | None = None,
    ) -> EditOutcome:
        if side not in ("source", "target"):
            return EditOutcome()
        if self._composition is not None and self._composition[0] != side:
            self._composition = None
        open_ = self._composition
        current = self.payload[side]
        if open_ is not None and not composing and text == current:
            self._composition = None
            self._typing = None
            return EditOutcome()
        if open_ is None and text == current:
            return EditOutcome()
        before = self.draft_snapshot()
        base = open_[1] if open_ is not None else before
        base_invalidations = open_[2] if open_ is not None else self.alignment_invalidations
        next_source = text if side == "source" else before["source"]
        next_target = text if side == "target" else before["target"]
        if open_ is not None:
            remap = remap_for_free_edit(
                base["alignments"], side, base["source"], base["target"], next_source, next_target
            )
        else:
            remap = remap_for_edit(
                base["alignments"], side, base["source"], base["target"], next_source, next_target, edit
            )
        value = dict(before)
        value[side] = text
        value["alignments"] = remap.alignments
        splice = narrow_splice(current, text, *edit) if edit is not None else compute_minimal_splice(current, text)
        if splice is None:
            splice = compute_minimal_splice(current, text) or (0, 0, 0)
        start, old_end, new_length = splice
        delta = max(old_end - start, new_length)
        line_break = "\n" in current[start:old_end] or "\n" in text[start:start + new_length]
        now = self.clock()
        typing = self._typing
        can_join = (
            open_ is None
            and not composing
            and not remap.touched
            and not line_break
            and typing is not None
            and typing[0] == side
            and now - typing[1] <= TYPING_COALESCE_MS
            and delta <= 1
            and self.history.top_serial() == typing[2]
        )
        if open_ is None and not can_join:
            serial = self._record_step(before, view_state)
        else:
            serial = typing[2] if typing is not None else 0
            self.history.drop_redo()
        self._typing = (
            (side, now, serial)
            if not composing and open_ is None and not remap.touched and not line_break and delta <= 1
            else None
        )
        if composing:
            self._composition = open_ if open_ is not None else (side, before, list(base_invalidations))
        else:
            self._composition = None
        edits = [(side, *edit)] if edit is not None else [(side, start, old_end, new_length)]
        merged = merge_draft(before, value)
        return self._commit_edit(before, merged, edits, exempt, remap, False, base_invalidations)

    def set_direction(self, direction: str, *, view_state: dict[str, Any] | None = None) -> EditOutcome:
        value = self.draft_snapshot()
        value["direction"] = direction
        if value["alignments"] is not None:
            value["alignments"] = []
        return self.apply_edit(value, view_state=view_state)

    def note_edit(self, edit: tuple[Any, ...]) -> None:
        self._session.apply_edit(*edit)

    def apply_parts_edit(self, parts: sr.Parts, *, view_state: dict[str, Any] | None = None) -> CommandOutcome:
        return CommandOutcome(True, edit=self.apply_edit(sr.with_parts(self.draft_snapshot(), parts), view_state=view_state))

    def can_undo(self) -> bool:
        return self.history.can_undo()

    def can_redo(self) -> bool:
        return self.history.can_redo()

    def clear_history(self) -> None:
        self.history.clear()
        self.alignment_invalidations = []
        self._typing = None
        self._composition = None

    def restore_invalidations(self, items: Sequence[Mapping[str, Any]]) -> None:
        self.alignment_invalidations = [
            copy.deepcopy(dict(item)) for item in items if valid_invalidation(item)
        ][-64:]

    def step_history(self, *, undo: bool, view_state: dict[str, Any] | None = None) -> HistoryOutcome | None:
        before = self.draft_snapshot()
        entry = self.history.step(
            undo=undo,
            current=HistoryEntry(
                before,
                self._session.snapshot(),
                self._editor_state_provider() if view_state is None else view_state,
                tuple(self.alignment_invalidations),
            ),
        )
        if entry is None:
            return None
        self._typing = None
        self._composition = None
        later = sr.parts_of(self.payload)
        self.restore_draft(entry.value)
        self.alignment_invalidations = [copy.deepcopy(item) for item in entry.invalidations]
        if "segments" in self.payload:
            self.put_parts(sr.carry_results(sr.parts_of(self.payload), later))
        self._session.restore(entry.session)
        cleared = False
        if before["direction"] != entry.value["direction"]:
            cleared = bool(self._on_direction_changed())
        return HistoryOutcome(
            entry,
            EditOutcome(True, cleared, any(before[key] != entry.value[key] for key in REVIEW_KEYS)),
        )

    # -- term choices & document choices ------------------------------------

    def reconfirmable(self, index: int, choice: dict[str, Any], term_basis: dict[str, Any] | None) -> bool:
        return sr.choice_reconfirmable(self.payload["segments"][index], choice, term_basis)

    def choose_term(
        self,
        index: int,
        start: int,
        end: int,
        choice: str,
        candidate_id: str | None = None,
    ) -> CommandOutcome:
        try:
            parts = sr.choose(self.payload, index, start, end, choice, candidate_id)
        except (ValueError, WorkfileError):
            return CommandOutcome(reason=REASON_FAILED)
        return self.apply_parts_edit(parts)

    def unchoose_term(self, index: int, start: int, end: int) -> CommandOutcome:
        return self.apply_parts_edit(sr.unchoose(self.payload, index, start, end))

    def restore_choice(self, position: int, index: int) -> CommandOutcome:
        try:
            parts = sr.restore(self.payload, position, index)
        except (ValueError, WorkfileError):
            return CommandOutcome(reason=REASON_FAILED)
        return self.apply_parts_edit(parts)

    def confirm_choice(
        self,
        index: int,
        choice: dict[str, Any],
        term_basis: dict[str, Any] | None,
    ) -> CommandOutcome:
        if not self.reconfirmable(index, choice, term_basis):
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        span = choice["source_span"]
        candidate = choice["candidate"]["candidate_id"] if choice["choice"] == sr.OFFICIAL_PAIR else None
        return self.choose_term(index, span["start"], span["end"], choice["choice"], candidate)

    def set_finished(self, index: int, finished: bool) -> CommandOutcome:
        try:
            parts = sr.set_finished(sr.parts_of(self.payload), index, finished)
        except ValueError:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        return self.apply_parts_edit(parts)

    def remove_history_choice(self, position: int) -> CommandOutcome:
        parts = sr.parts_of(self.payload)
        history = [entry for number, entry in enumerate(parts.history) if number != position]
        return self.apply_parts_edit(parts._replace(history=history))

    def latest_trusted(self) -> dict[str, Any] | None:
        history = self.payload["history"]
        latest = history[-1] if history else None
        if (
            latest is None
            or latest not in self.trusted_snapshots
            or latest["source"] != self.payload["source"]
            or latest["direction"] != self.payload["direction"]
        ):
            return None
        return latest

    def document_choice_at(self, finding: Mapping[str, Any]) -> dict[str, Any] | None:
        span = finding["source_span"]
        for choice in self.payload["choices"]:
            recovered = recover_span(choice["source"], choice["source_span"], self.payload["source"])
            if recovered is not None and sr.mention_id(recovered) == sr.mention_id(span):
                return choice
        return None

    def is_session_choice(self, choice: Mapping[str, Any] | None) -> bool:
        return choice is not None and choice in self.session_choices

    def choose_document(
        self,
        finding: Mapping[str, Any],
        candidate: Mapping[str, Any] | None,
        *,
        view_state: dict[str, Any] | None = None,
    ) -> CommandOutcome:
        latest = self.latest_trusted()
        if latest is None or latest["report"]["truncated"]:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        span = recover_span(latest["source"], finding["source_span"], self.payload["source"])
        if span is None or (
            candidate is not None
            and not any(c["candidate_id"] == candidate["candidate_id"] for c in finding["candidates"])
        ):
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        report = latest["report"]
        made = {
            "source": self.payload["source"],
            "direction": self.payload["direction"],
            "source_span": dict(span),
            "scope": mdr.scope_of(self.payload["alignments"], span),
            "choice": sr.OFFICIAL_PAIR if candidate is not None else sr.NOT_A_TERM,
            "candidate": None
            if candidate is None
            else {key: candidate[key] for key in ("candidate_id", "zh", "en", "category")},
            "basis": mdr.basis_of(report),
        }
        value = self.draft_snapshot()
        existing = self.document_choice_at({"source_span": span})
        choices = [c for c in value["choices"] if c != existing]
        if existing is None and len(choices) >= MAX_V4_CHOICES:
            return CommandOutcome(reason=REASON_FULL)
        value["choices"] = [*choices, made]
        self.session_choices = [c for c in self.session_choices if c != existing] + [copy.deepcopy(made)]
        return CommandOutcome(True, edit=self.apply_edit(value, view_state=view_state))

    def document_resolutions(self) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        latest = self.latest_trusted()
        own = [c for c in self.payload["choices"] if self.is_session_choice(c)]
        if latest is None or not own:
            return [], None
        report = latest["report"]
        reconciled = mdr.reconcile_choices(
            own,
            self.payload,
            {**latest, "trusted": True},
            imported=False,
            submits=True,
        )
        resolutions: list[dict[str, Any]] = []
        for choice, item in zip(own, reconciled, strict=True):
            if item["status"] != "applicable":
                continue
            resolution: dict[str, Any] = {"mention_id": sr.mention_id(item["span"]), "choice": choice["choice"]}
            if choice["choice"] == sr.OFFICIAL_PAIR:
                resolution["candidate_id"] = choice["candidate"]["candidate_id"]
            resolutions.append(resolution)
        if not resolutions or "matcher_revision" not in report or "revision" not in report["dictionary"]:
            return [], None
        return resolutions, {
            "source_revision": report["source_revision"],
            "rule_version": report["rule_version"],
            "dictionary_revision": report["dictionary"]["revision"],
            "matcher_revision": report["matcher_revision"],
        }

    def remove_choice(self, index: int) -> CommandOutcome:
        value = self.draft_snapshot()
        value["choices"].pop(index)
        return CommandOutcome(True, edit=self.apply_edit(value))

    def resolve_conflicting_choices_for_save(self) -> CommandOutcome:
        cleaned, dropped = sanitize_duplicate_choices(self.payload)
        if dropped <= 0:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        return CommandOutcome(True, edit=self.apply_edit(draft_of(cleaned)))
