"""Pure-Python translation execution runner with TM incremental prefill (no Qt, no HTTP).

Owns whole-manuscript translation, partial-prefix continuation, single-segment
translation/retranslation, dictionary-identity provenance probing before reuse,
and trusted translation-memory prefill transactions.
"""
from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .. import semantic, strings
from .. import semantic_session as sessions
from .. import translation_memory as tm
from ..document.alignment_state import AlignmentState
from ..document.edit_store import DocumentEditStore
from ..document_edits import (
    MAX_SPAN_SCALARS,
    AlignmentRemap,
    build_target_fill_transaction,
    remap_for_target_fill,
    remap_splice,
)
from ..errors import ERROR_CANCELLED, ClientError
from ..segmentation import (
    SEGMENT_LIMIT,
    STATUS_NEEDS_MANUAL_SPLIT,
    STATUS_READY,
    STATUS_UNTRANSLATED,
    WHITESPACE,
    _core as seg_core,
    _paragraph_ranges as seg_paragraph_ranges,
    segment_text,
)
from .review_coordinator import QUOTA_CODES

TRANSLATION_RESUMABLE = ("paused", "failed")
TRANSLATION_IDLE = "idle"
TRANSLATION_PROBING = "probing"


def blank(text: str) -> bool:
    return all(char in WHITESPACE for char in text)


@dataclass
class Translation:
    """One translation run and, once it stops, the cursor to resume it."""

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


class TranslationRunner:
    """Runs piece-by-piece translation, retranslation, continuation, and TM prefill."""

    def __init__(
        self,
        *,
        get_api_client: Callable[[], Any],
        host: Any,
        edits: DocumentEditStore,
        alignment: AlignmentState,
        translation_memory: tm.TranslationMemory | None,
        tm_provenance: Callable[[], tuple[str | None, str | None]],
        refresh_identity: Callable[[], Awaitable[Any]],
        set_capabilities: Callable[[frozenset[str]], None],
        structure_known_unavailable: Callable[[], bool],
        start_structure_check: Callable[..., bool],
        note_structure_progress: Callable[[str], None],
    ) -> None:
        self._get_api_client = get_api_client
        self.host = host
        self._edits = edits
        self._alignment = alignment
        self.translation_memory = translation_memory
        self._tm_provenance = tm_provenance
        self._refresh_identity = refresh_identity
        self._set_capabilities = set_capabilities
        self._structure_known_unavailable = structure_known_unavailable
        self._start_structure_check = start_structure_check
        self._note_structure_progress = note_structure_progress

        self.translate_generation = 0
        self.translate_task: asyncio.Task | None = None
        self.provenance_task: asyncio.Task | None = None
        self.translate_state = TRANSLATION_IDLE
        self.translate_run: Translation | None = None
        self.translate_status = ""
        self.translation_withdrawn = False
        self.last_tm_translation_summary: dict[str, Any] | None = None

    @property
    def _api_client(self) -> Any:
        return self._get_api_client()

    def client_configured(self) -> bool:
        client = self._api_client
        if hasattr(client, "is_configured"):
            return bool(client.is_configured)
        if hasattr(client, "configured"):
            return bool(client.configured)
        return True

    def translating(self) -> bool:
        return self.translate_task is not None and not self.translate_task.done()

    def stop_translation(self) -> None:
        self.translate_generation += 1
        task, self.translate_task = self.translate_task, None
        self.provenance_task = None
        self.translate_run, self.translate_state = None, TRANSLATION_IDLE
        if task is not None and not task.done():
            task.cancel()

    def cancel_provenance_probe_for_credential_change(self) -> None:
        probe, self.provenance_task = self.provenance_task, None
        if probe is not None and not probe.done():
            self.translate_generation += 1
            if self.translate_task is probe:
                self.translate_task = None
            self.translate_run = None
            self.translate_state = TRANSLATION_IDLE
            probe.cancel()

    def resumable(self) -> Translation | None:
        run = self.translate_run
        if run is None or self.translate_state not in TRANSLATION_RESUMABLE or self.translating():
            return None
        payload = self._edits.payload
        same = (
            payload["source"] == run.source
            and payload["direction"] == run.direction
            and payload["target"] == run.target
        )
        return run if same else None

    def resumable_partial_prefix(self) -> tuple[int, str, int, str, tuple[str, str]] | None:
        payload = self._edits.payload
        source, target = payload["source"], payload["target"]
        if payload["alignments"] is not None or blank(source) or blank(target):
            return None
        if len(segment_text(source)) < 2:
            return None
        if any(p.status == STATUS_UNTRANSLATED for p in self._alignment.current_pairs(payload)):
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
        payload = self._edits.payload
        if self.translation_withdrawn or blank(payload["source"]) or blank(payload["target"]):
            return False
        if any(p.status == STATUS_UNTRANSLATED for p in self._alignment.current_pairs(payload)):
            return True
        return self.resumable_partial_prefix() is not None

    def withdraw_translation_offer(self) -> str | None:
        if self.translate_state in TRANSLATION_RESUMABLE and self.resumable() is None:
            previous = self.translate_status
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.translation_withdrawn = True
            return previous
        return None

    def _translation_ui(self) -> None:
        self.host.refresh_segments_if_shown()
        self.host.update_controls()

    @staticmethod
    def tm_revision_label(revision: str | None) -> str:
        if not revision:
            return strings.TM_NOT_RECORDED
        return revision if len(revision) <= 16 else revision[:12]

    def _tm_l1_status_note(self, l1_started: bool) -> str:
        if l1_started:
            return strings.MANUSCRIPT_TRANSLATE_TM_L1_RUNNING
        if not self.client_configured():
            return strings.MANUSCRIPT_TRANSLATE_TM_L1_OFFLINE
        if self._structure_known_unavailable():
            return strings.MANUSCRIPT_TRANSLATE_TM_L1_UNAVAILABLE
        return ""

    def classify_tm_sources(
        self,
        sources: Sequence[str],
        direction: str,
        terms_rev: str | None,
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
    def count_translation_pieces(text: str) -> int:
        return sum(1 for piece in segment_text(text, SEGMENT_LIMIT) if piece.core_end > piece.core_start)

    def baseline_requests_before_tm(self) -> int:
        payload = self._edits.payload
        source, target = payload["source"], payload["target"]
        if blank(target):
            return self.count_translation_pieces(source)
        if payload["alignments"] is not None:
            return sum(
                self.count_translation_pieces(source[p.source.core_start:p.source.core_end])
                for p in self._alignment.current_pairs(payload)
                if p.status == STATUS_UNTRANSLATED
            )
        partial = self.resumable_partial_prefix()
        if partial is not None:
            return self.count_translation_pieces(partial[1])
        return 0

    def _apply_tm_fill_transaction(
        self,
        raw_target: str,
        alignments: list[dict[str, Any]] | None,
        fills: Sequence[tuple[int, int, str, int, int]],
    ) -> bool:
        current, edits, combined_remap = build_target_fill_transaction(
            self._edits.payload["source"],
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
        value = self._edits.draft_snapshot()
        value["target"] = current
        value["alignments"] = combined_remap.alignments
        outcome = self._edits.apply_edit(value, edits=edits, remap=combined_remap)
        return outcome.committed

    def _prefill_from_translation_memory(self, terms_rev: str, game_ver: str | None) -> tuple[bool, int]:
        memory = self.translation_memory
        if memory is None or not memory.usable:
            return False, 0
        payload = self._edits.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        if blank(source):
            return False, 0
        try:
            if payload["alignments"] is not None:
                pairs = self._alignment.current_pairs(payload)
                if not pairs:
                    return False, 0
                sources = [source[p.source.core_start:p.source.core_end] for p in pairs]
                classes = self.classify_tm_sources(sources, direction, terms_rev, game_ver)
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
                classes = self.classify_tm_sources(sources, direction, terms_rev, game_ver)
                matched = [
                    i
                    for i, item in enumerate(classes)
                    if item is not None
                    and item.status == tm.TM_STATUS_CONTEXT_EXACT_FRESH
                    and item.match is not None
                    and item.match.entry.target_text.strip()
                ]
                if not matched:
                    return False, 0
                if len(para_cores) > 64:
                    if len(matched) == len(para_cores):
                        changed = "\n\n".join(classes[i].match.entry.target_text for i in matched)  # type: ignore[union-attr]
                        ok = self.host.apply_target_write(
                            changed, ("target", 0, 0, len(changed)), remap=AlignmentRemap(None)
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
                classes = self.classify_tm_sources(sources, direction, terms_rev, game_ver)
                matched = [
                    i
                    for i in range(len(t_cores), len(s_cores))
                    if classes[i] is not None
                    and classes[i].status == tm.TM_STATUS_CONTEXT_EXACT_FRESH  # type: ignore[union-attr]
                    and classes[i].match is not None
                    and classes[i].match.entry.target_text.strip()  # type: ignore[union-attr]
                ]
                if not matched:
                    return False, 0
                base_alignments = [
                    {
                        "source": {"start": cs, "end": ce, "text": source[cs:ce]},
                        "target": {"start": tcs, "end": tce, "text": target[tcs:tce]},
                    }
                    for (cs, ce), (tcs, tce) in zip(s_cores[: len(t_cores)], t_cores, strict=True)
                ] + [
                    {"source": {"start": cs, "end": ce, "text": source[cs:ce]}, "target": None}
                    for cs, ce in s_cores[len(t_cores) :]
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
        pairs: Sequence[Any],
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
        rev_label = self.tm_revision_label(terms_rev)
        l1_note = self._tm_l1_status_note(l1_started)
        tail_parts: list[str] = []
        if mismatched > 0:
            tail_parts.append(strings.MANUSCRIPT_TRANSLATE_TM_MISMATCH_NOTE.format(count=mismatched))
        if pending_other > 0:
            tail_parts.append(strings.MANUSCRIPT_TRANSLATE_TM_PENDING_NOTE.format(count=pending_other))
        tail_note = "".join(tail_parts)
        if translated == 0:
            return strings.MANUSCRIPT_TRANSLATE_TM_ALL_REUSED.format(
                reused=reused, baseline=baseline_requests, revision=rev_label, l1_note=l1_note, tail_note=tail_note
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
        if self._needs_dictionary_identity(single_segment=single_segment):
            self._launch_translation_after_identity(single_segment)
            return
        self._start_translation(single_segment=single_segment)

    def _needs_dictionary_identity(self, *, single_segment: bool) -> bool:
        if single_segment:
            return False
        memory = self.translation_memory
        if memory is None or not memory.usable:
            return False
        if not self.client_configured():
            return False
        if not (blank(self._edits.payload["target"]) or not self.translation_withdrawn):
            return False
        resumed, _pair, _wanted = self._resume_for_press()
        return resumed is None

    def _resume_for_press(self) -> tuple[Translation | None, Any, str | None]:
        cursor = self.resumable()
        if cursor is None:
            return None, None, None
        if cursor.segment is None:
            return cursor, None, None
        wanted = STATUS_READY if cursor.retranslate else STATUS_UNTRANSLATED
        pair = next(
            (p for p in self._alignment.current_pairs(self._edits.payload) if p.status == wanted and p.source.core_start == cursor.segment),
            None,
        )
        if pair is None:
            return None, None, None
        return cursor, pair, wanted

    def _launch_translation_after_identity(self, single_segment: bool) -> None:
        current = self.translate_task
        if current is not None and current is self.provenance_task and not current.done():
            return
        payload = self._edits.payload
        basis = (
            payload["source"],
            payload["target"],
            payload["direction"],
            copy.deepcopy(payload["alignments"]),
        )
        probe = asyncio.ensure_future(self._start_translation_after_identity(single_segment, basis))
        self.provenance_task = probe
        self.translate_task = probe
        self.translate_state = TRANSLATION_PROBING
        self.host.report_status(strings.MANUSCRIPT_TRANSLATE_PROVENANCE)
        self._translation_ui()

    async def _start_translation_after_identity(self, single_segment: bool, basis: tuple[Any, ...]) -> None:
        generation = self.translate_generation
        await self._refresh_identity()
        if generation != self.translate_generation:
            return
        if self.provenance_task is asyncio.current_task():
            self.provenance_task = None
        if self.translate_task is asyncio.current_task():
            self.translate_task = None
        payload = self._edits.payload
        if (payload["source"], payload["target"], payload["direction"], payload["alignments"]) != basis:
            self.translate_run, self.translate_state = None, TRANSLATION_IDLE
            self.host.report_status(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            self._translation_ui()
            return
        self._start_translation(single_segment=single_segment)
        if self.translate_state == TRANSLATION_PROBING:
            self.translate_state = TRANSLATION_IDLE
            self._translation_ui()

    def _start_translation(self, *, single_segment: bool = False) -> None:
        payload = self._edits.payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]
        cursor, pair, _wanted = self._resume_for_press()

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
                    tm_baseline_requests = self.baseline_requests_before_tm()
                    rejected, tm_reused = self._prefill_from_translation_memory(terms_rev, game_ver)
                    if rejected:
                        self.host.report_status(strings.MANUSCRIPT_EDIT_REJECTED)
                        self._translation_ui()
                        return
                    if tm_reused > 0:
                        tm_revision, tm_game_version = terms_rev, game_ver
                        payload = self._edits.payload
                        source, target, direction = payload["source"], payload["target"], payload["direction"]

        if tm_reused > 0:
            self.stop_translation()
            self.translation_withdrawn = False
            pairs = self._alignment.current_pairs(payload)
            remaining = [p for p in pairs if p.status == STATUS_UNTRANSLATED]
            if not remaining:
                mismatched = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
                self.host.report_translation_complete(mismatched)
                l1_started = self._start_structure_check(restart=True) if self.client_configured() else False
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
                    self._note_structure_progress(summary_text)
                self._translation_ui()
                return
            pair = remaining[0]
            batch_spans = [(p.source.core_start, p.source.core_end) for p in remaining]
            planned_requests = sum(
                self.count_translation_pieces(source[p.source.core_start:p.source.core_end])
                for p in remaining
            )
            self.host.report_status(
                strings.MANUSCRIPT_TRANSLATE_TM_PREFILL.format(
                    reused=tm_reused,
                    remaining=len(batch_spans),
                    planned=planned_requests,
                    baseline=tm_baseline_requests,
                    revision=self.tm_revision_label(tm_revision),
                )
            )
        elif cursor is None and blank(target):
            self.translation_withdrawn = False
        elif cursor is None:
            selected = self.host.selected_segment()
            pairs = self._alignment.current_pairs(payload)
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
                source[run.source_base:]
                if run.source_base
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

        pairs = self._alignment.current_pairs(payload)
        run = Translation(
            generation=self.translate_generation,
            source=source,
            direction=direction,
            target=target,
            segment=None if pair is None else pair.source.core_start,
            previous=self._alignment.adjacent_before_context(payload, pairs, pair) if pair is not None else None,
        )
        text = source if pair is None else source[pair.source.core_start:pair.source.core_end]
        pieces = segment_text(text, SEGMENT_LIMIT)
        run.count = sum(1 for piece in pieces if piece.core_end > piece.core_start)
        self.translate_run, self.translate_state = run, "waiting"
        self.translate_task = asyncio.ensure_future(self.run_translation(run, pair, text, pieces))
        self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
        self._translation_ui()

    def _launch_retranslate(self, pair: Any) -> None:
        self.stop_translation()
        self.translation_withdrawn = False
        payload = self._edits.payload
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
            previous=self._alignment.adjacent_before_context(payload, self._alignment.current_pairs(payload), pair),
            count=sum(1 for piece in pieces if piece.core_end > piece.core_start),
        )
        self.translate_run, self.translate_state = run, "waiting"
        self.translate_task = asyncio.ensure_future(self.run_translation(run, pair, text, pieces))
        self.translate_task.add_done_callback(lambda _task: self.translation_done(run))
        self._translation_ui()

    def retranslate_segment(self, pair: Any = None) -> None:
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
        payload = self._edits.payload
        run = Translation(
            generation=self.translate_generation,
            source=payload["source"],
            direction=payload["direction"],
            target=payload["target"],
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
            capabilities = await self._api_client.get_capabilities()
            self._set_capabilities(capabilities)
            context_on = semantic.CAPABILITY_TRANSLATION_CONTEXT in capabilities
            payload = self._edits.payload
            candidates = (
                sessions.precedent_candidates(payload, self._alignment.current_pairs(payload))
                if context_on and self.host.precedents_selected()
                else []
            )
            template = strings.MANUSCRIPT_TRANSLATE_RETRYING if run.retry else strings.MANUSCRIPT_TRANSLATING
            while run.batch_remaining:
                core_start, _core_end = run.batch_remaining[0]
                payload = self._edits.payload
                pairs = self._alignment.current_pairs(payload)
                pair = next(
                    (p for p in pairs if p.status == STATUS_UNTRANSLATED and p.source.core_start == core_start),
                    None,
                )
                if pair is None:
                    run.batch_remaining.pop(0)
                    continue
                ctx_previous = self._alignment.adjacent_before_context(payload, pairs, pair)
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
                        context = {
                            **({"before": before} if before is not None else {}),
                            **({"precedents": precedents} if precedents else {}),
                        } or None
                    run.tm_model_calls += 1
                    result = await self._api_client.translate(core, to=run.direction, context=context)
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
                run.target = self._edits.payload["target"]
                run.source = self._edits.payload["source"]
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
        pairs = self._alignment.current_pairs(self._edits.payload)
        mismatched = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
        self.host.report_translation_complete(mismatched)
        if run.tm_reused > 0:
            l1_started = self._start_structure_check(restart=True) if self.client_configured() else False
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
                self._note_structure_progress(summary_text)
        self.host.refresh_segments_if_shown()

    def translation_done(self, run: Translation) -> None:
        if run.generation == self.translate_generation and self.translate_state in ("waiting", "retrying"):
            self.translation_stopped(run, "failed", strings.ERROR_MSG_UNKNOWN)
        self._translation_ui()

    async def run_translation(self, run: Translation, pair: Any, text: str, pieces: list[Any]) -> None:
        generation = run.generation
        base = run.source_base if pair is None else pair.source.core_start
        try:
            capabilities = await self._api_client.get_capabilities()
            self._set_capabilities(capabilities)
            context_on = semantic.CAPABILITY_TRANSLATION_CONTEXT in capabilities
            payload = self._edits.payload
            candidates = (
                sessions.precedent_candidates(payload, self._alignment.current_pairs(payload))
                if context_on and self.host.precedents_selected()
                else []
            )
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
                    context = {
                        **({"before": before} if before is not None else {}),
                        **({"precedents": precedents} if precedents else {}),
                    } or None
                result = await self._api_client.translate(core, to=run.direction, context=context)
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
        mismatched = sum(
            1 for p in self._alignment.current_pairs(self._edits.payload) if p.status == STATUS_NEEDS_MANUAL_SPLIT
        )
        self.host.report_translation_complete(mismatched)
        if run.retranslate and not mismatched:
            self.host.report_status(strings.MANUSCRIPT_RETRANSLATED)
        self.host.refresh_segments_if_shown()

    def translation_basis_unchanged(self, run: Translation) -> bool:
        payload = self._edits.payload
        return (
            payload["source"] == run.source
            and payload["direction"] == run.direction
            and payload["target"] == run.target
        )

    def write_translation(self, run: Translation, added: str, pair: Any = None) -> bool:
        target = run.target
        payload = self._edits.payload
        current_alignments = payload["alignments"]
        source = payload["source"]
        if pair is None:
            if run.append_start is not None and not run.written:
                inserted = run.append_prefix + added
                changed = target[:run.append_start] + inserted
                edit = ("target", run.append_start, len(target), len(inserted))
            else:
                start = len(target) if run.written else 0
                changed = (target if run.written else "") + added
                edit = ("target", start, len(target), len(added))
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
                remap = remap_for_target_fill(
                    current_alignments,
                    source,
                    changed,
                    pair.source.start,
                    pair.source.end,
                    position,
                    fill_start,
                    fill_end,
                    len(inserted),
                )
            else:
                remap = remap_splice(
                    current_alignments, "target", source, changed, position, position, len(inserted)
                )
        return self.host.apply_target_write(changed, edit, remap=remap)

    def translation_stopped(
        self,
        run: Translation | None,
        state: str,
        reason: str = "",
        *,
        quota: bool = False,
    ) -> None:
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
                run.target, run.written = self._edits.payload["target"], True
                if run.append_start is not None:
                    run.append_start = None
                    run.append_prefix = ""
            run.out = []
        if state == "failed":
            text = (
                strings.MANUSCRIPT_TRANSLATE_SEGMENT_FAILED if segment else strings.MANUSCRIPT_TRANSLATE_FAILED
            ).format(index=run.done + 1, total=run.count, done=run.done, reason=reason)
        else:
            if segment:
                template = (
                    strings.MANUSCRIPT_TRANSLATE_SEGMENT_QUOTA if quota else strings.MANUSCRIPT_TRANSLATE_SEGMENT_PAUSED
                )
            else:
                template = strings.MANUSCRIPT_TRANSLATE_QUOTA if quota else strings.MANUSCRIPT_TRANSLATE_PAUSED
            text = template.format(done=run.done, total=run.count)
        self.translate_run, self.translate_state, self.translate_status = run, state, text
        self.host.report_status(text)
        self._translation_ui()
