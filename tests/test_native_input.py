"""Where the operating system is told to draw IME candidates.

Offscreen checks prove the geometry the client reports. They do not prove where
a real Windows input method draws its candidate window; that stays on the
owner's device checklist.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication, QInputMethodEvent
from PySide6.QtWidgets import QApplication

from wuwaterm_client import theme
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _caret_in_window(window, editor) -> QRect:
    caret = editor.cursorRect()
    return QRect(editor.viewport().mapTo(window, caret.topLeft()), caret.size())


@pytest.mark.parametrize("side", ["source_edit", "target_edit"])
def test_ime_cursor_rectangle_matches_the_visible_caret(qapp, side) -> None:
    """The candidate window is placed from this rectangle. It used to omit the
    editor frame and stylesheet padding (20, 16 px in the workbench), so the
    candidate window covered the line being composed."""
    theme.apply_theme(qapp, "light")
    window = MainWindow(ClientConfig())
    window.resize(1280, 800)
    window.show()
    editor = getattr(window.manuscript_view, side)
    editor.set_raw_text("第一行\n第二行文字")
    editor.setFocus()
    cursor = editor.textCursor()
    cursor.setPosition(6)
    editor.setTextCursor(cursor)
    for _ in range(3):
        qapp.processEvents()
    assert QApplication.focusWidget() is editor
    assert editor.viewport().pos() != editor.rect().topLeft()

    def close_to_caret() -> bool:
        # Sub-pixel glyph advances round differently; the defect was 16-20 px.
        reported = QGuiApplication.inputMethod().cursorRectangle().topLeft()
        expected = _caret_in_window(window, editor).topLeft()
        return abs(reported.x() - expected.x()) <= 1 and abs(reported.y() - expected.y()) <= 1

    assert close_to_caret()
    # During composition the caret sits inside the preedit text.
    qapp.sendEvent(editor, QInputMethodEvent("zhong", []))
    qapp.processEvents()
    assert editor._ime_active
    assert close_to_caret()
    qapp.sendEvent(editor, QInputMethodEvent("", []))
    window.manuscript_view._saved_payload = window.manuscript_view._payload
    window.close()
