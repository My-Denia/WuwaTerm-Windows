"""No test hands its windows on to the next one (tests/conftest.py).

The check runs its own pytest session with the repository's conftest, because
what it looks at happens between two tests. Its inner tests follow the order
that crashed the suite: a window queued with ``deleteLater()``, then
``QTest.qWait``, then an application-wide ``setStyleSheet``. A session this
small does not reach the segfault itself, which needs the windows of many
modules to pile up, so it asserts that nothing is left to cause it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]

INNER_TESTS = '''
import pytest
import shiboken6
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from wuwaterm_client.config import ClientConfig
from wuwaterm_client.ui.main_window import MainWindow

LEFT_BEHIND = {}


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def shared_window(qapp):
    window = QWidget()
    window.show()
    return window


def test_leaves_windows_behind(qapp, shared_window):
    queued = QWidget()
    queued.show()
    queued.deleteLater()
    never_closed = QWidget()
    never_closed.show()
    # Closing this one would ask about the unsaved text in a modal box.
    unsaved = MainWindow(ClientConfig())
    unsaved.show()
    unsaved.manuscript_view.source_edit.insertPlainText("unsaved")
    view = unsaved.manuscript_view
    assert view._payload != view._saved_payload
    LEFT_BEHIND.update(queued=queued, never_closed=never_closed, unsaved=unsaved)


def test_starts_without_them(qapp, shared_window):
    assert len(LEFT_BEHIND) == 3, "runs after the test above"
    alive = [name for name, window in LEFT_BEHIND.items() if shiboken6.isValid(window)]
    assert alive == []
    assert shiboken6.isValid(shared_window) and shared_window.isVisible()
    QTest.qWait(50)
    previous = qapp.styleSheet()
    qapp.setStyleSheet("QWidget { color: black; }")
    qapp.setStyleSheet(previous)
'''


def test_the_next_test_starts_without_the_windows_of_the_last_one(tmp_path) -> None:
    shutil.copy(ROOT / "tests" / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(INNER_TESTS, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path), str(tmp_path / "test_inner.py")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        # A teardown stuck in a modal box would otherwise never return.
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
