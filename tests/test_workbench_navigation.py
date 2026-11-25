"""Keyboard navigation, scroll sync, caret following, the five review states
and the status bar of the manuscript workbench (offscreen Qt).

What these cannot show: a real input method's candidate window and a drag
between monitors with different scaling. Those stay owner checks on Windows.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wuwaterm_client import document_edits as de
from wuwaterm_client import strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.recovery import load_recovery, save_recovery
from wuwaterm_client.ui import review_states
from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, TRACK_TERMS, ManuscriptView
from wuwaterm_client.ui.scroll_sync import map_offset, monotonic_anchors
from wuwaterm_client.ui.shortcut_sheet import SHORTCUTS, registered_sequences


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# Eight paragraphs of very different lengths on the two sides, so a ratio of
# the scroll bars lands in the wrong paragraph and only the anchors land right.
# Each is one segment: no sentence ends inside it, and every paragraph is long
# enough that segmentation-v1 never merges two neighbours.
SOURCE_PARAGRAPHS = [f"第{n:02d}段" + "原文" * (520 if n % 2 else 800) for n in range(8)]
TARGET_PARAGRAPHS = [f"Paragraph {n:02d} " + "word " * (330 if n % 2 else 210) for n in range(8)]
SOURCE = "\n\n".join(SOURCE_PARAGRAPHS)
TARGET = "\n\n".join(TARGET_PARAGRAPHS)


def _view(source: str = SOURCE, target: str = TARGET, *, alignments=None) -> ManuscriptView:
    view = ManuscriptView(ApiClient(None))
    value = empty_workfile()
    value.update(source=source, target=target, direction="en", alignments=alignments)
    view._set_payload(value)
    # As if opened from a file (a long one already holds its segment records):
    # nothing unsaved, so any edit would show.
    view._records()
    view._saved_payload = copy.deepcopy(view._payload)
    view._update_controls()
    view.resize(1200, 760)
    view.show()
    QApplication.processEvents()
    return view


def _caret(editor) -> int:
    return editor.raw_offset_at_position(editor.textCursor().position())


def _pause(ms: int) -> None:
    """Let timers fire for ``ms`` without a nested event loop: QTest.qWait would
    also run every deferred deletion other test modules left queued."""
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


# -- scroll sync ----------------------------------------------------------------

def test_anchor_mapping_interpolates_inside_a_segment_and_refuses_a_crossing_list() -> None:
    anchors = monotonic_anchors([(0, 0), (10, 100), (20, 120)])
    assert map_offset(anchors, 5, forward=True) == 50
    assert map_offset(anchors, 15, forward=True) == 110
    assert map_offset(anchors, 110, forward=False) == 15
    assert map_offset(anchors, 99, forward=True) == 120
    assert monotonic_anchors([(0, 0), (10, 50), (20, 40)]) is None
    assert monotonic_anchors([]) is None


def test_scrolling_one_editor_brings_the_same_segment_to_the_top_of_the_other(qapp) -> None:
    view = _view()
    try:
        pairs = view._current_pairs()
        assert len(pairs) == 8 and all(pair.status == "ready" for pair in pairs)
        assert view.scroll_sync.is_enabled(), "on by default"
        pair = pairs[6]
        view.source_edit.scroll_to_raw_offset(pair.source.start)
        QApplication.processEvents()
        assert view.target_edit.raw_offset_at_top() == pair.target.start
        # And back the other way.
        pair = pairs[3]
        view.target_edit.scroll_to_raw_offset(pair.target.start)
        QApplication.processEvents()
        assert view.source_edit.raw_offset_at_top() == pair.source.start

        view.scroll_sync.set_enabled(False)
        before = view.target_edit.verticalScrollBar().value()
        view.source_edit.scroll_to_raw_offset(pairs[2].source.start)
        QApplication.processEvents()
        assert view.target_edit.verticalScrollBar().value() == before, "switched off, the target stays"
    finally:
        view.close()


def test_an_undo_restores_both_editors_scroll_positions_without_the_sync_moving_them(qapp) -> None:
    view = _view()
    try:
        view._current_pairs()
        with view.scroll_sync.suspended():
            view.source_edit.verticalScrollBar().setValue(3)
            view.target_edit.verticalScrollBar().setValue(17)
        recorded = (view.source_edit.verticalScrollBar().value(), view.target_edit.verticalScrollBar().value())
        cursor = view.target_edit.cursorForPosition(view.target_edit.viewport().rect().center())
        cursor.insertText("x")
        # Somewhere else entirely, synced.
        view.source_edit.verticalScrollBar().setValue(view.source_edit.verticalScrollBar().maximum())
        QApplication.processEvents()
        view._undo_replace()
        QApplication.processEvents()
        assert view._payload["target"] == TARGET
        assert (view.source_edit.verticalScrollBar().value(),
                view.target_edit.verticalScrollBar().value()) == recorded
    finally:
        view.close()


# -- keyboard shortcuts -----------------------------------------------------------

def test_alt_up_and_alt_down_jump_segments_in_the_editor_that_has_the_focus(qapp) -> None:
    view = _view()
    try:
        pairs = view._current_pairs()
        view.target_edit.setFocus()
        QApplication.processEvents()
        view.target_edit.set_raw_cursor_offset(pairs[2].target.core_start + 3)
        QTest.keyClick(view.target_edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
        assert _caret(view.target_edit) == pairs[3].target.core_start
        QTest.keyClick(view.target_edit, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
        assert _caret(view.target_edit) == pairs[2].target.core_start
        QTest.keyClick(view.target_edit, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
        assert _caret(view.target_edit) == pairs[1].target.core_start
        assert QApplication.focusWidget() is view.target_edit
        assert not view.document_dirty, "navigating never edits"
        view.target_edit.set_raw_cursor_offset(pairs[7].target.core_start)
        QTest.keyClick(view.target_edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
        assert view.status_label.text() == strings.MANUSCRIPT_SEGMENT_LAST
    finally:
        view.close()


def test_alt_left_and_alt_right_move_the_focus_between_the_editors(qapp) -> None:
    view = _view()
    try:
        view.source_edit.setFocus()
        QApplication.processEvents()
        QTest.keyClick(view.source_edit, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
        assert QApplication.focusWidget() is view.target_edit
        QTest.keyClick(view.target_edit, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
        assert QApplication.focusWidget() is view.source_edit
    finally:
        view.close()


def _term_report(source: str, target: str, verdicts: list[str]) -> dict:
    findings = []
    for n, verdict in enumerate(verdicts):
        marker = f"第{n:02d}段"
        start = source.index(marker)
        t_marker = f"Paragraph {n:02d}"
        t_start = target.index(t_marker)
        finding = {"id": f"f{n}", "rule_id": "term", "verdict": verdict,
                   "source_span": {"start": start, "end": start + len(marker), "text": marker},
                   "target_span": {"start": t_start, "end": t_start + len(t_marker), "text": t_marker},
                   "candidates": [], "candidates_truncated": False}
        if verdict == "not_evaluated":
            finding["reason"] = "ordinary_word"
        findings.append(finding)
    digest = hashlib.sha256(b"x").hexdigest()
    return {"request_id": "nav", "source_revision": digest, "target_revision": digest,
            "rule_version": "review-v2", "matcher_revision": "a" * 64,
            "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 1, "revision": "a" * 64},
            "coverage": {"evaluated": len(findings), "not_evaluated": 0, "rules": ["term"]},
            "findings": findings, "truncated": False}


def test_f7_steps_through_conflicts_and_checks_only_and_wraps(qapp) -> None:
    view = _view()
    try:
        verdicts = ["verified_constraint", "confirmed_conflict", "not_evaluated", "needs_review", "verified_constraint"]
        view._show_term_report(_term_report(SOURCE, TARGET, verdicts))
        view.findings.setCurrentRow(0)
        view.source_edit.setFocus()
        QApplication.processEvents()
        QTest.keyClick(view.source_edit, Qt.Key.Key_F7)
        assert view.findings.currentRow() == 1
        QTest.keyClick(view.source_edit, Qt.Key.Key_F7)
        assert view.findings.currentRow() == 3
        QTest.keyClick(view.source_edit, Qt.Key.Key_F7)
        assert view.findings.currentRow() == 1, "wraps to the first problem"
        QTest.keyClick(view.source_edit, Qt.Key.Key_F7, Qt.KeyboardModifier.ShiftModifier)
        assert view.findings.currentRow() == 3
        assert view.track_stack.currentIndex() == TRACK_TERMS
        assert view.status_label.text().startswith(strings.MANUSCRIPT_TERM_PROBLEM_AT.format(current=2, count=2))
    finally:
        view.close()


def test_ctrl_alt_digits_switch_tracks_and_leave_an_absent_track_closed(qapp) -> None:
    view = _view()
    try:
        view.source_edit.setFocus()
        QApplication.processEvents()
        QTest.keyClick(view.source_edit, Qt.Key.Key_3,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        assert view.track_stack.currentIndex() == TRACK_SEGMENTS
        assert view.semantic_track_button.isHidden(), "no semantic capability on this service"
        QTest.keyClick(view.source_edit, Qt.Key.Key_2,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        assert view.track_stack.currentIndex() == TRACK_SEGMENTS
        assert view.status_label.text() == strings.MANUSCRIPT_SEMANTIC_TRACK_UNAVAILABLE
        QTest.keyClick(view.source_edit, Qt.Key.Key_1,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        assert view.track_stack.currentIndex() == TRACK_TERMS
        assert view._payload["source"] == SOURCE, "no character was typed"
    finally:
        view.close()


def test_a_character_typed_with_altgr_goes_to_the_editor_not_to_a_shortcut(qapp) -> None:
    """Windows reports AltGr as Ctrl+Alt: AltGr+2 types "²" on a German layout."""
    view = _view()
    try:
        both = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        typed = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_2, both, "²")
        assert view.source_edit.event(typed) and typed.isAccepted()
        shortcut = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_2, both, "")
        shortcut.ignore()
        view.source_edit.event(shortcut)
        assert not shortcut.isAccepted(), "no character: the shortcut keeps the key"
        same = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_2, both, "2")
        same.ignore()
        view.source_edit.event(same)
        assert not same.isAccepted(), "the key's own digit is not AltGr text"
        # The inspector's own text fields: "#" is AltGr+3 on a French layout.
        for field in (view.replacement_edit, view.adjust_edit):
            hash_key = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_3, both, "#")
            hash_key.ignore()
            QApplication.sendEvent(field, hash_key)
            assert hash_key.isAccepted(), field
    finally:
        view.close()


# -- caret following ----------------------------------------------------------------

def test_a_resting_caret_selects_its_segment_row_without_moving_the_caret_or_the_focus(qapp) -> None:
    view = _view()
    try:
        pairs = view._current_pairs()
        view._show_track(TRACK_SEGMENTS)
        view.target_edit.setFocus()
        QApplication.processEvents()
        view.target_edit.set_raw_cursor_offset(pairs[5].target.core_start + 5)
        caret = view.target_edit.textCursor().position()
        assert view._caret_timer.isActive() and view._caret_timer.interval() == 300
        _pause(400)
        assert view.segment_list.currentRow() == 5
        assert view.target_edit.textCursor().position() == caret
        assert QApplication.focusWidget() is view.target_edit
        assert view._segment_cursor is None, "following never records a selection"
        assert not view.document_dirty
    finally:
        view.close()


def test_a_resting_caret_inside_a_finding_makes_it_current_without_selecting_text(qapp) -> None:
    view = _view()
    try:
        view._show_term_report(_term_report(SOURCE, TARGET, ["confirmed_conflict", "needs_review", "needs_review"]))
        view.findings.setCurrentRow(0)
        view.source_edit.setFocus()
        QApplication.processEvents()
        finding = view._report["findings"][2]
        view.source_edit.set_raw_cursor_offset(finding["source_span"]["start"] + 1)
        cursor = view.source_edit.textCursor()
        view._follow_caret()
        assert view.findings.currentRow() == 2
        assert view.source_edit.textCursor().position() == cursor.position()
        assert not view.source_edit.textCursor().hasSelection()
        assert QApplication.focusWidget() is view.source_edit
    finally:
        view.close()


def _adjacent_report(source: str, target: str) -> dict:
    """Two findings that touch: A ends where B starts, on both sides."""
    def finding(n: int, zh: str, en: str) -> dict:
        return {"id": f"adj{n}", "rule_id": "term", "verdict": "needs_review",
                "source_span": {"start": n, "end": n + 1, "text": source[n]},
                "target_span": {"start": n, "end": n + 1, "text": target[n]},
                "candidates": [{"zh": zh, "en": en, "category": "example", "sources": [],
                                "candidate_id": hashlib.sha256(en.encode()).hexdigest()}],
                "candidates_truncated": False}
    digest = hashlib.sha256(b"adj").hexdigest()
    return {"request_id": "adj", "source_revision": digest, "target_revision": digest,
            "rule_version": "review-v2", "matcher_revision": "a" * 64,
            "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 2, "revision": "a" * 64},
            "coverage": {"evaluated": 2, "not_evaluated": 0, "rules": ["term"]},
            "findings": [finding(0, "甲", "Alpha"), finding(1, "乙", "Beta")], "truncated": False}


def test_a_chosen_finding_stays_current_when_its_start_touches_the_previous_one(qapp) -> None:
    view = _view("甲乙 rest of the source", "甲乙 rest of the target")
    try:
        view._show_term_report(_adjacent_report(view._payload["source"], view._payload["target"]))
        view.findings.setCurrentRow(1)
        assert view.replacement_edit.text() == "Beta"
        _pause(400)
        assert view.findings.currentRow() == 1, "the caret at B's start is inside B, not at A's end"
        assert view.replacement_edit.text() == "Beta"
    finally:
        view.close()


def test_following_the_caret_into_another_finding_offers_that_findings_replacement(qapp) -> None:
    view = _view("甲乙 rest of the source", "甲乙 rest of the target")
    try:
        view._show_term_report(_adjacent_report(view._payload["source"], view._payload["target"]))
        view.findings.setCurrentRow(0)
        assert view.replacement_edit.text() == "Alpha"
        view.source_edit.setFocus()
        view.source_edit.set_raw_cursor_offset(1)
        view._caret_side = "source"
        view._follow_caret()
        assert view.findings.currentRow() == 1
        assert view.replacement_edit.text() == "Beta", "never the previous finding's candidate"
        assert _caret(view.source_edit) == 1
    finally:
        view.close()


# -- the five states -------------------------------------------------------------------

def test_every_row_carries_one_of_the_five_states_and_a_saved_result_is_never_verified(qapp) -> None:
    view = _view()
    try:
        view._show_track(TRACK_SEGMENTS)
        assert view.segment_list.count() == 8
        assert all(view.segment_list.item(i).data(review_states.STATE_ROLE) in review_states.STATES
                   for i in range(view.segment_list.count()))
        view._show_term_report(_term_report(SOURCE, TARGET, ["verified_constraint", "confirmed_conflict"]),
                               saved=[True, False])
        states = [view.findings.item(i).data(review_states.STATE_ROLE) for i in range(2)]
        assert states == [review_states.STALE, review_states.CONFLICT]
        assert not view.findings.item(0).icon().isNull()
        assert view.findings.item(0).toolTip().startswith(strings.REVIEW_STATE_STALE)
        view.findings.setCurrentRow(0)
        assert view.finding_verdict.property("reviewState") == review_states.STALE
    finally:
        view.close()


def test_the_five_states_map_every_source_vocabulary() -> None:
    assert review_states.for_verdict("verified_constraint") == review_states.VERIFIED
    assert review_states.for_verdict("verified_constraint", saved=True) == review_states.STALE
    assert review_states.for_verdict("confirmed_conflict") == review_states.CONFLICT
    assert review_states.for_verdict("needs_review") == review_states.NEEDS_CHECK
    assert review_states.for_verdict("not_evaluated") == review_states.UNEVALUATED
    assert review_states.for_group("inconsistent") == review_states.CONFLICT
    assert review_states.for_group("pending", saved=True) == review_states.STALE
    assert review_states.for_segment("reviewed") == review_states.VERIFIED
    assert review_states.for_segment("stale") == review_states.STALE
    assert review_states.for_segment("untranslated") == review_states.UNEVALUATED
    assert review_states.for_segment("needs_manual_split") == review_states.NEEDS_CHECK
    assert review_states.for_issue("pending") == review_states.NEEDS_CHECK
    assert review_states.for_issue("accepted") == review_states.VERIFIED
    assert review_states.for_issue("needs_reconfirm") == review_states.STALE
    assert set(review_states.LABELS) == set(review_states.STATES)


# -- the status bar and the shortcut sheet ---------------------------------------------------

def test_the_status_bar_counts_the_manuscript_switches_scroll_sync_and_opens_the_sheet(qapp) -> None:
    from wuwaterm_client.ui import main_window as main_window_module
    from wuwaterm_client.config import ClientConfig

    window = main_window_module.MainWindow(ClientConfig())
    try:
        window.show()
        value = empty_workfile()
        value.update(source="甲 乙\n\n丙", target="One two\n\nThree", direction="en")
        window.manuscript_view._set_payload(value)
        window.manuscript_view._current_pairs()
        window._refresh_status_bar()
        text = window.document_stats_label.full_text()
        assert strings.STATUS_BAR_CHARS.format(source="3", target="11") in text
        assert window.dictionary_label.full_text() == strings.STATUS_BAR_DICTIONARY_UNKNOWN
        assert window.scroll_sync_button.text() == strings.STATUS_BAR_SCROLL_SYNC_ON
        window.scroll_sync_button.click()
        assert not window.manuscript_view.scroll_sync.is_enabled()
        assert window.scroll_sync_button.text() == strings.STATUS_BAR_SCROLL_SYNC_OFF
        window.shortcuts_action.trigger()
        assert window._shortcut_sheet is not None and window._shortcut_sheet.isVisible()
        assert window._shortcut_sheet.table.rowCount() == len(SHORTCUTS)
        # Another page: the manuscript's counts and its switch leave the bar.
        window.show_page(main_window_module.PAGE_TERMS)
        assert window.document_stats_label.isHidden() and window.scroll_sync_button.isHidden()
    finally:
        window.manuscript_view._document_revision = 0
        window.manuscript_view._saved_payload = window.manuscript_view._payload
        window.close()


def test_the_shortcut_sheet_lists_exactly_the_shortcuts_that_are_registered(qapp) -> None:
    from PySide6.QtGui import QAction, QShortcut
    from wuwaterm_client.ui import main_window as main_window_module
    from wuwaterm_client.config import ClientConfig

    window = main_window_module.MainWindow(ClientConfig())
    try:
        bound = {shortcut.key().toString() for shortcut in window.findChildren(QShortcut)}
        bound |= {action.shortcut().toString() for action in window.findChildren(QAction)
                  if not action.shortcut().isEmpty()}
        assert bound == registered_sequences()
        for keys, _sequences, zh, en in SHORTCUTS:
            assert keys and zh and en and en.isascii(), keys
    finally:
        window.close()


# -- session list in the recovery draft ---------------------------------------------------

def test_the_recovery_draft_carries_the_session_list_beside_the_workfile(tmp_path) -> None:
    item = {"original_index": 1, "reason": "source_emptied",
            "entry": {"source": {"start": 6, "end": 10, "text": "Beta"}, "target": {"start": 2, "end": 3, "text": "乙"}}}
    payload = empty_workfile()
    payload.update(source="Alpha  Gamma", target="甲 乙 丙", direction="en", alignments=[])
    path = tmp_path / "draft.json"
    save_recovery(path, payload, alignment_invalidations=[item, {"reason": "bogus"}])
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["alignment_invalidations"] == [item]
    assert "invalidat" not in envelope["workfile_json"]
    state = load_recovery(path)
    assert state.alignment_invalidations == (item,)
    save_recovery(path, payload)
    assert "alignment_invalidations" not in json.loads(path.read_text(encoding="utf-8"))
    assert load_recovery(path).alignment_invalidations == ()


# -- undo steps the workbench commands make ------------------------------------------------

class _Host:
    def editor_state(self) -> dict:
        return {}

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_a_line_break_and_a_command_are_steps_of_their_own_inside_a_typing_run() -> None:
    from wuwaterm_client import document_controller as dc

    now = [0.0]
    controller = dc.DocumentRunController(object(), _Host(), clock=lambda: now[0])
    payload = empty_workfile()
    payload.update(source="ab", target="", direction="en")
    controller.payload = payload

    def type_at(ms: float, text: str) -> None:
        now[0] = ms
        controller.edit_text("source", text)

    type_at(0, "abc")
    type_at(100, "abcd")
    assert len(controller.history.undo) == 1, "a typing run is one step"
    type_at(200, "abcd\n")
    assert len(controller.history.undo) == 2, "a paragraph break is its own step"
    type_at(300, "abcd\ne")
    assert len(controller.history.undo) == 3
    value = controller.draft_snapshot()
    value["target"] = "译"
    controller.apply_edit(value)
    type_at(400, "abcd\nef")
    assert len(controller.history.undo) == 5, "a command splits the run on both sides"
    controller.undo()
    assert controller.payload["source"] == "abcd\ne" and controller.payload["target"] == "译"
    assert de.HISTORY_LIMIT == 50
