"""Execution controller of the manuscript workbench: requests, runs, commits.

The workbench has two halves. ``ui/manuscript_view.py`` is the Qt half: editors,
lists, buttons, undo history and every rendering decision. This module is the
other half, and holds no Qt and no HTTP of its own: it is handed the API client
and decides *what* to ask for, in what order, whether an answer still belongs to
the run that asked, and how a finished answer is committed to the manuscript.

Three runs live here, each with its own generation counter, so that an answer
from a superseded run is dropped instead of shown:

* the whole-manuscript term review (``start_review``/``run_review``): one
  ``review_pair`` for a short manuscript, then the segmented run for the
  semantic review;
* the serial segmented run (``start_semantic_run``/``run_segments``): one
  request in flight, term checks following the segment-record reuse rule
  (``segment_records``) and semantic requests following the semantic reuse rule
  (``semantic_session.Basis``), with the failure streak that distinguishes a
  bad segment from a bad service;
* one translation run (``start_translation``/``run_translation``): piece by
  piece, resumable from the first unfinished piece, writing what it finished as
  one undoable edit.

The document's own commands live here too, because they commit to the same
state the runs commit to and through the same checks: ``apply_edit`` and the
undo/redo pair over it, the term-choice commands (``choose_term``,
``unchoose_term``, ``restore_choice``, ``confirm_choice``, ``set_finished``),
the conflict removals, and the semantic decisions (``accept_issue``,
``adjust_issue``, ``ignore_issue``, ``record_decision``) with the questions
they ask of the document (``issue_state``, ``basis_holds``, ``can_accept``,
``can_adjust``, ``can_ignore``). They are pure domain operations: the Qt half
only says which one the owner asked for, hands in the two things this module
cannot do itself (the editor's view state and whether the editor can display
a text), and renders the outcome.

Committing also lives here, because a commit is what must be atomic with the
generation check: ``store_semantic``, ``remember_semantic_report`` and
``translation_stopped`` write the payload and the session, never a widget.

The Qt half implements :class:`WorkbenchHost`: it is told to refresh, to show a
report or to write the target side, and it owns the labels, the lists and the
editor. Everything this module holds is document state, so the view can read it
for rendering (that is what its ``_payload``/``_session``/``_term_basis``
properties do), but only this module writes it during a run.

Pure Python: no Qt, no HTTP.
"""
from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from . import segment_records as sr, semantic, strings
from . import semantic_session as sessions
from .document_edits import (
    TYPING_COALESCE_MS, AlignmentRemap, HistoryEntry, HistoryOutcome, CommandOutcome, EditHistory, EditOutcome,
    PART_KEYS, REASON_FAILED, REASON_FULL, REASON_NOT_APPLICABLE, REASON_RECONFIRM, REASON_REJECTED,
    REASON_STALE, REVIEW_KEYS, append_invalidations, compute_minimal_splice, copy_payload, draft_of,
    merge_draft, narrow_splice, normalize_semantic, remap_for_edit, remap_for_free_edit, remap_for_target_fill,
    remap_splice, restore_draft_into, valid_invalidation,
)
from .errors import (
    ERROR_CANCELLED, ERROR_FORBIDDEN, ERROR_INSECURE_ENDPOINT, ERROR_INVALID_REQUEST,
    ERROR_LLM_BUDGET_EXHAUSTED, ERROR_LLM_UNAVAILABLE, ERROR_NOT_CONFIGURED, ERROR_OFFLINE,
    ERROR_RATE_LIMITED, ERROR_UNAUTHORIZED, ClientError,
)
from .manuscript import (
    MAX_HISTORY_SEGMENTS, MAX_SEMANTIC_DECISIONS, MAX_SEMANTIC_HISTORY, ReviewReport, WorkfileError,
    empty_workfile, sanitize_duplicate_choices, semantic_payload_fits, serialize_workfile,
)
from .segmentation import (
    SEGMENT_LIMIT, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY, STATUS_UNTRANSLATED, WHITESPACE,
    _core as seg_core, _paragraph_ranges as seg_paragraph_ranges, pair_is_sendable, pair_segments, segment_text,
)

# The whole-manuscript term review keeps its historical request shape; above
# this length each side is reviewed per segmentation-v1 pair instead.
DOCUMENT_REVIEW_LIMIT = 2000
# A pause, not a failure: the run keeps its results and can continue later.
QUOTA_CODES = frozenset({ERROR_RATE_LIMITED, ERROR_LLM_BUDGET_EXHAUSTED})
# Nothing else can succeed until the owner fixes these, so the run stops.
FATAL_CODES = frozenset({ERROR_UNAUTHORIZED, ERROR_FORBIDDEN, ERROR_NOT_CONFIGURED,
                         ERROR_INSECURE_ENDPOINT, ERROR_OFFLINE, ERROR_LLM_UNAVAILABLE})
# The same failure on consecutive segments is the service, not the segment.
REPEATED_FAILURES = 2
# A stopped translation run, by the state it left: both offer a next press, and
# both are dropped once the text is no longer what the run left.
TRANSLATION_RESUMABLE = ("paused", "failed")
# The translation run's phases: idle, and the four the view renders apart.
TRANSLATION_IDLE = "idle"

# The draft primitives (``copy_payload``, ``normalize_semantic``, ``PART_KEYS``)
# live in ``document_edits``: the runs and the commands commit through them,
# and they are imported here so this module's surface does not move with them.


def blank(text: str) -> bool:
    return all(char in WHITESPACE for char in text)


@dataclass
class Translation:
    """One translation run and, once it stops, the cursor to resume it.

    ``target`` is what the next finished pieces are written onto: the blank
    target the run started from (``written`` False: they replace it) or the
    target as the partial write left it (``written`` True: they are appended).
    ``segment`` is the source start of a single untranslated segment, or None
    for the whole manuscript. ``out`` holds the pieces finished since the last
    write, each with its separators exactly as a full run writes them.
    """
    generation: int
    source: str
    direction: str
    target: str
    segment: int | None = None
    written: bool = False
    retry: bool = False
    next_piece: int = 0
    done: int = 0
    count: int = 0
    previous: tuple[str, str] | None = None
    out: list[str] = field(default_factory=list)
    append_start: int | None = None
    append_prefix: str = ""
    source_base: int = 0
    retranslate: bool = False
    batch_remaining: list[tuple[int, int]] = field(default_factory=list)
    batch_total: int = 0
    batch_done: int = 0


class TranslationResponse(Protocol):
    """One finished translation, as far as a run reads it.

    ``api.TranslationResult`` satisfies it. Naming only the field a run uses is
    what keeps this module (and its tests) importable with no HTTP library: the
    real result is built, and validated, by the client.
    """

    text: str


@runtime_checkable
class WorkbenchApiClient(Protocol):
    """The client calls this workbench makes, and nothing else.

    ``api.ApiClient`` implements it; the narrow surface is declared here so the
    runs can be driven by a stand-in with no HTTP. These annotations describe
    what a *validated* answer looks like - they are not checks and replace
    none: ``api`` refuses anything that is not this service's answer for the
    text it sent, and that runtime validation stays where it is.
    """

    async def get_capabilities(self) -> frozenset[str]:
        """The optional capabilities the server lists (empty when it lists none)."""

    async def review_pair(
        self, source: str, target: str, direction: str, *,
        alignments: list[dict[str, Any]] | None = None,
        resolutions: list[dict[str, Any]] | None = None,
        resolution_context: dict[str, str] | None = None,
    ) -> ReviewReport:
        """One term check (``review-v2``): the report for exactly these sides."""

    async def semantic_review(
        self, source: str, target: str, direction: str, *,
        before: dict[str, str] | None = None,
        precedents: Sequence[Mapping[str, Any]] = (),
    ) -> semantic.SemanticReviewReport:
        """One semantic review (``semantic-review-v1``) of exactly these sides."""

    async def translate(
        self, text: str, *, to: str | None = None, context: dict[str, Any] | None = None,
    ) -> TranslationResponse:
        """One piece of text, translated."""


@runtime_checkable
class WorkbenchHost(Protocol):
    """What the Qt half must provide for a run: rendering and the editor write.

    None of these decides anything about a run; they are called after this
    module has already committed the state, so the view can only show it.
    """

    def refresh_semantic(self) -> None:
        """Keep the issue rows in step with the session."""

    def refresh_segments(self) -> None:
        """Keep the segment list in step with the pairing and the records."""

    def refresh_segments_if_shown(self) -> None:
        """``refresh_segments`` only while the segment track is the one shown."""

    def update_controls(self) -> None:
        """Re-enable, re-label and re-render everything that depends on a run."""

    def render_semantic_progress(self) -> None:
        """Show the run's progress card."""

    def show_segment_term_report(self) -> None:
        """List a long manuscript's term results from its segment records."""

    def show_term_report(self, report: ReviewReport) -> None:
        """List the findings of a committed document review."""

    def editor_state(self) -> dict:
        """The editor's own view state right now (caret, focus, scroll).

        Opaque to this module: it is stored with an undo step and handed back
        when that step is restored, and nothing here reads it.
        """

    def render_review_state(self) -> None:
        """Show the review phase, its detail and the request id it came from."""

    def clear_report(self) -> None:
        """Drop the listed report and its findings before a new run."""

    def report_status(self, text: str) -> None:
        """Put one sentence in the workbench status line."""

    def report_unstorable_report(self, code: str) -> None:
        """A report that cannot be saved: show the refusal (and its list)."""

    def report_translation_complete(self, mismatched: int) -> None:
        """A finished translation: offer the manual splits it left, if any."""

    def trust_review_snapshot(self, snapshot: dict) -> None:
        """Remember a report this session checked itself (the Web's "trusted")."""

    def precedents_selected(self) -> bool:
        """Whether the owner keeps precedents on for this run."""

    def selected_segment(self):
        """The segment the owner has selected, as a pair, or None."""

    def apply_target_write(
        self,
        changed: str,
        edit: tuple,
        *,
        remap: AlignmentRemap | None = None,
    ) -> bool:
        """Write finished translation pieces as one undoable edit.

        ``remap`` is what the write does to the explicit alignments (kept,
        resized, filled, or the entries it invalidated). False when the editor
        cannot display ``changed``; nothing is committed in that case (the
        caller has already refused the write).
        """


class DocumentRunController:
    """The workbench's runs, its document commands and the state both commit to."""

    def __init__(self, api_client: WorkbenchApiClient, host: WorkbenchHost,
                 clock: Callable[[], float] | None = None) -> None:
        self.api_client = api_client
        self.host = host
        # The manuscript. Everything that commits writes it; the view reads it.
        self.payload: dict[str, Any] = empty_workfile()
        # The undo and redo steps of edits, term choices and decisions.
        self.history = EditHistory()
        # Milliseconds, for joining a typing run into one undo step.
        self.clock: Callable[[], float] = clock or (lambda: time.monotonic() * 1000.0)
        # Alignments edit transactions took out of the list this session, each
        # ``{"original_index", "reason", "entry"}``. Session state only: undo
        # steps and the recovery draft carry it, a saved manuscript never does.
        self.alignment_invalidations: list[dict[str, Any]] = []
        # The typing run the newest undo step belongs to: (side, at, serial).
        self._typing: tuple[str, float, int] | None = None
        # An input-method composition in progress: its side, the draft before
        # it and the session list before it. Alignments are judged against that
        # draft, and the whole composition is one undo step.
        self._composition: tuple[str, dict[str, Any], list[dict[str, Any]]] | None = None
        # Decisions by occurrence, rebuilt only when the decisions list is
        # replaced (every change assigns a new list: edits, undo/redo, loading).
        self.decision_cache: sessions.DecisionIndex | None = None
        # Term review of a short manuscript, one request.
        self.generation = 0
        self.task: asyncio.Task | None = None
        self.review_state = "none"
        self.review_detail = ""
        self.request_id: str | None = None
        self.capabilities: frozenset[str] | None = None
        self.term_mode = "document"
        # Semantic review (semantic-review-v1) and the segmented run.
        self.session = sessions.Session()
        self.semantic_generation = 0
        self.semantic_task: asyncio.Task | None = None
        self.semantic_state = "none"
        self.semantic_detail = ""
        self.segment_failures: dict[tuple[str, str], str] = {}
        self.segment_notes: dict[tuple[str, str], list[str]] = {}
        # Old whole texts by sha256, so an ignored decision can still recover
        # its spans after the owner edits elsewhere (Web ``semantic_texts``).
        self.known_texts: dict[str, str] = {}
        self.history_run = 0
        self.history_written = -1
        # Long manuscripts (segment_records). The term basis of this session:
        # None until a review-v2 response supplies it, so nothing saved counts
        # as current before the first check. Never saved.
        self.term_basis: dict | None = None
        # What the records were last normalized against (identity checks only).
        self.records_seen: tuple | None = None
        self.record_views = sr.RecordViews()
        # The pairing of the current text, cached by the text it was made from.
        self.pairs_key: tuple | None = None
        self.pairs: list = []
        # Counts every payload change the document's title and dirty flag see.
        self.document_revision = 0
        # Translation of the target side.
        self.translate_generation = 0
        self.translate_task: asyncio.Task | None = None
        # idle | waiting | retrying | paused | failed. In paused and failed the
        # run record is the resume cursor; it is kept only while the source,
        # direction and target are exactly what the run left.
        self.translate_state = TRANSLATION_IDLE
        self.translate_run: Translation | None = None
        self.translate_status = ""
        self.translation_withdrawn = False

    # -- document state the view reads while rendering -----------------------
    def decisions(self) -> list[dict]:
        return self.payload.get("semantic_decisions", [])

    def pairs_key_now(self) -> tuple:
        return (self.payload["source"], self.payload["target"], repr(self.payload["alignments"] or None))

    def current_pairs(self) -> list:
        source, target = self.payload["source"], self.payload["target"]
        alignments = self.payload["alignments"] or None
        key = self.pairs_key_now()
        if key != self.pairs_key:
            try:
                pairs = pair_segments(source, target, SEGMENT_LIMIT, alignments)
            except (ValueError, TypeError, AssertionError):
                # Alignments the rule cannot use fall back to automatic pairing.
                pairs = pair_segments(source, target, SEGMENT_LIMIT, None)
            self.pairs_key, self.pairs = key, pairs
        return self.pairs

    def cached_pairs(self) -> list | None:
        """The pairing of the current text if already computed; never computes."""
        return self.pairs if self.pairs_key == self.pairs_key_now() else None

    def segment_key(self, pair) -> tuple:
        """A pair's identity: where its cores are and what they say. Identical
        paragraphs at different places are different segments."""
        return sr.pair_key(pair, self.payload["source"], self.payload["target"])

    def long_now(self) -> bool:
        return sr.long_mode(self.payload["source"], self.payload["target"])

    def put_parts(self, parts: sr.Parts) -> None:
        """Hold ``parts`` in the payload, in place; not an undo step of its own."""
        value = sr.with_parts(self.payload, parts)
        for key in PART_KEYS:
            if key in value:
                self.payload[key] = value[key]
            else:
                self.payload.pop(key, None)

    def records(self) -> list | None:
        """The long-mode records, one per pair of ``current_pairs()``, or None
        in document mode. The only way records are read: they are normalized
        here whenever the text, inputs, pairing, basis or parts changed.
        Normalizing is not an edit, and an unchanged file stays unchanged."""
        payload = self.payload
        long = self.long_now()
        if not long and not payload.get("segments"):
            return None
        pairs = self.current_pairs() if long else []

        def now() -> tuple:
            # The stored objects themselves: an absent part is None, never a fresh [].
            return (payload["source"], payload["target"], payload["alignments"], self.term_basis, pairs,
                    *(payload.get(key) for key in PART_KEYS))
        seen = self.records_seen
        if seen is None or seen[-1] != payload["direction"] or any(a is not b for a, b in zip(seen, now())):
            self.put_parts(sr.normalize(payload, pairs, self.term_basis, check_limits=False))
            self.records_seen = (*now(), payload["direction"])
        return payload.get("segments") or [] if long else None

    def basis(self) -> sessions.Basis:
        """The current basis of the semantic reuse rule (``sessions.Basis``)."""
        return sessions.Basis(semantic_required=self.semantic_available())

    def semantic_available(self) -> bool:
        return self.capabilities is not None and semantic.CAPABILITY_SEMANTIC_REVIEW in self.capabilities

    def alignments_for(self, source_core: tuple[int, int], target_core: tuple[int, int]) -> list | None:
        """The owner's correspondence for one segment's term request
        (``segment_records.alignment_list``): automatic stays automatic, an
        explicit list, even empty, never becomes automatic."""
        return sr.alignment_list(self.payload["alignments"], self.payload["source"], self.payload["target"],
                                 source_core, target_core)

    def context_for(self, pair) -> dict | None:
        if pair.index == 0:
            return None
        previous = self.current_pairs()[pair.index - 1]
        source, target = self.payload["source"], self.payload["target"]
        return semantic.context_before(source[previous.source.core_start:previous.source.core_end],
                                       target[previous.target.core_start:previous.target.core_end])

    def texts_for_reconcile(self) -> dict[str, str]:
        texts = sessions.verified_texts(self.payload.get("semantic_texts") or [])
        texts.update(self.known_texts)
        return texts

    def semantic_texts(self, payload: dict) -> list[dict]:
        return sessions.semantic_texts_for(payload.get("semantic_decisions") or [], self.texts_for_reconcile(),
                                           payload["source"], payload["target"])

    def reset_term_session(self) -> None:
        """A new document or service: no term basis yet, so nothing saved is current."""
        self.term_basis = None
        self.records_seen = None
        self.pairs_key, self.pairs = None, []

    def reset_known_texts(self, value: dict | None = None) -> None:
        """The texts on-file decisions usually refer to; kept across edits.
        Without ``value`` the manuscript has none (a new document)."""
        self.known_texts = sessions.verified_texts((value or {}).get("semantic_texts") or [])
        for text in ((value or {}).get("source"), (value or {}).get("target")):
            if text is not None:
                self.known_texts.setdefault(sessions.revision(text), text)

    # -- document commands: edits, history, choices and decisions ------------
    # The Qt half asks for one of these and renders the outcome; each one
    # commits exactly the way its button always did. They are the domain
    # operations themselves - which is what makes the workbench's editing
    # behaviour testable without a widget (tests/test_document_controller.py).

    def draft_snapshot(self) -> dict:
        """The undoable draft of the document (``document_edits.draft_of``)."""
        return draft_of(self.payload)

    def restore_draft(self, value: dict) -> None:
        """Hold a draft in the payload, in place."""
        restore_draft_into(self.payload, value)

    def decision_index(self) -> sessions.DecisionIndex:
        """Decisions by occurrence, rebuilt only when the list is replaced."""
        decisions = self.decisions()
        if self.decision_cache is None or self.decision_cache.decisions is not decisions:
            self.decision_cache = sessions.DecisionIndex(decisions)
        return self.decision_cache

    def issue_state(self, issue: sessions.LiveIssue) -> str:
        """What still has to happen about this issue (``semantic_session.state_of``)."""
        return sessions.state_of(issue, self.decision_index(), self.payload["source"], self.payload["target"])

    def apply_edit(self, value: dict, *, view_state: dict | None = None, edits: list | None = None,
                   exempt: sessions.LiveIssue | None = None, remap: AlignmentRemap | None = None,
                   alignment_reset: bool = False) -> EditOutcome:
        """Commit one edit of the document as one undoable step.

        ``value`` is the draft after the change; keys it does not carry keep
        their current value, and a draft without long-mode parts leaves long
        mode. ``edits`` are the text replacements actually performed, as
        ``(side, start, old_end, new_length)``: without them a changed side is
        treated as replaced whole. ``exempt`` keeps the issue a decision was
        about alive across its own edit. ``view_state`` is the host's state to
        restore when this step is undone; without it the host is asked.
        ``remap`` is the edit transaction that produced ``value``'s alignments:
        the entries it invalidated join the session list. ``alignment_reset``
        (an explicit alignment choice) and a direction change start that list
        over; undo brings the old list back either way.
        """
        before = self.draft_snapshot()
        merged = merge_draft(before, value)
        if merged == before:
            return EditOutcome()
        self._typing = None
        self._composition = None
        self._record_step(before, view_state)
        return self._commit_edit(before, merged, edits, exempt, remap,
                                 alignment_reset or before["direction"] != merged["direction"],
                                 self.alignment_invalidations)

    def _record_step(self, before: dict, view_state: dict | None) -> int:
        return self.history.record(HistoryEntry(
            before, self.session.snapshot(), self.host.editor_state() if view_state is None else view_state,
            tuple(self.alignment_invalidations)))

    def _commit_edit(self, before: dict, merged: dict, edits: list | None, exempt: sessions.LiveIssue | None,
                     remap: AlignmentRemap | None, reset: bool, base_invalidations: list) -> EditOutcome:
        self.restore_draft(merged)
        self.document_revision += 1
        cleared = False
        if before["direction"] != merged["direction"]:
            # Results belong to one direction; the other side's cannot be kept.
            if self.session.results or self.semantic_running():
                self.clear_semantic()
                cleared = True
        elif edits is not None or before["source"] != merged["source"] or before["target"] != merged["target"]:
            self._apply_text_edits(before, merged, edits, exempt)
        if reset:
            self.alignment_invalidations = []
        else:
            self.alignment_invalidations = append_invalidations(
                base_invalidations, remap.invalidated if remap is not None else ())
        kept = len(merged["alignments"] or ())
        if remap is None:
            return EditOutcome(True, cleared, any(before[key] != merged[key] for key in REVIEW_KEYS), kept=kept)
        return EditOutcome(True, cleared, any(before[key] != merged[key] for key in REVIEW_KEYS),
                           alignment_notice=remap.notice(before["alignments"]),
                           invalidated=len(remap.invalidated), emptied_targets=len(remap.emptied_targets),
                           kept=kept)

    def edit_text(self, side: str, text: str, *, edit: tuple[int, int, int] | None = None,
                  composing: bool = False, view_state: dict | None = None,
                  exempt: sessions.LiveIssue | None = None) -> EditOutcome:
        """One edit of ``side`` typed, pasted or composed in its editor, as an
        edit transaction (contracts/workbench-state/edit-transactions.json).

        ``edit`` is the range the editor reports, ``(start, old_end,
        new_length)``; without it the change is the minimal splice between the
        texts. Alignments outside the change are kept or shifted, one holding
        it is resized, and only the entries it crosses or empties leave the
        list; an emptied target keeps its source anchor as ``target: None``.

        Undo: a one-scalar edit on the same side within ``TYPING_COALESCE_MS``
        of the last one joins its step, unless it invalidates or empties an
        alignment (a step of its own, so one undo restores the entries) or it
        types or deletes a line break (a paragraph cut is its own step).
        ``composing`` is an input-method composition update: the first opens
        the step, later ones join it, and alignments are judged against the
        text before the composition; the next edit of that side without
        ``composing`` (the composition end, even with the same text) closes it.
        """
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
            remap = remap_for_free_edit(base["alignments"], side, base["source"], base["target"],
                                        next_source, next_target)
        else:
            remap = remap_for_edit(base["alignments"], side, base["source"], base["target"],
                                   next_source, next_target, edit)
        value = dict(before)
        value[side] = text
        value["alignments"] = remap.alignments
        splice = (narrow_splice(current, text, *edit) if edit is not None
                  else compute_minimal_splice(current, text))
        if splice is None:
            splice = compute_minimal_splice(current, text) or (0, 0, 0)
        start, old_end, new_length = splice
        delta = max(old_end - start, new_length)
        line_break = "\n" in current[start:old_end] or "\n" in text[start:start + new_length]
        now = self.clock()
        typing = self._typing
        can_join = (open_ is None and not composing and not remap.touched and not line_break
                    and typing is not None and typing[0] == side and now - typing[1] <= TYPING_COALESCE_MS
                    and delta <= 1 and self.history.top_serial() == typing[2])
        if open_ is None and not can_join:
            serial = self._record_step(before, view_state)
        else:
            serial = typing[2] if typing is not None else 0
            self.history.drop_redo()
        self._typing = ((side, now, serial)
                        if not composing and open_ is None and not remap.touched and not line_break and delta <= 1
                        else None)
        if composing:
            self._composition = open_ if open_ is not None else (side, before, list(base_invalidations))
        else:
            self._composition = None
        edits = [(side, *edit)] if edit is not None else [(side, start, old_end, new_length)]
        merged = merge_draft(before, value)
        return self._commit_edit(before, merged, edits, exempt, remap, False, base_invalidations)

    def set_direction(self, direction: str, *, view_state: dict | None = None) -> EditOutcome:
        """A new translation direction: every explicit alignment is cleared (R7)."""
        value = self.draft_snapshot()
        value["direction"] = direction
        if value["alignments"] is not None:
            value["alignments"] = []
        return self.apply_edit(value, view_state=view_state)

    def _apply_text_edits(self, before: dict, after: dict, edits: list | None,
                          exempt: sessions.LiveIssue | None) -> None:
        """Move the stored results and the records to where the edit left the text."""
        if edits is None:
            edits = [(side, 0, len(before[side]), len(after[side]))
                     for side in ("source", "target") if before[side] != after[side]]
        parts = sr.parts_of(self.payload) if "segments" in self.payload else None
        for side, start, old_end, new_length in edits:
            self.session.apply_edit(side, start, old_end, new_length, exempt=exempt)
            if parts is not None:
                # Records the edit hit become pending (their finished marks go);
                # the rest shift. Normalized on the derived refresh.
                parts = sr.apply_edit(parts, before[side], side, start, old_end, new_length)
        if parts is not None:
            self.put_parts(parts)

    def note_edit(self, edit: tuple) -> None:
        """Text rewritten with identical text: still an edit of that range."""
        self.session.apply_edit(*edit)

    def apply_parts_edit(self, parts: sr.Parts, *, view_state: dict | None = None) -> CommandOutcome:
        """Commit a change of choices, history or finished marks as one undoable
        edit; the editors' text does not change."""
        return CommandOutcome(True, edit=self.apply_edit(sr.with_parts(self.draft_snapshot(), parts),
                                                       view_state=view_state))

    def can_undo(self) -> bool:
        return self.history.can_undo()

    def can_redo(self) -> bool:
        return self.history.can_redo()

    def clear_history(self) -> None:
        """A new document: nothing before it is undoable."""
        self.history.clear()
        self.translation_withdrawn = False
        self.alignment_invalidations = []
        self._typing = None
        self._composition = None

    def restore_invalidations(self, items: Sequence[Mapping[str, Any]]) -> None:
        """The session list a recovery draft carried (malformed entries dropped)."""
        self.alignment_invalidations = [copy.deepcopy(dict(item)) for item in items if valid_invalidation(item)][-64:]

    def undo(self, *, view_state: dict | None = None) -> HistoryOutcome | None:
        """Take back the last edit, its choices, decisions and positions."""
        return self._step_history(undo=True, view_state=view_state)

    def redo(self, *, view_state: dict | None = None) -> HistoryOutcome | None:
        """Put back the last edit taken back."""
        return self._step_history(undo=False, view_state=view_state)

    def _step_history(self, *, undo: bool, view_state: dict | None) -> HistoryOutcome | None:
        """Undo and redo restore text, decisions and the semantic positions together."""
        before = self.draft_snapshot()
        entry = self.history.step(undo=undo, current=HistoryEntry(
            before, self.session.snapshot(), self.host.editor_state() if view_state is None else view_state,
            tuple(self.alignment_invalidations)))
        if entry is None:
            return None
        self._typing = None
        self._composition = None
        later = sr.parts_of(self.payload)
        self.restore_draft(entry.value)
        self.alignment_invalidations = [copy.deepcopy(item) for item in entry.invalidations]
        if "segments" in self.payload:
            # Checks made after the snapshot stay where the content is the same.
            self.put_parts(sr.carry_results(sr.parts_of(self.payload), later))
        self.session.restore(entry.session)
        cleared = before["direction"] != entry.value["direction"]
        if cleared and (self.session.results or self.semantic_running()):
            self.clear_semantic()
        else:
            cleared = False
        return HistoryOutcome(entry, EditOutcome(True, cleared,
                                                 any(before[key] != entry.value[key] for key in REVIEW_KEYS)))

    # Term choices (segment_records). Each returns what the workbench shows:
    # ``REASON_FAILED`` is the records refusing the change, never a crash.

    def reconfirmable(self, index: int, choice: dict) -> bool:
        """An unconfirmed choice can be confirmed again only against a checked
        result that still reports its occurrence and, for an official pair,
        its candidate (SPEC "Runtime" step 7)."""
        return sr.choice_reconfirmable(self.payload["segments"][index], choice, self.term_basis)

    def choose_term(self, index: int, start: int, end: int, choice: str,
                    candidate_id: str | None = None) -> CommandOutcome:
        """The owner's choice at a listed finding: an official pair or not a term."""
        try:
            parts = sr.choose(self.payload, index, start, end, choice, candidate_id)
        except (ValueError, WorkfileError):
            return CommandOutcome(reason=REASON_FAILED)
        return self.apply_parts_edit(parts)

    def unchoose_term(self, index: int, start: int, end: int) -> CommandOutcome:
        """Remove the owner's choice at that span."""
        return self.apply_parts_edit(sr.unchoose(self.payload, index, start, end))

    def restore_choice(self, position: int, index: int) -> CommandOutcome:
        """Bring a history choice back into that record, unconfirmed."""
        try:
            parts = sr.restore(self.payload, position, index)
        except (ValueError, WorkfileError):
            return CommandOutcome(reason=REASON_FAILED)
        return self.apply_parts_edit(parts)

    def confirm_choice(self, index: int, choice: dict) -> CommandOutcome:
        """Confirm an unconfirmed choice again, against the current result."""
        if not self.reconfirmable(index, choice):
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        span = choice["source_span"]
        candidate = choice["candidate"]["candidate_id"] if choice["choice"] == sr.OFFICIAL_PAIR else None
        return self.choose_term(index, span["start"], span["end"], choice["choice"], candidate)

    def set_finished(self, index: int, finished: bool) -> CommandOutcome:
        """Mark (or unmark) a segment as finished."""
        try:
            parts = sr.set_finished(sr.parts_of(self.payload), index, finished)
        except ValueError:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        return self.apply_parts_edit(parts)

    def remove_history_choice(self, position: int) -> CommandOutcome:
        """Drop one entry of the choice history (the save refused it as over its cap)."""
        parts = sr.parts_of(self.payload)
        history = [entry for number, entry in enumerate(parts.history) if number != position]
        return self.apply_parts_edit(parts._replace(history=history))

    def remove_choice(self, index: int) -> CommandOutcome:
        """Drop one document-mode choice (the save refused it as a conflict)."""
        value = self.draft_snapshot()
        value["choices"].pop(index)
        return CommandOutcome(True, edit=self.apply_edit(value))

    def resolve_conflicting_choices_for_save(self) -> CommandOutcome:
        """Drop colliding duplicate historical choices as one undoable edit."""
        cleaned, dropped = sanitize_duplicate_choices(self.payload)
        if dropped <= 0:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        return CommandOutcome(True, edit=self.apply_edit(draft_of(cleaned)))

    # Semantic decisions. The questions and the commits are separate: the view
    # asks ``can_accept``/``can_adjust``/``can_ignore`` to enable its buttons,
    # then asks for one of the three decisions.

    def basis_holds(self, issue: sessions.LiveIssue) -> bool:
        """The issue's semantic basis still holds; checked again right before
        accept or adjust runs.

        This is about one issue, not about reusing its segment's cache: an
        accept elsewhere in the same segment makes the segment cache not
        reusable, but leaves the other issues there executable. The issue's
        own liveness (untouched by edits, spans still equal the text) is
        checked separately by ``issue_state`` and ``span_live``.
        """
        if issue.basis_lost or not self.semantic_available():
            return False
        return any(issue in r.issues and sessions.semantic_part_valid(r) for r in self.session.results)

    def can_accept(self, issue: sessions.LiveIssue | None) -> bool:
        return (issue is not None and issue.issue["suggestion"] is not None and self.basis_holds(issue)
                and self.issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
                and sessions.span_live(issue.suggestion_span, self.payload["target"]))

    def can_adjust(self, issue: sessions.LiveIssue | None) -> bool:
        return (issue is not None and issue.edit_span() is not None and self.basis_holds(issue)
                and self.issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
                and sessions.span_live(issue.edit_span(), self.payload["target"]))

    def can_ignore(self, issue: sessions.LiveIssue | None) -> bool:
        """A decision already made here stays as history; it is not decided again."""
        return (issue is not None and self.issue_state(issue) in sessions.UNHANDLED
                and sessions.decision_for(issue, self.decision_index()) is None)

    @staticmethod
    def selected_option(issue: sessions.LiveIssue, alternative: bool = False) -> tuple[str, str] | None:
        """(replacement, term_effect) of the suggestion or its chosen alternative."""
        suggestion = issue.issue["suggestion"]
        if suggestion is None:
            return None
        alternatives = issue.issue["alternatives"]
        if alternative and alternatives:
            return alternatives[0]["replacement"], alternatives[0]["term_effect"]
        return suggestion["replacement"], suggestion["term_effect"]

    def record_decision(self, issue: sessions.LiveIssue, decision: str, *,
                        applied_text: str | None = None, target: str | None = None,
                        replaced: dict | None = None) -> CommandOutcome:
        """Apply one decision as one undoable edit, after a save preflight."""
        record = sessions.make_decision(issue, decision, applied_text)
        decisions = sessions.with_decision(self.decisions(), issue, record)
        value = self.draft_snapshot()
        value["semantic_decisions"] = decisions
        remap = None
        if target is not None:
            before_target = value["target"]
            value["target"] = target
            if replaced is not None:
                remap = remap_splice(value["alignments"], "target", value["source"], target,
                                     replaced["start"], replaced["end"], len(applied_text or ""))
            else:
                remap = remap_for_free_edit(value["alignments"], "target", value["source"], before_target,
                                            value["source"], target)
            value["alignments"] = remap.alignments
        candidate = copy_payload(self.payload)
        candidate.update(copy_payload(value))
        candidate.setdefault("semantic_history", [])
        candidate["semantic_texts"] = self.semantic_texts(candidate)
        normalize_semantic(candidate)
        if len(decisions) > MAX_SEMANTIC_DECISIONS or not semantic_payload_fits(candidate):
            return CommandOutcome(reason=REASON_FULL)
        edits = None
        if replaced is not None:
            edits = [("target", replaced["start"], replaced["end"], len(applied_text or ""))]
        return CommandOutcome(True, edit=self.apply_edit(value, edits=edits, exempt=issue, remap=remap))

    def accept_issue(self, issue: sessions.LiveIssue | None, *, can_display: Callable[[str], bool],
                     alternative: bool = False) -> CommandOutcome:
        """Apply the issue's suggestion (or its chosen alternative) as one decision."""
        if issue is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        if not self.basis_holds(issue):
            return CommandOutcome(reason=REASON_RECONFIRM)
        option = self.selected_option(issue, alternative)
        if option is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        replacement = option[0]
        try:
            # The executable range must still be exactly what was reported.
            changed = semantic.apply_span(self.payload["target"], issue.suggestion_span, replacement)
        except semantic.StaleSpan:
            return CommandOutcome(reason=REASON_STALE)
        if not can_display(changed):
            return CommandOutcome(reason=REASON_REJECTED)
        return self.record_decision(issue, "accepted", applied_text=replacement, target=changed,
                                    replaced=issue.suggestion_span)

    def adjust_issue(self, issue: sessions.LiveIssue | None, text: str, *,
                     can_display: Callable[[str], bool]) -> CommandOutcome:
        """Apply the owner's own replacement as one adjusted decision."""
        if issue is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        if not self.can_adjust(issue):
            return CommandOutcome(reason=REASON_RECONFIRM)
        if len(text) > semantic.MAX_SIDE_SCALARS:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        try:
            changed = semantic.apply_span(self.payload["target"], issue.edit_span(), text)
        except semantic.StaleSpan:
            return CommandOutcome(reason=REASON_STALE)
        if not can_display(changed):
            return CommandOutcome(reason=REASON_REJECTED)
        return self.record_decision(issue, "adjusted", applied_text=text, target=changed,
                                    replaced=issue.edit_span())

    def ignore_issue(self, issue: sessions.LiveIssue | None) -> CommandOutcome:
        """Keep the report as history without touching the text."""
        if issue is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        return self.record_decision(issue, "ignored")

    # -- document review -----------------------------------------------------

    def set_review_state(self, state: str, *, detail: str = "", request_id: str | None = None) -> None:
        """Set and show the review phase: the one place it changes."""
        self.review_state, self.review_detail, self.request_id = state, detail, request_id
        self.host.render_review_state()

    def cancel_pending(self) -> None:
        """Drop the current document review: its answer is no longer wanted."""
        self.generation += 1
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()

    def start_review(self) -> None:
        """Start the document review from the text as it is now."""
        self.cancel_pending()
        self.stop_semantic()
        generation = self.generation
        payload = self.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        alignments = copy.deepcopy(payload["alignments"])
        self.host.clear_report()
        # A new run retries the segments that failed before.
        self.segment_failures.clear()
        self.semantic_state = "none"
        self.set_review_state("reviewing")
        self.task = asyncio.ensure_future(self.run_review(generation, source, target, direction, alignments))
        self.task.add_done_callback(lambda task: self.task_finished(task, generation))
        self.host.update_controls()

    async def run_review(self, generation: int, source: str, target: str, direction: str,
                         alignments: list | None) -> None:
        try:
            # Old servers answer 404 and the client keeps its previous
            # behaviour: the deterministic review only.
            capabilities = await self.api_client.get_capabilities()
        except ClientError as exc:
            if generation == self.generation:
                self.set_review_state("error", detail=exc.message, request_id=exc.request_id)
            return
        except asyncio.CancelledError:
            if generation == self.generation:
                self.set_review_state("cancelled")
            return
        if generation != self.generation:
            return
        self.capabilities = capabilities
        document = len(source) <= DOCUMENT_REVIEW_LIMIT and len(target) <= DOCUMENT_REVIEW_LIMIT
        self.term_mode = "document" if document else "segments"
        # Every review, short or long, applies the reuse rule first (one entry).
        self.rebuild_plan()
        self.host.refresh_semantic()
        if document and not await self.run_document_review(generation, source, target, direction, alignments):
            return
        if generation != self.generation:
            return
        if not document:
            self.set_review_state("none")
        if not document or self.semantic_available():
            self.start_semantic_run()
        self.host.update_controls()

    async def run_document_review(self, generation: int, source: str, target: str, direction: str,
                                  alignments: list | None) -> bool:
        try:
            report = await self.api_client.review_pair(source, target, direction, alignments=alignments)
        except ClientError as exc:
            if generation == self.generation:
                self.set_review_state("error", detail=exc.message, request_id=exc.request_id)
            return False
        except asyncio.CancelledError:
            if generation == self.generation:
                self.set_review_state("cancelled")
            return False
        if generation != self.generation:
            return False
        # The resolutions this check sent: a short manuscript sends none, and
        # [] (not unknown) lets the next check compare with it, as on the Web.
        snapshot = {"source": source, "target": target, "direction": direction,
                    "alignments": alignments, "report": report, "resolutions": []}
        candidate = copy_payload(self.payload)
        candidate["history"] = (candidate["history"] + [snapshot])[-2:]
        try:
            serialize_workfile(candidate)
        except WorkfileError as exc:
            self.set_review_state("error", detail=strings.MANUSCRIPT_REPORT_UNSAVEABLE)
            if exc.code == "workfile_duplicate_choices":
                self.host.report_unstorable_report(exc.code)
            return False
        self.payload = candidate
        self.host.trust_review_snapshot(snapshot)
        self.document_revision += 1
        # The phase is the controller's, so a run that commits says so even when
        # the report itself is only what the host draws.
        self.set_review_state("current", request_id=report["request_id"])
        self.host.show_term_report(report)
        self.host.report_status(strings.MANUSCRIPT_REPORT_NOTE)
        self.host.update_controls()
        return True

    def task_finished(self, task: asyncio.Task, generation: int) -> None:
        if generation != self.generation:
            return
        self.task = None
        if task.cancelled():
            self.set_review_state("cancelled")
        self.host.update_controls()

    def cancel_review(self) -> None:
        """The cancel button: pause what is running, in the order a press means it."""
        if self.translating():
            run = self.translate_run
            self.stop_translation()
            self.translation_stopped(run, "paused")
            return
        if self.semantic_running():
            self.stop_semantic("paused")
            self.host.refresh_semantic()
            self.host.update_controls()
            return
        if self.task is None:
            return
        self.cancel_pending()
        self.set_review_state("cancelled")
        self.host.update_controls()

    # -- segmented term and semantic run -------------------------------------
    def semantic_running(self) -> bool:
        return self.semantic_task is not None and not self.semantic_task.done()

    def start_semantic_run(self) -> None:
        self.stop_semantic()
        self.history_run += 1
        self.semantic_state = "running"
        generation = self.semantic_generation
        self.semantic_task = asyncio.ensure_future(self.run_segments(generation))
        self.semantic_task.add_done_callback(lambda task: self.semantic_finished(task, generation))
        self.host.render_semantic_progress()

    def stop_semantic(self, state: str | None = None) -> None:
        """Hard stop of the segmented run; the results already stored stay."""
        self.semantic_generation += 1
        task, self.semantic_task = self.semantic_task, None
        self.session.pending = None
        if task is not None and not task.done():
            task.cancel()
        if state is not None:
            self.semantic_state = state

    def clear_semantic(self) -> None:
        """A new document, direction or service: no semantic result survives."""
        self.stop_semantic("none")
        self.session.clear()
        self.segment_failures.clear()
        self.segment_notes.clear()
        self.host.refresh_semantic()
        self.host.refresh_segments()

    def semantic_finished(self, task: asyncio.Task, generation: int) -> None:
        if generation != self.semantic_generation:
            return
        self.semantic_task = None
        if task.cancelled():
            self.semantic_state = "paused"
        elif task.exception() is not None:
            self.semantic_state = "error"
            self.semantic_detail = strings.ERROR_MSG_UNKNOWN
        self.host.refresh_semantic()
        self.host.refresh_segments_if_shown()
        self.host.update_controls()

    def next_review(self, attempted: set) -> sr.NextReview | None:
        pairs = self.current_pairs()
        terms = self.term_mode == "segments" and self.records() is not None
        return sr.next_review(self.payload, pairs, self.term_basis, self.session, self.basis(),
                              terms=terms, attempted=attempted, views=self.record_views)

    def ready_pair_for(self, result: sessions.SegmentResult):
        source, target = self.payload["source"], self.payload["target"]
        return next((p for p in self.current_pairs()
                     if p.status == STATUS_READY and result.matches(p, source, target)), None)

    def rebuild_plan(self) -> None:
        """Apply the reuse rule: keep only results, failures and notes that a
        current ready pair can use under the current basis."""
        source, target = self.payload["source"], self.payload["target"]
        pairs = self.current_pairs()
        self.session.rebuild(pairs, source, target, self.basis())
        keys = {self.segment_key(p) for p in pairs if p.status == STATUS_READY}
        self.segment_failures = {k: v for k, v in self.segment_failures.items() if k in keys}
        self.segment_notes = {k: v for k, v in self.segment_notes.items() if k in keys}

    async def check_term(self, index: int, generation: int) -> ClientError | str | None:
        """One segment's term check (SPEC "Runtime" steps 4 to 7): the planned
        request, at most one fallback without resolutions when Core rejects
        them, and one more request when the kept choices must go out. Returns
        None when the record is current or holds the new result, ``"changed"``
        when the text moved on while the request was out, ``"not_storable"``,
        or the error."""
        request = sr.plan_request(self.payload, index, self.term_basis)
        fell_back = False

        def still_there() -> bool:
            # The owner may have edited while the refused request was out:
            # replan only for the same segment, with the same cores, in long mode.
            records = self.records() if self.term_mode == "segments" else None
            if records is None or not 0 <= index < len(records):
                return False
            record, payload = records[index], self.payload
            return (payload["direction"] == request.direction
                    and payload["source"][record["source_start"]:record["source_end"]] == request.source
                    and payload["target"][record["target_start"]:record["target_end"]] == request.target)
        for _ in range(3):
            try:
                report = await self.api_client.review_pair(
                    request.source, request.target, request.direction, alignments=request.alignments,
                    resolutions=request.resolutions or None, resolution_context=request.resolution_context)
            except ClientError as exc:
                if (exc.code == ERROR_INVALID_REQUEST and request.resolutions and not fell_back
                        and generation == self.semantic_generation):
                    fell_back = True
                    if not still_there():
                        return "changed"
                    request = sr.plan_request(self.payload, index, self.term_basis, allow_resolutions=False)
                    continue
                return exc
            if generation != self.semantic_generation:
                return "changed"
            applied = sr.apply_response(self.payload, request, report, self.term_basis)
            self.term_basis = applied.basis
            self.put_parts(applied.parts)
            self.document_revision += 1
            if applied.outcome == "discarded":
                return "changed"
            if applied.outcome == "not_storable":
                return "not_storable"
            if not applied.again:
                return None
            request = sr.plan_request(self.payload, index, self.term_basis)
        return None

    async def run_segments(self, generation: int) -> None:
        """Serial, one request in flight. Long-manuscript term checks follow the
        segment record reuse rule (``segment_records``); semantic requests the
        semantic reuse rule (``sessions.Basis``). A failure changes only its
        segment, and the next run continues from it."""
        attempted: set[tuple] = set()
        failure_streak: tuple[str | None, int] = (None, 0)
        self.rebuild_plan()
        if self.term_mode == "segments":
            # Segments that need no request still show their term results.
            self.host.show_segment_term_report()
        while generation == self.semantic_generation:
            next_review = self.next_review(attempted)
            if next_review is None:
                break
            pair = self.current_pairs()[next_review.index]
            source, target, direction = self.payload["source"], self.payload["target"], self.payload["direction"]
            key = self.segment_key(pair)
            attempted.add(key)
            if not pair_is_sendable(pair, source, target):
                # The final pre-send assertion: never send an oversized pair.
                self.segment_failures[key] = "needs_manual_split"
                continue
            need_term, need_semantic = next_review.term, next_review.semantic
            self.session.pending = sessions.SegmentResult(
                sent_source=key[4], sent_target=key[5],
                source_core=(pair.source.core_start, pair.source.core_end),
                target_core=(pair.target.core_start, pair.target.core_end),
                hard_cut=pair.hard_cut, report=None, index=pair.index)
            before = self.context_for(pair)
            precedents = semantic.select_precedents(
                sessions.precedent_candidates(self.payload, self.current_pairs()), key[4], pair.source.core_start
            ) if self.host.precedents_selected() and need_semantic else []
            self.host.render_semantic_progress()
            report = None
            error: ClientError | str | None = None
            if need_term:
                error = await self.check_term(pair.index, generation)
                if generation != self.semantic_generation:
                    return
                if error == "changed":
                    error = None  # the edited segment is a new segment; it gets its own turn
                if error is None:
                    self.segment_failures.pop(key, None)
                    self.host.show_segment_term_report()
            try:
                if error is None and need_semantic and generation == self.semantic_generation:
                    report = await self.api_client.semantic_review(
                        key[4], key[5], direction, before=before, precedents=precedents)
            except ClientError as exc:
                error = exc
            if generation != self.semantic_generation:
                return
            pending, self.session.pending = self.session.pending, None
            # Only an answer for a pair that still exists, exactly, is stored.
            target_pair = self.ready_pair_for(pending) if pending is not None else None
            if target_pair is not None:
                key = self.segment_key(target_pair)
                if report is not None:
                    self.store_semantic(target_pair, pending, report, direction)
            if error is not None:
                code = error if isinstance(error, str) else error.code
                if code == ERROR_CANCELLED:
                    return
                if code in QUOTA_CODES:
                    self.semantic_state = "quota"
                    return
                failure_streak = (code, failure_streak[1] + 1 if failure_streak[0] == code else 1)
                if target_pair is not None:
                    self.segment_failures[key] = "failed"
                if code in FATAL_CODES or failure_streak[1] >= REPEATED_FAILURES:
                    self.semantic_state = "error"
                    self.semantic_detail = (strings.MANUSCRIPT_TERM_NOT_STORABLE if code == "not_storable"
                                            else error.message)
                    return
                self.host.refresh_semantic()
                self.host.refresh_segments_if_shown()
                continue
            if report is not None and report["status"] != "complete":
                if target_pair is not None:
                    self.segment_failures[key] = "not_reviewed"
                continue
            self.host.refresh_segments_if_shown()
            failure_streak = (None, 0)
        if generation == self.semantic_generation:
            self.semantic_state = "done"
            self.rebuild_plan()
            if self.term_mode == "segments":
                self.host.show_segment_term_report()

    def store_semantic(self, pair, pending: sessions.SegmentResult, report: semantic.SemanticReviewReport,
                       direction: str) -> None:
        """Store a semantic report for ``pair``; an incomplete one is not stored."""
        source, target = self.payload["source"], self.payload["target"]
        if report["status"] != "complete":
            return
        result = self.session.result_for(pair, source, target)
        if result is None:
            result = sessions.SegmentResult(
                sent_source=pending.sent_source, sent_target=pending.sent_target,
                source_core=pending.source_core, target_core=pending.target_core,
                hard_cut=pending.hard_cut, report=None, index=pair.index)
            self.session.add(result)
        key = self.segment_key(pair)
        result.report, result.index = report, pair.index
        result.issues = sessions.live_issues_from_report(
            report, segment_index=pair.index, source_offset=result.source_core[0],
            target_offset=result.target_core[0], source_revision=sessions.revision(source),
            target_revision=sessions.revision(target))
        self.known_texts.setdefault(sessions.revision(source), source)
        self.known_texts.setdefault(sessions.revision(target), target)
        notes = []
        if report["omitted_issues"]:
            notes.append(strings.MANUSCRIPT_SEGMENT_HINT["truncated"].format(count=report["omitted_issues"]))
        if report["dropped_unanchored"]:
            notes.append(strings.MANUSCRIPT_SEGMENT_HINT["dropped"].format(count=report["dropped_unanchored"]))
        self.segment_notes[key] = notes
        # "Exactly one candidate" is judged over the whole current report.
        sessions.apply_reconcile(self.session.issues(), self.decisions(), self.texts_for_reconcile(),
                                 source, target)
        self.remember_semantic_report(direction)
        if not self.basis().needs(result):
            self.segment_failures.pop(key, None)
        self.host.refresh_semantic()
        self.host.refresh_segments_if_shown()
        self.host.update_controls()

    def remember_semantic_report(self, direction: str) -> None:
        """Keep this run as one untrusted snapshot of the whole manuscript
        (Web ``semantic_history``); the writer drops the oldest if too large."""
        source, target = self.payload["source"], self.payload["target"]
        segments = [{"segment_index": r.index, "source_start": r.source_core[0], "source_end": r.source_core[1],
                     "target_start": r.target_core[0], "target_end": r.target_core[1], "report": r.report}
                    for r in self.session.results
                    if sessions.semantic_part_valid(r) and self.ready_pair_for(r) is not None]
        snapshot = {"source": source, "target": target, "direction": direction,
                    "segments": copy.deepcopy(segments[:MAX_HISTORY_SEGMENTS])}
        candidate = copy_payload(self.payload)
        history = list(candidate.get("semantic_history", []))
        if self.history_written == self.history_run and history:
            history[-1] = snapshot
        else:
            history.append(snapshot)
        candidate["semantic_history"] = history[-MAX_SEMANTIC_HISTORY:]
        candidate.setdefault("semantic_decisions", [])
        candidate["semantic_texts"] = self.semantic_texts(candidate)
        if semantic_payload_fits(normalize_semantic(candidate)):
            self.payload = candidate
            self.history_written = self.history_run
            self.document_revision += 1

    # -- translation of the target side --------------------------------------
    def translating(self) -> bool:
        return self.translate_task is not None and not self.translate_task.done()

    def stop_translation(self) -> None:
        """Hard stop: nothing is written and nothing is offered afterwards."""
        self.translate_generation += 1
        task, self.translate_task = self.translate_task, None
        self.translate_run, self.translate_state = None, TRANSLATION_IDLE
        if task is not None and not task.done():
            task.cancel()

    def resumable(self) -> Translation | None:
        """The stopped run, while the text is exactly what it left."""
        run = self.translate_run
        if run is None or self.translate_state not in TRANSLATION_RESUMABLE or self.translating():
            return None
        payload = self.payload
        same = (payload["source"] == run.source and payload["direction"] == run.direction
                and payload["target"] == run.target)
        return run if same else None

    def resumable_partial_prefix(self) -> tuple[int, str, int, str, tuple[str, str]] | None:
        """For an unaligned, multi-segment partial-prefix manuscript, return
        ``(tail_start, tail_text, append_start, append_prefix, before_pair)``."""
        payload = self.payload
        source, target = payload["source"], payload["target"]
        if payload["alignments"] is not None or blank(source) or blank(target):
            return None
        if len(segment_text(source)) < 2:
            return None
        if any(p.status == STATUS_UNTRANSLATED for p in self.current_pairs()):
            return None
        s_paras = seg_paragraph_ranges(source)
        t_paras = seg_paragraph_ranges(target)
        if not (1 <= len(t_paras) < len(s_paras)):
            return None
        prefix_count = len(t_paras)
        tail_start = s_paras[prefix_count][0]
        tail_text = source[tail_start:]
        s_prev_cs, s_prev_ce = seg_core(source, *s_paras[prefix_count - 1])
        t_last_cs, t_last_ce = seg_core(target, *t_paras[-1])
        sep = source[s_prev_ce:tail_start]
        if target[t_last_ce:] == sep:
            append_start = len(target)
            append_prefix = ""
        else:
            append_start = t_last_ce
            append_prefix = sep
        before_pair = (source[s_prev_cs:s_prev_ce], target[t_last_cs:t_last_ce])
        return tail_start, tail_text, append_start, append_prefix, before_pair

    def has_resumable_remainder(self) -> bool:
        """Whether a non-blank manuscript has remaining untranslated paragraphs to resume."""
        if self.translation_withdrawn or blank(self.payload["source"]) or blank(self.payload["target"]):
            return False
        if any(p.status == STATUS_UNTRANSLATED for p in self.current_pairs()):
            return True
        return self.resumable_partial_prefix() is not None

    def withdraw_translation_offer(self) -> str | None:
        """An edit, undo or redo withdraws the offer to continue.

        Returns the status line the offer left, so the host can replace exactly
        that line and no other, or None when the offer still holds.
        """
        if self.translate_state in TRANSLATION_RESUMABLE and self.resumable() is None:
            previous = self.translate_status
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.translation_withdrawn = True
            return previous
        return None

    def _translation_ui(self) -> None:
        """A translation state changed: re-render the controls, and the segment
        list too while that is the track on screen.

        The rows carry the run's own verdict for pairs it cannot have judged
        yet, so they go stale twice per run - when it starts and when it stops -
        and a stop that writes nothing (a pause or a failure before the first
        finished piece) would otherwise leave "翻译中" on screen for good.
        """
        self.host.refresh_segments_if_shown()
        self.host.update_controls()

    def _adjacent_before_context(self, pairs: list, target_pair) -> tuple[str, str] | None:
        idx = pairs.index(target_pair)
        source, target = self.payload["source"], self.payload["target"]
        for prev in reversed(pairs[:idx]):
            if prev.status == STATUS_READY and prev.target.core_end > prev.target.core_start:
                return (
                    source[prev.source.core_start:prev.source.core_end],
                    target[prev.target.core_start:prev.target.core_end],
                )
        return None

    def start_translation(self, *, single_segment: bool = False) -> None:
        """Translate a blank target, resume remaining untranslated paragraphs,
        or translate/re-translate one segment without overwriting finished work."""
        payload = self.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        cursor = self.resumable()
        pair = None
        if cursor is not None and cursor.segment is not None:
            wanted = STATUS_READY if cursor.retranslate else STATUS_UNTRANSLATED
            pair = next((p for p in self.current_pairs()
                         if p.status == wanted and p.source.core_start == cursor.segment), None)
            if pair is None:
                cursor = None

        partial_tail = None
        batch_spans: list[tuple[int, int]] = []
        retranslate_pair = None
        if cursor is None and blank(target):
            self.translation_withdrawn = False
        elif cursor is None:
            selected = self.host.selected_segment()
            pairs = self.current_pairs()
            if single_segment:
                if selected is not None and selected.status == STATUS_UNTRANSLATED:
                    pair = selected
                elif (
                    selected is not None
                    and selected.status == STATUS_READY
                    and not selected.hard_cut
                    and 0 < selected.source.core_end - selected.source.core_start <= SEGMENT_LIMIT
                ):
                    retranslate_pair = selected
                else:
                    self.host.report_status(strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK)
                    return
            else:
                if self.translation_withdrawn:
                    self.host.report_status(strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK)
                    return
                untranslated = [p for p in pairs if p.status == STATUS_UNTRANSLATED]
                if untranslated:
                    pair = untranslated[0]
                    batch_spans = [(p.source.core_start, p.source.core_end) for p in untranslated]
                else:
                    partial_tail = self.resumable_partial_prefix()
                    if partial_tail is None:
                        self.host.report_status(strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK)
                        return

        if retranslate_pair is not None:
            self._launch_retranslate(retranslate_pair)
            return

        retry = cursor is not None and self.translate_state == "failed"
        self.stop_translation()
        self.translation_withdrawn = False
        if cursor is not None and cursor.batch_remaining:
            self._launch_batch_untranslated(cursor, retry=retry)
            return
        if batch_spans:
            initial = Translation(
                generation=self.translate_generation,
                source=source,
                direction=direction,
                target=target,
                segment=batch_spans[0][0],
                batch_remaining=list(batch_spans),
                batch_total=len(batch_spans),
                batch_done=0,
            )
            self._launch_batch_untranslated(initial, retry=False)
            return

        if cursor is not None:
            fresh = {"generation": self.translate_generation, "retry": retry, "out": list(cursor.out)}
            run = replace(cursor, **fresh)
            text = (
                source[run.source_base:] if run.source_base
                else (source if pair is None else source[pair.source.core_start:pair.source.core_end])
            )
            pieces = segment_text(text, SEGMENT_LIMIT)
            self.translate_run, self.translate_state = run, "retrying" if retry else "waiting"
            self.translate_task = asyncio.ensure_future(self.run_translation(run, pair, text, pieces))
            self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
            self._translation_ui()
            return

        if partial_tail is not None:
            tail_start, tail_text, append_start, append_prefix, before_pair = partial_tail
            pieces = segment_text(tail_text, SEGMENT_LIMIT)
            run = Translation(
                generation=self.translate_generation,
                source=source,
                direction=direction,
                target=target,
                previous=before_pair,
                append_start=append_start,
                append_prefix=append_prefix,
                source_base=tail_start,
                count=sum(1 for p in pieces if p.core_end > p.core_start),
            )
            self.translate_run, self.translate_state = run, "waiting"
            self.translate_task = asyncio.ensure_future(self.run_translation(run, None, tail_text, pieces))
            self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
            self._translation_ui()
            return

        run = Translation(
            generation=self.translate_generation,
            source=source,
            direction=direction,
            target=target,
            segment=None if pair is None else pair.source.core_start,
            previous=self._adjacent_before_context(self.current_pairs(), pair) if pair is not None else None,
        )
        text = source if pair is None else source[pair.source.core_start:pair.source.core_end]
        pieces = segment_text(text, SEGMENT_LIMIT)
        run.count = sum(1 for piece in pieces if piece.core_end > piece.core_start)
        self.translate_run, self.translate_state = run, "waiting"
        self.translate_task = asyncio.ensure_future(self.run_translation(run, pair, text, pieces))
        self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
        self._translation_ui()

    def _launch_retranslate(self, pair) -> None:
        self.stop_translation()
        self.translation_withdrawn = False
        payload = self.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        text = source[pair.source.core_start:pair.source.core_end]
        pieces = segment_text(text, SEGMENT_LIMIT)
        run = Translation(
            generation=self.translate_generation,
            source=source,
            direction=direction,
            target=target,
            segment=pair.source.core_start,
            retranslate=True,
            previous=self._adjacent_before_context(self.current_pairs(), pair),
            count=sum(1 for piece in pieces if piece.core_end > piece.core_start),
        )
        self.translate_run, self.translate_state = run, "waiting"
        self.translate_task = asyncio.ensure_future(self.run_translation(run, pair, text, pieces))
        self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
        self._translation_ui()

    def retranslate_segment(self, pair=None) -> None:
        """Translate a single ready segment again and replace only its core span."""
        target_pair = pair if pair is not None else self.host.selected_segment()
        if (
            target_pair is None
            or target_pair.status != STATUS_READY
            or target_pair.hard_cut
            or not (0 < target_pair.source.core_end - target_pair.source.core_start <= SEGMENT_LIMIT)
        ):
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK)
            return
        self._launch_retranslate(target_pair)

    def _launch_batch_untranslated(self, base_run: Translation, *, retry: bool) -> None:
        run = Translation(
            generation=self.translate_generation,
            source=self.payload["source"],
            direction=self.payload["direction"],
            target=self.payload["target"],
            segment=base_run.batch_remaining[0][0],
            retry=retry,
            previous=base_run.previous,
            batch_remaining=list(base_run.batch_remaining),
            batch_total=base_run.batch_total or len(base_run.batch_remaining),
            batch_done=base_run.batch_done,
        )
        self.translate_run, self.translate_state = run, "retrying" if retry else "waiting"
        self.translate_task = asyncio.ensure_future(self.run_batch_untranslated(run))
        self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
        self._translation_ui()

    async def run_batch_untranslated(self, run: Translation) -> None:
        generation = run.generation
        try:
            self.capabilities = await self.api_client.get_capabilities()
            context_on = semantic.CAPABILITY_TRANSLATION_CONTEXT in self.capabilities
            candidates = (sessions.precedent_candidates(self.payload, self.current_pairs())
                          if context_on and self.host.precedents_selected() else [])
            template = strings.MANUSCRIPT_TRANSLATE_RETRYING if run.retry else strings.MANUSCRIPT_TRANSLATING
            while run.batch_remaining:
                core_start, _core_end = run.batch_remaining[0]
                pairs = self.current_pairs()
                pair = next(
                    (p for p in pairs if p.status == STATUS_UNTRANSLATED and p.source.core_start == core_start),
                    None,
                )
                if pair is None:
                    run.batch_remaining.pop(0)
                    continue
                ctx_previous = self._adjacent_before_context(pairs, pair)
                pair_source = run.source[pair.source.core_start:pair.source.core_end]
                pieces = segment_text(pair_source, SEGMENT_LIMIT)
                self.host.report_status(template.format(index=run.batch_done + 1, total=run.batch_total))
                translated_parts: list[str] = []
                for piece in pieces:
                    core = pair_source[piece.core_start:piece.core_end]
                    if not core:
                        translated_parts.append(pair_source[piece.start:piece.end])
                        continue
                    context = None
                    if context_on:
                        before = semantic.context_before(*ctx_previous) if ctx_previous is not None else None
                        precedents = semantic.select_precedents(
                            candidates, core, pair.source.core_start + piece.core_start
                        )
                        context = {**({"before": before} if before is not None else {}),
                                   **({"precedents": precedents} if precedents else {})} or None
                    result = await self.api_client.translate(core, to=run.direction, context=context)
                    if generation != self.translate_generation:
                        return
                    translated_parts.append(
                        pair_source[piece.start:piece.core_start] + result.text + pair_source[piece.core_end:piece.end]
                    )
                    ctx_previous = (core, result.text)
                if generation != self.translate_generation:
                    return
                if not self.translation_basis_unchanged(run):
                    self.translate_run, self.translate_state = None, TRANSLATION_IDLE
                    self.host.report_status(strings.MANUSCRIPT_TRANSLATE_CHANGED)
                    self._translation_ui()
                    return
                if not self.write_translation(run, "".join(translated_parts), pair):
                    self.translate_run, self.translate_state = None, TRANSLATION_IDLE
                    self.host.report_status(strings.MANUSCRIPT_EDIT_REJECTED)
                    self._translation_ui()
                    return
                run.previous = ctx_previous
                run.target = self.payload["target"]
                run.source = self.payload["source"]
                run.batch_remaining.pop(0)
                run.batch_done += 1
                run.done = run.batch_done
                run.count = run.batch_total
                run.segment = run.batch_remaining[0][0] if run.batch_remaining else None
                run.written = True
                run.retry = False
        except ClientError as exc:
            if generation == self.translate_generation:
                run.done = run.batch_done
                run.count = run.batch_total
                if exc.code == ERROR_CANCELLED:
                    self.translation_stopped(run, "paused")
                else:
                    self.translation_stopped(
                        run,
                        "paused" if exc.code in QUOTA_CODES else "failed",
                        exc.message,
                        quota=exc.code in QUOTA_CODES,
                    )
            return
        if generation != self.translate_generation:
            return
        self.translate_task = None
        self.translate_run, self.translate_state = None, TRANSLATION_IDLE
        mismatched = sum(1 for p in self.current_pairs() if p.status == STATUS_NEEDS_MANUAL_SPLIT)
        self.host.report_translation_complete(mismatched)
        self.host.refresh_segments_if_shown()

    def translation_done(self, run: Translation) -> None:
        """A task that ended without an outcome (an unexpected error) is a failure."""
        if run.generation == self.translate_generation and self.translate_state in ("waiting", "retrying"):
            self.translation_stopped(run, "failed", strings.ERROR_MSG_UNKNOWN)
        self._translation_ui()

    async def run_translation(self, run: Translation, pair, text: str, pieces: list) -> None:
        generation = run.generation
        base = run.source_base if pair is None else pair.source.core_start
        try:
            # Capabilities are part of the basis: probe again, never reuse.
            self.capabilities = await self.api_client.get_capabilities()
            context_on = semantic.CAPABILITY_TRANSLATION_CONTEXT in self.capabilities
            candidates = (sessions.precedent_candidates(self.payload, self.current_pairs())
                          if context_on and self.host.precedents_selected() else [])
            template = strings.MANUSCRIPT_TRANSLATE_RETRYING if run.retry else strings.MANUSCRIPT_TRANSLATING
            for position in range(run.next_piece, len(pieces)):
                piece = pieces[position]
                core = text[piece.core_start:piece.core_end]
                if not core:
                    run.out.append(text[piece.start:piece.end])
                    run.next_piece = position + 1
                    continue
                self.host.report_status(template.format(index=run.done + 1, total=run.count))
                context = None
                if context_on:
                    before = semantic.context_before(*run.previous) if run.previous is not None else None
                    precedents = semantic.select_precedents(candidates, core, base + piece.core_start)
                    context = {**({"before": before} if before is not None else {}),
                               **({"precedents": precedents} if precedents else {})} or None
                result = await self.api_client.translate(core, to=run.direction, context=context)
                if generation != self.translate_generation:
                    return
                run.out.append(text[piece.start:piece.core_start] + result.text + text[piece.core_end:piece.end])
                run.next_piece, run.previous = position + 1, (core, result.text)
                run.done += 1
        except ClientError as exc:
            if generation == self.translate_generation:
                if exc.code == ERROR_CANCELLED:
                    self.translation_stopped(run, "paused")
                else:
                    self.translation_stopped(run, "paused" if exc.code in QUOTA_CODES else "failed",
                                             exc.message, quota=exc.code in QUOTA_CODES)
            return
        if generation != self.translate_generation:
            return
        self.translate_task = None
        if not self.translation_basis_unchanged(run):
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            self._translation_ui()
            return
        if not self.write_translation(run, "".join(run.out), pair):
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.host.report_status(strings.MANUSCRIPT_EDIT_REJECTED)
            self._translation_ui()
            return
        self.translate_run, self.translate_state = None, TRANSLATION_IDLE
        mismatched = sum(1 for p in self.current_pairs() if p.status == STATUS_NEEDS_MANUAL_SPLIT)
        self.host.report_translation_complete(mismatched)
        if run.retranslate and not mismatched:
            self.host.report_status(strings.MANUSCRIPT_RETRANSLATED)
        # The rows carry the run's own verdict; it just ended.
        self.host.refresh_segments_if_shown()

    def translation_basis_unchanged(self, run: Translation) -> bool:
        payload = self.payload
        return (payload["source"] == run.source and payload["direction"] == run.direction
                and payload["target"] == run.target)

    def write_translation(self, run: Translation, added: str, pair=None) -> bool:
        """Write finished pieces as one undoable edit; False if the editor
        cannot show the result."""
        target = run.target
        current_alignments = self.payload["alignments"]
        source = self.payload["source"]
        if pair is None:
            if run.append_start is not None and not run.written:
                inserted = run.append_prefix + added
                changed = target[:run.append_start] + inserted
                edit = ("target", run.append_start, len(target), len(inserted))
            else:
                start = len(target) if run.written else 0
                changed = (target if run.written else "") + added
                edit = ("target", start, len(target), len(added))
            # A new whole translation leaves no explicit alignment confirmed
            # (the Web's whole-text writes do the same).
            remap = AlignmentRemap(None if current_alignments is None else [])
        elif run.retranslate:
            start, end = pair.target.core_start, pair.target.core_end
            changed = target[:start] + added + target[end:]
            edit = ("target", start, end, len(added))
            remap = remap_splice(current_alignments, "target", source, changed, start, end, len(added))
        else:
            position = pair.target.core_start
            left = "\n\n" if position > 0 and target[position - 1] not in WHITESPACE else ""
            right = "\n\n" if position < len(target) and target[position] not in WHITESPACE else ""
            inserted = left + added + right
            changed = target[:position] + inserted + target[position:]
            edit = ("target", position, position, len(inserted))
            fill_start = position + len(left)
            fill_end = fill_start + len(added)
            if fill_end > fill_start:
                remap = remap_for_target_fill(current_alignments, source, changed, pair.source.start,
                                              pair.source.end, position, fill_start, fill_end, len(inserted))
            else:
                remap = remap_splice(current_alignments, "target", source, changed, position, position,
                                     len(inserted))
        return self.host.apply_target_write(changed, edit, remap=remap)

    def translation_stopped(self, run: Translation, state: str, reason: str = "", *,
                            quota: bool = False) -> None:
        """The run ended before its last piece: the owner paused it, the
        service asked to wait, or a piece failed. The run stays as the cursor
        for the next press."""
        self.translate_task = None
        if not self.translation_basis_unchanged(run):
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            self._translation_ui()
            return
        segment = run.segment is not None and not run.batch_remaining
        if not segment and not run.batch_remaining:
            kept = "".join(run.out)
            if kept:
                if not self.write_translation(run, kept):
                    self.translate_run, self.translate_state = None, TRANSLATION_IDLE
                    self.host.report_status(strings.MANUSCRIPT_EDIT_REJECTED)
                    self._translation_ui()
                    return
                run.target, run.written = self.payload["target"], True
                if run.append_start is not None:
                    run.append_start = None
                    run.append_prefix = ""
            run.out = []
        if state == "failed":
            text = (strings.MANUSCRIPT_TRANSLATE_SEGMENT_FAILED if segment else strings.MANUSCRIPT_TRANSLATE_FAILED).format(
                index=run.done + 1, total=run.count, done=run.done, reason=reason)
        else:
            if segment:
                template = strings.MANUSCRIPT_TRANSLATE_SEGMENT_QUOTA if quota else strings.MANUSCRIPT_TRANSLATE_SEGMENT_PAUSED
            else:
                template = strings.MANUSCRIPT_TRANSLATE_QUOTA if quota else strings.MANUSCRIPT_TRANSLATE_PAUSED
            text = template.format(done=run.done, total=run.count)
        self.translate_run, self.translate_state, self.translate_status = run, state, text
        self.host.report_status(text)
        self._translation_ui()
