"""Drop, clipboard, recent files and work-position restore on real widgets."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from wuwaterm_client import strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.config import ClientConfig, config_path
from wuwaterm_client.manuscript import empty_workfile, save_workfile
from wuwaterm_client.ui.main_window import MainWindow
from wuwaterm_client.ui.manuscript_view import MAX_IMPORT_BYTES, ManuscriptView


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _view(source: str = "src", target: str = "tgt") -> ManuscriptView:
    view = ManuscriptView(ApiClient(None))
    value = empty_workfile()
    value.update(source=source, target=target, direction="zh")
    view._set_payload(value)
    view._saved_payload = json.loads(json.dumps(view._payload))
    return view


def _drop(widget, *files: Path, text: str | None = None) -> None:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(f)) for f in files])
    if text is not None:
        mime.setText(text)
    target = widget.viewport() if hasattr(widget, "viewport") else widget
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, enter)
    assert enter.isAccepted()
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, event)
    assert event.isAccepted()
    QCoreApplication.processEvents()  # the deferred import


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("side", ["source", "target"])
def test_text_file_drop_replaces_that_side_as_one_undoable_edit(qapp, tmp_path, side, with_text) -> None:
    view = _view()
    other = "target" if side == "source" else "source"
    before_other = getattr(view, f"{other}_edit").raw_text()
    path = tmp_path / "in.txt"
    path.write_bytes("﻿新\r\n文".encode())
    _drop(getattr(view, f"{side}_edit"), path, text=str(path) if with_text else None)
    assert getattr(view, f"{side}_edit").raw_text() == "新\r\n文"
    assert getattr(view, f"{other}_edit").raw_text() == before_other
    assert view.document_dirty and view.document_path is None
    assert str(path) not in view.source_edit.raw_text() + view.target_edit.raw_text()
    view._undo_replace()
    assert getattr(view, f"{side}_edit").raw_text() == ("src" if side == "source" else "tgt")


def test_drop_outside_the_editors_targets_the_source(qapp, tmp_path) -> None:
    view = _view()
    path = tmp_path / "in.md"
    path.write_text("# 标题", encoding="utf-8")
    _drop(view.toolbar, path)
    assert view.source_edit.raw_text() == "# 标题" and view.target_edit.raw_text() == "tgt"


def test_a_workfile_drop_opens_through_the_same_open_function(qapp, tmp_path) -> None:
    value = empty_workfile()
    value.update(source="S", target="T", direction="zh")
    path = tmp_path / "w.json"
    save_workfile(path, value)
    view = _view()
    _drop(view.target_edit, path, text=str(path))
    assert view.document_path == path and view.source_edit.raw_text() == "S" and not view.document_dirty


def _rejected(tmp_path, name: str, data: bytes, message: str, second: bool = False) -> None:
    view = _view()
    path = tmp_path / name
    path.write_bytes(data)
    files = [path]
    if second:
        extra = tmp_path / "two.txt"
        extra.write_text("x")
        files.append(extra)
    _drop(view.source_edit, *files)
    assert view.status_label.text() == message
    assert (view.source_edit.raw_text(), view.target_edit.raw_text()) == ("src", "tgt")
    assert not view.document_dirty


def test_unusable_drops_say_why_and_change_nothing(qapp, tmp_path) -> None:
    _rejected(tmp_path, "bad.txt", b"\xff\xfe\x00bad", strings.IMPORT_NOT_UTF8)
    _rejected(tmp_path, "big.txt", b"a" * (MAX_IMPORT_BYTES + 1), strings.IMPORT_TOO_LARGE)
    _rejected(tmp_path, "a.txt", b"a", strings.IMPORT_ONE_FILE_ONLY, second=True)
    _rejected(tmp_path, "a.exe", b"a", strings.IMPORT_UNSUPPORTED_TYPE)


def test_exactly_one_mebibyte_is_accepted(qapp, tmp_path) -> None:
    view = _view()
    path = tmp_path / "edge.txt"
    path.write_bytes(b"a" * MAX_IMPORT_BYTES)
    view.import_files([str(path)], "source")
    assert len(view.source_edit.raw_text()) == MAX_IMPORT_BYTES


def test_plain_text_drag_is_left_to_qt(qapp) -> None:
    # What Qt then does with the text (LosslessEditor already rejects a
    # synthetic drop as an ambiguous edit, before this change too) is not
    # this feature's business; it must only not be taken for a file drop.
    view = _view("", "")
    view.show()
    dropped = []
    view.source_edit.filesDropped.connect(lambda *args: dropped.append(args))
    mime = QMimeData()
    mime.setText("typed")
    viewport = view.source_edit.viewport()
    enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport, enter)
    QApplication.sendEvent(viewport, QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    QCoreApplication.processEvents()
    assert enter.isAccepted() and dropped == []
    assert view.status_label.text() != strings.IMPORT_UNSUPPORTED_TYPE


def test_a_file_dropped_on_an_inspector_field_is_not_typed_into_it(qapp, tmp_path) -> None:
    view = _view()
    view.show()
    path = tmp_path / "in.txt"
    path.write_text("body")
    _drop(view.replacement_edit, path, text=str(path))
    assert view.replacement_edit.text() == "" and view.source_edit.raw_text() == "body"


def test_a_dropped_file_is_refused_during_composition(qapp, tmp_path) -> None:
    view = _view()
    view.source_edit._ime_active = True
    path = tmp_path / "in.txt"
    path.write_text("new")
    view.import_files([str(path)], "source")
    assert view.source_edit.raw_text() == "src"
    view.source_edit._ime_active = False


def test_pasting_a_copied_file_does_not_type_its_url(qapp, tmp_path) -> None:
    view = _view("", "")
    path = tmp_path / "in.txt"
    path.write_text("body")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    view.target_edit.insertFromMimeData(mime)
    QCoreApplication.processEvents()
    assert view.target_edit.raw_text() == "body"


def test_paste_as_source_and_target_are_undoable(qapp) -> None:
    view = _view()
    QApplication.clipboard().setText("clip\r\nline")
    assert view.paste_as("target")
    assert view.target_edit.raw_text() == "clip\r\nline" and view.source_edit.raw_text() == "src"
    view._undo_replace()
    assert view.target_edit.raw_text() == "tgt"
    assert view.paste_as("source") and view.source_edit.raw_text() == "clip\r\nline"
    QApplication.clipboard().clear()
    assert not view.paste_as("source")
    assert view.status_label.text() == strings.CLIPBOARD_EMPTY and view.source_edit.raw_text() == "clip\r\nline"


def test_copy_target_puts_the_raw_text_on_the_clipboard(qapp) -> None:
    view = _view("s", "a\r\nb\xa0c")
    QApplication.clipboard().clear()
    assert view.copy_target()
    assert QApplication.clipboard().text() == "a\r\nb\xa0c"


def test_new_from_clipboard_resets_then_fills_the_source(qapp, monkeypatch) -> None:
    view = _view()
    view.target_edit.textCursor().insertText("!")
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: QMessageBox.StandardButton.Discard)
    QApplication.clipboard().setText("fresh")
    assert view.new_from_clipboard()
    assert (view.source_edit.raw_text(), view.target_edit.raw_text(), view.document_path) == ("fresh", "", None)
    view._undo_replace()
    assert view.source_edit.raw_text() == ""


def test_new_from_clipboard_cancelled_keeps_the_draft(qapp, monkeypatch) -> None:
    view = _view()
    view.target_edit.textCursor().insertText("!")
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: QMessageBox.StandardButton.Cancel)
    QApplication.clipboard().setText("fresh")
    assert not view.new_from_clipboard()
    assert view.source_edit.raw_text() == "src"


def _window(tmp_path) -> MainWindow:
    window = MainWindow(ClientConfig(base_url="https://example.invalid"))
    window.resize(1200, 700)
    window.show()
    QCoreApplication.processEvents()
    return window


def _save_doc(tmp_path, name: str, source: str, target: str) -> Path:
    value = empty_workfile()
    value.update(source=source, target=target, direction="zh")
    path = tmp_path / name
    save_workfile(path, value)
    return path


def _entries(window: MainWindow):
    return ClientConfig.load().recent


def _menu_actions(window: MainWindow):
    window.recent_menu.aboutToShow.emit()
    return [a for a in window.recent_menu.actions()]


def test_recent_menu_marks_missing_files_and_removes_or_clears_them(qapp, tmp_path) -> None:
    window = _window(tmp_path)
    a = _save_doc(tmp_path, "a.json", "x", "y")
    b = _save_doc(tmp_path, "b.json", "x", "y")
    assert [x.text() for x in _menu_actions(window)] == [strings.RECENT_NONE]
    window.manuscript_view.open_path(a)
    window.manuscript_view.open_path(b)
    assert [e.path for e in _entries(window)] == [str(b), str(a)]
    a.unlink()
    actions = _menu_actions(window)
    assert actions[1].text().startswith("a.json") and actions[1].text().endswith(strings.RECENT_MISSING_SUFFIX)
    assert not actions[1].isEnabled() and actions[0].isEnabled()
    assert actions[0].toolTip() == str(b)
    remove = next(x for x in actions if x.text() == strings.RECENT_REMOVE_MISSING)
    assert remove.isEnabled()
    remove.trigger()
    assert [e.path for e in _entries(window)] == [str(b)]
    clear = next(x for x in _menu_actions(window) if x.text() == strings.RECENT_CLEAR)
    clear.trigger()
    assert _entries(window) == () and window.config.recent == ()


def test_a_recent_entry_that_vanished_after_the_menu_was_drawn_stays(qapp, tmp_path) -> None:
    window = _window(tmp_path)
    a = _save_doc(tmp_path, "a.json", "x", "y")
    window.manuscript_view.open_path(a)
    actions = _menu_actions(window)
    a.unlink()
    actions[0].trigger()
    assert window.manuscript_view.status_label.text() == strings.RECENT_FILE_MISSING.format(name="a.json")
    assert [e.path for e in _entries(window)] == [str(a)]


def test_a_menu_label_escapes_the_mnemonic_ampersand(qapp, tmp_path) -> None:
    window = _window(tmp_path)
    a = _save_doc(tmp_path, "R&D.json", "x", "y")
    window.manuscript_view.open_path(a)
    assert _menu_actions(window)[0].text().startswith("R&&D.json")


def test_reopening_from_recent_restores_cursor_scroll_and_panel_width(qapp, tmp_path) -> None:
    window = _window(tmp_path)
    view = window.manuscript_view
    long_text = "\r\n".join(f"line {n} 词" for n in range(400))
    a = _save_doc(tmp_path, "a.json", long_text, long_text)
    b = _save_doc(tmp_path, "b.json", "other", "other")
    view.open_path(a)
    view.inspector.show()  # the offscreen screen is small enough to make the view compact
    QCoreApplication.processEvents()
    offset = len("\r\n".join(f"line {n} 词" for n in range(250))) + 3
    view.source_edit.set_raw_cursor_offset(offset)
    view.source_edit.setFocus()
    view.source_edit.verticalScrollBar().setValue(120)
    view.target_edit.verticalScrollBar().setValue(60)
    view.work_splitter.setSizes([700, 290])
    QCoreApplication.processEvents()
    view.open_path(b)
    QCoreApplication.processEvents()
    (entry_a,) = [e for e in _entries(window) if e.path == str(a)]
    assert entry_a.cursor.side == "source" and entry_a.cursor.offset == offset
    assert (entry_a.scroll.source, entry_a.scroll.target) == (120, 60) and entry_a.panel_width == 290
    view.work_splitter.setSizes([800, 300])
    next(x for x in _menu_actions(window) if x.text().startswith("a.json")).trigger()
    QCoreApplication.processEvents()
    assert view.source_edit.raw_cursor_offset() == offset
    assert view.source_edit.verticalScrollBar().value() == 120
    assert view.target_edit.verticalScrollBar().value() == 60
    assert view.work_splitter.sizes()[1] == 290


def test_restore_clamps_when_the_file_got_shorter(qapp, tmp_path) -> None:
    view = _view("abc", "def")
    view.restore_work_position({"cursor": {"side": "target", "offset": 999}, "scroll": {"source": 9, "target": 9},
                                "panel_width": None, "selected_issue_id": None})
    QCoreApplication.processEvents()
    assert view.target_edit.raw_cursor_offset() == 3


def test_closing_remembers_the_position_and_an_untitled_draft_is_not_listed(qapp, tmp_path) -> None:
    window = _window(tmp_path)
    window.close()
    assert _entries(window) == ()
    window = _window(tmp_path)
    a = _save_doc(tmp_path, "a.json", "hello", "world")
    window.manuscript_view.open_path(a)
    window.manuscript_view.target_edit.set_raw_cursor_offset(4)
    window.manuscript_view.target_edit.setFocus()
    window.close()
    assert _entries(window)[0].cursor.offset == 4


def test_saving_as_a_new_path_lists_it(qapp, tmp_path, monkeypatch) -> None:
    window = _window(tmp_path)
    target = tmp_path / "new.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a: (str(target), ""))
    assert window.manuscript_view.save_document(save_as=True)
    assert [e.path for e in _entries(window)] == [str(target)]


def test_pending_issue_is_selected_only_if_the_owner_has_not_chosen(qapp) -> None:
    class Issue:
        def __init__(self, fingerprint):
            self.issue = {"fingerprint": fingerprint}

    view = _view()
    first, second = Issue("a" * 32), Issue("b" * 32)
    view._issue_rows = [first, second]
    view.semantic_list.addItems(["one", "two"])
    view.restore_work_position({"selected_issue_id": "b" * 32})
    view._select_pending_issue()
    assert view.work_position()["selected_issue_id"] == "b" * 32
    assert view.semantic_list.currentRow() == 1
    view.semantic_list.setCurrentRow(0)
    view._pending_issue_id = "b" * 32
    view._select_pending_issue()
    assert view.semantic_list.currentRow() == 0


def test_a_saved_workfile_never_contains_its_own_path(qapp, tmp_path) -> None:
    window = _window(tmp_path)
    path = tmp_path / "private-folder-name" / "draft.json"
    path.parent.mkdir()
    window.manuscript_view.source_edit.textCursor().insertText("text")
    window.manuscript_view._path = path
    assert window.manuscript_view.save_document()
    raw = path.read_text(encoding="utf-8")
    # JSON escapes Windows backslashes, so look for both spellings.
    for text in ("private-folder-name", str(tmp_path)):
        assert text not in raw and json.dumps(text)[1:-1] not in raw
    stored = json.loads(config_path().read_text(encoding="utf-8"))["recent"]
    assert [Path(entry["path"]) for entry in stored] == [path]


def test_a_damaged_settings_file_is_not_overwritten_by_the_recent_list(qapp, tmp_path) -> None:
    """The damaged file still holds the server address the owner can repair;
    opening a manuscript must not replace it with defaults."""
    damaged = b'{"base_url": "https://example.invalid", '
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_bytes(damaged)
    window = _window(tmp_path)
    window.manuscript_view.open_path(_save_doc(tmp_path, "a.json", "x", "y"))
    window.close()
    assert config_path().read_bytes() == damaged
