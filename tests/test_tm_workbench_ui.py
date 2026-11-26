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

import hashlib
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from wuwaterm_client import strings
from wuwaterm_client import translation_memory as tm
from wuwaterm_client.api import ApiClient
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.manuscript import empty_workfile
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
            alignments: list[dict] | None = None) -> None:
    value = empty_workfile()
    value.update(source=source, target=target, direction=direction, alignments=alignments)
    view._set_payload(value)
    view._update_controls()
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
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view.segment_list.setCurrentRow(0)
        view._refresh_tm_panel()
        panel = view.tm_panel
        assert len(panel.matches) == 1
        card = panel.hit_texts()[0]
        # Same text, same neighbour: the strongest kind, and no warning.
        assert strings.TM_MATCH_CONTEXT_EXACT in card
        assert "首句。" in card
        assert strings.TM_STALE_TERMS not in card and strings.TM_STALE_NUMERIC not in card
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
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
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
    memory.add("First line.", "首句。", "en", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
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
        assert view.status_label.text() == strings.TM_PANEL_CHECK_NO_SOURCE
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
    memory.add("First line.", "甲句。", "en", next_source="Second line.", origin="human_confirmed")
    memory.add("Second line.", "乙句。", "en", prev_source="First line.", next_source="Third line.",
               origin="human_confirmed")
    memory.add("Third line, +5.", "丙句，+5。", "en", prev_source="Second line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        install(view, source, target, alignments=alignments)
        view._check_memory_reuse()
        summary = view.tm_panel.summary
        assert summary is not None
        # The first two match with their neighbours; the third only references.
        assert (summary.fresh, summary.stale, summary.unmatched) == (2, 1, 0)
        assert summary.fillable == 1
        assert view.status_label.text() == strings.TM_PANEL_CHECK_SUMMARY.format(fresh=2, stale=1, unmatched=0)
        assert view.tm_panel.fill_button.text() == strings.TM_PANEL_CHECK_FILL.format(n=1)

        # Filling writes the one empty target, as one edit that undo takes back.
        view._fill_from_memory()
        assert view._payload["target"] == "甲句。乙句。"
        assert view.status_label.text() == strings.TM_PANEL_CHECK_FILLED.format(n=1)
        view.target_edit.setFocus()
        QTest.keyClick(view.target_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert view._payload["target"] == target
    finally:
        view.close()


def test_the_reuse_check_writes_nothing_while_it_counts(qapp, memory) -> None:
    source, target, alignments = two_segments()
    memory.add("First line.", "首句。", "en", next_source="Second line.", origin="human_confirmed")
    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
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
