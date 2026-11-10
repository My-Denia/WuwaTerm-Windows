"""Local manuscript workbench backed by the shared review API.

A QTextDocument normalises several characters. LosslessEditor retains the
original text and maps each real edit through the document's UTF-16 offsets;
opening and saving without editing therefore does not rewrite a manuscript.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QPalette, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import semantic, strings
from .. import semantic_session as sessions
from ..errors import (
    ERROR_CANCELLED, ERROR_FORBIDDEN, ERROR_INSECURE_ENDPOINT, ERROR_LLM_BUDGET_EXHAUSTED,
    ERROR_LLM_UNAVAILABLE, ERROR_NOT_CONFIGURED, ERROR_OFFLINE, ERROR_RATE_LIMITED, ERROR_UNAUTHORIZED, ClientError,
)
from ..manuscript import (
    MAX_HISTORY_SEGMENTS, MAX_SEMANTIC_DECISIONS, MAX_SEMANTIC_HISTORY, WorkfileError, choice_conflict_groups,
    empty_workfile, load_workfile, parse_workfile, save_workfile, semantic_payload_fits, serialize_workfile,
)
from ..segmentation import (
    SEGMENT_LIMIT, STATUS_READY, STATUS_UNTRANSLATED, WHITESPACE, pair_is_sendable,
    pair_segments, segment_text,
)
from .icons import icon

# The whole-manuscript term review keeps its historical request shape; above
# this length each side is reviewed per segmentation-v1 pair instead.
_DOCUMENT_REVIEW_LIMIT = 2000
# A pause, not a failure: the run keeps its results and can continue later.
_QUOTA_CODES = frozenset({ERROR_RATE_LIMITED, ERROR_LLM_BUDGET_EXHAUSTED})
# Nothing else can succeed until the owner fixes these, so the run stops.
_FATAL_CODES = frozenset({ERROR_UNAUTHORIZED, ERROR_FORBIDDEN, ERROR_NOT_CONFIGURED,
                          ERROR_INSECURE_ENDPOINT, ERROR_OFFLINE, ERROR_LLM_UNAVAILABLE})
# The same failure on consecutive segments is the service, not the segment.
_REPEATED_FAILURES = 2
TRACK_TERMS, TRACK_SEMANTIC, TRACK_SEGMENTS = 0, 1, 2
_DRAFT_KEYS = ("source", "target", "direction", "alignments", "choices")
_REVIEW_KEYS = ("source", "target", "direction", "alignments", "choices")


def _normalize_semantic(payload: dict) -> dict:
    """Semantic keys are present together, and only with semantic data.

    A manuscript without semantic data stays byte-for-byte a v1 workfile, so
    an untouched draft never looks edited because of an empty list.
    """
    decisions = payload.get("semantic_decisions") or []
    history = payload.get("semantic_history") or []
    if decisions or history:
        payload["semantic_decisions"], payload["semantic_history"] = decisions, history
        payload["semantic_texts"] = payload.get("semantic_texts") or []
    else:
        for key in ("semantic_decisions", "semantic_texts", "semantic_history"):
            payload.pop(key, None)
    return payload


def _core_key(source_core: tuple[int, int], target_core: tuple[int, int], source: str, target: str) -> tuple:
    (a, b), (c, d) = source_core, target_core
    return (a, b, c, d, source[a:b], target[c:d])


def _blank(text: str) -> bool:
    return all(char in WHITESPACE for char in text)


def _display_map(raw: str) -> tuple[str, list[int]]:
    """Qt plain text and the corresponding raw offset at each boundary."""
    out: list[str] = []
    bounds = [0]
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\r":
            i += 2 if i + 1 < len(raw) and raw[i + 1] == "\n" else 1
            out.append("\n")
        else:
            i += 1
            out.append(" " if c == "\xa0" else "\n" if c in "\u2028\u2029" else c)
        bounds.append(i)
    return "".join(out), bounds


def _utf16_position(text: str, index: int) -> int:
    return len(text[:index].encode("utf-16-le")) // 2


def _scalar_boundary(text: str, position: int) -> int | None:
    if position < 0:
        return None
    units = 0
    for index, char in enumerate(text):
        if units == position:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > position:
            return None
    return len(text) if units == position else None


class LosslessEditor(QPlainTextEdit):
    """Use document edits to preserve characters Qt cannot display distinctly."""

    rawChanged = Signal()
    editRejected = Signal()
    undoRequested = Signal()
    redoRequested = Signal()
    compositionChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._raw = ""
        self._shown = ""
        self._bounds = [0]
        self._setting = False
        self._pending: str | None = None
        self._pending_range: tuple[int, int] | None = None
        self._ime_active = False
        self._ime_origin: tuple[int, int] | None = None
        self._ignore_ime_changes = False
        # The raw range the last accepted edit replaced: (start, old_end,
        # new_length) in scalar offsets. It comes from the edit itself, never
        # from comparing texts, so repeated text cannot make it ambiguous.
        self.last_edit: tuple[int, int, int] | None = None
        # Qt's undo buffer contains normalised text, so using it could turn an
        # original CRLF or non-breaking space into a different character.
        self.setUndoRedoEnabled(False)
        self.document().contentsChange.connect(self._content_changed)

    def raw_text(self) -> str:
        return self._raw

    def can_display(self, raw: str) -> bool:
        shown, _ = _display_map(raw)
        probe = QPlainTextEdit()
        probe.setPlainText(raw)
        return probe.toPlainText() == shown

    def set_raw_text(self, raw: str) -> None:
        shown, bounds = _display_map(raw)
        self._setting = True
        try:
            super().setPlainText(raw)
            if self.toPlainText() != shown:
                raise ValueError("text cannot be represented by Qt")
            self._raw, self._shown, self._bounds = raw, shown, bounds
        finally:
            self._setting = False

    def _remember_cursor(self) -> None:
        cursor = self.textCursor()
        self._pending_range = (cursor.selectionStart(), cursor.selectionEnd())

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        self._pending = source.text() if source.hasText() else None
        self._remember_cursor()
        try:
            super().insertFromMimeData(source)
        finally:
            self._pending = None
            self._pending_range = None

    def createMimeDataFromSelection(self) -> QMimeData:  # noqa: N802
        cursor = self.textCursor()
        start = _scalar_boundary(self._shown, cursor.selectionStart())
        end = _scalar_boundary(self._shown, cursor.selectionEnd())
        if start is None or end is None:
            return super().createMimeDataFromSelection()
        data = QMimeData()
        data.setText(self._raw[self._bounds[start]:self._bounds[end]])
        return data

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._ime_active and (event.matches(QKeySequence.StandardKey.Undo) or event.matches(QKeySequence.StandardKey.Redo)):
            super().keyPressEvent(event)
            return
        if (event.key() == Qt.Key.Key_Z and
                event.modifiers() == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.redoRequested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undoRequested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Redo):
            self.redoRequested.emit()
            return
        self._pending = event.text() or None
        self._remember_cursor()
        try:
            super().keyPressEvent(event)
        finally:
            self._pending = None
            self._pending_range = None

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        committed = bool(event.commitString()) or bool(event.replacementLength())
        preedit = event.preeditString()
        if not committed:
            if preedit and not self._ime_active:
                self._remember_cursor()
                self._ime_origin = self._pending_range
                self._ime_active = True
                self.compositionChanged.emit()
            self._ignore_ime_changes = True
            try:
                super().inputMethodEvent(event)
            finally:
                self._ignore_ime_changes = False
                self._pending_range = None
            if not preedit and self._ime_active:
                # Cancelling a preedit after a selection can leave Qt's
                # provisional deletion in the document. Restore the raw
                # draft only after the composition has ended.
                position = self.textCursor().position()
                if self.toPlainText() != self._shown:
                    self.set_raw_text(self._raw)
                    cursor = self.textCursor()
                    cursor.setPosition(min(position, self.document().characterCount() - 1))
                    self.setTextCursor(cursor)
                self._ime_active, self._ime_origin = False, None
                self.compositionChanged.emit()
            return
        self._pending = event.commitString()
        self._remember_cursor()
        if self._ime_active and self._ime_origin is not None:
            start, end = self._ime_origin
            if event.replacementStart() or event.replacementLength():
                relative = self.textCursor().position() + event.replacementStart()
                relative_end = relative + event.replacementLength()
                removed = end - start
                start = relative if relative <= start else relative + removed
                end = relative_end + removed if relative_end >= self._ime_origin[0] else relative_end
            self._pending_range = (start, end)
        elif event.replacementStart() or event.replacementLength():
            start = self.textCursor().position() + event.replacementStart()
            self._pending_range = (start, start + event.replacementLength())
        try:
            super().inputMethodEvent(event)
        finally:
            self._pending = None
            self._pending_range = None
        if preedit:
            self._remember_cursor()
            self._ime_origin = self._pending_range
            self._pending_range = None
            self._ime_active = True
        else:
            self._ime_active, self._ime_origin = False, None
            self.compositionChanged.emit()

    def _content_changed(self, position: int, removed: int, added: int) -> None:
        if self._setting or self._ignore_ime_changes:
            return
        old, new = self._shown, self.toPlainText()
        # Qt reports IME preedit as a whole-document contents change even
        # though committed text is unchanged. Resetting the document here
        # destroys the active composition and moves its caret.
        if old == new and self._pending is None:
            return
        if self._pending is not None and self._pending_range is not None:
            left = _scalar_boundary(old, self._pending_range[0])
            right = _scalar_boundary(old, self._pending_range[1])
            inserted_shown, _ = _display_map(self._pending)
            if left is not None and right is not None and old[:left] + inserted_shown + old[right:] == new:
                raw = self._raw[:self._bounds[left]] + self._pending + self._raw[self._bounds[right]:]
                shown, bounds = _display_map(raw)
                self.last_edit = (self._bounds[left], self._bounds[right], len(self._pending))
                self._raw, self._shown, self._bounds = raw, shown, bounds
                self.rawChanged.emit()
                return
        start = _scalar_boundary(old, position)
        end = _scalar_boundary(old, position + removed)
        new_start = _scalar_boundary(new, position)
        new_end = _scalar_boundary(new, position + added)
        valid = (
            start is not None and end is not None and new_start is not None
            and new_end is not None and old[:start] == new[:new_start]
            and old[end:] == new[new_end:]
        )
        if valid:
            removed_part, inserted = old[start:end], new[new_start:new_end]
            prefix = 0
            while prefix < min(len(removed_part), len(inserted)) and removed_part[prefix] == inserted[prefix]:
                prefix += 1
            suffix = 0
            while (suffix < len(removed_part) - prefix and suffix < len(inserted) - prefix
                   and removed_part[-1 - suffix] == inserted[-1 - suffix]):
                suffix += 1
            left, right = start + prefix, end - suffix
            inserted = inserted[prefix:len(inserted) - suffix if suffix else None]
            raw = self._raw[:self._bounds[left]] + inserted + self._raw[self._bounds[right]:]
            shown, bounds = _display_map(raw)
            if shown == new:
                # The whole range Qt reported, not the trimmed difference.
                old_start, old_end = self._bounds[start], self._bounds[end]
                self.last_edit = (old_start, old_end, len(raw) - len(self._raw) + old_end - old_start)
                self._raw, self._shown, self._bounds = raw, shown, bounds
                self.rawChanged.emit()
                return
        # Never guess which of two visually identical characters was edited.
        raw = self._raw
        self.set_raw_text(raw)
        self.editRejected.emit()

    def select_raw_span(self, span: dict | None, *, move_cursor: bool = True) -> bool:
        self.setExtraSelections([])
        if not isinstance(span, dict):
            return False
        start, end, value = span.get("start"), span.get("end"), span.get("text")
        if (isinstance(start, bool) or not isinstance(start, int) or
                isinstance(end, bool) or not isinstance(end, int) or
                not isinstance(value, str) or start < 0 or end <= start or
                end > len(self._raw) or self._raw[start:end] != value):
            return False
        try:
            left, right = self._bounds.index(start), self._bounds.index(end)
        except ValueError:
            return False
        cursor = self.textCursor()
        cursor.setPosition(_utf16_position(self._shown, left))
        cursor.setPosition(_utf16_position(self._shown, right), QTextCursor.MoveMode.KeepAnchor)
        if move_cursor:
            self.setTextCursor(cursor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(self.palette().color(QPalette.ColorRole.Highlight))
        selection.format.setForeground(self.palette().color(QPalette.ColorRole.HighlightedText))
        self.setExtraSelections([selection])
        return True


class _ElidedDocumentLabel(QLabel):
    """Keep long filenames inside the command bar and available to readers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._update_elision()

    def _update_elision(self) -> None:
        self.setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width())
        ))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_full_text"):
            self._update_elision()


class ManuscriptView(QWidget):
    documentChanged = Signal()
    lookup_requested = Signal(str)

    def __init__(self, api_client, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._payload = empty_workfile()
        self._saved_payload = copy.deepcopy(self._payload)
        self._path: Path | None = None
        self._report: dict | None = None
        self._task: asyncio.Task | None = None
        self._generation = 0
        self._updating = False
        # Document history excludes report snapshots, saved baseline and path:
        # an edit undo must never erase a report added later or undo a save.
        self._undo_history: list[dict] = []
        self._redo_history: list[dict] = []
        # Semantic positions at each history step, restored with the text.
        self._undo_sessions: list[dict] = []
        self._redo_sessions: list[dict] = []
        self._review_state = "none"
        self._review_detail = ""
        self._request_id: str | None = None
        self._conflict_mode = False
        self._conflict_indexes: list[int] = []
        self._document_state: tuple[Path | None, bool, int] | None = None
        self._document_revision = 0
        self._compact = False
        self._inspector_requested = True
        # Semantic review (semantic-review-v1). Capabilities are probed only
        # when the owner starts a review or a translation, never on startup.
        self._capabilities: frozenset[str] | None = None
        self._session = sessions.Session()
        self._semantic_task: asyncio.Task | None = None
        self._semantic_generation = 0
        self._semantic_state = "none"
        self._semantic_detail = ""
        self._segment_failures: dict[tuple[str, str], str] = {}
        self._segment_notes: dict[tuple[str, str], list[str]] = {}
        # Old whole texts by sha256, so an ignored decision can still recover
        # its spans after the owner edits elsewhere (Web ``semantic_texts``).
        self._known_texts: dict[str, str] = {}
        self._history_run = 0
        self._history_written = -1
        self._term_mode = "document"
        self._pairs_key: tuple | None = None
        self._pairs: list = []
        self._issue_rows: list[sessions.LiveIssue] = []
        self._segment_rows: list = []
        self._refreshing_lists = False
        self._translate_task: asyncio.Task | None = None
        self._translate_generation = 0
        # Pairing a long manuscript is not free; typing never waits for it.
        self._derived_timer = QTimer(self)
        self._derived_timer.setSingleShot(True)
        self._derived_timer.setInterval(300)
        self._derived_timer.timeout.connect(self._refresh_derived)

        self.source_edit = LosslessEditor(self)
        self.target_edit = LosslessEditor(self)
        self.source_edit.setPlaceholderText(strings.MANUSCRIPT_SOURCE_PLACEHOLDER)
        self.target_edit.setPlaceholderText(strings.MANUSCRIPT_TARGET_PLACEHOLDER)
        for editor in (self.source_edit, self.target_edit):
            editor.setMinimumHeight(36)
            editor.setMinimumWidth(80)
            editor.rawChanged.connect(lambda side="source" if editor is self.source_edit else "target",
                                      e=editor: self._on_edit(edit=(side, *e.last_edit)))
            editor.undoRequested.connect(self._undo_replace)
            editor.redoRequested.connect(self._redo_edit)
            editor.compositionChanged.connect(self._on_composition_changed)
            editor.editRejected.connect(lambda: self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED))
        self.direction_combo = QComboBox(self)
        self.direction_combo.addItem(strings.DIRECTION_TO_EN, "en")
        self.direction_combo.addItem(strings.DIRECTION_TO_ZH, "zh")
        self.direction_combo.currentIndexChanged.connect(self._on_edit)
        self.review_button = QPushButton(strings.MANUSCRIPT_REVIEW, self)
        self.review_button.setObjectName("primaryButton")
        self.review_button.clicked.connect(self._start_review)
        self.cancel_button = QPushButton(strings.CANCEL_BUTTON, self)
        self.cancel_button.clicked.connect(self._cancel_review)
        self.auto_align_button = QPushButton(strings.MANUSCRIPT_AUTO_ALIGN, self)
        self.auto_align_button.clicked.connect(self._use_auto_alignment)
        self.findings = QListWidget(self)
        self.findings.setMinimumHeight(76)
        self.findings.setMaximumHeight(150)
        self.findings.setWordWrap(True)
        self.findings.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.findings.setAccessibleName(strings.MANUSCRIPT_INSPECTOR_TITLE)
        self.findings.currentRowChanged.connect(self._select_finding)
        self.replacement_edit = QLineEdit(self)
        self.replacement_edit.setPlaceholderText(strings.MANUSCRIPT_REPLACEMENT_HINT)
        self.replacement_edit.textChanged.connect(self._update_controls)
        self.replace_button = QPushButton(strings.MANUSCRIPT_REPLACE, self)
        self.replace_button.clicked.connect(self._replace_one)
        self.undo_button = QPushButton(strings.MANUSCRIPT_UNDO, self)
        self.undo_button.clicked.connect(self._undo_replace)
        self.redo_button = QPushButton(strings.MANUSCRIPT_REDO, self)
        self.redo_button.clicked.connect(self._redo_edit)
        self.new_button = QPushButton(strings.MANUSCRIPT_NEW, self)
        self.new_button.clicked.connect(self.new_document)
        self.open_button = QPushButton(strings.MANUSCRIPT_OPEN, self)
        self.open_button.clicked.connect(self.open_document)
        self.save_button = QPushButton(strings.MANUSCRIPT_SAVE, self)
        # Keep IME preedit in the editor on mouse clicks while retaining Tab navigation.
        self.save_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.save_button.clicked.connect(self.save_document)
        self.save_as_button = QPushButton(strings.MANUSCRIPT_SAVE_AS, self)
        self.save_as_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.save_as_button.clicked.connect(lambda: self.save_document(save_as=True))
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)
        self.save_label = QLabel(self)
        self.report_label = QLabel(self)
        self.report_label.setWordWrap(True)
        self.report_label.setText(strings.MANUSCRIPT_NO_REPORT)
        self.request_id_label = QLabel(strings.REQUEST_ID_LABEL.format(request_id=strings.REQUEST_ID_PLACEHOLDER), self)
        self.history_label = QLabel(self)
        self.history_label.setWordWrap(True)
        self.conflict_label = QLabel(strings.MANUSCRIPT_CHOICES_CONFLICT, self)
        self.conflict_label.setWordWrap(True)
        self.conflict_list = QListWidget(self)
        self.conflict_list.setMaximumHeight(110)
        self.conflict_list.currentRowChanged.connect(self._update_controls)
        self.remove_choice_button = QPushButton(strings.MANUSCRIPT_REMOVE_CHOICE, self)
        self.remove_choice_button.clicked.connect(self._remove_conflicting_choice)
        for widget in (self.conflict_label, self.conflict_list, self.remove_choice_button):
            widget.setVisible(False)
        self.translate_button = QPushButton(self)
        self.translate_button.clicked.connect(self._translate)
        self._build_semantic_widgets()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # These compact, named buttons are actual controls (rather than an
        # icon-only decoration), and keep the save buttons in the tab order.
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("workbenchToolbar")
        toolbar = QHBoxLayout(self.toolbar)
        toolbar.setContentsMargins(10, 5, 10, 5)
        toolbar.setSpacing(2)
        self.document_icon = QLabel(self.toolbar)
        self.document_icon.setFixedWidth(24)
        self._refresh_document_icon()
        toolbar.addWidget(self.document_icon)
        self.document_label = _ElidedDocumentLabel(self.toolbar)
        self.document_label.setObjectName("contextTitle")
        self.document_label.setMinimumWidth(44)
        self.document_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.document_label, 1)
        self.save_label.setObjectName("secondaryText")
        toolbar.addWidget(self.save_label)
        for button, name, description in (
            (self.new_button, "file-plus", strings.MANUSCRIPT_NEW),
            (self.open_button, "folder-open", strings.MANUSCRIPT_OPEN),
            (self.save_button, "save", strings.MANUSCRIPT_SAVE),
            (self.save_as_button, "save", strings.MANUSCRIPT_SAVE_AS),
            (self.undo_button, "undo-2", strings.MANUSCRIPT_UNDO),
            (self.redo_button, "redo-2", strings.MANUSCRIPT_REDO),
        ):
            button.setObjectName("commandButton")
            button.setText("")
            button.setIcon(icon(name))
            button.setIconSize(QSize(18, 18))
            button.setFixedWidth(34)
            button.setToolTip(description)
            button.setAccessibleName(description)
            toolbar.addWidget(button)
        self.direction_combo.setAccessibleName(strings.DIRECTION_LABEL)
        toolbar.addWidget(self.direction_combo)
        self.translate_button.setObjectName("commandButton")
        self.translate_button.setIcon(icon("languages"))
        self.translate_button.setIconSize(QSize(18, 18))
        self.translate_button.setFixedWidth(34)
        translate_description = f"{strings.MANUSCRIPT_TRANSLATE}（Ctrl+Shift+T）"
        self.translate_button.setToolTip(translate_description)
        self.translate_button.setAccessibleName(strings.MANUSCRIPT_TRANSLATE)
        self.review_button.setToolTip(f"{strings.MANUSCRIPT_REVIEW}（Ctrl+Shift+R）")
        toolbar.addWidget(self.review_button)
        toolbar.addWidget(self.cancel_button)
        self.inspector_button = QPushButton(self.toolbar)
        self.inspector_button.setObjectName("commandButton")
        self.inspector_button.setFixedWidth(34)
        self.inspector_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.inspector_button.clicked.connect(self._toggle_inspector)
        toolbar.addWidget(self.inspector_button)
        outer.addWidget(self.toolbar)

        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.setHandleWidth(8)
        for text, editor in ((strings.INPUT_LABEL, self.source_edit), (strings.RESULT_LABEL, self.target_edit)):
            surface = QFrame(self.editor_splitter)
            surface.setObjectName("editorSurface")
            column = QVBoxLayout(surface)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(0)
            header = QFrame(surface)
            header.setObjectName("editorHeader")
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(14, 4, 14, 4)
            label = QLabel(text, header)
            label.setBuddy(editor)
            if editor is self.source_edit:
                self.source_header_label = label
            else:
                self.target_header_label = label
            header_layout.addWidget(label)
            header_layout.addStretch(1)
            if editor is self.target_edit:
                # Translation only ever fills this side, so its action lives here.
                header_layout.addWidget(self.translate_button)
            else:
                # An invisible twin keeps both headers the same height in every style.
                twin = QPushButton(header)
                twin.setObjectName("commandButton")
                twin.setFixedWidth(34)
                twin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                policy = twin.sizePolicy()
                policy.setRetainSizeWhenHidden(True)
                twin.setSizePolicy(policy)
                twin.hide()
                header_layout.addWidget(twin)
            column.addWidget(header)
            column.addWidget(editor, 1)
            self.editor_splitter.addWidget(surface)
        self.editor_splitter.setStretchFactor(0, 1)
        self.editor_splitter.setStretchFactor(1, 1)

        self.inspector = QFrame(self)
        self.inspector.setObjectName("reviewInspector")
        self.inspector.setMinimumWidth(260)
        self.inspector.setMaximumWidth(330)
        inspector_layout = QVBoxLayout(self.inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        inspector_header = QFrame(self.inspector)
        inspector_header.setObjectName("editorHeader")
        inspector_heading = QHBoxLayout(inspector_header)
        inspector_heading.setContentsMargins(14, 4, 8, 4)
        heading = QLabel(strings.MANUSCRIPT_PANEL_TITLE, inspector_header)
        heading.setObjectName("contextTitle")
        self.finding_count = QLabel(inspector_header)
        inspector_heading.addWidget(heading, 1)
        inspector_heading.addWidget(self.finding_count)
        inspector_layout.addWidget(inspector_header)
        inspector_scroll = QScrollArea(self.inspector)
        inspector_scroll.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        inspector_scroll.setWidgetResizable(True)
        inspector_body = QWidget(inspector_scroll)
        inspector_body.setObjectName("inspectorBody")
        details = QVBoxLayout(inspector_body)
        details.setContentsMargins(18, 18, 18, 18)
        details.setSpacing(12)
        tracks = QHBoxLayout()
        tracks.setSpacing(4)
        for button in (self.terms_track_button, self.semantic_track_button, self.segments_track_button):
            tracks.addWidget(button)
        tracks.addStretch(1)
        details.addLayout(tracks)
        self.track_stack = QStackedWidget(inspector_body)
        term_page = QWidget(self.track_stack)
        term_page.setObjectName("inspectorBody")
        term_details = QVBoxLayout(term_page)
        term_details.setContentsMargins(0, 0, 0, 0)
        term_details.setSpacing(12)
        self.terms_note = QLabel(strings.MANUSCRIPT_TERMS_NOTE, term_page)
        self.terms_note.setObjectName("secondaryText")
        self.terms_note.setWordWrap(True)
        term_details.addWidget(self.terms_note)
        summary = QHBoxLayout()
        pair = QVBoxLayout()
        self.finding_title = QLabel(strings.MANUSCRIPT_FINDING_PROMPT, inspector_body)
        self.finding_title.setObjectName("findingTitle")
        self.finding_title.setWordWrap(True)
        pair.addWidget(self.finding_title)
        self.finding_pair = QLabel(inspector_body)
        self.finding_pair.setObjectName("findingPair")
        self.finding_pair.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_pair.setWordWrap(True)
        pair.addWidget(self.finding_pair)
        summary.addLayout(pair, 1)
        self.finding_verdict = QLabel(inspector_body)
        self.finding_verdict.setObjectName("findingVerdict")
        self.finding_verdict.setWordWrap(True)
        summary.addWidget(self.finding_verdict, 0, Qt.AlignmentFlag.AlignTop)
        term_details.addLayout(summary)
        self.finding_rule = QLabel(inspector_body)
        self.finding_rule.setObjectName("secondaryText")
        self.finding_rule.setWordWrap(True)
        self.finding_positions = QLabel(inspector_body)
        self.finding_positions.setObjectName("secondaryText")
        self.finding_positions.setWordWrap(True)
        self.candidates_label = QLabel(inspector_body)
        self.candidates_label.setWordWrap(True)
        term_details.addWidget(self.candidates_label)
        self.lookup_button = QPushButton(strings.NAV_ITEM_TERMS, inspector_body)
        self.lookup_button.setIcon(icon("external-link"))
        self.lookup_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.lookup_button.clicked.connect(self._request_lookup)
        term_details.addWidget(self.lookup_button)
        self.manual_edit_label = QLabel(strings.MANUSCRIPT_MANUAL_EDIT, inspector_body)
        self.manual_edit_label.setWordWrap(True)
        term_details.addWidget(self.manual_edit_label)
        self.replace_toggle = QPushButton(strings.MANUSCRIPT_REPLACE, inspector_body)
        self.replace_toggle.setCheckable(True)
        self.replace_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        term_details.addWidget(self.replace_toggle)
        self.replacement_area = QWidget(inspector_body)
        self.replacement_area.setObjectName("inspectorBody")
        replacement = QVBoxLayout(self.replacement_area)
        replacement.setContentsMargins(0, 0, 0, 0)
        replacement_label = QLabel(strings.MANUSCRIPT_REPLACEMENT, self.replacement_area)
        replacement_label.setBuddy(self.replacement_edit)
        replacement.addWidget(replacement_label)
        replacement.addWidget(self.replacement_edit)
        replacement.addWidget(self.replace_button)
        self.replace_toggle.toggled.connect(self.replacement_area.setVisible)
        term_details.addWidget(self.replacement_area)
        term_details.addWidget(self.findings)
        term_details.addStretch(1)
        self.track_stack.addWidget(term_page)
        self.track_stack.addWidget(self.semantic_page)
        self.track_stack.addWidget(self.segments_page)
        details.addWidget(self.track_stack)
        details.addWidget(self.conflict_label)
        details.addWidget(self.conflict_list)
        details.addWidget(self.remove_choice_button)
        self.details_button = QPushButton(strings.MANUSCRIPT_DETAILS, inspector_body)
        self.details_button.setObjectName("commandButton")
        self.details_button.setCheckable(True)
        details.addWidget(self.details_button)
        self.more_details = QWidget(inspector_body)
        self.more_details.setObjectName("inspectorBody")
        more = QVBoxLayout(self.more_details)
        more.setContentsMargins(0, 0, 0, 0)
        more.addWidget(self.finding_rule)
        more.addWidget(self.finding_positions)
        more.addWidget(self.history_label)
        more.addWidget(self.auto_align_button)
        self.request_id_label.setObjectName("secondaryText")
        self.request_id_label.setWordWrap(True)
        more.addWidget(self.request_id_label)
        self.more_details.hide()
        self.details_button.toggled.connect(self.more_details.setVisible)
        details.addWidget(self.more_details)
        details.addStretch(1)
        inspector_scroll.setWidget(inspector_body)
        inspector_layout.addWidget(inspector_scroll, 1)

        self.work_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.work_splitter.setChildrenCollapsible(False)
        self.work_splitter.setHandleWidth(8)
        self.work_splitter.addWidget(self.editor_splitter)
        self.work_splitter.addWidget(self.inspector)
        self.work_splitter.setStretchFactor(0, 1)
        self.work_splitter.setStretchFactor(1, 0)
        self.work_splitter.setSizes([1000, 300])
        workspace = QWidget(self)
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(12, 10, 12, 10)
        workspace_layout.addWidget(self.work_splitter)
        outer.addWidget(workspace, 1)

        footer = QFrame(self)
        footer.setObjectName("workbenchToolbar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 4, 12, 4)
        self.report_label.setObjectName("secondaryText")
        self.status_label.setObjectName("secondaryText")
        self.limit_label = QLabel(strings.MANUSCRIPT_REVIEW_LIMIT, footer)
        self.limit_label.setObjectName("secondaryText")
        self.limit_label.setWordWrap(True)
        footer_layout.addWidget(self.report_label, 1)
        footer_layout.addWidget(self.limit_label, 1)
        footer_layout.addWidget(self.status_label, 1)
        outer.addWidget(footer)
        self._render_finding(-1)
        self._build_shortcuts()
        self._render_issue(None)
        self._update_inspector_button()
        self._update_controls()

    # -- semantic review: widgets -------------------------------------------
    def _build_semantic_widgets(self) -> None:
        """The two tracks stay apart: deterministic term constraints, and
        model suspicions that say they may be wrong. Segments get their own
        list so long manuscripts can be navigated and their gaps explained."""
        self.track_group = QButtonGroup(self)
        self.track_group.setExclusive(True)
        self.terms_track_button = QPushButton(self)
        self.semantic_track_button = QPushButton(self)
        self.segments_track_button = QPushButton(self)
        for index, button in enumerate((self.terms_track_button, self.semantic_track_button,
                                        self.segments_track_button)):
            button.setObjectName("activityItem")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self.track_group.addButton(button, index)
        self.terms_track_button.setChecked(True)
        self.track_group.idClicked.connect(self._show_track)

        page = QWidget(self)
        page.setObjectName("inspectorBody")
        self.semantic_page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.semantic_note = QLabel(strings.MANUSCRIPT_SEMANTIC_NOTE, page)
        self.semantic_progress = QLabel(strings.MANUSCRIPT_SEMANTIC_NOT_RUN, page)
        for label in (self.semantic_note, self.semantic_progress):
            label.setObjectName("secondaryText")
            label.setWordWrap(True)
            layout.addWidget(label)
        heading = QHBoxLayout()
        self.issue_title = QLabel(strings.MANUSCRIPT_SEMANTIC_PROMPT, page)
        self.issue_title.setObjectName("findingTitle")
        self.issue_title.setTextFormat(Qt.TextFormat.PlainText)
        self.issue_title.setWordWrap(True)
        heading.addWidget(self.issue_title, 1)
        self.issue_state = QLabel(page)
        self.issue_state.setObjectName("findingVerdict")
        self.issue_state.setProperty("verdict", "not_evaluated")
        heading.addWidget(self.issue_state, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)
        self.issue_confidence = QLabel(page)
        self.issue_confidence.setObjectName("secondaryText")
        self.issue_confidence.setToolTip(strings.MANUSCRIPT_CONFIDENCE_NOTE)
        self.issue_excerpts = QLabel(page)
        self.issue_excerpts.setObjectName("findingPair")
        self.issue_rationale = QLabel(page)
        self.issue_suggestion = QLabel(page)
        self.issue_term_effect = QLabel(page)
        self.issue_term_effect.setObjectName("secondaryText")
        self.issue_notice = QLabel(page)
        for label in (self.issue_confidence, self.issue_excerpts, self.issue_rationale,
                      self.issue_suggestion, self.issue_term_effect, self.issue_notice):
            # Model text is untrusted: never rendered as rich text.
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.issue_alternative_button = QPushButton(page)
        self.issue_alternative_button.setObjectName("commandButton")
        self.issue_alternative_button.setCheckable(True)
        self.issue_alternative_button.toggled.connect(self._alternative_toggled)
        self.issue_alternative_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.issue_alternative_button, 0, Qt.AlignmentFlag.AlignLeft)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.accept_button = QPushButton(strings.MANUSCRIPT_ACCEPT, page)
        self.accept_button.setObjectName("primaryButton")
        self.accept_button.setToolTip(f"{strings.MANUSCRIPT_ACCEPT}（Ctrl+Enter）")
        self.accept_button.clicked.connect(self._accept_current)
        self.adjust_button = QPushButton(strings.MANUSCRIPT_ADJUST, page)
        self.adjust_button.setToolTip(f"{strings.MANUSCRIPT_ADJUST}（Ctrl+Shift+E）")
        self.adjust_button.clicked.connect(self._adjust_current)
        self.ignore_button = QPushButton(strings.MANUSCRIPT_IGNORE, page)
        self.ignore_button.setToolTip(f"{strings.MANUSCRIPT_IGNORE}（Ctrl+Shift+I）")
        self.ignore_button.clicked.connect(self._ignore_current)
        for button in (self.accept_button, self.adjust_button, self.ignore_button):
            # Keep an IME composition in the editor when an action is clicked.
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.adjust_area = QWidget(page)
        self.adjust_area.setObjectName("inspectorBody")
        adjust = QVBoxLayout(self.adjust_area)
        adjust.setContentsMargins(0, 0, 0, 0)
        adjust_label = QLabel(strings.MANUSCRIPT_ADJUST_LABEL, self.adjust_area)
        self.adjust_edit = QPlainTextEdit(self.adjust_area)
        self.adjust_edit.setMaximumHeight(90)
        self.adjust_edit.setAccessibleName(strings.MANUSCRIPT_ADJUST_LABEL)
        adjust_label.setBuddy(self.adjust_edit)
        self.adjust_apply_button = QPushButton(strings.MANUSCRIPT_ADJUST_APPLY, self.adjust_area)
        self.adjust_apply_button.clicked.connect(self._apply_adjustment)
        adjust.addWidget(adjust_label)
        adjust.addWidget(self.adjust_edit)
        adjust.addWidget(self.adjust_apply_button)
        self.adjust_area.hide()
        layout.addWidget(self.adjust_area)
        self.semantic_list = QListWidget(page)
        self.semantic_list.setMinimumHeight(76)
        self.semantic_list.setMaximumHeight(180)
        self.semantic_list.setWordWrap(True)
        self.semantic_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.semantic_list.setAccessibleName(strings.MANUSCRIPT_TRACK_SEMANTIC.format(count="").strip())
        self.semantic_list.currentRowChanged.connect(self._select_issue)
        layout.addWidget(self.semantic_list)
        self.shortcuts_label = QLabel(strings.MANUSCRIPT_SHORTCUTS, page)
        self.shortcuts_label.setObjectName("secondaryText")
        self.shortcuts_label.setWordWrap(True)
        layout.addWidget(self.shortcuts_label)
        layout.addStretch(1)

        page = QWidget(self)
        page.setObjectName("inspectorBody")
        self.segments_page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.segments_note = QLabel(strings.MANUSCRIPT_SEGMENTS_NOTE, page)
        self.segments_note.setObjectName("secondaryText")
        self.segments_note.setWordWrap(True)
        layout.addWidget(self.segments_note)
        self.segment_list = QListWidget(page)
        self.segment_list.setMinimumHeight(76)
        self.segment_list.setMaximumHeight(220)
        self.segment_list.setWordWrap(True)
        self.segment_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.segment_list.setAccessibleName(strings.MANUSCRIPT_TRACK_SEGMENTS.format(count="").strip())
        self.segment_list.currentRowChanged.connect(self._select_segment)
        layout.addWidget(self.segment_list)
        self.segment_hint = QLabel(page)
        self.segment_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.segment_hint.setWordWrap(True)
        layout.addWidget(self.segment_hint)
        self.translate_segment_button = QPushButton(strings.MANUSCRIPT_TRANSLATE_SEGMENT, page)
        self.translate_segment_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.translate_segment_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.translate_segment_button.clicked.connect(self._translate)
        layout.addWidget(self.translate_segment_button)
        layout.addStretch(1)

    def _build_shortcuts(self) -> None:
        self._manuscript_shortcuts: list[QShortcut] = []
        for sequence, handler in (
            ("F8", lambda: self._step_issue(1)),
            ("Shift+F8", lambda: self._step_issue(-1)),
            ("Ctrl+Return", self._accept_current),
            ("Ctrl+Enter", self._accept_current),
            ("Ctrl+Shift+E", self._adjust_current),
            ("Ctrl+Shift+I", self._ignore_current),
            ("Ctrl+Shift+R", self._start_review),
            ("Ctrl+Shift+T", self._translate),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
            self._manuscript_shortcuts.append(shortcut)

    def _show_track(self, index: int) -> None:
        button = self.track_group.button(index)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.track_stack.setCurrentIndex(index)
        if index == TRACK_SEGMENTS:
            self._refresh_segments()
        self._update_tracks()

    # -- semantic review: derived state --------------------------------------
    def _decisions(self) -> list[dict]:
        return self._payload.get("semantic_decisions", [])

    def _issue_state(self, issue: sessions.LiveIssue) -> str:
        return sessions.state_of(issue, self._decisions(), self._payload["source"], self._payload["target"])

    def _pairs_key_now(self) -> tuple:
        return (self._payload["source"], self._payload["target"], repr(self._payload["alignments"] or None))

    def _cached_pairs(self) -> list | None:
        """The pairing of the current text if already computed; never computes."""
        return self._pairs if self._pairs_key == self._pairs_key_now() else None

    def _refresh_derived(self) -> None:
        self._current_pairs()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._refresh_segments()
        self._update_tracks()
        self._render_semantic_progress()

    def _current_pairs(self) -> list:
        source, target = self._payload["source"], self._payload["target"]
        alignments = self._payload["alignments"] or None
        key = self._pairs_key_now()
        if key != self._pairs_key:
            try:
                pairs = pair_segments(source, target, SEGMENT_LIMIT, alignments)
            except (ValueError, TypeError, AssertionError):
                # Alignments the rule cannot use fall back to automatic pairing.
                pairs = pair_segments(source, target, SEGMENT_LIMIT, None)
            self._pairs_key, self._pairs = key, pairs
        return self._pairs

    def _segment_key(self, pair) -> tuple:
        """A pair's identity: where its cores are and what they say. Identical
        paragraphs at different places are different segments."""
        source, target = self._payload["source"], self._payload["target"]
        return _core_key((pair.source.core_start, pair.source.core_end),
                         (pair.target.core_start, pair.target.core_end), source, target)

    def _segment_status(self, pair) -> str:
        if pair.status != STATUS_READY:
            return pair.status
        result = self._session.result_for(pair, self._payload["source"], self._payload["target"])
        if not any(self._basis().needs(result)):
            return "reviewed"
        return self._segment_failures.get(self._segment_key(pair), "pending")

    def _progress(self) -> tuple[int, int] | None:
        pairs = self._cached_pairs()
        if pairs is None:
            self._derived_timer.start()
            return None
        ready = [pair for pair in pairs if pair.status == STATUS_READY]
        done = sum(1 for pair in ready if self._segment_status(pair) == "reviewed")
        return done, len(ready)

    def _semantic_available(self) -> bool:
        return self._capabilities is not None and semantic.CAPABILITY_SEMANTIC_REVIEW in self._capabilities

    def _semantic_running(self) -> bool:
        return self._semantic_task is not None and not self._semantic_task.done()

    def _translating(self) -> bool:
        return self._translate_task is not None and not self._translate_task.done()

    def _busy(self) -> bool:
        return self._review_state == "reviewing" or self._semantic_running() or self._translating()

    # -- semantic review: rendering ------------------------------------------
    def _refresh_semantic(self) -> None:
        if self._refreshing_lists:
            return
        current = self._current_issue()
        rows = sorted(self._session.issues(), key=lambda issue: (
            issue.segment_index,
            (issue.source_span or issue.target_span or {"start": 0})["start"]))
        self._refreshing_lists = True
        try:
            self._issue_rows = rows
            self.semantic_list.clear()
            for issue in rows:
                record = issue.issue
                self.semantic_list.addItem(strings.MANUSCRIPT_ISSUE_ITEM.format(
                    state=strings.MANUSCRIPT_ISSUE_STATE[self._issue_state(issue)],
                    category=strings.MANUSCRIPT_CATEGORY.get(record["category"], record["category"]),
                    source=record["source_excerpt"] or strings.MANUSCRIPT_ISSUE_NO_SIDE,
                    target=record["target_excerpt"] or strings.MANUSCRIPT_ISSUE_NO_SIDE))
            row = rows.index(current) if current in rows else -1
            self.semantic_list.setCurrentRow(row)
        finally:
            self._refreshing_lists = False
        self.semantic_list.setVisible(bool(rows))
        self._render_issue(self._current_issue())
        self._render_semantic_progress()

    def _render_semantic_progress(self) -> None:
        if self._semantic_state == "error" and not self._semantic_running():
            self.semantic_progress.setText(self._semantic_detail)
            return
        if self._capabilities is not None and not self._semantic_available() and not self._session.results:
            self.semantic_progress.setText(strings.MANUSCRIPT_SEMANTIC_UNAVAILABLE)
            return
        if self._semantic_state == "none" and not self._session.results and not self._semantic_running():
            self.semantic_progress.setText(strings.MANUSCRIPT_SEMANTIC_NOT_RUN)
            return
        progress = self._progress()
        if progress is None:
            return  # refreshed shortly by the derived-state timer
        done, total = progress
        pending = sum(1 for issue in self._session.issues() if self._issue_state(issue) in sessions.UNHANDLED)
        if self._semantic_running():
            text = strings.MANUSCRIPT_SEMANTIC_RUNNING.format(index=done + 1, done=done, total=total)
        elif self._semantic_state == "paused":
            text = strings.MANUSCRIPT_SEMANTIC_PAUSED.format(done=done, total=total)
        elif self._semantic_state == "quota":
            text = strings.MANUSCRIPT_SEMANTIC_QUOTA.format(done=done, total=total)
        else:
            text = strings.MANUSCRIPT_SEMANTIC_PROGRESS.format(done=done, total=total, pending=pending)
            failed = sum(1 for pair in self._cached_pairs() or () if self._segment_status(pair) in ("failed", "not_reviewed"))
            if failed:
                text += "\n" + strings.MANUSCRIPT_SEMANTIC_FAILED_SEGMENTS.format(count=failed)
            if not self._session.issues() and done:
                text += "\n" + strings.MANUSCRIPT_SEMANTIC_NONE
        self.semantic_progress.setText(text)

    def _current_issue(self) -> sessions.LiveIssue | None:
        row = self.semantic_list.currentRow()
        return self._issue_rows[row] if 0 <= row < len(self._issue_rows) else None

    def _selected_option(self, issue: sessions.LiveIssue) -> tuple[str, str] | None:
        """(replacement, term_effect) of the suggestion or its chosen alternative."""
        suggestion = issue.issue["suggestion"]
        if suggestion is None:
            return None
        alternatives = issue.issue["alternatives"]
        if self.issue_alternative_button.isChecked() and alternatives:
            return alternatives[0]["replacement"], alternatives[0]["term_effect"]
        return suggestion["replacement"], suggestion["term_effect"]

    def _render_issue(self, issue: sessions.LiveIssue | None) -> None:
        widgets = (self.issue_confidence, self.issue_excerpts, self.issue_rationale, self.issue_suggestion,
                   self.issue_term_effect, self.issue_notice)
        if issue is None:
            self.issue_title.setText(strings.MANUSCRIPT_SEMANTIC_PROMPT if self._issue_rows else "")
            self.issue_state.hide()
            for widget in widgets:
                widget.clear()
                widget.hide()
            for widget in (self.issue_alternative_button, self.accept_button, self.adjust_button,
                           self.ignore_button, self.adjust_area):
                widget.hide()
            return
        record = issue.issue
        state = self._issue_state(issue)
        self.issue_title.setText(strings.MANUSCRIPT_CATEGORY.get(record["category"], record["category"]))
        self.issue_state.setText(strings.MANUSCRIPT_ISSUE_STATE[state])
        self.issue_state.show()
        self.issue_confidence.setText(strings.MANUSCRIPT_CONFIDENCE.get(record["confidence"], record["confidence"]))
        self.issue_excerpts.setText(f'{record["source_excerpt"] or strings.MANUSCRIPT_ISSUE_NO_SIDE}\n'
                                    f'→ {record["target_excerpt"] or strings.MANUSCRIPT_ISSUE_NO_SIDE}')
        self.issue_rationale.setText(strings.MANUSCRIPT_ISSUE_RATIONALE.format(rationale=record["rationale"]))
        suggestion = record["suggestion"]
        alternatives = record["alternatives"]
        self.issue_alternative_button.blockSignals(True)
        if suggestion is None or not alternatives:
            self.issue_alternative_button.setChecked(False)
        self.issue_alternative_button.blockSignals(False)
        option = self._selected_option(issue)
        if suggestion is not None and option is not None:
            self.issue_suggestion.setText(strings.MANUSCRIPT_ISSUE_SUGGESTION.format(
                old=suggestion["target_span"]["text"], new=option[0]))
            self.issue_term_effect.setText(strings.MANUSCRIPT_TERM_EFFECT.get(option[1], ""))
        else:
            self.issue_suggestion.clear()
            self.issue_term_effect.clear()
        if alternatives and suggestion is not None:
            self.issue_alternative_button.setText(
                strings.MANUSCRIPT_ISSUE_PRIMARY if self.issue_alternative_button.isChecked()
                else strings.MANUSCRIPT_ISSUE_ALTERNATIVE.format(new=alternatives[0]["replacement"]))
        notices = []
        if record["suggestion_withheld"] is not None:
            notices.append(strings.MANUSCRIPT_ISSUE_WITHHELD)
        elif suggestion is None and issue.target_span is None:
            notices.append(strings.MANUSCRIPT_ISSUE_NO_TARGET)
        elif suggestion is None:
            notices.append(strings.MANUSCRIPT_ISSUE_NO_SUGGESTION)
        if state == sessions.STATE_STALE:
            notices.append(strings.MANUSCRIPT_ISSUE_STALE)
        elif state == sessions.STATE_NEEDS_RECONFIRM and issue.previous is not None:
            notices.append(strings.MANUSCRIPT_ISSUE_RECONFIRM.format(
                decision=strings.MANUSCRIPT_ISSUE_STATE[issue.previous["decision"]]))
        elif state == sessions.STATE_IGNORED_CARRIED:
            notices.append(strings.MANUSCRIPT_ISSUE_CARRIED)
        if issue.previously_applied and state in sessions.UNHANDLED:
            notices.append(strings.MANUSCRIPT_ISSUE_PREVIOUSLY_APPLIED)
        self.issue_notice.setText("\n".join(notices))
        for widget in widgets:
            widget.setVisible(bool(widget.text()))
        self.issue_alternative_button.setVisible(bool(alternatives) and suggestion is not None)
        for button in (self.accept_button, self.adjust_button, self.ignore_button):
            button.show()
        # suggestion_withheld leaves only adjust and ignore (plan §2.3).
        self.accept_button.setVisible(suggestion is not None)
        self.adjust_area.setVisible(self.adjust_area.isVisible() and self._can_adjust(issue))
        self._update_semantic_actions()

    def _can_accept(self, issue: sessions.LiveIssue | None) -> bool:
        return (issue is not None and issue.issue["suggestion"] is not None
                and self._issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
                and sessions.span_live(issue.suggestion_span, self._payload["target"]))

    def _can_adjust(self, issue: sessions.LiveIssue | None) -> bool:
        return (issue is not None and issue.edit_span() is not None
                and self._issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
                and sessions.span_live(issue.edit_span(), self._payload["target"]))

    def _update_semantic_actions(self) -> None:
        issue = self._current_issue()
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        free = not composing and not self._translating()
        self.accept_button.setEnabled(free and self._can_accept(issue))
        self.adjust_button.setEnabled(free and self._can_adjust(issue))
        # A decision already made here stays as history; it is not decided again.
        self.ignore_button.setEnabled(free and issue is not None and self._issue_state(issue) in sessions.UNHANDLED
                                      and sessions.decision_for(issue, self._decisions()) is None)
        self.adjust_apply_button.setEnabled(free and self._can_adjust(issue))
        self.semantic_list.setEnabled(not composing)

    def _alternative_toggled(self, _checked: bool) -> None:
        self._render_issue(self._current_issue())

    def _select_issue(self, _row: int) -> None:
        if self._refreshing_lists:
            return
        issue = self._current_issue()
        self.issue_alternative_button.blockSignals(True)
        self.issue_alternative_button.setChecked(False)
        self.issue_alternative_button.blockSignals(False)
        self.adjust_area.hide()
        self._render_issue(issue)
        self._update_tracks()
        if issue is None:
            return
        source_ok = self.source_edit.select_raw_span(issue.source_span) if issue.source_span else False
        target_ok = self.target_edit.select_raw_span(issue.target_span) if issue.target_span else False
        if issue.source_span is None:
            self.source_edit.setExtraSelections([])
        if issue.target_span is None:
            self.target_edit.setExtraSelections([])
        located = (source_ok or issue.source_span is None) and (target_ok or issue.target_span is None)
        self.status_label.setText(strings.MANUSCRIPT_LOCATED if located else strings.MANUSCRIPT_PARTIAL_LOCATION)

    def _step_issue(self, step: int) -> None:
        """F8 / Shift+F8: the next or previous issue still waiting for a decision."""
        if not self._input_ready() or not self._issue_rows:
            return
        self._show_track(TRACK_SEMANTIC)
        count = len(self._issue_rows)
        start = self.semantic_list.currentRow()
        for offset in range(1, count + 1):
            row = (start + step * offset) % count if start >= 0 else (offset - 1 if step > 0 else count - offset)
            if self._issue_state(self._issue_rows[row]) in sessions.UNHANDLED:
                self.semantic_list.setCurrentRow(row)
                return
        self.status_label.setText(strings.MANUSCRIPT_ALL_HANDLED)

    def _advance_after_decision(self, handled: sessions.LiveIssue) -> None:
        rows = self._issue_rows
        if handled in rows:
            index = rows.index(handled)
            for row in list(range(index + 1, len(rows))) + list(range(0, index)):
                if self._issue_state(rows[row]) in sessions.UNHANDLED:
                    self.semantic_list.setCurrentRow(row)
                    return
        self.status_label.setText(self.status_label.text() + " " + strings.MANUSCRIPT_ALL_HANDLED)

    # -- semantic review: decisions ------------------------------------------
    def _record_decision(self, issue: sessions.LiveIssue, decision: str, *,
                         applied_text: str | None = None, target: str | None = None,
                         replaced: dict | None = None) -> bool:
        """Apply one decision as one undoable edit, after a save preflight."""
        record = sessions.make_decision(issue, decision, applied_text)
        decisions = sessions.with_decision(self._decisions(), issue, record)
        value = self._draft_snapshot()
        value["semantic_decisions"] = decisions
        if target is not None:
            value["target"] = target
            if value["alignments"] is not None:
                value["alignments"] = []
        candidate = copy.deepcopy(self._payload)
        candidate.update(copy.deepcopy(value))
        candidate.setdefault("semantic_history", [])
        candidate["semantic_texts"] = self._semantic_texts(candidate)
        _normalize_semantic(candidate)
        if len(decisions) > MAX_SEMANTIC_DECISIONS or not semantic_payload_fits(candidate):
            self.status_label.setText(strings.MANUSCRIPT_DECISIONS_FULL)
            return False
        edits = None
        if replaced is not None:
            edits = [("target", replaced["start"], replaced["end"], len(applied_text or ""))]
        self._apply_edit(value, edits=edits, exempt=issue)
        return True

    def _accept_current(self) -> None:
        if self.adjust_area.isVisible() and self.adjust_edit.hasFocus():
            self._apply_adjustment()
            return
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None or not self.accept_button.isEnabled():
            return
        replacement, _effect = self._selected_option(issue)
        try:
            # The executable range must still be exactly what was reported.
            changed = semantic.apply_span(self._payload["target"], issue.suggestion_span, replacement)
        except semantic.StaleSpan:
            self.status_label.setText(strings.MANUSCRIPT_RECONFIRM_NEEDED)
            self._refresh_semantic()
            return
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        if self._record_decision(issue, "accepted", applied_text=replacement, target=changed,
                                 replaced=issue.suggestion_span):
            self.status_label.setText(strings.MANUSCRIPT_ACCEPTED)
            self._advance_after_decision(issue)

    def _adjust_current(self) -> None:
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None or not self.adjust_button.isEnabled():
            return
        option = self._selected_option(issue)
        self.adjust_edit.setPlainText(option[0] if option is not None else issue.edit_span()["text"])
        self.adjust_area.show()
        self.adjust_edit.setFocus()
        self.adjust_edit.selectAll()

    def _apply_adjustment(self) -> None:
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None or not self._can_adjust(issue):
            return
        text = self.adjust_edit.toPlainText()
        if len(text) > semantic.MAX_SIDE_SCALARS:
            return
        try:
            changed = semantic.apply_span(self._payload["target"], issue.edit_span(), text)
        except semantic.StaleSpan:
            self.status_label.setText(strings.MANUSCRIPT_RECONFIRM_NEEDED)
            self._refresh_semantic()
            return
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        if self._record_decision(issue, "adjusted", applied_text=text, target=changed,
                                 replaced=issue.edit_span()):
            self.adjust_area.hide()
            self.status_label.setText(strings.MANUSCRIPT_ADJUSTED)
            self._advance_after_decision(issue)

    def _ignore_current(self) -> None:
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None or not self.ignore_button.isEnabled():
            return
        if self._record_decision(issue, "ignored"):
            self.status_label.setText(strings.MANUSCRIPT_IGNORED)
            self._advance_after_decision(issue)

    # -- segments --------------------------------------------------------------
    def _refresh_segments(self) -> None:
        if self._refreshing_lists:
            return
        pairs = self._current_pairs()
        row = self.segment_list.currentRow()
        self._refreshing_lists = True
        try:
            self._segment_rows = list(pairs)
            self.segment_list.clear()
            for pair in pairs:
                status = strings.MANUSCRIPT_SEGMENT_STATUS[self._segment_status(pair)]
                if pair.hard_cut:
                    status += strings.MANUSCRIPT_SEGMENT_HARD_CUT
                self.segment_list.addItem(strings.MANUSCRIPT_SEGMENT_ITEM.format(
                    number=pair.index + 1, status=status,
                    source=pair.source.core_end - pair.source.core_start,
                    target=pair.target.core_end - pair.target.core_start))
            self.segment_list.setCurrentRow(row if row < len(pairs) else -1)
        finally:
            self._refreshing_lists = False
        self._render_segment(self._current_segment())

    def _current_segment(self):
        row = self.segment_list.currentRow()
        return self._segment_rows[row] if 0 <= row < len(self._segment_rows) else None

    def _render_segment(self, pair) -> None:
        if pair is None:
            self.segment_hint.clear()
            self.translate_segment_button.hide()
            return
        status = self._segment_status(pair)
        hints = []
        if status in strings.MANUSCRIPT_SEGMENT_HINT:
            hints.append(strings.MANUSCRIPT_SEGMENT_HINT[status])
        if pair.hard_cut:
            hints.append(strings.MANUSCRIPT_SEGMENT_HINT["hard_cut"])
        hints.extend(self._segment_notes.get(self._segment_key(pair), []))
        self.segment_hint.setText("\n".join(hints))
        self.translate_segment_button.setVisible(pair.status == STATUS_UNTRANSLATED)
        self.translate_segment_button.setEnabled(self._translate_ready())

    def _select_segment(self, _row: int) -> None:
        if self._refreshing_lists:
            return
        pair = self._current_segment()
        self._render_segment(pair)
        self._update_tracks()
        if pair is None:
            return
        source, target = self._payload["source"], self._payload["target"]
        for editor, text, region in ((self.source_edit, source, pair.source), (self.target_edit, target, pair.target)):
            if region.core_end > region.core_start:
                editor.select_raw_span({"start": region.core_start, "end": region.core_end,
                                        "text": text[region.core_start:region.core_end]})
            else:
                editor.setExtraSelections([])

    def _segments_track_text(self) -> str:
        pairs = self._cached_pairs()
        if pairs is None:
            self._derived_timer.start()
            return strings.MANUSCRIPT_TRACK_SEGMENTS.format(count="").strip()
        return strings.MANUSCRIPT_TRACK_SEGMENTS.format(count=len(pairs))

    def _update_tracks(self) -> None:
        term_total = len(self._report["findings"]) if self._report is not None else 0
        pending = sum(1 for issue in self._session.issues() if self._issue_state(issue) in sessions.UNHANDLED)
        for button, text in (
            (self.terms_track_button, strings.MANUSCRIPT_TRACK_TERMS.format(count=term_total)),
            (self.semantic_track_button, strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=pending)),
            (self.segments_track_button, self._segments_track_text()),
        ):
            button.setText(text)
            button.setAccessibleName(text)
        self.segments_track_button.setToolTip(strings.MANUSCRIPT_SEGMENTS_NOTE)
        track = self.track_stack.currentIndex()
        if track == TRACK_SEMANTIC:
            current, total = self.semantic_list.currentRow(), len(self._issue_rows)
        elif track == TRACK_SEGMENTS:
            current, total = self.segment_list.currentRow(), len(self._segment_rows)
        else:
            current, total = self.findings.currentRow(), term_total
        self.finding_count.setText(strings.MANUSCRIPT_FINDING_COUNT.format(
            current=current + 1 if 0 <= current < total else 0, total=total))

    def focus_input(self) -> None:
        self.source_edit.setFocus()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and hasattr(self, "document_icon"):
            self._refresh_document_icon()

    def _refresh_document_icon(self) -> None:
        self.document_icon.setPixmap(icon("file-text").pixmap(18, 18))

    @property
    def document_title(self) -> str:
        return self._path.name if self._path is not None else strings.MANUSCRIPT_UNTITLED

    @property
    def document_dirty(self) -> bool:
        return self._payload != self._saved_payload

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = event.size().width() < 900
        # Short workspaces need the available height for both editing areas.
        # On a narrow but tall workspace, stacking still offers wider lines.
        stacked = compact and event.size().height() >= 560
        orientation = Qt.Orientation.Vertical if stacked else Qt.Orientation.Horizontal
        if self.editor_splitter.orientation() != orientation:
            self.editor_splitter.setOrientation(orientation)
        if compact != self._compact:
            self._compact = compact
            # On a small workspace make room for the manuscript. The action
            # remains available so the inspector can be opened deliberately.
            self.inspector.setVisible(False if compact else self._inspector_requested)
            self.save_label.setVisible(not compact)
            self._update_inspector_button()

    def _toggle_inspector(self) -> None:
        self._inspector_requested = self.inspector.isHidden()
        self.inspector.setVisible(self._inspector_requested)
        if self._inspector_requested:
            self.work_splitter.setSizes([max(1, self.width() - 300), 300])
        self._update_inspector_button()

    def _update_inspector_button(self) -> None:
        showing = not self.inspector.isHidden()
        description = strings.MANUSCRIPT_INSPECTOR_HIDE if showing else strings.MANUSCRIPT_INSPECTOR_SHOW
        self.inspector_button.setIcon(icon("panel-right-close" if showing else "panel-right-open"))
        self.inspector_button.setIconSize(QSize(18, 18))
        self.inspector_button.setToolTip(description)
        self.inspector_button.setAccessibleName(description)

    def _target_span_reliable(self, span: object) -> bool:
        if not isinstance(span, dict):
            return False
        start, end, value = span.get("start"), span.get("end"), span.get("text")
        raw = self.target_edit.raw_text()
        return (type(start) is int and type(end) is int and isinstance(value, str)
                and 0 <= start < end <= len(raw) and raw[start:end] == value
                and start in self.target_edit._bounds and end in self.target_edit._bounds)

    def _render_finding(self, index: int) -> None:
        report = self._report
        finding = report["findings"][index] if report is not None and 0 <= index < len(report["findings"]) else None
        total = len(report["findings"]) if report is not None else 0
        self.finding_count.setText(strings.MANUSCRIPT_FINDING_COUNT.format(
            current=index + 1 if finding is not None else 0, total=total))
        self.findings.setVisible(total > 0)
        if finding is None:
            self.finding_title.setText(
                strings.MANUSCRIPT_FINDING_PROMPT if total else
                strings.MANUSCRIPT_NO_FINDINGS if report is not None else self.report_label.text()
            )
            for label in (self.finding_verdict, self.finding_pair, self.finding_rule, self.finding_positions, self.candidates_label):
                label.clear()
            self.finding_verdict.hide()
            self.lookup_button.hide()
            self.manual_edit_label.hide()
            self.replace_toggle.hide()
            self.replace_toggle.setChecked(False)
            self.replacement_area.hide()
            return
        source = finding.get("source_span") or {}
        target = finding.get("target_span")
        self.finding_verdict.show()
        self.finding_title.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_title.setText(source.get("text", ""))
        verdicts = {"verified_constraint": strings.MANUSCRIPT_VERDICT_VERIFIED,
                    "confirmed_conflict": strings.MANUSCRIPT_VERDICT_CONFLICT,
                    "needs_review": strings.MANUSCRIPT_VERDICT_NEEDS_REVIEW,
                    "not_evaluated": strings.MANUSCRIPT_VERDICT_NOT_EVALUATED}
        self.finding_verdict.setText(verdicts.get(finding["verdict"], finding["verdict"]))
        self.finding_verdict.setProperty("verdict", finding["verdict"])
        self.finding_verdict.style().unpolish(self.finding_verdict)
        self.finding_verdict.style().polish(self.finding_verdict)
        self.finding_pair.setText(target["text"] if isinstance(target, dict) and isinstance(target.get("text"), str) else "")
        rule = strings.MANUSCRIPT_RULE_TERM_PAIR if finding["rule_id"] == "review.term_pair" else finding["rule_id"]
        self.finding_rule.setText(strings.MANUSCRIPT_RULE.format(rule=rule))
        positions = []
        for side, span in ((strings.INPUT_LABEL, source), (strings.RESULT_LABEL, target)):
            if isinstance(span, dict) and type(span.get("start")) is int and type(span.get("end")) is int:
                positions.append(strings.MANUSCRIPT_POSITION.format(side=side, start=span["start"] + 1, end=span["end"]))
        self.finding_positions.setText("\n".join(positions))
        categories = {"core_term": strings.CATEGORY_LABEL_CORE_TERM,
                      "resonator": strings.CATEGORY_LABEL_RESONATOR,
                      "weapon": strings.CATEGORY_LABEL_WEAPON,
                      "echo": strings.CATEGORY_LABEL_ECHO,
                      "skill": strings.CATEGORY_LABEL_SKILL,
                      "sonata_effect": strings.CATEGORY_LABEL_SONATA_EFFECT,
                      "location": strings.CATEGORY_LABEL_LOCATION,
                      "item": strings.CATEGORY_LABEL_ITEM,
                      "speaker": strings.CATEGORY_LABEL_SPEAKER}
        pairs = [f'{item["zh"]} ↔ {item["en"]} · {categories.get(item["category"], item["category"])}'
                 for item in finding["candidates"]]
        self.candidates_label.setTextFormat(Qt.TextFormat.PlainText)
        self.candidates_label.setText("\n".join([strings.MANUSCRIPT_CANDIDATES, *pairs]))
        self.lookup_button.setVisible(bool(source.get("text")))
        reliable = self._target_span_reliable(target)
        self.manual_edit_label.setVisible(not reliable)
        self.replace_toggle.setChecked(False)
        self.replace_toggle.setVisible(reliable and finding["verdict"] != "verified_constraint")
        self.replacement_area.hide()

    def _request_lookup(self) -> None:
        if not self._input_ready():
            return
        index = self.findings.currentRow()
        if self._report is not None and 0 <= index < len(self._report["findings"]):
            source = self._report["findings"][index].get("source_span") or {}
            term = source.get("text")
            if isinstance(term, str) and term:
                self.lookup_requested.emit(term)

    def _update_controls(self) -> None:
        busy = self._busy()
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        # Long manuscripts are reviewed per segmentation-v1 pair, so any
        # non-empty pair of sides can start a review.
        valid = all(len(value) > 0 for value in (self._payload["source"], self._payload["target"]))
        self.review_button.setEnabled(bool(self._api_client.is_configured and valid and not busy and not composing))
        self.review_button.setVisible(not busy)
        progress = self._progress() if self._session.results else None
        partial = self._semantic_state in ("paused", "quota") or bool(progress and progress[0] < progress[1])
        self.review_button.setText(strings.MANUSCRIPT_REVIEW_CONTINUE if partial else strings.MANUSCRIPT_REVIEW)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setText(strings.MANUSCRIPT_REVIEW_PAUSE if self._semantic_running() else strings.CANCEL_BUTTON)
        self.translate_button.setEnabled(self._translate_ready())
        self.translate_segment_button.setEnabled(self._translate_ready())
        self.auto_align_button.setEnabled(self._payload["alignments"] is not None)
        self.findings.setEnabled(not composing)
        self.lookup_button.setEnabled(not composing)
        self.replace_toggle.setEnabled(not composing)
        index = self.findings.currentRow()
        has_target = (self._report is not None and 0 <= index < len(self._report["findings"])
                      and self._target_span_reliable(self._report["findings"][index].get("target_span")))
        self.replace_button.setEnabled(bool(has_target and self.replacement_edit.text() and not composing))
        self.undo_button.setEnabled(bool(self._undo_history) and not composing)
        self.redo_button.setEnabled(bool(self._redo_history) and not composing)
        self.remove_choice_button.setEnabled(0 <= self.conflict_list.currentRow() < len(self._conflict_indexes))
        self.save_label.setText(strings.MANUSCRIPT_SAVED if self._payload == self._saved_payload else strings.MANUSCRIPT_UNSAVED)
        source_language, target_language = (("EN", "ZH") if self._payload["direction"] == "zh" else ("ZH", "EN"))
        self.source_header_label.setText(f"{strings.INPUT_LABEL} · {source_language}")
        self.target_header_label.setText(f"{strings.RESULT_LABEL} · {target_language}")
        self.limit_label.setVisible(any(len(self._payload[key]) > _DOCUMENT_REVIEW_LIMIT for key in ("source", "target")))
        self.document_label.set_full_text(self.document_title + (" *" if self.document_dirty else ""))
        state = (self._path, self.document_dirty, self._document_revision)
        if state != self._document_state:
            self._document_state = state
            self.documentChanged.emit()
        choices, history = len(self._payload["choices"]), len(self._payload["history"])
        self.history_label.setVisible(bool(choices or history))
        self.history_label.setText(strings.MANUSCRIPT_SAVED_DETAILS.format(choices=choices, history=history))
        self._render_review_state()
        self._update_tracks()
        self._update_semantic_actions()
        self._render_semantic_progress()

    def _render_review_state(self) -> None:
        labels = {
            "none": strings.MANUSCRIPT_NO_REPORT,
            "history": strings.MANUSCRIPT_HISTORY,
            "reviewing": strings.MANUSCRIPT_REVIEWING,
            "stale": strings.MANUSCRIPT_REPORT_STALE,
            "stopped": strings.MANUSCRIPT_REVIEW_STOPPED,
            "cancelled": strings.STATUS_CANCELLED_NOTE,
            "error": self._review_detail,
        }
        if self._review_state == "current" and self._report is not None:
            report = self._report
            label = strings.MANUSCRIPT_CURRENT_REPORT.format(
                count=len(report["findings"]), not_evaluated=report["coverage"]["not_evaluated"],
                truncated=strings.STATUS_YES if report["truncated"] or any(f["candidates_truncated"] for f in report["findings"]) else strings.STATUS_NO)
        else:
            label = labels.get(self._review_state, strings.MANUSCRIPT_NO_REPORT)
        self.report_label.setText(label)
        self.report_label.setToolTip(strings.MANUSCRIPT_REPORT_NOTE)
        self.request_id_label.setText(strings.REQUEST_ID_LABEL.format(request_id=self._request_id or strings.REQUEST_ID_PLACEHOLDER))

    def _set_review_state(self, state: str, *, detail: str = "", request_id: str | None = None) -> None:
        self._review_state, self._review_detail, self._request_id = state, detail, request_id
        self._render_review_state()
        if self._report is None:
            self._render_finding(-1)

    def _draft_snapshot(self) -> dict:
        value = {key: self._payload[key] for key in _DRAFT_KEYS}
        # Decisions are part of the draft, so undo and redo take them back too.
        value["semantic_decisions"] = self._payload.get("semantic_decisions", [])
        return copy.deepcopy(value)

    def _restore_draft(self, value: dict) -> None:
        value = copy.deepcopy(value)
        decisions = value.pop("semantic_decisions", [])
        self._payload.update(value)
        self._payload["semantic_decisions"] = decisions
        self._payload.setdefault("semantic_history", [])
        self._payload.setdefault("semantic_texts", [])
        _normalize_semantic(self._payload)

    def _after_draft_change(self, before: dict, after: dict, edits: list | None = None,
                            exempt: sessions.LiveIssue | None = None) -> None:
        """Keep model results across edits; invalidate exactly what an edit touched.

        ``edits`` are the replacements actually performed, as
        ``(side, start, old_end, new_length)``. A text change that arrives
        without them is treated as replacing that whole side.
        """
        if before["direction"] != after["direction"]:
            if self._session.results or self._semantic_running():
                self._clear_semantic()
                self.status_label.setText(strings.MANUSCRIPT_SEMANTIC_DIRECTION_CHANGED)
        elif edits is not None or before["source"] != after["source"] or before["target"] != after["target"]:
            if edits is None:
                edits = [(side, 0, len(before[side]), len(after[side]))
                         for side in ("source", "target") if before[side] != after[side]]
            for side, start, old_end, new_length in edits:
                self._session.apply_edit(side, start, old_end, new_length, exempt=exempt)
        if any(before[key] != after[key] for key in _REVIEW_KEYS):
            self._invalidate_report()
        if self._conflict_mode:
            self._refresh_conflicts()
        self._refresh_semantic()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._derived_timer.start()
        self._update_controls()

    def _push_history(self, stack: list, sessions_stack: list, value: dict) -> tuple[list, list]:
        return (stack + [value])[-20:], (sessions_stack + [self._session.snapshot()])[-20:]

    def _apply_edit(self, value: dict, *, editors_already_updated: bool = False, edits: list | None = None,
                    exempt: sessions.LiveIssue | None = None) -> None:
        before = self._draft_snapshot()
        value = {**before, **copy.deepcopy(value)}
        if value == before:
            return
        self._undo_history, self._undo_sessions = self._push_history(self._undo_history, self._undo_sessions, before)
        self._redo_history.clear()
        self._redo_sessions.clear()
        self._restore_draft(value)
        self._document_revision += 1
        if self.status_label.text() == strings.MANUSCRIPT_SAVE_DONE:
            self.status_label.setText(strings.MANUSCRIPT_UNSAVED)
        if not editors_already_updated:
            self._set_payload(self._payload)
        self._after_draft_change(before, value, edits, exempt)

    def _apply_endpoint_state(self) -> None:
        self._update_controls()

    def _on_composition_changed(self) -> None:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        if composing and self._review_state == "reviewing":
            self._invalidate_report()
        index = self.findings.currentRow()
        if self._report is not None and 0 <= index < len(self._report["findings"]):
            finding = self._report["findings"][index]
            for editor, key in ((self.source_edit, "source_span"), (self.target_edit, "target_span")):
                if editor._ime_active:
                    # A selected span can collapse while Qt displays preedit.
                    editor.setExtraSelections([])
                elif not composing:
                    editor.select_raw_span(finding.get(key), move_cursor=False)
        self._update_controls()

    def _input_ready(self) -> bool:
        if self.source_edit._ime_active or self.target_edit._ime_active:
            self.status_label.setText(strings.MANUSCRIPT_FINISH_INPUT)
            return False
        return True

    def _cancel_pending(self) -> None:
        self._generation += 1
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    def cancel_background_work(self) -> None:
        """Stop every request this view started (window close)."""
        self._cancel_pending()
        self._stop_semantic()
        self._stop_translation()

    def _invalidate_report(self) -> None:
        previous = self._review_state
        self._cancel_pending()
        self._report = None
        self.findings.clear()
        if previous in ("current", "stale"):
            state = "stale"
        elif previous == "reviewing":
            state = "stopped"
        else:
            state = "history" if self._payload["history"] else "none"
        self._set_review_state(state)
        self.source_edit.setExtraSelections([])
        self.target_edit.setExtraSelections([])
        self._update_controls()

    def reset_for_endpoint_change(self) -> None:
        self._invalidate_report()
        self._stop_translation()
        self._clear_semantic()
        self._capabilities = None
        self.status_label.setText(strings.MANUSCRIPT_ENDPOINT_CHANGED)
        self._update_controls()

    def _on_edit(self, *_args, edit: tuple | None = None) -> None:
        if self._updating:
            return
        value = self._draft_snapshot()
        value["source"] = self.source_edit.raw_text()
        value["target"] = self.target_edit.raw_text()
        value["direction"] = self.direction_combo.currentData()
        if value == self._draft_snapshot():
            if edit is not None:
                # Text rewritten with identical text: still an edit of that range.
                self._session.apply_edit(*edit)
                self._refresh_semantic()
                self._update_controls()
            return
        if value["alignments"] is not None:
            value["alignments"] = []
            self.status_label.setText(strings.MANUSCRIPT_ALIGNMENTS_CLEARED)
        self._apply_edit(value, editors_already_updated=True, edits=[edit] if edit is not None else None)

    def _start_review(self) -> None:
        if not self.review_button.isEnabled() or not self._input_ready():
            return
        self._cancel_pending()
        self._stop_semantic()
        generation = self._generation
        source, target = self._payload["source"], self._payload["target"]
        direction = self._payload["direction"]
        alignments = copy.deepcopy(self._payload["alignments"])
        self._report = None
        self.findings.clear()
        # A new run retries the segments that failed before.
        self._segment_failures.clear()
        self._semantic_state = "none"
        self._set_review_state("reviewing")
        self._task = asyncio.ensure_future(self._run_review(generation, source, target, direction, alignments))
        self._task.add_done_callback(lambda task: self._task_finished(task, generation))
        self._update_controls()

    def _use_auto_alignment(self) -> None:
        if self._payload["alignments"] is None or not self._input_ready():
            return
        value = self._draft_snapshot()
        value["alignments"] = None
        self._apply_edit(value)
        self.status_label.setText(strings.MANUSCRIPT_AUTO_ALIGN_SET)
        self._update_controls()

    async def _run_review(self, generation, source, target, direction, alignments) -> None:
        try:
            # Old servers answer 404 and the client keeps its previous
            # behaviour: the deterministic review only.
            capabilities = await self._api_client.get_capabilities()
        except ClientError as exc:
            if generation == self._generation:
                self._set_review_state("error", detail=exc.message, request_id=exc.request_id)
            return
        except asyncio.CancelledError:
            if generation == self._generation:
                self._set_review_state("cancelled")
            return
        if generation != self._generation:
            return
        self._capabilities = capabilities
        document = len(source) <= _DOCUMENT_REVIEW_LIMIT and len(target) <= _DOCUMENT_REVIEW_LIMIT
        self._term_mode = "document" if document else "segments"
        if document and not await self._run_document_review(generation, source, target, direction, alignments):
            return
        if generation != self._generation:
            return
        if not document:
            self._set_review_state("none")
        if not document or self._semantic_available():
            self._start_semantic_run()
        self._update_controls()

    async def _run_document_review(self, generation, source, target, direction, alignments) -> bool:
        try:
            report = await self._api_client.review_pair(source, target, direction, alignments=alignments)
        except ClientError as exc:
            if generation == self._generation:
                self._set_review_state("error", detail=exc.message, request_id=exc.request_id)
            return False
        except asyncio.CancelledError:
            if generation == self._generation:
                self._set_review_state("cancelled")
            return False
        if generation != self._generation:
            return False
        snapshot = {"source": source, "target": target, "direction": direction,
                    "alignments": alignments, "report": report}
        candidate = copy.deepcopy(self._payload)
        candidate["history"] = (candidate["history"] + [snapshot])[-2:]
        try:
            serialize_workfile(candidate)
        except WorkfileError as exc:
            self._set_review_state("error", detail=strings.MANUSCRIPT_REPORT_UNSAVEABLE)
            if exc.code == "workfile_duplicate_choices":
                self._conflict_mode = True
                self._refresh_conflicts()
            return False
        self._payload = candidate
        self._document_revision += 1
        self._show_term_report(report)
        self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)
        self._update_controls()
        return True

    def _show_term_report(self, report: dict) -> None:
        row = self.findings.currentRow()
        self._report = report
        self._set_review_state("current", request_id=report["request_id"])
        self.findings.clear()
        verdicts = {"verified_constraint": strings.MANUSCRIPT_VERDICT_VERIFIED,
                    "confirmed_conflict": strings.MANUSCRIPT_VERDICT_CONFLICT,
                    "needs_review": strings.MANUSCRIPT_VERDICT_NEEDS_REVIEW,
                    "not_evaluated": strings.MANUSCRIPT_VERDICT_NOT_EVALUATED}
        for finding in report["findings"]:
            source_span = finding.get("source_span") or {}
            target_span = finding.get("target_span") or {}
            target_text = target_span.get("text")
            verdict = verdicts[finding["verdict"]]
            second_line = f"{target_text} · {verdict}" if target_text else verdict
            self.findings.addItem(f'{source_span.get("text", "")}\n{second_line}')
        if self.findings.count():
            self.findings.setCurrentRow(row if 0 <= row < self.findings.count() and self._term_mode == "segments" else 0)
        else:
            self._render_finding(-1)

    def _show_segment_term_report(self) -> None:
        """Long manuscripts: one display report from every segment still current."""
        source, target = self._payload["source"], self._payload["target"]
        basis = self._basis()
        usable = [r for r in self._session.results
                  if self._ready_pair_for(r) is not None and sessions.term_part_valid(r, basis)]
        reports = [r.term_report for r in usable]
        if not reports:
            return
        findings = [f for r in usable for f in r.term_findings
                    if sessions.span_live(f.get("source_span"), source) and sessions.span_live(f.get("target_span"), target)]
        self._show_term_report({
            "request_id": reports[-1]["request_id"],
            "findings": findings,
            "coverage": {"evaluated": sum(r["coverage"]["evaluated"] for r in reports),
                         "not_evaluated": sum(r["coverage"]["not_evaluated"] for r in reports),
                         "rules": reports[-1]["coverage"]["rules"]},
            "truncated": any(r["truncated"] for r in reports),
        })

    def _task_finished(self, task: asyncio.Task, generation: int) -> None:
        if generation != self._generation:
            return
        self._task = None
        if task.cancelled():
            self._set_review_state("cancelled")
        self._update_controls()

    def _cancel_review(self) -> None:
        if self._translating():
            self._stop_translation()
            self.status_label.setText(strings.STATUS_CANCELLED_NOTE)
            self._update_controls()
            return
        if self._semantic_running():
            self._stop_semantic("paused")
            self._refresh_semantic()
            self._update_controls()
            return
        if self._task is None:
            return
        self._cancel_pending()
        self._set_review_state("cancelled")
        self._update_controls()

    # -- segmented semantic run ------------------------------------------------
    def _start_semantic_run(self) -> None:
        self._stop_semantic()
        self._history_run += 1
        self._semantic_state = "running"
        generation = self._semantic_generation
        self._semantic_task = asyncio.ensure_future(self._run_segments(generation))
        self._semantic_task.add_done_callback(lambda task: self._semantic_finished(task, generation))
        self._render_semantic_progress()

    def _stop_semantic(self, state: str | None = None) -> None:
        self._semantic_generation += 1
        task, self._semantic_task = self._semantic_task, None
        self._session.pending = None
        if task is not None and not task.done():
            task.cancel()
        if state is not None:
            self._semantic_state = state

    def _clear_semantic(self) -> None:
        self._stop_semantic("none")
        self._session.clear()
        self._segment_failures.clear()
        self._segment_notes.clear()
        self._refresh_semantic()
        self._refresh_segments()

    def _semantic_finished(self, task: asyncio.Task, generation: int) -> None:
        if generation != self._semantic_generation:
            return
        self._semantic_task = None
        if task.cancelled():
            self._semantic_state = "paused"
        elif task.exception() is not None:
            self._semantic_state = "error"
            self._semantic_detail = strings.ERROR_MSG_UNKNOWN
        self._refresh_semantic()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._refresh_segments()
        self._update_controls()

    def _next_pair(self, attempted: set) -> object | None:
        source, target = self._payload["source"], self._payload["target"]
        basis = self._basis()
        for pair in self._current_pairs():
            if pair.status != STATUS_READY or self._segment_key(pair) in attempted:
                continue
            if any(basis.needs(self._session.result_for(pair, source, target))):
                return pair
        return None

    def _basis(self) -> sessions.Basis:
        """The current basis of the segment reuse rule (``sessions.Basis``)."""
        return sessions.Basis(term_required=self._term_mode == "segments",
                              semantic_required=self._semantic_available(),
                              alignments=self._alignments_for)

    def _ready_pair_for(self, result: sessions.SegmentResult):
        source, target = self._payload["source"], self._payload["target"]
        return next((p for p in self._current_pairs()
                     if p.status == STATUS_READY and result.matches(p, source, target)), None)

    def _rebuild_plan(self) -> None:
        """Apply the reuse rule: keep only results, failures and notes that a
        current ready pair can use under the current basis."""
        source, target = self._payload["source"], self._payload["target"]
        pairs = self._current_pairs()
        self._session.rebuild(pairs, source, target, self._basis())
        keys = {self._segment_key(p) for p in pairs if p.status == STATUS_READY}
        self._segment_failures = {k: v for k, v in self._segment_failures.items() if k in keys}
        self._segment_notes = {k: v for k, v in self._segment_notes.items() if k in keys}

    def _segment_alignments(self, pair) -> list | None:
        """The owner's correspondence for one segment's term request.

        ``None`` (automatic) stays automatic. An explicit list, including the
        empty list an edit leaves behind, never becomes automatic. Each span
        is intersected with the segment's core (segmentation-v1 strips the
        whitespace around a region, an alignment span may include it or cross
        into a neighbour) and moved to segment coordinates; the text is the
        core slice, as review-v2 requires. An alignment whose source, or
        non-null target, does not intersect the core is left out.
        """
        return self._alignments_for((pair.source.core_start, pair.source.core_end),
                                    (pair.target.core_start, pair.target.core_end))

    def _alignments_for(self, source_core: tuple[int, int], target_core: tuple[int, int]) -> list | None:
        alignments = self._payload["alignments"]
        if alignments is None:
            return None
        source_text, target_text = self._payload["source"], self._payload["target"]

        def clip(span: dict, core: tuple[int, int], text: str) -> dict | None:
            start, end = max(span["start"], core[0]), min(span["end"], core[1])
            if start >= end:
                return None
            return {"start": start - core[0], "end": end - core[0], "text": text[start:end]}

        out = []
        for item in alignments:
            source = clip(item["source"], source_core, source_text)
            target = None if item["target"] is None else clip(item["target"], target_core, target_text)
            if source is None or (item["target"] is not None and target is None):
                continue
            out.append({"source": source, "target": target})
        return out

    def _context_for(self, pair) -> dict | None:
        if pair.index == 0:
            return None
        previous = self._current_pairs()[pair.index - 1]
        source, target = self._payload["source"], self._payload["target"]
        return semantic.context_before(source[previous.source.core_start:previous.source.core_end],
                                       target[previous.target.core_start:previous.target.core_end])

    async def _run_segments(self, generation: int) -> None:
        """Serial, one request in flight. The plan follows the segment reuse
        rule (``sessions.Basis``): rebuilt at the start, and a pair is sent
        only for the parts it is missing under the current basis."""
        attempted: set[tuple] = set()
        failure_streak: tuple[str | None, int] = (None, 0)
        self._rebuild_plan()
        if self._term_mode == "segments":
            # Segments that need no request still show their term results.
            self._show_segment_term_report()
        while generation == self._semantic_generation:
            pair = self._next_pair(attempted)
            if pair is None:
                break
            source, target, direction = self._payload["source"], self._payload["target"], self._payload["direction"]
            key = self._segment_key(pair)
            attempted.add(key)
            if not pair_is_sendable(pair, source, target):
                # The final pre-send assertion: never send an oversized pair.
                self._segment_failures[key] = "needs_manual_split"
                continue
            existing = self._session.result_for(pair, source, target)
            need_term, need_semantic = self._basis().needs(existing)
            self._session.pending = sessions.SegmentResult(
                sent_source=key[4], sent_target=key[5],
                source_core=(pair.source.core_start, pair.source.core_end),
                target_core=(pair.target.core_start, pair.target.core_end),
                hard_cut=pair.hard_cut, report=None)
            before = self._context_for(pair)
            precedents = semantic.select_precedents(
                sessions.precedent_candidates(self._payload, self._current_pairs()), key[4], pair.source.core_start)
            term_alignments = self._segment_alignments(pair)
            self._render_semantic_progress()
            term_report = report = None
            error: ClientError | None = None
            try:
                if need_term:
                    term_report = await self._api_client.review_pair(
                        key[4], key[5], direction, alignments=term_alignments)
                if need_semantic and generation == self._semantic_generation:
                    report = await self._api_client.semantic_review(
                        key[4], key[5], direction, before=before, precedents=precedents)
            except ClientError as exc:
                error = exc
            if generation != self._semantic_generation:
                return
            pending, self._session.pending = self._session.pending, None
            # Only an answer for a pair that still exists, exactly, is stored.
            target_pair = self._ready_pair_for(pending) if pending is not None else None
            if target_pair is not None:
                key = self._segment_key(target_pair)
                self._store_parts(target_pair, pending, term_report, term_alignments, report, direction)
            if error is not None:
                if error.code == ERROR_CANCELLED:
                    return
                if error.code in _QUOTA_CODES:
                    self._semantic_state = "quota"
                    return
                failure_streak = (error.code, failure_streak[1] + 1 if failure_streak[0] == error.code else 1)
                if target_pair is not None:
                    self._segment_failures[key] = "failed"
                if error.code in _FATAL_CODES or failure_streak[1] >= _REPEATED_FAILURES:
                    self._semantic_state = "error"
                    self._semantic_detail = error.message
                    return
                self._refresh_semantic()
                continue
            if report is not None and report["status"] != "complete":
                if target_pair is not None:
                    self._segment_failures[key] = "not_reviewed"
                continue
            failure_streak = (None, 0)
        if generation == self._semantic_generation:
            self._semantic_state = "done"
            self._rebuild_plan()
            if self._term_mode == "segments":
                self._show_segment_term_report()

    def _store_parts(self, pair, pending: sessions.SegmentResult, term_report: dict | None,
                     term_alignments: list | None, report: dict | None, direction: str) -> None:
        """Store what this request produced for ``pair``; keep valid parts it did not redo."""
        source, target = self._payload["source"], self._payload["target"]
        complete = report is not None and report["status"] == "complete"
        if term_report is None and not complete:
            return
        result = self._session.result_for(pair, source, target)
        if result is None:
            result = sessions.SegmentResult(
                sent_source=pending.sent_source, sent_target=pending.sent_target,
                source_core=pending.source_core, target_core=pending.target_core,
                hard_cut=pending.hard_cut, report=None, index=pair.index)
            self._session.add(result)
        key = self._segment_key(pair)
        if term_report is not None:
            result.term_report, result.term_alignments = term_report, term_alignments
            result.term_findings = sessions.term_findings_from_report(
                term_report, source_offset=result.source_core[0], target_offset=result.target_core[0])
        if complete:
            result.report, result.index = report, pair.index
            result.issues = sessions.live_issues_from_report(
                report, segment_index=pair.index, source_offset=result.source_core[0],
                target_offset=result.target_core[0], source_revision=sessions.revision(source),
                target_revision=sessions.revision(target))
            self._known_texts.setdefault(sessions.revision(source), source)
            self._known_texts.setdefault(sessions.revision(target), target)
            notes = []
            if report["omitted_issues"]:
                notes.append(strings.MANUSCRIPT_SEGMENT_HINT["truncated"].format(count=report["omitted_issues"]))
            if report["dropped_unanchored"]:
                notes.append(strings.MANUSCRIPT_SEGMENT_HINT["dropped"].format(count=report["dropped_unanchored"]))
            self._segment_notes[key] = notes
            # "Exactly one candidate" is judged over the whole current report.
            sessions.apply_reconcile(self._session.issues(), self._decisions(), self._texts_for_reconcile(),
                                     source, target)
            self._remember_semantic_report(direction)
        if not any(self._basis().needs(result)):
            self._segment_failures.pop(key, None)
        if term_report is not None:
            self._show_segment_term_report()
        self._refresh_semantic()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._refresh_segments()
        self._update_controls()

    def _texts_for_reconcile(self) -> dict[str, str]:
        texts = sessions.verified_texts(self._payload.get("semantic_texts") or [])
        texts.update(self._known_texts)
        return texts

    def _semantic_texts(self, payload: dict) -> list[dict]:
        return sessions.semantic_texts_for(payload.get("semantic_decisions") or [], self._texts_for_reconcile(),
                                           payload["source"], payload["target"])

    def _remember_semantic_report(self, direction: str) -> None:
        """Keep this run as one untrusted snapshot of the whole manuscript
        (Web ``semantic_history``); the writer drops the oldest if too large."""
        source, target = self._payload["source"], self._payload["target"]
        segments = [{"segment_index": r.index, "source_start": r.source_core[0], "source_end": r.source_core[1],
                     "target_start": r.target_core[0], "target_end": r.target_core[1], "report": r.report}
                    for r in self._session.results
                    if sessions.semantic_part_valid(r) and self._ready_pair_for(r) is not None]
        snapshot = {"source": source, "target": target, "direction": direction,
                    "segments": copy.deepcopy(segments[:MAX_HISTORY_SEGMENTS])}
        candidate = copy.deepcopy(self._payload)
        history = list(candidate.get("semantic_history", []))
        if self._history_written == self._history_run and history:
            history[-1] = snapshot
        else:
            history.append(snapshot)
        candidate["semantic_history"] = history[-MAX_SEMANTIC_HISTORY:]
        candidate.setdefault("semantic_decisions", [])
        candidate["semantic_texts"] = self._semantic_texts(candidate)
        if semantic_payload_fits(_normalize_semantic(candidate)):
            self._payload = candidate
            self._history_written = self._history_run
            self._document_revision += 1

    # -- translation in the manuscript ---------------------------------------
    def _translate_ready(self) -> bool:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        return bool(self._api_client.is_configured and not self._busy() and not composing
                    and not _blank(self._payload["source"]))

    def _stop_translation(self) -> None:
        self._translate_generation += 1
        task, self._translate_task = self._translate_task, None
        if task is not None and not task.done():
            task.cancel()

    def _translate(self) -> None:
        """Translate a blank translation or one untranslated segment; never
        overwrite text the owner already has."""
        if not self._input_ready() or not self._translate_ready():
            return
        source, target = self._payload["source"], self._payload["target"]
        pair = None
        if not _blank(target):
            pair = self._current_segment() if self.track_stack.currentIndex() == TRACK_SEGMENTS else None
            if pair is None or pair.status != STATUS_UNTRANSLATED:
                self.status_label.setText(strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK)
                return
        self._stop_translation()
        generation = self._translate_generation
        self._translate_task = asyncio.ensure_future(
            self._run_translation(generation, pair, source, target, self._payload["direction"]))
        self._translate_task.add_done_callback(lambda _task: self._update_controls())
        self._update_controls()

    async def _run_translation(self, generation: int, pair, source: str, target: str, direction: str) -> None:
        try:
            # Capabilities are part of the basis: probe again, never reuse.
            self._capabilities = await self._api_client.get_capabilities()
            context_on = semantic.CAPABILITY_TRANSLATION_CONTEXT in self._capabilities
            if pair is None:
                text = source
            else:
                text = source[pair.source.core_start:pair.source.core_end]
            pieces = segment_text(text, SEGMENT_LIMIT)
            count = sum(1 for piece in pieces if piece.core_end > piece.core_start)
            out: list[str] = []
            previous: tuple[str, str] | None = None
            done = 0
            for piece in pieces:
                core = text[piece.core_start:piece.core_end]
                if not core:
                    out.append(text[piece.start:piece.end])
                    continue
                done += 1
                self.status_label.setText(strings.MANUSCRIPT_TRANSLATING.format(index=done, total=count))
                context = None
                if context_on and previous is not None:
                    # Precedents stay off until the ADR 0019 M′ gates pass.
                    before = semantic.context_before(*previous)
                    context = {"before": before} if before is not None else None
                result = await self._api_client.translate(core, to=direction, context=context)
                if generation != self._translate_generation:
                    return
                out.append(text[piece.start:piece.core_start] + result.text + text[piece.core_end:piece.end])
                previous = (core, result.text)
        except ClientError as exc:
            if generation == self._translate_generation and exc.code != ERROR_CANCELLED:
                self.status_label.setText(exc.message)
            return
        if generation != self._translate_generation:
            return
        self._translate_task = None
        if (self._payload["source"] != source or self._payload["target"] != target
                or self._payload["direction"] != direction):
            self.status_label.setText(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            return
        translated = "".join(out)
        if pair is None:
            changed = translated
            edit = ("target", 0, len(target), len(changed))
        else:
            position = pair.target.core_start
            left = "\n\n" if position > 0 and target[position - 1] not in WHITESPACE else ""
            right = "\n\n" if position < len(target) and target[position] not in WHITESPACE else ""
            inserted = left + translated + right
            changed = target[:position] + inserted + target[position:]
            edit = ("target", position, position, len(inserted))
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value["target"] = changed
        if value["alignments"] is not None:
            value["alignments"] = []
        self._apply_edit(value, edits=[edit])
        self.status_label.setText(strings.MANUSCRIPT_TRANSLATED)
        self._update_controls()

    def _select_finding(self, index: int) -> None:
        self._render_finding(index)
        if self._report is None or not 0 <= index < len(self._report["findings"]):
            self._update_controls()
            return
        finding = self._report["findings"][index]
        source_ok = self.source_edit.select_raw_span(finding.get("source_span"))
        target_ok = self.target_edit.select_raw_span(finding.get("target_span"))
        self.status_label.setText(strings.MANUSCRIPT_LOCATED if source_ok and target_ok else strings.MANUSCRIPT_PARTIAL_LOCATION)
        candidates = finding["candidates"]
        if len(candidates) == 1:
            self.replacement_edit.setText(candidates[0][self._payload["direction"]])
        else:
            self.replacement_edit.clear()
        self._update_controls()

    def _replace_one(self) -> None:
        if not self._input_ready():
            return
        index = self.findings.currentRow()
        if self._report is None or not 0 <= index < len(self._report["findings"]):
            return
        span = self._report["findings"][index].get("target_span")
        if not span or not self.target_edit.select_raw_span(span):
            self.status_label.setText(strings.MANUSCRIPT_PARTIAL_LOCATION)
            return
        replacement = self.replacement_edit.text()
        if not replacement:
            return
        raw = self._payload["target"]
        changed = raw[:span["start"]] + replacement + raw[span["end"]:]
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value["target"] = changed
        if value["alignments"] is not None:
            value["alignments"] = []
        self._apply_edit(value, edits=[("target", span["start"], span["end"], len(replacement))])
        self.status_label.setText(strings.MANUSCRIPT_REPLACED)
        self._update_controls()

    def _undo_replace(self) -> None:
        self._step_history(undo=True)

    def _redo_edit(self) -> None:
        self._step_history(undo=False)

    def _step_history(self, *, undo: bool) -> None:
        """Undo and redo restore text, decisions and the semantic positions together."""
        source = self._undo_history if undo else self._redo_history
        if not source or not self._input_ready():
            return
        value = source.pop()
        saved = (self._undo_sessions if undo else self._redo_sessions).pop()
        before = self._draft_snapshot()
        if undo:
            self._redo_history, self._redo_sessions = self._push_history(self._redo_history, self._redo_sessions, before)
        else:
            self._undo_history, self._undo_sessions = self._push_history(self._undo_history, self._undo_sessions, before)
        self._restore_draft(value)
        self._set_payload(self._payload)
        self._session.restore(saved)
        # Positions came back with the snapshot; only the review state follows.
        self._after_draft_change(before, value, edits=[])
        self.status_label.setText(strings.MANUSCRIPT_UNDONE if undo else strings.MANUSCRIPT_REDONE)
        self._update_controls()

    def _set_payload(self, value: dict) -> None:
        self._updating = True
        try:
            self.source_edit.set_raw_text(value["source"])
            self.target_edit.set_raw_text(value["target"])
            self.direction_combo.setCurrentIndex(self.direction_combo.findData(value["direction"]))
            self._payload = _normalize_semantic(copy.deepcopy(value))
            self._document_revision += 1
        finally:
            self._updating = False

    def _refresh_conflicts(self) -> None:
        groups = choice_conflict_groups(self._payload) if self._conflict_mode else []
        self._conflict_indexes = [index for group in groups for index in group]
        self.conflict_list.clear()
        for group_number, group in enumerate(groups, 1):
            for index in group:
                choice = self._payload["choices"][index]
                span = choice["source_span"]
                start, end = int(span["start"]), int(span["end"])
                decision = strings.MANUSCRIPT_CHOICE_NOT_TERM
                if choice["choice"] == "official_pair":
                    decision = strings.MANUSCRIPT_CHOICE_OFFICIAL.format(**choice["candidate"])
                context = choice["source"][max(0, start - 16):end + 16]
                context = context.replace("\r\n", " ↵ ").replace("\n", " ↵ ").replace("\r", " ↵ ")
                self.conflict_list.addItem(strings.MANUSCRIPT_CHOICE_ITEM.format(
                    group=group_number, number=index + 1, decision=decision, context=context,
                    start=start + 1, end=end))
        for widget in (self.conflict_label, self.conflict_list, self.remove_choice_button):
            widget.setVisible(bool(groups))
        if groups:
            self._inspector_requested = True
            self.inspector.show()
            self._update_inspector_button()
        self._update_controls()

    def _remove_conflicting_choice(self) -> None:
        if not self._input_ready():
            return
        row = self.conflict_list.currentRow()
        if not 0 <= row < len(self._conflict_indexes):
            return
        index = self._conflict_indexes[row]
        value = self._draft_snapshot()
        value["choices"].pop(index)
        self._apply_edit(value)
        self.status_label.setText(strings.MANUSCRIPT_CHOICE_REMOVED)

    def confirm_discard(self) -> bool:
        if not self._input_ready():
            return False
        if self._payload == self._saved_payload:
            return True
        box = QMessageBox(self)
        box.setWindowTitle(strings.MANUSCRIPT_UNSAVED_TITLE)
        box.setText(strings.MANUSCRIPT_UNSAVED_QUESTION)
        box.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Save).setText(strings.MANUSCRIPT_SAVE)
        box.button(QMessageBox.StandardButton.Discard).setText(strings.MANUSCRIPT_DISCARD)
        box.button(QMessageBox.StandardButton.Cancel).setText(strings.CANCEL_BUTTON)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            return self.save_document()
        return result == QMessageBox.StandardButton.Discard

    def save_document(self, _checked: bool = False, *, save_as: bool = False) -> bool:
        if not self._input_ready():
            return False
        path = None if save_as else self._path
        if path is None:
            name, _ = QFileDialog.getSaveFileName(self, strings.MANUSCRIPT_SAVE_AS, "", strings.MANUSCRIPT_FILE_FILTER)
            if not name:
                return False
            path = Path(name)
        if "semantic_decisions" in self._payload:
            self._payload["semantic_texts"] = self._semantic_texts(self._payload)
        try:
            save_workfile(path, self._payload)
        except WorkfileError as exc:
            if exc.code == "workfile_duplicate_choices":
                self._conflict_mode = True
                self._refresh_conflicts()
                self.status_label.setText(strings.MANUSCRIPT_CHOICES_CONFLICT)
            else:
                self.status_label.setText(strings.MANUSCRIPT_SAVE_FAILED.format(reason=str(exc)))
            return False
        except OSError as exc:
            self.status_label.setText(strings.MANUSCRIPT_SAVE_FAILED.format(reason=str(exc)))
            return False
        # Hold exactly what was written: the writer may have dropped history to
        # fit 1 MiB (possibly falling back to v1) and unneeded old texts.
        written = parse_workfile(serialize_workfile(self._payload))
        for key in ("semantic_decisions", "semantic_texts", "semantic_history"):
            if key in written:
                self._payload[key] = written[key]
            else:
                self._payload.pop(key, None)
        self._path = path
        self._saved_payload = copy.deepcopy(self._payload)
        self.status_label.setText(strings.MANUSCRIPT_SAVE_DONE)
        self._update_controls()
        return True

    def open_document(self, _checked: bool = False) -> None:
        if not self._input_ready():
            return
        name, _ = QFileDialog.getOpenFileName(self, strings.MANUSCRIPT_OPEN, "", strings.MANUSCRIPT_FILE_FILTER)
        if not name:
            return
        def read_candidate() -> dict:
            value = load_workfile(name)
            if not self.source_edit.can_display(value["source"]) or not self.target_edit.can_display(value["target"]):
                raise ValueError(strings.MANUSCRIPT_UNSUPPORTED_TEXT)
            return value
        try:
            read_candidate()
        except (OSError, WorkfileError, ValueError) as exc:
            self.status_label.setText(strings.MANUSCRIPT_OPEN_FAILED.format(reason=str(exc)))
            return
        if not self.confirm_discard():
            return
        # Saving the dirty draft may have overwritten the selected file. Read
        # again so the restored draft and the saved baseline reflect disk.
        try:
            value = read_candidate()
        except (OSError, WorkfileError, ValueError) as exc:
            self.status_label.setText(strings.MANUSCRIPT_OPEN_FAILED.format(reason=str(exc)))
            return
        self._invalidate_report()
        self._stop_translation()
        self._clear_semantic()
        self._undo_history.clear()
        self._redo_history.clear()
        self._undo_sessions.clear()
        self._redo_sessions.clear()
        self._conflict_mode = False
        self._refresh_conflicts()
        self._set_payload(value)
        self._known_texts = sessions.verified_texts(value.get("semantic_texts") or [])
        # The loaded texts are the versions on-file decisions usually refer to;
        # keep them so an edit before the next review does not lose them.
        for text in (value["source"], value["target"]):
            self._known_texts.setdefault(sessions.revision(text), text)
        self._path = Path(name)
        self._saved_payload = copy.deepcopy(self._payload)
        self._set_review_state("history" if value["history"] else "none")
        self.status_label.setText(strings.MANUSCRIPT_OPENED)
        self._update_controls()

    def new_document(self, _checked: bool = False) -> None:
        if not self.confirm_discard():
            return
        self._invalidate_report()
        self._stop_translation()
        self._clear_semantic()
        self._undo_history.clear()
        self._redo_history.clear()
        self._undo_sessions.clear()
        self._redo_sessions.clear()
        self._conflict_mode = False
        self._refresh_conflicts()
        value = empty_workfile()
        self._set_payload(value)
        self._known_texts = {}
        self._saved_payload = copy.deepcopy(value)
        self._path = None
        self._set_review_state("none")
        self._update_controls()
