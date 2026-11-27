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
from wuwaterm_client.api import ApiClient
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        # The open document's report names REVISION; the entry says another.
        view._report = {"dictionary": {"revision": REVISION}}
        view._refresh_tm_panel()
        card = view.tm_panel.hit_texts()[0]
        assert strings.TM_STALE_TERMS in card
        assert strings.TM_MATCH_RECHECK.format(reasons=strings.TM_STALE_TERMS) in card
    finally:
        view.close()


def test_adopting_a_candidate_is_one_edit_that_ctrl_z_takes_back(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        view._report = {"dictionary": {"revision": REVISION}}
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


def test_the_revision_comes_from_the_report_and_from_a_long_mode_basis(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed",
               terms_db_revision=REVISION)
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        # Nothing has named a revision yet, so nothing can be out of date.
        assert view._terms_db_revision() is None
        assert strings.TM_STALE_TERMS not in view.tm_panel.hit_texts()[0]
        report = {
            "request_id": "example", "source_revision": "a" * 64, "target_revision": "b" * 64,
            "rule_version": "review-v2", "matcher_revision": "c" * 64,
            "dictionary": {"schema_version": "1", "source_commit": "d" * 40, "term_count": 1,
                           "revision": OTHER_REVISION},
            "coverage": {"evaluated": 0, "not_evaluated": 0, "rules": ["review.term_pair"]},
            "findings": [], "truncated": False,
        }
        # A document report names the revision, and the panel follows at once:
        # no caret stop is needed to see that the entry is out of date.
        view._show_term_report(report, state="current")
        assert view._terms_db_revision() == OTHER_REVISION
        assert strings.TM_STALE_TERMS in view.tm_panel.hit_texts()[0]
        # A long manuscript keeps the basis per record instead of one report.
        view._report = None
        view._term_basis = {"rule_version": "review-v2", "matcher_revision": "c" * 64,
                            "dictionary": {"schema_version": "1", "source_commit": "d" * 40,
                                           "term_count": 1, "revision": OTHER_REVISION}}
        assert view._terms_db_revision() == OTHER_REVISION
        view._refresh_tm_panel()
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
        view._term_basis = {"dictionary": {"revision": REVISION, "game_version": "3.7.0"}}
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
        view._term_basis = {"dictionary": {"revision": REVISION, "source_commit": "c" * 40}}
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
        # Server report has source_commit but no explicit game_version -> _game_version() must be None.
        view._report = {
            "request_id": "r1",
            "findings": [],
            "dictionary": {"revision": OTHER_REVISION, "source_commit": "d" * 40},
        }
        view._term_basis = {"dictionary": {"revision": REVISION, "source_commit": "c" * 40}}
        install(view, source, target, alignments=alignments)
        assert view._game_version() is None
        # Report takes precedence over stale _term_basis in both view and controller.
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
        view._term_basis = {"dictionary": {"revision": REVISION}}
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
            view._term_basis = {"dictionary": {"revision": REVISION, "source_commit": source_commit}}
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
