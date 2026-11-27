"""The translation memory inside the workbench, on real widgets, offscreen.

The store is a real SQLite file in a temporary folder. What these tests hold
the wiring to is the part a later change could quietly break:

* the panel lists what was confirmed for the segment at the caret, with the
  kind of match and with the reasons it may no longer hold;
* adopting a candidate is one edit, and Ctrl+Z puts the text back;
* nothing reaches the memory until the person confirms the segment - a
  translated or edited segment never stores itself;
* the Edit menu opens the manager;
* the reuse check counts fresh, stale and unmatched segments and fills only the
  empty targets, as one undoable edit.

The pairing is explicit alignments: a segment with no translation is a segment
of its own (a ready pair and an untranslated one never merge), so a test's
boundaries are its own.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from wuwaterm_client import strings
from wuwaterm_client import translation_memory as tm
from wuwaterm_client.api import ApiClient, DictionaryIdentityResult, MetaResult
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.semantic_session import SegmentResult
from wuwaterm_client.ui.main_window import MainWindow
from wuwaterm_client.ui.manuscript_view import ManuscriptView
from wuwaterm_client.ui.tm_dialog import TranslationMemoryDialog

STAMP = "2026-11-30T00:00:00Z"
# The dictionary revision a review names; the memory compares entries with it.
REVISION = hashlib.sha256(b"dictionary").hexdigest()
OTHER_REVISION = hashlib.sha256(b"other dictionary").hexdigest()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def memory(tmp_path) -> tm.TranslationMemory:
    return tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)


def alignments_for(source: str, target: str, pairs: list[tuple[str, str]]) -> list[dict]:
    """Explicit alignments, so a test's segment boundaries are its own."""
    out = []
    for source_text, target_text in pairs:
        source_start = source.index(source_text)
        span = None
        if target_text:
            target_start = target.index(target_text)
            span = {"start": target_start, "end": target_start + len(target_text), "text": target_text}
        out.append({
            "source": {"start": source_start, "end": source_start + len(source_text), "text": source_text},
            "target": span,
        })
    return out


def install(view: ManuscriptView, source: str, target: str, *, direction: str = "en",
            alignments: list[dict] | None = None, list_segments: bool = True) -> None:
    """Put a document in the view. ``list_segments`` False leaves the segment
    list unbuilt, as it is while the terms track is the one showing."""
    value = empty_workfile()
    value.update(source=source, target=target, direction=direction, alignments=alignments)
    view._set_payload(value)
    view._update_controls()
    if list_segments:
        view._refresh_segments()
    view._refresh_tm_panel()


def two_segments() -> tuple[str, str, list[dict]]:
    """The first line translated, the second not: two segments of their own."""
    source = "First line. Second line."
    target = "甲句。"
    return source, target, alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])


# -- the panel: what the memory holds for the active segment ----------------------


def test_the_panel_lists_the_confirmed_pair_for_the_segment_at_the_caret(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        panel = view.tm_panel
        assert len(panel.matches) == 1
        card = panel.hit_texts()[0]
        # Same text, same neighbour, matching dictionary revision: trusted reuse, no warning.
        assert strings.TM_MATCH_CONTEXT_EXACT in card
        assert strings.TM_BADGE_TRUSTED_REUSE in card
        assert "首句。" in card
        assert strings.TM_STALE_TERMS not in card and strings.TM_STALE_NUMERIC not in card
        assert panel.matches[0].freshness == tm.TM_FRESHNESS_VERIFIED
        assert panel.matches[0].needs_recheck is False
        assert panel.confirm_button.isEnabled()
    finally:
        view.close()


def test_a_changed_number_is_shown_as_a_reference_to_check(qapp, memory) -> None:
    source = "First line, +7 ATK. Second line."
    target = "首句，攻击 +7。"
    alignments = alignments_for(source, target, [("First line, +7 ATK.", "首句，攻击 +7。"), ("Second line.", "")])
    memory.add("First line, +5 ATK.", "首句，攻击 +5。", "en", next_source="Second line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        match = view.tm_panel.matches[0]
        assert match.match_kind == tm.TM_MATCH_NUMERIC_VARIANT
        card = view.tm_panel.hit_texts()[0]
        assert strings.TM_MATCH_NUMERIC in card
        assert strings.TM_STALE_NUMERIC in card
        assert strings.TM_MATCH_RECHECK.format(reasons=strings.TM_STALE_NUMERIC) in card
        assert match.needs_recheck is True
    finally:
        view.close()


def test_an_entry_confirmed_under_another_dictionary_says_the_revision_moved(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", origin="human_confirmed", terms_db_revision=OTHER_REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        # Nothing known yet: no revision to compare against.
        view._refresh_tm_panel()
        assert strings.TM_STALE_TERMS not in view.tm_panel.hit_texts()[0]
        # The live review dictionary names REVISION; the entry says another.
        view._record_live_review_dictionary({"revision": REVISION})
        assert strings.TM_STALE_TERMS in view.tm_panel.hit_texts()[0]
    finally:
        view.close()


def test_adopting_a_candidate_is_one_edit_that_ctrl_z_takes_back(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        # Listed, and nothing has changed: a match never replaces text by itself.
        assert view.target_edit.raw_text() == target
        view.tm_panel.apply_at(0)
        assert view.target_edit.raw_text() == "首句。"
        assert view._payload["target"] == "首句。"
        assert view.status_label.text() == strings.TM_PANEL_APPLIED
        view.target_edit.setFocus()
        QTest.keyClick(view.target_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert view.target_edit.raw_text() == target
        assert view._payload["target"] == target
    finally:
        view.close()


# -- the one way into the memory -------------------------------------------------


def test_nothing_is_stored_until_the_person_confirms_the_segment(qapp, memory) -> None:
    source, target, alignments = two_segments()
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        # Typing a translation is not a confirmation.
        view.target_edit.textCursor().insertText("。")
        view._refresh_derived()
        view._refresh_tm_panel()
        assert len(memory) == 0
        assert strings.TM_PANEL_NO_MATCH in view.tm_panel.notice_label.text()

        # Only the explicit button writes, and it writes what is on screen now.
        view._refresh_segments()
        view.segment_list.setCurrentRow(0)
        pair = view._current_segment()
        assert pair is not None
        shown = view._payload["target"][pair.target.core_start:pair.target.core_end]
        view.tm_panel.confirm_button.click()
        assert len(memory) == 1
        entry = memory.entries()[0]
        assert entry.origin == "human_confirmed"
        assert entry.source_text == "First line."
        assert entry.target_text == shown
        assert view.status_label.text() == strings.TM_PANEL_CONFIRMED.format(count=entry.use_count)
        # The panel now lists it for the segment.
        assert view.tm_panel.matches and view.tm_panel.matches[0].entry.id == entry.id
    finally:
        view.close()


def test_a_segment_without_a_translation_is_never_confirmed(qapp, memory) -> None:
    source, target, alignments = two_segments()
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        assert view._active_segment_context().target == ""
        # Nothing to confirm: the button is off, and the refusal behind it says why.
        assert view.tm_panel.confirm_button.isEnabled() is False
        view._confirm_segment_to_memory()
        assert len(memory) == 0
        assert view.status_label.text() == strings.TM_PANEL_CONFIRM_EMPTY
    finally:
        view.close()


def test_without_a_memory_the_panel_says_so_and_writes_nothing(qapp, tmp_path) -> None:
    view = ManuscriptView(ApiClient(None))
    try:
        assert view.tm_panel.memory is None
        install(view, *two_segments()[:2], alignments=two_segments()[2])
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        assert view.tm_panel.notice_label.text() == strings.TM_PANEL_NO_MEMORY
        assert view.tm_panel.confirm_button.isEnabled() is False
        view._confirm_segment_to_memory()
        assert view.status_label.text() == strings.TM_PANEL_NO_MEMORY
        assert list(tmp_path.iterdir()) == []
    finally:
        view.close()


def test_a_confirmed_pair_stays_out_of_the_saved_manuscript(qapp, memory, tmp_path, monkeypatch) -> None:
    source, target, alignments = two_segments()
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view.tm_panel.confirm_button.click()
        assert len(memory) == 1
        path = tmp_path / "draft.json"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
        assert view.save_document()
        text = path.read_text(encoding="utf-8")
        # The manuscript keeps its own keys and its own text; the memory is a
        # separate file beside config.json and nothing of it is written here.
        assert set(json.loads(text)) == set(empty_workfile())
        assert "tm_entries" not in text and "tm_schema" not in text
        assert memory.entries()[0].created_at not in text
    finally:
        view.close()


def test_a_segment_with_only_a_translation_is_not_read_as_a_broken_memory(qapp, memory) -> None:
    source = "First line."
    target = "甲句。Extra target text."
    alignments = alignments_for(source, target, [("First line.", "甲句。")])
    memory.add("First line.", "首句。", "en", origin="human_confirmed", terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        assert view._active_segment_context().source == ""
        # A blank source is not a lookup: the notice says so instead of
        # reporting the memory file as invalid.
        assert view.tm_panel.notice_label.text() == strings.TM_PANEL_NO_SOURCE
        assert view.tm_panel.matches == []
        assert view.tm_panel.confirm_button.isEnabled() is False
        view._check_memory_reuse()
        # It is skipped and counted, not reported as a broken memory file;
        # the single source segment has no context, so it counts as reference-only.
        assert view.tm_panel.summary.skipped == 1
        assert view.tm_panel.summary.fresh == 0
        assert view.tm_panel.summary.stale == 1
        assert strings.TM_PANEL_CHECK_SKIPPED.format(n=1, max=tm.SOURCE_SCALARS[1]) in view.status_label.text()
        assert strings.TM_PANEL_UNUSABLE.split("{")[0] not in view.status_label.text()
        assert view.tm_panel.check_button.isEnabled() is True
    finally:
        view.close()


# -- the fill: order, spans and one undo step -------------------------------------


def test_consecutive_untranslated_segments_are_filled_in_document_order(qapp, memory) -> None:
    """Two empty targets at one offset: the later translation can sort first."""
    source = "First line. Second line. Third line."
    target = "甲句。"
    alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", ""),
                                                 ("Third line.", "")])
    memory.add("First line.", "甲句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    memory.add("Second line.", "zulu.", "en", prev_source="First line.", next_source="Third line.",
               origin="human_confirmed", terms_db_revision=REVISION)
    memory.add("Third line.", "alpha.", "en", prev_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view._check_memory_reuse()
        assert view.tm_panel.summary.fillable == 2
        view._fill_from_memory()
        # Both segments are empty at target offset 3; the text must follow the
        # source order, not the translations' alphabetical order.
        assert view._payload["target"] == "甲句。zulu.alpha."
        view._refresh_derived()
        filled = view._payload["alignments"]
        assert [entry["target"]["text"] for entry in filled] == ["甲句。", "zulu.", "alpha."]
        assert all(entry["target"] is not None for entry in filled)
        assert "needs_manual_split" not in [pair.status for pair in view._current_pairs()]
        # One edit step: undo puts the single translated paragraph back.
        view.target_edit.setFocus()
        QTest.keyClick(view.target_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert view._payload["target"] == target
    finally:
        view.close()


def test_filling_keeps_every_reviewed_segment_span_on_its_own_text(qapp, memory) -> None:
    """Four segments, translated and untranslated alternating: the semantic
    session's spans must land on the same text after two insertions."""
    source = "Alpha line. Beta line. Gamma line. Delta line. Epsilon line."
    target = "甲句。丙句。戊句。"
    alignments = alignments_for(source, target, [("Alpha line.", "甲句。"), ("Beta line.", ""),
                                                 ("Gamma line.", "丙句。"), ("Delta line.", ""),
                                                 ("Epsilon line.", "戊句。")])
    memory.add("Beta line.", "zulu.", "en", prev_source="Alpha line.", next_source="Gamma line.",
               origin="human_confirmed", terms_db_revision=REVISION)
    memory.add("Delta line.", "alpha.", "en", prev_source="Gamma line.", next_source="Epsilon line.",
               origin="human_confirmed", terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        pairs = view._current_pairs()
        assert [pair.status for pair in pairs] == [
            "ready", "untranslated", "ready", "untranslated", "ready"]
        results = []
        for index in (0, 2, 4):
            pair = pairs[index]
            result = SegmentResult(
                sent_source=source[pair.source.core_start:pair.source.core_end],
                sent_target=target[pair.target.core_start:pair.target.core_end],
                source_core=(pair.source.core_start, pair.source.core_end),
                target_core=(pair.target.core_start, pair.target.core_end),
                hard_cut=pair.hard_cut, report=None, index=index)
            view._controller.session.add(result)
            results.append(result)
        view._check_memory_reuse()
        assert view.tm_panel.summary.fillable == 2
        view._fill_from_memory()
        assert view._payload["target"] == "甲句。zulu.丙句。alpha.戊句。"
        # Every reviewed span lies on its own translation in the new text.
        for result, text in zip(results, ("甲句。", "丙句。", "戊句。")):
            start, end = result.target_core
            assert view._payload["target"][start:end] == text
            assert result.invalid is False
        assert view._payload["alignments"][1]["target"]["text"] == "zulu."
        assert view._payload["alignments"][3]["target"]["text"] == "alpha."
    finally:
        view.close()


# -- adopting a candidate into an untranslated segment ----------------------------


def test_adopting_a_candidate_fills_an_untranslated_segment_into_its_alignment(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        assert view._active_segment_context().target == ""
        view.tm_panel.apply_at(0)
        # The pair is a fill, so its own alignment entry receives the text and
        # the segment does not fall back to needing a manual split.
        assert view._payload["alignments"][1]["target"] is not None
        assert view._payload["alignments"][1]["target"]["text"] == "乙句。"
        filled = view._payload["target"]
        assert filled.endswith("乙句。") and filled.startswith("甲句。")
        view._refresh_derived()
        assert "needs_manual_split" not in [pair.status for pair in view._current_pairs()]
        assert "乙句。" in view._payload["target"]
    finally:
        view.close()


# -- the panel follows the caret on every track -----------------------------------


def test_the_panel_follows_the_caret_while_the_terms_track_is_showing(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed")
    memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        # The terms track is the default; its segment list is never built, so
        # nothing but the caret follow can refresh the memory panel.
        install(view, source, target, alignments=alignments, list_segments=False)
        assert view.track_stack.currentIndex() == 0
        assert view._segment_rows_from is None
        view.target_edit.setFocus()
        view.target_edit.set_raw_cursor_offset(0)
        view._caret_side = "target"
        view._follow_caret()
        assert view.tm_panel.matches[0].entry.target_text == "首句。"
        view.target_edit.set_raw_cursor_offset(len("甲句。"))
        view._follow_caret()
        assert view.tm_panel.matches[0].entry.target_text == "乙句。"
    finally:
        view.close()


# -- a source the store cannot hold ------------------------------------------------


def test_a_segment_longer_than_the_memory_holds_is_not_a_broken_file(qapp, memory) -> None:
    limit = tm.SOURCE_SCALARS[1]
    long_source = "字" * (limit + 1)
    memory.add("First line.", "首句。", "en", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, long_source, "甲句。",
                alignments=[{"source": {"start": 0, "end": len(long_source), "text": long_source},
                             "target": {"start": 0, "end": 3, "text": "甲句。"}}])
        assert len(view._active_segment_context().source) == limit + 1
        view._refresh_tm_panel()
        assert view.tm_panel.notice_label.text() == strings.TM_PANEL_SOURCE_TOO_LONG.format(
            length=limit + 1, max=limit)
        assert strings.TM_PANEL_UNUSABLE.split("{")[0] not in view.tm_panel.notice_label.text()
        assert view.tm_panel.confirm_button.isEnabled() is False
        # The check still runs: it skips this segment and says so.
        assert view.tm_panel.check_button.isEnabled() is True
        view._check_memory_reuse()
        assert view.tm_panel.summary.skipped == 1
        assert strings.TM_PANEL_UNUSABLE.split("{")[0] not in view.status_label.text()
        # Confirming is refused in the workbench's words, not the file's.
        view._confirm_segment_to_memory()
        assert view.status_label.text() == strings.TM_PANEL_CONFIRM_TOO_LONG.format(
            length=limit + 1, max=limit)
        assert len(memory) == 1
    finally:
        view.close()


def test_the_reuse_check_classifies_the_segments_it_can_read(qapp, memory) -> None:
    limit = tm.SOURCE_SCALARS[1]
    memory.add("Alpha line.", "甲句。", "en", origin="human_confirmed", terms_db_revision=REVISION)
    memory.add("Gamma line.", "丙句。", "en", origin="human_confirmed", terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        classes = view._classify_reuse(["Alpha line.", "字" * (limit + 1), "Gamma line."])
        # The unreadable middle segment is skipped; the others are still read,
        # with no context on the side the skip took away.
        assert classes[1] is None
        assert [item.status for item in (classes[0], classes[2])] == ["exact_fresh", "exact_fresh"]
        assert classes[2].match.entry.target_text == "丙句。"
    finally:
        view.close()


def test_a_long_next_segment_is_no_context_not_a_broken_file(qapp, memory) -> None:
    """A short segment followed by a >2000-character tail: the neighbour the
    store cannot hash must not turn the panel's read or its confirm into an
    error about the file."""
    limit = tm.SOURCE_SCALARS[1]
    short = "First line."
    long_tail = "字" * (limit + 1)
    source = f"{short}\n\n{long_tail}"
    target = "甲句。"
    alignments = alignments_for(source, target, [(short, "甲句。"), (long_tail, "")])
    memory.add(short, "首句。", "en", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        assert len(view._active_segment_context().source) == len(short)
        assert view._active_segment_context().next_source is None
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        # The stored pair is found, and neither action is disabled.
        assert view.tm_panel.matches and view.tm_panel.matches[0].entry.target_text == "首句。"
        assert strings.TM_PANEL_UNUSABLE.split("{")[0] not in view.tm_panel.notice_label.text()
        assert view.tm_panel.confirm_button.isEnabled() is True
        assert view.tm_panel.check_button.isEnabled() is True
        # Confirming writes what the segment says now, beside the older pair.
        view.tm_panel.confirm_button.click()
        assert len(memory) == 2
        assert sorted(entry.target_text for entry in memory.entries()) == ["甲句。", "首句。"]
        for entry in memory.entries():
            assert entry.next_source_hash is None
    finally:
        view.close()


# -- opening the memory, and what a failure means ---------------------------------


def test_a_memory_that_cannot_be_opened_leaves_the_workbench_running(qapp, monkeypatch) -> None:
    from wuwaterm_client.ui import main_window as main_window_module

    for failure in (tm.TmError("tm_schema_mismatch", "format"), OSError("no such device")):
        def refuse(*_args, error=failure, **_kwargs):
            raise error
        monkeypatch.setattr(main_window_module, "TranslationMemory", refuse)
        assert main_window_module.open_translation_memory() is None
        window = MainWindow(config=ClientConfig())
        try:
            assert window.manuscript_view.tm_panel.memory is None
            assert window.manuscript_view.tm_panel.notice_label.text() == strings.TM_PANEL_NO_MEMORY
        finally:
            window.close()


# -- which dictionary revision the panel compares against -------------------------


def test_the_revision_comes_from_live_review_only_never_from_historical_report_or_basis(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        # Nothing has named a live revision yet, so nothing can be out of date.
        assert view._terms_db_revision() is None
        assert strings.TM_STALE_TERMS not in view.tm_panel.hit_texts()[0]
        report = {
            "request_id": "example", "source_revision": "a" * 64, "target_revision": "b" * 64,
            "rule_version": "review-v2", "matcher_revision": "c" * 64,
            "dictionary": {"schema_version": "1", "source_commit": "d" * 40, "term_count": 1,
                           "revision": OTHER_REVISION},
            "coverage": {"evaluated": 0, "not_evaluated": 0, "rules": ["review.term_pair", "review.sentence_meaning"]},
            "findings": [], "truncated": False,
        }
        # Historical/loaded _report or _term_basis alone must NEVER grant a live revision.
        view._show_term_report(report, state="saved")
        view._term_basis = {"rule_version": "review-v2", "matcher_revision": "c" * 64,
                            "dictionary": {"schema_version": "1", "source_commit": "d" * 40,
                                           "term_count": 1, "revision": OTHER_REVISION}}
        assert view._terms_db_revision() is None
        assert view.tm_panel.matches[0].freshness == tm.TM_FRESHNESS_UNKNOWN

        # A live /v1/reviews dictionary updates provenance and refreshes the panel at once.
        view._record_live_review_dictionary(report["dictionary"])
        assert view._terms_db_revision() == OTHER_REVISION
        assert strings.TM_STALE_TERMS in view.tm_panel.hit_texts()[0]
    finally:
        view.close()


# -- the manager, from the menu --------------------------------------------------


def test_the_edit_menu_opens_the_translation_memory_dialog(qapp, monkeypatch) -> None:
    opened: list[int] = []
    monkeypatch.setattr(ManuscriptView, "show_tm_dialog", lambda self: opened.append(1))
    window = MainWindow(config=ClientConfig())
    try:
        assert isinstance(window.manuscript_view.tm_panel.memory, tm.TranslationMemory)
        edit_menu = next(a.menu() for a in window.menuBar().actions() if a.menu() and a.text() == strings.MENU_EDIT)
        action = next(a for a in edit_menu.actions() if a.text() == strings.TM_MENU)
        assert action.shortcut().toString() == "Ctrl+M"
        action.trigger()
        assert opened == [1]
    finally:
        window.close()


def test_the_manager_dialog_writes_the_file_the_panel_reads(qapp, memory) -> None:
    dialog = TranslationMemoryDialog(memory)
    try:
        dialog.source_edit.setText("First line.")
        dialog.target_edit.setText("首句。")
        dialog._submit()
        assert len(memory) == 1 and memory.entries()[0].target_text == "首句。"
    finally:
        dialog.close()


# -- the reuse check -------------------------------------------------------------


def test_the_reuse_check_counts_fresh_stale_and_unmatched_segments(qapp, memory) -> None:
    source = "First line. Second line. Third line, +2."
    target = "甲句。"
    alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", ""),
                                                 ("Third line, +2.", "")])
    memory.add("First line.", "甲句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    memory.add("Second line.", "乙句。", "en", prev_source="First line.", next_source="Third line, +2.",
               origin="human_confirmed", terms_db_revision=REVISION)
    memory.add("Third line, +5.", "丙句，+5。", "en", prev_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view._check_memory_reuse()
        summary = view.tm_panel.summary
        assert summary is not None
        # The first two match with their neighbours and verified revision; the third only references.
        assert (summary.fresh, summary.stale, summary.unmatched) == (2, 1, 0)
        assert summary.fillable == 1
        assert view.status_label.text() == strings.TM_PANEL_CHECK_SUMMARY.format(fresh=2, stale=1, unmatched=0)
        assert view.tm_panel.fill_button.text() == strings.TM_PANEL_CHECK_FILL.format(n=1)

        # Filling writes the one empty target, as one edit that undo takes back,
        # and reports offline Layer 1 check guidance when unconfigured.
        view._fill_from_memory()
        assert view._payload["target"] == "甲句。乙句。"
        assert view.status_label.text() == strings.TM_PANEL_CHECK_FILLED_OFFLINE.format(n=1)
        view.target_edit.setFocus()
        QTest.keyClick(view.target_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert view._payload["target"] == target
    finally:
        view.close()


def test_the_reuse_check_writes_nothing_while_it_counts(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        before = (len(memory), view._payload["target"], view.document_dirty)
        view._check_memory_reuse()
        assert view.tm_panel.summary.fresh == 1
        # The matching segment already has a translation: nothing is filled.
        assert view.tm_panel.summary.fillable == 0
        assert view.tm_panel.fill_button.text() == ""
        assert (len(memory), view._payload["target"], view.document_dirty) == before
    finally:
        view.close()


def test_homograph_encore_with_different_context_is_reference_only_never_batch_filled(qapp, memory) -> None:
    # Confirmed in character story context.
    memory.add("Encore!", "安可！", "en", prev_source="剧情对话：", next_source="她露出了微笑。",
               origin="human_confirmed", terms_db_revision=REVISION, game_version="3.7.0")
    # Current manuscript is a performance review with different neighbours.
    source = "演出评论开场。Encore! 观众鼓掌。"
    target = "Show intro."
    alignments = alignments_for(source, target, [
        ("演出评论开场。", "Show intro."),
        ("Encore!", ""),
        ("观众鼓掌。", ""),
    ])
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION, "game_version": "3.7.0"})
        install(view, source, target, alignments=alignments)
        view._check_memory_reuse()
        summary = view.tm_panel.summary
        assert summary is not None
        assert (summary.fresh, summary.stale, summary.unmatched) == (0, 1, 2)
        assert summary.fillable == 0
        assert view._tm_fill_plan == []
        assert view.tm_panel.fill_button.isVisible() is False

        # Manual adoption in the panel remains available as reference.
        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        assert len(view.tm_panel.matches) == 1
        assert view.tm_panel.matches[0].match_kind == tm.TM_MATCH_EXACT
        card = view.tm_panel.hit_texts()[0]
        assert strings.TM_MATCH_EXACT in card
        assert strings.TM_BADGE_REFERENCE_ONLY in card
        assert "安可！" in card
        view.tm_panel.apply_at(0)
        assert "安可！" in view._payload["target"]

        # Moving Encore! to the end of the manuscript (prev matches, next disappeared) also stays reference-only.
        end_source = "剧情对话：Encore!"
        end_target = "Story line:"
        end_alignments = alignments_for(end_source, end_target, [("剧情对话：", "Story line:"), ("Encore!", "")])
        install(view, end_source, end_target, alignments=end_alignments)
        view._check_memory_reuse()
        assert view.tm_panel.summary.fresh == 0
        assert view.tm_panel.summary.stale == 1
        assert view._tm_fill_plan == []
    finally:
        view.close()


def test_provenance_unknown_shows_badge_and_never_enters_trusted_fill(qapp, memory) -> None:
    source, target, alignments = two_segments()
    # Stored without terms_db_revision -> provenance_unknown even if context matches.
    memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION, "source_commit": "c" * 40})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        match = view.tm_panel.matches[0]
        assert match.freshness == tm.TM_FRESHNESS_UNKNOWN
        assert match.needs_recheck is True
        assert tm.TM_STALE_PROVENANCE_UNKNOWN in match.stale_reasons
        card = view.tm_panel.hit_texts()[0]
        assert strings.TM_STALE_PROVENANCE_UNKNOWN in card
        assert strings.TM_MATCH_RECHECK.format(reasons=strings.TM_STALE_PROVENANCE_UNKNOWN) in card

        view._check_memory_reuse()
        assert view.tm_panel.summary.fresh == 0
        assert view.tm_panel.summary.stale == 1
        assert view.tm_panel.summary.fillable == 0
        assert view._tm_fill_plan == []
    finally:
        view.close()


def test_reconfirming_with_source_commit_only_preserves_existing_game_version_and_report_overrides_basis(
    qapp, memory
) -> None:
    source, target, alignments = two_segments()
    memory.add(
        "First line.", "甲句。", "en", next_source="Second line.",
        origin="human_confirmed", terms_db_revision=REVISION, game_version="3.7.0",
    )
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        # Live review dictionary has source_commit but no explicit game_version -> _game_version() must be None.
        view._term_basis = {"dictionary": {"revision": REVISION, "source_commit": "c" * 40}}
        view._record_live_review_dictionary({"revision": OTHER_REVISION, "source_commit": "d" * 40})
        install(view, source, target, alignments=alignments)
        assert view._game_version() is None
        # Live review dictionary takes precedence over stale _term_basis in both view and controller.
        assert view._terms_db_revision() == OTHER_REVISION
        assert view._controller._tm_provenance() == (OTHER_REVISION, None)

        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        view.tm_panel.confirm_button.click()
        (entry,) = memory.entries()
        assert entry.terms_db_revision == OTHER_REVISION
        assert entry.game_version == "3.7.0"
    finally:
        view.close()


def test_overlong_neighbour_never_grants_context_exact_in_panel_or_reuse_check(qapp, memory) -> None:
    limit = tm.SOURCE_SCALARS[1]
    short = "First line."
    long_tail = "字" * (limit + 1)
    source = f"{short}\n\n{long_tail}"
    target = ""
    alignments = alignments_for(source, target, [(short, ""), (long_tail, "")])
    memory.add(short, "首句。", "en", origin="human_confirmed", terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        assert view.tm_panel.matches[0].match_kind == tm.TM_MATCH_EXACT
        view._check_memory_reuse()
        assert view.tm_panel.summary.fresh == 0
        assert view.tm_panel.summary.stale == 1
        assert view._tm_fill_plan == []
    finally:
        view.close()


def test_fill_from_memory_triggers_layer_one_structure_check_and_restarts_inflight_check(qapp, memory) -> None:
    async def scenario() -> None:
        source_commit = "d" * 40
        source = "First line. Second line."
        target = "甲句。"
        alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION)

        requests: list[tuple[str, dict | None]] = []
        hold_first_l1 = asyncio.Event()
        l1_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal l1_calls
            body = json.loads(request.content) if request.content else None
            requests.append((request.url.path, body))
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={
                    "api_version": "v1",
                    "capabilities": ["structure-check-v1"],
                    "request_id": "caps",
                })
            if request.url.path == "/v1/structure-checks":
                l1_calls += 1
                if l1_calls == 1:
                    # Simulate a stale in-flight L1 check before fill; the client cancels it via generation.
                    pass
                return httpx.Response(200, json={
                    "request_id": f"l1-{l1_calls}",
                    "check_version": "structure-check-v1",
                    "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
                    "segments": [
                        {"index": i, "segment_id": seg["segment_id"], "findings": [], "omitted_findings": 0,
                         "layout": {"source_paragraphs": 1, "target_paragraphs": 1,
                                    "source_lines": 1, "target_lines": 1}}
                        for i, seg in enumerate(body["segments"])
                    ],
                })
            raise AssertionError(request.url.path)

        client = ApiClient(
            "https://example.invalid",
            token_provider=lambda: "test-token",
            _test_transport=httpx.MockTransport(handler),
        )
        view = ManuscriptView(client, translation_memory=memory)
        try:
            view._record_live_review_dictionary({"revision": REVISION, "source_commit": source_commit})
            install(view, source, target, alignments=alignments)
            # Start an in-flight Layer 1 check before filling; _fill_from_memory must restart it.
            assert view._controller.start_structure_check() is True
            prior_generation = view._controller.structure_generation
            view._check_memory_reuse()
            assert view.tm_panel.summary.fillable == 1
            view._fill_from_memory()
            assert view._controller.structure_generation > prior_generation
            assert view._payload["target"] == "甲句。乙句。"
            assert view.status_label.text() == strings.TM_PANEL_CHECK_FILLED_CHECKING.format(n=1)
            for _ in range(100):
                task = view._controller.structure_task
                if task is None or task.done():
                    break
                await asyncio.sleep(0.01)
            assert [path for path, _ in requests if path == "/v1/structure-checks"]
            # Latest structure check covered the newly filled target and updated the status bar.
            last_l1_body = [body for path, body in requests if path == "/v1/structure-checks"][-1]
            assert "乙句。" in last_l1_body["segments"][0]["target"]
            assert view._controller.structure.phase == "done"
            assert view.status_label.text() == strings.MANUSCRIPT_L1_CLEAN.format(checked=1)
            # Undo remains intact and reverts the batch fill cleanly.
            assert view._controller.can_undo() is True
            view._undo_replace()
            assert view._payload["target"] == target
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_confirming_segment_beside_overlong_neighbour_stores_no_context(qapp, memory) -> None:
    overlong = "Tail " + "x" * 2001
    source = f"First line. Middle segment. {overlong}"
    target = "中段。尾段。"
    alignments = alignments_for(
        source,
        target,
        [("First line.", ""), ("Middle segment.", "中段。"), (overlong, "尾段。")],
    )
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(1)
        active = view._active_segment_context()
        assert active is not None
        assert active.prev_source == "First line."
        assert active.next_unhashable is True
        view.tm_panel.confirm_button.click()

        assert len(memory) == 1
        entry = memory.entries()[0]
        assert entry.prev_source_hash is None
        assert entry.next_source_hash is None

        # Moving the segment to a boundary must only yield context-free exact, never auto-fill.
        new_source = "Other head. Middle segment."
        new_target = "他句。"
        new_alignments = alignments_for(new_source, new_target, [("Other head.", "他句。"), ("Middle segment.", "")])
        install(view, new_source, new_target, alignments=new_alignments)
        view._check_memory_reuse()
        assert view.tm_panel.summary.fillable == 0
        assert view.tm_panel.summary.stale == 1
        view.segment_list.setCurrentRow(1)
        assert [m.match_kind for m in view.tm_panel.matches] == [tm.TM_MATCH_EXACT]
        assert view._tm_fill_plan == []
    finally:
        view.close()


def test_undoing_fill_during_inflight_layer_one_does_not_report_zero_checked(qapp, memory) -> None:
    async def scenario() -> None:
        source = "First line. Second line."
        target = "甲句。"
        alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION)

        l1_started = asyncio.Event()
        release_l1 = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={
                    "api_version": "v1",
                    "capabilities": ["structure-check-v1"],
                    "request_id": "caps",
                })
            if request.url.path == "/v1/structure-checks":
                l1_started.set()
                await release_l1.wait()
                return httpx.Response(200, json={
                    "request_id": "l1-held",
                    "check_version": "structure-check-v1",
                    "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
                    "segments": [
                        {"index": i, "segment_id": seg["segment_id"], "findings": [], "omitted_findings": 0,
                         "layout": {"source_paragraphs": 1, "target_paragraphs": 1,
                                    "source_lines": 1, "target_lines": 1}}
                        for i, seg in enumerate(body["segments"])
                    ],
                })
            raise AssertionError(request.url.path)

        client = ApiClient(
            "https://example.invalid",
            token_provider=lambda: "test-token",
            _test_transport=httpx.MockTransport(handler),
        )
        view = ManuscriptView(client, translation_memory=memory)
        try:
            view._record_live_review_dictionary({"revision": REVISION})
            install(view, source, target, alignments=alignments)
            view._check_memory_reuse()
            view._fill_from_memory()
            await asyncio.wait_for(l1_started.wait(), timeout=2.0)

            # User hits Ctrl+Z while the Layer 1 request for the filled target is still in flight.
            view._undo_replace()
            assert view._payload["target"] == target
            assert view.status_label.text() == strings.MANUSCRIPT_UNDONE

            task = view._controller.structure_task
            release_l1.set()
            if task is not None:
                await asyncio.wait_for(task, timeout=2.0)

            assert view._controller.structure.phase == "idle"
            assert view.status_label.text() == strings.MANUSCRIPT_UNDONE
            assert strings.MANUSCRIPT_L1_CLEAN.format(checked=0) not in view.status_label.text()
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_editing_during_inflight_layer_one_clears_running_status_when_stale_response_returns(qapp, memory) -> None:
    async def scenario() -> None:
        source = "First line. Second line."
        target = "甲句。"
        alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION)

        l1_started = asyncio.Event()
        release_l1 = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={
                    "api_version": "v1",
                    "capabilities": ["structure-check-v1"],
                    "request_id": "caps",
                })
            if request.url.path == "/v1/structure-checks":
                l1_started.set()
                await release_l1.wait()
                return httpx.Response(200, json={
                    "request_id": "l1-held",
                    "check_version": "structure-check-v1",
                    "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
                    "segments": [
                        {"index": i, "segment_id": seg["segment_id"], "findings": [], "omitted_findings": 0,
                         "layout": {"source_paragraphs": 1, "target_paragraphs": 1,
                                    "source_lines": 1, "target_lines": 1}}
                        for i, seg in enumerate(body["segments"])
                    ],
                })
            raise AssertionError(request.url.path)

        client = ApiClient(
            "https://example.invalid",
            token_provider=lambda: "test-token",
            _test_transport=httpx.MockTransport(handler),
        )
        view = ManuscriptView(client, translation_memory=memory)
        try:
            view._record_live_review_dictionary({"revision": REVISION})
            install(view, source, target, alignments=alignments)
            view._check_memory_reuse()
            view._fill_from_memory()
            assert "正在自动运行" in view.status_label.text()
            await asyncio.wait_for(l1_started.wait(), timeout=2.0)

            # Edit one character while Layer 1 is still in flight; the status bar still shows the progress text.
            view.target_edit.textCursor().insertText("！")
            view._refresh_derived()
            assert "正在自动运行" in view.status_label.text()

            task = view._controller.structure_task
            release_l1.set()
            if task is not None:
                await asyncio.wait_for(task, timeout=2.0)

            assert view._controller.structure.phase == "idle"
            assert "正在自动运行" not in view.status_label.text()
            assert view.status_label.text() == strings.MANUSCRIPT_L1_NOT_RUN
            assert strings.MANUSCRIPT_L1_NOT_RUN in view.l1_status.text()
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_undoing_fill_during_inflight_layer_one_503_keeps_undo_status(qapp, memory) -> None:
    async def scenario() -> None:
        source = "First line. Second line."
        target = "甲句。"
        alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION)

        l1_started = asyncio.Event()
        release_l1 = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={
                    "api_version": "v1",
                    "capabilities": ["structure-check-v1"],
                    "request_id": "caps",
                })
            if request.url.path == "/v1/structure-checks":
                l1_started.set()
                await release_l1.wait()
                return httpx.Response(503, json={
                    "error": {"code": "service_unavailable", "message": "Service unavailable"},
                    "request_id": "l1-503",
                })
            raise AssertionError(request.url.path)

        client = ApiClient(
            "https://example.invalid",
            token_provider=lambda: "test-token",
            _test_transport=httpx.MockTransport(handler),
        )
        view = ManuscriptView(client, translation_memory=memory)
        try:
            view._record_live_review_dictionary({"revision": REVISION})
            install(view, source, target, alignments=alignments)
            view._check_memory_reuse()
            view._fill_from_memory()
            await asyncio.wait_for(l1_started.wait(), timeout=2.0)

            view._undo_replace()
            assert view._payload["target"] == target
            assert view.status_label.text() == strings.MANUSCRIPT_UNDONE

            task = view._controller.structure_task
            release_l1.set()
            if task is not None:
                await asyncio.wait_for(task, timeout=2.0)

            assert view._controller.structure.phase == "idle"
            assert view._controller.structure.error is None
            assert view.status_label.text() == strings.MANUSCRIPT_UNDONE
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


# -- live service provenance vs historical workfiles (MY-491 §5.1) --------------


def _meta_payload(
    *,
    schema_version: str = "1",
    source_commit: str = "a" * 40,
    term_count: int = 42,
    source_game_version: str | None = "3.7.0",
    request_id: str = "req-meta",
) -> dict:
    payload = {
        "service_version": "0.4.0",
        "api_version": "v1",
        "schema_version": schema_version,
        "source_profile": "official",
        "source_commit": source_commit,
        "term_count": term_count,
        "llm_configured": True,
        "request_id": request_id,
    }
    if source_game_version is not None:
        payload["source_game_version"] = source_game_version
    return payload


def _review_response(
    source: str,
    target: str,
    *,
    schema_version: str = "1",
    source_commit: str = "a" * 40,
    term_count: int = 42,
    revision: str = REVISION,
    request_id: str = "req-review",
) -> dict:
    return {
        "request_id": request_id,
        "source_revision": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "target_revision": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "rule_version": "review-v2",
        "matcher_revision": "a" * 64,
        "dictionary": {
            "schema_version": schema_version,
            "source_commit": source_commit,
            "term_count": term_count,
            "revision": revision,
        },
        "coverage": {"evaluated": 0, "not_evaluated": 0, "rules": ["review.term_pair", "review.sentence_meaning"]},
        "findings": [],
        "truncated": False,
    }


def _historical_workfile_payload(source: str, target: str, alignments: list[dict], *, old_revision: str) -> dict:
    doc = empty_workfile()
    doc.update(source=source, target=target, direction="en", alignments=alignments)
    old_report = _review_response(source, target, source_commit="0" * 40, term_count=10, revision=old_revision)
    doc["history"] = [
        {
            "source": source,
            "target": target,
            "direction": "en",
            "alignments": alignments,
            "report": old_report,
        }
    ]
    return doc


def test_historical_workfile_revision_never_counts_as_live_service_provenance(qapp, tmp_path, memory) -> None:
    """Scenario (b): opening a historical .wuwaterm with old report.dictionary.revision
    must NOT treat that old revision as the live server's dictionary revision."""
    from wuwaterm_client.manuscript import save_workfile

    source = "First line. Second line."
    target = "甲句。"
    alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])
    memory.add(
        "Second line.", "乙句。", "en", prev_source="First line.",
        origin="human_confirmed", terms_db_revision=OTHER_REVISION, game_version="3.6.0",
    )

    workfile_path = tmp_path / "historical.wuwaterm"
    save_workfile(workfile_path, _historical_workfile_payload(source, target, alignments, old_revision=OTHER_REVISION))

    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        assert view.open_path(workfile_path) is True
        # Even if a historical report is held/inspected on the view, live revision stays None.
        view._report = view._payload["history"][-1]["report"]
        assert view._terms_db_revision() is None
        assert view._controller._tm_provenance() == (None, None)

        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        assert len(view.tm_panel.matches) == 1
        match = view.tm_panel.matches[0]
        assert match.freshness == tm.TM_FRESHNESS_UNKNOWN
        assert match.freshness != tm.TM_FRESHNESS_VERIFIED

        view._check_memory_reuse()
        assert view.tm_panel.summary.fresh == 0
        assert view.tm_panel.summary.fillable == 0
        assert view._tm_fill_plan == []

        # Confirming segment 0 does NOT stamp the historical file's OTHER_REVISION onto the new TM row.
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        view.tm_panel.confirm_button.click()
        new_entry = next(e for e in memory.entries() if e.source_text == "First line.")
        assert new_entry.terms_db_revision is None
    finally:
        view.close()


def test_live_meta_game_version_change_marks_old_entries_known_stale_before_review(qapp, tmp_path, memory) -> None:
    """Scenario (c): live /v1/meta reporting source_game_version='3.7.0' makes old '3.6.0'
    entries known_stale (game_version_changed) immediately, even before any live /v1/reviews."""
    from wuwaterm_client.api import MetaResult
    from wuwaterm_client.manuscript import save_workfile

    source = "First line. Second line."
    target = "甲句。"
    alignments = alignments_for(source, target, [("First line.", "甲句。"), ("Second line.", "")])
    memory.add(
        "Second line.", "乙句。", "en", prev_source="First line.",
        origin="human_confirmed", terms_db_revision=OTHER_REVISION, game_version="3.6.0",
    )
    workfile_path = tmp_path / "historical_360.wuwaterm"
    save_workfile(workfile_path, _historical_workfile_payload(source, target, alignments, old_revision=OTHER_REVISION))

    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        assert view.open_path(workfile_path) is True
        view.set_service_meta(
            MetaResult.from_json(_meta_payload(source_commit="b" * 40, term_count=55, source_game_version="3.7.0"))
        )
        assert view._controller._tm_provenance() == (None, "3.7.0")

        view.segment_list.setCurrentRow(1)
        view._refresh_tm_panel()
        match = view.tm_panel.matches[0]
        assert match.freshness == tm.TM_FRESHNESS_STALE
        assert tm.TM_STALE_GAME_VERSION in match.stale_reasons
        assert strings.TM_STALE_GAME in view.tm_panel.hit_texts()[0]

        view._check_memory_reuse()
        assert view.tm_panel.summary.fresh == 0
        assert view.tm_panel.summary.stale == 1
        assert view.tm_panel.summary.fillable == 0
    finally:
        view.close()


@pytest.mark.parametrize(
    "changed_field",
    [
        {"schema_version": "2"},
        {"source_commit": "f" * 40},
        {"term_count": 999},
    ],
)
def test_live_meta_dictionary_fingerprint_change_invalidates_live_review_dictionary(
    qapp, memory, changed_field
) -> None:
    """Scenario (d): when /v1/meta reports a change in (schema_version, source_commit, term_count),
    any previously recorded _live_review_dictionary is invalidated and old entries stop being verified_fresh."""
    from wuwaterm_client.api import MetaResult

    source, target, alignments = two_segments()
    memory.add(
        "First line.", "甲句。", "en", next_source="Second line.",
        origin="human_confirmed", terms_db_revision=REVISION, game_version="3.7.0",
    )
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        initial_meta = _meta_payload(schema_version="1", source_commit="a" * 40, term_count=42, source_game_version="3.7.0")
        view.set_service_meta(MetaResult.from_json(initial_meta))
        view._record_live_review_dictionary({
            "schema_version": "1",
            "source_commit": "a" * 40,
            "term_count": 42,
            "revision": REVISION,
        })
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        assert view.tm_panel.matches[0].freshness == tm.TM_FRESHNESS_VERIFIED

        updated_meta = {**initial_meta, **changed_field}
        view.set_service_meta(MetaResult.from_json(updated_meta))
        assert view._live_review_dictionary is None
        assert view._terms_db_revision() is None
        assert view.tm_panel.matches[0].freshness == tm.TM_FRESHNESS_UNKNOWN
    finally:
        view.close()


def test_startup_silent_meta_and_live_review_end_to_end_tm_confirm_and_reuse(
    qapp, tmp_path, monkeypatch, memory
) -> None:
    """Scenario (e): MainWindow starting on ManuscriptView silently fetches /v1/meta; after a live
    /v1/reviews with matching fingerprint, confirming writes both terms_db_revision and game_version
    and enables verified_fresh batch fill and incremental translation skip."""
    from wuwaterm_client import config as config_module
    from wuwaterm_client.ui import main_window as main_window_module, status_view as status_view_module

    async def scenario() -> None:
        monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
        monkeypatch.setattr(main_window_module, "has_token", lambda: True)
        monkeypatch.setattr(status_view_module, "read_token", lambda: "wtd1.test")
        monkeypatch.setattr(status_view_module, "active_backend_name", lambda: "MemoryKeyring")

        commit = "e" * 40
        asked: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            asked.append(request.url.path)
            body = json.loads(request.content) if request.content else None
            if request.url.path == "/v1/meta":
                return httpx.Response(200, json=_meta_payload(
                    schema_version="1", source_commit=commit, term_count=42, source_game_version="3.7.0",
                ))
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": [], "request_id": "caps"})
            if request.url.path == "/v1/reviews":
                return httpx.Response(200, json=_review_response(
                    body["source"], body["target"],
                    schema_version="1", source_commit=commit, term_count=42, revision=REVISION,
                ))
            if request.url.path == "/v1/translations":
                return httpx.Response(200, json={
                    "kind": "llm",
                    "text": f"[MT:{body['text']}]",
                    "direction": body.get("to") or "en",
                    "dictionary_miss": False,
                    "request_id": "tr-1",
                })
            raise AssertionError(f"unexpected path: {request.url.path}")

        client = ApiClient(
            "https://api.example.invalid",
            token_provider=lambda: "wtd1.test",
            _test_transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(main_window_module.ApiClient, "from_config", classmethod(lambda cls, _cfg: client))
        monkeypatch.setattr(main_window_module, "open_translation_memory", lambda: memory)

        window = MainWindow(ClientConfig(base_url="https://api.example.invalid"))
        try:
            # Startup stays on ManuscriptView and silently triggers GET /v1/meta.
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)

            assert "/v1/meta" in asked
            assert window._meta is not None
            assert window._meta.source_game_version == "3.7.0"
            assert window.manuscript_view._controller._tm_provenance() == (None, "3.7.0")

            # Run live document /v1/reviews in this session.
            view = window.manuscript_view
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)
            view._start_review()
            assert view._task is not None
            await asyncio.wait_for(view._task, timeout=2.0)
            await asyncio.sleep(0)

            assert view._controller._tm_provenance() == (REVISION, "3.7.0")
            view.segment_list.setCurrentRow(0)
            view._refresh_tm_panel()
            view.tm_panel.confirm_button.click()

            (entry,) = memory.entries()
            assert entry.terms_db_revision == REVISION
            assert entry.game_version == "3.7.0"

            # Same dictionary + same game version -> verified_fresh and reusable in incremental translation.
            source2 = "First line. Second line."
            target2 = ""
            align2 = alignments_for(source2, target2, [("First line.", ""), ("Second line.", "")])
            install(view, source2, target2, alignments=align2)
            view.segment_list.setCurrentRow(0)
            view._refresh_tm_panel()
            assert view.tm_panel.matches[0].freshness == tm.TM_FRESHNESS_VERIFIED

            view._check_memory_reuse()
            assert view.tm_panel.summary.fresh == 1
            assert view.tm_panel.summary.fillable == 1

            # Incremental translation skips segment 0 via TM and only sends segment 1 to /v1/translations.
            translate_calls_before = asked.count("/v1/translations")
            view._controller.start_translation()
            if view._controller.translate_task is not None:
                await asyncio.wait_for(view._controller.translate_task, timeout=2.0)
            assert asked.count("/v1/translations") == translate_calls_before + 1
            assert "首句。" in view._payload["target"] or "甲句。" in view._payload["target"]
            assert "[MT:Second line.]" in view._payload["target"]

            # Also verify long-manuscript segment-mode check_term records _live_review_dictionary.
            long_seg0 = "Long segment zero. " + ("x" * 1000)
            long_seg1 = "Long segment one. " + ("y" * 1000)
            long_source = f"{long_seg0}\n\n{long_seg1}"
            long_target = "甲长段。\n\n乙长段。"
            long_align = alignments_for(long_source, long_target, [(long_seg0, "甲长段。"), (long_seg1, "乙长段。")])
            view.set_service_meta(None)
            assert view._terms_db_revision() is None
            view.set_service_meta(
                MetaResult.from_json(_meta_payload(
                    schema_version="1", source_commit=commit, term_count=42, source_game_version="3.7.0",
                ))
            )
            install(view, long_source, long_target, alignments=long_align)
            err = await view._controller.check_term(0, view._controller.semantic_generation)
            assert err is None
            assert view._controller._tm_provenance() == (REVISION, "3.7.0")
        finally:
            await client.aclose()
            window.manuscript_view._saved_payload = dict(window.manuscript_view._payload)
            window.close()

    asyncio.run(scenario())


def test_endpoint_change_clears_live_provenance_and_blocks_late_old_meta_reply(
    qapp, tmp_path, monkeypatch, memory
) -> None:
    """Scenario (f): switching endpoint clears _meta / _live_review_dictionary at once,
    drops a slow in-flight /v1/meta from the old endpoint via generation guard, and clearing
    the endpoint or credential leaves live provenance empty."""
    from wuwaterm_client import config as config_module
    from wuwaterm_client.ui import main_window as main_window_module, status_view as status_view_module

    async def scenario() -> None:
        monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
        monkeypatch.setattr(main_window_module, "has_token", lambda: True)
        monkeypatch.setattr(status_view_module, "read_token", lambda: "wtd1.test")
        monkeypatch.setattr(status_view_module, "active_backend_name", lambda: "MemoryKeyring")
        monkeypatch.setattr(ClientConfig, "save", lambda self: None)

        old_meta_started = asyncio.Event()
        release_old_meta = asyncio.Event()
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            calls.append(f"{host}{request.url.path}")
            if request.url.path == "/v1/meta":
                if host == "old.example.invalid":
                    if calls.count("old.example.invalid/v1/meta") >= 2:
                        old_meta_started.set()
                        await release_old_meta.wait()
                    return httpx.Response(200, json=_meta_payload(
                        source_commit="1" * 40, term_count=10, source_game_version="3.6.0", request_id="meta-old",
                    ))
                return httpx.Response(200, json=_meta_payload(
                    source_commit="2" * 40, term_count=20, source_game_version="3.8.0", request_id="meta-new",
                ))
            raise AssertionError(f"unexpected request: {request.url}")

        client = ApiClient(
            "https://old.example.invalid",
            token_provider=lambda: "wtd1.test",
            _test_transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(main_window_module.ApiClient, "from_config", classmethod(lambda cls, _cfg: client))
        monkeypatch.setattr(main_window_module, "open_translation_memory", lambda: memory)

        window = MainWindow(ClientConfig(base_url="https://old.example.invalid"))
        try:
            # Wait for initial startup meta.
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            assert window.manuscript_view._game_version() == "3.6.0"

            # Record live review provenance on old endpoint.
            window.manuscript_view._record_live_review_dictionary({
                "schema_version": "1",
                "source_commit": "1" * 40,
                "term_count": 10,
                "revision": REVISION,
            })
            assert window.manuscript_view._terms_db_revision() == REVISION

            # Start a slow manual meta refresh on old endpoint, then switch to new endpoint mid-flight.
            window.status_view._on_refresh_clicked()
            old_task = window.status_view._task
            await asyncio.wait_for(old_meta_started.wait(), timeout=2.0)

            # Switch to a new endpoint mid-flight -> immediately clears all live provenance.
            new_meta_started = asyncio.Event()
            release_new_meta = asyncio.Event()

            async def handler_with_new_gate(request: httpx.Request) -> httpx.Response:
                host = request.url.host
                calls.append(f"{host}{request.url.path}")
                if request.url.path == "/v1/meta":
                    if host == "old.example.invalid":
                        old_meta_started.set()
                        await release_old_meta.wait()
                        return httpx.Response(200, json=_meta_payload(
                            source_commit="1" * 40, term_count=10, source_game_version="3.6.0", request_id="meta-old",
                        ))
                    new_meta_started.set()
                    await release_new_meta.wait()
                    return httpx.Response(200, json=_meta_payload(
                        source_commit="2" * 40, term_count=20, source_game_version="3.8.0", request_id="meta-new",
                    ))
                raise AssertionError(f"unexpected request: {request.url}")

            client._client._transport = httpx.MockTransport(handler_with_new_gate)

            window._apply_settings(ClientConfig(base_url="https://new.example.invalid"))
            assert window._meta is None
            assert window.manuscript_view._service_meta is None
            assert window.manuscript_view._live_review_dictionary is None
            assert window.manuscript_view._controller._tm_provenance() == (None, None)

            # Release the slow old-endpoint response; generation guard must drop it before new endpoint answers.
            release_old_meta.set()
            if old_task is not None:
                await asyncio.gather(old_task, return_exceptions=True)
            assert window._meta is None
            assert window.manuscript_view._service_meta is None
            assert window.manuscript_view._controller._tm_provenance() == (None, None)

            # Now allow the new endpoint's silent /v1/meta to complete.
            await asyncio.wait_for(new_meta_started.wait(), timeout=2.0)
            release_new_meta.set()
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            assert window._meta is not None
            assert window._meta.source_game_version == "3.8.0"
            assert window.manuscript_view._controller._tm_provenance() == (None, "3.8.0")

            # Removing the credential via Settings also clears live provenance immediately.
            from PySide6.QtWidgets import QDialog

            token_present = True
            monkeypatch.setattr(main_window_module, "has_token", lambda: token_present)

            class _CredentialClearedDialog:
                def __init__(self, *args, **kwargs):
                    pass

                def exec(self):
                    nonlocal token_present
                    token_present = False
                    return QDialog.DialogCode.Rejected

            monkeypatch.setattr(main_window_module, "SettingsDialog", _CredentialClearedDialog)
            window._on_settings_clicked()
            assert window._meta is None
            assert window.manuscript_view._service_meta is None
            assert window.manuscript_view._controller._tm_provenance() == (None, None)
        finally:
            await client.aclose()
            window.manuscript_view._saved_payload = dict(window.manuscript_view._payload)
            window.close()

    asyncio.run(scenario())


def test_late_old_review_after_newer_meta_and_503_refetch_preserves_meta_and_blocks_reuse(
    qapp, tmp_path, monkeypatch, memory
) -> None:
    """Finding 1: 3.7.0 /v1/meta arrives before a late old-dictionary /v1/reviews,
    and the triggered silent refetch fails with 503. Must retain 3.7.0 _service_meta,
    refuse the stale review dictionary, keep TM known_stale, and never batch-fill."""
    from wuwaterm_client import config as config_module
    from wuwaterm_client.ui import main_window as main_window_module, status_view as status_view_module

    async def scenario() -> None:
        monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
        monkeypatch.setattr(main_window_module, "has_token", lambda: True)
        monkeypatch.setattr(status_view_module, "read_token", lambda: "wtd1.test")
        monkeypatch.setattr(status_view_module, "active_backend_name", lambda: "MemoryKeyring")

        memory.add(
            "Second line.", "乙句。", "en", prev_source="First line.",
            origin="human_confirmed", terms_db_revision=OTHER_REVISION, game_version="3.6.0",
        )
        meta_calls = 0
        review_started = asyncio.Event()
        release_review = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal meta_calls
            body = json.loads(request.content) if request.content else None
            if request.url.path == "/v1/meta":
                meta_calls += 1
                if meta_calls == 1:
                    return httpx.Response(200, json=_meta_payload(
                        source_commit="b" * 40, term_count=55, source_game_version="3.7.0", request_id="meta-370",
                    ))
                return httpx.Response(503, json={"error": {"code": "service_unavailable", "message": "down"}})
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": [], "request_id": "caps"})
            if request.url.path == "/v1/reviews":
                review_started.set()
                await release_review.wait()
                return httpx.Response(200, json=_review_response(
                    body["source"], body["target"],
                    source_commit="0" * 40, term_count=10, revision=OTHER_REVISION, request_id="rev-old",
                ))
            raise AssertionError(f"unexpected path: {request.url.path}")

        client = ApiClient(
            "https://api.example.invalid",
            token_provider=lambda: "wtd1.test",
            _test_transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(main_window_module.ApiClient, "from_config", classmethod(lambda cls, _cfg: client))
        monkeypatch.setattr(main_window_module, "open_translation_memory", lambda: memory)

        window = MainWindow(ClientConfig(base_url="https://api.example.invalid"))
        try:
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            assert window.manuscript_view._game_version() == "3.7.0"

            view = window.manuscript_view
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)
            view._start_review()
            await asyncio.wait_for(review_started.wait(), timeout=2.0)

            release_review.set()
            if view._task is not None:
                await asyncio.wait_for(view._task, timeout=2.0)
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)

            # Refetch failed with 503, yet 3.7.0 is preserved and old dictionary is rejected.
            assert meta_calls >= 2
            assert window._meta is not None and window._meta.source_game_version == "3.7.0"
            assert view._service_meta is not None and view._service_meta.source_game_version == "3.7.0"
            assert view._live_review_dictionary is None
            assert view._controller._tm_provenance() == (None, "3.7.0")

            view.segment_list.setCurrentRow(1)
            view._refresh_tm_panel()
            assert view.tm_panel.matches[0].freshness == tm.TM_FRESHNESS_STALE
            view._check_memory_reuse()
            assert view.tm_panel.summary.fresh == 0
            assert view.tm_panel.summary.fillable == 0
            assert view._tm_fill_plan == []
        finally:
            await client.aclose()
            window.manuscript_view._saved_payload = dict(window.manuscript_view._payload)
            window.close()

    asyncio.run(scenario())


def test_same_origin_token_replacement_advances_review_generations_and_blocks_inflight_writeback(
    qapp, tmp_path, monkeypatch, memory
) -> None:
    """Finding 2: replacing a stored token on the same endpoint clears live provenance,
    advances both review generations so in-flight document/segment reviews cannot write back,
    and refreshes /v1/meta under the new token."""
    from PySide6.QtWidgets import QDialog
    from wuwaterm_client import config as config_module
    from wuwaterm_client.ui import main_window as main_window_module, status_view as status_view_module

    async def scenario() -> None:
        current_token = "tok-A"
        monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
        monkeypatch.setattr(main_window_module, "has_token", lambda: bool(current_token))
        monkeypatch.setattr(main_window_module, "read_token", lambda: current_token)
        monkeypatch.setattr(status_view_module, "read_token", lambda: current_token)
        monkeypatch.setattr(status_view_module, "active_backend_name", lambda: "MemoryKeyring")

        meta_tokens: list[str] = []
        review_started = asyncio.Event()
        release_review = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            body = json.loads(request.content) if request.content else None
            if request.url.path == "/v1/meta":
                meta_tokens.append(auth)
                gv = "3.6.0" if auth.endswith("tok-A") else "3.7.0"
                return httpx.Response(200, json=_meta_payload(
                    source_commit="a" * 40, term_count=42, source_game_version=gv,
                ))
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": [], "request_id": "caps"})
            if request.url.path == "/v1/reviews":
                review_started.set()
                await release_review.wait()
                return httpx.Response(200, json=_review_response(
                    body["source"], body["target"],
                    source_commit="a" * 40, term_count=42, revision=OTHER_REVISION,
                ))
            raise AssertionError(f"unexpected path: {request.url.path}")

        client = ApiClient(
            "https://api.example.invalid",
            token_provider=lambda: current_token,
            _test_transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(main_window_module.ApiClient, "from_config", classmethod(lambda cls, _cfg: client))
        monkeypatch.setattr(main_window_module, "open_translation_memory", lambda: memory)

        window = MainWindow(ClientConfig(base_url="https://api.example.invalid"))
        try:
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            assert window.manuscript_view._game_version() == "3.6.0"

            view = window.manuscript_view
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)

            # Start both an in-flight document review AND a segment-mode check_term under tok-A.
            view._start_review()
            old_review_task = view._task
            await asyncio.wait_for(review_started.wait(), timeout=2.0)

            long_seg0 = "Segment zero. " + ("x" * 1050)
            long_seg1 = "Segment one. " + ("z" * 1050)
            long_tgt0 = "甲长段。" + ("y" * 1050)
            long_tgt1 = "乙长段。" + ("w" * 1050)
            long_view_payload = empty_workfile()
            long_view_payload.update(
                source=f"{long_seg0}\n\n{long_seg1}",
                target=f"{long_tgt0}\n\n{long_tgt1}",
                direction="en",
            )
            view._controller.payload = long_view_payload
            assert view._controller.records() is not None
            review_started.clear()
            seg_task = asyncio.ensure_future(
                view._controller.check_term(0, view._controller.semantic_generation)
            )
            await asyncio.wait_for(review_started.wait(), timeout=2.0)

            doc_gen_before = view._controller.generation
            seg_gen_before = view._controller.semantic_generation
            meta_gen_before = window.status_view._generation

            # Replace tok-A -> tok-B via SettingsDialog (same endpoint, has_token stays True).
            class _ReplaceTokenDialog:
                def __init__(self, *args, **kwargs):
                    self.credential_modified = False

                def exec(self):
                    nonlocal current_token
                    current_token = "tok-B"
                    self.credential_modified = True
                    return QDialog.DialogCode.Rejected

            monkeypatch.setattr(main_window_module, "SettingsDialog", _ReplaceTokenDialog)
            window._on_settings_clicked()

            assert window._meta is None
            assert view._service_meta is None
            assert view._live_review_dictionary is None
            assert view._controller.generation > doc_gen_before
            assert view._controller.semantic_generation > seg_gen_before
            assert window.status_view._generation > meta_gen_before

            # Complete both pre-change reviews and wait for the replacement silent meta.
            release_review.set()
            await asyncio.gather(
                *(t for t in (old_review_task, seg_task) if t is not None),
                return_exceptions=True,
            )
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)

            assert view._live_review_dictionary is None
            assert window._meta is not None and window._meta.source_game_version == "3.7.0"
            assert meta_tokens[-1] == "Bearer tok-B"
        finally:
            await client.aclose()
            window.manuscript_view._saved_payload = dict(window.manuscript_view._payload)
            window.close()

    asyncio.run(scenario())


def test_status_view_manual_refresh_never_sticks_loading_when_silent_refresh_requested(qapp) -> None:
    """Finding 3: a manual refresh in flight must preserve visible completion/error
    feedback even if a silent refresh is requested concurrently."""
    from wuwaterm_client.ui.status_view import StatusView

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        fail_next = False

        async def handler(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            if fail_next:
                return httpx.Response(503, json={
                    "error": {"code": "service_unavailable", "message": "down"},
                    "request_id": "req-503",
                })
            return httpx.Response(200, json=_meta_payload(source_game_version="3.7.0"))

        client = ApiClient(
            "https://api.example.invalid",
            token_provider=lambda: "wtd1.test",
            _test_transport=httpx.MockTransport(handler),
        )
        view = StatusView(client)
        try:
            # Case 1: manual refresh succeeds after concurrent silent request.
            view._on_refresh_clicked()
            await asyncio.wait_for(started.wait(), timeout=2.0)
            assert view.status_label.text() == strings.STATUS_LOADING
            view.refresh_meta(silent=True)
            release.set()
            if view._task is not None:
                await asyncio.wait_for(view._task, timeout=2.0)
            assert view.status_label.text() == strings.STATUS_BAR_DONE

            # Case 2: silent refresh keeps refresh_button enabled, promoted by click(), then fails with 503.
            started.clear()
            release.clear()
            fail_next = True
            view.refresh_meta(silent=True)
            await asyncio.wait_for(started.wait(), timeout=2.0)
            assert view.refresh_button.isEnabled() is True
            view.refresh_button.click()
            assert view.refresh_button.isEnabled() is False
            assert view.status_label.text() == strings.STATUS_LOADING
            release.set()
            if view._task is not None:
                await asyncio.wait_for(view._task, timeout=2.0)
            assert view.status_label.text() != strings.STATUS_LOADING
            assert view._banner.is_showing() is True
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_invalid_game_version_with_nul_or_surrogate_never_breaks_healthy_tm(qapp, memory) -> None:
    """Finding 4: NUL / lone surrogates / overlong game_version values normalize to None
    before reaching TranslationMemory, so a healthy TM is never reported as broken."""
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION})
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)

        for bad_version in ("3.7.0\x00", "3.7.\ud800", "v" * 129):
            view.set_service_meta(MetaResult(
                service_version="0.4.0",
                api_version="v1",
                schema_version="1",
                source_profile="official",
                source_commit="a" * 40,
                term_count=42,
                llm_configured=True,
                request_id="req-bad-gv",
                source_game_version=bad_version,
            ))
            assert view._game_version() is None
            view._refresh_tm_panel()
            assert strings.TM_PANEL_UNUSABLE.split("{")[0] not in view.tm_panel.notice_label.text()
            view._check_memory_reuse()
            assert view.tm_panel.summary is not None
            view.tm_panel.confirm_button.click()
            assert strings.TM_PANEL_CONFIRM_FAILED.split("{")[0] not in view.status_label.text()
    finally:
        view.close()


def test_settings_dialog_credential_change_is_origin_scoped_and_immediate(
    qapp, tmp_path, monkeypatch, memory
) -> None:
    """Changing/deleting a token in SettingsDialog emits credentialChanged(origin):
    unrelated origins on Cancel must NOT interrupt active reviews/provenance, while
    changing the active origin invalidates provenance & cancels reviews immediately."""
    from PySide6.QtWidgets import QDialog, QMessageBox
    from wuwaterm_client import config as config_module
    from wuwaterm_client.ui import (
        main_window as main_window_module,
        settings_dialog as settings_dialog_module,
        status_view as status_view_module,
    )

    async def scenario() -> None:
        monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
        tokens: dict[str, str] = {"https://a.example": "tok-a"}
        next_token = "tok-b"

        def _fake_has_token(origin=None):
            return bool(origin and origin in tokens)

        def _fake_read_token(origin=None):
            return tokens.get(origin) if origin else None

        def _fake_store_token(token, origin=None):
            assert origin is not None
            tokens[origin] = token

        def _fake_delete_token(origin=None):
            assert origin is not None
            tokens.pop(origin, None)

        monkeypatch.setattr(main_window_module, "has_token", _fake_has_token)
        monkeypatch.setattr(main_window_module, "read_token", _fake_read_token)
        monkeypatch.setattr(status_view_module, "read_token", _fake_read_token)
        monkeypatch.setattr(status_view_module, "active_backend_name", lambda: "MemoryKeyring")
        monkeypatch.setattr(settings_dialog_module, "read_token", _fake_read_token)
        monkeypatch.setattr(settings_dialog_module, "store_token", _fake_store_token)
        monkeypatch.setattr(settings_dialog_module, "delete_token", _fake_delete_token)
        monkeypatch.setattr(settings_dialog_module, "active_backend_name", lambda: "MemoryKeyring")
        monkeypatch.setattr(main_window_module, "open_translation_memory", lambda: memory)

        class _StubTokenDialog:
            def __init__(self, parent=None):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def token(self):
                return next_token

            def show_store_error(self, _msg):
                raise AssertionError("unexpected store error")

        monkeypatch.setattr(settings_dialog_module, "TokenDialog", _StubTokenDialog)
        monkeypatch.setattr(
            settings_dialog_module.QMessageBox,
            "exec",
            lambda _self: QMessageBox.StandardButton.Yes,
        )

        review_started = asyncio.Event()
        release_review = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            if request.url.path == "/v1/meta":
                gv = "3.7.0" if auth.endswith("tok-a") else "3.8.0"
                return httpx.Response(200, json=_meta_payload(source_game_version=gv))
            if request.url.path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": [], "request_id": "caps"})
            if request.url.path == "/v1/reviews":
                review_started.set()
                await release_review.wait()
                return httpx.Response(200, json=_review_response(
                    body["source"], body["target"],
                    source_commit="a" * 40, term_count=42, revision=REVISION,
                ))
            raise AssertionError(f"unexpected path: {request.url.path}")

        client = ApiClient(
            "https://a.example",
            token_provider=lambda: tokens.get("https://a.example"),
            _test_transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(main_window_module.ApiClient, "from_config", classmethod(lambda cls, _cfg: client))

        window = MainWindow(ClientConfig(base_url="https://a.example"))
        try:
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            assert window._meta is not None and window._meta.source_game_version == "3.7.0"

            view = window.manuscript_view
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)
            live_dict = {
                "revision": REVISION,
                "source_commit": window._meta.source_commit,
                "schema_version": window._meta.schema_version,
                "term_count": window._meta.term_count,
                "profile": window._meta.source_profile,
            }
            view._record_live_review_dictionary(live_dict)

            # Start an in-flight review on active origin https://a.example.
            view._start_review()
            task_a = view._task
            await asyncio.wait_for(review_started.wait(), timeout=2.0)
            gen_before = view._controller.generation

            # 1. Edit & delete credential for another origin https://b.example, then Cancel.
            def _other_origin_exec(dlg: settings_dialog_module.SettingsDialog) -> int:
                nonlocal next_token
                dlg.base_url_edit.setText("https://b.example")
                next_token = "tok-b"
                dlg._on_enter_token_clicked()
                dlg._on_forget_token_clicked()
                return QDialog.DialogCode.Rejected

            monkeypatch.setattr(settings_dialog_module.SettingsDialog, "exec", _other_origin_exec)
            window._on_settings_clicked()

            assert window.api_client.current_origin == "https://a.example"
            assert window._meta is not None and window._meta.source_game_version == "3.7.0"
            assert view._service_meta is not None
            assert view._live_review_dictionary == live_dict
            assert view._controller.generation == gen_before

            release_review.set()
            if task_a is not None:
                await asyncio.wait_for(task_a, timeout=2.0)
            assert view._controller.generation == gen_before

            # 2. Change token for active origin https://a.example -> cancels BEFORE dialog closes!
            review_started.clear()
            release_review.clear()
            view._start_review()
            task_b = view._task
            await asyncio.wait_for(review_started.wait(), timeout=2.0)
            gen_before_2 = view._controller.generation
            seen_inside_exec: dict[str, object] = {}

            def _same_origin_exec(dlg: settings_dialog_module.SettingsDialog) -> int:
                nonlocal next_token
                next_token = "tok-a2"
                dlg._on_enter_token_clicked()
                seen_inside_exec["meta_is_none"] = window._meta is None
                seen_inside_exec["service_meta_is_none"] = view._service_meta is None
                seen_inside_exec["live_dict_is_none"] = view._live_review_dictionary is None
                seen_inside_exec["gen_bumped"] = view._controller.generation > gen_before_2
                return QDialog.DialogCode.Rejected

            monkeypatch.setattr(settings_dialog_module.SettingsDialog, "exec", _same_origin_exec)
            window._on_settings_clicked()

            assert seen_inside_exec == {
                "meta_is_none": True,
                "service_meta_is_none": True,
                "live_dict_is_none": True,
                "gen_bumped": True,
            }
            assert tokens.get("https://a.example") == "tok-a2"

            release_review.set()
            if task_b is not None:
                await asyncio.gather(task_b, return_exceptions=True)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            assert window._meta is not None and window._meta.source_game_version == "3.8.0"
        finally:
            await client.aclose()
            window.manuscript_view._saved_payload = dict(window.manuscript_view._payload)
            window.close()

    asyncio.run(scenario())


# -- the served dictionary's identity (ADR 0023, `dictionary-identity-v1`) -----


async def _settle_translation(view) -> None:
    """Wait out a press of 翻译, probe and run, whichever is in flight."""
    for _ in range(200):
        task = view._controller.translate_task
        if task is None:
            return
        await asyncio.wait_for(task, timeout=2.0)
        await asyncio.sleep(0)
    raise AssertionError("the translation never settled")


def _dictionary_payload(revision: str = REVISION, *, game_version: str | None = "3.7.0",
                        schema_version: str = "1", source_commit: str = "a" * 40,
                        term_count: int = 42) -> dict:
    return {
        "revision": revision,
        "schema_version": schema_version,
        "source_commit": source_commit,
        "source_game_version": game_version,
        "term_count": term_count,
        "request_id": "req-dictionary",
    }


def test_the_served_dictionary_identity_is_live_provenance_without_a_review(qapp, memory) -> None:
    """No term review in this session, and the revision is still known.

    This is the whole reason the endpoint exists: before it, `terms_db_revision`
    only appeared after a `POST /v1/reviews`, so a document reopened in a fresh
    session paid for every segment again. The 翻译 press asks for the identity
    once and reuses the confirmed segment with zero model calls.
    """
    asked: list[str] = []

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            asked.append(path)
            if path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": ["dictionary-identity-v1"],
                                                 "request_id": "caps"})
            if path == "/v1/dictionary":
                return httpx.Response(200, json=_dictionary_payload())
            if path == "/v1/translations":
                body = json.loads(request.content)
                return httpx.Response(200, json={
                    "kind": "llm", "text": f"[MT:{body['text']}]", "direction": body.get("to") or "en",
                    "dictionary_miss": False, "request_id": "tr-1",
                })
            raise AssertionError(f"unexpected path: {path}")

        client = ApiClient("https://api.example.invalid", token_provider=lambda: "wtd1.test",
                           _test_transport=httpx.MockTransport(handler))
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION, game_version="3.7.0")
        view = ManuscriptView(client, translation_memory=memory)
        try:
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)
            assert view._terms_db_revision() is None, "nothing has verified a dictionary yet"

            view._controller.start_translation()
            await _settle_translation(view)

            assert asked.count("/v1/dictionary") == 1, f"the identity was not fetched once: {asked}"
            assert "/v1/reviews" not in asked, "a review was spent to learn the revision"
            assert view._terms_db_revision() == REVISION
            assert view._game_version() == "3.7.0"
            assert asked.count("/v1/translations") == 0, "the confirmed segment cost a model call"
            assert view._payload["target"] == "甲句。\n\n乙句。", view._payload["target"]
            assert view._controller.last_tm_translation_summary["reused_segments"] == 1
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_a_confirmation_records_the_served_dictionary_identity(qapp, memory) -> None:
    """The identity reaches the memory, not only the status line: an entry the
    person confirms in a session that never ran a review is still recorded
    against the served revision, so a later session can classify it."""
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        source, target, alignments = two_segments()
        install(view, source, target, alignments=alignments)
        view.set_service_dictionary(DictionaryIdentityResult(
            revision=REVISION, schema_version="1", source_commit="a" * 40,
            source_game_version="3.7.0", term_count=42, request_id="req-dictionary",
        ))
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        view.tm_panel.confirm_button.click()

        (entry,) = memory.entries()
        assert entry.source_text == "First line."
        assert entry.terms_db_revision == REVISION
        assert entry.game_version == "3.7.0"
    finally:
        view.close()


def test_a_dictionary_edit_with_the_same_meta_tuple_still_makes_entries_stale(qapp, memory) -> None:
    """The tuple cannot see this change; the revision can.

    `schema_version`, `source_commit` and `term_count` are identical on both
    sides - exactly the case ADR 0023 says the tuple cannot distinguish - so the
    entry must not be reused, and the panel has to say why.
    """
    asked: list[str] = []

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            asked.append(path)
            if path == "/v1/meta":
                return httpx.Response(200, json=_meta_payload(schema_version="1", source_commit="a" * 40,
                                                              term_count=42, source_game_version="3.7.0"))
            if path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": ["dictionary-identity-v1"],
                                                 "request_id": "caps"})
            if path == "/v1/dictionary":
                # Same tuple, different content: an entry changed and nothing else did.
                return httpx.Response(200, json=_dictionary_payload(OTHER_REVISION))
            if path == "/v1/translations":
                body = json.loads(request.content)
                return httpx.Response(200, json={
                    "kind": "llm", "text": f"[MT:{body['text']}]", "direction": body.get("to") or "en",
                    "dictionary_miss": False, "request_id": "tr-1",
                })
            raise AssertionError(f"unexpected path: {path}")

        client = ApiClient("https://api.example.invalid", token_provider=lambda: "wtd1.test",
                           _test_transport=httpx.MockTransport(handler))
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION, game_version="3.7.0")
        view = ManuscriptView(client, translation_memory=memory)
        try:
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)
            view.set_service_meta(MetaResult.from_json(_meta_payload(
                schema_version="1", source_commit="a" * 40, term_count=42, source_game_version="3.7.0",
            )))
            view.set_service_dictionary(DictionaryIdentityResult(
                revision=OTHER_REVISION, schema_version="1", source_commit="a" * 40,
                source_game_version="3.7.0", term_count=42, request_id="req-dictionary",
            ))
            view.segment_list.setCurrentRow(1)
            view._refresh_tm_panel()

            assert view._terms_db_revision() == OTHER_REVISION
            assert [match.freshness for match in view.tm_panel.matches] == [tm.TM_FRESHNESS_STALE], (
                "an entry confirmed under another dictionary was treated as fresh"
            )

            view._controller.start_translation()
            await _settle_translation(view)

            assert asked.count("/v1/translations") == 1, "a stale entry was skipped"
            assert "乙句。" not in view._payload["target"]
            assert view._controller.last_tm_translation_summary is None
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_a_server_without_the_capability_keeps_the_meta_and_review_path(qapp, memory) -> None:
    """An older Core: the endpoint is never requested, and the revision still
    arrives through the review this session runs - the path MY-491 #64 built."""
    asked: list[str] = []

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            asked.append(path)
            if path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": [], "request_id": "caps"})
            if path == "/v1/dictionary":
                raise AssertionError("a server without the capability was asked for a dictionary")
            if path == "/v1/reviews":
                body = json.loads(request.content)
                return httpx.Response(200, json=_review_response(body["source"], body["target"]))
            raise AssertionError(f"unexpected path: {path}")

        client = ApiClient("https://api.example.invalid", token_provider=lambda: "wtd1.test",
                           _test_transport=httpx.MockTransport(handler))
        view = ManuscriptView(client, translation_memory=memory)
        try:
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)
            view._start_review()
            assert view._task is not None
            await asyncio.wait_for(view._task, timeout=2.0)

            assert view._terms_db_revision() == REVISION
            assert asked.count("/v1/dictionary") == 0
        finally:
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_the_live_identity_is_cleared_by_every_reset(qapp, memory) -> None:
    """A revision names the dictionary one service was serving. After a failure,
    another address or a new credential it is not knowledge any more - and an
    unknown revision reuses nothing, which is the safe direction."""
    identity = DictionaryIdentityResult(
        revision=REVISION, schema_version="1", source_commit="a" * 40,
        source_game_version="3.7.0", term_count=42, request_id="req-dictionary",
    )
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        for reset in (
            lambda: view.set_service_dictionary(None),
            lambda: view.set_service_meta(None),
            view.reset_for_credential_change,
            view.reset_for_endpoint_change,
        ):
            view.set_service_dictionary(identity)
            assert view._terms_db_revision() == REVISION
            reset()
            assert view._service_dictionary is None, reset
            assert view._terms_db_revision() is None, reset
    finally:
        view.close()


def test_a_review_dictionary_against_another_dictionary_is_dropped(qapp, memory) -> None:
    """Both endpoints state a revision; when they disagree the served identity
    is the authority and the review's basis is stale, so it is not adopted and
    a refresh is requested."""
    identity = DictionaryIdentityResult(
        revision=REVISION, schema_version="1", source_commit="a" * 40,
        source_game_version="3.7.0", term_count=42, request_id="req-dictionary",
    )
    review_dictionary = {"schema_version": "1", "source_commit": "a" * 40, "term_count": 42,
                         "revision": OTHER_REVISION}
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    requested: list[int] = []
    view.serviceMetaRefreshRequested.connect(lambda: requested.append(1))
    try:
        view.set_service_dictionary(identity)
        view._record_live_review_dictionary(review_dictionary)

        # A review is served LATER than the identity, so its disagreement means
        # the cached revision may be the superseded one: neither statement is
        # kept, and reuse stays off until a refresh settles it.
        assert view._live_review_dictionary is None, "a stale review basis was adopted"
        assert view._service_dictionary is None, "a possibly superseded identity stayed authoritative"
        assert requested, "nothing asked for a refresh"
        assert view._terms_db_revision() is None
        assert view._controller._tm_provenance() == (None, None), "a superseded revision was still reported"

        # The other order: a review was recorded first and the identity arrives after.
        view.set_service_dictionary(None)
        view._record_live_review_dictionary({"schema_version": "1", "source_commit": "a" * 40,
                                             "term_count": 42, "revision": OTHER_REVISION})
        assert view._live_review_dictionary is not None
        view.set_service_dictionary(identity)
        assert view._live_review_dictionary is None
        assert view._terms_db_revision() == REVISION
    finally:
        view.close()


def test_the_window_fetches_the_identity_with_the_meta_and_clears_it_on_failure(
    qapp, tmp_path, monkeypatch, memory
) -> None:
    """The window's meta refresh is where this session learns the service is
    reachable, so it is where the dictionary is identified too. When that
    request fails the identity is dropped rather than kept from the last good
    answer."""
    from wuwaterm_client import config as config_module
    from wuwaterm_client.ui import main_window as main_window_module

    async def scenario() -> None:
        monkeypatch.setattr(config_module, "app_data_dir", lambda: tmp_path)
        monkeypatch.setattr(main_window_module, "has_token", lambda: True)
        monkeypatch.setattr(main_window_module, "open_translation_memory", lambda: memory)

        state = {"dictionary_fails": False}
        asked: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            asked.append(path)
            if path == "/v1/meta":
                return httpx.Response(200, json=_meta_payload())
            if path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": ["dictionary-identity-v1"],
                                                 "request_id": "caps"})
            if path == "/v1/dictionary":
                if state["dictionary_fails"]:
                    return httpx.Response(503, json={"error": {"code": "llm_unavailable"}, "request_id": "req-d"})
                return httpx.Response(200, json=_dictionary_payload())
            raise AssertionError(f"unexpected path: {path}")

        client = ApiClient("https://api.example.invalid", token_provider=lambda: "wtd1.test",
                           _test_transport=httpx.MockTransport(handler))
        monkeypatch.setattr(main_window_module.ApiClient, "from_config", classmethod(lambda cls, _cfg: client))

        window = MainWindow(ClientConfig(base_url="https://api.example.invalid"))
        try:
            await asyncio.sleep(0)
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            if window._dictionary_task is not None:
                await asyncio.wait_for(window._dictionary_task, timeout=2.0)

            view = window.manuscript_view
            assert asked.count("/v1/dictionary") == 1
            assert view._service_dictionary is not None
            assert view._terms_db_revision() == REVISION
            assert view._game_version() == "3.7.0"

            state["dictionary_fails"] = True
            window.status_view._on_refresh_clicked()
            if window.status_view._task is not None:
                await asyncio.wait_for(window.status_view._task, timeout=2.0)
            if window._dictionary_task is not None:
                await asyncio.wait_for(window._dictionary_task, timeout=2.0)

            assert asked.count("/v1/dictionary") == 2
            assert view._service_dictionary is None, "a failed refresh kept the previous identity"
            assert view._terms_db_revision() is None
        finally:
            await client.aclose()
            window.manuscript_view._saved_payload = dict(window.manuscript_view._payload)
            window.close()

    asyncio.run(scenario())


def test_the_identity_wait_shows_cancel_and_cancelling_leaves_the_text_alone(qapp, memory) -> None:
    """The press that waits for the dictionary is cancellable on screen.

    The probe is a new network round trip on the path to 翻译, so it has to look
    like one: busy controls, a Cancel button, and a status line that says what
    is being waited for. Cancelling it must not write a reused segment or a
    translated one - no model call has been made yet.
    """
    asked: list[str] = []
    gate: list[asyncio.Event] = []

    async def scenario() -> None:
        release = asyncio.Event()
        gate.append(release)

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            asked.append(path)
            if path == "/v1/capabilities":
                return httpx.Response(200, json={"api_version": "v1", "capabilities": ["dictionary-identity-v1"],
                                                 "request_id": "caps"})
            if path == "/v1/dictionary":
                await release.wait()
                return httpx.Response(200, json=_dictionary_payload())
            if path == "/v1/translations":
                body = json.loads(request.content)
                return httpx.Response(200, json={
                    "kind": "llm", "text": f"[MT:{body['text']}]", "direction": body.get("to") or "en",
                    "dictionary_miss": False, "request_id": "tr-1",
                })
            raise AssertionError(f"unexpected path: {path}")

        client = ApiClient("https://api.example.invalid", token_provider=lambda: "wtd1.test",
                           _test_transport=httpx.MockTransport(handler))
        memory.add("Second line.", "乙句。", "en", prev_source="First line.", origin="human_confirmed",
                   terms_db_revision=REVISION, game_version="3.7.0")
        view = ManuscriptView(client, translation_memory=memory)
        try:
            source, target, alignments = two_segments()
            install(view, source, target, alignments=alignments)

            view._translate()
            for _ in range(200):
                if "/v1/dictionary" in asked:
                    break
                await asyncio.sleep(0)
            assert "/v1/dictionary" in asked, asked

            assert view.cancel_button.isHidden() is False, "a waiting press offered no Cancel"
            assert view.cancel_button.isEnabled() is True
            assert view._translating() is True
            assert view.translate_button.isEnabled() is False, "a second press was left available"
            assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_PROVENANCE

            view.cancel_button.click()
            for _ in range(200):
                if view._controller.translate_task is None:
                    break
                await asyncio.sleep(0)

            assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_PROVENANCE_CANCELLED
            assert view.cancel_button.isHidden() is True
            assert view.translate_button.isEnabled() is True
            assert view._payload["target"] == target, "a cancelled press wrote text"
            assert "/v1/translations" not in asked

            release.set()
            for _ in range(50):
                await asyncio.sleep(0)
            assert view._payload["target"] == target
            assert view._controller.translate_task is None
            assert "/v1/translations" not in asked, "the cancelled press started a run"
        finally:
            gate[0].set()
            await client.aclose()
            view.close()

    asyncio.run(scenario())


def test_a_meta_refresh_that_names_another_dictionary_drops_the_identity(qapp, memory) -> None:
    """The dictionary can change between two refreshes.

    A `/v1/meta` naming another term count or source commit describes a state
    the cached identity no longer belongs to, and the replacement identity
    arrives in its own request afterwards. Keeping the old one in between would
    let a 翻译 pressed in that window reuse entries confirmed under the previous
    dictionary - so the identity goes, and only the identity's own request may
    put one back.
    """
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        source, target, alignments = two_segments()
        install(view, source, target, alignments=alignments)
        identity = DictionaryIdentityResult(
            revision=REVISION, schema_version="1", source_commit="a" * 40,
            source_game_version="3.7.0", term_count=42, request_id="req-dictionary",
        )
        view.set_service_meta(MetaResult.from_json(_meta_payload(
            schema_version="1", source_commit="a" * 40, term_count=42, source_game_version="3.7.0",
        )))
        view.set_service_dictionary(identity)
        assert view._terms_db_revision() == REVISION

        # Same dictionary state: the identity still describes it.
        view.set_service_meta(MetaResult.from_json(_meta_payload(
            schema_version="1", source_commit="a" * 40, term_count=42, source_game_version="3.7.0",
        )))
        assert view._terms_db_revision() == REVISION

        # Another dictionary: the cached revision is not knowledge any more.
        view.set_service_meta(MetaResult.from_json(_meta_payload(
            schema_version="1", source_commit="b" * 40, term_count=43, source_game_version="3.8.0",
        )))
        assert view._service_dictionary is None
        assert view._terms_db_revision() is None
    finally:
        view.close()
