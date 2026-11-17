"""Real Qt composition and manuscript history preserve the editing surface."""
from __future__ import annotations

import copy

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QRect, Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.ui.manuscript_view import ManuscriptView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _hint_region(editor):
    # Exclude the short preedit and blinking caret; the hint extends here.
    return editor.viewport().grab(QRect(100, 0, 180, 70)).toImage()


@pytest.mark.parametrize("side", ["source", "target"])
def test_empty_hint_does_not_paint_over_qt_preedit_and_returns_on_cancel(qapp, side):
    view = ManuscriptView(ApiClient(None))
    try:
        view.resize(1100, 600)
        view.show()
        editor = getattr(view, side + "_edit")
        editor.setFocus()
        qapp.processEvents()
        hint = editor.placeholderText()
        editor.setPlaceholderText("")
        blank = _hint_region(editor)
        editor.setPlaceholderText(hint)
        with_hint = _hint_region(editor)
        assert with_hint != blank, "the paint check must actually see the hint"

        QCoreApplication.sendEvent(editor, QInputMethodEvent("ni", []))
        qapp.processEvents()
        assert editor._ime_active and editor.raw_text() == ""
        assert _hint_region(editor) == blank, "the hint must not paint beneath preedit"
        assert view._undo_history == []

        QCoreApplication.sendEvent(editor, QInputMethodEvent())
        qapp.processEvents()
        assert not editor._ime_active and editor.raw_text() == ""
        assert editor.placeholderText() == hint
        assert _hint_region(editor) == with_hint
        assert view._undo_history == []

        QCoreApplication.sendEvent(editor, QInputMethodEvent("ni", []))
        commit = QInputMethodEvent()
        commit.setCommitString("你😀")
        QCoreApplication.sendEvent(editor, commit)
        assert editor.raw_text() == view._payload[side] == "你😀"
        assert not editor._ime_active
        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        qapp.processEvents()
        assert editor.raw_text() == "" and _hint_region(editor) == with_hint
    finally:
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _utf16(text, offset):
    return len(text[:offset].encode("utf-16-le")) // 2


def _surface(editor):
    cursor = editor.textCursor()
    return (cursor.position(), cursor.anchor(), editor.verticalScrollBar().value(),
            editor.horizontalScrollBar().value())


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("kind", ["key", "ime", "paste"])
def test_real_edit_history_restores_utf16_selection_focus_and_scroll(qapp, side, kind):
    raw = "".join(f"row{i:03d} 😀\xa0unit\r\n" for i in range(100))
    view = ManuscriptView(ApiClient(None))
    try:
        value = empty_workfile()
        value.update(source=raw, target=raw, direction="en")
        view._set_payload(value)
        view.resize(1000, 520)
        view.show()
        qapp.processEvents()
        editor = getattr(view, side + "_edit")
        other = view.target_edit if side == "source" else view.source_edit
        for widget, row in ((other, 22), (editor, 55)):
            shown = widget.toPlainText()
            left = shown.index("😀", shown.index(f"row{row:03d}"))
            right = left + len("😀 unit")
            cursor = widget.textCursor()
            cursor.setPosition(_utf16(shown, right))
            cursor.setPosition(_utf16(shown, left), QTextCursor.MoveMode.KeepAnchor)
            widget.setTextCursor(cursor)
        editor.setFocus()
        qapp.processEvents()
        for widget, row in ((editor, 55), (other, 22)):
            bar = widget.verticalScrollBar()
            assert bar.maximum() > row
            bar.setValue(row - 4)
            assert bar.value() > 0
        before_payload = copy.deepcopy(view._draft_snapshot())
        before = {s: _surface(getattr(view, s + "_edit")) for s in ("source", "target")}
        raw_start = raw.index("😀", raw.index("row055"))
        raw_end = raw_start + len("😀\xa0unit")

        if kind == "key":
            replacement = "X"
            QTest.keyClicks(editor, replacement)
        elif kind == "ime":
            replacement = "你😀"
            QCoreApplication.sendEvent(editor, QInputMethodEvent("ni", []))
            commit = QInputMethodEvent()
            commit.setCommitString(replacement)
            QCoreApplication.sendEvent(editor, commit)
        else:
            replacement = "你😀\r\nnew\xa0"
            mime = QMimeData()
            mime.setText(replacement)
            editor.insertFromMimeData(mime)
        qapp.processEvents()
        expected = raw[:raw_start] + replacement + raw[raw_end:]
        assert editor.raw_text() == view._payload[side] == expected
        assert other.raw_text() == raw
        after_payload = copy.deepcopy(view._draft_snapshot())
        after = {s: _surface(getattr(view, s + "_edit")) for s in ("source", "target")}

        if kind == "key":
            QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        else:
            QTest.mouseClick(view.undo_button, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert view._draft_snapshot() == before_payload
        assert {s: _surface(getattr(view, s + "_edit")) for s in before} == before
        assert editor.hasFocus()

        if kind == "key":
            QTest.keyClick(editor, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        else:
            QTest.mouseClick(view.redo_button, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert view._draft_snapshot() == after_payload
        assert {s: _surface(getattr(view, s + "_edit")) for s in after} == after
        assert editor.hasFocus()
        QTest.keyClicks(editor, "Z")
        assert editor.raw_text() == raw[:raw_start] + replacement + "Z" + raw[raw_end:]
        assert set(view._draft_snapshot()) == set(before_payload), "UI positions are not manuscript fields"
    finally:
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
