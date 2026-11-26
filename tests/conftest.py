"""Shared pytest setup.

Sets QT_QPA_PLATFORM=offscreen before any PySide6 import so Qt-constructing
tests run headlessly, without a real display. The API, configuration,
credential and error tests do not need Qt or its event loop.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _parentless_windows() -> set:
    """Top-level widgets that nothing else will destroy.

    Reads PySide6 only when a test has already imported it: this file also
    runs in the contract job, where desktop dependencies are absent.
    """
    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None or widgets.QApplication.instance() is None:
        return set()
    return {w for w in widgets.QApplication.topLevelWidgets() if w.parentWidget() is None}


@pytest.fixture(autouse=True)
def destroy_test_windows(isolated_user_state):
    """Destroy each window a test created, before the next test starts.

    A window left alive, or queued with ``deleteLater()`` and never flushed,
    stays in the process for every later test. A later nested event loop
    (``QTest.qWait``) then deletes the queued ones, and an application-wide
    ``setStyleSheet`` has to repolish all the others; together this has
    segfaulted the suite.

    Windows that already existed at setup belong to a wider fixture and are
    left alone. This hides and deletes rather than calls ``close()``:
    ``MainWindow.closeEvent`` asks about unsaved text in a modal box, which
    would block a headless run.
    """
    before = _parentless_windows()
    yield
    created = _parentless_windows() - before
    for window in created:
        window.hide()
        window.deleteLater()
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent

    app = QCoreApplication.instance()
    if app is None:
        return
    # Also flushes deletions the test queued itself. Processing events can
    # queue more of them, hence the second flush.
    for _ in range(2):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path, monkeypatch, request):
    # This read-only Windows-only test explicitly inspects the real path.
    if request.node.name != "test_on_this_machine_the_configuration_is_not_in_a_temporary_directory":
        monkeypatch.setenv("APPDATA", str(tmp_path))
    # Portable manuscript contract tests also run in the server environment,
    # where desktop dependencies are intentionally absent. They still run;
    # only credential-store isolation is unnecessary there.
    try:
        import keyring
        import keyring.errors
        from keyring.backend import KeyringBackend
    except ModuleNotFoundError as exc:
        if exc.name != "keyring":
            raise
        yield
        return

    class _IsolatedKeyring(KeyringBackend):
        priority = 1

        def __init__(self):
            self.values = {}

        def get_password(self, service, username):
            return self.values.get((service, username))

        def set_password(self, service, username, password):
            self.values[(service, username)] = password

        def delete_password(self, service, username):
            try:
                del self.values[(service, username)]
            except KeyError:
                raise keyring.errors.PasswordDeleteError("not found") from None

    previous = keyring.get_keyring()
    keyring.set_keyring(_IsolatedKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


@pytest.fixture
def contract_source():
    """Resolve optional external sources, and require the pinned bytes when set.

    CI sets WUWATERM_CONTRACTS_REQUIRED=1 so a missing checkout cannot silently
    remove a cross-product test. Local pure-file tests need neither checkout.
    """
    import hashlib
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pins = json.loads((root / "contracts" / "sources.json").read_text(encoding="utf-8"))

    def resolve(name: str) -> Path:
        variable = f"WUWATERM_{name.upper()}_ROOT"
        path = os.environ.get(variable)
        if not path:
            if os.environ.get("WUWATERM_CONTRACTS_REQUIRED") == "1":
                pytest.fail(f"{variable} is required for cross-product contracts")
            pytest.skip(f"set {variable} to run the cross-product contract")
        checkout = Path(path)
        for relative, expected in pins[name]["files"].items():
            candidate = checkout / relative
            assert candidate.is_file(), f"pinned source missing: {name}/{relative}"
            assert hashlib.sha256(candidate.read_bytes()).hexdigest() == expected, (
                f"{name}/{relative} differs from the pinned source {pins[name]['commit']}"
            )
        return checkout

    return resolve
