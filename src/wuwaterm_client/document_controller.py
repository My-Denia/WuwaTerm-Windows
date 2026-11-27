"""Execution controller of the manuscript workbench: thin facade over decoupled services.

Composes five pure-Python boundaries (no desktop toolkit and no HTTP client):

* ``document.alignment_state.AlignmentState`` — immutable ``PairingSnapshot`` and alignment queries
* ``document.edit_store.DocumentEditStore`` — payload, revision, edit transactions, history, choices
* ``services.provenance_service.DictionaryProvenanceService`` — single live dictionary provenance source
* ``services.review_coordinator.ReviewCoordinator`` — term review, structure check, semantic review
* ``services.translation_runner.TranslationRunner`` — translation, retranslation, continuation, TM prefill

All existing attributes and methods on :class:`DocumentRunController` remain
available as clean property/method delegations for backward compatibility with
headless contract tests and the Qt host adapter.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from . import segment_records as sr
from . import semantic
from . import semantic_session as sessions
from . import structure
from . import translation_memory as tm
from .document.alignment_state import AlignmentState
from .document.edit_store import DocumentEditStore
from .document_edits import AlignmentRemap, CommandOutcome, EditOutcome, HistoryOutcome
from .manuscript import ReviewReport
from .services.provenance_service import (
    IDENTITY_FAILED,
    IDENTITY_FRESH,
    IDENTITY_SUPERSEDED,
    IDENTITY_UNSUPPORTED,
    DictionaryIdentity,
    DictionaryProvenanceService,
)
from .services.review_coordinator import (
    DOCUMENT_REVIEW_LIMIT,
    FATAL_CODES,
    QUOTA_CODES,
    REPEATED_FAILURES,
    ReviewCoordinator,
    features_kw as _features_kw,
)
from .services.translation_runner import (
    TRANSLATION_IDLE,
    TRANSLATION_PROBING,
    TRANSLATION_RESUMABLE,
    Translation,
    TranslationRunner,
    blank,
)

__all__ = [
    "DOCUMENT_REVIEW_LIMIT",
    "DictionaryIdentity",
    "DocumentRunController",
    "FATAL_CODES",
    "IDENTITY_FAILED",
    "IDENTITY_FRESH",
    "IDENTITY_SUPERSEDED",
    "IDENTITY_UNSUPPORTED",
    "QUOTA_CODES",
    "REPEATED_FAILURES",
    "TRANSLATION_IDLE",
    "TRANSLATION_PROBING",
    "TRANSLATION_RESUMABLE",
    "Translation",
    "TranslationResponse",
    "WorkbenchApiClient",
    "WorkbenchHost",
    "_features_kw",
    "blank",
]


class TranslationResponse(Protocol):
    """One finished translation, as far as a run reads it."""

    text: str


@runtime_checkable
class WorkbenchApiClient(Protocol):
    """The narrow client call surface this workbench makes, and nothing else."""

    async def get_capabilities(self) -> frozenset[str]: ...
    async def get_dictionary(self) -> "DictionaryIdentity | None": ...
    async def structure_check(self, body: dict[str, Any]) -> dict[str, Any]: ...
    async def review_pair(
        self,
        source: str,
        target: str,
        direction: str,
        *,
        alignments: list[dict[str, Any]] | None = None,
        resolutions: list[dict[str, Any]] | None = None,
        resolution_context: dict[str, str] | None = None,
        features: Sequence[str] = (),
    ) -> ReviewReport: ...
    async def semantic_review(
        self,
        source: str,
        target: str,
        direction: str,
        *,
        before: dict[str, str] | None = None,
        precedents: Sequence[Mapping[str, Any]] = (),
    ) -> semantic.SemanticReviewReport: ...
    async def translate(
        self,
        text: str,
        *,
        to: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TranslationResponse: ...


@runtime_checkable
class WorkbenchHost(Protocol):
    """What the Qt half provides for a run: rendering and the editor write."""

    def refresh_semantic(self) -> None: ...
    def refresh_segments(self) -> None: ...
    def refresh_segments_if_shown(self) -> None: ...
    def update_controls(self) -> None: ...
    def render_semantic_progress(self) -> None: ...
    def show_segment_term_report(self) -> None: ...
    def show_term_report(self, report: ReviewReport) -> None: ...
    def editor_state(self) -> dict[str, Any]: ...
    def render_review_state(self) -> None: ...
    def clear_report(self) -> None: ...
    def report_status(self, text: str) -> None: ...
    def report_unstorable_report(self, code: str) -> None: ...
    def report_translation_complete(self, mismatched: int) -> None: ...
    def trust_review_snapshot(self, snapshot: dict[str, Any]) -> None: ...
    def precedents_selected(self) -> bool: ...
    def selected_segment(self) -> Any: ...
    def render_structure(self) -> None: ...
    def apply_target_write(
        self,
        changed: str,
        edit: tuple[Any, ...],
        *,
        remap: AlignmentRemap | None = None,
    ) -> bool: ...


class DocumentRunController:
    """Facade composing the workbench's pure-Python document and execution services."""

    def __init__(
        self,
        api_client: WorkbenchApiClient,
        host: WorkbenchHost,
        clock: Callable[[], float] | None = None,
        translation_memory: tm.TranslationMemory | None = None,
    ) -> None:
        self.api_client = api_client
        self.host = host
        self.alignment = AlignmentState()
        self.provenance = DictionaryProvenanceService()
        self._session = sessions.Session()
        self.edits = DocumentEditStore(
            self.alignment,
            self._session,
            editor_state_provider=lambda: self.host.editor_state(),
            on_direction_changed=self._clear_semantic_if_active,
            clock=clock,
        )
        self.reviews = ReviewCoordinator(
            get_api_client=lambda: self.api_client,
            host=host,
            edits=self.edits,
            alignment=self.alignment,
            session=self._session,
            record_live_review_dictionary=self.record_live_review_dictionary,
        )
        self.translator = TranslationRunner(
            get_api_client=lambda: self.api_client,
            host=host,
            edits=self.edits,
            alignment=self.alignment,
            translation_memory=translation_memory,
            tm_provenance=self._tm_provenance,
            refresh_identity=self.refresh_live_dictionary_identity,
            set_capabilities=self._set_capabilities,
            structure_known_unavailable=self.structure_known_unavailable,
            start_structure_check=self.start_structure_check,
            note_structure_progress=self.note_structure_progress,
        )

    def _clear_semantic_if_active(self) -> bool:
        if self._session.results or self.reviews.semantic_running():
            self.reviews.clear_semantic()
            return True
        return False

    def _set_capabilities(self, capabilities: frozenset[str] | None) -> None:
        self.reviews.capabilities = capabilities

    # -- composed state properties (single source of truth in sub-services) -

    @property
    def payload(self) -> dict[str, Any]:
        return self.edits.payload

    @payload.setter
    def payload(self, value: dict[str, Any]) -> None:
        self.edits.payload = value

    @property
    def history(self):
        return self.edits.history

    @property
    def clock(self):
        return self.edits.clock

    @clock.setter
    def clock(self, value) -> None:
        self.edits.clock = value

    @property
    def alignment_invalidations(self) -> list[dict[str, Any]]:
        return self.edits.alignment_invalidations

    @alignment_invalidations.setter
    def alignment_invalidations(self, value: list[dict[str, Any]]) -> None:
        self.edits.alignment_invalidations = value

    @property
    def decision_cache(self):
        return self.edits.decision_cache

    @decision_cache.setter
    def decision_cache(self, value) -> None:
        self.edits.decision_cache = value

    @property
    def document_revision(self) -> int:
        return self.edits.document_revision

    @document_revision.setter
    def document_revision(self, value: int) -> None:
        self.edits.document_revision = value

    @property
    def records_seen(self):
        return self.edits.records_seen

    @records_seen.setter
    def records_seen(self, value) -> None:
        self.edits.records_seen = value

    @property
    def session_choices(self) -> list[dict[str, Any]]:
        return self.edits.session_choices

    @session_choices.setter
    def session_choices(self, value: list[dict[str, Any]]) -> None:
        self.edits.session_choices = value

    @property
    def trusted_snapshots(self) -> list[dict[str, Any]]:
        return self.edits.trusted_snapshots

    @trusted_snapshots.setter
    def trusted_snapshots(self, value: list[dict[str, Any]]) -> None:
        self.edits.trusted_snapshots = value

    @property
    def pairs_key(self):
        return self.alignment.pairs_key

    @pairs_key.setter
    def pairs_key(self, value) -> None:
        self.alignment.pairs_key = value

    @property
    def pairs(self) -> list[Any]:
        return self.alignment.pairs

    @pairs.setter
    def pairs(self, value: list[Any]) -> None:
        self.alignment.pairs = value

    @property
    def pairs_version(self) -> str:
        return self.alignment.pairs_version

    @pairs_version.setter
    def pairs_version(self, value: str) -> None:
        self.alignment.pairs_version = value

    @property
    def live_review_dictionary(self) -> dict[str, Any] | None:
        return self.provenance.live_review_dictionary

    @live_review_dictionary.setter
    def live_review_dictionary(self, value: dict[str, Any] | None) -> None:
        self.provenance.live_review_dictionary = value

    @property
    def live_dictionary_identity(self) -> DictionaryIdentity | None:
        return self.provenance.service_dictionary

    @live_dictionary_identity.setter
    def live_dictionary_identity(self, value: DictionaryIdentity | None) -> None:
        self.provenance.service_dictionary = value

    @property
    def identity_probe_state(self) -> str | None:
        return self.provenance.identity_probe_state

    @identity_probe_state.setter
    def identity_probe_state(self, value: str | None) -> None:
        self.provenance.identity_probe_state = value

    @property
    def generation(self) -> int:
        return self.reviews.generation

    @generation.setter
    def generation(self, value: int) -> None:
        self.reviews.generation = value

    @property
    def task(self) -> asyncio.Task | None:
        return self.reviews.task

    @task.setter
    def task(self, value: asyncio.Task | None) -> None:
        self.reviews.task = value

    @property
    def review_state(self) -> str:
        return self.reviews.review_state

    @review_state.setter
    def review_state(self, value: str) -> None:
        self.reviews.review_state = value

    @property
    def review_detail(self) -> str:
        return self.reviews.review_detail

    @review_detail.setter
    def review_detail(self, value: str) -> None:
        self.reviews.review_detail = value

    @property
    def request_id(self) -> str | None:
        return self.reviews.request_id

    @request_id.setter
    def request_id(self, value: str | None) -> None:
        self.reviews.request_id = value

    @property
    def capabilities(self) -> frozenset[str] | None:
        return self.reviews.capabilities

    @capabilities.setter
    def capabilities(self, value: frozenset[str] | None) -> None:
        self.reviews.capabilities = value

    @property
    def term_mode(self) -> str:
        return self.reviews.term_mode

    @term_mode.setter
    def term_mode(self, value: str) -> None:
        self.reviews.term_mode = value

    @property
    def session(self) -> sessions.Session:
        return self._session

    @property
    def semantic_generation(self) -> int:
        return self.reviews.semantic_generation

    @semantic_generation.setter
    def semantic_generation(self, value: int) -> None:
        self.reviews.semantic_generation = value

    @property
    def semantic_task(self) -> asyncio.Task | None:
        return self.reviews.semantic_task

    @semantic_task.setter
    def semantic_task(self, value: asyncio.Task | None) -> None:
        self.reviews.semantic_task = value

    @property
    def semantic_state(self) -> str:
        return self.reviews.semantic_state

    @semantic_state.setter
    def semantic_state(self, value: str) -> None:
        self.reviews.semantic_state = value

    @property
    def semantic_detail(self) -> str:
        return self.reviews.semantic_detail

    @semantic_detail.setter
    def semantic_detail(self, value: str) -> None:
        self.reviews.semantic_detail = value

    @property
    def segment_failures(self) -> dict[tuple[Any, ...], str]:
        return self.reviews.segment_failures

    @segment_failures.setter
    def segment_failures(self, value: dict[tuple[Any, ...], str]) -> None:
        self.reviews.segment_failures = value

    @property
    def segment_notes(self) -> dict[tuple[Any, ...], list[str]]:
        return self.reviews.segment_notes

    @segment_notes.setter
    def segment_notes(self, value: dict[tuple[Any, ...], list[str]]) -> None:
        self.reviews.segment_notes = value

    @property
    def known_texts(self) -> dict[str, str]:
        return self.reviews.known_texts

    @known_texts.setter
    def known_texts(self, value: dict[str, str]) -> None:
        self.reviews.known_texts = value

    @property
    def history_run(self) -> int:
        return self.reviews.history_run

    @history_run.setter
    def history_run(self, value: int) -> None:
        self.reviews.history_run = value

    @property
    def history_written(self) -> int:
        return self.reviews.history_written

    @history_written.setter
    def history_written(self, value: int) -> None:
        self.reviews.history_written = value

    @property
    def term_basis(self) -> dict[str, Any] | None:
        return self.reviews.term_basis

    @term_basis.setter
    def term_basis(self, value: dict[str, Any] | None) -> None:
        self.reviews.term_basis = value

    @property
    def record_views(self) -> sr.RecordViews:
        return self.reviews.record_views

    @property
    def structure(self) -> structure.State:
        return self.reviews.structure

    @structure.setter
    def structure(self, value: structure.State) -> None:
        self.reviews.structure = value

    @property
    def structure_generation(self) -> int:
        return self.reviews.structure_generation

    @structure_generation.setter
    def structure_generation(self, value: int) -> None:
        self.reviews.structure_generation = value

    @property
    def structure_task(self) -> asyncio.Task | None:
        return self.reviews.structure_task

    @structure_task.setter
    def structure_task(self, value: asyncio.Task | None) -> None:
        self.reviews.structure_task = value

    @property
    def translation_memory(self) -> tm.TranslationMemory | None:
        return self.translator.translation_memory

    @translation_memory.setter
    def translation_memory(self, value: tm.TranslationMemory | None) -> None:
        self.translator.translation_memory = value

    @property
    def last_tm_translation_summary(self) -> dict[str, Any] | None:
        return self.translator.last_tm_translation_summary

    @last_tm_translation_summary.setter
    def last_tm_translation_summary(self, value: dict[str, Any] | None) -> None:
        self.translator.last_tm_translation_summary = value

    @property
    def translate_generation(self) -> int:
        return self.translator.translate_generation

    @translate_generation.setter
    def translate_generation(self, value: int) -> None:
        self.translator.translate_generation = value

    @property
    def translate_task(self) -> asyncio.Task | None:
        return self.translator.translate_task

    @translate_task.setter
    def translate_task(self, value: asyncio.Task | None) -> None:
        self.translator.translate_task = value

    @property
    def provenance_task(self) -> asyncio.Task | None:
        return self.translator.provenance_task

    @provenance_task.setter
    def provenance_task(self, value: asyncio.Task | None) -> None:
        self.translator.provenance_task = value

    @property
    def translate_state(self) -> str:
        return self.translator.translate_state

    @translate_state.setter
    def translate_state(self, value: str) -> None:
        self.translator.translate_state = value

    @property
    def translate_run(self) -> Translation | None:
        return self.translator.translate_run

    @translate_run.setter
    def translate_run(self, value: Translation | None) -> None:
        self.translator.translate_run = value

    @property
    def translate_status(self) -> str:
        return self.translator.translate_status

    @translate_status.setter
    def translate_status(self, value: str) -> None:
        self.translator.translate_status = value

    @property
    def translation_withdrawn(self) -> bool:
        return self.translator.translation_withdrawn

    @translation_withdrawn.setter
    def translation_withdrawn(self, value: bool) -> None:
        self.translator.translation_withdrawn = value

    # -- alignment & document queries ---------------------------------------

    def decisions(self) -> list[dict[str, Any]]:
        return self.edits.decisions()

    def pairs_key_now(self) -> tuple[str, str, str]:
        return self.alignment.key_for(self.edits.payload)

    def current_pairs(self) -> list[Any]:
        return self.alignment.current_pairs(self.edits.payload)

    def cached_pairs(self) -> list[Any] | None:
        return self.alignment.cached_pairs(self.edits.payload)

    def segment_key(self, pair: Any) -> tuple[Any, ...]:
        return self.alignment.segment_key(pair, self.edits.payload)

    def long_now(self) -> bool:
        return self.alignment.long_now(self.edits.payload)

    def put_parts(self, parts: sr.Parts) -> None:
        self.edits.put_parts(parts)

    def records(self) -> list[dict[str, Any]] | None:
        return self.edits.records(self.reviews.term_basis)

    def basis(self) -> sessions.Basis:
        return self.reviews.basis()

    def semantic_available(self) -> bool:
        return self.reviews.semantic_available()

    def review_features(self) -> tuple[str, ...] | None:
        return self.reviews.review_features()

    def structure_known_unavailable(self) -> bool:
        return self.reviews.structure_known_unavailable()

    def alignments_for(self, source_core: tuple[int, int], target_core: tuple[int, int]) -> list[dict[str, Any]] | None:
        return self.alignment.alignments_for(self.edits.payload, source_core, target_core)

    def context_for(self, pair: Any) -> dict[str, str] | None:
        return self.alignment.context_for(self.edits.payload, pair)

    def texts_for_reconcile(self) -> dict[str, str]:
        return self.reviews.texts_for_reconcile()

    def semantic_texts(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.reviews.semantic_texts(payload)

    def reset_term_session(self) -> None:
        self.reviews.reset_term_session()

    def reset_known_texts(self, value: Mapping[str, Any] | None = None) -> None:
        self.reviews.reset_known_texts(value)

    # -- provenance delegation (single DictionaryProvenanceService) ---------

    def record_live_review_dictionary(self, dictionary: Mapping[str, Any] | None) -> None:
        recorder = getattr(self.host, "record_live_review_dictionary", None)
        if callable(recorder):
            recorder(dictionary)
            return
        self.provenance.record_live_review_dictionary(dictionary)

    def record_live_dictionary_identity(self, identity: DictionaryIdentity | None) -> None:
        self.provenance.service_dictionary = identity
        setter = getattr(self.host, "set_service_dictionary", None)
        if callable(setter):
            setter(identity)

    def _apply_dictionary_identity(self, identity: DictionaryIdentity | None, *, seq: int | None = None) -> bool:
        applier = getattr(self.host, "apply_dictionary_identity", None)
        if callable(applier):
            if applier(identity, seq=seq) is False:
                return False
            self.provenance.service_dictionary = identity
            return True
        return self.provenance.apply_identity(identity, seq=seq)

    def _begin_identity_request(self) -> int | None:
        beginner = getattr(self.host, "begin_dictionary_identity_request", None)
        if callable(beginner):
            return beginner()
        return self.provenance.begin_identity_request()

    def clear_live_provenance(self, *, seq: int | None = None) -> bool:
        clearer = getattr(self.host, "clear_live_provenance", None)
        if callable(clearer):
            applied = clearer() if seq is None else clearer(seq=seq)
            if applied is False:
                return False
            self.provenance.live_review_dictionary = None
            self.provenance.service_dictionary = None
            return True
        return self.provenance.clear_live_provenance(seq=seq)

    async def refresh_live_dictionary_identity(self) -> DictionaryIdentity | None:
        return await self.provenance.refresh_live_dictionary_identity(
            self.api_client,
            is_configured=self.translator.client_configured(),
            on_capabilities=self._set_capabilities,
            begin_request=self._begin_identity_request,
            apply_identity=self._apply_dictionary_identity,
            clear_provenance=self.clear_live_provenance,
        )

    def invalidate_provenance_for_credential_change(self) -> None:
        self.provenance.reset_for_credential_change()
        self.translator.cancel_provenance_probe_for_credential_change()
        self.cancel_pending()
        self.stop_semantic("paused" if self.semantic_running() else None)

    def _tm_provenance(self) -> tuple[str | None, str | None]:
        getter = getattr(self.host, "tm_provenance", None)
        if callable(getter):
            return getter()
        return self.provenance.tm_provenance()

    # -- edit store delegation ----------------------------------------------

    def draft_snapshot(self) -> dict[str, Any]:
        return self.edits.draft_snapshot()

    def restore_draft(self, value: dict[str, Any]) -> None:
        self.edits.restore_draft(value)

    def decision_index(self) -> sessions.DecisionIndex:
        return self.edits.decision_index()

    def issue_state(self, issue: sessions.LiveIssue) -> str:
        return self.reviews.issue_state(issue)

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
        return self.edits.apply_edit(
            value, view_state=view_state, edits=edits, exempt=exempt, remap=remap, alignment_reset=alignment_reset
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
        return self.edits.edit_text(
            side, text, edit=edit, composing=composing, view_state=view_state, exempt=exempt
        )

    def set_direction(self, direction: str, *, view_state: dict[str, Any] | None = None) -> EditOutcome:
        return self.edits.set_direction(direction, view_state=view_state)

    def note_edit(self, edit: tuple[Any, ...]) -> None:
        self.edits.note_edit(edit)

    def apply_parts_edit(self, parts: sr.Parts, *, view_state: dict[str, Any] | None = None) -> CommandOutcome:
        return self.edits.apply_parts_edit(parts, view_state=view_state)

    def can_undo(self) -> bool:
        return self.edits.can_undo()

    def can_redo(self) -> bool:
        return self.edits.can_redo()

    def clear_history(self) -> None:
        self.edits.clear_history()
        self.translator.translation_withdrawn = False

    def restore_invalidations(self, items: Sequence[Mapping[str, Any]]) -> None:
        self.edits.restore_invalidations(items)

    def undo(self, *, view_state: dict[str, Any] | None = None) -> HistoryOutcome | None:
        return self.edits.step_history(undo=True, view_state=view_state)

    def redo(self, *, view_state: dict[str, Any] | None = None) -> HistoryOutcome | None:
        return self.edits.step_history(undo=False, view_state=view_state)

    def reconfirmable(self, index: int, choice: dict[str, Any]) -> bool:
        return self.edits.reconfirmable(index, choice, self.reviews.term_basis)

    def choose_term(
        self, index: int, start: int, end: int, choice: str, candidate_id: str | None = None
    ) -> CommandOutcome:
        return self.edits.choose_term(index, start, end, choice, candidate_id)

    def unchoose_term(self, index: int, start: int, end: int) -> CommandOutcome:
        return self.edits.unchoose_term(index, start, end)

    def restore_choice(self, position: int, index: int) -> CommandOutcome:
        return self.edits.restore_choice(position, index)

    def confirm_choice(self, index: int, choice: dict[str, Any]) -> CommandOutcome:
        return self.edits.confirm_choice(index, choice, self.reviews.term_basis)

    def set_finished(self, index: int, finished: bool) -> CommandOutcome:
        return self.edits.set_finished(index, finished)

    def remove_history_choice(self, position: int) -> CommandOutcome:
        return self.edits.remove_history_choice(position)

    def latest_trusted(self) -> dict[str, Any] | None:
        return self.edits.latest_trusted()

    def document_choice_at(self, finding: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.edits.document_choice_at(finding)

    def is_session_choice(self, choice: Mapping[str, Any] | None) -> bool:
        return self.edits.is_session_choice(choice)

    def choose_document(
        self, finding: Mapping[str, Any], candidate: Mapping[str, Any] | None, *, view_state: dict[str, Any] | None = None
    ) -> CommandOutcome:
        return self.edits.choose_document(finding, candidate, view_state=view_state)

    def document_resolutions(self) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
        return self.edits.document_resolutions()

    def remove_choice(self, index: int) -> CommandOutcome:
        return self.edits.remove_choice(index)

    def resolve_conflicting_choices_for_save(self) -> CommandOutcome:
        return self.edits.resolve_conflicting_choices_for_save()

    # -- semantic decisions & review delegation -----------------------------

    def basis_holds(self, issue: sessions.LiveIssue) -> bool:
        return self.reviews.basis_holds(issue)

    def can_accept(self, issue: sessions.LiveIssue | None) -> bool:
        return self.reviews.can_accept(issue)

    def can_adjust(self, issue: sessions.LiveIssue | None) -> bool:
        return self.reviews.can_adjust(issue)

    def can_ignore(self, issue: sessions.LiveIssue | None) -> bool:
        return self.reviews.can_ignore(issue)

    @staticmethod
    def selected_option(issue: sessions.LiveIssue, alternative: bool = False) -> tuple[str, str] | None:
        return ReviewCoordinator.selected_option(issue, alternative)

    def record_decision(
        self,
        issue: sessions.LiveIssue,
        decision: str,
        *,
        applied_text: str | None = None,
        target: str | None = None,
        replaced: dict[str, Any] | None = None,
    ) -> CommandOutcome:
        return self.reviews.record_decision(
            issue, decision, applied_text=applied_text, target=target, replaced=replaced
        )

    def accept_issue(
        self, issue: sessions.LiveIssue | None, *, can_display: Callable[[str], bool], alternative: bool = False
    ) -> CommandOutcome:
        return self.reviews.accept_issue(issue, can_display=can_display, alternative=alternative)

    def adjust_issue(
        self, issue: sessions.LiveIssue | None, text: str, *, can_display: Callable[[str], bool]
    ) -> CommandOutcome:
        return self.reviews.adjust_issue(issue, text, can_display=can_display)

    def ignore_issue(self, issue: sessions.LiveIssue | None) -> CommandOutcome:
        return self.reviews.ignore_issue(issue)

    def set_review_state(self, state: str, *, detail: str = "", request_id: str | None = None) -> None:
        self.reviews.set_review_state(state, detail=detail, request_id=request_id)

    def cancel_pending(self) -> None:
        self.reviews.cancel_pending()

    def start_review(self) -> None:
        self.reviews.start_review()

    async def run_review(
        self, generation: int, source: str, target: str, direction: str, alignments: list[dict[str, Any]] | None
    ) -> None:
        await self.reviews.run_review(generation, source, target, direction, alignments)

    async def run_document_review(
        self, generation: int, source: str, target: str, direction: str, alignments: list[dict[str, Any]] | None
    ) -> bool:
        return await self.reviews.run_document_review(generation, source, target, direction, alignments)

    def task_finished(self, task: asyncio.Task, generation: int) -> None:
        self.reviews.task_finished(task, generation)

    def cancel_review(self) -> None:
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

    def structure_running(self) -> bool:
        return self.reviews.structure_running()

    def structure_plan(self) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
        return self.reviews.structure_plan()

    def structure_views(self, pairs: Sequence[Any] | None = None) -> list[structure.View]:
        return self.reviews.structure_views(pairs)

    def cancel_structure_check(self) -> None:
        self.reviews.cancel_structure_check()

    def note_structure_progress(self, text: str) -> None:
        self.reviews.note_structure_progress(text)

    def start_structure_check(self, *, restart: bool = False) -> bool:
        return self.reviews.start_structure_check(restart=restart)

    async def run_structure_check(self, generation: int) -> None:
        await self.reviews.run_structure_check(generation)

    def semantic_running(self) -> bool:
        return self.reviews.semantic_running()

    def start_semantic_run(self) -> None:
        self.reviews.start_semantic_run()

    def stop_semantic(self, state: str | None = None) -> None:
        self.reviews.stop_semantic(state)

    def clear_semantic(self) -> None:
        self.reviews.clear_semantic()

    def semantic_finished(self, task: asyncio.Task, generation: int) -> None:
        self.reviews.semantic_finished(task, generation)

    def next_review(self, attempted: set[Any]) -> sr.NextReview | None:
        return self.reviews.next_review(attempted)

    def ready_pair_for(self, result: sessions.SegmentResult) -> Any:
        return self.reviews.ready_pair_for(result)

    def rebuild_plan(self) -> None:
        self.reviews.rebuild_plan()

    async def check_term(self, index: int, generation: int) -> Any:
        return await self.reviews.check_term(index, generation)

    async def run_segments(self, generation: int) -> None:
        await self.reviews.run_segments(generation)

    def store_semantic(
        self, pair: Any, pending: sessions.SegmentResult, report: semantic.SemanticReviewReport, direction: str
    ) -> None:
        self.reviews.store_semantic(pair, pending, report, direction)

    def remember_semantic_report(self, direction: str) -> None:
        self.reviews.remember_semantic_report(direction)

    # -- translation runner delegation --------------------------------------

    def translating(self) -> bool:
        return self.translator.translating()

    def stop_translation(self) -> None:
        self.translator.stop_translation()

    def resumable(self) -> Translation | None:
        return self.translator.resumable()

    def resumable_partial_prefix(self) -> tuple[int, str, int, str, tuple[str, str]] | None:
        return self.translator.resumable_partial_prefix()

    def has_resumable_remainder(self) -> bool:
        return self.translator.has_resumable_remainder()

    def withdraw_translation_offer(self) -> str | None:
        return self.translator.withdraw_translation_offer()

    def _resume_for_press(self) -> tuple[Translation | None, Any, str | None]:
        return self.translator._resume_for_press()

    def _needs_dictionary_identity(self, *, single_segment: bool) -> bool:
        return self.translator._needs_dictionary_identity(single_segment=single_segment)

    def start_translation(self, *, single_segment: bool = False) -> None:
        self.translator.start_translation(single_segment=single_segment)

    def retranslate_segment(self, pair: Any = None) -> None:
        self.translator.retranslate_segment(pair)

    async def run_batch_untranslated(self, run: Translation) -> None:
        await self.translator.run_batch_untranslated(run)

    def translation_done(self, run: Translation) -> None:
        self.translator.translation_done(run)

    async def run_translation(self, run: Translation, pair: Any, text: str, pieces: list[Any]) -> None:
        await self.translator.run_translation(run, pair, text, pieces)

    def translation_basis_unchanged(self, run: Translation) -> bool:
        return self.translator.translation_basis_unchanged(run)

    def write_translation(self, run: Translation, added: str, pair: Any = None) -> bool:
        return self.translator.write_translation(run, added, pair)

    def translation_stopped(
        self, run: Translation | None, state: str, reason: str = "", *, quota: bool = False
    ) -> None:
        self.translator.translation_stopped(run, state, reason, quota=quota)
