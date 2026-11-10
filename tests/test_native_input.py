"""Where the operating system is told to draw IME candidates.

Offscreen checks prove the geometry the client reports. They do not prove where
a real Windows input method draws its candidate window; that stays on the
owner's device checklist.

The check runs in its own process: a shown window with a focused editor and an
input method query left the rest of this suite's process far slower.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = textwrap.dedent(
    """
    import keyring
    from keyring.backend import KeyringBackend
    from PySide6.QtGui import QGuiApplication, QInputMethodEvent
    from PySide6.QtWidgets import QApplication


    class _Empty(KeyringBackend):
        priority = 1

        def get_password(self, *_):
            return None

        def set_password(self, *_):
            pass

        def delete_password(self, *_):
            pass


    keyring.set_keyring(_Empty())
    from wuwaterm_client import theme
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.ui.main_window import MainWindow

    app = QApplication([])
    app.setStyle("Fusion")
    theme.apply_theme(app, "light")
    window = MainWindow(ClientConfig())
    window.resize(1280, 800)
    window.show()


    def offset(editor):
        caret = editor.cursorRect()
        expected = editor.viewport().mapTo(window, caret.topLeft())
        reported = QGuiApplication.inputMethod().cursorRectangle().topLeft()
        return round(reported.x() - expected.x()), round(reported.y() - expected.y())


    for name in ("source_edit", "target_edit"):
        editor = getattr(window.manuscript_view, name)
        editor.set_raw_text("第一行\\n第二行文字")
        editor.setFocus()
        cursor = editor.textCursor()
        cursor.setPosition(6)
        editor.setTextCursor(cursor)
        for _ in range(3):
            app.processEvents()
        assert QApplication.focusWidget() is editor
        assert editor.viewport().pos() != editor.rect().topLeft()
        idle = offset(editor)
        # During composition the caret sits inside the preedit text.
        app.sendEvent(editor, QInputMethodEvent("zhong", []))
        app.processEvents()
        assert editor._ime_active
        composing = offset(editor)
        app.sendEvent(editor, QInputMethodEvent("", []))
        print("OFFSET", name, *idle, *composing)
    """
)


def test_ime_cursor_rectangle_matches_the_visible_caret(tmp_path) -> None:
    """The candidate window is placed from this rectangle. It used to omit the
    editor frame and stylesheet padding (20, 16 px in the workbench), so the
    candidate window covered the line being composed."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT / "src"), APPDATA=str(tmp_path))
    completed = subprocess.run([sys.executable, "-c", _PROBE], cwd=tmp_path, env=env, check=False,
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    lines = [line.split()[1:] for line in completed.stdout.splitlines() if line.startswith("OFFSET ")]
    assert [line[0] for line in lines] == ["source_edit", "target_edit"]
    for _name, *offsets in lines:
        # Sub-pixel glyph advances round differently; the defect was 16-20 px.
        assert all(abs(int(value)) <= 1 for value in offsets), offsets
