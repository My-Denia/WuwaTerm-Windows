"""Local manuscript workbench backed by the shared review API.

A QTextDocument normalises several characters. LosslessEditor retains the
original text and maps each real edit through the document's UTF-16 offsets;
opening and saving without editing therefore does not rewrite a manuscript.
"""
from __future__ import annotations

import asyncio
import bisect
import copy
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence, QPalette, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import markdown_report as mdr
from .. import segment_records as sr
from .. import semantic, strings
from ..config import MAX_PANEL_WIDTH, MIN_PANEL_WIDTH
from .. import semantic_session as sessions
from ..errors import (
    ERROR_CANCELLED, ERROR_FORBIDDEN, ERROR_INSECURE_ENDPOINT, ERROR_INVALID_REQUEST, ERROR_LLM_BUDGET_EXHAUSTED,
    ERROR_LLM_UNAVAILABLE, ERROR_NOT_CONFIGURED, ERROR_OFFLINE, ERROR_RATE_LIMITED, ERROR_UNAUTHORIZED, ClientError,
)
from ..manuscript import (
    MAX_HISTORY_SEGMENTS, MAX_SEGMENT_CHOICE_HISTORY, MAX_SEMANTIC_DECISIONS, MAX_SEMANTIC_HISTORY, WorkfileError,
    choice_conflict_groups,
    empty_workfile, load_workfile, parse_workfile, save_workfile, semantic_payload_fits, serialize_workfile,
)
from ..segmentation import (
    SEGMENT_LIMIT, STATUS_READY, STATUS_UNTRANSLATED, WHITESPACE, pair_is_sendable,
    pair_segments, segment_text,
)
from .components import ChevronComboBox
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
# Precedents (the owner's earlier choices in this manuscript) go with both
# translation and semantic review by default, as on the Web, because the
# Core evaluation's M' gates (same-sense reuse, led astray) were met with
# them (C2 receipt, round 1). Re-read the receipt when the Core pin moves.
PRECEDENTS_DEFAULT_ON = True
TRACK_TERMS, TRACK_SEMANTIC, TRACK_SEGMENTS = 0, 1, 2
# A dropped text file is read whole into the editor, so it gets the same
# ceiling as a workfile.
MAX_IMPORT_BYTES = 1_048_576
_TEXT_SUFFIXES = (".txt", ".md")
_DRAFT_KEYS = ("source", "target", "direction", "alignments", "choices")
_REVIEW_KEYS = ("source", "target", "direction", "alignments", "choices")
# The v3 long-mode parts (segment_records). Their values are never changed in
# place, so copies of the payload share them instead of copying hundreds of
# records on every keystroke.
_PART_KEYS = ("segments", "segment_choice_history", "progress")
# What a finding of a long manuscript still needs, by its record's state.
_RECHECK_STATES = ("stale", "pending", "failed", "not_reviewed", "needs_manual_split", "untranslated",
                   "unpaired_target")


def _copy_payload(value: dict) -> dict:
    """A deep copy that shares the immutable long-mode parts."""
    return {key: item if key in _PART_KEYS else copy.deepcopy(item) for key, item in value.items()}


def _offset(span: dict | None, by: int) -> dict | None:
    return None if span is None else {"start": span["start"] + by, "end": span["end"] + by, "text": span["text"]}


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


@dataclass
class _Translation:
    """One translation run and, once it stops, the cursor to resume it.

    ``target`` is what the next finished pieces are written onto: the blank
    target the run started from (``written`` False: they replace it) or the
    target as the partial write left it (``written`` True: they are appended).
    ``segment`` is the source start of a single untranslated segment, or None
    for the whole manuscript. ``out`` holds the pieces finished since the last
    write, each with its separators exactly as a full run writes them.
    """
    generation: int
    source: str
    direction: str
    target: str
    segment: int | None = None
    written: bool = False
    retry: bool = False
    next_piece: int = 0
    done: int = 0
    count: int = 0
    previous: tuple[str, str] | None = None
    out: list[str] = field(default_factory=list)


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
    # Local files dropped (or pasted) on the editor: (paths, side). The view
    # decides what they mean; the editor only keeps Qt from typing a URL.
    filesDropped = Signal(object, str)
    focused = Signal()

    side = "source"

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
        self._placeholder_text = self.placeholderText()
        self._pending_view: dict | None = None
        self._ime_view: dict | None = None
        self.last_edit_view: dict | None = None
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

    def setPlaceholderText(self, text: str) -> None:  # noqa: N802
        self._placeholder_text = text
        super().setPlaceholderText("" if self._ime_active else text)

    def view_state(self) -> dict:
        """Transient Qt positions: UTF-16 units, never manuscript offsets."""
        cursor = self.textCursor()
        return {"position": cursor.position(), "anchor": cursor.anchor(),
                "vertical": self.verticalScrollBar().value(), "horizontal": self.horizontalScrollBar().value()}

    def restore_view_state(self, state: dict) -> None:
        cursor = self.textCursor()
        end = self.document().characterCount() - 1
        cursor.setPosition(max(0, min(state["anchor"], end)))
        cursor.setPosition(max(0, min(state["position"], end)), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(state["vertical"])
        self.horizontalScrollBar().setValue(state["horizontal"])

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
        self._pending_view = self.view_state()

    @staticmethod
    def local_files(mime) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        # File URLs are intercepted even when the drag also carries text:
        # QMimeData.text() would otherwise hand Qt the URL to type in.
        if self.local_files(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self.local_files(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        files = self.local_files(event.mimeData())
        if not files:
            super().dropEvent(event)
            return
        # Accept and return at once: whatever the drop leads to may show a
        # dialog, and a modal inside the drop blocks the Explorer drag source.
        event.acceptProposedAction()
        self.filesDropped.emit(files, self.side)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self.focused.emit()
        super().focusInEvent(event)

    def raw_cursor_offset(self) -> int:
        index = _scalar_boundary(self._shown, self.textCursor().position())
        return self._bounds[index] if index is not None else 0

    def set_raw_cursor_offset(self, offset: int) -> None:
        """Caret at a raw scalar offset, clamped; one inside a CRLF goes before it."""
        offset = max(0, min(offset, len(self._raw)))
        index = max(0, bisect.bisect_right(self._bounds, offset) - 1)
        cursor = self.textCursor()
        cursor.setPosition(_utf16_position(self._shown, index))
        self.setTextCursor(cursor)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        files = self.local_files(source)
        if files:
            self.filesDropped.emit(files, self.side)
            return
        self._pending = source.text() if source.hasText() else None
        self._remember_cursor()
        try:
            super().insertFromMimeData(source)
        finally:
            self._pending = None
            self._pending_range = None
            self._pending_view = None

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
            self._pending_view = None

    def inputMethodQuery(self, query):
        """Report caret rectangles in this widget's coordinates.

        QPlainTextEdit answers in viewport coordinates, while the input method
        maps the answer from the editor's own origin. The frame and the
        stylesheet padding then move the IME candidate window up and left by
        the viewport offset, onto the line being composed.
        """
        value = super().inputMethodQuery(query)
        if query in (Qt.InputMethodQuery.ImCursorRectangle, Qt.InputMethodQuery.ImAnchorRectangle):
            offset = self.viewport().pos()
            if isinstance(value, QRectF):
                return value.translated(offset.x(), offset.y())
            if isinstance(value, QRect):
                return value.translated(offset)
        return value

    def inputMethodEvent(self, event) -> None:  # noqa: N802
        committed = bool(event.commitString()) or bool(event.replacementLength())
        preedit = event.preeditString()
        # Qt leaves the document empty during preedit and still paints its
        # placeholder. Let Qt draw the composition alone until it ends.
        super().setPlaceholderText("" if preedit else self._placeholder_text)
        if not committed:
            if preedit and not self._ime_active:
                self._remember_cursor()
                self._ime_origin = self._pending_range
                self._ime_view = self._pending_view
                self._ime_active = True
                self.compositionChanged.emit()
            self._ignore_ime_changes = True
            try:
                super().inputMethodEvent(event)
            finally:
                self._ignore_ime_changes = False
                self._pending_range = None
                self._pending_view = None
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
                self._ime_view = None
                self.compositionChanged.emit()
            return
        self._pending = event.commitString()
        self._remember_cursor()
        if self._ime_active and self._ime_origin is not None:
            self._pending_view = self._ime_view
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
            self._pending_view = None
        if preedit:
            self._remember_cursor()
            self._ime_origin = self._pending_range
            self._ime_view = self._pending_view
            self._pending_range = None
            self._pending_view = None
            self._ime_active = True
        else:
            self._ime_active, self._ime_origin = False, None
            self._ime_view = None
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
                self.last_edit_view = self._pending_view
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
                self.last_edit_view = self._pending_view
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
        # Qt scrolls to the caret, not the anchor. Show the end first, then put the
        # caret on the start: a span that fits the editor is wholly in view, and one
        # taller than the editor shows its first line instead of its last.
        cursor = self.textCursor()
        cursor.setPosition(_utf16_position(self._shown, right))
        if move_cursor:
            self.setTextCursor(cursor)
        cursor.setPosition(_utf16_position(self._shown, left), QTextCursor.MoveMode.KeepAnchor)
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
    # File continuity: the owner of the config (MainWindow) keeps the recent
    # list; the view only reports. `documentLeaving` fires after the discard
    # was settled and before the old document is replaced, so work_position()
    # still describes it.
    documentLeaving = Signal()
    documentSaved = Signal()
    documentOpened = Signal(str)

    def __init__(self, api_client, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._payload = empty_workfile()
        self._saved_payload = _copy_payload(self._payload)
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
        self._undo_editor_states: list[dict] = []
        self._redo_editor_states: list[dict] = []
        self._review_state = "none"
        self._review_detail = ""
        self._request_id: str | None = None
        self._conflict_mode = False
        self._conflict_indexes: list[int] = []
        self._document_state: tuple[Path | None, bool, int] | None = None
        self._document_revision = 0
        # Work position (see work_position): the editor last focused, an issue
        # fingerprint waiting for its review to come back, and a counter that
        # drops a deferred scroll restore once another document is open.
        self._last_side = "target"
        self._pending_issue_id: str | None = None
        self._restore_serial = 0
        self.setAcceptDrops(True)
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
        self._row_texts: list[str] = []
        self._rows_dirty = False
        self._index: sessions.DecisionIndex | None = None
        self._segment_rows: list = []
        # The pairing the rows were listed from (identity only).
        self._segment_rows_from: list | None = None
        self._refreshing_lists = False
        self._translate_task: asyncio.Task | None = None
        self._translate_generation = 0
        # idle | waiting | retrying | paused | failed. In paused and failed the
        # run record is the resume cursor; it is kept only while the source,
        # direction and target are exactly what the run left.
        self._translate_state = "idle"
        self._translate_run: _Translation | None = None
        self._translate_status = ""
        # Pairing a long manuscript is not free; typing never waits for it.
        self._derived_timer = QTimer(self)
        self._derived_timer.setSingleShot(True)
        self._derived_timer.setInterval(300)
        self._derived_timer.timeout.connect(self._refresh_derived)
        # Long manuscripts (segment_records). The term basis of this session:
        # None until a review-v2 response supplies it, so nothing saved counts
        # as current before the first check. Never saved.
        self._term_basis: dict | None = None
        # What the records were last normalized against (identity checks only).
        self._records_seen: tuple | None = None
        self._state_cache: dict = {}
        # The selected segment: written to progress.current only on save, so
        # navigating never makes the manuscript look edited.
        self._segment_cursor: int | None = None
        # For each listed term finding of a long manuscript: (record, start, end)
        # relative to that record's source core, and whether it awaits a re-check.
        self._finding_refs: list[tuple[int, int, int]] = []
        self._finding_recheck: list[bool] = []
        self._quiet_row = -1
        # The save-failure list shows history choices instead of conflicts.
        self._history_removal = False

        self.source_edit = LosslessEditor(self)
        self.target_edit = LosslessEditor(self)
        self.source_edit.setPlaceholderText(strings.MANUSCRIPT_SOURCE_PLACEHOLDER)
        self.target_edit.setPlaceholderText(strings.MANUSCRIPT_TARGET_PLACEHOLDER)
        self.target_edit.side = "target"
        for editor in (self.source_edit, self.target_edit):
            editor.filesDropped.connect(self._files_dropped)
            editor.focused.connect(lambda e=editor: setattr(self, "_last_side", e.side))
            editor.setMinimumHeight(36)
            editor.setMinimumWidth(80)
            editor.rawChanged.connect(lambda side="source" if editor is self.source_edit else "target",
                                      e=editor: self._on_edit(edit=(side, *e.last_edit)))
            editor.undoRequested.connect(self._undo_replace)
            editor.redoRequested.connect(self._redo_edit)
            editor.compositionChanged.connect(self._on_composition_changed)
            editor.editRejected.connect(lambda: self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED))
        self.direction_combo = ChevronComboBox(self)
        self.direction_combo.addItem(strings.DIRECTION_TO_EN, "en")
        self.direction_combo.addItem(strings.DIRECTION_TO_ZH, "zh")
        self.direction_combo.currentIndexChanged.connect(self._on_edit)
        self.review_button = QPushButton(strings.MANUSCRIPT_REVIEW, self)
        self.review_button.setObjectName("primaryButton")
        self.review_button.clicked.connect(self._start_review)
        self.cancel_button = QPushButton(strings.CANCEL_BUTTON, self)
        self.cancel_button.clicked.connect(self._cancel_review)
        self.auto_align_button = QPushButton(strings.MANUSCRIPT_AUTO_ALIGN, self)
        # A report is a read-only snapshot for reading or sharing, never a
        # manuscript: it is written as Markdown and cannot be opened to resume.
        self.export_report_button = QPushButton(strings.MANUSCRIPT_EXPORT_REPORT, self)
        self.export_report_button.clicked.connect(self.export_report)
        # Document reports checked in this session (the Web's "trusted"): only
        # these can be current; any other history came from a file.
        self._session_snapshots: list[dict] = []
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
        details.setContentsMargins(16, 14, 16, 16)
        details.setSpacing(12)
        self.review_status = QFrame(inspector_body)
        self.review_status.setObjectName("reviewStatus")
        self.review_status.setProperty("tone", "neutral")
        self.review_status.hide()
        status_layout = QVBoxLayout(self.review_status)
        status_layout.setContentsMargins(12, 8, 12, 10)
        status_layout.setSpacing(6)
        self.semantic_progress.setParent(self.review_status)
        status_layout.addWidget(self.semantic_progress)
        self.review_progress = QProgressBar(self.review_status)
        self.review_progress.setObjectName("progressLine")
        self.review_progress.setTextVisible(False)
        self.review_progress.hide()
        status_layout.addWidget(self.review_progress)
        details.addWidget(self.review_status)
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
        # What to do next while there is nothing to show yet.
        self.inspector_hint = QLabel(inspector_body)
        self.inspector_hint.setObjectName("secondaryText")
        self.inspector_hint.setWordWrap(True)
        pair.addWidget(self.inspector_hint)
        self.finding_pair = QLabel(inspector_body)
        self.finding_pair.setObjectName("findingPair")
        self.finding_pair.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_pair.setWordWrap(True)
        summary.addLayout(pair, 1)
        self.finding_verdict = QLabel(inspector_body)
        self.finding_verdict.setObjectName("findingVerdict")
        self.finding_verdict.setWordWrap(True)
        summary.addWidget(self.finding_verdict, 0, Qt.AlignmentFlag.AlignTop)
        term_details.addLayout(summary)
        term_details.addWidget(self.finding_pair)
        self.finding_rule = QLabel(inspector_body)
        self.finding_rule.setObjectName("secondaryText")
        self.finding_rule.setWordWrap(True)
        self.finding_positions = QLabel(inspector_body)
        self.finding_positions.setObjectName("secondaryText")
        self.finding_positions.setWordWrap(True)
        self.candidates_label = QLabel(inspector_body)
        self.candidates_label.setWordWrap(True)
        term_details.addWidget(self.candidates_label)
        # Long manuscripts: the owner's term choice at this occurrence. It is
        # saved with the segment and sent with that segment's next check.
        self.choice_area = QWidget(inspector_body)
        self.choice_area.setObjectName("inspectorBody")
        choice_layout = QVBoxLayout(self.choice_area)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        choice_layout.setSpacing(6)
        choice_label = QLabel(strings.MANUSCRIPT_TERM_CHOICE_LABEL, self.choice_area)
        self.choice_combo = ChevronComboBox(self.choice_area)
        self.choice_combo.setAccessibleName(strings.MANUSCRIPT_TERM_CHOICE_LABEL)
        # Long candidate texts elide instead of widening the inspector.
        self.choice_combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.choice_combo.setMinimumContentsLength(8)
        self.choice_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        choice_label.setBuddy(self.choice_combo)
        self.choice_button = QPushButton(strings.MANUSCRIPT_TERM_CHOICE_RECORD, self.choice_area)
        self.choice_button.clicked.connect(self._choose_term)
        self.unchoice_button = QPushButton(strings.MANUSCRIPT_TERM_CHOICE_REMOVE, self.choice_area)
        self.unchoice_button.clicked.connect(self._unchoose_term)
        choice_buttons = QHBoxLayout()
        choice_buttons.setSpacing(6)
        for button in (self.choice_button, self.unchoice_button):
            # Keep an IME composition in the editor when an action is clicked.
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            choice_buttons.addWidget(button)
        choice_buttons.addStretch(1)
        self.choice_state = QLabel(self.choice_area)
        self.choice_state.setObjectName("secondaryText")
        self.choice_state.setTextFormat(Qt.TextFormat.PlainText)
        self.choice_state.setWordWrap(True)
        choice_layout.addWidget(choice_label)
        choice_layout.addWidget(self.choice_combo)
        choice_layout.addLayout(choice_buttons)
        choice_layout.addWidget(self.choice_state)
        self.choice_area.hide()
        term_details.addWidget(self.choice_area)
        self.lookup_button = QPushButton(strings.NAV_ITEM_TERMS, inspector_body)
        self.lookup_button.setIcon(icon("external-link"))
        self.lookup_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.lookup_button.clicked.connect(self._request_lookup)
        self.replace_toggle = QPushButton(strings.MANUSCRIPT_REPLACE, inspector_body)
        self.replace_toggle.setCheckable(True)
        self.replace_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        term_actions = QHBoxLayout()
        term_actions.setSpacing(6)
        term_actions.addWidget(self.replace_toggle)
        term_actions.addWidget(self.lookup_button)
        term_actions.addStretch(1)
        term_details.addLayout(term_actions)
        self.manual_edit_label = QLabel(strings.MANUSCRIPT_MANUAL_EDIT, inspector_body)
        self.manual_edit_label.setWordWrap(True)
        term_details.addWidget(self.manual_edit_label)
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
        self.details_button.setIcon(icon("chevron-right"))
        self.details_button.setIconSize(QSize(16, 16))
        self.details_button.toggled.connect(
            lambda shown: self.details_button.setIcon(icon("chevron-down" if shown else "chevron-right")))
        details.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.more_details = QWidget(inspector_body)
        self.more_details.setObjectName("inspectorBody")
        more = QVBoxLayout(self.more_details)
        more.setContentsMargins(0, 0, 0, 0)
        more.addWidget(self.finding_rule)
        more.addWidget(self.finding_positions)
        more.addWidget(self.history_label)
        more.addWidget(self.auto_align_button)
        more.addWidget(self.export_report_button)
        self.request_id_label.setObjectName("secondaryText")
        self.request_id_label.setWordWrap(True)
        more.addWidget(self.request_id_label)
        self.precedents_check = QCheckBox(strings.MANUSCRIPT_USE_PRECEDENTS, self.more_details)
        self.precedents_check.setChecked(PRECEDENTS_DEFAULT_ON)
        self.precedents_check.hide()
        more.addWidget(self.precedents_check)
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
        footer_layout.setSpacing(16)
        footer_layout.addWidget(self.status_label, 3)
        footer_layout.addWidget(self.limit_label, 3)
        self.report_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footer_layout.addWidget(self.report_label, 2)
        outer.addWidget(footer)
        self._render_finding(-1)
        self._build_shortcuts()
        self._render_issue(None)
        self._update_inspector_button()
        self._update_controls()
        self._guard_text_inputs()

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
        self.semantic_track_button.setToolTip(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL_NOTE)
        self.semantic_track_button.hide()
        self.track_group.idClicked.connect(self._show_track)

        page = QWidget(self)
        page.setObjectName("inspectorBody")
        self.semantic_page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.semantic_badge = QLabel(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL, page)
        self.semantic_badge.setObjectName("experimentalBadge")
        self.semantic_badge.setToolTip(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL_NOTE)
        layout.addWidget(self.semantic_badge, 0, Qt.AlignmentFlag.AlignLeft)
        self.semantic_note = QLabel(strings.MANUSCRIPT_SEMANTIC_NOTE, page)
        self.semantic_note.setObjectName("secondaryText")
        self.semantic_note.setWordWrap(True)
        layout.addWidget(self.semantic_note)
        # Shown in the status card above the tracks, so a pause or a limit is
        # visible from every track, not only from the semantic one.
        self.semantic_progress = QLabel(strings.MANUSCRIPT_SEMANTIC_NOT_RUN, page)
        self.semantic_progress.setObjectName("statusText")
        self.semantic_progress.setWordWrap(True)
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
        self.issue_suggestion.setObjectName("suggestionText")
        self.issue_term_effect = QLabel(page)
        self.issue_term_effect.setObjectName("secondaryText")
        self.issue_notice = QLabel(page)
        self.issue_notice.setObjectName("noticeText")
        for label in (self.issue_confidence, self.issue_excerpts, self.issue_rationale,
                      self.issue_suggestion, self.issue_term_effect, self.issue_notice):
            # Model text is untrusted: never rendered as rich text.
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.issue_alternative_button = QPushButton(page)
        self.issue_alternative_button.setObjectName("linkButton")
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
        self.shortcuts_label.setObjectName("captionText")
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
        segment_actions = QHBoxLayout()
        segment_actions.setSpacing(6)
        self.finish_button = QPushButton(strings.MANUSCRIPT_SEGMENT_FINISH, page)
        self.finish_button.clicked.connect(self._toggle_finished)
        self.next_pending_button = QPushButton(strings.MANUSCRIPT_NEXT_PENDING, page)
        self.next_pending_button.clicked.connect(self._next_pending)
        for button in (self.finish_button, self.next_pending_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            segment_actions.addWidget(button)
        segment_actions.addStretch(1)
        layout.addLayout(segment_actions)
        # The selected segment's term choices, and history choices that can
        # come back here: none is ever dropped without the owner removing it.
        self.segment_choices = QListWidget(page)
        self.segment_choices.setMaximumHeight(120)
        self.segment_choices.setWordWrap(True)
        self.segment_choices.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.segment_choices.setAccessibleName(strings.MANUSCRIPT_SEGMENT_CHOICES)
        self.segment_choices.currentRowChanged.connect(self._update_segment_choice_actions)
        layout.addWidget(self.segment_choices)
        choice_actions = QHBoxLayout()
        choice_actions.setSpacing(6)
        self.segment_choice_action = QPushButton(strings.MANUSCRIPT_TERM_CHOICE_RECONFIRM, page)
        self.segment_choice_action.clicked.connect(self._segment_choice_primary)
        self.segment_choice_remove = QPushButton(strings.MANUSCRIPT_TERM_CHOICE_REMOVE, page)
        self.segment_choice_remove.clicked.connect(self._segment_choice_removed)
        for button in (self.segment_choice_action, self.segment_choice_remove):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            choice_actions.addWidget(button)
        choice_actions.addStretch(1)
        layout.addLayout(choice_actions)
        self._segment_choice_rows: list[tuple] = []
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
        if index == TRACK_SEMANTIC and self._rows_dirty:
            self._refresh_semantic()
        self._update_tracks()

    # -- semantic review: derived state --------------------------------------
    def _decisions(self) -> list[dict]:
        return self._payload.get("semantic_decisions", [])

    def _decision_index(self) -> sessions.DecisionIndex:
        """Decisions by occurrence, rebuilt only when the decisions list is
        replaced (every change assigns a new list: edits, undo/redo, loading)."""
        decisions = self._decisions()
        if self._index is None or self._index.decisions is not decisions:
            self._index = sessions.DecisionIndex(decisions)
        return self._index

    def _issue_state(self, issue: sessions.LiveIssue) -> str:
        return sessions.state_of(issue, self._decision_index(), self._payload["source"], self._payload["target"])

    def _pairs_key_now(self) -> tuple:
        return (self._payload["source"], self._payload["target"], repr(self._payload["alignments"] or None))

    def _cached_pairs(self) -> list | None:
        """The pairing of the current text if already computed; never computes."""
        return self._pairs if self._pairs_key == self._pairs_key_now() else None

    def _refresh_derived(self) -> None:
        self._current_pairs()
        self._records()
        # An edit cleared the list; what is still current comes straight back.
        if self._report is None and self._review_state in ("none", "history", "stale", "saved", "current"):
            self._show_segment_term_report()
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

    # -- long manuscripts: segment records (segment_records) ---------------
    def _long_now(self) -> bool:
        return sr.long_mode(self._payload["source"], self._payload["target"])

    def _put_parts(self, parts: sr.Parts) -> None:
        """Hold ``parts`` in the payload, in place; not an undo step of its own."""
        value = sr.with_parts(self._payload, parts)
        for key in _PART_KEYS:
            if key in value:
                self._payload[key] = value[key]
            else:
                self._payload.pop(key, None)

    def _records(self) -> list | None:
        """The long-mode records, one per pair of ``_current_pairs()``, or None
        in document mode. The only way records are read: they are normalized
        here whenever the text, inputs, pairing, basis or parts changed.
        Normalizing is not an edit, and an unchanged file stays unchanged."""
        payload = self._payload
        long = self._long_now()
        if not long and not payload.get("segments"):
            return None
        pairs = self._current_pairs() if long else []

        def now() -> tuple:
            # The stored objects themselves: an absent part is None, never a fresh [].
            return (payload["source"], payload["target"], payload["alignments"], self._term_basis, pairs,
                    *(payload.get(key) for key in _PART_KEYS))
        seen = self._records_seen
        if seen is None or seen[-1] != payload["direction"] or any(a is not b for a, b in zip(seen, now())):
            self._put_parts(sr.normalize(payload, pairs, self._term_basis, check_limits=False))
            self._records_seen = (*now(), payload["direction"])
        return payload.get("segments") or [] if long else None

    def _term_state(self, record: dict) -> str:
        """current, saved (before the session's first check), stale or pending,
        by the segment record reuse rule. Cached per record object while the
        texts, inputs and basis are the same objects: records never change in
        place, and every status refresh asks for all of them."""
        if record["result"] is None:
            return "pending"
        payload = self._payload
        key = (payload["source"], payload["target"], payload["alignments"], payload["direction"], self._term_basis)
        cache = self._state_cache
        if cache.get("key") is None or any(a is not b for a, b in zip(cache["key"], key)) \
                or cache["key"][3] != key[3]:
            cache.clear()
            cache["key"] = key
        hit = cache.get(id(record))
        if hit is not None and hit[0] is record:
            return hit[1]
        if self._term_basis is None:
            state = "saved" if sr.reuse_candidate(record, payload) else "stale"
        else:
            state = "current" if sr.is_current(record, payload, self._term_basis) else "stale"
        cache[id(record)] = (record, state)
        return state

    def _has_results(self) -> bool:
        return bool(self._session.results) or any(r["status"] != "pending" for r in self._payload.get("segments") or ())

    def _segment_status(self, pair) -> str:
        if pair.status != STATUS_READY:
            return pair.status
        records = self._records()
        term = "current" if records is None else self._term_state(records[pair.index])
        result = self._session.result_for(pair, self._payload["source"], self._payload["target"])
        if term == "current" and not self._basis().needs(result):
            return "reviewed"
        failure = self._segment_failures.get(self._segment_key(pair))
        if failure is not None:
            return failure
        return term if term in ("saved", "stale") else "pending"

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
    def _issue_row_text(self, issue: sessions.LiveIssue) -> str:
        record = issue.issue
        return strings.MANUSCRIPT_ISSUE_ITEM.format(
            state=strings.MANUSCRIPT_ISSUE_STATE[self._issue_state(issue)],
            category=strings.MANUSCRIPT_CATEGORY.get(record["category"], record["category"]),
            source=record["source_excerpt"] or strings.MANUSCRIPT_ISSUE_NO_SIDE,
            target=record["target_excerpt"] or strings.MANUSCRIPT_ISSUE_NO_SIDE)

    def _refresh_semantic(self) -> None:
        """Keep the issue rows in step. The list widget is rebuilt only when the
        rows themselves change; otherwise only changed row texts are updated,
        and not at all while the semantic track is hidden."""
        if self._refreshing_lists:
            return
        current = self._current_issue()
        rows = sorted(self._session.issues(), key=lambda issue: (
            issue.segment_index,
            (issue.source_span or issue.target_span or {"start": 0})["start"]))
        same = len(rows) == len(self._issue_rows) and all(a is b for a, b in zip(rows, self._issue_rows))
        visible = self.track_stack.currentIndex() == TRACK_SEMANTIC
        self._refreshing_lists = True
        try:
            if not same:
                self._issue_rows = rows
                self._row_texts = [self._issue_row_text(issue) for issue in rows]
                self.semantic_list.clear()
                self.semantic_list.addItems(self._row_texts)
                self.semantic_list.setCurrentRow(rows.index(current) if current in rows else -1)
                self._rows_dirty = False
            elif not visible:
                self._rows_dirty = True
            else:
                for row, issue in enumerate(rows):
                    text = self._issue_row_text(issue)
                    if text != self._row_texts[row]:
                        self._row_texts[row] = text
                        self.semantic_list.item(row).setText(text)
                self._rows_dirty = False
        finally:
            self._refreshing_lists = False
        self._select_pending_issue()
        self.semantic_list.setVisible(bool(rows))
        self._render_issue(self._current_issue())
        self._render_semantic_progress()

    def _render_semantic_progress(self) -> None:
        if self._semantic_state == "error" and not self._semantic_running():
            self._show_status(self._semantic_detail, "danger")
            return
        semantic = self._semantic_available()
        if self._capabilities is not None and not semantic and not self._has_results():
            # The usual state: a short manuscript reviewed for terms only. Its
            # result is the term report; an empty semantic card would be noise.
            self.semantic_progress.setText(strings.MANUSCRIPT_SEMANTIC_UNAVAILABLE)
            self._hide_status()
            return
        if self._semantic_state == "none" and not self._has_results() and not self._semantic_running():
            self.semantic_progress.setText(strings.MANUSCRIPT_SEMANTIC_NOT_RUN)
            self._hide_status()
            return
        progress = self._progress()
        if progress is None:
            return  # refreshed shortly by the derived-state timer
        done, total = progress
        pending = sum(1 for issue in self._session.issues() if self._issue_state(issue) in sessions.UNHANDLED)
        tone, bar = "neutral", None
        if self._semantic_running():
            # Name the segment actually in flight, numbered as in the segment
            # list; the count of segments already reviewed is separate.
            checking = self._session.pending
            pairs = self._cached_pairs() or ()
            text = (strings.MANUSCRIPT_SEMANTIC_RUNNING.format(index=checking.index + 1, count=len(pairs),
                                                               done=done, total=total)
                    if checking is not None else strings.MANUSCRIPT_SEMANTIC_STARTING.format(done=done, total=total))
            tone, bar = "busy", (done, total)
        elif self._semantic_state == "paused":
            text = strings.MANUSCRIPT_SEMANTIC_PAUSED.format(done=done, total=total)
            tone, bar = "paused", (done, total)
        elif self._semantic_state == "quota":
            text = strings.MANUSCRIPT_SEMANTIC_QUOTA.format(done=done, total=total)
            tone, bar = "warn", (done, total)
        else:
            text = (strings.MANUSCRIPT_SEMANTIC_PROGRESS.format(done=done, total=total, pending=pending) if semantic
                    else strings.MANUSCRIPT_SEGMENTS_PROGRESS.format(done=done, total=total))
            statuses = [self._segment_status(pair) for pair in self._cached_pairs() or ()]
            failed = sum(1 for status in statuses if status in ("failed", "not_reviewed"))
            if failed:
                text += "\n" + strings.MANUSCRIPT_SEMANTIC_FAILED_SEGMENTS.format(count=failed)
                tone = "warn"
            if "saved" in statuses:
                text += "\n" + strings.MANUSCRIPT_SEGMENTS_SAVED.format(count=statuses.count("saved"))
            elif "stale" in statuses:
                text += "\n" + strings.MANUSCRIPT_SEGMENTS_RECHECK.format(count=statuses.count("stale"))
            finished = len(sr.parts_of(self._payload).progress["completed"])
            if finished:
                text += "\n" + strings.MANUSCRIPT_SEGMENTS_FINISHED.format(count=finished)
            if semantic and not self._session.issues() and done:
                text += "\n" + strings.MANUSCRIPT_SEMANTIC_NONE
        self._show_status(text, tone, bar)

    def _show_status(self, text: str, tone: str, bar: tuple[int, int] | None = None) -> None:
        """One status card for the run: text, a colour for its kind, and the
        segment count as a bar while the run is going or waiting to go on.
        Neutral and done look alike on purpose: no issues is not a pass."""
        self.semantic_progress.setText(text)
        if self.review_status.isHidden():
            self.review_status.show()
        if self.review_status.property("tone") != tone:
            self.review_status.setProperty("tone", tone)
            self.review_status.style().unpolish(self.review_status)
            self.review_status.style().polish(self.review_status)
        show_bar = bar is not None and bar[1] > 0
        if show_bar:
            self.review_progress.setMaximum(bar[1])
            self.review_progress.setValue(bar[0])
        if self.review_progress.isVisibleTo(self.review_status) != show_bar:
            self.review_progress.setVisible(show_bar)

    def _hide_status(self) -> None:
        if not self.review_status.isHidden():
            self.review_status.hide()

    def _render_inspector_hint(self) -> None:
        if self._report is not None or self._review_state == "reviewing":
            self.inspector_hint.hide()
            return
        configured = self._api_client.is_configured
        self.inspector_hint.setText(strings.MANUSCRIPT_HINT_START if configured else strings.MANUSCRIPT_HINT_UNCONFIGURED)
        self.inspector_hint.show()

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

    def _basis_holds(self, issue: sessions.LiveIssue) -> bool:
        """The issue's semantic basis still holds; checked again right before
        accept or adjust runs.

        This is about one issue, not about reusing its segment's cache: an
        accept elsewhere in the same segment makes the segment cache not
        reusable, but leaves the other issues there executable. The issue's
        own liveness (untouched by edits, spans still equal the text) is
        checked separately by ``state_of`` and ``span_live``.
        """
        if issue.basis_lost or not self._semantic_available():
            return False
        return any(issue in r.issues and sessions.semantic_part_valid(r) for r in self._session.results)

    def _can_accept(self, issue: sessions.LiveIssue | None) -> bool:
        return (issue is not None and issue.issue["suggestion"] is not None and self._basis_holds(issue)
                and self._issue_state(issue) in (sessions.STATE_PENDING, sessions.STATE_NEEDS_RECONFIRM)
                and sessions.span_live(issue.suggestion_span, self._payload["target"]))

    def _can_adjust(self, issue: sessions.LiveIssue | None) -> bool:
        return (issue is not None and issue.edit_span() is not None and self._basis_holds(issue)
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
                                      and sessions.decision_for(issue, self._decision_index()) is None)
        self.adjust_apply_button.setEnabled(free and self._can_adjust(issue))
        self.semantic_list.setEnabled(not composing)

    def _alternative_toggled(self, _checked: bool) -> None:
        self._render_issue(self._current_issue())

    def _select_issue(self, _row: int) -> None:
        if self._refreshing_lists:
            return
        issue = self._current_issue()
        if issue is not None:
            self._pending_issue_id = None  # the owner chose; do not override
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
        candidate = _copy_payload(self._payload)
        candidate.update(_copy_payload(value))
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
        if not self._basis_holds(issue):
            self.status_label.setText(strings.MANUSCRIPT_RECONFIRM_NEEDED)
            self._refresh_semantic()
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
            if issue is not None:
                self.status_label.setText(strings.MANUSCRIPT_RECONFIRM_NEEDED)
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
        records = self._records()
        row = self.segment_list.currentRow()
        if row < 0 and self._segment_cursor is not None:
            row = self._segment_cursor  # restored from progress.current on open
        self._refreshing_lists = True
        try:
            self._segment_rows, self._segment_rows_from = list(pairs), pairs
            self.segment_list.clear()
            for pair in pairs:
                self.segment_list.addItem(strings.MANUSCRIPT_SEGMENT_ITEM.format(
                    number=pair.index + 1, status=self._segment_row_status(pair, records),
                    source=pair.source.core_end - pair.source.core_start,
                    target=pair.target.core_end - pair.target.core_start))
            self.segment_list.setCurrentRow(row if row < len(pairs) else -1)
        finally:
            self._refreshing_lists = False
        pair = self._current_segment()
        if pair is not None and pair.index != self._segment_cursor:
            # A row clicked while the list still held the old text: now that
            # it lists this text, the row is the selected segment.
            self._segment_cursor = pair.index
            self._select_cores(pair)
        self._render_segment(pair)

    def _segment_row_status(self, pair, records: list | None) -> str:
        """The row's state, its finished mark and its choices, in words."""
        status = strings.MANUSCRIPT_SEGMENT_STATUS[self._segment_status(pair)]
        if pair.hard_cut:
            status += strings.MANUSCRIPT_SEGMENT_HARD_CUT
        if records is not None:
            record = records[pair.index]
            if pair.index in sr.parts_of(self._payload).progress["completed"]:
                status += strings.MANUSCRIPT_SEGMENT_FINISHED_MARK
            if record["result"] is not None and record["result"]["truncated"]:
                status += strings.MANUSCRIPT_SEGMENT_TRUNCATED_MARK.format(count=len(record["result"]["findings"]))
            if record["choices"]:
                status += strings.MANUSCRIPT_SEGMENT_CHOICES_MARK.format(count=len(record["choices"]))
                unconfirmed = sum(1 for choice in record["choices"] if not choice["confirmed"])
                if record["status"] == "pending":
                    status += strings.MANUSCRIPT_SEGMENT_WAITING_MARK
                elif unconfirmed:
                    status += strings.MANUSCRIPT_SEGMENT_RECONFIRM_MARK.format(count=unconfirmed)
        return status

    def _current_segment(self):
        """The selected row's pair, only while the rows list the current
        pairing. Until the list is rebuilt after an edit, a row's offsets and
        index belong to the old text: selecting, finishing or translating it
        would act on the wrong range."""
        pairs = self._cached_pairs()
        if pairs is None or pairs is not self._segment_rows_from:
            return None
        row = self.segment_list.currentRow()
        return self._segment_rows[row] if 0 <= row < len(self._segment_rows) else None

    def _render_segment(self, pair) -> None:
        records = self._records() if pair is not None else None
        record = records[pair.index] if records is not None and pair.index < len(records) else None
        if pair is None:
            self.segment_hint.clear()
            self.translate_segment_button.hide()
        else:
            status = self._segment_status(pair)
            hints = []
            if status in strings.MANUSCRIPT_SEGMENT_HINT:
                hints.append(strings.MANUSCRIPT_SEGMENT_HINT[status])
            if pair.hard_cut:
                hints.append(strings.MANUSCRIPT_SEGMENT_HINT["hard_cut"])
            hints.extend(self._segment_notes.get(self._segment_key(pair), []))
            if record is not None and status != "reviewed" and pair.index in sr.parts_of(self._payload).progress["completed"]:
                hints.append(strings.MANUSCRIPT_SEGMENT_HINT["finished_recheck"])
            if record is not None and record["result"] is not None and record["result"]["truncated"]:
                hints.append(strings.MANUSCRIPT_SEGMENT_TERMS_TRUNCATED.format(
                    number=pair.index + 1, count=len(record["result"]["findings"])))
            self.segment_hint.setText("\n".join(hints))
            self.translate_segment_button.setVisible(pair.status == STATUS_UNTRANSLATED)
            self.translate_segment_button.setEnabled(self._translate_ready())
        finished = record is not None and pair.index in sr.parts_of(self._payload).progress["completed"]
        self.finish_button.setText(strings.MANUSCRIPT_SEGMENT_UNFINISH if finished else strings.MANUSCRIPT_SEGMENT_FINISH)
        long = self._long_now()
        self.finish_button.setVisible(long)
        self.next_pending_button.setVisible(long)
        self._render_segment_choices(pair.index if record is not None else None)
        self._update_segment_actions()

    def _render_segment_choices(self, index: int | None) -> None:
        """The segment's choices, then history choices that can come back to it.

        SPEC "Segment choice": in a pending record (no result) every choice
        waits for the segment's check, which realigns it (Runtime step 7), even
        one still confirmed after an edit of the translation alone; the Web
        shows them all as waiting. Only an unconfirmed choice of a record with a
        result needs reconfirmation."""
        rows: list[tuple] = []
        self.segment_choices.clear()
        if index is not None:
            record = self._payload["segments"][index]
            pending = record["status"] == "pending"
            for choice in record["choices"]:
                rows.append(("choice", choice))
                template = (strings.MANUSCRIPT_SEGMENT_CHOICE_WAITING if pending
                            else strings.MANUSCRIPT_SEGMENT_CHOICE_ITEM if choice["confirmed"]
                            else strings.MANUSCRIPT_SEGMENT_CHOICE_UNCONFIRMED)
                self.segment_choices.addItem(template.format(text=choice["source_span"]["text"],
                                                             choice=self._describe_choice(choice)))
            history = sr.parts_of(self._payload).history
            for position, target in sr.restorable(self._payload):
                if target == index:
                    entry = history[position]
                    rows.append(("history", position))
                    self.segment_choices.addItem(strings.MANUSCRIPT_SEGMENT_CHOICE_HISTORY.format(
                        text=entry["source_span"]["text"], choice=self._describe_choice(entry)))
        self._segment_choice_rows = rows
        self.segment_choices.setVisible(bool(rows))
        self.segment_choice_action.setVisible(bool(rows))
        self.segment_choice_remove.setVisible(bool(rows))

    def _selected_segment_choice(self) -> tuple | None:
        row = self.segment_choices.currentRow()
        return self._segment_choice_rows[row] if 0 <= row < len(self._segment_choice_rows) else None

    def _reconfirmable(self, index: int, choice: dict) -> bool:
        """An unconfirmed choice can be confirmed again only against a checked
        result that still reports its occurrence and, for an official pair,
        its candidate (SPEC "Runtime" step 7)."""
        if choice["confirmed"] or self._term_basis is None:
            return False
        record = self._payload["segments"][index]
        span = choice["source_span"]
        finding = next((f for f in (record["result"] or {}).get("findings", ())
                        if f["source"] == [span["start"], span["end"]]), None)
        return finding is not None and (choice["choice"] == sr.NOT_A_TERM or choice["candidate"] in finding["candidates"])

    def _update_segment_actions(self) -> None:
        pair = self._current_segment()
        # Typing never waits for the pairing: until it is current, nothing is enabled.
        records = (self._records() if pair is not None and self._long_now() and self._cached_pairs() is not None
                   else None)
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        free = not composing and not self._translating()
        status = records[pair.index]["status"] if records is not None and pair.index < len(records) else "pending"
        self.finish_button.setEnabled(free and status in ("done", "stale"))
        self.next_pending_button.setEnabled(not composing)
        self._update_segment_choice_actions()

    def _update_segment_choice_actions(self, _row: int = -1) -> None:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        selected = self._selected_segment_choice()
        pair = self._current_segment()
        primary = False
        if selected is not None and pair is not None and not composing:
            primary = selected[0] == "history" or self._reconfirmable(pair.index, selected[1])
        self.segment_choice_action.setText(strings.MANUSCRIPT_SEGMENT_CHOICE_RESTORE if selected is not None
                                           and selected[0] == "history" else strings.MANUSCRIPT_TERM_CHOICE_RECONFIRM)
        self.segment_choice_action.setEnabled(primary and not self._translating())
        self.segment_choice_remove.setEnabled(selected is not None and selected[0] == "choice" and not composing
                                              and not self._translating())

    def _segment_choice_primary(self) -> None:
        """Confirm an unconfirmed choice again, or bring a history choice back."""
        selected, pair = self._selected_segment_choice(), self._current_segment()
        if selected is None or pair is None or not self._input_ready():
            return
        if selected[0] == "history":
            try:
                parts = sr.restore(self._payload, selected[1], pair.index)
            except (ValueError, WorkfileError):
                self.status_label.setText(strings.MANUSCRIPT_TERM_CHOICE_FAILED)
                return
            self._apply_parts_edit(parts, strings.MANUSCRIPT_SEGMENT_CHOICE_RESTORED)
            return
        choice = selected[1]
        if not self._reconfirmable(pair.index, choice):
            return
        span = choice["source_span"]
        candidate = choice["candidate"]["candidate_id"] if choice["choice"] == sr.OFFICIAL_PAIR else None
        try:
            parts = sr.choose(self._payload, pair.index, span["start"], span["end"], choice["choice"], candidate)
        except (ValueError, WorkfileError):
            self.status_label.setText(strings.MANUSCRIPT_TERM_CHOICE_FAILED)
            return
        self._apply_parts_edit(parts, strings.MANUSCRIPT_TERM_CHOICE_RECORDED)

    def _segment_choice_removed(self) -> None:
        selected, pair = self._selected_segment_choice(), self._current_segment()
        if selected is None or selected[0] != "choice" or pair is None or not self._input_ready():
            return
        span = selected[1]["source_span"]
        self._apply_parts_edit(sr.unchoose(self._payload, pair.index, span["start"], span["end"]),
                               strings.MANUSCRIPT_TERM_CHOICE_REMOVED)

    def _toggle_finished(self) -> None:
        pair = self._current_segment()
        if pair is None or not self._input_ready() or self._records() is None:
            return
        parts = sr.parts_of(self._payload)
        finished = pair.index in parts.progress["completed"]
        try:
            parts = sr.set_finished(parts, pair.index, not finished)
        except ValueError:
            return
        self._apply_parts_edit(parts, strings.MANUSCRIPT_SEGMENT_UNFINISHED if finished
                               else strings.MANUSCRIPT_SEGMENT_FINISHED)

    def _next_pending(self) -> None:
        """The next segment that still needs something: a check, a re-check, a
        retry or a choice to confirm again; wraps around."""
        if not self._input_ready():
            return
        self._show_track(TRACK_SEGMENTS)
        pairs, records = self._segment_rows, self._records()
        start = self.segment_list.currentRow()
        for offset in range(1, len(pairs) + 1):
            row = (start + offset) % len(pairs)
            pair = pairs[row]
            unconfirmed = records is not None and any(not c["confirmed"] for c in records[pair.index]["choices"])
            if self._segment_status(pair) not in ("reviewed", "saved") or unconfirmed:
                self.segment_list.setCurrentRow(row)
                return
        self.status_label.setText(strings.MANUSCRIPT_NO_PENDING_SEGMENT)

    def _select_segment(self, _row: int) -> None:
        if self._refreshing_lists:
            return
        pair = self._current_segment()
        if pair is not None:
            self._segment_cursor = pair.index
        self._render_segment(pair)
        self._update_tracks()
        if pair is not None:
            self._select_cores(pair)

    def _select_cores(self, pair) -> None:
        """Select the pair's source and target cores in the editors."""
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
        # Semantic review is an experimental server option, off by default.
        # Without it the track would only ever be empty; it appears once the
        # server offers it, or when the manuscript already carries its data.
        offered = self._capabilities is not None and bool(
            {semantic.CAPABILITY_SEMANTIC_REVIEW, semantic.CAPABILITY_TRANSLATION_CONTEXT} & self._capabilities)
        if self.precedents_check.isVisibleTo(self.more_details) != offered:
            self.precedents_check.setVisible(offered)
        show_semantic = self._semantic_available() or bool(self._issue_rows) or bool(self._decisions())
        if self.semantic_track_button.isVisibleTo(self) != show_semantic:
            self.semantic_track_button.setVisible(show_semantic)
        if not show_semantic and self.track_stack.currentIndex() == TRACK_SEMANTIC:
            self._show_track(TRACK_TERMS)
            return
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
    def document_path(self) -> Path | None:
        return self._path

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
        self._render_inspector_hint()
        self._render_choice(index if finding is not None else -1)
        if finding is None:
            self.finding_title.setText(
                strings.MANUSCRIPT_FINDING_PROMPT if total else
                strings.MANUSCRIPT_NO_FINDINGS if report is not None else self.report_label.text()
            )
            for label in (self.finding_verdict, self.finding_pair, self.finding_rule, self.finding_positions, self.candidates_label):
                label.clear()
            self.finding_verdict.hide()
            self.finding_pair.hide()
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
        self.finding_pair.setVisible(bool(self.finding_pair.text()))
        rule = strings.MANUSCRIPT_RULE_TERM_PAIR if finding["rule_id"] == "review.term_pair" else finding["rule_id"]
        self.finding_rule.setText(strings.MANUSCRIPT_RULE.format(rule=rule))
        positions = []
        for side, span in ((strings.INPUT_LABEL, source), (strings.RESULT_LABEL, target)):
            if isinstance(span, dict) and type(span.get("start")) is int and type(span.get("end")) is int:
                positions.append(strings.MANUSCRIPT_POSITION.format(side=side, start=span["start"] + 1, end=span["end"]))
        self.finding_positions.setText("\n".join(positions))
        pairs = [self._candidate_text(item) for item in finding["candidates"]]
        # Choices stay open on what is shown; the notes say what is not.
        notes = [strings.MANUSCRIPT_CANDIDATES_TRUNCATED] if finding["candidates_truncated"] else []
        ref = self._finding_record(index)
        record = self._payload["segments"][ref[0]] if ref is not None else None
        if record is not None and record["result"] is not None and record["result"]["truncated"]:
            notes.append(strings.MANUSCRIPT_SEGMENT_TERMS_TRUNCATED.format(
                number=ref[0] + 1, count=len(record["result"]["findings"])))
        self.candidates_label.setTextFormat(Qt.TextFormat.PlainText)
        self.candidates_label.setText("\n".join([strings.MANUSCRIPT_CANDIDATES, *pairs, *notes]))
        self.lookup_button.setVisible(bool(source.get("text")))
        reliable = self._target_span_reliable(target)
        self.manual_edit_label.setVisible(not reliable)
        self.replace_toggle.setChecked(False)
        self.replace_toggle.setVisible(reliable and finding["verdict"] != "verified_constraint")
        self.replacement_area.hide()

    @staticmethod
    def _candidate_text(item: dict) -> str:
        categories = {"core_term": strings.CATEGORY_LABEL_CORE_TERM,
                      "resonator": strings.CATEGORY_LABEL_RESONATOR,
                      "weapon": strings.CATEGORY_LABEL_WEAPON,
                      "echo": strings.CATEGORY_LABEL_ECHO,
                      "skill": strings.CATEGORY_LABEL_SKILL,
                      "sonata_effect": strings.CATEGORY_LABEL_SONATA_EFFECT,
                      "location": strings.CATEGORY_LABEL_LOCATION,
                      "item": strings.CATEGORY_LABEL_ITEM,
                      "speaker": strings.CATEGORY_LABEL_SPEAKER}
        return f'{item["zh"]} ↔ {item["en"]} · {categories.get(item["category"], item["category"])}'

    def _describe_choice(self, choice: dict) -> str:
        if choice["choice"] == sr.OFFICIAL_PAIR:
            return strings.MANUSCRIPT_CHOICE_OFFICIAL_SHORT.format(candidate=self._candidate_text(choice["candidate"]))
        return strings.MANUSCRIPT_CHOICE_NOT_TERM

    # -- long manuscripts: term choices ------------------------------------------
    def _finding_record(self, index: int) -> tuple[int, int, int] | None:
        """(record, start, end) of a listed long-manuscript finding."""
        if not 0 <= index < len(self._finding_refs):
            return None
        ref = self._finding_refs[index]
        return ref if ref[0] < len(self._payload.get("segments") or ()) else None

    def _choice_at(self, index: int) -> dict | None:
        ref = self._finding_record(index)
        if ref is None:
            return None
        record = self._payload["segments"][ref[0]]
        return next((c for c in record["choices"]
                     if (c["source_span"]["start"], c["source_span"]["end"]) == ref[1:]), None)

    def _render_choice(self, index: int) -> None:
        """The owner's choice at this finding: an official candidate or not a term."""
        report = self._report
        ref = self._finding_record(index)
        if ref is None or report is None or not report["findings"][index]["candidates"]:
            self.choice_area.hide()
            return
        finding = report["findings"][index]
        choice = self._choice_at(index)
        self.choice_combo.blockSignals(True)
        self.choice_combo.clear()
        for item in finding["candidates"]:
            self.choice_combo.addItem(self._candidate_text(item), item["candidate_id"])
        self.choice_combo.addItem(strings.MANUSCRIPT_CHOICE_NOT_TERM, "")
        selected = 0
        if choice is not None:
            ids = [item["candidate_id"] for item in finding["candidates"]]
            if choice["choice"] == sr.NOT_A_TERM:
                selected = len(ids)
            elif choice["candidate"]["candidate_id"] in ids:
                selected = ids.index(choice["candidate"]["candidate_id"])
        self.choice_combo.setCurrentIndex(selected)
        self.choice_combo.blockSignals(False)
        if self._term_basis is None:
            state = strings.MANUSCRIPT_TERM_CHOICE_WAIT
        elif choice is None:
            state = strings.MANUSCRIPT_TERM_CHOICE_NONE
        elif choice["confirmed"]:
            state = strings.MANUSCRIPT_TERM_CHOICE_CHOSEN.format(choice=self._describe_choice(choice))
        else:
            state = strings.MANUSCRIPT_TERM_CHOICE_UNCONFIRMED.format(choice=self._describe_choice(choice))
        self.choice_state.setText(state)
        self.choice_button.setText(strings.MANUSCRIPT_TERM_CHOICE_RECONFIRM if choice is not None
                                   and not choice["confirmed"] else strings.MANUSCRIPT_TERM_CHOICE_RECORD)
        self.choice_area.show()
        self._update_choice_actions()

    def _update_choice_actions(self) -> None:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        index = self.findings.currentRow()
        # Choices are made against a checked result: not before the session's first check.
        free = not composing and not self._translating() and self._term_basis is not None
        ref = self._finding_record(index)
        self.choice_combo.setEnabled(free and ref is not None)
        self.choice_button.setEnabled(free and ref is not None)
        self.unchoice_button.setEnabled(free and self._choice_at(index) is not None)

    def _apply_parts_edit(self, parts: sr.Parts, message: str) -> None:
        """A change of choices, history or finished marks as one undoable edit;
        the editors and their caret are left alone."""
        self._apply_edit(sr.with_parts(self._draft_snapshot(), parts), editors_already_updated=True)
        self.status_label.setText(message)
        if self._long_now():
            self._show_segment_term_report()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._refresh_segments()
        self._update_controls()

    def _choose_term(self) -> None:
        if not self._input_ready() or self._term_basis is None:
            return
        ref = self._finding_record(self.findings.currentRow())
        if ref is None:
            return
        candidate_id = self.choice_combo.currentData()
        choice = sr.OFFICIAL_PAIR if candidate_id else sr.NOT_A_TERM
        try:
            parts = sr.choose(self._payload, *ref, choice, candidate_id or None)
        except (ValueError, WorkfileError):
            self.status_label.setText(strings.MANUSCRIPT_TERM_CHOICE_FAILED)
            return
        self._apply_parts_edit(parts, strings.MANUSCRIPT_TERM_CHOICE_RECORDED)

    def _unchoose_term(self) -> None:
        if not self._input_ready():
            return
        ref = self._finding_record(self.findings.currentRow())
        if ref is None or self._choice_at(self.findings.currentRow()) is None:
            return
        self._apply_parts_edit(sr.unchoose(self._payload, *ref), strings.MANUSCRIPT_TERM_CHOICE_REMOVED)

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
        self._sync_translation()
        busy = self._busy()
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        # Long manuscripts are reviewed per segmentation-v1 pair, so any
        # non-empty pair of sides can start a review.
        valid = all(len(value) > 0 for value in (self._payload["source"], self._payload["target"]))
        self.review_button.setEnabled(bool(self._api_client.is_configured and valid and not busy and not composing))
        self.review_button.setVisible(not busy)
        progress = self._progress() if self._has_results() else None
        partial = self._semantic_state in ("paused", "quota") or bool(progress and progress[0] < progress[1])
        self.review_button.setText(strings.MANUSCRIPT_REVIEW_CONTINUE if partial else strings.MANUSCRIPT_REVIEW)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setText(strings.MANUSCRIPT_REVIEW_PAUSE if self._semantic_running() or self._translating()
                                   else strings.CANCEL_BUTTON)
        self.translate_button.setEnabled(self._translate_ready())
        self.translate_segment_button.setEnabled(self._translate_ready())
        self._render_translate_offer()
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
        self._update_choice_actions()
        self._update_segment_actions()
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
        if self._review_state == "saved" and self._report is not None:
            label = strings.MANUSCRIPT_SAVED_REPORT.format(count=len(self._report["findings"]))
        elif self._review_state == "current" and self._report is not None:
            report = self._report
            label = strings.MANUSCRIPT_CURRENT_REPORT.format(
                count=len(report["findings"]), not_evaluated=report["coverage"]["not_evaluated"],
                truncated=strings.STATUS_YES if report["truncated"] or any(f["candidates_truncated"] for f in report["findings"]) else strings.STATUS_NO)
            if any(self._finding_recheck):
                label += strings.MANUSCRIPT_REPORT_RECHECK.format(count=sum(self._finding_recheck))
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
        value = copy.deepcopy(value)
        # So are the long-mode records, choices and progress: held by reference.
        for key in _PART_KEYS:
            if key in self._payload:
                value[key] = self._payload[key]
        return value

    def _restore_draft(self, value: dict) -> None:
        value = _copy_payload(value)
        decisions = value.pop("semantic_decisions", [])
        for key in _PART_KEYS:
            self._payload.pop(key, None)
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
            parts = sr.parts_of(self._payload) if "segments" in self._payload else None
            for side, start, old_end, new_length in edits:
                self._session.apply_edit(side, start, old_end, new_length, exempt=exempt)
                if parts is not None:
                    # Records the edit hit become pending (their finished marks
                    # go); the rest shift. Normalized on the derived refresh.
                    parts = sr.apply_edit(parts, before[side], side, start, old_end, new_length)
            if parts is not None:
                self._put_parts(parts)
        if any(before[key] != after[key] for key in _REVIEW_KEYS):
            self._invalidate_report()
        if self._conflict_mode:
            self._refresh_conflicts()
        self._refresh_semantic()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._derived_timer.start()
        self._update_controls()

    def _editor_state(self) -> dict:
        return {"side": self._last_side, "source": self.source_edit.view_state(),
                "target": self.target_edit.view_state()}

    def _restore_editor_state(self, state: dict) -> None:
        editor = self.source_edit if state["side"] == "source" else self.target_edit
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        for side, widget in (("source", self.source_edit), ("target", self.target_edit)):
            widget.restore_view_state(state[side])

    def _push_history(self, stack: list, sessions_stack: list, editor_states: list, value: dict,
                      editor_state: dict | None = None) -> tuple[list, list, list]:
        return ((stack + [value])[-20:], (sessions_stack + [self._session.snapshot()])[-20:],
                (editor_states + [self._editor_state() if editor_state is None else editor_state])[-20:])

    def _apply_edit(self, value: dict, *, editors_already_updated: bool = False, edits: list | None = None,
                    exempt: sessions.LiveIssue | None = None) -> None:
        before = self._draft_snapshot()
        incoming = value
        value = {**before, **_copy_payload(value)}
        for key in _PART_KEYS:
            # A draft without long-mode parts has none (it left long mode).
            if key not in incoming:
                value.pop(key, None)
        if value == before:
            return
        editor_state = self._editor_state()
        if editors_already_updated:
            for side, *_range in edits or []:
                editor = self.source_edit if side == "source" else self.target_edit
                if editor.last_edit_view is not None:
                    editor_state[side] = editor.last_edit_view
        self._undo_history, self._undo_sessions, self._undo_editor_states = self._push_history(
            self._undo_history, self._undo_sessions, self._undo_editor_states, before, editor_state)
        self._redo_history.clear()
        self._redo_sessions.clear()
        self._redo_editor_states.clear()
        self._restore_draft(value)
        self._document_revision += 1
        if self.status_label.text() == strings.MANUSCRIPT_SAVE_DONE:
            self.status_label.setText(strings.MANUSCRIPT_UNSAVED)
        if not editors_already_updated:
            self._set_payload(self._payload)
        self._after_draft_change(before, value, edits, exempt)

    def _apply_endpoint_state(self) -> None:
        self._render_inspector_hint()
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
        self._quiet_row = self.findings.currentRow()
        self._report = None
        self._finding_refs, self._finding_recheck = [], []
        self.findings.clear()
        if previous in ("current", "stale", "saved"):
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
        # Another service: its basis is unknown until its first check.
        self._reset_term_session(self._segment_cursor)
        self.status_label.setText(strings.MANUSCRIPT_ENDPOINT_CHANGED)
        self._update_controls()

    def _reset_term_session(self, cursor: int | None) -> None:
        """A new document or service: no term basis yet, so nothing saved is current."""
        self._term_basis = None
        self._records_seen = None
        self._segment_cursor = cursor if isinstance(cursor, int) else None
        self.segment_list.setCurrentRow(-1)

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
            recheck = self._records_with_input_change([])
            value["alignments"] = []
            self.status_label.setText(strings.MANUSCRIPT_ALIGNMENTS_CLEARED + (
                strings.MANUSCRIPT_TERM_RECHECK_COUNT.format(count=recheck) if recheck else ""))
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
        recheck = self._records_with_input_change(None)
        value = self._draft_snapshot()
        value["alignments"] = None
        self._apply_edit(value, editors_already_updated=True)
        self.status_label.setText(strings.MANUSCRIPT_AUTO_ALIGN_RECHECK.format(count=recheck) if recheck
                                  else strings.MANUSCRIPT_AUTO_ALIGN_SET)
        self._update_controls()

    def _records_with_input_change(self, alignments: list | None) -> int:
        """How many segment term results a change to ``alignments`` sends back
        for a re-check: their request's alignment input would differ."""
        payload = {**self._payload, "alignments": alignments}
        return sum(1 for record in self._payload.get("segments") or () if record["result"] is not None
                   and sr.alignment_input(alignments, payload["source"], payload["target"],
                                          (record["source_start"], record["source_end"]),
                                          (record["target_start"], record["target_end"])) != record["alignment_input"])

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
        # Every review, short or long, applies the reuse rule first (one entry).
        self._rebuild_plan()
        self._refresh_semantic()
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
        # The resolutions this check sent: a short manuscript sends none, and
        # [] (not unknown) lets the next check compare with it, as on the Web.
        snapshot = {"source": source, "target": target, "direction": direction,
                    "alignments": alignments, "report": report, "resolutions": []}
        candidate = _copy_payload(self._payload)
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
        self._session_snapshots.append(snapshot)
        self._document_revision += 1
        self._show_term_report(report)
        self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)
        self._update_controls()
        return True

    def _show_term_report(self, report: dict, *, state: str = "current", refs: list | None = None,
                          recheck: list | None = None) -> None:
        """List a report's findings. ``refs`` and ``recheck`` belong to a long
        manuscript's findings: their records, and whether each awaits a re-check."""
        row = self.findings.currentRow()
        # Rebuilt from the records (after an edit, an action or a segment of a
        # run): the list keeps its place without locating anything, so the
        # caret and the status line stay as they are.
        quiet = refs is not None
        if quiet and row < 0:
            row = self._quiet_row
        self._report = report
        self._finding_refs = list(refs or [])
        self._finding_recheck = list(recheck or [False] * len(report["findings"]))
        self._set_review_state(state, request_id=report["request_id"])
        self.findings.blockSignals(quiet)
        self.findings.clear()
        verdicts = {"verified_constraint": strings.MANUSCRIPT_VERDICT_VERIFIED,
                    "confirmed_conflict": strings.MANUSCRIPT_VERDICT_CONFLICT,
                    "needs_review": strings.MANUSCRIPT_VERDICT_NEEDS_REVIEW,
                    "not_evaluated": strings.MANUSCRIPT_VERDICT_NOT_EVALUATED}
        for index, finding in enumerate(report["findings"]):
            source_span = finding.get("source_span") or {}
            target_span = finding.get("target_span") or {}
            target_text = target_span.get("text")
            verdict = verdicts[finding["verdict"]]
            second_line = f"{target_text} · {verdict}" if target_text else verdict
            choice = self._choice_at(index)
            if choice is not None:
                second_line += " · " + (strings.MANUSCRIPT_FINDING_CHOSEN if choice["confirmed"]
                                        else strings.MANUSCRIPT_FINDING_RECONFIRM)
            if self._finding_recheck[index]:
                second_line += " · " + strings.MANUSCRIPT_FINDING_RECHECK
            self.findings.addItem(f'{source_span.get("text", "")}\n{second_line}')
        if self.findings.count():
            self.findings.setCurrentRow(row if 0 <= row < self.findings.count() and (quiet or self._term_mode == "segments")
                                        else 0)
        self.findings.blockSignals(False)
        if quiet:
            self._render_finding(self.findings.currentRow())
            self._update_controls()
        elif not self.findings.count():
            self._render_finding(-1)

    def _show_segment_term_report(self) -> None:
        """Long manuscripts: one display report from every segment record whose
        result was computed for exactly the text now there. Before the
        session's first check it reads as saved; afterwards a result that is
        not current is listed as awaiting a re-check, never as current."""
        records = self._records()
        if records is None:
            return
        payload = self._payload
        source, target, basis = payload["source"], payload["target"], self._term_basis
        findings, refs, recheck = [], [], []
        evaluated = not_evaluated = shown = 0
        truncated = False
        for index, record in enumerate(records):
            state = self._term_state(record) if record["result"] is not None else "pending"
            if state == "pending" or not self._content_current(record):
                continue
            report = sr.expand_result(record, source, target)
            for finding in report["findings"]:
                findings.append({**finding, "source_span": _offset(finding["source_span"], record["source_start"]),
                                 "target_span": _offset(finding["target_span"], record["target_start"])})
                refs.append((index, finding["source_span"]["start"], finding["source_span"]["end"]))
                recheck.append(state == "stale")
            evaluated += report["coverage"]["evaluated"]
            not_evaluated += report["coverage"]["not_evaluated"]
            truncated = truncated or report["truncated"]
            shown += 1
        if not shown:
            return
        self._show_term_report({
            "request_id": None, "findings": findings,
            "coverage": {"evaluated": evaluated, "not_evaluated": not_evaluated, "rules": list(sr.COVERAGE_RULES)},
            "truncated": truncated,
        }, state="saved" if basis is None else "current", refs=refs, recheck=recheck)

    def _content_current(self, record: dict) -> bool:
        """The record's result describes the text now at its cores."""
        source, target = self._payload["source"], self._payload["target"]
        return (record["direction"] == self._payload["direction"]
                and sr.content_hash(source[record["source_start"]:record["source_end"]]) == record["source_hash"]
                and sr.content_hash(target[record["target_start"]:record["target_end"]]) == record["target_hash"])

    def _task_finished(self, task: asyncio.Task, generation: int) -> None:
        if generation != self._generation:
            return
        self._task = None
        if task.cancelled():
            self._set_review_state("cancelled")
        self._update_controls()

    def _cancel_review(self) -> None:
        if self._translating():
            run = self._translate_run
            self._stop_translation()
            self._translation_stopped(run, "paused")
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
        """The next pair that needs a term check or a semantic request.

        Long manuscripts: while the session has no term basis, the probe of
        ``segment_records.next_index`` (SPEC step 4) goes first. Otherwise
        pairs go in order."""
        source, target = self._payload["source"], self._payload["target"]
        pairs = self._current_pairs()
        skip = {i for i, pair in enumerate(pairs) if self._segment_key(pair) in attempted}
        term = None
        if self._term_mode == "segments" and self._records() is not None:
            term = sr.next_index(self._payload, pairs, self._term_basis, skip)
            if self._term_basis is None and term is not None:
                return pairs[term]
        basis = self._basis()
        semantic_index = next((i for i, pair in enumerate(pairs) if i not in skip and pair.status == STATUS_READY
                               and basis.needs(self._session.result_for(pair, source, target))), None)
        found = [i for i in (term, semantic_index) if i is not None]
        return pairs[min(found)] if found else None

    def _basis(self) -> sessions.Basis:
        """The current basis of the semantic reuse rule (``sessions.Basis``)."""
        return sessions.Basis(semantic_required=self._semantic_available())

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

    def _alignments_for(self, source_core: tuple[int, int], target_core: tuple[int, int]) -> list | None:
        """The owner's correspondence for one segment's term request
        (``segment_records.alignment_list``): automatic stays automatic, an
        explicit list, even empty, never becomes automatic."""
        return sr.alignment_list(self._payload["alignments"], self._payload["source"], self._payload["target"],
                                 source_core, target_core)

    def _context_for(self, pair) -> dict | None:
        if pair.index == 0:
            return None
        previous = self._current_pairs()[pair.index - 1]
        source, target = self._payload["source"], self._payload["target"]
        return semantic.context_before(source[previous.source.core_start:previous.source.core_end],
                                       target[previous.target.core_start:previous.target.core_end])

    async def _check_term(self, index: int, generation: int) -> ClientError | str | None:
        """One segment's term check (SPEC "Runtime" steps 4 to 7): the planned
        request, at most one fallback without resolutions when Core rejects
        them, and one more request when the kept choices must go out. Returns
        None when the record is current or holds the new result, ``"changed"``
        when the text moved on while the request was out, ``"not_storable"``,
        or the error."""
        request = sr.plan_request(self._payload, index, self._term_basis)
        fell_back = False

        def still_there() -> bool:
            # The owner may have edited while the refused request was out:
            # replan only for the same segment, with the same cores, in long mode.
            records = self._records() if self._term_mode == "segments" else None
            if records is None or not 0 <= index < len(records):
                return False
            record, payload = records[index], self._payload
            return (payload["direction"] == request.direction
                    and payload["source"][record["source_start"]:record["source_end"]] == request.source
                    and payload["target"][record["target_start"]:record["target_end"]] == request.target)
        for _ in range(3):
            try:
                report = await self._api_client.review_pair(
                    request.source, request.target, request.direction, alignments=request.alignments,
                    resolutions=request.resolutions or None, resolution_context=request.resolution_context)
            except ClientError as exc:
                if (exc.code == ERROR_INVALID_REQUEST and request.resolutions and not fell_back
                        and generation == self._semantic_generation):
                    fell_back = True
                    if not still_there():
                        return "changed"
                    request = sr.plan_request(self._payload, index, self._term_basis, allow_resolutions=False)
                    continue
                return exc
            if generation != self._semantic_generation:
                return "changed"
            applied = sr.apply_response(self._payload, request, report, self._term_basis)
            self._term_basis = applied.basis
            self._put_parts(applied.parts)
            self._document_revision += 1
            if applied.outcome == "discarded":
                return "changed"
            if applied.outcome == "not_storable":
                return "not_storable"
            if not applied.again:
                return None
            request = sr.plan_request(self._payload, index, self._term_basis)
        return None

    async def _run_segments(self, generation: int) -> None:
        """Serial, one request in flight. Long-manuscript term checks follow the
        segment record reuse rule (``segment_records``); semantic requests the
        semantic reuse rule (``sessions.Basis``). A failure changes only its
        segment, and the next run continues from it."""
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
            records = self._records() if self._term_mode == "segments" else None
            need_term = records is not None and (
                self._term_basis is None or sr.needs_check(records[pair.index], self._payload, self._term_basis))
            need_semantic = self._basis().needs(self._session.result_for(pair, source, target))
            self._session.pending = sessions.SegmentResult(
                sent_source=key[4], sent_target=key[5],
                source_core=(pair.source.core_start, pair.source.core_end),
                target_core=(pair.target.core_start, pair.target.core_end),
                hard_cut=pair.hard_cut, report=None, index=pair.index)
            before = self._context_for(pair)
            precedents = semantic.select_precedents(
                sessions.precedent_candidates(self._payload, self._current_pairs()), key[4], pair.source.core_start
            ) if self.precedents_check.isChecked() and need_semantic else []
            self._render_semantic_progress()
            report = None
            error: ClientError | str | None = None
            if need_term:
                error = await self._check_term(pair.index, generation)
                if generation != self._semantic_generation:
                    return
                if error == "changed":
                    error = None  # the edited segment is a new segment; it gets its own turn
                if error is None:
                    self._segment_failures.pop(key, None)
                    self._show_segment_term_report()
            try:
                if error is None and need_semantic and generation == self._semantic_generation:
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
                if report is not None:
                    self._store_semantic(target_pair, pending, report, direction)
            if error is not None:
                code = error if isinstance(error, str) else error.code
                if code == ERROR_CANCELLED:
                    return
                if code in _QUOTA_CODES:
                    self._semantic_state = "quota"
                    return
                failure_streak = (code, failure_streak[1] + 1 if failure_streak[0] == code else 1)
                if target_pair is not None:
                    self._segment_failures[key] = "failed"
                if code in _FATAL_CODES or failure_streak[1] >= _REPEATED_FAILURES:
                    self._semantic_state = "error"
                    self._semantic_detail = (strings.MANUSCRIPT_TERM_NOT_STORABLE if code == "not_storable"
                                             else error.message)
                    return
                self._refresh_semantic()
                if self.track_stack.currentIndex() == TRACK_SEGMENTS:
                    self._refresh_segments()
                continue
            if report is not None and report["status"] != "complete":
                if target_pair is not None:
                    self._segment_failures[key] = "not_reviewed"
                continue
            if self.track_stack.currentIndex() == TRACK_SEGMENTS:
                self._refresh_segments()
            failure_streak = (None, 0)
        if generation == self._semantic_generation:
            self._semantic_state = "done"
            self._rebuild_plan()
            if self._term_mode == "segments":
                self._show_segment_term_report()

    def _store_semantic(self, pair, pending: sessions.SegmentResult, report: dict, direction: str) -> None:
        """Store a semantic report for ``pair``; an incomplete one is not stored."""
        source, target = self._payload["source"], self._payload["target"]
        if report["status"] != "complete":
            return
        result = self._session.result_for(pair, source, target)
        if result is None:
            result = sessions.SegmentResult(
                sent_source=pending.sent_source, sent_target=pending.sent_target,
                source_core=pending.source_core, target_core=pending.target_core,
                hard_cut=pending.hard_cut, report=None, index=pair.index)
            self._session.add(result)
        key = self._segment_key(pair)
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
        if not self._basis().needs(result):
            self._segment_failures.pop(key, None)
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
        candidate = _copy_payload(self._payload)
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
        """Hard stop: nothing is written and nothing is offered afterwards."""
        self._translate_generation += 1
        task, self._translate_task = self._translate_task, None
        self._translate_run, self._translate_state = None, "idle"
        if task is not None and not task.done():
            task.cancel()

    def _resumable(self) -> _Translation | None:
        """The stopped run, while the text is exactly what it left."""
        run = self._translate_run
        if run is None or self._translate_state not in ("paused", "failed") or self._translating():
            return None
        payload = self._payload
        same = (payload["source"] == run.source and payload["direction"] == run.direction
                and payload["target"] == run.target)
        return run if same else None

    def _sync_translation(self) -> None:
        """An edit, an undo or a redo withdraws the offer to continue."""
        if self._translate_state in ("paused", "failed") and self._resumable() is None:
            self._translate_run, self._translate_state = None, "idle"
            if self.status_label.text() == self._translate_status:
                self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)

    def _render_translate_offer(self) -> None:
        """Both translate buttons say what the next press does."""
        offer = {"paused": strings.MANUSCRIPT_TRANSLATE_CONTINUE,
                 "failed": strings.MANUSCRIPT_TRANSLATE_RETRY}.get(self._translate_state)
        button = self.translate_button
        if button.text() != (offer or ""):
            button.setText(offer or "")
            if offer:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)  # Qt's QWIDGETSIZE_MAX: the icon button grows to fit its label
            else:
                button.setFixedWidth(34)
        button.setToolTip(f"{offer or strings.MANUSCRIPT_TRANSLATE}（Ctrl+Shift+T）")
        button.setAccessibleName(offer or strings.MANUSCRIPT_TRANSLATE)
        self.translate_segment_button.setText(offer or strings.MANUSCRIPT_TRANSLATE_SEGMENT)

    def _translate(self) -> None:
        """Translate a blank translation or one untranslated segment; never
        overwrite text the owner already has. After a pause or a failure the
        same press continues from the first unfinished piece."""
        if not self._input_ready() or not self._translate_ready():
            return
        source, target, direction = self._payload["source"], self._payload["target"], self._payload["direction"]
        cursor = self._resumable()
        pair = None
        if cursor is not None and cursor.segment is not None:
            pair = next((p for p in self._current_pairs()
                         if p.status == STATUS_UNTRANSLATED and p.source.core_start == cursor.segment), None)
            if pair is None:
                cursor = None
        if cursor is None and not _blank(target):
            pair = self._current_segment() if self.track_stack.currentIndex() == TRACK_SEGMENTS else None
            if pair is None or pair.status != STATUS_UNTRANSLATED:
                self.status_label.setText(strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK)
                return
        retry = cursor is not None and self._translate_state == "failed"
        self._stop_translation()
        # A new record, so the ended task's callback never sees this run's identity.
        # A single-segment run carries its unwritten pieces (``out``) along.
        fresh = {"generation": self._translate_generation, "retry": retry,
                 "out": [] if cursor is None else list(cursor.out)}
        run = replace(cursor, **fresh) if cursor is not None else _Translation(
            source=source, direction=direction, target=target,
            segment=None if pair is None else pair.source.core_start, **fresh)
        text = source if pair is None else source[pair.source.core_start:pair.source.core_end]
        pieces = segment_text(text, SEGMENT_LIMIT)
        run.count = sum(1 for piece in pieces if piece.core_end > piece.core_start)
        self._translate_run, self._translate_state = run, "retrying" if retry else "waiting"
        self._translate_task = asyncio.ensure_future(self._run_translation(run, pair, text, pieces))
        self._translate_task.add_done_callback(lambda _task: self._translate_done(run))
        self._update_controls()

    def _translate_done(self, run: _Translation) -> None:
        """A task that ended without an outcome (an unexpected error) is a failure."""
        if run.generation == self._translate_generation and self._translate_state in ("waiting", "retrying"):
            self._translation_stopped(run, "failed", strings.ERROR_MSG_UNKNOWN)
        self._update_controls()

    async def _run_translation(self, run: _Translation, pair, text: str, pieces: list) -> None:
        generation = run.generation
        base = 0 if pair is None else pair.source.core_start
        try:
            # Capabilities are part of the basis: probe again, never reuse.
            self._capabilities = await self._api_client.get_capabilities()
            context_on = semantic.CAPABILITY_TRANSLATION_CONTEXT in self._capabilities
            candidates = (sessions.precedent_candidates(self._payload, self._current_pairs())
                          if context_on and self.precedents_check.isChecked() else [])
            template = strings.MANUSCRIPT_TRANSLATE_RETRYING if run.retry else strings.MANUSCRIPT_TRANSLATING
            for position in range(run.next_piece, len(pieces)):
                piece = pieces[position]
                core = text[piece.core_start:piece.core_end]
                if not core:
                    run.out.append(text[piece.start:piece.end])
                    run.next_piece = position + 1
                    continue
                self.status_label.setText(template.format(index=run.done + 1, total=run.count))
                context = None
                if context_on:
                    before = semantic.context_before(*run.previous) if run.previous is not None else None
                    precedents = semantic.select_precedents(candidates, core, base + piece.core_start)
                    context = {**({"before": before} if before is not None else {}),
                               **({"precedents": precedents} if precedents else {})} or None
                result = await self._api_client.translate(core, to=run.direction, context=context)
                if generation != self._translate_generation:
                    return
                run.out.append(text[piece.start:piece.core_start] + result.text + text[piece.core_end:piece.end])
                run.next_piece, run.previous = position + 1, (core, result.text)
                run.done += 1
        except ClientError as exc:
            if generation == self._translate_generation:
                if exc.code == ERROR_CANCELLED:
                    self._translation_stopped(run, "paused")
                else:
                    self._translation_stopped(run, "paused" if exc.code in _QUOTA_CODES else "failed",
                                              exc.message, quota=exc.code in _QUOTA_CODES)
            return
        if generation != self._translate_generation:
            return
        self._translate_task = None
        if not self._translation_basis_unchanged(run):
            self._translate_run, self._translate_state = None, "idle"
            self.status_label.setText(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            self._update_controls()
            return
        if not self._write_translation(run, "".join(run.out), pair):
            self._translate_run, self._translate_state = None, "idle"
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            self._update_controls()
            return
        self._translate_run, self._translate_state = None, "idle"
        self.status_label.setText(strings.MANUSCRIPT_TRANSLATED)
        self._update_controls()

    def _translation_basis_unchanged(self, run: _Translation) -> bool:
        payload = self._payload
        return (payload["source"] == run.source and payload["direction"] == run.direction
                and payload["target"] == run.target)

    def _write_translation(self, run: _Translation, added: str, pair=None) -> bool:
        """Write finished pieces as one undoable edit; False if the editor
        cannot show the result. Without ``pair`` they go to the whole target:
        replacing the blank one, or appended to the part already written."""
        target = run.target
        if pair is None:
            changed = (target if run.written else "") + added
            edit = ("target", len(target) if run.written else 0, len(target), len(added))
        else:
            position = pair.target.core_start
            left = "\n\n" if position > 0 and target[position - 1] not in WHITESPACE else ""
            right = "\n\n" if position < len(target) and target[position] not in WHITESPACE else ""
            inserted = left + added + right
            changed = target[:position] + inserted + target[position:]
            edit = ("target", position, position, len(inserted))
        if not self.target_edit.can_display(changed):
            return False
        value = self._draft_snapshot()
        value["target"] = changed
        if value["alignments"] is not None:
            value["alignments"] = []
        self._apply_edit(value, edits=[edit])
        return True

    def _translation_stopped(self, run: _Translation, state: str, reason: str = "", *, quota: bool = False) -> None:
        """The run ended before its last piece: the owner paused it, the
        service asked to wait, or a piece failed. The run stays as the cursor
        for the next press. A whole-manuscript run writes what is finished as
        one edit. A single-segment run keeps its finished pieces unwritten and
        writes them all when the segment completes: writing part of it would
        make the segment no longer untranslated, so the resume could not find
        it again."""
        self._translate_task = None
        if not self._translation_basis_unchanged(run):
            self._translate_run, self._translate_state = None, "idle"
            self.status_label.setText(strings.MANUSCRIPT_TRANSLATE_CHANGED)
            self._update_controls()
            return
        segment = run.segment is not None
        if not segment:
            kept = "".join(run.out)
            if kept:
                if not self._write_translation(run, kept):
                    self._translate_run, self._translate_state = None, "idle"
                    self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
                    self._update_controls()
                    return
                run.target, run.written = self._payload["target"], True
            run.out = []
        if state == "failed":
            text = (strings.MANUSCRIPT_TRANSLATE_SEGMENT_FAILED if segment else strings.MANUSCRIPT_TRANSLATE_FAILED).format(
                index=run.done + 1, total=run.count, done=run.done, reason=reason)
        else:
            if segment:
                template = strings.MANUSCRIPT_TRANSLATE_SEGMENT_QUOTA if quota else strings.MANUSCRIPT_TRANSLATE_SEGMENT_PAUSED
            else:
                template = strings.MANUSCRIPT_TRANSLATE_QUOTA if quota else strings.MANUSCRIPT_TRANSLATE_PAUSED
            text = template.format(done=run.done, total=run.count)
        self._translate_run, self._translate_state, self._translate_status = run, state, text
        self.status_label.setText(text)
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
        editor_state = (self._undo_editor_states if undo else self._redo_editor_states).pop()
        before = self._draft_snapshot()
        if undo:
            self._redo_history, self._redo_sessions, self._redo_editor_states = self._push_history(
                self._redo_history, self._redo_sessions, self._redo_editor_states, before)
        else:
            self._undo_history, self._undo_sessions, self._undo_editor_states = self._push_history(
                self._undo_history, self._undo_sessions, self._undo_editor_states, before)
        later = sr.parts_of(self._payload)
        self._restore_draft(value)
        if "segments" in self._payload:
            # Checks made after the snapshot stay where the content is the same.
            self._put_parts(sr.carry_results(sr.parts_of(self._payload), later))
        self._set_payload(self._payload)
        self._session.restore(saved)
        # Positions came back with the snapshot; only the review state follows.
        self._after_draft_change(before, value, edits=[])
        self.status_label.setText(strings.MANUSCRIPT_UNDONE if undo else strings.MANUSCRIPT_REDONE)
        self._update_controls()
        self._restore_editor_state(editor_state)

    def _set_payload(self, value: dict) -> None:
        self._updating = True
        try:
            self.source_edit.set_raw_text(value["source"])
            self.target_edit.set_raw_text(value["target"])
            self.direction_combo.setCurrentIndex(self.direction_combo.findData(value["direction"]))
            self._payload = _normalize_semantic(_copy_payload(value))
            self._document_revision += 1
        finally:
            self._updating = False

    def _refresh_conflicts(self) -> None:
        history = sr.parts_of(self._payload).history if self._history_removal else []
        if len(history) <= MAX_SEGMENT_CHOICE_HISTORY:
            self._history_removal, history = False, []
        if history:
            # The same list, for a save refused because the choice history is
            # over its cap: the owner removes entries until it fits.
            self._conflict_indexes = list(range(len(history)))
            self.conflict_label.setText(strings.MANUSCRIPT_HISTORY_FULL.format(
                count=len(history), limit=MAX_SEGMENT_CHOICE_HISTORY))
            self.remove_choice_button.setText(strings.MANUSCRIPT_REMOVE_HISTORY_CHOICE)
            self.conflict_list.clear()
            for number, entry in enumerate(history, 1):
                self.conflict_list.addItem(strings.MANUSCRIPT_HISTORY_ITEM.format(
                    number=number, text=entry["source_span"]["text"], choice=self._describe_choice(entry)))
            for widget in (self.conflict_label, self.conflict_list, self.remove_choice_button):
                widget.setVisible(True)
            self._inspector_requested = True
            self.inspector.show()
            self._update_inspector_button()
            self._update_controls()
            return
        self.conflict_label.setText(strings.MANUSCRIPT_CHOICES_CONFLICT)
        self.remove_choice_button.setText(strings.MANUSCRIPT_REMOVE_CHOICE)
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
        if self._history_removal:
            parts = sr.parts_of(self._payload)
            history = [entry for number, entry in enumerate(parts.history) if number != row]
            self._apply_parts_edit(parts._replace(history=history), strings.MANUSCRIPT_HISTORY_CHOICE_REMOVED)
            self._refresh_conflicts()
            self.conflict_list.setCurrentRow(min(row, self.conflict_list.count() - 1))
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
        # Records follow the text being saved; the selected segment becomes
        # progress.current only now, so navigating alone never looked edited.
        self._records()
        payload = self._payload
        parts = sr.parts_of(payload)
        cursor = self._segment_cursor
        if parts.segments and cursor is not None and 0 <= cursor < len(parts.segments):
            payload = sr.with_parts(payload, sr.set_current(parts, cursor))
        try:
            save_workfile(path, payload)
        except WorkfileError as exc:
            if exc.code == "workfile_duplicate_choices":
                self._conflict_mode = True
                self._refresh_conflicts()
                self.status_label.setText(strings.MANUSCRIPT_CHOICES_CONFLICT)
            else:
                self.status_label.setText(strings.MANUSCRIPT_SAVE_FAILED.format(reason=str(exc)))
                if len(parts.history) > MAX_SEGMENT_CHOICE_HISTORY:
                    self._history_removal = True
                    self._refresh_conflicts()
            return False
        except OSError as exc:
            self.status_label.setText(strings.MANUSCRIPT_SAVE_FAILED.format(reason=str(exc)))
            return False
        if payload is not self._payload:
            self._put_parts(sr.parts_of(payload))
        # Hold exactly what was written: the writer may have dropped history to
        # fit 1 MiB (possibly falling back to v1) and unneeded old texts.
        written = parse_workfile(serialize_workfile(self._payload))
        for key in ("semantic_decisions", "semantic_texts", "semantic_history"):
            if key in written:
                self._payload[key] = written[key]
            else:
                self._payload.pop(key, None)
        self._path = path
        self._saved_payload = _copy_payload(self._payload)
        self.status_label.setText(strings.MANUSCRIPT_SAVE_DONE)
        self._update_controls()
        self.documentSaved.emit()
        return True

    def open_document(self, _checked: bool = False) -> None:
        if not self._input_ready():
            return
        name, _ = QFileDialog.getOpenFileName(self, strings.MANUSCRIPT_OPEN, "", strings.MANUSCRIPT_FILE_FILTER)
        if name:
            self.open_path(name)

    def open_path(self, name: str | Path) -> bool:
        """The one way a workfile is opened: dialog, drop and recent list."""
        if not self._input_ready():
            return False
        name = str(name)
        def read_candidate() -> dict:
            value = load_workfile(name)
            if not self.source_edit.can_display(value["source"]) or not self.target_edit.can_display(value["target"]):
                raise ValueError(strings.MANUSCRIPT_UNSUPPORTED_TEXT)
            return value
        if self._is_report_file(Path(name)):
            self.status_label.setText(strings.MANUSCRIPT_REPORT_NOT_MANUSCRIPT)
            return False
        try:
            read_candidate()
        except (OSError, WorkfileError, ValueError) as exc:
            self.status_label.setText(strings.MANUSCRIPT_OPEN_FAILED.format(reason=str(exc)))
            return False
        if not self.confirm_discard():
            return False
        # Saving the dirty draft may have overwritten the selected file. Read
        # again so the restored draft and the saved baseline reflect disk.
        try:
            value = read_candidate()
        except (OSError, WorkfileError, ValueError) as exc:
            self.status_label.setText(strings.MANUSCRIPT_OPEN_FAILED.format(reason=str(exc)))
            return False
        self._replace_document(value, (value.get("progress") or {}).get("current"))
        self._known_texts = sessions.verified_texts(value.get("semantic_texts") or [])
        # The loaded texts are the versions on-file decisions usually refer to;
        # keep them so an edit before the next review does not lose them.
        for text in (value["source"], value["target"]):
            self._known_texts.setdefault(sessions.revision(text), text)
        self._path = Path(name)
        # A file this client wrote normalizes to itself; anything else is
        # mapped now, before the baseline, so opening never looks edited.
        self._records()
        self._saved_payload = _copy_payload(self._payload)
        self._set_review_state("history" if value["history"] else "none")
        # Saved segment results show as saved: nothing is sent until a review.
        self._show_segment_term_report()
        self.status_label.setText(strings.MANUSCRIPT_OPENED)
        self._update_controls()
        self.documentOpened.emit(name)
        return True

    def new_document(self, _checked: bool = False) -> bool:
        if not self.confirm_discard():
            return False
        value = empty_workfile()
        self._replace_document(value, None)
        self._known_texts = {}
        self._saved_payload = _copy_payload(value)
        self._path = None
        self._set_review_state("none")
        self._update_controls()
        return True

    def _replace_document(self, value: dict, cursor: int | None) -> None:
        """Leave the open manuscript and hold ``value`` in its place: the one
        way opening and New replace the whole document."""
        self.documentLeaving.emit()
        self._pending_issue_id = None
        self._restore_serial += 1
        self._invalidate_report()
        self._stop_translation()
        self._clear_semantic()
        self._undo_history.clear()
        self._redo_history.clear()
        self._undo_sessions.clear()
        self._redo_sessions.clear()
        self._undo_editor_states.clear()
        self._redo_editor_states.clear()
        self._conflict_mode = self._history_removal = False
        self._refresh_conflicts()
        self._set_payload(value)
        self._reset_term_session(cursor)
        self._session_snapshots = []
        # Clearing the runs above listed the outgoing manuscript's segments,
        # and an open caches the new pairing, so no refresh would follow: a
        # stale row selected its old offsets in the new text and marked its
        # index. Rebuilt after the cursor reset, so progress.current is the
        # row selected, never the old one.
        self._refresh_segments()

    # -- report export ----------------------------------------------------------
    def _report_input(self) -> dict:
        """What the Markdown report shows, with the Web export's meanings."""
        payload = self._payload
        source, target, direction = payload["source"], payload["target"], payload["direction"]

        def snapshot(entry: dict | None) -> dict | None:
            if entry is None:
                return None
            return {**entry, "resolutions": entry.get("resolutions"),
                    "trusted": any(entry == own for own in self._session_snapshots)}
        # A long manuscript's result is its segment reports (the Web export's
        # rule): a document report left from an earlier, shorter text is not
        # exported, and the result is current only when every segment is
        # ready and every record current.
        long = self._long_now()
        history = [] if long else payload["history"]
        latest = snapshot(history[-1] if history else None)
        previous = snapshot(history[-2] if len(history) > 1 else None)
        # The Web's rule: checked in this session, for exactly this text, and
        # the workbench still holds it as the current report (an endpoint
        # change or a failed re-review ends that, though the text is the same).
        current = bool(latest and latest["trusted"] and self._review_state == "current"
                       and latest["source"] == source and latest["target"] == target
                       and latest["direction"] == direction and latest["alignments"] == payload["alignments"])
        currency = "none" if latest is None else "current" if current else "stale" if latest["trusted"] else "imported"
        draft = {"source": source, "target": target, "direction": direction, "alignments": payload["alignments"]}
        segment_terms = None
        records = self._records()
        if records is not None:
            segment_terms = []
            for index, record in enumerate(records):
                state = self._term_state(record)
                if record["result"] is None and not record["choices"]:
                    continue
                findings = []
                if record["result"] is not None:
                    for finding in sr.expand_result(record, source, target)["findings"]:
                        findings.append({"verdict": finding["verdict"],
                                         "source_span": _offset(finding["source_span"], record["source_start"]),
                                         "target_span": _offset(finding["target_span"], record["target_start"]),
                                         "candidates": [{"zh": c["zh"], "en": c["en"]} for c in finding["candidates"]],
                                         "candidates_truncated": finding["candidates_truncated"] is True})
                choices = [{**choice, "source_span": _offset(choice["source_span"], record["source_start"])}
                           for choice in record["choices"]]
                # Truncation goes with the row, so an export never reads complete
                # when findings or candidates were left out.
                truncated = record["result"] is not None and record["result"]["truncated"] is True
                segment_terms.append({"index": index, "state": state, "stale": state != "current",
                                      "truncated": truncated, "findings": findings, "choices": choices})
        semantic_part = None
        issues = self._session.issues()
        if issues or self._decisions():
            statuses = {"needs_reconfirm": "needs_reconfirmation", "ignored_carried": "ignored"}
            items = []
            for issue in issues:
                raw = issue.issue
                state = self._issue_state(issue)
                items.append({"issue_id": raw["id"], "category": raw["category"], "confidence": raw["confidence"],
                              "segment_index": raw.get("segment_index"), "source_excerpt": raw.get("source_excerpt"),
                              "target_excerpt": raw.get("target_excerpt"), "rationale": raw["rationale"],
                              "suggestion": raw["suggestion"]["replacement"] if raw.get("suggestion") else None,
                              "suggestion_withheld": raw.get("suggestion_withheld"),
                              "status": statuses.get(state, state)})
            ready = [pair for pair in self._current_pairs() if pair.status == STATUS_READY]
            reviewed = sum(1 for pair in ready if (result := self._session.result_for(pair, source, target)) is not None
                           and result.report is not None)
            semantic_part = {"items": items, "resolved": sum(1 for item in items
                                                             if item["status"] in ("accepted", "adjusted", "ignored")),
                             "segments": {"total": len(ready), "reviewed": reviewed}}
        if long:
            records = records or []
            if not any(record["result"] is not None for record in records):
                currency = "none"
            elif (all(pair.status == STATUS_READY for pair in self._current_pairs())
                  and all(self._term_state(record) == "current" for record in records)):
                currency = "current"
            else:
                currency = "stale"
        return {
            "draft": draft, "latest": latest, "previous": previous, "currency": currency,
            "choices": payload["choices"],
            "reconciled": mdr.reconcile_choices(payload["choices"], draft, latest),
            "comparison": mdr.compare_reports(previous, latest) if latest and previous else None,
            "semantic": semantic_part, "segment_terms": segment_terms,
            "segment_choice_history": len(payload.get("segment_choice_history") or []),
        }

    def report_markdown(self) -> str:
        return mdr.render(self._report_input())

    def export_report(self, _checked: bool = False) -> bool:
        """Write the Markdown report: a read-only snapshot, not a manuscript.
        It sends nothing and leaves the path, the saved state and the recent
        list alone; the open dialog never offers it as a manuscript."""
        if not self._input_ready():
            return False
        name, _ = QFileDialog.getSaveFileName(self, strings.MANUSCRIPT_EXPORT_REPORT, "wuwaterm-report.md",
                                              strings.MANUSCRIPT_REPORT_FILTER)
        if not name:
            return False
        destination = Path(name)
        if self._path is not None and destination.resolve() == self._path.resolve():
            # A report is not a manuscript: never replace the open one with it.
            self.status_label.setText(strings.MANUSCRIPT_EXPORT_OVER_MANUSCRIPT)
            return False
        content = self.report_markdown().encode("utf-8")
        try:
            fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(content)
                os.replace(temporary, destination)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
        except OSError as exc:
            self.status_label.setText(strings.MANUSCRIPT_EXPORT_FAILED.format(reason=str(exc)))
            return False
        self.status_label.setText(strings.MANUSCRIPT_EXPORT_DONE)
        return True

    # -- file continuity: import, clipboard, work position -------------------
    def _guard_text_inputs(self) -> None:
        """Inspector text fields take a dropped file as a file, not as typed text.

        QLineEdit and the adjust box accept drops by default and would type
        the file's path into a field that can later be written into the draft.
        """
        for widget in (*self.findChildren(QLineEdit), self.adjust_edit):
            widget.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            files = LosslessEditor.local_files(event.mimeData())
            if files:
                event.acceptProposedAction()
                if event.type() == QEvent.Type.Drop:
                    self._files_dropped(files, "source")
                return True
        return super().eventFilter(watched, event)

    def _files_dropped(self, paths: list[str], side: str) -> None:
        QTimer.singleShot(0, lambda: self.import_files(paths, side))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if LosslessEditor.local_files(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if LosslessEditor.local_files(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        files = LosslessEditor.local_files(event.mimeData())
        if not files:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        self._files_dropped(files, "source")

    def import_files(self, paths: list[str], side: str = "source") -> bool:
        """What dropped files mean: .json opens, .txt/.md replace one side."""
        if not self._input_ready():
            return False
        if len(paths) != 1:
            self.status_label.setText(strings.IMPORT_ONE_FILE_ONLY)
            return False
        path = Path(paths[0])
        suffix = path.suffix.lower()
        if suffix == ".json":
            return bool(self.open_path(path))
        if suffix in _TEXT_SUFFIXES:
            text = self._read_import_text(path)
            return text is not None and self._replace_side(side, text)
        self.status_label.setText(strings.IMPORT_UNSUPPORTED_TYPE)
        return False

    def _read_import_text(self, path: Path) -> str | None:
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_IMPORT_BYTES + 1)
        except OSError as exc:
            self.status_label.setText(strings.IMPORT_READ_FAILED.format(reason=exc.strerror or type(exc).__name__))
            return None
        if len(data) > MAX_IMPORT_BYTES:
            self.status_label.setText(strings.IMPORT_TOO_LARGE)
            return None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.status_label.setText(strings.IMPORT_NOT_UTF8)
            return None
        if mdr.is_report_export(text):
            self.status_label.setText(strings.MANUSCRIPT_REPORT_NOT_MANUSCRIPT)
            return None
        return text

    @staticmethod
    def _is_report_file(path: Path) -> bool:
        """An exported report, by the Web's rule (its first line); unreadable
        files are left to the manuscript reader's own message."""
        try:
            with path.open("rb") as handle:
                head = handle.read(4096)
        except OSError:
            return False
        return mdr.is_report_export(head.decode("utf-8", errors="replace"))

    def _replace_side(self, side: str, text: str) -> bool:
        """Replace one whole side as a single undoable edit."""
        editor = self.target_edit if side == "target" else self.source_edit
        if not editor.can_display(text):
            self.status_label.setText(strings.MANUSCRIPT_UNSUPPORTED_TEXT)
            return False
        value = self._draft_snapshot()
        if value[side] == text:
            self.status_label.setText(strings.IMPORT_UNCHANGED)
            return False
        value[side] = text
        if value["alignments"] is not None:
            value["alignments"] = []
        self._apply_edit(value)
        self.status_label.setText(strings.IMPORT_REPLACED[side])
        return True

    @staticmethod
    def _clipboard_files() -> list[str]:
        """Local files on the clipboard, also when only spelled as file URLs.

        Copying a file in Explorer puts its URL on the clipboard, and the
        plain-text form of that is the full path. A path is never manuscript
        text: it would be saved, exported and sent with the next review.
        """
        mime = QApplication.clipboard().mimeData()
        files = LosslessEditor.local_files(mime)
        if files or mime is None or not mime.hasText():
            return files
        lines = [line.strip() for line in mime.text().splitlines() if line.strip()]
        if lines and all(line.lower().startswith("file:") for line in lines):
            return [QUrl(line).toLocalFile() or line for line in lines]
        return []

    def paste_as(self, side: str) -> bool:
        if not self._input_ready():
            return False
        files = self._clipboard_files()
        if files:
            # The same rules as dropping that file onto this side.
            return self.import_files(files, side)
        text = QApplication.clipboard().text()
        if not text:
            self.status_label.setText(strings.CLIPBOARD_EMPTY)
            return False
        return self._replace_side(side, text)

    def copy_target(self) -> bool:
        text = self.target_edit.raw_text()
        if not text:
            self.status_label.setText(strings.COPY_TARGET_EMPTY)
            return False
        QApplication.clipboard().setText(text)
        self.status_label.setText(strings.COPY_TARGET_DONE)
        return True

    def new_from_clipboard(self) -> bool:
        if not self._input_ready():
            return False
        files = self._clipboard_files()
        if files:
            if len(files) != 1 or Path(files[0]).suffix.lower() not in _TEXT_SUFFIXES:
                # A workfile opens as itself; anything else is refused there.
                return self.import_files(files, "source")
            text = self._read_import_text(Path(files[0]))
            if text is None:
                return False
        else:
            text = QApplication.clipboard().text()
        if not text:
            self.status_label.setText(strings.CLIPBOARD_EMPTY)
            return False
        # Checked before the old draft is given up, not after.
        if not self.source_edit.can_display(text):
            self.status_label.setText(strings.MANUSCRIPT_UNSUPPORTED_TEXT)
            return False
        if not self.new_document():
            return False
        return self._replace_side("source", text)

    def work_position(self) -> dict:
        """Where the owner was, as plain data for the recent list.

        Pure reads of widget state; called on save, leave and close, never
        while typing.
        """
        editor = self.source_edit if self._last_side == "source" else self.target_edit
        issue = self._current_issue()
        width = None
        if not self.inspector.isHidden():
            value = self.work_splitter.sizes()[1]
            if MIN_PANEL_WIDTH <= value <= MAX_PANEL_WIDTH:
                width = value
        return {
            "cursor": {"side": editor.side, "offset": editor.raw_cursor_offset()},
            "scroll": {"source": self.source_edit.verticalScrollBar().value(),
                       "target": self.target_edit.verticalScrollBar().value()},
            "selected_issue_id": self._issue_anchor(issue) if issue is not None else self._pending_issue_id,
            "panel_width": width,
        }

    def restore_work_position(self, entry: dict) -> None:
        """Apply a stored position to the open document; clamps if it changed."""
        cursor = entry.get("cursor")
        if isinstance(cursor, dict) and cursor.get("side") in ("source", "target") \
                and isinstance(cursor.get("offset"), int):
            editor = self.source_edit if cursor["side"] == "source" else self.target_edit
            editor.set_raw_cursor_offset(cursor["offset"])
            self._last_side = cursor["side"]
        width = entry.get("panel_width")
        if isinstance(width, int) and not isinstance(width, bool) and not self.inspector.isHidden():
            self.work_splitter.setSizes([max(1, sum(self.work_splitter.sizes()) - width), width])
        issue_id = entry.get("selected_issue_id")
        self._pending_issue_id = issue_id if isinstance(issue_id, str) else None
        scroll = entry.get("scroll")
        if isinstance(scroll, dict):
            # After layout: before it the scrollbars have no range to clamp to,
            # and setting the caret above scrolls to make it visible.
            self._restore_serial += 1
            serial = self._restore_serial
            QTimer.singleShot(0, lambda: self._restore_scroll(serial, scroll))

    def _restore_scroll(self, serial: int, scroll: dict) -> None:
        if serial != self._restore_serial:
            return
        for side, editor in (("source", self.source_edit), ("target", self.target_edit)):
            value = scroll.get(side)
            if isinstance(value, int) and not isinstance(value, bool):
                editor.verticalScrollBar().setValue(value)

    def _texts_tag(self) -> str:
        return sessions.revision(self._payload["source"] + "\0" + self._payload["target"])[:16]

    @staticmethod
    def _occurrence(issue: sessions.LiveIssue) -> str:
        def bounds(span: dict | None) -> str:
            return "-" if span is None else f'{span["start"]}-{span["end"]}'

        return "|".join((issue.issue["fingerprint"], bounds(issue.source_span), bounds(issue.target_span)))

    def _issue_anchor(self, issue: sessions.LiveIssue) -> str:
        """One occurrence of an issue: the texts it was measured in, its
        fingerprint and both spans in whole-text coordinates.

        The fingerprint alone repeats for identical paragraphs, so restoring
        by it selected the first copy rather than the one the owner was on.
        """
        return f"{self._texts_tag()}|{self._occurrence(issue)}"

    def _select_pending_issue(self) -> None:
        if self._pending_issue_id is None or self._current_issue() is not None or not self._issue_rows:
            return
        tag, _, occurrence = self._pending_issue_id.partition("|")
        if tag != self._texts_tag():
            # Other texts (or an older bare fingerprint): never guess.
            self._pending_issue_id = None
            return
        matches = [row for row, issue in enumerate(self._issue_rows) if self._occurrence(issue) == occurrence]
        # Only an exact, unique occurrence; otherwise nothing is selected.
        if len(matches) == 1:
            self._pending_issue_id = None
            self.semantic_list.setCurrentRow(matches[0])
