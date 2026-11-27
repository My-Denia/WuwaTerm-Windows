"""Pure-Python review coordinator and view-projection models (no Qt, no HTTP).

Owns short-manuscript document review, long-manuscript segmented term & semantic
review, deterministic structure checks (Layer 1), semantic decisions, and pure
projection helpers that transform review state into lightweight row models for
the Qt Model/View layer.
"""
from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import segment_records as sr
from .. import semantic, strings, structure
from .. import semantic_session as sessions
from ..document.alignment_state import AlignmentState
from ..document.edit_store import DocumentEditStore
from ..document_edits import (
    REASON_FULL,
    REASON_NOT_APPLICABLE,
    REASON_RECONFIRM,
    REASON_REJECTED,
    REASON_STALE,
    CommandOutcome,
    copy_payload,
    normalize_semantic,
    remap_for_free_edit,
    remap_splice,
)
from ..errors import (
    ERROR_CANCELLED,
    ERROR_FORBIDDEN,
    ERROR_INSECURE_ENDPOINT,
    ERROR_INVALID_REQUEST,
    ERROR_LLM_BUDGET_EXHAUSTED,
    ERROR_LLM_UNAVAILABLE,
    ERROR_NOT_CONFIGURED,
    ERROR_OFFLINE,
    ERROR_RATE_LIMITED,
    ERROR_UNAUTHORIZED,
    ClientError,
)
from ..manuscript import (
    MAX_HISTORY_SEGMENTS,
    MAX_SEMANTIC_DECISIONS,
    MAX_SEMANTIC_HISTORY,
    WorkfileError,
    is_ambiguous_mention,
    semantic_payload_fits,
    serialize_workfile,
)
from ..segmentation import STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY

DOCUMENT_REVIEW_LIMIT = 2000
QUOTA_CODES = frozenset({ERROR_RATE_LIMITED, ERROR_LLM_BUDGET_EXHAUSTED})
FATAL_CODES = frozenset({
    ERROR_UNAUTHORIZED,
    ERROR_FORBIDDEN,
    ERROR_NOT_CONFIGURED,
    ERROR_INSECURE_ENDPOINT,
    ERROR_OFFLINE,
    ERROR_LLM_UNAVAILABLE,
})
REPEATED_FAILURES = 2


def features_kw(features: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """``review_pair``'s ``features`` argument, only when there are features."""
    return {"features": tuple(features)} if features else {}


def offset_span(span: dict[str, Any] | None, by: int) -> dict[str, Any] | None:
    return None if span is None else {"start": span["start"] + by, "end": span["end"] + by, "text": span["text"]}


@dataclass(frozen=True)
class SegmentTermReportProjection:
    """Pure-Python display projection of long-manuscript segment term findings."""

    report: dict[str, Any]
    state: str
    refs: list[tuple[int, int, int]]
    recheck: list[bool]
    saved_flags: list[bool]
    ambiguous: list[tuple[dict[str, Any], tuple[int, int, int] | None]]


@dataclass(frozen=True)
class StructureRowProjection:
    """One projected structure-check finding row."""

    pair_index: int
    finding: dict[str, Any]
    text: str


def build_document_term_groups(
    payload: Mapping[str, Any],
    report: Mapping[str, Any],
    review_state: str,
) -> list[dict[str, Any]]:
    """Group findings of a short/document-mode report using segment_records rules."""
    source, target, direction = payload["source"], payload["target"], payload["direction"]
    if not report["findings"]:
        return []
    synthetic_findings = [
        {**f, "rule_id": sr.TERM_PAIR_RULE}
        for f in report["findings"]
        if isinstance(f.get("source_span"), dict)
    ]
    dictionary = report.get("dictionary") or {}
    coverage = report.get("coverage") or {}
    synthetic_report = {
        "request_id": report.get("request_id") or "doc",
        "source_revision": sr.content_hash(source),
        "target_revision": sr.content_hash(target),
        "rule_version": sr.RULE_VERSION,
        "matcher_revision": report.get("matcher_revision") or ("0" * 64),
        "dictionary": {
            "schema_version": dictionary.get("schema_version") or "1",
            "source_commit": dictionary.get("source_commit") or "doc",
            "term_count": int(dictionary.get("term_count") or 0),
            "revision": dictionary.get("revision") or ("0" * 64),
        },
        "coverage": {
            "evaluated": int(coverage.get("evaluated") or 0),
            "not_evaluated": int(coverage.get("not_evaluated") or 0),
            "rules": list(sr.COVERAGE_RULES),
        },
        "findings": synthetic_findings,
        "truncated": bool(report.get("truncated")),
    }
    compact = sr.compact_result(synthetic_report, source, target)
    if compact is None:
        return []
    basis = sr.basis_of(synthetic_report)
    record = {
        "index": 0,
        "segmentation_version": "segmentation-v1",
        "source_start": 0,
        "source_end": len(source),
        "target_start": 0,
        "target_end": len(target),
        "source_hash": sr.content_hash(source),
        "target_hash": sr.content_hash(target),
        "status": "done" if review_state == "current" else "stale",
        "direction": direction,
        "alignment_input": sr.alignment_input(
            payload.get("alignments"), source, target, (0, len(source)), (0, len(target))
        ),
        "basis": basis,
        "result": compact,
        "choices": [],
    }
    return sr.group_term_occurrences(
        [record],
        [],
        source,
        target,
        direction,
        alignments=payload.get("alignments"),
        basis=basis if review_state == "current" else None,
    )


def project_segment_term_report(
    payload: Mapping[str, Any],
    records: Sequence[dict[str, Any]] | None,
    record_views: sr.RecordViews,
    term_basis: dict[str, Any] | None,
    features: tuple[str, ...] | None,
) -> SegmentTermReportProjection | None:
    """Build one display report from every current long-manuscript segment record."""
    if records is None:
        return None
    source, target = payload["source"], payload["target"]
    findings: list[dict[str, Any]] = []
    refs: list[tuple[int, int, int]] = []
    recheck: list[bool] = []
    saved_flags: list[bool] = []
    ambiguous: list[tuple[dict[str, Any], tuple[int, int, int] | None]] = []
    evaluated = not_evaluated = shown = 0
    truncated = False
    for index, record in enumerate(records):
        view = record_views.get(record, payload, term_basis, features)
        state = view.state
        if not view.content_current:
            continue
        report = sr.expand_result(record, source, target)
        for finding in report["findings"]:
            placed = {
                **finding,
                "source_span": offset_span(finding["source_span"], record["source_start"]),
                "target_span": offset_span(finding["target_span"], record["target_start"]),
            }
            ref = (index, finding["source_span"]["start"], finding["source_span"]["end"])
            if is_ambiguous_mention(finding):
                ambiguous.append((placed, ref))
                not_evaluated -= 1
                continue
            findings.append(placed)
            refs.append(ref)
            recheck.append(state == "stale")
            saved_flags.append(state == "saved")
        evaluated += report["coverage"]["evaluated"]
        not_evaluated += report["coverage"]["not_evaluated"]
        truncated = truncated or report["truncated"]
        shown += 1
    if not shown:
        return None
    return SegmentTermReportProjection(
        report={
            "request_id": None,
            "findings": findings,
            "coverage": {
                "evaluated": evaluated,
                "not_evaluated": max(0, not_evaluated),
                "rules": list(sr.COVERAGE_RULES),
            },
            "truncated": truncated,
        },
        state="saved" if term_basis is None else "current",
        refs=refs,
        recheck=recheck,
        saved_flags=saved_flags,
        ambiguous=ambiguous,
    )


def project_structure_rows(views: Sequence[structure.View]) -> list[StructureRowProjection]:
    """Project Layer 1 structure-check views into displayable rows."""
    rows: list[StructureRowProjection] = []
    for view in views:
        if not view.flagged:
            continue
        number = view.index + 1
        layout = view.result["layout"]
        if view.layout_differs:
            rows.append(
                StructureRowProjection(
                    pair_index=view.index,
                    finding={"rule_id": None, "source_span": None, "target_span": None},
                    text=strings.MANUSCRIPT_L1_LAYOUT.format(
                        number=number,
                        sp=layout["source_paragraphs"],
                        tp=layout["target_paragraphs"],
                        sl=layout["source_lines"],
                        tl=layout["target_lines"],
                    ),
                )
            )
        for finding in view.result["findings"]:
            rule = strings.MANUSCRIPT_L1_RULES.get(finding["rule_id"], finding["rule_id"])
            span = finding["source_span"] or finding["target_span"]
            if span is None:
                text = strings.MANUSCRIPT_L1_ITEM_SEGMENT.format(number=number, rule=rule)
            else:
                side = strings.MANUSCRIPT_L1_SOURCE if finding["source_span"] else strings.MANUSCRIPT_L1_TARGET
                text = strings.MANUSCRIPT_L1_ITEM.format(number=number, rule=rule, side=side, text=span["text"])
            rows.append(StructureRowProjection(pair_index=view.index, finding=finding, text=text))
    return rows


def issue_pair_index(issue: sessions.LiveIssue, pairs: Sequence[Any]) -> int | None:
    """The current pair whose core holds the issue (its source span, else target span)."""
    span, side = (issue.source_span, "source") if issue.source_span is not None else (issue.target_span, "target")
    if span is None:
        return None
    for pair in pairs:
        region = getattr(pair, side)
        if region.core_start <= span["start"] and span["end"] <= region.core_end:
            return pair.index
    return None


def project_layer2_rows(
    issues: Sequence[sessions.LiveIssue],
    issue_state_fn: Callable[[sessions.LiveIssue], str],
    pairs: Sequence[Any],
    views: Sequence[structure.View],
    has_structure_results: bool,
) -> tuple[list[sessions.LiveIssue], int]:
    """Filter semantic issues already covered by deterministic structure findings."""
    if not issues or not has_structure_results:
        return list(issues), 0
    listed: list[sessions.LiveIssue] = []
    covered = 0
    for issue in issues:
        if issue_state_fn(issue) in sessions.UNHANDLED:
            index = issue_pair_index(issue, pairs)
            view = views[index] if index is not None and index < len(views) else None
            if structure.covered_by(issue.issue["category"], issue.source_span, issue.target_span, view):
                covered += 1
                continue
        listed.append(issue)
    return listed, covered


class ReviewCoordinator:
    """Coordinates document term review, segmented term/semantic review, and Layer 1 checks."""

    def __init__(
        self,
        *,
        get_api_client: Callable[[], Any],
        host: Any,
        edits: DocumentEditStore,
        alignment: AlignmentState,
        session: sessions.Session,
        record_live_review_dictionary: Callable[[Mapping[str, Any] | None], None],
    ) -> None:
        self._get_api_client = get_api_client
        self.host = host
        self._edits = edits
        self._alignment = alignment
        self.session = session
        self._record_live_review_dictionary = record_live_review_dictionary

        # Short document term review
        self.generation = 0
        self.task: asyncio.Task | None = None
        self.review_state = "none"
        self.review_detail = ""
        self.request_id: str | None = None
        self.capabilities: frozenset[str] | None = None
        self.term_mode = "document"

        # Segmented term & semantic run
        self.semantic_generation = 0
        self.semantic_task: asyncio.Task | None = None
        self.semantic_state = "none"
        self.semantic_detail = ""
        self.segment_failures: dict[tuple[Any, ...], str] = {}
        self.segment_notes: dict[tuple[Any, ...], list[str]] = {}
        self.known_texts: dict[str, str] = {}
        self.history_run = 0
        self.history_written = -1
        self.term_basis: dict[str, Any] | None = None
        self.record_views = sr.RecordViews()

        # Layer 1 deterministic structure check
        self.structure = structure.State()
        self.structure_generation = 0
        self.structure_task: asyncio.Task | None = None
        self._structure_progress_status: str | None = None

    @property
    def _api_client(self) -> Any:
        return self._get_api_client()

    # -- state queries ------------------------------------------------------

    def basis(self) -> sessions.Basis:
        return sessions.Basis(semantic_required=self.semantic_available())

    def semantic_available(self) -> bool:
        return self.capabilities is not None and semantic.CAPABILITY_SEMANTIC_REVIEW in self.capabilities

    def review_features(self) -> tuple[str, ...] | None:
        return sr.session_review_features(self.capabilities)

    def structure_known_unavailable(self) -> bool:
        return self.capabilities is not None and structure.CAPABILITY_STRUCTURE_CHECK not in self.capabilities

    def texts_for_reconcile(self) -> dict[str, str]:
        texts = sessions.verified_texts(self._edits.payload.get("semantic_texts") or [])
        texts.update(self.known_texts)
        return texts

    def semantic_texts(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return sessions.semantic_texts_for(
            payload.get("semantic_decisions") or [],
            self.texts_for_reconcile(),
            payload["source"],
            payload["target"],
        )

    def reset_known_texts(self, value: Mapping[str, Any] | None = None) -> None:
        self.known_texts = sessions.verified_texts((value or {}).get("semantic_texts") or [])
        for text in ((value or {}).get("source"), (value or {}).get("target")):
            if text is not None:
                self.known_texts.setdefault(sessions.revision(text), text)

    def reset_term_session(self) -> None:
        self.term_basis = None
        self._edits.reset_session_tracking()
        self._alignment.invalidate()
        self.cancel_structure_check()
        self.structure = structure.State()

    def issue_state(self, issue: sessions.LiveIssue) -> str:
        payload = self._edits.payload
        return sessions.state_of(issue, self._edits.decision_index(), payload["source"], payload["target"])

    # -- semantic decisions -------------------------------------------------

    def basis_holds(self, issue: sessions.LiveIssue) -> bool:
        if issue.basis_lost or not self.semantic_available():
            return False
        return any(issue in r.issues and sessions.semantic_part_valid(r) for r in self.session.results)

    def can_accept(self, issue: sessions.LiveIssue | None) -> bool:
        return (
            issue is not None
            and issue.issue["suggestion"] is not None
            and self.basis_holds(issue)
            and self.issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
            and sessions.span_live(issue.suggestion_span, self._edits.payload["target"])
        )

    def can_adjust(self, issue: sessions.LiveIssue | None) -> bool:
        return (
            issue is not None
            and issue.edit_span() is not None
            and self.basis_holds(issue)
            and self.issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
            and sessions.span_live(issue.edit_span(), self._edits.payload["target"])
        )

    def can_ignore(self, issue: sessions.LiveIssue | None) -> bool:
        return (
            issue is not None
            and self.issue_state(issue) in sessions.UNHANDLED
            and sessions.decision_for(issue, self._edits.decision_index()) is None
        )

    @staticmethod
    def selected_option(issue: sessions.LiveIssue, alternative: bool = False) -> tuple[str, str] | None:
        suggestion = issue.issue["suggestion"]
        if suggestion is None:
            return None
        alternatives = issue.issue["alternatives"]
        if alternative and alternatives:
            return alternatives[0]["replacement"], alternatives[0]["term_effect"]
        return suggestion["replacement"], suggestion["term_effect"]

    def record_decision(
        self,
        issue: sessions.LiveIssue,
        decision: str,
        *,
        applied_text: str | None = None,
        target: str | None = None,
        replaced: dict[str, Any] | None = None,
    ) -> CommandOutcome:
        record = sessions.make_decision(issue, decision, applied_text)
        decisions = sessions.with_decision(self._edits.decisions(), issue, record)
        value = self._edits.draft_snapshot()
        value["semantic_decisions"] = decisions
        remap = None
        if target is not None:
            before_target = value["target"]
            value["target"] = target
            if replaced is not None:
                remap = remap_splice(
                    value["alignments"], "target", value["source"], target,
                    replaced["start"], replaced["end"], len(applied_text or ""),
                )
            else:
                remap = remap_for_free_edit(
                    value["alignments"], "target", value["source"], before_target, value["source"], target
                )
            value["alignments"] = remap.alignments
        candidate = copy_payload(self._edits.payload)
        candidate.update(copy_payload(value))
        candidate.setdefault("semantic_history", [])
        candidate["semantic_texts"] = self.semantic_texts(candidate)
        normalize_semantic(candidate)
        if len(decisions) > MAX_SEMANTIC_DECISIONS or not semantic_payload_fits(candidate):
            return CommandOutcome(reason=REASON_FULL)
        edits = None
        if replaced is not None:
            edits = [("target", replaced["start"], replaced["end"], len(applied_text or ""))]
        return CommandOutcome(True, edit=self._edits.apply_edit(value, edits=edits, exempt=issue, remap=remap))

    def accept_issue(
        self,
        issue: sessions.LiveIssue | None,
        *,
        can_display: Callable[[str], bool],
        alternative: bool = False,
    ) -> CommandOutcome:
        if issue is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        if not self.basis_holds(issue):
            return CommandOutcome(reason=REASON_RECONFIRM)
        option = self.selected_option(issue, alternative)
        if option is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        replacement = option[0]
        try:
            changed = semantic.apply_span(self._edits.payload["target"], issue.suggestion_span, replacement)
        except semantic.StaleSpan:
            return CommandOutcome(reason=REASON_STALE)
        if not can_display(changed):
            return CommandOutcome(reason=REASON_REJECTED)
        return self.record_decision(
            issue, "accepted", applied_text=replacement, target=changed, replaced=issue.suggestion_span
        )

    def adjust_issue(
        self,
        issue: sessions.LiveIssue | None,
        text: str,
        *,
        can_display: Callable[[str], bool],
    ) -> CommandOutcome:
        if issue is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        if not self.can_adjust(issue):
            return CommandOutcome(reason=REASON_RECONFIRM)
        if len(text) > semantic.MAX_SIDE_SCALARS:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        try:
            changed = semantic.apply_span(self._edits.payload["target"], issue.edit_span(), text)
        except semantic.StaleSpan:
            return CommandOutcome(reason=REASON_STALE)
        if not can_display(changed):
            return CommandOutcome(reason=REASON_REJECTED)
        return self.record_decision(
            issue, "adjusted", applied_text=text, target=changed, replaced=issue.edit_span()
        )

    def ignore_issue(self, issue: sessions.LiveIssue | None) -> CommandOutcome:
        if issue is None:
            return CommandOutcome(reason=REASON_NOT_APPLICABLE)
        return self.record_decision(issue, "ignored")

    # -- document review ----------------------------------------------------

    def set_review_state(self, state: str, *, detail: str = "", request_id: str | None = None) -> None:
        self.review_state, self.review_detail, self.request_id = state, detail, request_id
        self.host.render_review_state()

    def cancel_pending(self) -> None:
        self.generation += 1
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()

    def start_review(self) -> None:
        self.cancel_pending()
        self.stop_semantic()
        generation = self.generation
        payload = self._edits.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        alignments = copy.deepcopy(payload["alignments"])
        self.host.clear_report()
        self.segment_failures.clear()
        self.semantic_state = "none"
        self.set_review_state("reviewing")
        self.task = asyncio.ensure_future(self.run_review(generation, source, target, direction, alignments))
        self.task.add_done_callback(lambda task: self.task_finished(task, generation))
        self.host.update_controls()

    async def run_review(
        self,
        generation: int,
        source: str,
        target: str,
        direction: str,
        alignments: list[dict[str, Any]] | None,
    ) -> None:
        try:
            capabilities = await self._api_client.get_capabilities()
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

    async def run_document_review(
        self,
        generation: int,
        source: str,
        target: str,
        direction: str,
        alignments: list[dict[str, Any]] | None,
    ) -> bool:
        features = tuple(self.review_features() or ())
        resolutions, context = self._edits.document_resolutions()
        while True:
            try:
                report = await self._api_client.review_pair(
                    source,
                    target,
                    direction,
                    alignments=alignments,
                    resolutions=resolutions or None,
                    resolution_context=context if resolutions else None,
                    **features_kw(features),
                )
            except ClientError as exc:
                if exc.code == ERROR_INVALID_REQUEST and resolutions and generation == self.generation:
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
        self._record_live_review_dictionary(report.get("dictionary") if isinstance(report, Mapping) else None)
        snapshot = {
            "source": source,
            "target": target,
            "direction": direction,
            "alignments": alignments,
            "report": report,
            "resolutions": list(resolutions),
        }
        candidate = copy_payload(self._edits.payload)
        candidate["history"] = (candidate["history"] + [snapshot])[-2:]
        try:
            serialize_workfile(candidate)
        except WorkfileError as exc:
            self.set_review_state("error", detail=strings.MANUSCRIPT_REPORT_UNSAVEABLE)
            if exc.code == "workfile_duplicate_choices":
                self.host.report_unstorable_report(exc.code)
            return False
        self._edits.payload = candidate
        self._edits.trusted_snapshots.append(copy.deepcopy(snapshot))
        self.host.trust_review_snapshot(snapshot)
        self._edits.document_revision += 1
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

    # -- Layer 1: deterministic structure checks ----------------------------

    def structure_running(self) -> bool:
        return self.structure_task is not None and not self.structure_task.done()

    def structure_plan(self) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
        payload = self._edits.payload
        return structure.plan(self._alignment.current_pairs(payload), payload["source"], payload["target"])

    def structure_views(self, pairs: Sequence[Any] | None = None) -> list[structure.View]:
        payload = self._edits.payload
        resolved = self._alignment.current_pairs(payload) if pairs is None else pairs
        return structure.views(resolved, payload["source"], payload["target"], self.structure)

    def cancel_structure_check(self) -> None:
        self.structure_generation += 1
        task, self.structure_task = self.structure_task, None
        self._structure_progress_status = None
        if task is not None and not task.done():
            task.cancel()
        if self.structure.phase == "running":
            self.structure.phase = "idle"

    def note_structure_progress(self, text: str) -> None:
        self._structure_progress_status = text

    def _abort_stale_structure_check(self) -> str | None:
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
            or "正在自动运行结构核查" in current
            or "正在自动运行第一层结构核查" in current
        ):
            return strings.MANUSCRIPT_L1_NOT_RUN
        return None

    def start_structure_check(self, *, restart: bool = False) -> bool:
        if self.structure_known_unavailable() or (self.structure_running() and not restart):
            return False
        self.cancel_structure_check()
        generation = self.structure_generation
        self.structure.phase, self.structure.error = "running", None
        self.structure_task = asyncio.ensure_future(self.run_structure_check(generation))
        self.host.render_structure()
        return True

    def _structure_snapshot(self) -> tuple[str, str, str, str, int]:
        payload = self._edits.payload
        return (
            payload["source"],
            payload["target"],
            payload["direction"],
            repr(payload["alignments"]),
            self._edits.document_revision,
        )

    async def run_structure_check(self, generation: int) -> None:
        status_note: str | None = None
        snapshot = self._structure_snapshot()
        try:
            if self.capabilities is None:
                self.capabilities = await self._api_client.get_capabilities()
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
                response = await self._api_client.structure_check(body)
                if generation != self.structure_generation:
                    return
                if self._structure_snapshot() != snapshot:
                    status_note = self._abort_stale_structure_check()
                    return
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
                    pairs = self._alignment.current_pairs(self._edits.payload)
                    mismatched = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
                    if mismatched > 0:
                        status_note += strings.MANUSCRIPT_TRANSLATE_TM_MISMATCH_NOTE.format(count=mismatched)
                    self.host.report_status(status_note)
                self.host.update_controls()

    # -- segmented term & semantic review -----------------------------------

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
        self.semantic_generation += 1
        task, self.semantic_task = self.semantic_task, None
        self.session.pending = None
        if task is not None and not task.done():
            task.cancel()
        if state is not None:
            self.semantic_state = state

    def clear_semantic(self) -> None:
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

    def next_review(self, attempted: set[Any]) -> sr.NextReview | None:
        payload = self._edits.payload
        pairs = self._alignment.current_pairs(payload)
        terms = self.term_mode == "segments" and self._edits.records(self.term_basis) is not None
        return sr.next_review(
            payload,
            pairs,
            self.term_basis,
            self.session,
            self.basis(),
            terms=terms,
            attempted=attempted,
            views=self.record_views,
            features=self.review_features(),
        )

    def ready_pair_for(self, result: sessions.SegmentResult) -> Any:
        payload = self._edits.payload
        source, target = payload["source"], payload["target"]
        return next(
            (p for p in self._alignment.current_pairs(payload) if p.status == STATUS_READY and result.matches(p, source, target)),
            None,
        )

    def rebuild_plan(self) -> None:
        payload = self._edits.payload
        source, target = payload["source"], payload["target"]
        pairs = self._alignment.current_pairs(payload)
        self.session.rebuild(pairs, source, target, self.basis())
        keys = {self._alignment.segment_key(p, payload) for p in pairs if p.status == STATUS_READY}
        self.segment_failures = {k: v for k, v in self.segment_failures.items() if k in keys}
        self.segment_notes = {k: v for k, v in self.segment_notes.items() if k in keys}

    async def check_term(self, index: int, generation: int) -> ClientError | str | None:
        features = self.review_features()
        request = sr.plan_request(self._edits.payload, index, self.term_basis, features=features)
        fell_back = False

        def still_there() -> bool:
            records = self._edits.records(self.term_basis) if self.term_mode == "segments" else None
            if records is None or not 0 <= index < len(records):
                return False
            record, payload = records[index], self._edits.payload
            return (
                payload["direction"] == request.direction
                and payload["source"][record["source_start"]:record["source_end"]] == request.source
                and payload["target"][record["target_start"]:record["target_end"]] == request.target
            )

        for _ in range(3):
            try:
                report = await self._api_client.review_pair(
                    request.source,
                    request.target,
                    request.direction,
                    alignments=request.alignments,
                    resolutions=request.resolutions or None,
                    resolution_context=request.resolution_context,
                    **features_kw(request.features),
                )
            except ClientError as exc:
                if (
                    exc.code == ERROR_INVALID_REQUEST
                    and request.resolutions
                    and not fell_back
                    and generation == self.semantic_generation
                ):
                    fell_back = True
                    if not still_there():
                        return "changed"
                    request = sr.plan_request(
                        self._edits.payload, index, self.term_basis, allow_resolutions=False, features=features
                    )
                    continue
                return exc
            if generation != self.semantic_generation:
                return "changed"
            self._record_live_review_dictionary(report.get("dictionary") if isinstance(report, Mapping) else None)
            applied = sr.apply_response(self._edits.payload, request, report, self.term_basis)
            self.term_basis = applied.basis
            self._edits.put_parts(applied.parts)
            self._edits.document_revision += 1
            if applied.outcome == "discarded":
                return "changed"
            if applied.outcome == "not_storable":
                return "not_storable"
            if not applied.again:
                return None
            request = sr.plan_request(self._edits.payload, index, self.term_basis, features=features)
        return None

    async def run_segments(self, generation: int) -> None:
        from ..segmentation import pair_is_sendable

        attempted: set[tuple[Any, ...]] = set()
        failure_streak: tuple[str | None, int] = (None, 0)
        self.rebuild_plan()
        if self.term_mode == "segments":
            self.host.show_segment_term_report()
        while generation == self.semantic_generation:
            next_review = self.next_review(attempted)
            if next_review is None:
                break
            payload = self._edits.payload
            pairs = self._alignment.current_pairs(payload)
            pair = pairs[next_review.index]
            source, target, direction = payload["source"], payload["target"], payload["direction"]
            key = self._alignment.segment_key(pair, payload)
            attempted.add(key)
            if not pair_is_sendable(pair, source, target):
                self.segment_failures[key] = "needs_manual_split"
                continue
            need_term, need_semantic = next_review.term, next_review.semantic
            self.session.pending = sessions.SegmentResult(
                sent_source=key[4],
                sent_target=key[5],
                source_core=(pair.source.core_start, pair.source.core_end),
                target_core=(pair.target.core_start, pair.target.core_end),
                hard_cut=pair.hard_cut,
                report=None,
                index=pair.index,
            )
            before = self._alignment.context_for(payload, pair)
            precedents = (
                semantic.select_precedents(
                    sessions.precedent_candidates(payload, pairs), key[4], pair.source.core_start
                )
                if self.host.precedents_selected() and need_semantic
                else []
            )
            self.host.render_semantic_progress()
            report = None
            error: ClientError | str | None = None
            if need_term:
                error = await self.check_term(pair.index, generation)
                if generation != self.semantic_generation:
                    return
                if error == "changed":
                    error = None
                if error is None:
                    self.segment_failures.pop(key, None)
                    self.host.show_segment_term_report()
            try:
                if error is None and need_semantic and generation == self.semantic_generation:
                    report = await self._api_client.semantic_review(
                        key[4], key[5], direction, before=before, precedents=precedents
                    )
            except ClientError as exc:
                error = exc
            if generation != self.semantic_generation:
                return
            pending, self.session.pending = self.session.pending, None
            target_pair = self.ready_pair_for(pending) if pending is not None else None
            if target_pair is not None:
                key = self._alignment.segment_key(target_pair, self._edits.payload)
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
                    self.semantic_detail = (
                        strings.MANUSCRIPT_TERM_NOT_STORABLE if code == "not_storable" else error.message
                    )
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

    def store_semantic(
        self,
        pair: Any,
        pending: sessions.SegmentResult,
        report: semantic.SemanticReviewReport,
        direction: str,
    ) -> None:
        payload = self._edits.payload
        source, target = payload["source"], payload["target"]
        if report["status"] != "complete":
            return
        result = self.session.result_for(pair, source, target)
        if result is None:
            result = sessions.SegmentResult(
                sent_source=pending.sent_source,
                sent_target=pending.sent_target,
                source_core=pending.source_core,
                target_core=pending.target_core,
                hard_cut=pending.hard_cut,
                report=None,
                index=pair.index,
            )
            self.session.add(result)
        key = self._alignment.segment_key(pair, payload)
        result.report, result.index = report, pair.index
        result.issues = sessions.live_issues_from_report(
            report,
            segment_index=pair.index,
            source_offset=result.source_core[0],
            target_offset=result.target_core[0],
            source_revision=sessions.revision(source),
            target_revision=sessions.revision(target),
        )
        self.known_texts.setdefault(sessions.revision(source), source)
        self.known_texts.setdefault(sessions.revision(target), target)
        notes: list[str] = []
        if report["omitted_issues"]:
            notes.append(strings.MANUSCRIPT_SEGMENT_HINT["truncated"].format(count=report["omitted_issues"]))
        if report["dropped_unanchored"]:
            notes.append(strings.MANUSCRIPT_SEGMENT_HINT["dropped"].format(count=report["dropped_unanchored"]))
        self.segment_notes[key] = notes
        sessions.apply_reconcile(
            self.session.issues(), self._edits.decisions(), self.texts_for_reconcile(), source, target
        )
        self.remember_semantic_report(direction)
        if not self.basis().needs(result):
            self.segment_failures.pop(key, None)
        self.host.refresh_semantic()
        self.host.refresh_segments_if_shown()
        self.host.update_controls()

    def remember_semantic_report(self, direction: str) -> None:
        payload = self._edits.payload
        source, target = payload["source"], payload["target"]
        segments = [
            {
                "segment_index": r.index,
                "source_start": r.source_core[0],
                "source_end": r.source_core[1],
                "target_start": r.target_core[0],
                "target_end": r.target_core[1],
                "report": r.report,
            }
            for r in self.session.results
            if sessions.semantic_part_valid(r) and self.ready_pair_for(r) is not None
        ]
        snapshot = {
            "source": source,
            "target": target,
            "direction": direction,
            "segments": copy.deepcopy(segments[:MAX_HISTORY_SEGMENTS]),
        }
        candidate = copy_payload(payload)
        history = list(candidate.get("semantic_history", []))
        if self.history_written == self.history_run and history:
            history[-1] = snapshot
        else:
            history.append(snapshot)
        candidate["semantic_history"] = history[-MAX_SEMANTIC_HISTORY:]
        candidate.setdefault("semantic_decisions", [])
        candidate["semantic_texts"] = self.semantic_texts(candidate)
        if semantic_payload_fits(normalize_semantic(candidate)):
            self._edits.payload = candidate
            self.history_written = self.history_run
            self._edits.document_revision += 1
