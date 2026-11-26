"""Translation memory (confirmed): the local store, as a manager dialog.

``TranslationMemoryDialog`` lists, searches, adds, edits and deletes confirmed
pairs and imports and exports ``wuwaterm-tm-v1`` files. Every change is saved
to the local SQLite file before the list shows it.

The dialog reads and writes nothing but the memory file and the files the
person picks, and it calls no service. It is deliberately not wired into a view
yet; the manuscript pass that uses the store is a separate change. Nothing here
is machine translation: only confirmed pairs are stored, a match is a
reference, and provenance (terms revision, game version, use count) is shown
next to it rather than folded into it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import strings
from .. import translation_memory as tm
from .components import Banner, ChevronComboBox, FieldError
from .error_presentation import SEVERITY_DANGER

_REFUSALS = {
    "tm_not_json": strings.TM_ERROR_NOT_JSON,
    "tm_too_large": strings.TM_ERROR_TOO_LARGE,
    "tm_too_many_entries": strings.TM_ERROR_TOO_MANY,
    "tm_duplicate_key": strings.TM_ERROR_DUPLICATE_KEY,
    "tm_forbidden_key": strings.TM_ERROR_FORBIDDEN_KEY,
    "tm_nul": strings.TM_ERROR_NUL,
    "tm_surrogate": strings.TM_ERROR_SURROGATE,
    "tm_non_finite": strings.TM_ERROR_NON_FINITE,
    "tm_invalid_content": strings.TM_ERROR_INVALID_CONTENT,
    "tm_unconfirmed_origin": strings.TM_ERROR_UNCONFIRMED_ORIGIN,
    "tm_schema_mismatch": strings.TM_ERROR_SCHEMA_MISMATCH,
    "tm_unreadable": strings.TM_ERROR_UNREADABLE,
    "tm_unavailable": strings.TM_ERROR_UNAVAILABLE,
    "tm_store_error": strings.TM_ERROR_STORE_ERROR,
}
_ORIGIN_LABELS = {"human_confirmed": strings.TM_ORIGIN_HUMAN, "imported_confirmed": strings.TM_ORIGIN_IMPORTED}
_DIRECTION_LABELS = {"en": strings.TM_DIRECTION_EN, "zh": strings.TM_DIRECTION_ZH}


def direction_label(direction: str) -> str:
    return _DIRECTION_LABELS.get(direction, direction)


def origin_label(origin: str) -> str:
    return _ORIGIN_LABELS.get(origin, origin)


def refusal_text(error: BaseException) -> str:
    """What a refused or unreadable memory file means for the person.

    The location (``entries[3].source_text``) is kept, the file's content never
    is: a ``TmError`` carries none.
    """
    if isinstance(error, tm.TmError):
        text = _REFUSALS.get(error.code, strings.TM_ERROR_INVALID_CONTENT)
        if error.detail and error.code in ("tm_invalid_content", "tm_unconfirmed_origin", "tm_duplicate_entry"):
            text += " " + strings.TM_ERROR_LOCATION.format(detail=error.detail)
        return text
    return strings.TM_FILE_UNREADABLE_IO


def _os_reason(error: OSError) -> str:
    # The reason without the path: the message is shown on screen and may be photographed.
    return error.strerror or type(error).__name__


class TranslationMemoryDialog(QDialog):
    """Manage confirmed translations: search, add, edit, delete, import and export."""

    changed = Signal()

    def __init__(self, memory: tm.TranslationMemory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._memory = memory
        self._editing: int | None = None
        self.setWindowTitle(strings.TM_DIALOG_TITLE)
        self.resize(QSize(820, 700))

        title = QLabel(strings.TM_DIALOG_TITLE, self)
        title.setObjectName("pageTitle")
        note = QLabel(f"{strings.TM_BADGE}\n{strings.TM_FILE_NOTE}", self)
        note.setObjectName("secondaryText")
        note.setWordWrap(True)
        self.banner = Banner(self)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(strings.TM_SEARCH_PLACEHOLDER)
        self.search_edit.setMaxLength(400)
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _text: self._reload_rows())
        self.direction_filter = ChevronComboBox(self)
        self.direction_filter.addItem(strings.TM_DIRECTION_ANY, None)
        self.direction_filter.addItem(strings.TM_DIRECTION_EN, "en")
        self.direction_filter.addItem(strings.TM_DIRECTION_ZH, "zh")
        self.direction_filter.currentIndexChanged.connect(lambda _index: self._reload_rows())
        filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(8)
        filters.addWidget(self._labelled(strings.TM_SEARCH_LABEL, self.search_edit), 2)
        filters.addWidget(self._labelled(strings.TM_FIELD_DIRECTION, self.direction_filter), 1)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels([
            strings.TM_TABLE_SOURCE, strings.TM_TABLE_TARGET, strings.TM_TABLE_DIRECTION,
            strings.TM_TABLE_USE, strings.TM_TABLE_NOTE,
        ])
        self.table.setAccessibleName(strings.TM_LIST_TITLE)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(160)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in (0, 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, 200)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._edit_selected())

        self.empty_label = QLabel(strings.TM_LIST_EMPTY, self)
        self.empty_label.setObjectName("secondaryText")
        self.empty_label.setWordWrap(True)
        self.details_label = QLabel(strings.TM_DETAILS_NONE, self)
        self.details_label.setObjectName("secondaryText")
        self.details_label.setWordWrap(True)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("secondaryText")

        self.edit_button = QPushButton(strings.TM_EDIT, self)
        self.edit_button.setObjectName("secondaryButton")
        self.edit_button.clicked.connect(self._edit_selected)
        self.remove_button = QPushButton(strings.TM_REMOVE, self)
        self.remove_button.setObjectName("secondaryButton")
        self.remove_button.clicked.connect(self._remove_selected)
        row_actions = QHBoxLayout()
        row_actions.setContentsMargins(0, 0, 0, 0)
        row_actions.setSpacing(8)
        row_actions.addWidget(self.count_label, 1)
        row_actions.addWidget(self.edit_button)
        row_actions.addWidget(self.remove_button)

        self.form_title = QLabel(strings.TM_ADD_TITLE, self)
        self.form_title.setObjectName("sectionTitle")
        self.source_edit = QLineEdit(self)
        self.target_edit = QLineEdit(self)
        self.terms_edit = QLineEdit(self)
        self.game_edit = QLineEdit(self)
        self.note_edit = QLineEdit(self)
        self.direction_combo = ChevronComboBox(self)
        self.direction_combo.addItem(strings.TM_DIRECTION_OPTION_EN, "en")
        self.direction_combo.addItem(strings.TM_DIRECTION_OPTION_ZH, "zh")
        self.source_edit.setMaxLength(tm.SOURCE_SCALARS[1])
        self.target_edit.setMaxLength(tm.TARGET_SCALARS[1])
        self.terms_edit.setMaxLength(tm.REVISION_SCALARS[1])
        self.game_edit.setMaxLength(tm.REVISION_SCALARS[1])
        self.note_edit.setMaxLength(tm.NOTE_SCALARS[1])
        self.terms_edit.setPlaceholderText(strings.TM_REVISION_PLACEHOLDER)
        self.game_edit.setPlaceholderText(strings.TM_REVISION_PLACEHOLDER)
        self.form_error = FieldError(self)

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(8)
        fields.addWidget(self._labelled(strings.TM_FIELD_SOURCE, self.source_edit), 1)
        fields.addWidget(self._labelled(strings.TM_FIELD_TARGET, self.target_edit), 1)
        options = QHBoxLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(8)
        options.addWidget(self._labelled(strings.TM_FIELD_DIRECTION, self.direction_combo), 2)
        options.addWidget(self._labelled(strings.TM_FIELD_TERMS_REVISION, self.terms_edit), 2)
        options.addWidget(self._labelled(strings.TM_FIELD_GAME_VERSION, self.game_edit), 2)
        options.addWidget(self._labelled(strings.TM_FIELD_NOTE, self.note_edit), 3)

        self.submit_button = QPushButton(strings.TM_ADD, self)
        self.submit_button.setObjectName("primaryButton")
        self.submit_button.clicked.connect(self._submit)
        self.cancel_edit_button = QPushButton(strings.TM_CANCEL_EDIT, self)
        self.cancel_edit_button.setObjectName("secondaryButton")
        self.cancel_edit_button.clicked.connect(self._cancel_edit)
        for field in (self.source_edit, self.target_edit, self.terms_edit, self.game_edit, self.note_edit):
            field.returnPressed.connect(self._submit)
        form_actions = QHBoxLayout()
        form_actions.setContentsMargins(0, 0, 0, 0)
        form_actions.setSpacing(8)
        form_actions.addWidget(self.cancel_edit_button)
        form_actions.addStretch(1)
        form_actions.addWidget(self.submit_button)

        self.import_button = QPushButton(strings.TM_IMPORT, self)
        self.import_button.setObjectName("secondaryButton")
        self.import_button.clicked.connect(self.import_file)
        self.export_button = QPushButton(strings.TM_EXPORT, self)
        self.export_button.setObjectName("secondaryButton")
        self.export_button.clicked.connect(self.export_file)
        self.clear_button = QPushButton(strings.TM_CLEAR, self)
        self.clear_button.setObjectName("secondaryButton")
        self.clear_button.clicked.connect(self._clear)
        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("secondaryText")
        self.notice_label.setWordWrap(True)
        self.close_button = QPushButton(strings.TM_CLOSE, self)
        self.close_button.setObjectName("primaryButton")
        self.close_button.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        footer.addWidget(self.import_button)
        footer.addWidget(self.export_button)
        footer.addWidget(self.clear_button)
        footer.addStretch(1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addWidget(self.banner)
        layout.addLayout(filters)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.details_label)
        layout.addLayout(row_actions)
        layout.addWidget(self.form_title)
        layout.addLayout(fields)
        layout.addLayout(options)
        layout.addWidget(self.form_error)
        layout.addLayout(form_actions)
        layout.addWidget(self.notice_label)
        layout.addLayout(footer)

        self._reload_rows()
        self._show_file_state()
        self._set_mode(None)
        # Return in a field submits the form (returnPressed) and nothing else.
        # Every push button in a QDialog starts out auto-default, so Return
        # would go on to press whichever one Qt had made the default.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def _labelled(self, text: str, field: QWidget) -> QWidget:
        host = QWidget(self)
        column = QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        label = QLabel(text, host)
        label.setBuddy(field)
        column.addWidget(label)
        column.addWidget(field)
        return host

    # -- state ------------------------------------------------------------------

    @property
    def memory(self) -> tm.TranslationMemory:
        return self._memory

    def _show_file_state(self) -> None:
        usable = self._memory.usable
        if usable:
            self.banner.clear()
        else:
            error = self._memory.error
            reason = _os_reason(error) if isinstance(error, OSError) else refusal_text(error)
            self.banner.show_message(
                strings.TM_FILE_UNREADABLE.format(reason=reason), SEVERITY_DANGER, closable=False
            )
        for widget in (self.search_edit, self.direction_filter, self.source_edit, self.target_edit,
                       self.terms_edit, self.game_edit, self.note_edit, self.direction_combo,
                       self.submit_button, self.cancel_edit_button, self.edit_button, self.remove_button,
                       self.import_button, self.export_button, self.clear_button):
            widget.setEnabled(usable)
        self._update_row_actions()

    def _reload_rows(self, select_id: int | None = None) -> None:
        direction = self.direction_filter.currentData()
        search = self.search_edit.text()
        entries = self._memory.entries(query=search or None, direction=direction)
        total = len(self._memory)
        self.table.blockSignals(True)
        self.table.setRowCount(len(entries))
        selected_row = -1
        for row, entry in enumerate(entries):
            cells = (entry.source_text, entry.target_text, direction_label(entry.direction),
                     str(entry.use_count), entry.note)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry.id)
                self.table.setItem(row, column, item)
            if entry.id == select_id:
                selected_row = row
        self.table.blockSignals(False)
        self.empty_label.setVisible(not entries)
        filtered = bool(search.strip()) or direction is not None
        self.count_label.setText(
            strings.TM_FILTERED_COUNT.format(n=len(entries), total=total) if filtered
            else strings.TM_ENTRY_COUNT.format(n=total)
        )
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        self._on_selection_changed()

    def _selected_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _on_selection_changed(self) -> None:
        entry_id = self._selected_id()
        entry = self._memory.entry(entry_id) if entry_id is not None else None
        if entry is None:
            self.details_label.setText(strings.TM_DETAILS_NONE)
        else:
            context = strings.TM_DETAILS_CONTEXT if entry.prev_source_hash or entry.next_source_hash else ""
            self.details_label.setText(strings.TM_DETAILS.format(
                origin=origin_label(entry.origin),
                revision=entry.terms_db_revision or strings.TM_NOT_RECORDED,
                game=entry.game_version or strings.TM_NOT_RECORDED,
                count=entry.use_count,
                updated_at=entry.updated_at,
                context=context,
            ))
        self._update_row_actions()

    def _update_row_actions(self) -> None:
        has_selection = self._selected_id() is not None and self._memory.usable
        self.edit_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)

    def _set_mode(self, entry_id: int | None) -> None:
        self._editing = entry_id
        editing = entry_id is not None
        self.form_title.setText(
            strings.TM_EDIT_TITLE.format(id=entry_id) if editing else strings.TM_ADD_TITLE)
        self.submit_button.setText(strings.TM_UPDATE if editing else strings.TM_ADD)
        self.cancel_edit_button.setVisible(editing)

    def _say(self, text: str) -> None:
        self.notice_label.setText(text)

    # -- the form -----------------------------------------------------------------

    def _read_form(self) -> dict | None:
        """The values the form describes, or None after showing why not."""
        fields = (
            (self.source_edit, strings.TM_FIELD_SOURCE, "source", tm.SOURCE_SCALARS),
            (self.target_edit, strings.TM_FIELD_TARGET, "target", tm.TARGET_SCALARS),
            (self.note_edit, strings.TM_FIELD_NOTE_SHORT, "note", tm.NOTE_SCALARS),
        )
        values: dict[str, str] = {}
        for edit, label, key, (low, high) in fields:
            text = edit.text().strip()
            if low > 0 and not text:
                self.form_error.show_error(strings.TM_FIELD_REQUIRED.format(field=label))
                edit.setFocus()
                return None
            if len(text) > high:
                self.form_error.show_error(strings.TM_FIELD_TOO_LONG.format(field=label, max=high))
                edit.setFocus()
                return None
            if "\u0000" in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                self.form_error.show_error(strings.TM_FIELD_INVALID.format(field=label))
                edit.setFocus()
                return None
            values[key] = text
        optional: dict[str, str | None] = {}
        for edit, label, key in ((self.terms_edit, strings.TM_FIELD_TERMS_REVISION, "terms_db_revision"),
                                 (self.game_edit, strings.TM_FIELD_GAME_VERSION, "game_version")):
            text = edit.text().strip()
            if len(text) > tm.REVISION_SCALARS[1]:
                self.form_error.show_error(
                    strings.TM_FIELD_TOO_LONG.format(field=label, max=tm.REVISION_SCALARS[1]))
                edit.setFocus()
                return None
            if "\u0000" in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                self.form_error.show_error(strings.TM_FIELD_INVALID.format(field=label))
                edit.setFocus()
                return None
            optional[key] = text or None
        self.form_error.clear()
        return {
            "source_text": values["source"],
            "target_text": values["target"],
            "note": values["note"],
            "direction": self.direction_combo.currentData(),
            "terms_db_revision": optional["terms_db_revision"],
            "game_version": optional["game_version"],
        }

    def _clear_form(self) -> None:
        for edit in (self.source_edit, self.target_edit, self.terms_edit, self.game_edit, self.note_edit):
            edit.clear()
        self.form_error.clear()

    def _submit(self) -> None:
        if not self._memory.usable:
            return
        values = self._read_form()
        if values is None:
            return
        editing = self._editing
        if editing is None and len(self._memory) >= tm.MAX_TM_ENTRIES:
            self.form_error.show_error(strings.TM_LIST_FULL)
            return
        try:
            if editing is None:
                entry = self._memory.add(**values)
            else:
                entry = self._memory.update(editing, **values)
        except tm.TmError as error:
            if error.code == "tm_duplicate_entry":
                self.form_error.show_error(strings.TM_NOTICE_DUPLICATE)
            elif error.code == "tm_too_many_entries":
                self.form_error.show_error(strings.TM_LIST_FULL)
            else:
                self.form_error.show_error(refusal_text(error))
            return
        except KeyError:
            self._cancel_edit()
            self._reload_rows()
            self._say(strings.TM_NOTICE_REMOVED)
            return
        except OSError as error:
            self.form_error.show_error(strings.TM_SAVE_FAILED.format(reason=_os_reason(error)))
            return
        self._clear_form()
        self._set_mode(None)
        self._reload_rows(select_id=entry.id)
        self._say(strings.TM_NOTICE_UPDATED if editing is not None else strings.TM_NOTICE_ADDED)
        self.changed.emit()

    def _edit_selected(self) -> None:
        entry_id = self._selected_id()
        entry = self._memory.entry(entry_id) if entry_id is not None else None
        if entry is None or not self._memory.usable:
            return
        self.source_edit.setText(entry.source_text)
        self.target_edit.setText(entry.target_text)
        self.terms_edit.setText(entry.terms_db_revision or "")
        self.game_edit.setText(entry.game_version or "")
        self.note_edit.setText(entry.note)
        self.direction_combo.setCurrentIndex(self.direction_combo.findData(entry.direction))
        self.form_error.clear()
        self._set_mode(entry.id)
        self.source_edit.setFocus()

    def _cancel_edit(self) -> None:
        self._clear_form()
        self._set_mode(None)

    def _remove_selected(self) -> None:
        entry_id = self._selected_id()
        entry = self._memory.entry(entry_id) if entry_id is not None else None
        if entry is None or not self._memory.usable:
            return
        if not self._ask(strings.TM_REMOVE_CONFIRM.format(source=entry.source_text),
                         strings.TM_REMOVE, strings.TM_CLEAR_KEEP):
            return
        try:
            self._memory.remove(entry.id)
        except KeyError:
            self._reload_rows()
            return
        except tm.TmError as error:
            self.banner.show_message(strings.TM_SAVE_FAILED.format(reason=refusal_text(error)), SEVERITY_DANGER)
            return
        except OSError as error:
            self.banner.show_message(strings.TM_SAVE_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return
        if self._editing == entry.id:
            self._cancel_edit()
        self._reload_rows()
        self._say(strings.TM_NOTICE_REMOVED)
        self.changed.emit()

    # -- files the person picks -----------------------------------------------------

    def _ask(self, text: str, confirm_label: str, keep_label: str) -> bool:
        """A yes/no question whose default is to change nothing."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(strings.TM_DIALOG_TITLE)
        box.setText(text)
        confirm = box.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(keep_label, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is confirm

    def import_file(self, _checked: bool = False) -> bool:
        name, _ = QFileDialog.getOpenFileName(self, strings.TM_IMPORT_TITLE, "", strings.TM_FILE_FILTER)
        if not name:
            return False
        return self.import_path(Path(name))

    def import_path(self, path: Path) -> bool:
        """Merges the file's confirmed pairs into the memory, or changes nothing."""
        try:
            data = tm.read_tm_file_bytes(path)
            count = self._memory.import_tm_json_bytes(data)
        except tm.TmError as error:
            self.banner.show_message(f"{strings.TM_IMPORT_FAILED} {refusal_text(error)}", SEVERITY_DANGER)
            return False
        except OSError:
            self.banner.show_message(
                f"{strings.TM_IMPORT_FAILED} {strings.TM_IMPORT_UNREADABLE}", SEVERITY_DANGER)
            return False
        self._cancel_edit()
        self._reload_rows()
        self._show_file_state()
        self._say(strings.TM_NOTICE_IMPORTED.format(n=count))
        self.changed.emit()
        return True

    def export_file(self, _checked: bool = False) -> bool:
        name, _ = QFileDialog.getSaveFileName(
            self, strings.TM_EXPORT_TITLE, strings.TM_EXPORT_DEFAULT_NAME, strings.TM_FILE_FILTER)
        if not name:
            return False
        return self.export_path(Path(name))

    def export_path(self, path: Path) -> bool:
        try:
            tm.export_tm_file(self._memory, path)
        except tm.TmError as error:
            self.banner.show_message(strings.TM_EXPORT_FAILED.format(reason=refusal_text(error)), SEVERITY_DANGER)
            return False
        except OSError as error:
            self.banner.show_message(strings.TM_EXPORT_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return False
        self.banner.clear()
        self._say(strings.TM_NOTICE_EXPORTED.format(n=len(self._memory)))
        return True

    def _clear(self) -> None:
        if not self._memory.usable:
            return
        count = len(self._memory)
        if not count:
            return
        if not self._ask(strings.TM_CLEAR_PROMPT.format(n=count),
                         strings.TM_CLEAR_CONFIRM, strings.TM_CLEAR_KEEP):
            return
        try:
            self._memory.clear()
        except tm.TmError as error:
            self.banner.show_message(strings.TM_SAVE_FAILED.format(reason=refusal_text(error)), SEVERITY_DANGER)
            return
        except OSError as error:
            self.banner.show_message(strings.TM_SAVE_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return
        self._cancel_edit()
        self._reload_rows()
        self._say(strings.TM_NOTICE_CLEARED)
        self.changed.emit()
