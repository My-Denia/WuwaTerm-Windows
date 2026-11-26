"""My terms (reference): the dialog, the workbench panel and what they promise.

Real widgets, offscreen. The store is a real file in a temporary folder; the
only things replaced are the system file pickers and the confirmation box,
which need a person.
"""
from __future__ import annotations

import json

import httpx
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QFrame, QLabel, QPushButton

from wuwaterm_client import strings
from wuwaterm_client import termbase as tb
from wuwaterm_client import theme
from wuwaterm_client.api import ApiClient
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.ui.main_window import MainWindow
from wuwaterm_client.ui.manuscript_view import ManuscriptView
from wuwaterm_client.ui.termbase_dialog import MyTermsPanel, TermbaseDialog, refusal_text

STAMP = "2026-11-25T00:00:00Z"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path) -> tb.TermbaseStore:
    return tb.TermbaseStore(tmp_path / "termbase.json", clock=lambda: STAMP)


def fill(dialog: TermbaseDialog, source: str, target: str, *, direction: str = "en", kind: str = "community_term",
         note: str = "") -> None:
    dialog.source_edit.setText(source)
    dialog.target_edit.setText(target)
    dialog.note_edit.setText(note)
    dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData(direction))
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(kind))


def rows(dialog: TermbaseDialog) -> list[list[str]]:
    table = dialog.table
    return [[table.item(r, c).text() for c in range(table.columnCount())] for r in range(table.rowCount())]


def answer(dialog: TermbaseDialog, *replies: bool) -> list[str]:
    asked: list[str] = []
    queue = list(replies)

    def ask(text, _confirm, _keep):
        asked.append(text)
        return queue.pop(0)

    dialog._ask = ask  # the confirmation box needs a person
    return asked


# -- the dialog -------------------------------------------------------------------


def test_the_dialog_says_a_term_is_the_persons_own_reference_and_where_it_lives(qapp, store) -> None:
    dialog = TermbaseDialog(store)
    text = dialog.reference_label.text()
    assert strings.TERMBASE_HIT_BADGE in text and strings.TERMBASE_REFERENCE_NOTE in text
    assert "不写入稿件" in text and "不上传" in text and "凭据" in text
    assert dialog.empty_label.isHidden() is False and dialog.windowTitle() == strings.TERMBASE_DIALOG_TITLE


def test_add_edit_and_delete_save_the_local_file_each_time(qapp, store) -> None:
    dialog = TermbaseDialog(store)
    changes: list[int] = []
    dialog.changed.connect(lambda: changes.append(len(store)))

    fill(dialog, "主C", "main DPS", note="role shorthand")
    dialog._submit()
    fill(dialog, "burst window", "爆发窗口", direction="zh", kind="preferred_term")
    dialog._submit()
    assert rows(dialog) == [["主C", "main DPS", strings.TERMBASE_DIRECTION_EN, strings.TERMBASE_KIND_COMMUNITY, "role shorthand"],
                            ["burst window", "爆发窗口", strings.TERMBASE_DIRECTION_ZH, strings.TERMBASE_KIND_PREFERRED, ""]]
    assert tb.TermbaseStore(store.path).entries == store.entries and len(store) == 2
    assert dialog.source_edit.text() == "" and dialog.notice_label.text() == strings.TERMBASE_NOTICE_ADDED

    # Edit the first entry: the form fills, the button changes, the id stays.
    dialog.table.selectRow(0)
    dialog._edit_selected()
    assert dialog.source_edit.text() == "主C" and dialog.submit_button.text() == strings.TERMBASE_UPDATE
    assert dialog.form_title.text() == strings.TERMBASE_EDIT_TITLE.format(id="tb-001")
    dialog.target_edit.setText("primary DPS")
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("do_not_translate"))
    dialog._submit()
    assert store.entries[0]["id"] == "tb-001" and store.entries[0]["target"] == "primary DPS"
    assert store.entries[0]["kind"] == "do_not_translate"
    assert dialog.submit_button.text() == strings.TERMBASE_ADD and dialog.notice_label.text() == strings.TERMBASE_NOTICE_UPDATED

    # Cancelling an edit leaves the entry alone.
    dialog.table.selectRow(1)
    dialog._edit_selected()
    dialog.target_edit.setText("changed")
    dialog._cancel_edit()
    assert store.entries[1]["target"] == "爆发窗口" and dialog.source_edit.text() == ""

    # Delete asks first; "keep" deletes nothing, "delete" does and saves.
    asked = answer(dialog, False, True)
    dialog.table.selectRow(1)
    dialog._remove_selected()
    assert len(store) == 2
    dialog._remove_selected()
    assert [e["source"] for e in store.entries] == ["主C"] and asked[0] == strings.TERMBASE_REMOVE_CONFIRM.format(source="burst window")
    assert tb.TermbaseStore(store.path).entries == store.entries
    assert changes == [1, 2, 2, 1]


def test_return_in_a_field_submits_the_form_and_presses_no_button(qapp, store) -> None:
    dialog = TermbaseDialog(store)
    dialog.show()
    for edit, source in ((dialog.source_edit, "主C"), (dialog.target_edit, "副C"), (dialog.note_edit, "回能")):
        fill(dialog, source, "rendering")
        edit.setFocus()
        QTest.keyClick(edit, Qt.Key.Key_Return)
        # Added, selected in the list, and the form is ready for the next one.
        assert store.entries[-1]["source"] == source
        assert dialog.submit_button.text() == strings.TERMBASE_ADD and dialog.cancel_edit_button.isHidden()
        assert dialog.source_edit.text() == "" and dialog.target_edit.text() == ""
    assert len(store) == 3

    # Updating with Return leaves edit mode.
    dialog.table.selectRow(0)
    dialog._edit_selected()
    dialog.target_edit.setText("main DPS")
    dialog.target_edit.setFocus()
    QTest.keyClick(dialog.target_edit, Qt.Key.Key_Return)
    assert store.entries[0]["target"] == "main DPS" and len(store) == 3
    assert dialog.submit_button.text() == strings.TERMBASE_ADD and dialog.source_edit.text() == ""
    dialog.close()


def test_return_on_a_form_that_cannot_be_saved_keeps_the_message_and_the_typing(qapp, store) -> None:
    dialog = TermbaseDialog(store)
    dialog.show()
    fill(dialog, "主C", "main DPS")
    dialog._submit()
    dialog.table.selectRow(0)  # a row is selected, so Edit is enabled
    assert dialog.edit_button.isEnabled()
    fill(dialog, "half typed", "")
    dialog.source_edit.setFocus()
    QTest.keyClick(dialog.source_edit, Qt.Key.Key_Return)
    assert dialog.form_error.is_showing()
    assert dialog.form_error.text() == strings.TERMBASE_FIELD_REQUIRED.format(field=strings.TERMBASE_FIELD_TARGET)
    assert dialog.source_edit.text() == "half typed" and dialog.submit_button.text() == strings.TERMBASE_ADD
    assert len(store) == 1 and store.entries[0]["source"] == "主C"
    # A message on screen survives Return too: no button, the banner's included, is pressed.
    broken = store.path.parent / "broken.json"
    broken.write_bytes(b"{")
    assert dialog.import_path(broken) is False and dialog.banner.is_showing()
    dialog.source_edit.setFocus()
    QTest.keyClick(dialog.source_edit, Qt.Key.Key_Return)
    assert dialog.banner.is_showing() and strings.TERMBASE_IMPORT_FAILED in dialog.banner.message_text
    assert dialog.source_edit.text() == "half typed"
    assert not [b for b in dialog.findChildren(QPushButton) if b.autoDefault() or b.isDefault()]
    dialog.close()


def test_a_form_that_cannot_be_saved_says_why_and_writes_nothing(qapp, store) -> None:
    dialog = TermbaseDialog(store)
    fill(dialog, "  ", "x")
    dialog._submit()
    assert dialog.form_error.is_showing() and strings.TERMBASE_FIELD_REQUIRED.format(field=strings.TERMBASE_FIELD_SOURCE) in dialog.form_error.text()
    fill(dialog, "a", "")
    dialog._submit()
    assert strings.TERMBASE_FIELD_REQUIRED.format(field=strings.TERMBASE_FIELD_TARGET) in dialog.form_error.text()
    dialog.source_edit.setMaxLength(1000)
    fill(dialog, "x" * 121, "t")
    dialog._submit()
    assert strings.TERMBASE_FIELD_TOO_LONG.format(field=strings.TERMBASE_FIELD_SOURCE, max=120) == dialog.form_error.text()
    assert not store.path.exists() and len(store) == 0
    # The note is optional, and fields are trimmed.
    fill(dialog, "  主C  ", " main DPS ")
    dialog._submit()
    assert store.entries[0]["source"] == "主C" and store.entries[0]["target"] == "main DPS" and store.entries[0]["note"] == ""


def test_a_failed_save_keeps_the_list_as_it_was(qapp, store, monkeypatch) -> None:
    import os

    dialog = TermbaseDialog(store)
    fill(dialog, "a", "b")
    dialog._submit()
    before = store.path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    fill(dialog, "c", "d")
    dialog._submit()
    assert "No space left on device" in dialog.form_error.text() and str(store.path) not in dialog.form_error.text()
    assert store.path.read_bytes() == before and rows(dialog) == [["a", "b", strings.TERMBASE_DIRECTION_EN, strings.TERMBASE_KIND_COMMUNITY, ""]]


def test_a_full_termbase_refuses_one_more_entry_in_words(qapp, store) -> None:
    store.replace(tb.validate_termbase({
        "format": tb.TERMBASE_FORMAT, "schema_version": 1, "updated_at": STAMP,
        "entries": [{"id": f"tb-{i:04d}", "source": f"s{i}", "target": "t", "direction": "en", "kind": "community_term",
                     "note": "", "updated_at": STAMP} for i in range(2000)]}))
    dialog = TermbaseDialog(store)
    fill(dialog, "one more", "t")
    dialog._submit()
    assert dialog.form_error.text() == strings.TERMBASE_LIST_FULL and len(store) == 2000


def test_import_replaces_only_after_the_person_agrees_and_refuses_a_bad_file_whole(qapp, store, tmp_path, monkeypatch) -> None:
    dialog = TermbaseDialog(store)
    fill(dialog, "keep me", "x")
    dialog._submit()
    incoming = tb.serialize_termbase(tb.validate_termbase({
        "format": tb.TERMBASE_FORMAT, "schema_version": 1, "updated_at": STAMP,
        "entries": [{"id": "tb-001", "source": "今汐", "target": "Jinhsi", "direction": "en", "kind": "do_not_translate",
                     "note": "", "updated_at": STAMP}]}))
    chosen = tmp_path / "terms.json"
    chosen.write_bytes(incoming)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(chosen), ""))
    changes: list[int] = []
    dialog.changed.connect(lambda: changes.append(1))

    asked = answer(dialog, False, True)
    dialog.import_file()
    assert [e["source"] for e in store.entries] == ["keep me"] and not changes
    assert asked == [strings.TERMBASE_REPLACE_PROMPT.format(n=1)]
    dialog.import_file()
    assert [e["source"] for e in store.entries] == ["今汐"] and changes == [1]
    assert tb.TermbaseStore(store.path).entries == store.entries
    assert dialog.notice_label.text() == strings.TERMBASE_NOTICE_IMPORTED.format(n=1) and rows(dialog)[0][0] == "今汐"

    # A refused file changes nothing, names the reason and the location, and does not ask.
    bad = tmp_path / "bad.json"
    bad.write_text(incoming.decode().replace('"direction": "en"', '"direction": "xx"'), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(bad), ""))
    asked = answer(dialog)
    before = store.path.read_bytes()
    assert dialog.import_file() is False
    assert dialog.banner.is_showing() and strings.TERMBASE_IMPORT_FAILED in dialog.banner.message_text
    assert strings.TERMBASE_ERROR_INVALID_CONTENT in dialog.banner.message_text and "entries[0].direction" in dialog.banner.message_text
    assert store.path.read_bytes() == before and asked == []
    # Cancelling the picker does nothing.
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    assert dialog.import_file() is False


def test_export_writes_the_canonical_file_to_the_chosen_path(qapp, store, tmp_path, monkeypatch) -> None:
    dialog = TermbaseDialog(store)
    fill(dialog, "主C", "main DPS")
    dialog._submit()
    target = tmp_path / "out" / "mine.json"
    target.parent.mkdir()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert dialog.export_file() is True
    assert target.read_bytes() == tb.serialize_termbase(store.termbase) == store.path.read_bytes()
    assert dialog.notice_label.text() == strings.TERMBASE_NOTICE_EXPORTED.format(n=1)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "missing" / "x.json"), ""))
    assert dialog.export_file() is False and dialog.banner.is_showing()


def test_an_unusable_local_file_blocks_edits_and_is_only_replaced_on_purpose(qapp, tmp_path) -> None:
    path = tmp_path / "termbase.json"
    path.write_bytes(b'{"format": "wuwaterm-termbase-v1", "schema_version": 1, "entries": []}')
    original = path.read_bytes()
    dialog = TermbaseDialog(tb.TermbaseStore(path, clock=lambda: STAMP))
    assert dialog.banner.is_showing() and "不会被覆盖" in dialog.banner.message_text
    assert not dialog.source_edit.isEnabled() and not dialog.submit_button.isEnabled()
    assert not dialog.reset_button.isHidden() and not dialog.export_button.isEnabled()
    dialog._submit()
    assert path.read_bytes() == original
    answer(dialog, False, True)
    dialog._reset()
    assert path.read_bytes() == original
    dialog._reset()
    assert dialog.store.usable and dialog.source_edit.isEnabled() and dialog.reset_button.isHidden()
    assert tb.TermbaseStore(path).entries == []


def test_importing_over_an_unusable_file_says_what_will_be_written(qapp, tmp_path) -> None:
    path = tmp_path / "termbase.json"
    path.write_bytes(b"{")
    dialog = TermbaseDialog(tb.TermbaseStore(path, clock=lambda: STAMP))
    chosen = tmp_path / "terms.json"
    chosen.write_bytes(tb.serialize_termbase(tb.validate_termbase({
        "format": tb.TERMBASE_FORMAT, "schema_version": 1, "updated_at": STAMP,
        "entries": [{"id": f"tb-{i:03d}", "source": f"词{i}", "target": f"w{i}", "direction": "en",
                     "kind": "community_term", "note": "", "updated_at": STAMP} for i in (1, 2)]})))
    asked = answer(dialog, False, True)
    assert dialog.import_path(chosen) is False and path.read_bytes() == b"{"
    assert dialog.import_path(chosen) is True
    assert asked == [strings.TERMBASE_REPLACE_UNUSABLE_PROMPT.format(n=2)] * 2
    assert "2 条" in asked[0] and "空术语表" not in asked[0]
    assert path.read_bytes() == chosen.read_bytes() and dialog.store.usable


def test_every_refusal_code_has_its_own_message_and_no_file_content(qapp) -> None:
    messages = {refusal_text(tb.TermbaseError(code)) for code in tb.TERMBASE_ERROR_CODES}
    assert len(messages) == len(tb.TERMBASE_ERROR_CODES)
    assert refusal_text(OSError("boom")) == strings.TERMBASE_FILE_UNREADABLE_IO


def test_every_field_of_the_dialog_is_attached_to_its_label(qapp, store) -> None:
    dialog = TermbaseDialog(store)
    buddies = {label.buddy() for label in dialog.findChildren(QLabel) if label.buddy() is not None}
    assert {dialog.source_edit, dialog.target_edit, dialog.note_edit, dialog.direction_combo, dialog.kind_combo} <= buddies


# -- the workbench ------------------------------------------------------------------


def seeded(store: tb.TermbaseStore) -> tb.TermbaseStore:
    store.add(source="主C", target="main DPS", direction="en", kind="community_term", note="Community role shorthand.")
    store.add(source="今汐", target="Jinhsi", direction="en", kind="do_not_translate")
    store.add(source="burst window", target="爆发窗口", direction="zh", kind="preferred_term")
    return store


def make_view(store: tb.TermbaseStore, requests: list | None = None) -> ManuscriptView:
    seen = requests if requests is not None else []

    async def routed(request):
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "invalid_request", "message": "not found"}, "request_id": "x"})

    client = ApiClient("https://example.invalid", token_provider=lambda: "test-token", _test_transport=httpx.MockTransport(routed))
    return ManuscriptView(client, None, termbase_store=store)


def install(view: ManuscriptView, source: str, target: str, direction: str) -> None:
    value = empty_workfile()
    value.update(source=source, target=target, direction=direction)
    view._set_payload(value)
    view._refresh_my_terms()


def test_the_workbench_lists_the_terms_found_in_the_source_text_for_the_current_direction(qapp, store) -> None:
    view = make_view(seeded(store))
    panel = view.my_terms
    assert panel.title_label.text() == strings.TERMBASE_TITLE
    install(view, "今汐是主C，今汐强。", "", "en")
    assert [m.entry["id"] for m in panel.matches.matches] == ["tb-002", "tb-001"]
    shown = panel.hit_texts()
    assert shown[0].startswith("今汐 → Jinhsi") and strings.TERMBASE_KIND_DO_NOT_TRANSLATE in shown[0]
    assert strings.TERMBASE_HIT_OCCURRENCES.format(n=2) in shown[0]
    assert "主C → main DPS" in shown[1] and "Community role shorthand." in shown[1]
    assert panel.reference_label.text() == strings.TERMBASE_HIT_BADGE
    assert strings.TERMBASE_ENTRY_COUNT.format(n=3) in panel.count_label.text()
    assert strings.TERMBASE_HIT_COUNT.format(n=2) in panel.count_label.text()
    assert panel.basis_label.text() == strings.TERMBASE_MATCH_BASIS.format(direction=strings.TERMBASE_DIRECTION_EN)

    # The other direction reads only its own entries.
    view.direction_combo.setCurrentIndex(view.direction_combo.findData("zh"))
    QTest.qWait(450)
    assert panel.hit_texts() == [] and panel.notice_label.text() == strings.TERMBASE_HITS_NONE
    assert panel.basis_label.text() == strings.TERMBASE_MATCH_BASIS.format(direction=strings.TERMBASE_DIRECTION_ZH)
    view.source_edit.set_raw_text("Open the Burst Window now")
    view._on_edit()
    QTest.qWait(450)
    assert panel.hit_texts() and "burst window → 爆发窗口" in panel.hit_texts()[0]


def test_typing_in_the_source_updates_the_hits_after_a_pause(qapp, store) -> None:
    view = make_view(seeded(store))
    install(view, "", "", "zh")
    assert panel_text(view) == strings.TERMBASE_HITS_NONE
    view.source_edit.setFocus()
    QTest.keyClicks(view.source_edit, "Open the burst window")
    assert view.my_terms.matches.matches == ()  # not on every keystroke
    QTest.qWait(450)
    assert [m.entry["id"] for m in view.my_terms.matches.matches] == ["tb-003"]


def panel_text(view: ManuscriptView) -> str:
    return view.my_terms.notice_label.text()


def test_an_empty_or_unreadable_termbase_is_said_in_the_panel(qapp, tmp_path) -> None:
    empty = make_view(tb.TermbaseStore(tmp_path / "a.json"))
    assert panel_text(empty) == strings.TERMBASE_HITS_EMPTY_BASE
    bad = tmp_path / "b.json"
    bad.write_bytes(b"{")
    broken = make_view(tb.TermbaseStore(bad))
    assert "不会被覆盖" in panel_text(broken) and bad.read_bytes() == b"{"


def test_changes_in_the_dialog_reach_the_panel_at_once(qapp, store, monkeypatch) -> None:
    view = make_view(store)
    install(view, "今汐在这里", "", "en")
    assert view.my_terms.hit_texts() == []

    def open_dialog(self):
        fill(self, "今汐", "Jinhsi")
        self._submit()
        return 0

    monkeypatch.setattr(TermbaseDialog, "exec", open_dialog)
    view.my_terms.manage_button.click()
    assert [m.entry["source"] for m in view.my_terms.matches.matches] == ["今汐"]
    assert "今汐 → Jinhsi" in view.my_terms.hit_texts()[0]


def test_the_panel_collapses_to_its_title_line_and_never_widens_the_inspector(qapp, store) -> None:
    from PySide6.QtWidgets import QScrollArea

    view = make_view(seeded(store))
    view.resize(1100, 700)
    view.show()
    install(view, "今汐是主C，守岸人负责回能。", "", "en")
    QTest.qWait(100)
    panel = view.my_terms
    assert panel.is_expanded() and panel.isVisible() and len(panel.hit_texts()) == 2
    assert view.inspector.findChild(QScrollArea).horizontalScrollBar().maximum() == 0
    panel.toggle_button.click()
    assert not panel.is_expanded() and not panel.reference_label.isVisible() and not panel._hits_host.isVisible()
    assert panel.count_label.isVisible() and strings.TERMBASE_HIT_COUNT.format(n=2) in panel.count_label.text()
    panel.toggle_button.click()
    assert panel.is_expanded() and panel._hits_host.isVisible()
    # Only a handful of hits are drawn; the rest are counted.
    many = tb.TermbaseStore(store.path.parent / "many.json")
    for i in range(8):
        many.add(source=f"词{i}", target=f"w{i}", direction="en", kind="community_term")
    crowded = MyTermsPanel(many)
    crowded.set_context("".join(f"词{i}" for i in range(8)), "en")
    assert len(crowded.hit_texts()) == 5 and strings.TERMBASE_HITS_MORE.format(n=3) in crowded.notice_label.text()
    view.close()


def test_the_dialog_fits_a_small_workspace(qapp, store) -> None:
    assert TermbaseDialog(store).minimumSizeHint().height() <= 640


def test_the_edit_menu_opens_my_terms(qapp, monkeypatch, tmp_path) -> None:
    opened: list[int] = []
    monkeypatch.setattr(ManuscriptView, "show_termbase_dialog", lambda self: opened.append(1))
    window = MainWindow(config=ClientConfig())
    try:
        edit_menu = next(a.menu() for a in window.menuBar().actions() if a.menu() and a.text() == strings.MENU_EDIT)
        action = next(a for a in edit_menu.actions() if a.text() == strings.TERMBASE_MENU)
        action.trigger()
        assert opened == [1]
    finally:
        window.close()


# -- what a termbase never does, on real widgets --------------------------------------------


def test_terms_never_reach_a_request_or_a_saved_manuscript(qapp, store, tmp_path) -> None:
    requests: list = []
    view = make_view(seeded(store), requests)
    install(view, "今汐是主C", "Jinhsi is the main DPS", "en")
    dialog = TermbaseDialog(store)
    fill(dialog, "SECRET-SURFACE", "SECRET-RENDERING", note="SECRET-NOTE")
    dialog._submit()
    view._refresh_my_terms()
    assert requests == []  # not even a capability probe left because of a term
    saved = tmp_path / "doc.wuwaterm"
    from wuwaterm_client.manuscript import save_workfile

    save_workfile(saved, view._payload)
    text = saved.read_text(encoding="utf-8")
    assert "SECRET" not in text and "termbase" not in text.lower()
    assert "Jinhsi" in text and "main DPS" in text  # the manuscript's own target text is all that is in the file
    assert "Community role shorthand." not in text
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".wuwaterm"] == ["doc.wuwaterm"]
    assert not hasattr(view._controller, "termbase") and "termbase" not in json.dumps(view._payload).lower()


# -- light and dark -----------------------------------------------------------------------


def _luminance(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_hits_and_dialog_are_legible_and_unclipped_in_both_themes(qapp, store, scheme) -> None:
    tokens = theme.TOKENS_BY_SCHEME[scheme]
    assert _contrast(tokens["text-primary"], tokens["bg-sunken"]) >= 7
    assert _contrast(tokens["text-secondary"], tokens["bg-sunken"]) >= 4.5
    assert "@" not in theme.load_stylesheet(scheme) and "#termbaseHit" in theme.load_stylesheet(scheme)
    qapp.setStyleSheet(theme.load_stylesheet(scheme))
    try:
        panel = MyTermsPanel(seeded(store))
        panel.set_context("今汐是主C，今汐强。", "en")
        panel.setFixedWidth(280)  # the narrowest inspector
        panel.show()
        QTest.qWait(50)
        assert panel.isVisible() and panel.hit_texts()
        for label in panel.findChildren(QLabel):
            if label.isVisible() and label.text():
                assert label.width() <= panel.width(), label.text()
                assert label.sizeHint().height() <= label.height() or label.wordWrap(), label.text()
        for card in panel._hits_host.findChildren(QFrame, "termbaseHit"):
            assert card.geometry().right() <= panel.width() and card.height() >= card.minimumSizeHint().height()
        dialog = TermbaseDialog(seeded(store))
        dialog.resize(640, 560)
        dialog.show()
        QTest.qWait(50)
        for widget in (dialog.source_edit, dialog.target_edit, dialog.note_edit, dialog.direction_combo, dialog.kind_combo,
                       dialog.submit_button, dialog.import_button, dialog.export_button, dialog.close_button, dialog.table):
            assert widget.isVisible() and widget.geometry().right() <= dialog.width() and widget.geometry().bottom() <= dialog.height()
            assert widget.width() >= widget.minimumSizeHint().width(), widget.objectName() or type(widget).__name__
        dialog.close()
        panel.close()
    finally:
        qapp.setStyleSheet("")


def test_promote_to_personal_termbase_prefills_from_term_finding_semantic_and_editor(qapp, store, monkeypatch) -> None:
    from PySide6.QtGui import QTextCursor
    from wuwaterm_client import semantic_session as sessions
    from wuwaterm_client.ui.manuscript_view import TRACK_SEMANTIC, TRACK_TERMS

    view = make_view(store)
    source = "椿在城楼。椿停下。"
    target = "Camelia stood on the tower. Camelia stopped."
    install(view, source, target, "en")

    captured: list[dict[str, str]] = []

    def fake_exec(self):
        captured.append({
            "source": self.source_edit.text(),
            "target": self.target_edit.text(),
            "direction": self.direction_combo.currentData(),
            "kind": self.kind_combo.currentData(),
        })
        self._submit()
        return 1

    monkeypatch.setattr(TermbaseDialog, "exec", fake_exec)

    # 1) Term finding promote (uses replacement_edit when edited, else finding target)
    report = {
        "request_id": "r-promote",
        "source_revision": "a" * 64,
        "target_revision": "b" * 64,
        "rule_version": "review-v2",
        "matcher_revision": "c" * 64,
        "dictionary": {"schema_version": "1", "source_commit": "d" * 40, "term_count": 1, "revision": "e" * 64},
        "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": ["review.term_pair"]},
        "findings": [{
            "id": "0:1:椿",
            "rule_id": "review.term_pair",
            "verdict": "confirmed_conflict",
            "source_span": {"start": 0, "end": 1, "text": "椿"},
            "target_span": {"start": 0, "end": 7, "text": "Camelia"},
            "candidates": [{"candidate_id": "f" * 64, "zh": "椿", "en": "Camellya", "category": "resonator", "sources": []}],
            "candidates_truncated": False,
        }],
        "truncated": False,
    }
    view._show_term_report(report)
    view._show_track(TRACK_TERMS)
    view.findings.setCurrentRow(0)
    view.replacement_edit.setText("Camellya")
    assert view.promote_term_button.isVisibleTo(view) and view.promote_term_button.isEnabled()
    view.promote_term_button.click()
    assert captured[-1] == {"source": "椿", "target": "Camellya", "direction": "en", "kind": "preferred_term"}
    assert store.entries[-1]["source"] == "椿" and store.entries[-1]["target"] == "Camellya"
    assert store.entries[-1]["kind"] == "preferred_term"
    # Promoting to personal termbase never auto-replaces homographs in the manuscript.
    assert view._payload["target"] == target

    # 2) Semantic review detail card promote
    view._controller.capabilities = frozenset({"semantic-review-v1"})
    result = sessions.SegmentResult(
        sent_source=source,
        sent_target=target,
        source_core=(0, len(source)),
        target_core=(0, len(target)),
        hard_cut=False,
        index=0,
        report={"status": "complete", "review_version": "semantic-review-v1"},
        issues=[
            sessions.LiveIssue(
                issue={
                    "id": "sem-1",
                    "fingerprint": "1" * 32,
                    "category": "term_sense",
                    "confidence": "high",
                    "source_span": {"start": 5, "end": 6, "text": "椿"},
                    "target_span": {"start": 28, "end": 35, "text": "Camelia"},
                    "source_excerpt": "椿",
                    "target_excerpt": "Camelia",
                    "rationale": "r",
                    "suggestion": {
                        "target_span": {"start": 28, "end": 35, "text": "Camelia"},
                        "replacement": "Camellya",
                        "term_effect": "none",
                    },
                    "suggestion_withheld": None,
                    "alternatives": [],
                    "segment_index": 0,
                },
                segment_index=0,
                source_revision=sessions.revision(source),
                target_revision=sessions.revision(target),
                term_basis={"dictionary_revision": "e" * 64, "matcher_revision": "c" * 64},
                source_span={"start": 5, "end": 6, "text": "椿"},
                target_span={"start": 28, "end": 35, "text": "Camelia"},
                suggestion_span={"start": 28, "end": 35, "text": "Camelia"},
            )
        ],
    )
    view._session.add(result)
    view._refresh_semantic()
    view._show_track(TRACK_SEMANTIC)
    view.semantic_list.setCurrentRow(0)
    assert view.semantic_promote_button.isVisibleTo(view) and view.semantic_promote_button.isEnabled()
    view.semantic_promote_button.click()
    assert captured[-1] == {"source": "椿", "target": "Camellya", "direction": "en", "kind": "preferred_term"}

    # 3) Editor selection promote button
    sc = view.source_edit.textCursor()
    sc.setPosition(2)
    sc.setPosition(4, QTextCursor.MoveMode.KeepAnchor)
    view.source_edit.setTextCursor(sc)
    tc = view.target_edit.textCursor()
    tc.setPosition(17)
    tc.setPosition(26, QTextCursor.MoveMode.KeepAnchor)
    view.target_edit.setTextCursor(tc)
    view.promote_editor_button.click()
    assert captured[-1] == {"source": "城楼", "target": "the tower", "direction": "en", "kind": "preferred_term"}
    view.close()
