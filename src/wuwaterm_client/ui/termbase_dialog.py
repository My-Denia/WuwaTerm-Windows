"""My terms (reference): the local user termbase, in the workbench and as a dialog.

Two widgets, one store (``termbase.TermbaseStore``):

* ``MyTermsPanel`` sits in the manuscript inspector. It lists which of the
  person's own terms occur in the open source text, for the current
  direction, and opens the dialog.
* ``TermbaseDialog`` adds, edits and deletes entries and imports and exports
  ``wuwaterm-termbase-v1`` files. Every change is saved to the local file
  atomically before the list shows it.

Neither widget reads or writes anything but the termbase file and the files
the person picks, and neither calls the service. A match is a reading aid:
it is labelled as the person's own reference, is not the official dictionary,
and takes no part in machine translation, term locking or review.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import strings
from .. import termbase as tb
from .components import Banner, ChevronComboBox, FieldError
from .icons import icon
from .error_presentation import SEVERITY_DANGER

# Hits listed in the inspector; the rest are counted, not drawn.
MAX_LISTED_HITS = 5

_REFUSALS = {
    "termbase_not_json": strings.TERMBASE_ERROR_NOT_JSON,
    "termbase_too_large": strings.TERMBASE_ERROR_TOO_LARGE,
    "termbase_too_many_entries": strings.TERMBASE_ERROR_TOO_MANY,
    "termbase_duplicate_key": strings.TERMBASE_ERROR_DUPLICATE_KEY,
    "termbase_forbidden_key": strings.TERMBASE_ERROR_FORBIDDEN_KEY,
    "termbase_nul": strings.TERMBASE_ERROR_NUL,
    "termbase_surrogate": strings.TERMBASE_ERROR_SURROGATE,
    "termbase_non_finite": strings.TERMBASE_ERROR_NON_FINITE,
    "termbase_invalid_content": strings.TERMBASE_ERROR_INVALID_CONTENT,
}
_KIND_LABELS = {
    "community_term": strings.TERMBASE_KIND_COMMUNITY,
    "preferred_term": strings.TERMBASE_KIND_PREFERRED,
    "do_not_translate": strings.TERMBASE_KIND_DO_NOT_TRANSLATE,
}
_DIRECTION_LABELS = {"en": strings.TERMBASE_DIRECTION_EN, "zh": strings.TERMBASE_DIRECTION_ZH}


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


def direction_label(direction: str) -> str:
    return _DIRECTION_LABELS.get(direction, direction)


def refusal_text(error: BaseException) -> str:
    """What a refused or unreadable termbase file means for the person.

    The location (``entries[3].source``) is kept, the file's content never is:
    a ``TermbaseError`` carries none.
    """
    if isinstance(error, tb.TermbaseError):
        text = _REFUSALS.get(error.code, strings.TERMBASE_ERROR_INVALID_CONTENT)
        if error.detail and error.code == "termbase_invalid_content":
            text += " " + strings.TERMBASE_ERROR_LOCATION.format(detail=error.detail)
        return text
    return strings.TERMBASE_FILE_UNREADABLE_IO


def _os_reason(error: OSError) -> str:
    # The reason without the path: the message is shown on screen and may be photographed.
    return error.strerror or type(error).__name__


class MyTermsPanel(QFrame):
    """The inspector section: the person's own terms found in the source text."""

    manage_requested = Signal()

    def __init__(self, store: tb.TermbaseStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("termbasePanel")
        self._store = store
        self._text = ""
        self._direction = "en"
        self._matches = tb.TermbaseMatches((), False)

        self.title_label = QLabel(strings.TERMBASE_TITLE, self)
        self.title_label.setObjectName("sectionTitle")
        self.count_label = QLabel(self)
        self.count_label.setObjectName("secondaryText")
        self.count_label.setWordWrap(True)
        self.manage_button = QPushButton(strings.TERMBASE_MANAGE, self)
        self.manage_button.setObjectName("commandButton")
        self.manage_button.setToolTip(strings.TERMBASE_DIALOG_TITLE)
        self.manage_button.clicked.connect(self.manage_requested.emit)
        self.toggle_button = QPushButton(self)
        self.toggle_button.setObjectName("commandButton")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setFixedWidth(34)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.toggle_button.setAccessibleName(strings.TERMBASE_TITLE)
        self.toggle_button.setIconSize(QSize(16, 16))
        self.toggle_button.toggled.connect(self.set_expanded)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self.toggle_button)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.manage_button)

        self.reference_label = QLabel(strings.TERMBASE_HIT_BADGE, self)
        self.reference_label.setObjectName("secondaryText")
        self.reference_label.setWordWrap(True)
        self.basis_label = QLabel(self)
        self.basis_label.setObjectName("secondaryText")
        self.basis_label.setWordWrap(True)
        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("secondaryText")
        self.notice_label.setWordWrap(True)
        # A long word wraps inside the inspector; it never widens it.
        for label in (self.count_label, self.reference_label, self.basis_label, self.notice_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._hits_host = QWidget(self)
        self._hits_host.setObjectName("inspectorBody")
        self._hits_layout = QVBoxLayout(self._hits_host)
        self._hits_layout.setContentsMargins(0, 0, 0, 0)
        self._hits_layout.setSpacing(6)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.count_label)
        self._body = QWidget(self)
        self._body.setObjectName("inspectorBody")
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        body.addWidget(self.reference_label)
        body.addWidget(self.basis_label)
        body.addWidget(self._hits_host)
        body.addWidget(self.notice_label)
        layout.addWidget(self._body)
        self.set_expanded(True)
        self.refresh()

    def set_expanded(self, expanded: bool) -> None:
        """The hits and their notes shown, or only the title line and the counts."""
        self._body.setVisible(expanded)
        self.toggle_button.setIcon(icon("chevron-down" if expanded else "chevron-right"))
        self.toggle_button.setToolTip(strings.TERMBASE_COLLAPSE if expanded else strings.TERMBASE_EXPAND)
        if self.toggle_button.isChecked() != expanded:
            self.toggle_button.setChecked(expanded)

    def is_expanded(self) -> bool:
        return self.toggle_button.isChecked()

    # -- what the panel shows ----------------------------------------------------

    @property
    def matches(self) -> tb.TermbaseMatches:
        return self._matches

    def hit_texts(self) -> list[str]:
        """The visible text of every listed hit, in order (for tests and accessibility)."""
        out = []
        for index in range(self._hits_layout.count()):
            widget = self._hits_layout.itemAt(index).widget()
            if widget is not None and widget.objectName() == "termbaseHit":
                out.append("\n".join(label.text() for label in widget.findChildren(QLabel)))
        return out

    def set_context(self, text: str, direction: str) -> None:
        """The source text and translation direction the hits are read from."""
        self._text = text
        self._direction = direction if direction in tb.TERMBASE_DIRECTION_VALUES else "en"
        self.refresh()

    def refresh(self) -> None:
        entries = self._store.entries
        self._matches = tb.match_termbase(entries, self._text, self._direction)
        self.count_label.setText(
            strings.TERMBASE_ENTRY_COUNT.format(n=len(entries))
            + (" · " + strings.TERMBASE_HIT_COUNT.format(n=len(self._matches.matches)) if self._matches.matches else "")
        )
        self.basis_label.setText(strings.TERMBASE_MATCH_BASIS.format(direction=direction_label(self._direction)))
        self._clear_hits()
        if not self._store.usable:
            self.notice_label.setText(strings.TERMBASE_FILE_UNREADABLE.format(reason=refusal_text(self._store.error)))
            return
        if not entries:
            self.notice_label.setText(strings.TERMBASE_HITS_EMPTY_BASE)
            return
        if not self._matches.matches:
            self.notice_label.setText(strings.TERMBASE_HITS_NONE)
        else:
            self.notice_label.setText("")
            for match in self._matches.matches[:MAX_LISTED_HITS]:
                self._hits_layout.addWidget(self._hit_card(match))
            rest = len(self._matches.matches) - MAX_LISTED_HITS
            if rest > 0:
                self.notice_label.setText(strings.TERMBASE_HITS_MORE.format(n=rest))
        if self._matches.truncated:
            more = self.notice_label.text()
            truncated = strings.TERMBASE_HITS_TRUNCATED.format(n=tb.MATCH_TEXT_LIMIT)
            self.notice_label.setText(f"{more}\n{truncated}" if more else truncated)

    def _clear_hits(self) -> None:
        while self._hits_layout.count():
            item = self._hits_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _hit_card(self, match: tb.TermbaseMatch) -> QFrame:
        entry = match.entry
        card = QFrame(self._hits_host)
        card.setObjectName("termbaseHit")
        card.setToolTip(strings.TERMBASE_HIT_BADGE)
        lines = QVBoxLayout(card)
        lines.setContentsMargins(10, 6, 10, 6)
        lines.setSpacing(2)
        main = QLabel(f"{entry['source']} → {entry['target']}", card)
        main.setTextFormat(Qt.TextFormat.PlainText)
        main.setWordWrap(True)
        detail = QLabel(
            f"{kind_label(entry['kind'])} · " + strings.TERMBASE_HIT_OCCURRENCES.format(n=match.count), card
        )
        detail.setObjectName("secondaryText")
        detail.setWordWrap(True)
        lines.addWidget(main)
        lines.addWidget(detail)
        if entry["note"]:
            note = QLabel(entry["note"], card)
            note.setObjectName("secondaryText")
            note.setTextFormat(Qt.TextFormat.PlainText)
            note.setWordWrap(True)
            lines.addWidget(note)
        for label in card.findChildren(QLabel):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return card


class TermbaseDialog(QDialog):
    """Manage the person's own terms: add, edit, delete, import and export."""

    changed = Signal()

    def __init__(self, store: tb.TermbaseStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._editing: str | None = None
        self.setWindowTitle(strings.TERMBASE_DIALOG_TITLE)
        self.resize(QSize(720, 640))

        title = QLabel(strings.TERMBASE_DIALOG_TITLE, self)
        title.setObjectName("pageTitle")
        self.reference_label = QLabel(
            f"{strings.TERMBASE_HIT_BADGE}\n{strings.TERMBASE_REFERENCE_NOTE}\n{strings.TERMBASE_FILE_NOTE}", self
        )
        self.reference_label.setObjectName("secondaryText")
        self.reference_label.setWordWrap(True)
        self.banner = Banner(self)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels([
            strings.TERMBASE_FIELD_SOURCE, strings.TERMBASE_FIELD_TARGET, strings.TERMBASE_FIELD_DIRECTION,
            strings.TERMBASE_FIELD_KIND, strings.TERMBASE_FIELD_NOTE_SHORT,
        ])
        self.table.setAccessibleName(strings.TERMBASE_LIST_TITLE)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(150)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in (0, 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, 150)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_row_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        self.empty_label = QLabel(strings.TERMBASE_LIST_EMPTY, self)
        self.empty_label.setObjectName("secondaryText")
        self.empty_label.setWordWrap(True)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("secondaryText")

        self.edit_button = QPushButton(strings.TERMBASE_EDIT, self)
        self.edit_button.setObjectName("secondaryButton")
        self.edit_button.clicked.connect(self._edit_selected)
        self.remove_button = QPushButton(strings.TERMBASE_REMOVE, self)
        self.remove_button.setObjectName("secondaryButton")
        self.remove_button.clicked.connect(self._remove_selected)
        row_actions = QHBoxLayout()
        row_actions.setContentsMargins(0, 0, 0, 0)
        row_actions.setSpacing(8)
        row_actions.addWidget(self.count_label, 1)
        row_actions.addWidget(self.edit_button)
        row_actions.addWidget(self.remove_button)

        self.form_title = QLabel(strings.TERMBASE_ADD_TITLE, self)
        self.form_title.setObjectName("sectionTitle")
        self.source_edit = QLineEdit(self)
        self.target_edit = QLineEdit(self)
        self.note_edit = QLineEdit(self)
        self.direction_combo = ChevronComboBox(self)
        self.direction_combo.addItem(strings.TERMBASE_DIRECTION_OPTION_EN, "en")
        self.direction_combo.addItem(strings.TERMBASE_DIRECTION_OPTION_ZH, "zh")
        self.kind_combo = ChevronComboBox(self)
        for kind in tb.TERMBASE_KIND_VALUES:
            self.kind_combo.addItem(kind_label(kind), kind)
        self.source_edit.setMaxLength(tb.SOURCE_SCALARS[1])
        self.target_edit.setMaxLength(tb.TARGET_SCALARS[1])
        self.note_edit.setMaxLength(tb.NOTE_SCALARS[1])
        self.form_error = FieldError(self)

        def labelled(text: str, field: QWidget) -> QWidget:
            host = QWidget(self)
            column = QVBoxLayout(host)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            label = QLabel(text, host)
            label.setBuddy(field)
            column.addWidget(label)
            column.addWidget(field)
            return host

        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(8)
        fields.addWidget(labelled(strings.TERMBASE_FIELD_SOURCE, self.source_edit), 1)
        fields.addWidget(labelled(strings.TERMBASE_FIELD_TARGET, self.target_edit), 1)
        options = QHBoxLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(8)
        options.addWidget(labelled(strings.TERMBASE_FIELD_DIRECTION, self.direction_combo), 3)
        options.addWidget(labelled(strings.TERMBASE_FIELD_KIND, self.kind_combo), 2)
        options.addWidget(labelled(strings.TERMBASE_FIELD_NOTE, self.note_edit), 3)

        self.submit_button = QPushButton(strings.TERMBASE_ADD, self)
        self.submit_button.setObjectName("primaryButton")
        self.submit_button.clicked.connect(self._submit)
        self.cancel_edit_button = QPushButton(strings.TERMBASE_CANCEL_EDIT, self)
        self.cancel_edit_button.setObjectName("secondaryButton")
        self.cancel_edit_button.clicked.connect(self._cancel_edit)
        for field in (self.source_edit, self.target_edit, self.note_edit):
            field.returnPressed.connect(self._submit)
        form_actions = QHBoxLayout()
        form_actions.setContentsMargins(0, 0, 0, 0)
        form_actions.setSpacing(8)
        form_actions.addWidget(self.cancel_edit_button)
        form_actions.addStretch(1)
        form_actions.addWidget(self.submit_button)

        self.import_button = QPushButton(strings.TERMBASE_IMPORT, self)
        self.import_button.setObjectName("secondaryButton")
        self.import_button.setAccessibleName(strings.TERMBASE_IMPORT)
        self.import_button.clicked.connect(self.import_file)
        self.export_button = QPushButton(strings.TERMBASE_EXPORT, self)
        self.export_button.setObjectName("secondaryButton")
        self.export_button.clicked.connect(self.export_file)
        self.reset_button = QPushButton(strings.TERMBASE_RESET, self)
        self.reset_button.setObjectName("secondaryButton")
        self.reset_button.clicked.connect(self._reset)
        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("secondaryText")
        self.notice_label.setWordWrap(True)
        self.close_button = QPushButton(strings.TERMBASE_CLOSE, self)
        self.close_button.setObjectName("primaryButton")
        self.close_button.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        footer.addWidget(self.import_button)
        footer.addWidget(self.export_button)
        footer.addWidget(self.reset_button)
        footer.addStretch(1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self.reference_label)
        layout.addWidget(self.banner)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty_label)
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

    # -- state ------------------------------------------------------------------

    @property
    def store(self) -> tb.TermbaseStore:
        return self._store

    def _show_file_state(self) -> None:
        usable = self._store.usable
        if usable:
            self.banner.clear()
        else:
            reason = refusal_text(self._store.error)
            self.banner.show_message(
                strings.TERMBASE_FILE_UNREADABLE.format(reason=reason), SEVERITY_DANGER, closable=False
            )
        for widget in (self.source_edit, self.target_edit, self.note_edit, self.direction_combo,
                       self.kind_combo, self.submit_button, self.cancel_edit_button):
            widget.setEnabled(usable)
        self.reset_button.setVisible(not usable)
        self.export_button.setEnabled(usable)
        self._update_row_actions()

    def _reload_rows(self, select_id: str | None = None) -> None:
        entries = self._store.entries
        self.table.setRowCount(len(entries))
        selected_row = -1
        for row, entry in enumerate(entries):
            cells = (entry["source"], entry["target"], direction_label(entry["direction"]),
                     kind_label(entry["kind"]), entry["note"])
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry["id"])
                self.table.setItem(row, column, item)
            if entry["id"] == select_id:
                selected_row = row
        self.empty_label.setVisible(not entries)
        self.count_label.setText(strings.TERMBASE_ENTRY_COUNT.format(n=len(entries)))
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        self._update_row_actions()

    def _selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _update_row_actions(self) -> None:
        has_selection = self._selected_id() is not None and self._store.usable
        self.edit_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)

    def _set_mode(self, entry_id: str | None) -> None:
        self._editing = entry_id
        editing = entry_id is not None
        self.form_title.setText(strings.TERMBASE_EDIT_TITLE.format(id=entry_id) if editing else strings.TERMBASE_ADD_TITLE)
        self.submit_button.setText(strings.TERMBASE_UPDATE if editing else strings.TERMBASE_ADD)
        self.cancel_edit_button.setVisible(editing)

    def _say(self, text: str) -> None:
        self.notice_label.setText(text)

    # -- the form -----------------------------------------------------------------

    def _read_form(self) -> dict | None:
        """The entry the form describes, or None after showing why not."""
        fields = (
            (self.source_edit, strings.TERMBASE_FIELD_SOURCE, tb.SOURCE_SCALARS),
            (self.target_edit, strings.TERMBASE_FIELD_TARGET, tb.TARGET_SCALARS),
            (self.note_edit, strings.TERMBASE_FIELD_NOTE_SHORT, tb.NOTE_SCALARS),
        )
        values: dict[str, str] = {}
        for edit, name, (low, high) in fields:
            text = edit.text().strip()
            if low > 0 and not text:
                self.form_error.show_error(strings.TERMBASE_FIELD_REQUIRED.format(field=name))
                edit.setFocus()
                return None
            if len(text) > high:
                self.form_error.show_error(strings.TERMBASE_FIELD_TOO_LONG.format(field=name, max=high))
                edit.setFocus()
                return None
            if "\u0000" in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                self.form_error.show_error(strings.TERMBASE_FIELD_INVALID.format(field=name))
                edit.setFocus()
                return None
            values[name] = text
        self.form_error.clear()
        return {
            "source": values[strings.TERMBASE_FIELD_SOURCE],
            "target": values[strings.TERMBASE_FIELD_TARGET],
            "note": values[strings.TERMBASE_FIELD_NOTE_SHORT],
            "direction": self.direction_combo.currentData(),
            "kind": self.kind_combo.currentData(),
        }

    def _clear_form(self) -> None:
        for edit in (self.source_edit, self.target_edit, self.note_edit):
            edit.clear()
        self.form_error.clear()

    def _submit(self) -> None:
        if not self._store.usable:
            return
        values = self._read_form()
        if values is None:
            return
        editing = self._editing
        if editing is None and len(self._store) >= tb.MAX_TERMBASE_ENTRIES:
            self.form_error.show_error(strings.TERMBASE_LIST_FULL)
            return
        try:
            if editing is None:
                entry = self._store.add(**values)
            else:
                entry = self._store.update(editing, **values)
        except tb.TermbaseError as error:
            self.form_error.show_error(
                strings.TERMBASE_LIST_TOO_LARGE if error.code == "termbase_too_large" else refusal_text(error)
            )
            return
        except OSError as error:
            self.form_error.show_error(strings.TERMBASE_SAVE_FAILED.format(reason=_os_reason(error)))
            return
        self._clear_form()
        self._set_mode(None)
        self._reload_rows(select_id=entry["id"])
        self._say(strings.TERMBASE_NOTICE_UPDATED if editing else strings.TERMBASE_NOTICE_ADDED)
        self.changed.emit()

    def _edit_selected(self) -> None:
        entry_id = self._selected_id()
        entry = next((e for e in self._store.entries if e["id"] == entry_id), None)
        if entry is None or not self._store.usable:
            return
        self.source_edit.setText(entry["source"])
        self.target_edit.setText(entry["target"])
        self.note_edit.setText(entry["note"])
        self.direction_combo.setCurrentIndex(self.direction_combo.findData(entry["direction"]))
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(entry["kind"]))
        self.form_error.clear()
        self._set_mode(entry["id"])
        self.source_edit.setFocus()

    def _cancel_edit(self) -> None:
        self._clear_form()
        self._set_mode(None)

    def _remove_selected(self) -> None:
        entry_id = self._selected_id()
        entry = next((e for e in self._store.entries if e["id"] == entry_id), None)
        if entry is None or not self._store.usable:
            return
        if not self._ask(strings.TERMBASE_REMOVE_CONFIRM.format(source=entry["source"]),
                         strings.TERMBASE_REMOVE, strings.TERMBASE_REPLACE_KEEP):
            return
        try:
            self._store.remove(entry["id"])
        except OSError as error:
            self.banner.show_message(strings.TERMBASE_SAVE_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return
        if self._editing == entry["id"]:
            self._cancel_edit()
        self._reload_rows()
        self._say(strings.TERMBASE_NOTICE_REMOVED)
        self.changed.emit()

    # -- files the person picks -----------------------------------------------------

    def _ask(self, text: str, confirm_label: str, keep_label: str) -> bool:
        """A yes/no question whose default is to change nothing."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(strings.TERMBASE_DIALOG_TITLE)
        box.setText(text)
        confirm = box.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(keep_label, QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is confirm

    def import_file(self, _checked: bool = False) -> bool:
        name, _ = QFileDialog.getOpenFileName(self, strings.TERMBASE_IMPORT_TITLE, "", strings.TERMBASE_FILE_FILTER)
        if not name:
            return False
        return self.import_path(Path(name))

    def import_path(self, path: Path) -> bool:
        """Replaces the termbase with the file's entries, after the person agrees."""
        try:
            incoming = tb.import_termbase_file(path)
        except tb.TermbaseError as error:
            self.banner.show_message(
                f"{strings.TERMBASE_IMPORT_FAILED} {refusal_text(error)}", SEVERITY_DANGER)
            return False
        except OSError:
            self.banner.show_message(
                f"{strings.TERMBASE_IMPORT_FAILED} {strings.TERMBASE_IMPORT_UNREADABLE}", SEVERITY_DANGER)
            return False
        if len(self._store) or not self._store.usable:
            prompt = (strings.TERMBASE_REPLACE_PROMPT.format(n=len(self._store)) if self._store.usable
                      else strings.TERMBASE_REPLACE_UNUSABLE_PROMPT.format(n=len(incoming["entries"])))
            if not self._ask(prompt, strings.TERMBASE_REPLACE_CONFIRM, strings.TERMBASE_REPLACE_KEEP):
                return False
        try:
            self._store.replace(incoming)
        except tb.TermbaseError as error:
            self.banner.show_message(f"{strings.TERMBASE_IMPORT_FAILED} {refusal_text(error)}", SEVERITY_DANGER)
            return False
        except OSError as error:
            self.banner.show_message(
                strings.TERMBASE_SAVE_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return False
        self._cancel_edit()
        self._reload_rows()
        self._show_file_state()
        self._say(strings.TERMBASE_NOTICE_IMPORTED.format(n=len(incoming["entries"])))
        self.changed.emit()
        return True

    def export_file(self, _checked: bool = False) -> bool:
        name, _ = QFileDialog.getSaveFileName(
            self, strings.TERMBASE_EXPORT_TITLE, strings.TERMBASE_EXPORT_DEFAULT_NAME, strings.TERMBASE_FILE_FILTER)
        if not name:
            return False
        return self.export_path(Path(name))

    def export_path(self, path: Path) -> bool:
        try:
            tb.export_termbase_file(self._store.termbase, path)
        except OSError as error:
            self.banner.show_message(strings.TERMBASE_EXPORT_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return False
        self.banner.clear()
        self._say(strings.TERMBASE_NOTICE_EXPORTED.format(n=len(self._store)))
        return True

    def _reset(self) -> None:
        if not self._ask(strings.TERMBASE_RESET_PROMPT, strings.TERMBASE_REPLACE_CONFIRM, strings.TERMBASE_REPLACE_KEEP):
            return
        try:
            self._store.reset()
        except OSError as error:
            self.banner.show_message(strings.TERMBASE_SAVE_FAILED.format(reason=_os_reason(error)), SEVERITY_DANGER)
            return
        self._reload_rows()
        self._show_file_state()
        self.changed.emit()
