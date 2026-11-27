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
  one undoable edit;
* the Layer 1 structure check (``start_structure_check``/``run_structure_check``,
  Core ``POST /v1/structure-checks``): deterministic, user-triggered, and kept
  in ``structure`` only - never in the payload, an undo step, a recovery draft
  or an export.

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

from . import markdown_report as mdr
from . import segment_records as sr, semantic, strings
from . import semantic_session as sessions
from . import structure
from . import translation_memory as tm
from .document_edits import (
    MAX_SPAN_SCALARS, TYPING_COALESCE_MS, AlignmentRemap, HistoryEntry, HistoryOutcome, CommandOutcome, EditHistory,
    EditOutcome, PART_KEYS, REASON_FAILED, REASON_FULL, REASON_NOT_APPLICABLE, REASON_RECONFIRM, REASON_REJECTED,
    REASON_STALE, REVIEW_KEYS, append_invalidations, build_target_fill_transaction, compute_minimal_splice,
    copy_payload, draft_of, merge_draft, narrow_splice, normalize_semantic, remap_for_edit, remap_for_free_edit,
    remap_for_target_fill, remap_splice, restore_draft_into, valid_invalidation,
)
from .errors import (
    ERROR_CANCELLED, ERROR_FORBIDDEN, ERROR_INSECURE_ENDPOINT, ERROR_INVALID_REQUEST,
    ERROR_LLM_BUDGET_EXHAUSTED, ERROR_LLM_UNAVAILABLE, ERROR_NOT_CONFIGURED, ERROR_OFFLINE,
    ERROR_RATE_LIMITED, ERROR_UNAUTHORIZED, ClientError,
)
from .manuscript import (
    MAX_HISTORY_SEGMENTS, MAX_SEMANTIC_DECISIONS, MAX_SEMANTIC_HISTORY, MAX_V4_CHOICES, ReviewReport, WorkfileError,
    empty_workfile, recover_span, sanitize_duplicate_choices, semantic_payload_fits, serialize_workfile,
)
from .segmentation import (
    SEGMENT_LIMIT, SEGMENTATION_VERSION, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY, STATUS_UNTRANSLATED, WHITESPACE,
    _core as seg_core, _paragraph_ranges as seg_paragraph_ranges, pair_is_sendable, pair_segments,
    pair_segments_with_gaps, segment_text,
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
# The translation run's phases: idle, the wait for the served dictionary's
# identity before any reuse decision, and the four the view renders apart.
TRANSLATION_IDLE = "idle"
TRANSLATION_PROBING = "probing"
# What a dictionary-identity probe established, for the run that asked it. The
# three answers are different decisions, not different messages: a fresh answer
# is used, an unsupported endpoint falls back to `/v1/meta` + `/v1/reviews`, and
# a failed request may not fall back to anything.
IDENTITY_FRESH = "fresh"
IDENTITY_UNSUPPORTED = "unsupported"
IDENTITY_FAILED = "failed"

# The draft primitives (``copy_payload``, ``normalize_semantic``, ``PART_KEYS``)
# live in ``document_edits``: the runs and the commands commit through them,
# and they are imported here so this module's surface does not move with them.


def _features_kw(features: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """``review_pair``'s ``features`` argument, only when there are features."""
    return {"features": tuple(features)} if features else {}


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
    tm_reused: int = 0
    tm_revision: str | None = None
    tm_game_version: str | None = None
    tm_model_calls: int = 0
    tm_baseline_requests: int = 0


class TranslationResponse(Protocol):
    """One finished translation, as far as a run reads it.

    ``api.TranslationResult`` satisfies it. Naming only the field a run uses is
    what keeps this module (and its tests) importable with no HTTP library: the
    real result is built, and validated, by the client.
    """

    text: str


class DictionaryIdentity(Protocol):
    """One ``GET /v1/dictionary`` answer, as far as a run reads it.

    ``api.DictionaryIdentityResult`` satisfies it, and that class - not this
    one - validates the body. The two fields below are the ones provenance
    needs: the revision the memory compares entries against, and the game
    version the entry was confirmed under. Declared here for the same reason as
    ``TranslationResponse``: the controller takes no HTTP dependency.
    """

    revision: str
    source_game_version: str | None


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

    async def get_dictionary(self) -> "DictionaryIdentity | None":
        """The served dictionary's authoritative identity (``dictionary-identity-v1``),
        or None when the server does not serve ``GET /v1/dictionary``."""

    async def structure_check(self, body: dict[str, Any]) -> dict[str, Any]:
        """One Layer 1 batch (``structure-check-v1``), checked for exactly these segments."""

    async def review_pair(
        self, source: str, target: str, direction: str, *,
        alignments: list[dict[str, Any]] | None = None,
        resolutions: list[dict[str, Any]] | None = None,
        resolution_context: dict[str, str] | None = None,
        features: Sequence[str] = (),
    ) -> ReviewReport:
        """One term check (``review-v2``): the report for exactly these sides.
        ``features``: the recorded review features to ask for (sent only when
        there are any, so a stand-in without them keeps working)."""

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

    def render_structure(self) -> None:
        """Show the Layer 1 state (``DocumentRunController.structure``)."""

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
                 clock: Callable[[], float] | None = None,
                 translation_memory: tm.TranslationMemory | None = None) -> None:
        self.api_client = api_client
        self.host = host
        self.translation_memory = translation_memory
        self.last_tm_translation_summary: dict[str, Any] | None = None
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
        # Most recent live /v1/reviews dictionary (document or segment mode)
        # observed in this session; cleared when endpoint/credential resets.
        self.live_review_dictionary: dict[str, Any] | None = None
        # Most recent live GET /v1/dictionary identity observed in this session
        # (``dictionary-identity-v1``): the authoritative revision, which needs
        # no review call to exist. Cleared with the same resets.
        self.live_dictionary_identity: DictionaryIdentity | None = None
        # What the last probe established: IDENTITY_FRESH, IDENTITY_UNSUPPORTED,
        # IDENTITY_FAILED, or None when nothing was asked.
        self.identity_probe_state: str | None = None
        # What the records were last normalized against (identity checks only).
        self.records_seen: tuple | None = None
        self.record_views = sr.RecordViews()
        # The pairing of the current text, cached by the text it was made from.
        self.pairs_key: tuple | None = None
        self.pairs: list = []
        # The pairing label of ``pairs``: segmentation-v1, or segmentation-v1-gaps
        # when explicit alignments leave text uncovered (manuscript v5).
        self.pairs_version = SEGMENTATION_VERSION
        # Layer 1 structure checks: session state only, never saved anywhere.
        self.structure = structure.State()
        self.structure_generation = 0
        self.structure_task: asyncio.Task | None = None
        self._structure_progress_status: str | None = None
        # Document-mode choices made in this session (by identity): only these
        # go out as resolutions; a file's choices stay on file (Windows short
        # manuscripts never submit them).
        self.session_choices: list[dict] = []
        # Document reports checked in this session (by equality): only these
        # can be the basis of a document choice or of the resolutions it sends.
        self.trusted_snapshots: list[dict] = []
        # Counts every payload change the document's title and dirty flag see.
        self.document_revision = 0
        # Translation of the target side.
        self.translate_generation = 0
        self.translate_task: asyncio.Task | None = None
        # The dictionary-identity request a 翻译 press is waiting on, while it
        # is the only thing in flight (``translate_task`` points at it then).
        self.provenance_task: asyncio.Task | None = None
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
                # Explicit alignments that leave text uncovered keep every
                # aligned region; only the gap needs a manual split (MY-496).
                pairs, version = pair_segments_with_gaps(source, target, SEGMENT_LIMIT, alignments)
            except (ValueError, TypeError, AssertionError):
                # Alignments the rule cannot use fall back to automatic pairing.
                pairs, version = pair_segments(source, target, SEGMENT_LIMIT, None), SEGMENTATION_VERSION
            self.pairs_key, self.pairs, self.pairs_version = key, pairs, version
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
            self.put_parts(sr.normalize(payload, pairs, self.term_basis, check_limits=False,
                                        version=self.pairs_version if long else SEGMENTATION_VERSION))
            self.records_seen = (*now(), payload["direction"])
        return payload.get("segments") or [] if long else None

    def basis(self) -> sessions.Basis:
        """The current basis of the semantic reuse rule (``sessions.Basis``)."""
        return sessions.Basis(semantic_required=self.semantic_available())

    def semantic_available(self) -> bool:
        return self.capabilities is not None and semantic.CAPABILITY_SEMANTIC_REVIEW in self.capabilities

    def review_features(self) -> tuple[str, ...] | None:
        """The review features this session asks Core for (manuscript-v5 SPEC
        "Review features"): ``ambiguous-mention-v1`` when the server lists it,
        ``None`` while the capabilities are unknown. A saved result is current
        only under the features it was made with."""
        return sr.session_review_features(self.capabilities)

    def structure_known_unavailable(self) -> bool:
        """The server answered its capabilities without structure-check-v1."""
        return self.capabilities is not None and structure.CAPABILITY_STRUCTURE_CHECK not in self.capabilities

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
        """A new document or service: no term basis yet, so nothing saved is current.
        Layer 1 results and this session's document choices belong to it too."""
        self.term_basis = None
        self.live_review_dictionary = None
        self.records_seen = None
        self.pairs_key, self.pairs, self.pairs_version = None, [], SEGMENTATION_VERSION
        self.cancel_structure_check()
        self.structure = structure.State()
        self.session_choices = []
        self.trusted_snapshots = []

    def record_live_review_dictionary(self, dictionary: Mapping[str, Any] | None) -> None:
        """Record the newest live ``/v1/reviews`` dictionary from this session."""
        if isinstance(dictionary, Mapping):
            self.live_review_dictionary = copy.deepcopy(dict(dictionary))
        recorder = getattr(self.host, "record_live_review_dictionary", None)
        if callable(recorder):
            recorder(dictionary)

    def record_live_dictionary_identity(self, identity: DictionaryIdentity | None) -> None:
        """Record the live ``GET /v1/dictionary`` identity, and hand it to the host.

        Two holders on purpose. The host (the workbench view) is the one that
        answers ``tm_provenance()``, so it is where a confirmed entry's
        provenance comes from; this attribute is the same fact for a host that
        has no such view, and it is what the next run reads when the view is
        gone (the controller-only runs in the tests).
        """
        self.live_dictionary_identity = identity
        setter = getattr(self.host, "set_service_dictionary", None)
        if callable(setter):
            setter(identity)

    async def refresh_live_dictionary_identity(self) -> DictionaryIdentity | None:
        """Ask the service for its authoritative dictionary identity, once.

        Only when it can answer: an unconfigured client sends nothing, and a
        server that does not list ``dictionary-identity-v1`` (or that predates
        the endpoint, answering 404) leaves the older `/v1/meta` + `/v1/reviews`
        path in charge, which ADR 0023 requires.

        Never raises, and records what it learned in ``identity_probe_state``
        because the three answers are three different decisions:

        * ``IDENTITY_FRESH`` - the answer is read and recorded; the run reuses
          against the revision it states.
        * ``IDENTITY_UNSUPPORTED`` - the endpoint is not there. The fallback is
          allowed; that is a server old enough to have no identity.
        * ``IDENTITY_FAILED`` - it was asked and could not answer (503, a
          timeout, a broken body). Nothing this session already believed about
          the served dictionary may stand in for it, so both live statements are
          cleared and this run reuses nothing. An older review basis is exactly
          the value that might describe a dictionary the service replaced.
        """
        self.identity_probe_state = None
        if not self._client_configured():
            return None
        client = self.api_client
        getter = getattr(client, "get_dictionary", None)
        try:
            if self.capabilities is None:
                self.capabilities = await client.get_capabilities()
            if semantic.CAPABILITY_DICTIONARY_IDENTITY not in self.capabilities:
                self.identity_probe_state = IDENTITY_UNSUPPORTED
                return None
            if not callable(getter):
                self.identity_probe_state = IDENTITY_UNSUPPORTED
                return None
            identity = await getter()
        except Exception:  # noqa: BLE001 - see the docstring: fail closed, never raise
            self.identity_probe_state = IDENTITY_FAILED
            self.clear_live_provenance()
            return None
        if identity is None:
            self.identity_probe_state = IDENTITY_UNSUPPORTED
            return None
        self.record_live_dictionary_identity(identity)
        self.identity_probe_state = IDENTITY_FRESH
        return identity

    def clear_live_provenance(self) -> None:
        """Forget both live statements about the served dictionary.

        Called when the identity request could not be read: the run that asked
        cannot decide on a revision it could not verify, and the review basis is
        not a substitute for the answer that did not arrive - it may describe a
        dictionary the service has since replaced.
        """
        self.live_review_dictionary = None
        self.live_dictionary_identity = None
        clearer = getattr(self.host, "clear_live_provenance", None)
        if callable(clearer):
            clearer()
            return
        identity_setter = getattr(self.host, "set_service_dictionary", None)
        if callable(identity_setter):
            identity_setter(None)

    def invalidate_provenance_for_credential_change(self) -> None:
        """Clear live review and dictionary provenance and advance both review
        generations so in-flight requests issued under an older token cannot
        write back."""
        self.live_review_dictionary = None
        self.live_dictionary_identity = None
        # A 翻译 press waiting on the identity request was issued under the old
        # credential. Dropped before its answer lands: otherwise the reply
        # restores the provenance just cleared and starts the run anyway.
        probe, self.provenance_task = self.provenance_task, None
        if probe is not None and not probe.done():
            self.translate_generation += 1
            if self.translate_task is probe:
                self.translate_task = None
            self.translate_run = None
            self.translate_state = TRANSLATION_IDLE
            probe.cancel()
        self.cancel_pending()
        self.stop_semantic("paused" if self.semantic_running() else None)

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

    # Document-mode choices: only an ambiguous one-character name
    # (``ambiguous_mention``) is resolved here. The choice is staged in the
    # manuscript and goes out as a resolution with the next document check;
    # choices read from a file are never sent (``markdown_report``'s
    # ``desktop_not_submitted``).

    def latest_trusted(self) -> dict | None:
        """The newest document report checked in this session for the text as it
        is now: the basis a document choice is made against."""
        history = self.payload["history"]
        latest = history[-1] if history else None
        if (latest is None or latest not in self.trusted_snapshots
                or latest["source"] != self.payload["source"] or latest["direction"] != self.payload["direction"]):
            return None
        return latest

    def document_choice_at(self, finding: Mapping[str, Any]) -> dict | None:
        """The manuscript's document choice whose span now reads this finding's place."""
        span = finding["source_span"]
        for choice in self.payload["choices"]:
            recovered = recover_span(choice["source"], choice["source_span"], self.payload["source"])
            if recovered is not None and sr.mention_id(recovered) == sr.mention_id(span):
                return choice
        return None

    def is_session_choice(self, choice: Mapping[str, Any] | None) -> bool:
        return choice is not None and choice in self.session_choices

    def choose_document(self, finding: Mapping[str, Any], candidate: Mapping[str, Any] | None, *,
                        view_state: dict | None = None) -> CommandOutcome:
        """Adopt ``candidate`` (an official pair) or mark the place not a term,
        at a finding of the latest report checked in this session (Web
        ``choose``). It replaces any choice at the same place, is one undoable
        edit, and goes out as a resolution with the next document check. A
        short manuscript holds up to 128 choices, as v4/v5 files and Core's
        ``resolutions`` do."""
        latest = self.latest_trusted()
        if latest is None or latest["report"]["truncated"]:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        span = recover_span(latest["source"], finding["source_span"], self.payload["source"])
        if span is None or (candidate is not None and not any(
                c["candidate_id"] == candidate["candidate_id"] for c in finding["candidates"])):
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        report = latest["report"]
        made = {
            "source": self.payload["source"], "direction": self.payload["direction"], "source_span": dict(span),
            "scope": mdr.scope_of(self.payload["alignments"], span),
            "choice": sr.OFFICIAL_PAIR if candidate is not None else sr.NOT_A_TERM,
            "candidate": None if candidate is None else {key: candidate[key] for key in (
                "candidate_id", "zh", "en", "category")},
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

    def document_resolutions(self) -> tuple[list[dict], dict | None]:
        """The resolutions and their context the next document check sends: this
        session's choices that still apply to the latest trusted report (Web
        ``reconcileChoices``), nothing else."""
        latest = self.latest_trusted()
        own = [c for c in self.payload["choices"] if self.is_session_choice(c)]
        if latest is None or not own:
            return [], None
        report = latest["report"]
        reconciled = mdr.reconcile_choices(own, self.payload, {**latest, "trusted": True},
                                           imported=False, submits=True)
        resolutions = []
        for choice, item in zip(own, reconciled, strict=True):
            if item["status"] != "applicable":
                continue
            resolution = {"mention_id": sr.mention_id(item["span"]), "choice": choice["choice"]}
            if choice["choice"] == sr.OFFICIAL_PAIR:
                resolution["candidate_id"] = choice["candidate"]["candidate_id"]
            resolutions.append(resolution)
        if not resolutions or "matcher_revision" not in report or "revision" not in report["dictionary"]:
            return [], None
        return resolutions, {"source_revision": report["source_revision"], "rule_version": report["rule_version"],
                             "dictionary_revision": report["dictionary"]["revision"],
                             "matcher_revision": report["matcher_revision"]}

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
        features = tuple(self.review_features() or ())
        resolutions, context = self.document_resolutions()
        while True:
            try:
                report = await self.api_client.review_pair(
                    source, target, direction, alignments=alignments, resolutions=resolutions or None,
                    resolution_context=context if resolutions else None, **_features_kw(features))
            except ClientError as exc:
                if (exc.code == ERROR_INVALID_REQUEST and resolutions and generation == self.generation):
                    # Core no longer reports a finding a resolution names (another
                    # service, or one without ambiguous-mention-v1): check once more
                    # without them; the choices stay and wait for a matching report.
                    resolutions, context = [], None
                    continue
                if generation == self.generation:
                    self.set_review_state("error", detail=exc.message, request_id=exc.request_id)
                return False
            except asyncio.CancelledError:
                if generation == self.generation:
                    self.set_review_state("cancelled")
                return False
            break
        if generation != self.generation:
            return False
        self.record_live_review_dictionary(report.get("dictionary") if isinstance(report, Mapping) else None)
        # The resolutions this check sent: only this session's own choices,
        # and [] (not unknown) lets the next check compare with it, as on the Web.
        snapshot = {"source": source, "target": target, "direction": direction,
                    "alignments": alignments, "report": report, "resolutions": list(resolutions)}
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
        self.trusted_snapshots.append(copy.deepcopy(snapshot))
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

    # -- Layer 1: deterministic structure checks ------------------------------
    # User-triggered, deterministic and never saved: results live in
    # ``self.structure`` only, keyed by the hashes of the cores they were made
    # for, so an edit hides a segment's result at once and nothing reaches the
    # payload, an undo step, the recovery draft or an export.

    def structure_running(self) -> bool:
        return self.structure_task is not None and not self.structure_task.done()

    def structure_plan(self) -> tuple[list, list[dict], list[str]]:
        """The items of the current pairing, their batches and the skipped ids."""
        return structure.plan(self.current_pairs(), self.payload["source"], self.payload["target"])

    def structure_views(self, pairs: Sequence | None = None) -> list[structure.View]:
        pairs = self.current_pairs() if pairs is None else pairs
        return structure.views(pairs, self.payload["source"], self.payload["target"], self.structure)

    def cancel_structure_check(self) -> None:
        self.structure_generation += 1
        task, self.structure_task = self.structure_task, None
        self._structure_progress_status = None
        if task is not None and not task.done():
            task.cancel()
        if self.structure.phase == "running":
            self.structure.phase = "idle"

    def note_structure_progress(self, text: str) -> None:
        """Remember the active in-progress status message sent for the current L1 run."""
        self._structure_progress_status = text

    def _abort_stale_structure_check(self) -> str | None:
        """Reset stale L1 state and clear its progress message only if status is still that message."""
        self.structure.phase = "idle"
        self.structure.skipped = 0
        self.structure.error = None
        reader = getattr(self.host, "current_status", None)
        current = reader() if callable(reader) else None
        if not current:
            return None
        if (
            current == self._structure_progress_status
            or current == strings.MANUSCRIPT_L1_RUNNING
            or strings.MANUSCRIPT_TRANSLATE_TM_L1_RUNNING in current
            or "正在自动运行第一层结构核查" in current
        ):
            return strings.MANUSCRIPT_L1_NOT_RUN
        return None

    def start_structure_check(self, *, restart: bool = False) -> bool:
        """Check every checkable segment of the text as it is now.

        When ``restart`` is True, any in-flight check against older text is
        cancelled and a fresh check runs on the latest manuscript revision.
        """
        if self.structure_known_unavailable() or (self.structure_running() and not restart):
            return False
        self.cancel_structure_check()
        generation = self.structure_generation
        self.structure.phase, self.structure.error = "running", None
        self.structure_task = asyncio.ensure_future(self.run_structure_check(generation))
        self.host.render_structure()
        return True

    def _structure_snapshot(self) -> tuple[str, str, str, str, int]:
        """Snapshot of manuscript content used to detect edits or undo mid-L1."""
        return (
            self.payload["source"],
            self.payload["target"],
            self.payload["direction"],
            repr(self.payload["alignments"]),
            self.document_revision,
        )

    async def run_structure_check(self, generation: int) -> None:
        status_note: str | None = None
        snapshot = self._structure_snapshot()
        try:
            if self.capabilities is None:
                # Asked for now, when the owner pressed the button; never at startup.
                self.capabilities = await self.api_client.get_capabilities()
                if generation != self.structure_generation:
                    return
                if self._structure_snapshot() != snapshot:
                    status_note = self._abort_stale_structure_check()
                    return
                self.host.update_controls()
            if structure.CAPABILITY_STRUCTURE_CHECK not in self.capabilities:
                self.structure.phase = "failed"
                self.structure.error = (strings.MANUSCRIPT_L1_UNAVAILABLE, None)
                status_note = strings.MANUSCRIPT_L1_UNAVAILABLE
                return
            items, batches, skipped = self.structure_plan()
            keys = {item.segment_id: item.key for item in items}
            self.structure.skipped = len(skipped)
            for body in batches:
                response = await self.api_client.structure_check(body)
                if generation != self.structure_generation:
                    return
                if self._structure_snapshot() != snapshot:
                    status_note = self._abort_stale_structure_check()
                    return
                # Keyed by content: a result whose texts were edited meanwhile is
                # stored under the old hashes and so never shown for the new text.
                structure.store(self.structure, body, response, keys)
                self.host.render_structure()
            if generation == self.structure_generation:
                if self._structure_snapshot() != snapshot:
                    status_note = self._abort_stale_structure_check()
                    return
                summary = structure.summary(self.structure_views())
                if not items:
                    self.structure.phase = "done"
                    status_note = strings.MANUSCRIPT_L1_NONE
                elif summary.checked == 0:
                    status_note = self._abort_stale_structure_check()
                elif summary.findings + summary.omitted == 0:
                    self.structure.phase = "done"
                    status_note = strings.MANUSCRIPT_L1_CLEAN.format(checked=summary.checked)
                else:
                    self.structure.phase = "done"
                    status_note = strings.MANUSCRIPT_L1_SUMMARY.format(
                        checked=summary.checked,
                        segments=summary.with_findings,
                        count=summary.findings + summary.omitted,
                    )
        except ClientError as exc:
            if generation == self.structure_generation:
                if self._structure_snapshot() != snapshot:
                    status_note = self._abort_stale_structure_check()
                else:
                    self.structure.phase = "failed"
                    self.structure.error = (exc.message, exc.request_id)
                    status_note = strings.MANUSCRIPT_L1_FAILED.format(reason=exc.message)
        except asyncio.CancelledError:
            if generation == self.structure_generation:
                self.structure.phase = "idle"
        finally:
            if generation == self.structure_generation:
                self.structure_task = None
                self._structure_progress_status = None
                self.host.render_structure()
                if status_note is not None:
                    mismatched = sum(1 for p in self.current_pairs() if p.status == STATUS_NEEDS_MANUAL_SPLIT)
                    if mismatched > 0:
                        status_note += strings.MANUSCRIPT_TRANSLATE_TM_MISMATCH_NOTE.format(count=mismatched)
                    self.host.report_status(status_note)
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
                              terms=terms, attempted=attempted, views=self.record_views,
                              features=self.review_features())

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
        features = self.review_features()
        request = sr.plan_request(self.payload, index, self.term_basis, features=features)
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
                    resolutions=request.resolutions or None, resolution_context=request.resolution_context,
                    **_features_kw(request.features))
            except ClientError as exc:
                if (exc.code == ERROR_INVALID_REQUEST and request.resolutions and not fell_back
                        and generation == self.semantic_generation):
                    fell_back = True
                    if not still_there():
                        return "changed"
                    request = sr.plan_request(self.payload, index, self.term_basis, allow_resolutions=False,
                                              features=features)
                    continue
                return exc
            if generation != self.semantic_generation:
                return "changed"
            self.record_live_review_dictionary(report.get("dictionary") if isinstance(report, Mapping) else None)
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
            request = sr.plan_request(self.payload, index, self.term_basis, features=features)
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
        self.provenance_task = None
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

    def _tm_provenance(self) -> tuple[str | None, str | None]:
        """Current verified (terms_db_revision, game_version) from this session.

        Delegates to ``self.host.tm_provenance()`` when available; otherwise
        reads the live ``GET /v1/dictionary`` identity (``revision`` and
        ``source_game_version``) and then ``self.live_review_dictionary``
        (``revision`` and ``game_version`` / ``source_game_version``), never
        falling back to historical workfile ``term_basis`` or ``source_commit``.
        The identity comes first because it is the served dictionary itself,
        while a review dictionary is the snapshot one review ran against.
        """
        getter = getattr(self.host, "tm_provenance", None)
        if callable(getter):
            return getter()
        identity = self.live_dictionary_identity
        if identity is not None:
            terms = tm.normalize_provenance_value(getattr(identity, "revision", None))
            game = tm.normalize_provenance_value(getattr(identity, "source_game_version", None))
            return terms, game
        if not isinstance(self.live_review_dictionary, dict):
            return None, None
        terms = tm.normalize_provenance_value(self.live_review_dictionary.get("revision"))
        game: str | None = None
        for key in ("game_version", "source_game_version"):
            game = tm.normalize_provenance_value(self.live_review_dictionary.get(key))
            if game is not None:
                break
        return terms, game

    @staticmethod
    def _tm_revision_label(revision: str | None) -> str:
        if not revision:
            return strings.TM_NOT_RECORDED
        return revision if len(revision) <= 16 else revision[:12]

    def _client_configured(self) -> bool:
        if hasattr(self.api_client, "is_configured"):
            return bool(self.api_client.is_configured)
        if hasattr(self.api_client, "configured"):
            return bool(self.api_client.configured)
        return True

    def _tm_l1_status_note(self, l1_started: bool) -> str:
        if l1_started:
            return strings.MANUSCRIPT_TRANSLATE_TM_L1_RUNNING
        if not self._client_configured():
            return strings.MANUSCRIPT_TRANSLATE_TM_L1_OFFLINE
        if self.structure_known_unavailable():
            return strings.MANUSCRIPT_TRANSLATE_TM_L1_UNAVAILABLE
        return ""

    def _classify_tm_sources(
        self,
        sources: Sequence[str],
        direction: str,
        terms_rev: str,
        game_ver: str | None,
    ) -> list[tm.TmSegmentClassification | None]:
        memory = self.translation_memory
        assert memory is not None

        def readable(text: str) -> bool:
            return bool(text.strip()) and len(text) <= tm.SOURCE_SCALARS[1]

        def neighbour(index: int) -> tuple[str | None, bool]:
            if not 0 <= index < len(sources):
                return None, False
            text = sources[index]
            if not text.strip():
                return None, False
            if len(text) > tm.SOURCE_SCALARS[1]:
                return None, True
            return text, False

        classes: list[tm.TmSegmentClassification | None] = [None] * len(sources)
        run: list[int] = []

        def flush() -> None:
            if not run:
                return
            items = []
            for index in run:
                prev_src, prev_unhashable = neighbour(index - 1)
                next_src, next_unhashable = neighbour(index + 1)
                items.append({
                    "source": sources[index],
                    "prev_source": prev_src,
                    "next_source": next_src,
                    "prev_unhashable": prev_unhashable,
                    "next_unhashable": next_unhashable,
                })
            found = memory.classify_segments_for_tm(
                items,
                direction,
                current_terms_db_revision=terms_rev,
                current_game_version=game_ver,
            )
            for index, item in zip(run, found):
                classes[index] = item
            run.clear()

        for position, text in enumerate(sources):
            if readable(text):
                run.append(position)
            else:
                flush()
        flush()
        return classes

    @staticmethod
    def _count_translation_pieces(text: str) -> int:
        return sum(1 for piece in segment_text(text, SEGMENT_LIMIT) if piece.core_end > piece.core_start)

    def _baseline_requests_before_tm(self) -> int:
        """How many ``translate()`` requests the original non-TM path would send."""
        payload = self.payload
        source, target = payload["source"], payload["target"]
        if blank(target):
            return self._count_translation_pieces(source)
        if payload["alignments"] is not None:
            return sum(
                self._count_translation_pieces(source[p.source.core_start:p.source.core_end])
                for p in self.current_pairs()
                if p.status == STATUS_UNTRANSLATED
            )
        partial = self.resumable_partial_prefix()
        if partial is not None:
            return self._count_translation_pieces(partial[1])
        return 0

    def _apply_tm_fill_transaction(
        self,
        raw_target: str,
        alignments: list[dict[str, Any]] | None,
        fills: Sequence[tuple[int, int, str, int, int]],
    ) -> bool:
        """Write matched TM segments into target in one undoable transaction."""
        current, edits, combined_remap = build_target_fill_transaction(
            self.payload["source"],
            raw_target,
            copy.deepcopy(alignments),
            fills,
            whitespace=WHITESPACE,
            separate_paragraphs=True,
        )
        multi = getattr(self.host, "apply_target_edits", None)
        if callable(multi):
            return bool(multi(current, edits, remap=combined_remap))
        if len(edits) == 1:
            return self.host.apply_target_write(current, edits[0], remap=combined_remap)
        value = self.draft_snapshot()
        value["target"] = current
        value["alignments"] = combined_remap.alignments
        outcome = self.apply_edit(value, edits=edits, remap=combined_remap)
        return outcome.committed

    def _prefill_from_translation_memory(
        self,
        terms_rev: str,
        game_ver: str | None,
    ) -> tuple[bool, int]:
        """Pre-fill strictly trusted (context_exact + verified_fresh) segments.

        Returns ``(rejected_by_editor, reused_count)``.
        """
        memory = self.translation_memory
        if memory is None or not memory.usable:
            return False, 0
        payload = self.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        if blank(source):
            return False, 0
        try:
            if payload["alignments"] is not None:
                pairs = self.current_pairs()
                if not pairs:
                    return False, 0
                sources = [source[p.source.core_start:p.source.core_end] for p in pairs]
                classes = self._classify_tm_sources(sources, direction, terms_rev, game_ver)
                fills: list[tuple[int, int, str, int, int]] = []
                for p, item in zip(pairs, classes):
                    if p.status != STATUS_UNTRANSLATED:
                        continue
                    if item is None or item.status != tm.TM_STATUS_CONTEXT_EXACT_FRESH or item.match is None:
                        continue
                    text = item.match.entry.target_text
                    if text.strip():
                        fills.append((p.target.core_start, p.target.core_end, text, p.source.start, p.source.end))
                if not fills:
                    return False, 0
                fills.sort(key=lambda item: (item[0], item[1], item[3], item[4]))
                if not self._apply_tm_fill_transaction(target, payload["alignments"], fills):
                    return True, 0
                return False, len(fills)

            if blank(target):
                para_ranges = seg_paragraph_ranges(source)
                if not para_ranges:
                    return False, 0
                para_cores = [seg_core(source, ps, pe) for ps, pe in para_ranges]
                sources = [source[cs:ce] for cs, ce in para_cores]
                classes = self._classify_tm_sources(sources, direction, terms_rev, game_ver)
                matched = [
                    i for i, item in enumerate(classes)
                    if item is not None and item.status == tm.TM_STATUS_CONTEXT_EXACT_FRESH
                    and item.match is not None and item.match.entry.target_text.strip()
                ]
                if not matched:
                    return False, 0
                if len(para_cores) > 64:
                    if len(matched) == len(para_cores):
                        changed = "\n\n".join(classes[i].match.entry.target_text for i in matched)  # type: ignore[union-attr]
                        ok = self.host.apply_target_write(
                            changed, ("target", 0, 0, len(changed)), remap=AlignmentRemap(None),
                        )
                        return (not ok), (len(matched) if ok else 0)
                    return False, 0
                align_cores = [(cs, ce) for cs, ce in para_cores if 0 < ce - cs <= MAX_SPAN_SCALARS]
                base_alignments = [
                    {"source": {"start": cs, "end": ce, "text": source[cs:ce]}, "target": None}
                    for cs, ce in align_cores
                ]
                fills = [
                    (0, 0, classes[i].match.entry.target_text, para_cores[i][0], para_cores[i][1])  # type: ignore[union-attr]
                    for i in matched
                ]
                if not self._apply_tm_fill_transaction("", base_alignments, fills):
                    return True, 0
                return False, len(fills)

            partial = self.resumable_partial_prefix()
            if partial is not None:
                s_paras = seg_paragraph_ranges(source)
                t_paras = seg_paragraph_ranges(target)
                if not (1 <= len(t_paras) < len(s_paras)) or len(s_paras) > 64:
                    return False, 0
                s_cores = [seg_core(source, ps, pe) for ps, pe in s_paras]
                t_cores = [seg_core(target, ps, pe) for ps, pe in t_paras]
                if any(ce - cs > MAX_SPAN_SCALARS for cs, ce in (*s_cores, *t_cores)):
                    return False, 0
                sources = [source[cs:ce] for cs, ce in s_cores]
                classes = self._classify_tm_sources(sources, direction, terms_rev, game_ver)
                matched = [
                    i for i in range(len(t_cores), len(s_cores))
                    if classes[i] is not None and classes[i].status == tm.TM_STATUS_CONTEXT_EXACT_FRESH  # type: ignore[union-attr]
                    and classes[i].match is not None and classes[i].match.entry.target_text.strip()  # type: ignore[union-attr]
                ]
                if not matched:
                    return False, 0
                base_alignments = [
                    {
                        "source": {"start": cs, "end": ce, "text": source[cs:ce]},
                        "target": {"start": tcs, "end": tce, "text": target[tcs:tce]},
                    }
                    for (cs, ce), (tcs, tce) in zip(s_cores[:len(t_cores)], t_cores, strict=True)
                ] + [
                    {"source": {"start": cs, "end": ce, "text": source[cs:ce]}, "target": None}
                    for cs, ce in s_cores[len(t_cores):]
                ]
                insert_at = t_cores[-1][1]
                fills = [
                    (insert_at, insert_at, classes[i].match.entry.target_text, s_cores[i][0], s_cores[i][1])  # type: ignore[union-attr]
                    for i in matched
                ]
                if not self._apply_tm_fill_transaction(target, base_alignments, fills):
                    return True, 0
                return False, len(fills)
        except (tm.TmError, OSError):
            return False, 0
        return False, 0

    def _record_tm_summary(
        self,
        *,
        reused: int,
        translated: int,
        model_calls: int,
        baseline_requests: int,
        terms_rev: str | None,
        game_ver: str | None,
        l1_started: bool,
        pairs: Sequence,
    ) -> str:
        total = reused + translated
        requests_reduced = baseline_requests > 0 and model_calls < baseline_requests
        saved_ratio = ((baseline_requests - model_calls) / baseline_requests) if requests_reduced else 0.0
        mismatched = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
        pending_other = sum(1 for p in pairs if p.status not in (STATUS_READY, STATUS_NEEDS_MANUAL_SPLIT))
        self.last_tm_translation_summary = {
            "reused_segments": reused,
            "translated_segments": translated,
            "total_segments": total,
            "model_calls": model_calls,
            "baseline_requests": baseline_requests,
            "requests_reduced": requests_reduced,
            "saved_ratio": saved_ratio,
            "terms_db_revision": terms_rev,
            "game_version": game_ver,
            "l1_triggered": l1_started,
            "mismatched_segments": mismatched,
            "pending_segments": pending_other,
        }
        rev_label = self._tm_revision_label(terms_rev)
        l1_note = self._tm_l1_status_note(l1_started)
        tail_parts: list[str] = []
        if mismatched > 0:
            tail_parts.append(strings.MANUSCRIPT_TRANSLATE_TM_MISMATCH_NOTE.format(count=mismatched))
        if pending_other > 0:
            tail_parts.append(strings.MANUSCRIPT_TRANSLATE_TM_PENDING_NOTE.format(count=pending_other))
        tail_note = "".join(tail_parts)
        if translated == 0:
            return strings.MANUSCRIPT_TRANSLATE_TM_ALL_REUSED.format(
                reused=reused,
                baseline=baseline_requests,
                revision=rev_label,
                l1_note=l1_note,
                tail_note=tail_note,
            )
        if not requests_reduced:
            return strings.MANUSCRIPT_TRANSLATE_TM_SUMMARY_NO_SAVING.format(
                reused=reused,
                total=total,
                translated=translated,
                actual=model_calls,
                baseline=baseline_requests,
                revision=rev_label,
                l1_note=l1_note,
                tail_note=tail_note,
            )
        pct = round(saved_ratio * 100)
        return strings.MANUSCRIPT_TRANSLATE_TM_SUMMARY.format(
            reused=reused,
            total=total,
            translated=translated,
            baseline=baseline_requests,
            actual=model_calls,
            pct=pct,
            revision=rev_label,
            l1_note=l1_note,
            tail_note=tail_note,
        )

    def start_translation(self, *, single_segment: bool = False) -> None:
        """Translate a blank target, resume remaining untranslated paragraphs,
        or translate/re-translate one segment without overwriting finished work.

        A run that could reuse confirmed segments needs the dictionary the
        service is *serving* right now. When this session has not learned it
        from a review, the authoritative ``GET /v1/dictionary`` answer is
        fetched once before the reuse decision, so a fresh session can skip
        trusted segments without a term review first. Fail-closed: if that
        answer is absent or unusable the revision stays unknown and nothing is
        skipped.
        """
        if self._needs_dictionary_identity(single_segment=single_segment):
            self._launch_translation_after_identity(single_segment)
            return
        self._start_translation(single_segment=single_segment)

    def _needs_dictionary_identity(self, *, single_segment: bool) -> bool:
        """Whether this press must ask which dictionary is served before reusing.

        Every press that could reuse asks, not only the first of a session. The
        dictionary can be replaced under a running workbench without any field
        of `/v1/meta` or `/v1/reviews` moving, and a cached revision would then
        let entries confirmed under the previous dictionary count as fresh. One
        request per press is what makes that change visible at the moment it
        matters.

        A server already known not to list ``dictionary-identity-v1`` is the one
        case that spends nothing: it has no answer to give, and the older
        `/v1/meta` + `/v1/reviews` path stays in charge.
        """
        if single_segment:
            return False
        memory = self.translation_memory
        if memory is None or not memory.usable:
            return False
        if not self._client_configured():
            return False
        if self.resumable() is not None:
            return False
        if not (blank(self.payload["target"]) or not self.translation_withdrawn):
            return False
        if (
            self.capabilities is not None
            and semantic.CAPABILITY_DICTIONARY_IDENTITY not in self.capabilities
        ):
            return False
        return True

    def _launch_translation_after_identity(self, single_segment: bool) -> None:
        """Fetch the identity, then run the same decision ``start_translation`` makes.

        The probe occupies ``translate_task`` while it is the only thing the
        translation path is doing, so Cancel, endpoint changes and the
        workbench's own settle checks see a run in progress. It hands the slot
        over before re-entering: that call may stop the previous run, and a
        ``stop_translation`` from inside this task would cancel this task.

        A second press while the probe is still running is ignored. The press
        already waiting will start the run when the answer lands, and two
        presses must not become two runs (the prefill's ``stop_translation``
        would cancel whichever arrived second).
        """
        current = self.translate_task
        if current is not None and current is self.provenance_task and not current.done():
            return
        payload = self.payload
        # What this press was pressed on. A run is bound to the text it started
        # from (``translation_basis_unchanged``); a press held up by the identity
        # request has to hold the same line, or an edit - or an alignment change,
        # which resets the pairing the reuse decision is made on - is translated
        # without anybody pressing 翻译 for it.
        basis = (
            payload["source"],
            payload["target"],
            payload["direction"],
            copy.deepcopy(payload["alignments"]),
        )
        probe = asyncio.ensure_future(self._start_translation_after_identity(single_segment, basis))
        self.provenance_task = probe
        self.translate_task = probe
        # The wait is a network round trip, so it is shown and cancellable like
        # any other: without this the workbench would render a press that
        # apparently did nothing, with no Cancel, until the answer arrived.
        self.translate_state = TRANSLATION_PROBING
        self.host.report_status(strings.MANUSCRIPT_TRANSLATE_PROVENANCE)
        self._translation_ui()

    async def _start_translation_after_identity(self, single_segment: bool, basis: tuple) -> None:
        generation = self.translate_generation
        await self.refresh_live_dictionary_identity()
        # A stop (Cancel, an endpoint change, a replaced run) during the request
        # owns the decision now; this press must not resurrect a run under it.
        if generation != self.translate_generation:
            return
        if self.provenance_task is asyncio.current_task():
            self.provenance_task = None
        if self.translate_task is asyncio.current_task():
            self.translate_task = None
        payload = self.payload
        if (
            payload["source"],
            payload["target"],
            payload["direction"],
            payload["alignments"],
        ) != basis:
            # The owner edited the document, switched direction, or re-paired it
            # while the identity was in flight. The press belonged to the earlier
            # document, so it ends the way a changed run ends: nothing written,
            # said so.
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            self._translation_ui()
            return
        self._start_translation(single_segment=single_segment)
        if self.translate_state == TRANSLATION_PROBING:
            # Nothing was launched (a refusal or a completion line is already on
            # screen): back to idle, so no Cancel outlives the wait it belonged to.
            self.translate_state = TRANSLATION_IDLE
            self._translation_ui()

    def _start_translation(self, *, single_segment: bool = False) -> None:
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
        tm_reused = 0
        tm_baseline_requests = 0
        tm_revision: str | None = None
        tm_game_version: str | None = None
        if cursor is None and not single_segment and (blank(target) or not self.translation_withdrawn):
            if self.translation_memory is not None and self.translation_memory.usable:
                terms_rev, game_ver = self._tm_provenance()
                if terms_rev is not None:
                    tm_baseline_requests = self._baseline_requests_before_tm()
                    rejected, tm_reused = self._prefill_from_translation_memory(terms_rev, game_ver)
                    if rejected:
                        self.host.report_status(strings.MANUSCRIPT_EDIT_REJECTED)
                        self._translation_ui()
                        return
                    if tm_reused > 0:
                        tm_revision, tm_game_version = terms_rev, game_ver
                        payload = self.payload
                        source, target, direction = payload["source"], payload["target"], payload["direction"]

        if tm_reused > 0:
            self.stop_translation()
            self.translation_withdrawn = False
            pairs = self.current_pairs()
            remaining = [p for p in pairs if p.status == STATUS_UNTRANSLATED]
            if not remaining:
                mismatched = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
                self.host.report_translation_complete(mismatched)
                l1_started = self.start_structure_check(restart=True) if self._client_configured() else False
                summary_text = self._record_tm_summary(
                    reused=tm_reused,
                    translated=0,
                    model_calls=0,
                    baseline_requests=tm_baseline_requests,
                    terms_rev=tm_revision,
                    game_ver=tm_game_version,
                    l1_started=l1_started,
                    pairs=pairs,
                )
                self.host.report_status(summary_text)
                if l1_started:
                    self.note_structure_progress(summary_text)
                self._translation_ui()
                return
            pair = remaining[0]
            batch_spans = [(p.source.core_start, p.source.core_end) for p in remaining]
            planned_requests = sum(
                self._count_translation_pieces(source[p.source.core_start:p.source.core_end])
                for p in remaining
            )
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_TM_PREFILL.format(
                reused=tm_reused,
                remaining=len(batch_spans),
                planned=planned_requests,
                baseline=tm_baseline_requests,
                revision=self._tm_revision_label(tm_revision),
            ))
        elif cursor is None and blank(target):
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
        if tm_reused == 0:
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
                tm_reused=tm_reused,
                tm_revision=tm_revision,
                tm_game_version=tm_game_version,
                tm_baseline_requests=tm_baseline_requests,
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
            tm_reused=base_run.tm_reused,
            tm_revision=base_run.tm_revision,
            tm_game_version=base_run.tm_game_version,
            tm_model_calls=base_run.tm_model_calls,
            tm_baseline_requests=base_run.tm_baseline_requests,
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
                    run.tm_model_calls += 1
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
        pairs = self.current_pairs()
        mismatched = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
        self.host.report_translation_complete(mismatched)
        if run.tm_reused > 0:
            l1_started = self.start_structure_check(restart=True) if self._client_configured() else False
            summary_text = self._record_tm_summary(
                reused=run.tm_reused,
                translated=run.batch_done,
                model_calls=run.tm_model_calls,
                baseline_requests=run.tm_baseline_requests,
                terms_rev=run.tm_revision,
                game_ver=run.tm_game_version,
                l1_started=l1_started,
                pairs=pairs,
            )
            self.host.report_status(summary_text)
            if l1_started:
                self.note_structure_progress(summary_text)
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

    def translation_stopped(self, run: Translation | None, state: str, reason: str = "", *,
                            quota: bool = False) -> None:
        """The run ended before its last piece: the owner paused it, the
        service asked to wait, or a piece failed. The run stays as the cursor
        for the next press.

        ``run`` is None when the owner cancelled a press that was still waiting
        for the served dictionary's identity: no piece ran, so there is no
        cursor to keep and nothing to report - the document is untouched and
        the controls come back.
        """
        if run is None:
            self.translate_task = None
            self.provenance_task = None
            self.translate_run = None
            self.translate_state, self.translate_status = TRANSLATION_IDLE, ""
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_PROVENANCE_CANCELLED)
            self._translation_ui()
            return
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
