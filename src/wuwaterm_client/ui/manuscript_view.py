"""Local manuscript workbench backed by the shared review API.

A QTextDocument normalises several characters. LosslessEditor retains the
original text and maps each real edit through the document's UTF-16 offsets;
opening and saving without editing therefore does not rewrite a manuscript.

This is the Qt half of the workbench. The runs themselves — what is requested,
in what order, whether an answer still belongs to the run that asked, and how a
finished answer is committed — live in ``document_controller``, which imports
no Qt. The view is its host: it triggers commands, renders the state the
controller holds (through the properties below ``__init__``), and performs the
one write a run cannot do itself, the editor's own translation edit.
"""
from __future__ import annotations

import bisect
import os
import re
import tempfile
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPalette, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionButton, QStylePainter,
    QTextEdit, QVBoxLayout, QWidget,
)

from .. import markdown_report as mdr
from .. import segment_records as sr
from .. import semantic, strings
from .. import structure as l1
from .. import translation_memory as tm
from ..config import MAX_PANEL_WIDTH, MIN_PANEL_WIDTH
from .error_presentation import SEVERITY_INFO, SEVERITY_WARN
from .. import semantic_session as sessions
from ..document_controller import (
    DOCUMENT_REVIEW_LIMIT as _DOCUMENT_REVIEW_LIMIT, Translation as _Translation,
    DocumentRunController, WorkbenchHost, blank as _blank,
)
from ..document_edits import (
    NOTICE_ALL, NOTICE_PARTIAL, REASON_FAILED, REASON_FULL, REASON_NOT_APPLICABLE, REASON_RECONFIRM,
    REASON_REJECTED, REASON_STALE, AlignmentRemap, CommandOutcome, EditOutcome, copy_payload as _copy_payload,
    normalize_semantic as _normalize_semantic, remap_for_free_edit, remap_for_target_fill, remap_splice,
)
from ..manuscript import (
    MAX_SEGMENT_CHOICE_HISTORY, WorkfileError, choice_conflict_groups, empty_workfile, is_ambiguous_mention,
    load_workfile, parse_workfile, sanitize_duplicate_choices, save_workfile, serialize_workfile,
)
from ..recovery import RecoveryState, clear_recovery, load_recovery, save_recovery
from ..search_replace import SearchReplaceController
from ..segmentation import (
    SEGMENT_LIMIT, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY, STATUS_UNTRANSLATED, WHITESPACE,
)
from ..termbase import TermbaseStore
from . import review_states
from .components import Banner, ChevronComboBox
from .icons import icon
from .scroll_sync import ScrollSync
from .termbase_dialog import MyTermsPanel, TermbaseDialog
from .tm_dialog import TranslationMemoryDialog, refusal_text as tm_refusal_text
from .tm_panel import ActiveSegment, ReuseSummary, TranslationMemoryPanel, stale_labels

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
# The term-view status filters, in the order the combo lists them.
_TERM_FILTER_KEYS = ("unhandled", "all", "inconsistent", "pending", "not_evaluated", "confirmed")
_DEFAULT_TERM_FILTER = "unhandled"
# What a finding of a long manuscript still needs, by its record's state.
_RECHECK_STATES = ("stale", "pending", "failed", "not_reviewed", "needs_manual_split", "untranslated",
                   "unpaired_target")
# Pairing verdicts a translation in flight has not settled yet. While the target
# is still being written its paragraphs are incomplete by construction, so the
# counts cannot match and every pair reads as unalignable. That is not a finding
# about the manuscript: reporting it mid-run made the inspector flash
# "需要手动切分" for the length of every run and take it back at the end.
_TRANSLATION_PENDING_STATES = (STATUS_NEEDS_MANUAL_SPLIT,)


def _offset(span: dict | None, by: int) -> dict | None:
    return None if span is None else {"start": span["start"] + by, "end": span["end"] + by, "text": span["text"]}


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


_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")
# Whitespace, which the status bar's character counts leave out.
_WHITESPACE_RUN = re.compile(r"\s+")


def _astral_indexes(text: str) -> list[int]:
    """Scalar indexes of the characters UTF-16 stores as two units (a regex
    scan, so a long manuscript costs C time, not a Python loop)."""
    return [match.start() for match in _ASTRAL.finditer(text)]


def _splice_raw(prefix: str, inserted: str, suffix: str) -> str:
    """Join ``prefix + inserted + suffix`` without letting an adjacent bare CR
    and LF collapse into a single CRLF display line break."""
    if inserted:
        if prefix.endswith("\r") and inserted.startswith("\n"):
            inserted = "\n" + inserted
        if inserted.endswith("\r") and suffix.startswith("\n"):
            inserted = inserted + "\n"
    elif prefix.endswith("\r") and suffix.startswith("\n"):
        inserted = "\n"
    return prefix + inserted + suffix


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
    promoteRequested = Signal()

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
        self._rejected_view: dict | None = None
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

    # -- positions for scroll sync and navigation (cheap on long texts) ------
    def _astral(self) -> list[int]:
        cached = getattr(self, "_astral_cache", None)
        if cached is None or cached[0] is not self._shown:
            cached = (self._shown, _astral_indexes(self._shown))
            self._astral_cache = cached
        return cached[1]

    def _scalar_index_at(self, position: int) -> int:
        """The shown-text scalar index at a UTF-16 position (rounded down
        inside a surrogate pair)."""
        astral = self._astral()
        if not astral:
            return max(0, min(position, len(self._shown)))
        low, high = 0, len(self._shown)
        while low < high:
            middle = (low + high + 1) // 2
            if middle + bisect.bisect_left(astral, middle) <= position:
                low = middle
            else:
                high = middle - 1
        return low

    def _position_of(self, index: int) -> int:
        """The UTF-16 position of a shown-text scalar index."""
        index = max(0, min(index, len(self._shown)))
        astral = self._astral()
        return index + (bisect.bisect_left(astral, index) if astral else 0)

    def raw_offset_at_position(self, position: int) -> int:
        """The raw scalar offset at a Qt (UTF-16) document position."""
        return self._bounds[self._scalar_index_at(position)]

    def position_of_raw_offset(self, offset: int) -> int:
        """The Qt (UTF-16) document position of a raw scalar offset; one inside
        a CRLF goes before it."""
        offset = max(0, min(offset, len(self._raw)))
        return self._position_of(max(0, bisect.bisect_right(self._bounds, offset) - 1))

    def raw_offset_at_top(self) -> int | None:
        """The raw offset of the first visible line."""
        document = self.document()
        value = self.verticalScrollBar().value()
        block = document.findBlockByLineNumber(value)
        if not block.isValid():
            return len(self._raw)
        position = block.position()
        layout = block.layout()
        line = value - block.firstLineNumber()
        if layout is not None and 0 <= line < layout.lineCount():
            position += layout.lineAt(line).textStart()
        return self.raw_offset_at_position(position)

    def scroll_to_raw_offset(self, offset: int) -> None:
        """Scroll so the line holding a raw offset is the first visible one."""
        document = self.document()
        position = self.position_of_raw_offset(offset)
        block = document.findBlock(position)
        if not block.isValid():
            return
        value = block.firstLineNumber()
        layout = block.layout()
        if layout is not None and layout.lineCount() > 0:
            line = layout.lineForTextPosition(position - block.position())
            if line.isValid():
                value += line.lineNumber()
        self.verticalScrollBar().setValue(value)

    def set_raw_cursor_offset(self, offset: int) -> None:
        """Caret at a raw scalar offset, clamped; one inside a CRLF goes before it."""
        offset = max(0, min(offset, len(self._raw)))
        index = max(0, bisect.bisect_right(self._bounds, offset) - 1)
        cursor = self.textCursor()
        cursor.setPosition(_utf16_position(self._shown, index))
        self.setTextCursor(cursor)

    def _finalize_pending(self) -> None:
        rejected = self._rejected_view
        self._pending = None
        self._pending_range = None
        self._pending_view = None
        self._rejected_view = None
        if rejected is not None:
            if self.toPlainText() != self._shown:
                self.set_raw_text(self._raw)
            self.restore_view_state(rejected)

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
            self._finalize_pending()

    def selected_raw_text(self) -> str:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return ""
        start = _scalar_boundary(self._shown, cursor.selectionStart())
        end = _scalar_boundary(self._shown, cursor.selectionEnd())
        if start is None or end is None:
            return cursor.selectedText()
        return self._raw[self._bounds[start]:self._bounds[end]]

    def createMimeDataFromSelection(self) -> QMimeData:  # noqa: N802
        cursor = self.textCursor()
        start = _scalar_boundary(self._shown, cursor.selectionStart())
        end = _scalar_boundary(self._shown, cursor.selectionEnd())
        if start is None or end is None:
            return super().createMimeDataFromSelection()
        data = QMimeData()
        data.setText(self._raw[self._bounds[start]:self._bounds[end]])
        return data

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        promote = menu.addAction(strings.TERMBASE_PROMOTE_BUTTON)
        promote.triggered.connect(self.promoteRequested.emit)
        menu.exec(event.globalPos())

    @staticmethod
    def typed_with_altgr(event) -> bool:
        """Whether a Ctrl+Alt key event types a character.

        Windows reports AltGr as Ctrl+Alt, so on many layouts Ctrl+Alt+digit
        is how a character such as "²" or "@" is typed. When the event carries
        a printable character other than the key's own, it is text, and the
        workbench's Ctrl+Alt shortcuts must leave it to the editor.
        """
        both = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        text = event.text()
        if (event.modifiers() & both) != both or not text or not text.isprintable() or text.isspace():
            return False
        key = event.key()
        own = chr(key) if 0x20 < key < 0x7F else ""
        return text.casefold() != own.casefold()

    def event(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.ShortcutOverride and self.typed_with_altgr(event):
            event.accept()
            return True
        return super().event(event)

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
        text = event.text()
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            no_cmd_mods = not (
                event.modifiers()
                & (
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier
                )
            )
            if no_cmd_mods or text == "\r":
                text = "\n"
        elif text == "\r":
            text = "\n"
        self._pending = text or None
        self._remember_cursor()
        try:
            super().keyPressEvent(event)
        finally:
            self._finalize_pending()

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
            self._finalize_pending()
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
        if self._pending is not None and self._pending_range is not None:
            left = _scalar_boundary(old, self._pending_range[0])
            right = _scalar_boundary(old, self._pending_range[1])
            inserted_shown, _ = _display_map(self._pending)
            preliminary = old == new and (
                position > self._pending_range[1] or position + removed < self._pending_range[0]
            )
            if (not preliminary and left is not None and right is not None
                    and old[:left] + inserted_shown + old[right:] == new):
                raw_left, raw_right = self._bounds[left], self._bounds[right]
                raw = _splice_raw(self._raw[:raw_left], self._pending, self._raw[raw_right:])
                shown, bounds = _display_map(raw)
                if shown == new:
                    self.last_edit = (raw_left, raw_right, len(raw) - len(self._raw) + raw_right - raw_left)
                    self.last_edit_view = self._pending_view
                    self._raw, self._shown, self._bounds = raw, shown, bounds
                    self._pending = None
                    self._pending_range = None
                    self.rawChanged.emit()
                    return
        # Qt reports IME preedit or block-property updates as a contentsChange
        # before committed text changes; never roll back when shown text is unchanged.
        if old == new:
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
            ins_mid = inserted[prefix:len(inserted) - suffix if suffix else None]
            raw = _splice_raw(self._raw[:self._bounds[left]], ins_mid, self._raw[self._bounds[right]:])

            suffix2 = 0
            while suffix2 < min(len(removed_part), len(inserted)) and removed_part[-1 - suffix2] == inserted[-1 - suffix2]:
                suffix2 += 1
            prefix2 = 0
            while (prefix2 < len(removed_part) - suffix2 and prefix2 < len(inserted) - suffix2
                   and removed_part[prefix2] == inserted[prefix2]):
                prefix2 += 1
            left2, right2 = start + prefix2, end - suffix2
            ins_mid2 = inserted[prefix2:len(inserted) - suffix2 if suffix2 else None]
            raw2 = _splice_raw(self._raw[:self._bounds[left2]], ins_mid2, self._raw[self._bounds[right2]:])

            shown, bounds = _display_map(raw)
            if raw == raw2 and shown == new:
                # The whole range Qt reported, not the trimmed difference.
                old_start, old_end = self._bounds[start], self._bounds[end]
                self.last_edit = (old_start, old_end, len(raw) - len(self._raw) + old_end - old_start)
                self.last_edit_view = self._pending_view
                self._raw, self._shown, self._bounds = raw, shown, bounds
                self.rawChanged.emit()
                return
        # Never guess which of two visually identical characters was edited;
        # restore pre-edit cursor and scroll state instead of jumping to 0.
        saved_view = dict(self._pending_view) if self._pending_view is not None else None
        if saved_view is None:
            cur = self.textCursor()
            doc_limit = _utf16_position(old, len(old))
            cur_pos = cur.position()
            if cur_pos >= position + added:
                restored_pos = max(0, min(cur_pos - added + removed, doc_limit))
            else:
                restored_pos = max(0, min(position, doc_limit))
            saved_view = {
                "position": restored_pos,
                "anchor": restored_pos,
                "vertical": self.verticalScrollBar().value(),
                "horizontal": self.horizontalScrollBar().value(),
            }
        self._rejected_view = saved_view
        raw = self._raw
        self.set_raw_text(raw)
        self.restore_view_state(saved_view)
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


class _WordWrapDelegate(QStyledItemDelegate):
    """Measure multiline items against the live viewport width so wrapped lines never clip."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        view = self.parent()
        width = view.viewport().width() if isinstance(view, QListWidget) else 0
        if width <= 40:
            return super().sizeHint(option, index)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(text, str) or not text:
            return super().sizeHint(option, index)
        # A state icon takes its width (and a gap) from the text's line.
        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        icon_width = 0
        if isinstance(decoration, QIcon) and not decoration.isNull():
            size = view.iconSize() if view.iconSize().isValid() else option.decorationSize
            icon_width = size.width() + 8
        rect = option.fontMetrics.boundingRect(
            0, 0, max(20, width - 20 - icon_width), 10_000, Qt.TextFlag.TextWordWrap, text
        )
        return QSize(width, rect.height() + 14)


class _WrappedListWidget(QListWidget):
    """Recompute wrapped item heights whenever the list is shown or resized.

    With ``fit_rows`` set, the list is at most as tall as that many whole rows
    (fewer when it has fewer), so no row is cut in half at any scale."""

    def __init__(self, parent: QWidget | None = None, *, fit_rows: int | None = None) -> None:
        super().__init__(parent)
        self.setItemDelegate(_WordWrapDelegate(self))
        self.fit_rows = fit_rows

    def fit_height(self) -> None:
        if not self.fit_rows or not self.count():
            return
        rows = sum(self.sizeHintForRow(i) for i in range(min(self.count(), self.fit_rows)))
        height = max(self.minimumHeight(), rows + 2 * self.frameWidth())
        if self.maximumHeight() != height:
            self.setMaximumHeight(height)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.scheduleDelayedItemsLayout()
        self.fit_height()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()
        if event.size().width() != event.oldSize().width():
            self.fit_height()


class _ElidingButton(QPushButton):
    """A button that may be narrower than its text, so it never widens the
    inspector: short of room it draws the text elided. Its own sizeHint is the
    whole text, so it is shown whole whenever there is room; its accessible
    name and tooltip keep the whole text."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        metrics = self.fontMetrics()
        icon = self.iconSize().width() + 6 if not self.icon().isNull() else 0
        # Room for the first character and an ellipsis, and the padding.
        least = icon + metrics.horizontalAdvance(self.text()[:1] + "…") + 26
        return QSize(min(hint.width(), least), hint.height())

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.width() >= self.sizeHint().width():
            # Room for the whole text: drawn exactly as any button.
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        room = contents.width() - (option.iconSize.width() + 6 if not option.icon.isNull() else 0)
        lines = option.text.split("\n")
        elided = "\n".join(option.fontMetrics.elidedText(line, Qt.TextElideMode.ElideRight, max(0, room))
                           for line in lines)
        if elided == option.text:
            super().paintEvent(event)
            return
        option.text = elided
        QStylePainter(self).drawControl(QStyle.ControlElement.CE_PushButton, option)


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


class _WorkbenchHostAdapter(WorkbenchHost):
    """``document_controller.WorkbenchHost``, implemented over one view.

    This is the whole Qt boundary of a run: the controller says what happened,
    this says how the workbench shows it. Nothing here decides anything about a
    request, and nothing outside it is called by the controller, so the runs
    stay testable without a widget.
    """

    def __init__(self, view: "ManuscriptView") -> None:
        self._view = view

    def refresh_semantic(self) -> None:
        self._view._refresh_semantic()

    def refresh_segments(self) -> None:
        self._view._refresh_segments()

    def refresh_segments_if_shown(self) -> None:
        if self._view.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._view._refresh_segments()

    def update_controls(self) -> None:
        self._view._update_controls()

    def render_semantic_progress(self) -> None:
        self._view._render_semantic_progress()

    def show_segment_term_report(self) -> None:
        self._view._show_segment_term_report()

    def show_term_report(self, report: dict) -> None:
        self._view._show_term_report(report)

    def editor_state(self) -> dict:
        return self._view._editor_state()

    def render_review_state(self) -> None:
        self._view._render_review_state()
        if self._view._report is None:
            self._view._render_finding(-1)

    def clear_report(self) -> None:
        self._view._report = None
        self._view.findings.clear()

    def report_status(self, text: str) -> None:
        self._view.status_label.setText(text)

    def report_unstorable_report(self, code: str) -> None:
        self._view._conflict_mode = True
        self._view._refresh_conflicts()

    def report_translation_complete(self, mismatched: int) -> None:
        view = self._view
        view._mismatch_prompt_active = bool(mismatched)
        view.status_label.setText(strings.MANUSCRIPT_TRANSLATED_MISMATCH.format(count=mismatched) if mismatched
                                  else strings.MANUSCRIPT_TRANSLATED)
        view._update_controls()

    def render_structure(self) -> None:
        self._view._render_structure()

    def trust_review_snapshot(self, snapshot: dict) -> None:
        self._view._session_snapshots.append(snapshot)

    def precedents_selected(self) -> bool:
        return self._view.precedents_check.isChecked()

    def selected_segment(self):
        view = self._view
        return view._current_segment() if view.track_stack.currentIndex() == TRACK_SEGMENTS else None

    def apply_target_write(
        self,
        changed: str,
        edit: tuple,
        *,
        remap: AlignmentRemap | None = None,
    ) -> bool:
        view = self._view
        if not view.target_edit.can_display(changed):
            return False
        value = view._draft_snapshot()
        value["target"] = changed
        if remap is not None:
            value["alignments"] = remap.alignments
        elif value["alignments"] is not None:
            value["alignments"] = []
        view._apply_edit(value, edits=[edit], remap=remap)
        view._save_recovery_snapshot()
        return True

    def tm_provenance(self) -> tuple[str | None, str | None]:
        return self._view._terms_db_revision(), self._view._game_version()


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
    # The status bar's facts (counts, the caret's segment, the dictionary)
    # changed; sent at most once per caret or typing pause.
    workbenchStatusChanged = Signal()

    def __init__(
        self,
        api_client,
        parent: QWidget | None = None,
        *,
        recovery_path: Path | None = None,
        termbase_store: TermbaseStore | None = None,
        translation_memory: tm.TranslationMemory | None = None,
    ) -> None:
        super().__init__(parent)
        self._recovery_path = recovery_path
        # The person's own terms: a local reference list. It is read for the
        # source text on screen and never reaches a request, a review or a
        # manuscript file.
        self._termbase_store = termbase_store if termbase_store is not None else TermbaseStore()
        # Confirmed translations, read for the segment at the caret. None means
        # this workbench was opened without one: the panel then says so and
        # nothing is read or written. It reaches no request and no file either,
        # and only the person's own confirmation puts text into it.
        self._translation_memory = translation_memory
        self._tm_fill_plan: list[tuple[int, int, str, int, int]] = []
        self._my_terms_timer = QTimer(self)
        self._my_terms_timer.setSingleShot(True)
        self._my_terms_timer.setInterval(300)
        self._my_terms_timer.timeout.connect(self._refresh_my_terms)
        # The memory lookup wakes on the same pause as the terms: a caret only
        # passing through a segment does not query it.
        self._tm_timer = QTimer(self)
        self._tm_timer.setSingleShot(True)
        self._tm_timer.setInterval(300)
        self._tm_timer.timeout.connect(self._refresh_tm_panel)
        self._recovery_pending = False
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(1500)
        self._recovery_timer.timeout.connect(self._save_recovery_snapshot)
        # The runs and the document state they commit to. Its host is
        # _WorkbenchHostAdapter below: it renders state and performs the one
        # edit a run cannot do itself.
        self._controller = DocumentRunController(
            api_client,
            _WorkbenchHostAdapter(self),
            translation_memory=self._translation_memory,
        )
        self._saved_payload = _copy_payload(self._payload)
        self._path: Path | None = None
        self._report: dict | None = None
        self._updating = False
        # Document history excludes report snapshots, saved baseline and path:
        # an edit undo must never erase a report added later or undo a save.
        # The steps themselves are the controller's (document_edits.EditHistory);
        # this half only supplies the editor's own view state.
        self._conflict_mode = False
        self._conflict_indexes: list[int] = []
        self._document_state: tuple[Path | None, bool, int] | None = None
        # Work position (see work_position): the editor last focused, an issue
        # fingerprint waiting for its review to come back, and a counter that
        # drops a deferred scroll restore once another document is open.
        self._last_side = "target"
        self._pending_issue_id: str | None = None
        self._restore_serial = 0
        self.setAcceptDrops(True)
        self._compact = False
        self._inspector_requested = True
        self._issue_rows: list[sessions.LiveIssue] = []
        self._row_texts: list[str] = []
        self._rows_dirty = False
        self._segment_rows: list = []
        # The pairing the rows were listed from (identity only).
        self._segment_rows_from: list | None = None
        self._refreshing_lists = False
        # Pairing a long manuscript is not free; typing never waits for it.
        self._derived_timer = QTimer(self)
        self._derived_timer.setSingleShot(True)
        self._derived_timer.setInterval(300)
        self._derived_timer.timeout.connect(self._refresh_derived)
        # The selected segment: written to progress.current only on save, so
        # navigating never makes the manuscript look edited.
        self._segment_cursor: int | None = None
        # A row chosen in segment_list while it still held an earlier pairing;
        # kept apart from caret-following highlights so _refresh_segments only
        # commits genuine selections.
        self._pending_segment_row: int | None = None
        # For each listed term finding of a long manuscript: (record, start, end)
        # relative to that record's source core, and whether it awaits a re-check.
        self._finding_refs: list[tuple[int, int, int]] = []
        self._finding_recheck: list[bool] = []
        self._finding_saved: list[bool] = []
        self._quiet_row = -1
        self._quiet_ref: tuple[int, int, int] | None = None
        # Term inspector view mode ("grouped" default in long mode, "order" in short)
        # and status filter ("unhandled" default hiding confirmed noise).
        self._term_view_override: str | None = None
        self._term_filter: str = _DEFAULT_TERM_FILTER
        self._term_groups_data: list[dict] = []
        self._finding_group_status: list[set[str]] = []
        self._syncing_term_selection = False
        self._mismatch_prompt_active = False
        # The save-failure list shows history choices instead of conflicts.
        self._history_removal = False
        # Ambiguous one-character names (ambiguous_mention) of the listed
        # report, kept apart from the judged findings and folded by default:
        # (finding with whole-text spans, (record, start, end) or None).
        self._ambiguous: list[tuple[dict, tuple[int, int, int] | None]] = []
        # Layer 1 rows listed: (pair index, finding with whole-text spans), and
        # whether the pairing last rendered had anything to send.
        self._l1_rows: list[tuple[int, dict]] = []
        self._l1_sendable = False
        self._l1_views: list = []
        # Layer 2: issues left out because Layer 1 covers them, folded
        # low-confidence ones, and hidden handled issues.
        self._covered_count = 0
        self._low_rows: list[sessions.LiveIssue] = []
        self._handled_rows: list[sessions.LiveIssue] = []
        # Every issue Layer 2 lists, folded low-confidence ones included.
        self._layer2_listed: list[sessions.LiveIssue] = []
        self._search_controller = SearchReplaceController()
        self._suppress_find_refresh = False

        self.source_edit = LosslessEditor(self)
        self.target_edit = LosslessEditor(self)
        self.source_edit.setPlaceholderText(strings.MANUSCRIPT_SOURCE_PLACEHOLDER)
        self.target_edit.setPlaceholderText(strings.MANUSCRIPT_TARGET_PLACEHOLDER)
        self.target_edit.side = "target"
        for editor in (self.source_edit, self.target_edit):
            editor.filesDropped.connect(self._files_dropped)
            editor.focused.connect(lambda e=editor: setattr(self, "_last_side", e.side))
            editor.promoteRequested.connect(lambda e=editor: self._promote_from_editor(e.side))
            editor.setMinimumHeight(36)
            editor.setMinimumWidth(80)
            editor.rawChanged.connect(lambda side="source" if editor is self.source_edit else "target",
                                      e=editor: self._on_edit(edit=(side, *e.last_edit)))
            editor.undoRequested.connect(self._undo_replace)
            editor.redoRequested.connect(self._redo_edit)
            editor.compositionChanged.connect(self._on_composition_changed)
            editor.editRejected.connect(lambda: self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED))
            editor.cursorPositionChanged.connect(lambda e=editor: self._caret_moved(e))
        # The two editors scroll together, by segment anchors (on by default).
        self.scroll_sync = ScrollSync(
            self.source_edit, self.target_edit, self._scroll_anchors, self,
            paused=lambda: self.source_edit._ime_active or self.target_edit._ime_active)
        # A caret that stops for 300 ms is followed in the segment and issue
        # lists; following never moves the caret or takes the focus.
        self._caret_side = "source"
        self._caret_timer = QTimer(self)
        self._caret_timer.setSingleShot(True)
        self._caret_timer.setInterval(300)
        self._caret_timer.timeout.connect(self._follow_caret)
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
        self.findings = _WrappedListWidget(self)
        self.findings.setMinimumHeight(76)
        self.findings.setMaximumHeight(220)
        self.findings.setWordWrap(True)
        self.findings.setTextElideMode(Qt.TextElideMode.ElideNone)
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
        self.resolve_conflicts_save_button = QPushButton(strings.MANUSCRIPT_RESOLVE_CONFLICTS_SAVE, self)
        self.resolve_conflicts_save_button.setObjectName("primaryButton")
        self.resolve_conflicts_save_button.clicked.connect(self._resolve_conflicting_choices_and_save)
        self.export_prose_button = QPushButton(strings.MANUSCRIPT_EXPORT_PROSE, self)
        self.export_prose_button.clicked.connect(self.export_prose)
        for widget in (
            self.conflict_label,
            self.conflict_list,
            self.remove_choice_button,
            self.resolve_conflicts_save_button,
            self.export_prose_button,
        ):
            widget.setVisible(False)
        self.translate_button = QPushButton(self)
        self.translate_button.clicked.connect(self._translate)
        self.promote_editor_button = QPushButton(self)
        self.promote_editor_button.setObjectName("commandButton")
        self.promote_editor_button.setIcon(icon("book-open"))
        self.promote_editor_button.setIconSize(QSize(16, 16))
        self.promote_editor_button.setFixedWidth(34)
        self.promote_editor_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.promote_editor_button.setToolTip(strings.TERMBASE_PROMOTE_BUTTON)
        self.promote_editor_button.setAccessibleName(strings.TERMBASE_PROMOTE_BUTTON)
        self.promote_editor_button.clicked.connect(lambda: self._promote_from_editor())
        self._build_find_bar()
        self._build_semantic_widgets()
        self._build_layer_widgets()

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
            (self.save_as_button, "save-all", strings.MANUSCRIPT_SAVE_AS),
            (self.undo_button, "undo-2", strings.MANUSCRIPT_UNDO),
            (self.redo_button, "redo-2", strings.MANUSCRIPT_REDO),
        ):
            button.setObjectName("commandButton")
            button.setText("")
            button.setIcon(icon(name))
            button.setProperty("iconName", name)
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
        self.recovery_banner = Banner(self)
        outer.addWidget(self.recovery_banner)

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
                header_layout.addWidget(self.promote_editor_button)
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
        self.mismatch_banner = QFrame(inspector_body)
        self.mismatch_banner.setObjectName("reviewStatus")
        self.mismatch_banner.setProperty("tone", "warn")
        self.mismatch_banner.hide()
        mismatch_layout = QVBoxLayout(self.mismatch_banner)
        mismatch_layout.setContentsMargins(12, 8, 12, 10)
        mismatch_layout.setSpacing(6)
        self.mismatch_label = QLabel(self.mismatch_banner)
        self.mismatch_label.setObjectName("statusText")
        self.mismatch_label.setWordWrap(True)
        self.jump_mismatch_button = QPushButton(strings.MANUSCRIPT_JUMP_MISMATCH, self.mismatch_banner)
        self.jump_mismatch_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.jump_mismatch_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.jump_mismatch_button.setToolTip(f"{strings.MANUSCRIPT_JUMP_MISMATCH}（Ctrl+Shift+M）")
        self.jump_mismatch_button.clicked.connect(self._jump_first_mismatch)
        mismatch_layout.addWidget(self.mismatch_label)
        mismatch_layout.addWidget(self.jump_mismatch_button, 0, Qt.AlignmentFlag.AlignLeft)
        details.addWidget(self.mismatch_banner)
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
        # The person's own terms found in the source text: under the header,
        # where a hit is seen without scrolling past the review tracks.
        self.my_terms = MyTermsPanel(self._termbase_store, inspector_body)
        self.my_terms.manage_requested.connect(self.show_termbase_dialog)
        details.addWidget(self.my_terms)
        self._refresh_my_terms()
        # Confirmed translations for the segment at the caret: beside the terms,
        # because both are references read from local files while translating.
        self.tm_panel = TranslationMemoryPanel(self._translation_memory, inspector_body)
        self.tm_panel.manage_requested.connect(self.show_tm_dialog)
        self.tm_panel.apply_requested.connect(self._apply_tm_match)
        self.tm_panel.confirm_requested.connect(self._confirm_segment_to_memory)
        self.tm_panel.check_requested.connect(self._check_memory_reuse)
        self.tm_panel.fill_requested.connect(self._fill_from_memory)
        details.addWidget(self.tm_panel)
        self._refresh_tm_panel()
        details.addWidget(self.layers_card)
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
        term_details.setSpacing(8)
        self.terms_note = QLabel(strings.MANUSCRIPT_TERMS_NOTE, term_page)
        self.terms_note.setObjectName("secondaryText")
        self.terms_note.setWordWrap(True)
        term_details.addWidget(self.terms_note)
        self.term_toolbar = QWidget(term_page)
        self.term_toolbar.setObjectName("inspectorBody")
        term_bar = QHBoxLayout(self.term_toolbar)
        term_bar.setContentsMargins(0, 0, 0, 0)
        term_bar.setSpacing(6)
        self.term_view_combo = ChevronComboBox(self.term_toolbar)
        self.term_view_combo.addItem(strings.MANUSCRIPT_TERM_VIEW_GROUPED, "grouped")
        self.term_view_combo.addItem(strings.MANUSCRIPT_TERM_VIEW_ORDER, "order")
        self.term_view_combo.setAccessibleName(strings.MANUSCRIPT_TERM_VIEW_LABEL)
        self.term_view_combo.setToolTip(strings.MANUSCRIPT_TERM_VIEW_LABEL)
        self.term_view_combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.term_view_combo.setMinimumContentsLength(5)
        self.term_view_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.term_view_combo.currentIndexChanged.connect(self._on_term_view_changed)
        self.term_filter_combo = ChevronComboBox(self.term_toolbar)
        for label_text, key in (
            (strings.MANUSCRIPT_TERM_FILTER_UNHANDLED, "unhandled"),
            (strings.MANUSCRIPT_TERM_FILTER_ALL, "all"),
            (strings.MANUSCRIPT_TERM_FILTER_INCONSISTENT, "inconsistent"),
            (strings.MANUSCRIPT_TERM_FILTER_PENDING, "pending"),
            (strings.MANUSCRIPT_TERM_FILTER_NOT_EVALUATED, "not_evaluated"),
            (strings.MANUSCRIPT_TERM_FILTER_CONFIRMED, "confirmed"),
        ):
            self.term_filter_combo.addItem(label_text, key)
        self.term_filter_combo.setAccessibleName(strings.MANUSCRIPT_TERM_FILTER_LABEL)
        self.term_filter_combo.setToolTip(strings.MANUSCRIPT_TERM_FILTER_LABEL)
        self.term_filter_combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.term_filter_combo.setMinimumContentsLength(3)
        self.term_filter_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.term_filter_combo.currentIndexChanged.connect(self._on_term_filter_changed)
        term_bar.addWidget(self.term_view_combo, 1)
        term_bar.addWidget(self.term_filter_combo, 1)
        self.term_toolbar.hide()
        term_details.addWidget(self.term_toolbar)
        self.term_groups = _WrappedListWidget(term_page)
        self.term_groups.setMinimumHeight(140)
        self.term_groups.setMaximumHeight(175)
        self.term_groups.setWordWrap(True)
        self.term_groups.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.term_groups.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.term_groups.setAccessibleName(strings.MANUSCRIPT_INSPECTOR_TITLE)
        self.term_groups.currentRowChanged.connect(self._on_group_row_changed)
        self.term_groups.itemClicked.connect(self._on_group_item_clicked)
        self.term_groups.hide()
        term_details.addWidget(self.term_groups)
        term_details.addWidget(self.findings)
        # Ambiguous one-character names: after the judged findings, folded,
        # never counted with them.
        self.ambiguous_toggle = _ElidingButton(parent=term_page)
        self.ambiguous_toggle.setObjectName("commandButton")
        self.ambiguous_toggle.setCheckable(True)
        self.ambiguous_toggle.setIconSize(QSize(16, 16))
        self.ambiguous_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.ambiguous_toggle.toggled.connect(self._ambiguous_toggled)
        self.ambiguous_toggle.hide()
        term_details.addWidget(self.ambiguous_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.ambiguous_area = QWidget(term_page)
        self.ambiguous_area.setObjectName("inspectorBody")
        ambiguous = QVBoxLayout(self.ambiguous_area)
        ambiguous.setContentsMargins(0, 0, 0, 0)
        ambiguous.setSpacing(6)
        self.ambiguous_note = QLabel(strings.MANUSCRIPT_AMBIGUOUS_NOTE, self.ambiguous_area)
        self.ambiguous_note.setObjectName("secondaryText")
        self.ambiguous_note.setWordWrap(True)
        ambiguous.addWidget(self.ambiguous_note)
        self.ambiguous_list = _WrappedListWidget(self.ambiguous_area, fit_rows=3)
        self.ambiguous_list.setMinimumHeight(52)
        self.ambiguous_list.setWordWrap(True)
        self.ambiguous_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.ambiguous_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ambiguous_list.setAccessibleName(strings.MANUSCRIPT_AMBIGUOUS_LIST)
        self.ambiguous_list.currentRowChanged.connect(self._select_ambiguous)
        ambiguous.addWidget(self.ambiguous_list)
        # The candidate pair (Core offers the official name or names; long ones
        # elide instead of widening the inspector), then adopt or ordinary word.
        self.ambiguous_actions = QWidget(self.ambiguous_area)
        self.ambiguous_actions.setObjectName("inspectorBody")
        actions = QVBoxLayout(self.ambiguous_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        self.ambiguous_combo = ChevronComboBox(self.ambiguous_actions)
        self.ambiguous_combo.setAccessibleName(strings.MANUSCRIPT_AMBIGUOUS_CANDIDATE)
        self.ambiguous_combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.ambiguous_combo.setMinimumContentsLength(8)
        self.ambiguous_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        actions.addWidget(self.ambiguous_combo)
        self.ambiguous_adopt_button = _ElidingButton(strings.MANUSCRIPT_AMBIGUOUS_ADOPT_BUTTON, self.ambiguous_actions)
        self.ambiguous_adopt_button.clicked.connect(self._adopt_ambiguous)
        self.ambiguous_ignore_button = _ElidingButton(strings.MANUSCRIPT_AMBIGUOUS_IGNORE, self.ambiguous_actions)
        self.ambiguous_ignore_button.clicked.connect(lambda: self._choose_ambiguous(None))
        for button in (self.ambiguous_adopt_button, self.ambiguous_ignore_button):
            # Keep an IME composition in the editor when an action is clicked.
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            # One per line: side by side they are wider than the narrowest inspector.
            actions.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        ambiguous.addWidget(self.ambiguous_actions)
        self.ambiguous_state = QLabel(self.ambiguous_area)
        self.ambiguous_state.setObjectName("secondaryText")
        self.ambiguous_state.setTextFormat(Qt.TextFormat.PlainText)
        self.ambiguous_state.setWordWrap(True)
        ambiguous.addWidget(self.ambiguous_state)
        self.ambiguous_area.hide()
        term_details.addWidget(self.ambiguous_area)
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
        self.finding_reason = QLabel(inspector_body)
        self.finding_reason.setObjectName("secondaryText")
        self.finding_reason.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_reason.setWordWrap(True)
        self.finding_reason.hide()
        term_details.addWidget(self.finding_reason)
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
        self.choice_button = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_RECORD, self.choice_area)
        self.choice_button.clicked.connect(self._choose_term)
        self.unchoice_button = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_REMOVE, self.choice_area)
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
        self.lookup_button = _ElidingButton(strings.NAV_ITEM_TERMS, inspector_body)
        self.lookup_button.setIcon(icon("external-link"))
        self.lookup_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.lookup_button.clicked.connect(self._request_lookup)
        self.replace_toggle = _ElidingButton(strings.MANUSCRIPT_REPLACE, inspector_body)
        self.replace_toggle.setCheckable(True)
        self.replace_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        term_actions = QHBoxLayout()
        term_actions.setSpacing(6)
        term_actions.addWidget(self.replace_toggle)
        term_actions.addWidget(self.lookup_button)
        term_actions.addStretch(1)
        term_details.addLayout(term_actions)
        self.promote_term_button = _ElidingButton(strings.TERMBASE_PROMOTE_BUTTON, inspector_body)
        self.promote_term_button.setToolTip(strings.TERMBASE_PROMOTE_TOOLTIP)
        self.promote_term_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.promote_term_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.promote_term_button.clicked.connect(self._promote_from_term_finding)
        term_details.addWidget(self.promote_term_button, 0, Qt.AlignmentFlag.AlignLeft)
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
        term_details.addStretch(1)
        self.track_stack.addWidget(term_page)
        self.track_stack.addWidget(self.semantic_page)
        self.track_stack.addWidget(self.segments_page)
        details.addWidget(self.track_stack)
        details.addWidget(self.conflict_label)
        details.addWidget(self.conflict_list)
        details.addWidget(self.remove_choice_button)
        details.addWidget(self.resolve_conflicts_save_button)
        details.addWidget(self.export_prose_button)
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

        self.editor_pane = QWidget(self)
        editor_pane_layout = QVBoxLayout(self.editor_pane)
        editor_pane_layout.setContentsMargins(0, 0, 0, 0)
        editor_pane_layout.setSpacing(6)
        editor_pane_layout.addWidget(self.find_bar)
        editor_pane_layout.addWidget(self.editor_splitter, 1)

        self.work_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.work_splitter.setChildrenCollapsible(False)
        self.work_splitter.setHandleWidth(8)
        self.work_splitter.addWidget(self.editor_pane)
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
        self._check_startup_recovery()
        self._update_controls()
        self._guard_text_inputs()

    # -- the controller's state, read here for rendering ---------------------
    #
    # The runs and the document state they commit to belong to
    # ``DocumentRunController``; these properties keep the workbench's existing
    # attribute protocol (rendering reads them, and the tests read them) without
    # duplicating a single field. Writes during a run go through the controller
    # only; the setters exist for the owner's commands (open, new, the term
    # basis a test pins) and delegate there too.
    @property
    def _api_client(self):
        """The client every run uses; the controller's, never a second copy."""
        return self._controller.api_client

    @_api_client.setter
    def _api_client(self, value) -> None:
        self._controller.api_client = value

    @property
    def _payload(self) -> dict:
        return self._controller.payload

    @_payload.setter
    def _payload(self, value: dict) -> None:
        self._controller.payload = value

    @property
    def _session(self) -> sessions.Session:
        return self._controller.session

    @property
    def _record_views(self) -> sr.RecordViews:
        return self._controller.record_views

    @property
    def _term_basis(self) -> dict | None:
        return self._controller.term_basis

    @_term_basis.setter
    def _term_basis(self, value: dict | None) -> None:
        self._controller.term_basis = value

    @property
    def _segment_failures(self) -> dict[tuple[str, str], str]:
        return self._controller.segment_failures

    @property
    def _segment_notes(self) -> dict[tuple[str, str], list[str]]:
        return self._controller.segment_notes

    @property
    def _known_texts(self) -> dict[str, str]:
        return self._controller.known_texts

    @property
    def _capabilities(self) -> frozenset[str] | None:
        return self._controller.capabilities

    @_capabilities.setter
    def _capabilities(self, value: frozenset[str] | None) -> None:
        self._controller.capabilities = value

    @property
    def _term_mode(self) -> str:
        return self._controller.term_mode

    @property
    def _review_state(self) -> str:
        return self._controller.review_state

    @property
    def _review_detail(self) -> str:
        return self._controller.review_detail

    @property
    def _request_id(self) -> str | None:
        return self._controller.request_id

    @property
    def _semantic_state(self) -> str:
        return self._controller.semantic_state

    @property
    def _semantic_detail(self) -> str:
        return self._controller.semantic_detail

    @property
    def _translate_state(self) -> str:
        return self._controller.translate_state

    @property
    def _document_revision(self) -> int:
        return self._controller.document_revision

    @_document_revision.setter
    def _document_revision(self, value: int) -> None:
        self._controller.document_revision = value

    # The undo and redo steps are the controller's (document_edits.EditHistory);
    # the view reads them for its buttons and its tests, and never edits them.
    @property
    def _undo_history(self) -> list:
        return self._controller.history.undo

    @property
    def _redo_history(self) -> list:
        return self._controller.history.redo

    # The runs' handles: read by the workbench's tests to await a run, and by
    # nothing in the view (it waits for the controller's callback instead).
    @property
    def _generation(self) -> int:
        return self._controller.generation

    @property
    def _task(self):
        return self._controller.task

    @property
    def _semantic_task(self):
        return self._controller.semantic_task

    @property
    def _translate_task(self):
        return self._controller.translate_task

    # -- semantic review: widgets -------------------------------------------
    def _build_semantic_widgets(self) -> None:
        """The two tracks stay apart: deterministic term constraints, and
        model suspicions that say they may be wrong. Segments get their own
        list so long manuscripts can be navigated and their gaps explained."""
        self.track_group = QButtonGroup(self)
        self.track_group.setExclusive(True)
        # They elide rather than widen the inspector when the counts and a wide
        # system font do not fit side by side.
        self.terms_track_button = _ElidingButton(parent=self)
        self.semantic_track_button = _ElidingButton(parent=self)
        self.segments_track_button = _ElidingButton(parent=self)
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
        self.l2_title = QLabel(strings.MANUSCRIPT_L2_TITLE, page)
        self.l2_title.setObjectName("secondaryText")
        self.l2_title.setWordWrap(True)
        layout.addWidget(self.l2_title)
        self.semantic_badge = QLabel(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL, page)
        self.semantic_badge.setObjectName("experimentalBadge")
        self.semantic_badge.setToolTip(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL_NOTE)
        layout.addWidget(self.semantic_badge, 0, Qt.AlignmentFlag.AlignLeft)
        self.l2_note = QLabel(strings.MANUSCRIPT_L2_NOTE, page)
        self.l2_note.setObjectName("secondaryText")
        self.l2_note.setWordWrap(True)
        layout.addWidget(self.l2_note)
        # How many issues Layer 1 already shows (ADR 0022: Layer 1 wins an overlap).
        self.l2_covered = QLabel(page)
        self.l2_covered.setObjectName("secondaryText")
        self.l2_covered.setWordWrap(True)
        self.l2_covered.hide()
        layout.addWidget(self.l2_covered)
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
        self.issue_state.setProperty("reviewState", review_states.UNEVALUATED)
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
        self.accept_button = _ElidingButton(strings.MANUSCRIPT_ACCEPT, page)
        self.accept_button.setObjectName("primaryButton")
        self.accept_button.setToolTip(f"{strings.MANUSCRIPT_ACCEPT}（Ctrl+Enter）")
        self.accept_button.clicked.connect(self._accept_current)
        self.adjust_button = _ElidingButton(strings.MANUSCRIPT_ADJUST, page)
        self.adjust_button.setToolTip(f"{strings.MANUSCRIPT_ADJUST}（Ctrl+Shift+E）")
        self.adjust_button.clicked.connect(self._adjust_current)
        self.ignore_button = _ElidingButton(strings.MANUSCRIPT_IGNORE, page)
        self.ignore_button.setToolTip(f"{strings.MANUSCRIPT_IGNORE}（Ctrl+Shift+I）")
        self.ignore_button.clicked.connect(self._ignore_current)
        for button in (self.accept_button, self.adjust_button, self.ignore_button):
            # Keep an IME composition in the editor when an action is clicked.
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.semantic_promote_button = _ElidingButton(strings.TERMBASE_PROMOTE_BUTTON, page)
        self.semantic_promote_button.setToolTip(strings.TERMBASE_PROMOTE_TOOLTIP)
        self.semantic_promote_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.semantic_promote_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.semantic_promote_button.clicked.connect(self._promote_from_semantic_issue)
        layout.addWidget(self.semantic_promote_button, 0, Qt.AlignmentFlag.AlignLeft)
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
        # Low-confidence issues are folded at the end of the list until opened.
        self.low_toggle = _ElidingButton(parent=page)
        self.low_toggle.setObjectName("commandButton")
        self.low_toggle.setCheckable(True)
        self.low_toggle.setIconSize(QSize(16, 16))
        self.low_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.low_toggle.toggled.connect(self._low_toggled)
        self.low_toggle.hide()
        layout.addWidget(self.low_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        # Already accepted/adjusted/ignored issues stay hidden until toggled on.
        self.handled_toggle = _ElidingButton(parent=page)
        self.handled_toggle.setObjectName("commandButton")
        self.handled_toggle.setCheckable(True)
        self.handled_toggle.setChecked(False)
        self.handled_toggle.setIconSize(QSize(16, 16))
        self.handled_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.handled_toggle.toggled.connect(self._handled_toggled)
        self.handled_toggle.hide()
        layout.addWidget(self.handled_toggle, 0, Qt.AlignmentFlag.AlignLeft)
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
        self.segment_list = _WrappedListWidget(page)
        self.segment_list.setMinimumHeight(76)
        self.segment_list.setMaximumHeight(220)
        # Rows wrap instead of eliding. A row carries the segment's state and
        # both core lengths; at 150% scaling the single-line form is wider than
        # the inspector, and an ellipsis hid the counts the row exists for.
        self.segment_list.setWordWrap(True)
        self.segment_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.segment_list.setUniformItemSizes(False)
        self.segment_list.setResizeMode(QListView.ResizeMode.Adjust)
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
        self.finish_button = _ElidingButton(strings.MANUSCRIPT_SEGMENT_FINISH, page)
        self.finish_button.clicked.connect(self._toggle_finished)
        self.next_pending_button = _ElidingButton(strings.MANUSCRIPT_NEXT_PENDING, page)
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
        self.segment_choice_action = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_RECONFIRM, page)
        self.segment_choice_action.clicked.connect(self._segment_choice_primary)
        self.segment_choice_remove = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_REMOVE, page)
        self.segment_choice_remove.clicked.connect(self._segment_choice_removed)
        for button in (self.segment_choice_action, self.segment_choice_remove):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            choice_actions.addWidget(button)
        choice_actions.addStretch(1)
        layout.addLayout(choice_actions)
        self._segment_choice_rows: list[tuple] = []
        self.translate_segment_button = _ElidingButton(strings.MANUSCRIPT_TRANSLATE_SEGMENT, page)
        self.translate_segment_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.translate_segment_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.translate_segment_button.clicked.connect(self._translate_segment)
        layout.addWidget(self.translate_segment_button)
        layout.addStretch(1)

    def _build_layer_widgets(self) -> None:
        """The layered review card above the tracks: Layer 1 deterministic
        structure checks (user-triggered, never saved), a line that sums up
        Layer 2 (the semantic track), and the reserved Layer 3 switch (off by
        default; it sends and stores nothing)."""
        card = QFrame(self)
        card.setObjectName("reviewStatus")
        card.setProperty("tone", "neutral")
        self.layers_card = card
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)
        self.layers_title = QLabel(strings.MANUSCRIPT_LAYERS_TITLE, card)
        self.layers_title.setObjectName("sectionTitle")
        layout.addWidget(self.layers_title)
        self.l1_title = QLabel(strings.MANUSCRIPT_L1_TITLE, card)
        self.l1_title.setObjectName("statusText")
        self.l1_title.setWordWrap(True)
        self.l1_title.setToolTip(strings.MANUSCRIPT_L1_NOTE_DETAIL)
        layout.addWidget(self.l1_title)
        self.l1_note = QLabel(strings.MANUSCRIPT_L1_NOTE, card)
        self.l1_note.setObjectName("secondaryText")
        self.l1_note.setWordWrap(True)
        self.l1_note.setToolTip(strings.MANUSCRIPT_L1_NOTE_DETAIL)
        layout.addWidget(self.l1_note)
        self.l1_run_button = _ElidingButton(strings.MANUSCRIPT_L1_RUN.format(count=0), card)
        self.l1_run_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.l1_run_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.l1_run_button.clicked.connect(self._run_structure_check)
        layout.addWidget(self.l1_run_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.l1_status = QLabel(strings.MANUSCRIPT_L1_NOT_RUN, card)
        self.l1_status.setObjectName("secondaryText")
        self.l1_status.setTextFormat(Qt.TextFormat.PlainText)
        self.l1_status.setWordWrap(True)
        layout.addWidget(self.l1_status)
        self.l1_list = _WrappedListWidget(card, fit_rows=3)
        self.l1_list.setMinimumHeight(52)
        self.l1_list.setWordWrap(True)
        self.l1_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.l1_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.l1_list.setAccessibleName(strings.MANUSCRIPT_L1_LIST)
        self.l1_list.currentRowChanged.connect(self._select_l1_row)
        self.l1_list.hide()
        layout.addWidget(self.l1_list)
        # A label, not a button: it wraps, so it never widens the inspector.
        self.l2_overview = QLabel(card)
        self.l2_overview.setObjectName("secondaryText")
        self.l2_overview.setTextFormat(Qt.TextFormat.PlainText)
        self.l2_overview.setWordWrap(True)
        self.l2_overview.hide()
        layout.addWidget(self.l2_overview)
        # The title wraps; the switch beside it has a short label, so neither
        # widens the inspector. Component state only: never saved, never sent.
        self.l3_title = QLabel(strings.MANUSCRIPT_L3_TITLE, card)
        self.l3_title.setObjectName("statusText")
        self.l3_title.setWordWrap(True)
        layout.addWidget(self.l3_title)
        self.l3_check = QCheckBox(strings.MANUSCRIPT_L3_TOGGLE, card)
        self.l3_check.setChecked(False)
        self.l3_check.setAccessibleName(strings.MANUSCRIPT_L3_TITLE)
        self.l3_check.toggled.connect(self._style_toggled)
        layout.addWidget(self.l3_check)
        self.l3_note = QLabel(strings.MANUSCRIPT_L3_OFF, card)
        self.l3_note.setObjectName("secondaryText")
        self.l3_note.setWordWrap(True)
        layout.addWidget(self.l3_note)

    def _style_toggled(self, on: bool) -> None:
        """Layer 3 is a reserved place: switching it on sends and stores nothing."""
        self.l3_note.setText(strings.MANUSCRIPT_L3_ON if on else strings.MANUSCRIPT_L3_OFF)

    # -- Layer 1: deterministic structure checks -------------------------------
    def _run_structure_check(self) -> None:
        if not self._input_ready() or not self._api_client.is_configured:
            return
        if self._controller.start_structure_check():
            self.status_label.setText(strings.MANUSCRIPT_L1_RUNNING)

    def _render_structure(self) -> None:
        """The Layer 1 card from the controller's session state. Results follow
        the text: one whose cores changed is no longer listed, and while the
        pairing of a fresh edit is pending nothing old is shown."""
        controller = self._controller
        state = controller.structure
        running = controller.structure_running()
        pairs = self._cached_pairs()
        if pairs is None:
            # The text changed and its pairing comes after the pause: no row
            # may describe text that is gone, so the list empties now and the
            # results still valid come back with the pairing.
            self._derived_timer.start()
            if self._l1_rows or self.l1_list.count():
                self._l1_rows = []
                self.l1_list.clear()
                self.l1_list.hide()
            return
        configured = self._api_client.is_configured
        unavailable = controller.structure_known_unavailable()
        lines: list[str] = []
        items, batches, skipped = controller.structure_plan()
        views = controller.structure_views(pairs)
        self._l1_views = views
        summary = l1.summary(views)
        sendable = len(items) - len(skipped)
        if running:
            self.l1_run_button.setText(strings.MANUSCRIPT_L1_RUNNING)
        else:
            self.l1_run_button.setText((strings.MANUSCRIPT_L1_RERUN if summary.checked else strings.MANUSCRIPT_L1_RUN)
                                       .format(count=sendable))
        self._l1_sendable = bool(batches)
        self.l1_run_button.setEnabled(configured and not running and not unavailable and bool(batches)
                                      and not self._translating())
        if unavailable:
            lines.append(strings.MANUSCRIPT_L1_UNAVAILABLE)
        elif state.phase == "failed" and state.error is not None:
            lines.append(strings.MANUSCRIPT_L1_FAILED.format(reason=state.error[0]))
        if not items:
            lines.append(strings.MANUSCRIPT_L1_NONE)
        elif summary.checked:
            if summary.findings == 0 and summary.omitted == 0:
                lines.append(strings.MANUSCRIPT_L1_CLEAN.format(checked=summary.checked))
            else:
                lines.append(strings.MANUSCRIPT_L1_SUMMARY.format(
                    checked=summary.checked, segments=summary.with_findings, count=summary.findings + summary.omitted))
            if summary.omitted:
                lines.append(strings.MANUSCRIPT_L1_OMITTED.format(count=summary.omitted))
            if summary.unchecked:
                lines.append(strings.MANUSCRIPT_L1_UNCHECKED.format(count=summary.unchecked))
        elif not running and not unavailable and state.phase != "failed":
            lines.append(strings.MANUSCRIPT_L1_NOT_RUN)
        if batches and not running and not unavailable and not summary.checked:
            lines.append(strings.MANUSCRIPT_L1_COST.format(count=len(batches)))
        if skipped:
            lines.append(strings.MANUSCRIPT_L1_SKIPPED.format(count=len(skipped)))
        self.l1_status.setText("\n".join(lines))
        self.l1_status.setVisible(bool(lines))
        # A service that does not offer the check: the feature is hidden
        # (ADR 0022), and only the line saying so stays.
        self.l1_note.setVisible(not unavailable)
        self.l1_run_button.setVisible(not unavailable)
        rows: list[tuple[int, dict]] = []
        texts: list[str] = []
        for view in views:
            if not view.flagged:
                continue
            number = view.index + 1
            layout = view.result["layout"]
            if view.layout_differs:
                rows.append((view.index, {"rule_id": None, "source_span": None, "target_span": None}))
                texts.append(strings.MANUSCRIPT_L1_LAYOUT.format(
                    number=number, sp=layout["source_paragraphs"], tp=layout["target_paragraphs"],
                    sl=layout["source_lines"], tl=layout["target_lines"]))
            for finding in view.result["findings"]:
                rule = strings.MANUSCRIPT_L1_RULES.get(finding["rule_id"], finding["rule_id"])
                span = finding["source_span"] or finding["target_span"]
                rows.append((view.index, finding))
                if span is None:
                    texts.append(strings.MANUSCRIPT_L1_ITEM_SEGMENT.format(number=number, rule=rule))
                else:
                    side = strings.MANUSCRIPT_L1_SOURCE if finding["source_span"] else strings.MANUSCRIPT_L1_TARGET
                    texts.append(strings.MANUSCRIPT_L1_ITEM.format(number=number, rule=rule, side=side,
                                                                   text=span["text"]))
        if rows != self._l1_rows or self.l1_list.count() != len(rows):
            self._l1_rows = rows
            self.l1_list.blockSignals(True)
            self.l1_list.clear()
            for row, text in enumerate(texts):
                self.l1_list.addItem(text)
                item = self.l1_list.item(row)
                item.setToolTip(text)
                review_states.mark(item, review_states.NEEDS_CHECK)
            self.l1_list.setCurrentRow(-1)
            self.l1_list.blockSignals(False)
        self.l1_list.setVisible(bool(rows) and not unavailable)
        self.l1_list.fit_height()
        self._render_layer_overview()
        if self._covered_count or (state.results and self._session.issues()):
            # What Layer 1 covers decides which semantic issues are listed,
            # folded low-confidence ones included (they are not in _issue_rows).
            self._refresh_semantic()

    def _select_l1_row(self, row: int) -> None:
        """Locate a Layer 1 finding: its span, or the whole segment when the
        finding is about the segment (empty translation, layout counts)."""
        if not 0 <= row < len(self._l1_rows):
            return
        index, finding = self._l1_rows[row]
        pairs = self._cached_pairs() or []
        with self.scroll_sync.suspended():
            if finding["source_span"] is not None:
                located = self.source_edit.select_raw_span(finding["source_span"])
                self.target_edit.setExtraSelections([])
            elif finding["target_span"] is not None:
                located = self.target_edit.select_raw_span(finding["target_span"])
                self.source_edit.setExtraSelections([])
            elif 0 <= index < len(pairs):
                self._select_cores(pairs[index])
                return
            else:
                located = False
        self.status_label.setText(strings.MANUSCRIPT_LOCATED if located else strings.MANUSCRIPT_PARTIAL_LOCATION)

    def _render_layer_overview(self) -> None:
        """The card's Layer 2 line: high and medium issues listed, low folded."""
        if not self._semantic_available() and not self._session.issues():
            self.l2_overview.hide()
            return
        counts = {"high": 0, "medium": 0, "low": 0}
        for issue in self._layer2_listed:
            if self._issue_state(issue) in sessions.UNHANDLED:
                confidence = issue.issue.get("confidence")
                if confidence in counts:
                    counts[confidence] += 1
        text = strings.MANUSCRIPT_L2_OVERVIEW.format(**counts)
        if self._covered_count:
            text += "\n" + strings.MANUSCRIPT_L2_COVERED.format(count=self._covered_count)
        self.l2_overview.setText(text)
        self.l2_overview.setVisible(True)

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
            ("Ctrl+Shift+M", self._jump_first_mismatch),
            ("Alt+Up", lambda: self._jump_segment(-1)),
            ("Alt+Down", lambda: self._jump_segment(1)),
            ("Alt+Left", lambda: self._focus_editor("source")),
            ("Alt+Right", lambda: self._focus_editor("target")),
            ("F7", lambda: self._step_term_problem(1)),
            ("Shift+F7", lambda: self._step_term_problem(-1)),
            ("Ctrl+Alt+1", lambda: self._shortcut_track(TRACK_TERMS)),
            ("Ctrl+Alt+2", lambda: self._shortcut_track(TRACK_SEMANTIC)),
            ("Ctrl+Alt+3", lambda: self._shortcut_track(TRACK_SEGMENTS)),
            ("Ctrl+F", lambda: self._open_find_bar(replace=False)),
            ("Ctrl+H", lambda: self._open_find_bar(replace=True)),
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

    # -- keyboard navigation, scroll sync and caret following ----------------
    def _scroll_anchors(self) -> list[tuple[int, int]] | None:
        """Where the two texts correspond: the start of every paired segment
        and both ends. None until the pairing of the current text exists
        (pairing is never forced from a scroll)."""
        pairs = self._cached_pairs()
        if not pairs:
            return None
        points = [(0, 0)]
        points.extend((pair.source.start, pair.target.start) for pair in pairs)
        points.append((len(self._payload["source"]), len(self._payload["target"])))
        return points

    @staticmethod
    def _pair_index_at(pairs: list, side: str, offset: int) -> int:
        """The index of the pair whose region on ``side`` holds ``offset``."""
        starts = [getattr(pair, side).start for pair in pairs]
        return max(0, min(len(pairs) - 1, bisect.bisect_right(starts, offset) - 1))

    def _editor(self, side: str) -> LosslessEditor:
        return self.source_edit if side == "source" else self.target_edit

    def _caret_offset(self, side: str) -> int:
        editor = self._editor(side)
        return editor.raw_offset_at_position(editor.textCursor().position())

    def _caret_moved(self, editor: LosslessEditor) -> None:
        # A closed view (or one being torn down) has nothing to follow.
        if self._updating or not editor.isVisible():
            return
        self._caret_side = editor.side
        self._caret_timer.start()

    def _follow_caret(self) -> None:
        """Show where the caret is, in the lists beside the editors.

        The segment row, the term finding and the semantic issue at the caret
        become the current rows, and their details are shown. Nothing here
        selects text, moves a caret or takes the focus: the owner keeps typing.
        """
        if not (self.source_edit._ime_active or self.target_edit._ime_active):
            side = self._caret_side
            offset = self._caret_offset(side)
            pairs = self._cached_pairs()
            if pairs:
                self._highlight_segment_row(pairs, self._pair_index_at(pairs, side, offset))
            self._highlight_finding_at(side, offset)
            self._highlight_issue_at(side, offset)
            # The memory panel is not a row of the segment list: when the terms
            # track is the one showing, nothing above would refresh it.
            self._refresh_tm_panel()
        self.workbenchStatusChanged.emit()

    def _highlight_segment_row(self, pairs: list, index: int) -> None:
        if pairs is not self._segment_rows_from or not 0 <= index < self.segment_list.count():
            return
        if self.segment_list.currentRow() == index:
            return
        self._refreshing_lists = True
        try:
            self.segment_list.setCurrentRow(index)
        finally:
            self._refreshing_lists = False
        self.segment_list.scrollToItem(self.segment_list.item(index))
        self._render_segment(self._current_segment())
        self._update_tracks()

    @staticmethod
    def _span_holds(span: object, offset: int) -> bool:
        """Whether ``offset`` lies inside ``span`` (half-open, so the end of one
        finding and the start of the next never both hold it)."""
        return isinstance(span, dict) and isinstance(span.get("start"), int) and isinstance(span.get("end"), int) \
            and span["start"] <= offset < span["end"]

    def _highlight_finding_at(self, side: str, offset: int) -> None:
        report = self._report
        if report is None:
            return
        key = f"{side}_span"
        findings = report["findings"]
        current = self.findings.currentRow()
        if 0 <= current < len(findings) and self._span_holds(findings[current].get(key), offset):
            return  # the caret is still in the finding the owner chose
        index = next((i for i, finding in enumerate(findings)
                      if self._span_holds(finding.get(key), offset) and not self.findings.isRowHidden(i)), -1)
        if index < 0:
            return
        self.findings.blockSignals(True)
        self.findings.setCurrentRow(index)
        self.findings.blockSignals(False)
        self.findings.scrollToItem(self.findings.item(index))
        self._sync_group_row_to_finding(index)
        self._render_finding(index)
        # What _select_finding does besides locating: the replacement offered
        # is this finding's own, never the previous one's.
        candidates = findings[index]["candidates"]
        if len(candidates) == 1:
            self.replacement_edit.setText(candidates[0][self._payload["direction"]])
        else:
            self.replacement_edit.clear()
        self._update_controls()

    def _highlight_issue_at(self, side: str, offset: int) -> None:
        key = "source_span" if side == "source" else "target_span"
        current = self._current_issue()
        if current is not None and self._span_holds(getattr(current, key), offset):
            return
        row = next((i for i, issue in enumerate(self._issue_rows)
                    if self._span_holds(getattr(issue, key), offset)), -1)
        if row < 0 or row >= self.semantic_list.count():
            return
        self._refreshing_lists = True
        try:
            self.semantic_list.setCurrentRow(row)
        finally:
            self._refreshing_lists = False
        self.semantic_list.scrollToItem(self.semantic_list.item(row))
        issue = self._current_issue()
        if issue is not None:
            self._pending_issue_id = None
        # What _select_issue does besides locating: the alternative and the
        # adjustment typed for the previous issue never carry over.
        self.issue_alternative_button.blockSignals(True)
        self.issue_alternative_button.setChecked(False)
        self.issue_alternative_button.blockSignals(False)
        self.adjust_area.hide()
        self._render_issue(issue)
        self._update_tracks()

    def _jump_segment(self, step: int) -> None:
        """Alt+Up / Alt+Down: the caret to the start of the previous or next
        segment in the same editor; the other editor follows by scroll sync."""
        if not self._input_ready():
            return
        pairs = self._current_pairs()
        if not pairs:
            self.status_label.setText(strings.MANUSCRIPT_NO_SEGMENTS)
            return
        side = self._last_side
        offset = self._caret_offset(side)
        index = self._pair_index_at(pairs, side, offset)
        here = getattr(pairs[index], side)
        # Alt+Up inside a segment goes to its own start first, as a text editor does.
        target = index if step < 0 and offset > here.core_start else index + step
        if not 0 <= target < len(pairs):
            self.status_label.setText(strings.MANUSCRIPT_SEGMENT_FIRST if step < 0 else strings.MANUSCRIPT_SEGMENT_LAST)
            return
        editor = self._editor(side)
        editor.set_raw_cursor_offset(getattr(pairs[target], side).core_start)
        editor.ensureCursorVisible()
        self._caret_side = side
        self._caret_timer.stop()
        self._follow_caret()

    def _focus_editor(self, side: str) -> None:
        """Alt+Left / Alt+Right: the source or the target editor, caret kept."""
        if not self._input_ready():
            return
        self._editor(side).setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _step_term_problem(self, step: int) -> None:
        """F7 / Shift+F7: the next or previous term finding that is a conflict
        or needs a check, located in both editors."""
        if not self._input_ready():
            return
        findings = self._report["findings"] if self._report is not None else []
        problems = [i for i, finding in enumerate(findings)
                    if finding["verdict"] in ("confirmed_conflict", "needs_review")]
        if not problems:
            self.status_label.setText(strings.MANUSCRIPT_NO_TERM_PROBLEMS)
            return
        self._show_track(TRACK_TERMS)
        current = self.findings.currentRow()
        if step > 0:
            row = next((i for i in problems if i > current), problems[0])
        else:
            row = next((i for i in reversed(problems) if i < current), problems[-1])
        if self.findings.isRowHidden(row):
            # A filter hides it: show every state rather than skip a problem.
            self.term_filter_combo.setCurrentIndex(max(0, self.term_filter_combo.findData("all")))
        if self.findings.currentRow() == row:
            self._select_finding(row)
        else:
            self.findings.setCurrentRow(row)
        located = self.status_label.text()
        self.status_label.setText(strings.MANUSCRIPT_TERM_PROBLEM_AT.format(
            current=problems.index(row) + 1, count=len(problems)) + " · " + located)

    def _shortcut_track(self, index: int) -> None:
        """Ctrl+Alt+1/2/3: the terms, semantic or segments track, with the
        inspector shown; a track the service does not offer stays closed."""
        button = self.track_group.button(index)
        if button is None or button.isHidden():
            if index == TRACK_SEMANTIC:
                self.status_label.setText(strings.MANUSCRIPT_SEMANTIC_TRACK_UNAVAILABLE)
            return
        if self.inspector.isHidden():
            self._toggle_inspector()
        self._show_track(index)

    def status_summary(self) -> dict:
        """What the status bar shows about the open manuscript."""
        payload = self._payload
        pairs = self._cached_pairs()
        if pairs is None:
            # Pairing waits for a pause like the segment track's count does.
            self._derived_timer.start()
        current = None
        if pairs:
            current = self._pair_index_at(pairs, self._caret_side, self._caret_offset(self._caret_side)) + 1
        basis = self._term_basis
        return {
            "source_chars": len(_WHITESPACE_RUN.sub("", payload["source"])),
            "target_chars": len(_WHITESPACE_RUN.sub("", payload["target"])),
            "segments": None if pairs is None else len(pairs),
            "current_segment": current,
            "dictionary": dict(basis["dictionary"]) if isinstance(basis, dict) and isinstance(
                basis.get("dictionary"), dict) else None,
        }

    # -- semantic review: derived state --------------------------------------
    def _decisions(self) -> list[dict]:
        """The semantic decisions stored in the manuscript (controller state)."""
        return self._controller.decisions()

    def _issue_state(self, issue: sessions.LiveIssue) -> str:
        return self._controller.issue_state(issue)

    def _cached_pairs(self) -> list | None:
        """The pairing of the current text if already computed; never computes."""
        return self._controller.cached_pairs()

    def _refresh_derived(self) -> None:
        self._current_pairs()
        self._records()
        # An edit cleared the list; what is still current comes straight back.
        if self._report is None and self._review_state in ("none", "history", "stale", "saved", "current"):
            self._show_segment_term_report()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._refresh_segments()
        self._render_mismatch_prompt()
        self._render_structure()
        self._update_tracks()
        self._render_semantic_progress()
        self.workbenchStatusChanged.emit()

    def _current_pairs(self) -> list:
        """The segmentation-v1 pairing of the current text (controller state)."""
        return self._controller.current_pairs()

    def _segment_key(self, pair) -> tuple:
        """A pair's identity: where its cores are and what they say. Identical
        paragraphs at different places are different segments."""
        return self._controller.segment_key(pair)

    def _long_now(self) -> bool:
        """Whether the manuscript is reviewed per segment (controller state)."""
        return self._controller.long_now()

    def _put_parts(self, parts: sr.Parts) -> None:
        """Hold ``parts`` in the payload, in place; not an undo step of its own."""
        self._controller.put_parts(parts)

    def _records(self) -> list | None:
        """The long-mode records, one per pair of ``_current_pairs()``, or None
        in document mode. The controller normalizes them whenever the text,
        inputs, pairing, basis or parts changed; normalizing is not an edit, and
        an unchanged file stays unchanged."""
        return self._controller.records()

    def _term_state(self, record: dict) -> str:
        return self._record_views.get(record, self._payload, self._term_basis,
                                      self._controller.review_features()).state

    def _has_results(self) -> bool:
        return bool(self._session.results) or any(r["status"] != "pending" for r in self._payload.get("segments") or ())

    def _segment_status(self, pair) -> str:
        if pair.status != STATUS_READY:
            if pair.status in _TRANSLATION_PENDING_STATES and self._translating():
                # The target is still being written: this pair's verdict says
                # nothing yet, and the run ends by pairing it properly.
                return "translating"
            return pair.status
        records = self._records()
        if records is None:
            if self._review_state == "current" and self._report is not None:
                term = "current"
            elif self._review_state == "stale":
                term = "stale"
            elif self._review_state == "error":
                term = "failed"
            else:
                term = "pending"
        else:
            term = self._term_state(records[pair.index])
        failure = self._segment_failures.get(self._segment_key(pair))
        return sr.review_status(pair, term, self._payload, self._session, self._basis(), failure)

    def _progress(self) -> tuple[int, int] | None:
        pairs = self._cached_pairs()
        if pairs is None:
            self._derived_timer.start()
            return None
        ready = [pair for pair in pairs if pair.status == STATUS_READY]
        done = sum(1 for pair in ready if self._segment_status(pair) == "reviewed")
        return done, len(ready)

    def _semantic_available(self) -> bool:
        return self._controller.semantic_available()

    def _semantic_running(self) -> bool:
        return self._controller.semantic_running()

    def _translating(self) -> bool:
        return self._controller.translating()

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

    @property
    def hide_handled(self) -> bool:
        return not self.handled_toggle.isChecked()

    @hide_handled.setter
    def hide_handled(self, value: bool) -> None:
        self.handled_toggle.setChecked(not bool(value))

    def _refresh_semantic(self) -> None:
        """Keep the issue rows in step. The list widget is rebuilt only when the
        rows themselves change; otherwise only changed row texts are updated,
        and not at all while the semantic track is hidden."""
        if self._refreshing_lists:
            return
        current = self._current_issue()
        listed, covered = self._layer2_rows()
        open_low = self.low_toggle.isChecked()
        hide_handled = self.hide_handled
        handled = [
            issue for issue in listed
            if self._issue_state(issue) not in sessions.UNHANDLED
        ]
        active_pool = (
            [issue for issue in listed if self._issue_state(issue) in sessions.UNHANDLED]
            if hide_handled else listed
        )
        low = [issue for issue in active_pool if issue.issue.get("confidence") == "low"]
        # High and medium first; low-confidence issues are folded at the end
        # of the list until the owner opens them.
        rows = sorted([issue for issue in active_pool if open_low or issue.issue.get("confidence") != "low"],
                      key=lambda issue: (
            1 if issue.issue.get("confidence") == "low" else 0,
            0 if self._issue_state(issue) in sessions.UNHANDLED else 1,
            issue.segment_index,
            (issue.source_span or issue.target_span or {"start": 0})["start"]))
        self._covered_count, self._low_rows, self._handled_rows, self._layer2_listed = covered, low, handled, listed
        self.l2_covered.setText(strings.MANUSCRIPT_L2_COVERED.format(count=covered))
        self.l2_covered.setVisible(bool(covered))
        self.low_toggle.setText((strings.MANUSCRIPT_L2_LOW_HIDE if open_low else strings.MANUSCRIPT_L2_LOW_SHOW)
                                .format(count=len(low)))
        self.low_toggle.setIcon(icon("chevron-down" if open_low else "chevron-right"))
        self.low_toggle.setVisible(bool(low))
        show_handled = not hide_handled
        self.handled_toggle.setText(
            (strings.MANUSCRIPT_L2_HANDLED_HIDE if show_handled else strings.MANUSCRIPT_L2_HANDLED_SHOW)
            .format(count=len(handled))
        )
        self.handled_toggle.setIcon(icon("chevron-down" if show_handled else "chevron-right"))
        self.handled_toggle.setVisible(bool(handled))
        same = len(rows) == len(self._issue_rows) and all(a is b for a, b in zip(rows, self._issue_rows))
        visible = self.track_stack.currentIndex() == TRACK_SEMANTIC
        self._refreshing_lists = True
        try:
            if not same:
                self._issue_rows = rows
                self._row_texts = [self._issue_row_text(issue) for issue in rows]
                self.semantic_list.clear()
                self.semantic_list.addItems(self._row_texts)
                for row, issue in enumerate(rows):
                    state = self._issue_state(issue)
                    review_states.mark(self.semantic_list.item(row), review_states.for_issue(state),
                                       label=strings.MANUSCRIPT_ISSUE_STATE[state])
                self.semantic_list.setCurrentRow(rows.index(current) if current in rows else -1)
                self._rows_dirty = False
            elif not visible:
                self._rows_dirty = True
            else:
                for row, issue in enumerate(rows):
                    text = self._issue_row_text(issue)
                    if text != self._row_texts[row]:
                        self._row_texts[row] = text
                        item = self.semantic_list.item(row)
                        item.setText(text)
                        item.setToolTip("")
                        state = self._issue_state(issue)
                        review_states.mark(item, review_states.for_issue(state),
                                           label=strings.MANUSCRIPT_ISSUE_STATE[state])
                self._rows_dirty = False
        finally:
            self._refreshing_lists = False
        self._select_pending_issue()
        self.semantic_list.setVisible(bool(rows))
        self._render_issue(self._current_issue())
        self._render_semantic_progress()
        self._render_layer_overview()

    def _layer2_rows(self) -> tuple[list[sessions.LiveIssue], int]:
        """The semantic issues Layer 2 lists, and how many it leaves out because
        a Layer 1 finding of the same segment already describes the same break
        (ADR 0022 "Layer 1 wins an overlap"). Only an unhandled issue is left
        out; decisions and the saved manuscript are untouched."""
        issues = self._session.issues()
        if not issues or not self._controller.structure.results:
            return list(issues), 0
        pairs = self._cached_pairs()
        # While a fresh edit waits for its pairing, the last views decide, so
        # the list does not flicker on every keystroke.
        views = self._controller.structure_views(pairs) if pairs is not None else self._l1_views
        pairs = pairs if pairs is not None else self._controller.pairs
        listed, covered = [], 0
        for issue in issues:
            if self._issue_state(issue) in sessions.UNHANDLED:
                index = self._issue_pair(issue, pairs)
                view = views[index] if index is not None and index < len(views) else None
                if l1.covered_by(issue.issue["category"], issue.source_span, issue.target_span, view):
                    covered += 1
                    continue
            listed.append(issue)
        return listed, covered

    @staticmethod
    def _issue_pair(issue: sessions.LiveIssue, pairs: list) -> int | None:
        """The current pair whose core holds the issue (its source span, else its target span)."""
        span, side = (issue.source_span, "source") if issue.source_span is not None else (issue.target_span, "target")
        if span is None:
            return None
        for pair in pairs:
            region = getattr(pair, side)
            if region.core_start <= span["start"] and span["end"] <= region.core_end:
                return pair.index
        return None

    def _low_toggled(self, _open: bool) -> None:
        self._refresh_semantic()
        self._update_tracks()

    def _handled_toggled(self, _open: bool) -> None:
        self._refresh_semantic()
        self._update_tracks()

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
        pending = max(0, pending - self._covered_count)  # Layer 1 shows those
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
        return self._controller.selected_option(issue, self.issue_alternative_button.isChecked())

    def _render_issue(self, issue: sessions.LiveIssue | None) -> None:
        widgets = (self.issue_confidence, self.issue_excerpts, self.issue_rationale, self.issue_suggestion,
                   self.issue_term_effect, self.issue_notice)
        if issue is None:
            if self._issue_rows:
                self.issue_title.setText(strings.MANUSCRIPT_SEMANTIC_PROMPT)
            elif self._handled_rows:
                self.issue_title.setText(strings.MANUSCRIPT_ALL_HANDLED)
            else:
                self.issue_title.setText("")
            self.issue_state.hide()
            for widget in widgets:
                widget.clear()
                widget.hide()
            for widget in (self.issue_alternative_button, self.accept_button, self.adjust_button,
                           self.ignore_button, self.semantic_promote_button, self.adjust_area):
                widget.hide()
            return
        record = issue.issue
        state = self._issue_state(issue)
        self.issue_title.setText(strings.MANUSCRIPT_CATEGORY.get(record["category"], record["category"]))
        self.issue_state.setText(strings.MANUSCRIPT_ISSUE_STATE[state])
        self.issue_state.setProperty("reviewState", review_states.for_issue(state))
        self.issue_state.style().unpolish(self.issue_state)
        self.issue_state.style().polish(self.issue_state)
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
        for button in (self.accept_button, self.adjust_button, self.ignore_button, self.semantic_promote_button):
            button.show()
        # suggestion_withheld leaves only adjust and ignore (plan §2.3).
        self.accept_button.setVisible(suggestion is not None)
        self.adjust_area.setVisible(self.adjust_area.isVisible() and self._can_adjust(issue))
        self._update_semantic_actions()

    def _can_accept(self, issue: sessions.LiveIssue | None) -> bool:
        return self._controller.can_accept(issue)

    def _can_adjust(self, issue: sessions.LiveIssue | None) -> bool:
        return self._controller.can_adjust(issue)

    def _update_semantic_actions(self) -> None:
        issue = self._current_issue()
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        free = not composing and not self._translating()
        self.accept_button.setEnabled(free and self._can_accept(issue))
        self.adjust_button.setEnabled(free and self._can_adjust(issue))
        # A decision already made here stays as history; it is not decided again.
        self.ignore_button.setEnabled(free and self._controller.can_ignore(issue))
        self.adjust_apply_button.setEnabled(free and self._can_adjust(issue))
        self.semantic_promote_button.setEnabled(not composing and issue is not None)
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
        with self.scroll_sync.suspended():
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
        else:
            handled_pos = (
                1 if handled.issue.get("confidence") == "low" else 0,
                handled.segment_index,
                (handled.source_span or handled.target_span or {"start": 0})["start"],
            )
            unhandled_rows = [
                (
                    row,
                    (
                        1 if issue.issue.get("confidence") == "low" else 0,
                        issue.segment_index,
                        (issue.source_span or issue.target_span or {"start": 0})["start"],
                    ),
                )
                for row, issue in enumerate(rows)
                if self._issue_state(issue) in sessions.UNHANDLED
            ]
            if unhandled_rows:
                next_row = next((row for row, pos in unhandled_rows if pos >= handled_pos), unhandled_rows[0][0])
                self.semantic_list.setCurrentRow(next_row)
                return
        self.status_label.setText(self.status_label.text() + " " + strings.MANUSCRIPT_ALL_HANDLED)

    # -- semantic review: decisions ------------------------------------------
    def _decision_refused(self, outcome: CommandOutcome, *, refresh: bool = False) -> None:
        """Say why a decision did not apply, in the workbench's own words.

        ``refresh`` re-lists the issue: its spans described text that has moved
        since, which is what the owner has to see before deciding again.
        """
        if outcome.reason in (REASON_RECONFIRM, REASON_STALE):
            self.status_label.setText(strings.MANUSCRIPT_RECONFIRM_NEEDED)
            if refresh:
                self._refresh_semantic()
        elif outcome.reason == REASON_REJECTED:
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
        elif outcome.reason == REASON_FULL:
            self.status_label.setText(strings.MANUSCRIPT_DECISIONS_FULL)

    def _accept_current(self) -> None:
        if self.adjust_area.isVisible() and self.adjust_edit.hasFocus():
            self._apply_adjustment()
            return
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None or not self.accept_button.isEnabled():
            return
        outcome = self._controller.accept_issue(
            issue, alternative=self.issue_alternative_button.isChecked(),
            can_display=self.target_edit.can_display)
        if self._commit(outcome, editors=True):
            self.status_label.setText(strings.MANUSCRIPT_ACCEPTED)
            self._advance_after_decision(issue)
            return
        self._decision_refused(outcome, refresh=True)

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
        if issue is None:
            return
        outcome = self._controller.adjust_issue(issue, self.adjust_edit.toPlainText(),
                                                can_display=self.target_edit.can_display)
        if self._commit(outcome, editors=True):
            self.adjust_area.hide()
            self.status_label.setText(strings.MANUSCRIPT_ADJUSTED)
            self._advance_after_decision(issue)
            return
        self._decision_refused(outcome)

    def _ignore_current(self) -> None:
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None or not self.ignore_button.isEnabled():
            return
        outcome = self._controller.ignore_issue(issue)
        if self._commit(outcome, editors=True):
            self.status_label.setText(strings.MANUSCRIPT_IGNORED)
            self._advance_after_decision(issue)
            return
        self._decision_refused(outcome)

    # -- segments --------------------------------------------------------------
    def _refresh_segments(self) -> None:
        if self._refreshing_lists:
            return
        pairs = self._current_pairs()
        records = self._records()
        pending_row = self._pending_segment_row
        self._pending_segment_row = None
        if pending_row is not None:
            row = pending_row
        else:
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
                review_states.mark(self.segment_list.item(self.segment_list.count() - 1),
                                   review_states.for_segment(self._segment_status(pair)))
            self.segment_list.setCurrentRow(row if row < len(pairs) else -1)
        finally:
            self._refreshing_lists = False
        pair = self._current_segment()
        if pending_row is not None and pair is not None and pair.index != self._segment_cursor:
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
            can_retranslate = (
                pair.status == STATUS_READY
                and not pair.hard_cut
                and 0 < pair.source.core_end - pair.source.core_start <= SEGMENT_LIMIT
            )
            self.translate_segment_button.setVisible(pair.status == STATUS_UNTRANSLATED or can_retranslate)
            self.translate_segment_button.setEnabled(self._translate_ready())
        finished = record is not None and pair.index in sr.parts_of(self._payload).progress["completed"]
        self.finish_button.setText(strings.MANUSCRIPT_SEGMENT_UNFINISH if finished else strings.MANUSCRIPT_SEGMENT_FINISH)
        long = self._long_now()
        self.finish_button.setVisible(long)
        self.next_pending_button.setVisible(long)
        self._render_segment_choices(pair.index if record is not None else None)
        self._update_segment_actions()
        self._render_translate_offer()
        # The memory lists what is confirmed for this segment, so it follows the
        # same event as the segment details. A read of a local file: cheap, and
        # nothing is written.
        self._refresh_tm_panel()

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
        return self._controller.reconfirmable(index, choice)

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
            self._finish_choice(self._controller.restore_choice(selected[1], pair.index),
                                strings.MANUSCRIPT_SEGMENT_CHOICE_RESTORED)
            return
        self._finish_choice(self._controller.confirm_choice(pair.index, selected[1]),
                            strings.MANUSCRIPT_TERM_CHOICE_RECORDED)

    def _segment_choice_removed(self) -> None:
        selected, pair = self._selected_segment_choice(), self._current_segment()
        if selected is None or selected[0] != "choice" or pair is None or not self._input_ready():
            return
        span = selected[1]["source_span"]
        self._apply_parts_edit(self._controller.unchoose_term(pair.index, span["start"], span["end"]),
                               strings.MANUSCRIPT_TERM_CHOICE_REMOVED)

    def _toggle_finished(self) -> None:
        pair = self._current_segment()
        if pair is None or not self._input_ready() or self._records() is None:
            return
        finished = pair.index in sr.parts_of(self._payload).progress["completed"]
        outcome = self._controller.set_finished(pair.index, not finished)
        if outcome.reason == REASON_NOT_APPLICABLE:
            return
        self._apply_parts_edit(outcome, strings.MANUSCRIPT_SEGMENT_UNFINISHED if finished
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
            self._pending_segment_row = None
            self._segment_cursor = pair.index
        else:
            self._pending_segment_row = _row if _row >= 0 else None
        self._render_segment(pair)
        self._update_tracks()
        if pair is not None:
            self._select_cores(pair)

    def _select_cores(self, pair) -> None:
        """Select the pair's source and target cores in the editors."""
        source, target = self._payload["source"], self._payload["target"]
        with self.scroll_sync.suspended():
            for editor, text, region in ((self.source_edit, source, pair.source),
                                         (self.target_edit, target, pair.target)):
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
        # Issues Layer 1 already shows are not counted again (folded low ones are).
        pending = sum(1 for issue in self._session.issues() if self._issue_state(issue) in sessions.UNHANDLED)
        pending = max(0, pending - self._covered_count)
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

    def _effective_term_view(self) -> str:
        if self._term_view_override is not None:
            return self._term_view_override
        return "grouped" if self._long_now() else "order"

    def _update_term_list_visibility(self) -> None:
        total = len(self._report["findings"]) if self._report is not None else 0
        self.term_toolbar.setVisible(total > 0)
        if total == 0:
            self.findings.hide()
            self.term_groups.hide()
            return
        mode = self._effective_term_view()
        group_has = any(
            self.term_groups.item(r).data(Qt.ItemDataRole.UserRole) is not None
            for r in range(self.term_groups.count())
        )
        order_has = any(not self.findings.isRowHidden(r) for r in range(self.findings.count()))
        self.term_groups.setVisible(mode == "grouped" and group_has)
        self.findings.setVisible(mode == "order" and order_has)
        if mode == "grouped" and group_has:
            self.term_groups.scheduleDelayedItemsLayout()
        elif mode == "order" and order_has:
            self.findings.scheduleDelayedItemsLayout()

    def _on_term_view_changed(self, _index: int = -1) -> None:
        if self._syncing_term_selection:
            return
        data = self.term_view_combo.currentData()
        if data in ("grouped", "order"):
            self._term_view_override = data
        self._refresh_term_lists(quiet=False, preferred_index=self.findings.currentRow())

    def _on_term_filter_changed(self, _index: int = -1) -> None:
        if self._syncing_term_selection:
            return
        data = self.term_filter_combo.currentData()
        if isinstance(data, str):
            self._term_filter = data
        self._refresh_term_lists(quiet=False, preferred_index=self.findings.currentRow())

    def _on_group_item_clicked(self, item: QListWidgetItem) -> None:
        if self._syncing_term_selection or not self._input_ready():
            return
        if item.data(Qt.ItemDataRole.UserRole) is None:
            first_row = item.data(Qt.ItemDataRole.UserRole + 1)
            if isinstance(first_row, int) and 0 <= first_row < self.term_groups.count():
                self.term_groups.setCurrentRow(first_row)

    def _on_group_row_changed(self, group_row: int) -> None:
        if self._syncing_term_selection:
            return
        item = self.term_groups.item(group_row) if 0 <= group_row < self.term_groups.count() else None
        finding_index = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if finding_index is None:
            return
        self._syncing_term_selection = True
        try:
            if self.findings.currentRow() != finding_index:
                self.findings.setCurrentRow(finding_index)
            else:
                self._select_finding(finding_index)
        finally:
            self._syncing_term_selection = False

    def _sync_group_row_to_finding(self, finding_index: int) -> None:
        target_row = -1
        for r in range(self.term_groups.count()):
            if self.term_groups.item(r).data(Qt.ItemDataRole.UserRole) == finding_index:
                target_row = r
                break
        self.term_groups.blockSignals(True)
        self.term_groups.setCurrentRow(target_row)
        self.term_groups.blockSignals(False)

    def _build_document_term_groups(self, report: dict) -> list[dict]:
        """Group findings of a short/document-mode report with the same rules."""
        source, target, direction = self._payload["source"], self._payload["target"], self._payload["direction"]
        if not report["findings"]:
            return []
        synthetic_findings = [
            {**f, "rule_id": sr.TERM_PAIR_RULE} for f in report["findings"]
            if isinstance(f.get("source_span"), dict)
        ]
        synthetic_report = {
            "request_id": report.get("request_id") or "doc",
            "source_revision": sr.content_hash(source),
            "target_revision": sr.content_hash(target),
            "rule_version": sr.RULE_VERSION,
            "matcher_revision": report.get("matcher_revision") or ("0" * 64),
            "dictionary": {
                "schema_version": (report.get("dictionary") or {}).get("schema_version") or "1",
                "source_commit": (report.get("dictionary") or {}).get("source_commit") or "doc",
                "term_count": int((report.get("dictionary") or {}).get("term_count") or 0),
                "revision": (report.get("dictionary") or {}).get("revision") or ("0" * 64),
            },
            "coverage": {
                "evaluated": int((report.get("coverage") or {}).get("evaluated") or 0),
                "not_evaluated": int((report.get("coverage") or {}).get("not_evaluated") or 0),
                "rules": list(sr.COVERAGE_RULES),
            },
            "findings": synthetic_findings,
            "truncated": bool(report.get("truncated")),
        }
        compact = sr.compact_result(synthetic_report, source, target)
        if compact is None:
            return []
        basis = sr.basis_of(synthetic_report)
        record = {
            "index": 0,
            "segmentation_version": "segmentation-v1",
            "source_start": 0,
            "source_end": len(source),
            "target_start": 0,
            "target_end": len(target),
            "source_hash": sr.content_hash(source),
            "target_hash": sr.content_hash(target),
            "status": "done" if self._review_state == "current" else "stale",
            "direction": direction,
            "alignment_input": sr.alignment_input(
                self._payload["alignments"], source, target, (0, len(source)), (0, len(target))
            ),
            "basis": basis,
            "result": compact,
            "choices": [],
        }
        return sr.group_term_occurrences(
            [record], [], source, target, direction,
            alignments=self._payload["alignments"],
            basis=basis if self._review_state == "current" else None,
        )

    def _group_status_label(self, group: dict) -> str:
        """The group header's status word. A saved result's basis is not the live
        one yet, so a group that waits only for that check names the check
        rather than a state this session cannot vouch for."""
        if group.get("saved_count") and group["status"] == "pending":
            return strings.MANUSCRIPT_TERM_GROUP_STATUS_SAVED
        return strings.MANUSCRIPT_TERM_GROUP_STATUS.get(group["status"], group["status"])

    def _refresh_term_lists(self, *, quiet: bool = False, preferred_index: int = -1) -> None:
        report = self._report
        total = len(report["findings"]) if report is not None else 0
        mode = self._effective_term_view()
        self._syncing_term_selection = True
        try:
            self.term_view_combo.blockSignals(True)
            self.term_view_combo.setCurrentIndex(max(0, self.term_view_combo.findData(mode)))
            self.term_view_combo.blockSignals(False)
            self.term_filter_combo.blockSignals(True)
            self.term_filter_combo.setCurrentIndex(max(0, self.term_filter_combo.findData(self._term_filter)))
            self.term_filter_combo.blockSignals(False)

            self.term_groups.blockSignals(True)
            self.term_groups.clear()
            if report is not None and total > 0:
                for group in self._term_groups_data:
                    # A filter shows a group that holds a place in that state,
                    # whatever the group's own status is (``group_matches_filter``).
                    if not sr.group_matches_filter(group, self._term_filter):
                        continue
                    header_text = strings.MANUSCRIPT_TERM_GROUP_HEADER.format(
                        surface=group["surface"],
                        count=group["count"],
                        status=self._group_status_label(group),
                        distribution=group["distribution_text"],
                    )
                    if group.get("saved_count"):
                        # The results come from the file, not from this session: the
                        # header says so and never reads as 已核 before the check.
                        header_text += "\n" + strings.MANUSCRIPT_TERM_SAVED_NOTE
                    if group["unverified_segments"]:
                        # Places the group does not hold yet: the header says so
                        # rather than counting a manuscript as settled.
                        header_text += "\n" + strings.MANUSCRIPT_TERM_GROUP_UNVERIFIED.format(
                            count=len(group["unverified_segments"]),
                            segments="、".join(f"#{n}" for n in group["unverified_segments"]),
                        )
                    header_item = QListWidgetItem(header_text)
                    review_states.mark(header_item, review_states.for_group(
                        group["status"], saved=bool(group.get("saved_count"))))
                    header_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    header_item.setData(Qt.ItemDataRole.UserRole, None)
                    first_occ_row = self.term_groups.count() + 1
                    header_item.setData(Qt.ItemDataRole.UserRole + 1, first_occ_row)
                    self.term_groups.addItem(header_item)

                    for occ in group["occurrences"]:
                        summary_parts = []
                        if occ["actual_translation"] is not None:
                            summary_parts.append(occ["actual_translation"])
                        elif occ["verdict"] != "not_evaluated":
                            summary_parts.append(strings.MANUSCRIPT_TERM_TARGET_MISS)
                        summary_parts.append(
                            strings.MANUSCRIPT_TERM_SAVED_NOTE if occ["freshness"] == "saved"
                            else occ["verdict_label"]
                        )
                        if occ["choice"] is not None:
                            summary_parts.append(
                                strings.MANUSCRIPT_FINDING_CHOSEN
                                if occ["choice"]["confirmed"]
                                else strings.MANUSCRIPT_FINDING_RECONFIRM
                            )
                        if occ["freshness"] == "stale":
                            summary_parts.append(strings.MANUSCRIPT_FINDING_RECHECK)
                        summary_str = " · ".join(summary_parts)
                        row_text = strings.MANUSCRIPT_TERM_OCCURRENCE_ROW.format(
                            segment=occ["segment_number"],
                            summary=summary_str,
                            source_context=occ["source_context"],
                            target_context=occ["target_context"],
                        )
                        occ_item = QListWidgetItem(row_text)
                        occ_item.setData(Qt.ItemDataRole.UserRole, occ["finding_index"])
                        occ_item.setToolTip(
                            strings.MANUSCRIPT_TERM_OCCURRENCE_TOOLTIP.format(
                                segment=occ["segment_number"],
                                summary=summary_str,
                                source_context=occ["source_context"],
                                target_context=occ["target_context"],
                            )
                        )
                        review_states.mark(occ_item, review_states.for_verdict(
                            occ["verdict"], saved=occ["freshness"] == "saved", recheck=occ["freshness"] == "stale"))
                        self.term_groups.addItem(occ_item)
            self.term_groups.blockSignals(False)

            for i in range(self.findings.count()):
                status = self._finding_group_status[i] if i < len(self._finding_group_status) else {"all", "pending"}
                matches = self._term_filter == "all" or self._term_filter in status
                self.findings.setRowHidden(i, not matches)

            self._update_term_list_visibility()
            if total == 0:
                self._render_finding(-1)
                return

            def allowed(idx: int) -> bool:
                if not 0 <= idx < total:
                    return False
                if self._term_filter == "all":
                    return True
                st = self._finding_group_status[idx] if idx < len(self._finding_group_status) else {"all", "pending"}
                return self._term_filter in st

            selected = preferred_index if allowed(preferred_index) else -1
            if selected < 0:
                if mode == "grouped":
                    for r in range(self.term_groups.count()):
                        f_idx = self.term_groups.item(r).data(Qt.ItemDataRole.UserRole)
                        if isinstance(f_idx, int) and allowed(f_idx):
                            selected = f_idx
                            break
                else:
                    selected = next((i for i in range(total) if allowed(i)), -1)

            self.findings.blockSignals(True)
            self.findings.setCurrentRow(selected)
            self.findings.blockSignals(False)
            self._sync_group_row_to_finding(selected)
        finally:
            self._syncing_term_selection = False

        if quiet or selected < 0:
            self._render_finding(selected)
            self._update_controls()
        else:
            self._select_finding(selected)

    def _render_finding(self, index: int) -> None:
        report = self._report
        finding = report["findings"][index] if report is not None and 0 <= index < len(report["findings"]) else None
        total = len(report["findings"]) if report is not None else 0
        self.finding_count.setText(strings.MANUSCRIPT_FINDING_COUNT.format(
            current=index + 1 if finding is not None else 0, total=total))
        self._update_term_list_visibility()
        self._render_inspector_hint()
        self._render_choice(index if finding is not None else -1)
        if finding is None:
            if total > 0 and self._term_filter == "unhandled":
                prompt = strings.MANUSCRIPT_TERM_FILTER_UNHANDLED_EMPTY.format(count=total)
            elif total > 0 and self._term_filter != "all":
                prompt = strings.MANUSCRIPT_TERM_FILTER_EMPTY
            elif total > 0:
                prompt = strings.MANUSCRIPT_FINDING_PROMPT
            elif report is not None:
                prompt = strings.MANUSCRIPT_NO_FINDINGS
            else:
                prompt = self.report_label.text()
            self.finding_title.setText(prompt)
            for label in (
                self.finding_verdict, self.finding_pair, self.finding_reason,
                self.finding_rule, self.finding_positions, self.candidates_label,
            ):
                label.clear()
            self.finding_verdict.hide()
            self.finding_pair.hide()
            self.finding_reason.hide()
            self.lookup_button.hide()
            self.promote_term_button.hide()
            self.manual_edit_label.hide()
            self.replace_toggle.hide()
            self.replace_toggle.setChecked(False)
            self.replacement_area.hide()
            return
        source = finding.get("source_span") or {}
        target = finding.get("target_span")
        verdict = finding["verdict"]
        reason = finding.get("reason") if verdict == "not_evaluated" else None
        self.finding_verdict.show()
        self.finding_title.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_title.setText(source.get("text", ""))
        # A saved result is not this session's verification: the badge names the
        # check this place waits for instead of a verdict nothing has confirmed.
        saved_result = 0 <= index < len(self._finding_saved) and self._finding_saved[index]
        self.finding_verdict.setText(strings.MANUSCRIPT_TERM_GROUP_STATUS_SAVED if saved_result
                                     else sr.localized_verdict_label(verdict, reason))
        recheck = 0 <= index < len(self._finding_recheck) and self._finding_recheck[index]
        self.finding_verdict.setProperty("reviewState", review_states.for_verdict(
            verdict, saved=saved_result, recheck=recheck))
        self.finding_verdict.style().unpolish(self.finding_verdict)
        self.finding_verdict.style().polish(self.finding_verdict)
        self.finding_pair.setText(target["text"] if isinstance(target, dict) and isinstance(target.get("text"), str) else "")
        self.finding_pair.setVisible(bool(self.finding_pair.text()))
        if reason:
            self.finding_reason.setText(sr.localized_not_evaluated_detail(reason) or "")
            self.finding_reason.setVisible(bool(self.finding_reason.text()))
        else:
            self.finding_reason.clear()
            self.finding_reason.hide()
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
        elif ref is None and report is not None and report.get("truncated"):
            notes.append(strings.MANUSCRIPT_DOCUMENT_TERMS_TRUNCATED.format(
                count=len(report["findings"])))
        self.candidates_label.setTextFormat(Qt.TextFormat.PlainText)
        if pairs or notes:
            self.candidates_label.setText("\n".join([strings.MANUSCRIPT_CANDIDATES, *pairs, *notes]))
            self.candidates_label.show()
        else:
            self.candidates_label.clear()
            self.candidates_label.hide()
        self.lookup_button.setVisible(bool(source.get("text")))
        self.promote_term_button.setVisible(bool(source.get("text")))
        reliable = self._target_span_reliable(target)
        skip_manual_notice = verdict == "not_evaluated" and reason in ("not_a_term", "ordinary_word")
        self.manual_edit_label.setVisible(not reliable and not skip_manual_notice)
        self.replace_toggle.setChecked(False)
        self.replace_toggle.setVisible(reliable and verdict not in ("verified_constraint", "not_evaluated"))
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

    def _apply_parts_edit(self, outcome: CommandOutcome, message: str) -> None:
        """A change of choices, history or finished marks as one undoable edit;
        the editors and their caret are left alone."""
        self._commit(outcome, editors=False)
        self.status_label.setText(message)
        if self._long_now():
            self._show_segment_term_report()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._refresh_segments()
        self._update_controls()

    def _finish_choice(self, outcome: CommandOutcome, message: str) -> None:
        """Show a choice or a restored history entry; a refusal says so instead."""
        if outcome.reason == REASON_FAILED:
            self.status_label.setText(strings.MANUSCRIPT_TERM_CHOICE_FAILED)
            return
        if outcome.reason == REASON_NOT_APPLICABLE:
            return
        self._apply_parts_edit(outcome, message)

    def _choose_term(self) -> None:
        if not self._input_ready() or self._term_basis is None:
            return
        ref = self._finding_record(self.findings.currentRow())
        if ref is None:
            return
        candidate_id = self.choice_combo.currentData()
        choice = sr.OFFICIAL_PAIR if candidate_id else sr.NOT_A_TERM
        self._finish_choice(self._controller.choose_term(*ref, choice, candidate_id or None),
                            strings.MANUSCRIPT_TERM_CHOICE_RECORDED)

    # -- ambiguous one-character names (ambiguous-mention-v1) -----------------
    def _ambiguous_toggled(self, _open: bool) -> None:
        self._render_ambiguous()

    def _ambiguous_choice(self, finding: dict, ref: tuple[int, int, int] | None) -> dict | None:
        """The owner's choice at an ambiguous name: the segment choice there, or
        the document choice whose span now reads it."""
        if ref is not None:
            segments = self._payload.get("segments") or []
            if ref[0] >= len(segments):
                return None
            return next((c for c in segments[ref[0]]["choices"]
                         if (c["source_span"]["start"], c["source_span"]["end"]) == ref[1:]), None)
        return self._controller.document_choice_at(finding)

    def _ambiguous_row_text(self, finding: dict, ref: tuple[int, int, int] | None) -> str:
        span = finding["source_span"]
        if ref is not None:
            text = strings.MANUSCRIPT_AMBIGUOUS_ITEM.format(text=span["text"], number=ref[0] + 1)
        else:
            text = strings.MANUSCRIPT_AMBIGUOUS_ITEM_DOCUMENT.format(text=span["text"], start=span["start"] + 1,
                                                                    end=span["end"])
        choice = self._ambiguous_choice(finding, ref)
        if choice is not None:
            staged = (strings.MANUSCRIPT_FINDING_CHOSEN if ref is not None and choice["confirmed"]
                      else strings.MANUSCRIPT_FINDING_RECONFIRM if ref is not None
                      else strings.MANUSCRIPT_AMBIGUOUS_STAGED)
            text += f"\n{self._describe_choice(choice)} · {staged}"
        return text

    def _render_ambiguous(self) -> None:
        """The folded list of ambiguous names, after the judged findings."""
        items = self._ambiguous
        count = len(items)
        expanded = self.ambiguous_toggle.isChecked()
        self.ambiguous_toggle.setText((strings.MANUSCRIPT_AMBIGUOUS_HIDE if expanded
                                       else strings.MANUSCRIPT_AMBIGUOUS_SHOW).format(count=count))
        self.ambiguous_toggle.setIcon(icon("chevron-down" if expanded else "chevron-right"))
        self.ambiguous_toggle.setVisible(bool(count))
        row = self.ambiguous_list.currentRow()
        self.ambiguous_list.blockSignals(True)
        self.ambiguous_list.clear()
        for index, (finding, ref) in enumerate(items):
            self.ambiguous_list.addItem(self._ambiguous_row_text(finding, ref))
            review_states.mark(self.ambiguous_list.item(index), review_states.UNEVALUATED,
                               label=strings.MANUSCRIPT_VERDICT_NOT_EVALUATED)
        self.ambiguous_list.setCurrentRow(row if 0 <= row < count else (0 if count else -1))
        self.ambiguous_list.blockSignals(False)
        self.ambiguous_area.setVisible(bool(count) and expanded)
        # As tall as its rows (a usual report has one or two), at most three.
        self.ambiguous_list.fit_height()
        self._render_ambiguous_actions()

    def _current_ambiguous(self) -> tuple[dict, tuple[int, int, int] | None] | None:
        row = self.ambiguous_list.currentRow()
        return self._ambiguous[row] if 0 <= row < len(self._ambiguous) else None

    def _ambiguous_choosable(self, ref: tuple[int, int, int] | None) -> bool:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        if composing or self._translating():
            return False
        if ref is not None:
            return self._term_basis is not None
        latest = self._controller.latest_trusted()
        return latest is not None and not latest["report"]["truncated"]

    def _render_ambiguous_actions(self) -> None:
        current = self._current_ambiguous()
        self.ambiguous_actions.setVisible(current is not None)
        self.ambiguous_combo.blockSignals(True)
        self.ambiguous_combo.clear()
        if current is None:
            self.ambiguous_combo.blockSignals(False)
            self.ambiguous_state.clear()
            self.ambiguous_state.hide()
            return
        finding, ref = current
        choice = self._ambiguous_choice(finding, ref)
        for candidate in finding["candidates"]:
            self.ambiguous_combo.addItem(self._candidate_text(candidate), candidate["candidate_id"])
        chosen = (choice or {}).get("candidate") or {}
        at = self.ambiguous_combo.findData(chosen.get("candidate_id"))
        self.ambiguous_combo.setCurrentIndex(max(0, at))
        self.ambiguous_combo.blockSignals(False)
        self._update_ambiguous_actions()
        free = self._ambiguous_choosable(ref)
        if not free and (ref is None or self._term_basis is None):
            state = strings.MANUSCRIPT_TERM_CHOICE_WAIT
        elif choice is None:
            state = strings.MANUSCRIPT_AMBIGUOUS_NONE
        else:
            state = strings.MANUSCRIPT_TERM_CHOICE_CHOSEN.format(choice=self._describe_choice(choice))
        self.ambiguous_state.setText(state)
        self.ambiguous_state.show()

    def _update_ambiguous_actions(self) -> None:
        """Only the enabled state; the candidates follow the selection."""
        current = self._current_ambiguous()
        free = current is not None and self._ambiguous_choosable(current[1])
        self.ambiguous_combo.setEnabled(free and self.ambiguous_combo.count() > 0)
        self.ambiguous_adopt_button.setEnabled(free and self.ambiguous_combo.count() > 0)
        self.ambiguous_ignore_button.setEnabled(free)

    def _adopt_ambiguous(self) -> None:
        current = self._current_ambiguous()
        if current is None:
            return
        wanted = self.ambiguous_combo.currentData()
        candidate = next((c for c in current[0]["candidates"] if c["candidate_id"] == wanted), None)
        if candidate is not None:
            self._choose_ambiguous(candidate)

    def _select_ambiguous(self, _row: int) -> None:
        self._render_ambiguous_actions()
        current = self._current_ambiguous()
        if current is None:
            return
        with self.scroll_sync.suspended():
            located = self.source_edit.select_raw_span(current[0]["source_span"])
        self.target_edit.setExtraSelections([])
        self.status_label.setText(strings.MANUSCRIPT_LOCATED if located else strings.MANUSCRIPT_PARTIAL_LOCATION)

    def _choose_ambiguous(self, candidate: dict | None) -> None:
        """Adopt a candidate pair, or mark the character an ordinary word. The
        choice goes out as a resolution with the next check."""
        current = self._current_ambiguous()
        if current is None or not self._input_ready():
            return
        finding, ref = current
        if not self._ambiguous_choosable(ref):
            return
        span = finding["source_span"]
        message = (strings.MANUSCRIPT_AMBIGUOUS_ADOPTED.format(zh=candidate["zh"], en=candidate["en"])
                   if candidate is not None else strings.MANUSCRIPT_AMBIGUOUS_IGNORED.format(text=span["text"]))
        row = self.ambiguous_list.currentRow()
        if ref is not None:
            choice = sr.OFFICIAL_PAIR if candidate is not None else sr.NOT_A_TERM
            self._finish_choice(self._controller.choose_term(
                *ref, choice, candidate["candidate_id"] if candidate is not None else None), message)
        else:
            latest = self._controller.latest_trusted()
            outcome = self._controller.choose_document(finding, candidate, view_state=self._editor_state())
            if outcome.reason == REASON_FULL:
                self.status_label.setText(strings.MANUSCRIPT_TERM_CHOICE_FAILED)
                return
            if not outcome.committed or latest is None:
                return
            self._commit(outcome, editors=False)
            # The text is what the report was made for: it stays listed, and
            # the staged choice goes out with the next check.
            self._show_term_report(latest["report"], state="current", refs=None)
            self.status_label.setText(message)
        if 0 <= row < self.ambiguous_list.count():
            self.ambiguous_list.setCurrentRow(row)

    def _unchoose_term(self) -> None:
        if not self._input_ready():
            return
        ref = self._finding_record(self.findings.currentRow())
        if ref is None or self._choice_at(self.findings.currentRow()) is None:
            return
        self._apply_parts_edit(self._controller.unchoose_term(*ref), strings.MANUSCRIPT_TERM_CHOICE_REMOVED)

    def _request_lookup(self) -> None:
        if not self._input_ready():
            return
        index = self.findings.currentRow()
        if self._report is not None and 0 <= index < len(self._report["findings"]):
            source = self._report["findings"][index].get("source_span") or {}
            term = source.get("text")
            if isinstance(term, str) and term:
                self.lookup_requested.emit(term)

    def _sync_primary_cta(self) -> None:
        translate_primary = not _blank(self._payload["source"]) and _blank(self._payload["target"])
        changed = False
        for button, target_name in (
            (self.translate_button, "primaryButton" if translate_primary else "commandButton"),
            (self.review_button, "commandButton" if translate_primary else "primaryButton"),
        ):
            if button.objectName() != target_name:
                button.setObjectName(target_name)
                button.style().unpolish(button)
                button.style().polish(button)
                changed = True
        if changed:
            icon_color = "accent-fg" if translate_primary else ""
            self.translate_button.setIcon(icon("languages", icon_color or None))
            self.translate_button.setProperty("iconColor", icon_color)

    def _update_controls(self) -> None:
        self._sync_translation()
        self._sync_primary_cta()
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
        self.term_groups.setEnabled(not composing)
        self.term_view_combo.setEnabled(not composing)
        self.term_filter_combo.setEnabled(not composing)
        self.jump_mismatch_button.setEnabled(not composing)
        self._render_mismatch_prompt()
        self.lookup_button.setEnabled(not composing)
        self.promote_term_button.setEnabled(not composing)
        self.promote_editor_button.setEnabled(not composing)
        self.replace_toggle.setEnabled(not composing)
        if hasattr(self, "find_bar") and self.find_bar.isVisible() and not self._suppress_find_refresh:
            self._update_find_controls()
        index = self.findings.currentRow()
        has_target = (self._report is not None and 0 <= index < len(self._report["findings"])
                      and self._target_span_reliable(self._report["findings"][index].get("target_span")))
        self.replace_button.setEnabled(bool(has_target and self.replacement_edit.text() and not composing))
        self.undo_button.setEnabled(self._controller.can_undo() and not composing)
        self.redo_button.setEnabled(self._controller.can_redo() and not composing)
        self.remove_choice_button.setEnabled(0 <= self.conflict_list.currentRow() < len(self._conflict_indexes))
        self.resolve_conflicts_save_button.setEnabled(not composing and not self._busy())
        self.export_prose_button.setEnabled(not composing)
        self._update_choice_actions()
        self._update_segment_actions()
        self.l1_run_button.setEnabled(bool(
            self._l1_sendable and self._api_client.is_configured and not composing and not self._translating()
            and not self._controller.structure_running() and not self._controller.structure_known_unavailable()))
        self._update_ambiguous_actions()
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
        self._schedule_recovery_save()

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
        self._controller.set_review_state(state, detail=detail, request_id=request_id)

    def _draft_snapshot(self) -> dict:
        """The undoable draft of the document (the controller's)."""
        return self._controller.draft_snapshot()

    def _editor_state(self) -> dict:
        return {"side": self._last_side, "source": self.source_edit.view_state(),
                "target": self.target_edit.view_state()}

    def _restore_editor_state(self, state: dict) -> None:
        editor = self.source_edit if state["side"] == "source" else self.target_edit
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        with self.scroll_sync.suspended():
            for side, widget in (("source", self.source_edit), ("target", self.target_edit)):
                widget.restore_view_state(state[side])

    def _commit(self, outcome: CommandOutcome, *, editors: bool) -> bool:
        """The host's side of a committed command: the editors, then the refresh.

        ``editors`` is False when the command's caller already put the text
        into the editors (``apply_target_write``, typing, a parts-only edit),
        which is what ``editors_already_updated`` used to say.
        """
        self._finish(outcome.edit, editors=editors)
        return outcome.committed

    def _finish(self, edit: EditOutcome | None, *, editors: bool) -> None:
        """Write a committed edit into the editors and refresh what it changed."""
        if edit is None or not edit.committed:
            return
        if self.status_label.text() == strings.MANUSCRIPT_SAVE_DONE:
            self.status_label.setText(strings.MANUSCRIPT_UNSAVED)
        if editors:
            self._set_payload(self._payload)
        self._after_edit(edit)

    def _after_edit(self, outcome: EditOutcome) -> None:
        """What a committed edit means for the workbench, in the order it means it.

        The results and the records have already moved with the edit (the
        controller), so this only says so: the direction's results are gone,
        the listed report is stale, the rows and the buttons follow.
        """
        if outcome.semantic_cleared:
            self.status_label.setText(strings.MANUSCRIPT_SEMANTIC_DIRECTION_CHANGED)
        if outcome.review_inputs_changed:
            self._invalidate_report()
        notice = self._alignment_notice_text(outcome)
        if notice:
            self.status_label.setText(notice)
        if self._conflict_mode:
            self._refresh_conflicts()
        # Layer 1 results follow the text: an edited segment's result goes at once.
        self._render_structure()
        self._refresh_semantic()
        if self.track_stack.currentIndex() == TRACK_SEGMENTS:
            self._derived_timer.start()
        self._update_controls()
        if hasattr(self, "find_bar") and self.find_bar.isVisible() and not self._suppress_find_refresh:
            self._refresh_find_matches(preserve_current=True)
        # The status bar's counts follow on the next pause, not every keystroke.
        self._caret_timer.start()
        self._my_terms_timer.start()
        self._tm_timer.start()
        # An earlier reuse count described text this edit just replaced.
        self._tm_fill_plan = []

    def _alignment_notice_text(self, outcome: EditOutcome) -> str:
        """What an edit did to the explicit alignments, in the workbench's words.

        Only the entries an edit crossed or whose source it deleted are
        invalidated; an entry whose translation it deleted keeps its source
        anchor so the segment can be translated again. Undo restores both.
        """
        parts = []
        if outcome.alignment_notice == NOTICE_ALL:
            parts.append(strings.MANUSCRIPT_ALIGNMENTS_CLEARED)
        elif outcome.alignment_notice == NOTICE_PARTIAL:
            parts.append(strings.MANUSCRIPT_ALIGNMENTS_PARTIAL.format(invalidated=outcome.invalidated,
                                                                      kept=outcome.kept))
        if outcome.emptied_targets:
            parts.append(strings.MANUSCRIPT_ALIGNMENTS_TARGET_EMPTIED.format(count=outcome.emptied_targets))
        if not parts:
            return ""
        recheck = self._records_with_input_change(self._payload["alignments"])
        if recheck:
            parts.append(strings.MANUSCRIPT_TERM_RECHECK_COUNT.format(count=recheck))
        return "".join(parts)

    def _apply_edit(self, value: dict, *, editors_already_updated: bool = False, edits: list | None = None,
                    exempt: sessions.LiveIssue | None = None, remap: AlignmentRemap | None = None,
                    alignment_reset: bool = False) -> None:
        editor_state = self._editor_state()
        if editors_already_updated:
            for side, *_range in edits or []:
                editor = self.source_edit if side == "source" else self.target_edit
                if editor.last_edit_view is not None:
                    editor_state[side] = editor.last_edit_view
        outcome = self._controller.apply_edit(value, view_state=editor_state, edits=edits, exempt=exempt,
                                              remap=remap, alignment_reset=alignment_reset)
        self._finish(outcome, editors=not editors_already_updated)

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
        """Drop the document review in flight; its answer is no longer wanted."""
        self._controller.cancel_pending()

    def cancel_background_work(self, *, clear_recovery_draft: bool = False) -> None:
        """Stop every request this view started (window close)."""
        self._recovery_timer.stop()
        if clear_recovery_draft and not self._recovery_pending:
            self._clear_recovery_draft()
        self._cancel_pending()
        self._stop_semantic()
        self._stop_translation()
        # A Layer 1 check in flight would otherwise answer into a closed view.
        self._controller.cancel_structure_check()

    @staticmethod
    def _has_recovery_content(payload: dict) -> bool:
        return any(bool(payload.get(k)) for k in ("source", "target", "choices", "history", "semantic_decisions"))

    def _check_startup_recovery(self) -> None:
        if self._recovery_path is None:
            return
        state = load_recovery(self._recovery_path)
        if state.status == "valid" and state.payload is not None and self._has_recovery_content(state.payload):
            self._recovery_pending = True
            message = (
                strings.RECOVERY_DRAFT_BANNER.format(time=state.saved_at)
                if state.saved_at
                else strings.RECOVERY_DRAFT_BANNER_NO_TIME
            )
            if state.dropped_conflicting_choices > 0:
                message += strings.RECOVERY_DROPPED_CONFLICTS_NOTE.format(
                    count=state.dropped_conflicting_choices
                )
            self.recovery_banner.show_message(
                message,
                SEVERITY_INFO,
                actions=[
                    (strings.RECOVERY_RESTORE_BUTTON, lambda s=state: self._restore_recovery_draft(s)),
                    (strings.RECOVERY_DISCARD_BUTTON, self._discard_recovery_draft),
                ],
                closable=True,
            )
        elif state.status == "corrupt":
            self._recovery_pending = True
            self.recovery_banner.show_message(
                strings.RECOVERY_DRAFT_CORRUPT_BANNER,
                SEVERITY_WARN,
                actions=[(strings.RECOVERY_DISCARD_BUTTON, self._discard_recovery_draft)],
                closable=True,
            )

    def _restore_recovery_draft(self, state: RecoveryState) -> None:
        if state.payload is None:
            return
        payload = _copy_payload(state.payload)
        if not self.source_edit.can_display(payload["source"]) or not self.target_edit.can_display(payload["target"]):
            self.status_label.setText(strings.MANUSCRIPT_UNSUPPORTED_TEXT)
            return
        self._recovery_pending = False
        self.recovery_banner.clear()
        self._replace_document(payload, (payload.get("progress") or {}).get("current"))
        self._controller.reset_known_texts(payload)
        self._controller.restore_invalidations(state.alignment_invalidations)
        self._path = Path(state.source_path) if state.source_path else None
        self._records()
        self._saved_payload = empty_workfile()
        self._set_review_state("history" if payload["history"] else "none")
        self._show_segment_term_report()
        self.status_label.setText(strings.RECOVERY_RESTORED_STATUS)
        self._update_controls()
        self._save_recovery_snapshot()

    def _discard_recovery_draft(self) -> None:
        self._recovery_pending = False
        self._recovery_timer.stop()
        self._clear_recovery_draft()
        self.recovery_banner.clear()

    def _clear_recovery_draft(self) -> None:
        if self._recovery_path is not None:
            clear_recovery(self._recovery_path)

    def _schedule_recovery_save(self) -> None:
        if self._recovery_path is None:
            return
        if self.document_dirty and self._has_recovery_content(self._payload):
            self._recovery_pending = False
            if self.recovery_banner.is_showing():
                self.recovery_banner.clear()
            self._recovery_timer.start()
        elif not self.document_dirty and not self._recovery_pending:
            self._recovery_timer.stop()
            self._clear_recovery_draft()

    def _save_recovery_snapshot(self) -> None:
        if self._recovery_path is None:
            return
        self._recovery_timer.stop()
        if self.document_dirty and self._has_recovery_content(self._payload):
            try:
                snapshot = _copy_payload(self._payload)
                if snapshot.get("semantic_decisions") or snapshot.get("semantic_history"):
                    snapshot.setdefault("semantic_history", [])
                    snapshot["semantic_texts"] = self._controller.semantic_texts(snapshot)
                    _normalize_semantic(snapshot)
                save_recovery(self._recovery_path, snapshot, source_path=self._path,
                              alignment_invalidations=self._controller.alignment_invalidations)
            except (OSError, WorkfileError):
                pass

    def _invalidate_report(self) -> None:
        previous = self._review_state
        self._cancel_pending()
        self._quiet_row = self.findings.currentRow()
        self._quiet_ref = self._finding_record(self._quiet_row)
        self._report = None
        self._finding_refs, self._finding_recheck, self._finding_saved = [], [], []
        self._term_groups_data, self._finding_group_status = [], []
        self._ambiguous = []
        self._render_ambiguous()
        self.findings.clear()
        self.term_groups.clear()
        self.term_toolbar.hide()
        self.term_groups.hide()
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
        self._controller.reset_term_session()
        self._segment_cursor = cursor if isinstance(cursor, int) else None
        self._pending_segment_row = None
        self.segment_list.setCurrentRow(-1)
        self._render_structure()

    def _on_edit(self, *_args, edit: tuple | None = None) -> None:
        if self._updating:
            return
        before = self._draft_snapshot()
        direction = self.direction_combo.currentData()
        if direction != before["direction"]:
            # A new direction clears every explicit alignment (R7).
            self._finish(self._controller.set_direction(direction, view_state=self._editor_state()),
                         editors=False)
            return
        texts = {"source": self.source_edit.raw_text(), "target": self.target_edit.raw_text()}
        if texts["source"] == before["source"] and texts["target"] == before["target"]:
            if edit is not None:
                # Text rewritten with identical text: still an edit of that range.
                self._controller.note_edit(edit)
                self._refresh_semantic()
                self._update_controls()
            return
        for side, editor in (("source", self.source_edit), ("target", self.target_edit)):
            if texts[side] == self._payload[side]:
                continue
            reported = tuple(edit[1:]) if edit is not None and edit[0] == side else None
            view_state = self._editor_state()
            if reported is not None and editor.last_edit_view is not None:
                view_state[side] = editor.last_edit_view
            outcome = self._controller.edit_text(side, texts[side], edit=reported, view_state=view_state)
            self._finish(outcome, editors=False)

    def _start_review(self) -> None:
        """Start the document review from the text as it is now."""
        if not self.review_button.isEnabled() or not self._input_ready():
            return
        self._controller.start_review()

    def _use_auto_alignment(self) -> None:
        if self._payload["alignments"] is None or not self._input_ready():
            return
        recheck = self._records_with_input_change(None)
        value = self._draft_snapshot()
        value["alignments"] = None
        self._apply_edit(value, editors_already_updated=True, alignment_reset=True)
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

    def _show_term_report(self, report: dict, *, state: str = "current", refs: list | None = None,
                          recheck: list | None = None, saved: list | None = None,
                          ambiguous: list | None = None) -> None:
        """List a report's findings. ``refs``, ``recheck`` and ``saved`` belong to a
        long manuscript's findings: their records, whether each awaits a re-check,
        and whether each comes from a saved result whose basis this session has
        not checked against the live server yet; ``ambiguous`` holds its
        ambiguous one-character names. A document report's ambiguous names are
        taken out here: they are listed apart, folded, and never counted with
        the judged findings (manuscript-v5 SPEC "Reason ambiguous_mention")."""
        if refs is None:
            held = [f for f in report["findings"] if is_ambiguous_mention(f)]
            if held:
                coverage = report.get("coverage") or {}
                report = {**report, "findings": [f for f in report["findings"] if not is_ambiguous_mention(f)]}
                if isinstance(coverage.get("not_evaluated"), int):
                    report["coverage"] = {**coverage, "not_evaluated": max(0, coverage["not_evaluated"] - len(held))}
            self._ambiguous = [(finding, None) for finding in held]
        else:
            self._ambiguous = list(ambiguous or [])
        row = self.findings.currentRow()
        prev_ref = self._finding_record(row) if row >= 0 else self._quiet_ref
        # Rebuilt from the records (after an edit, an action or a segment of a
        # run): the list keeps its place without locating anything, so the
        # caret and the status line stay as they are.
        quiet = refs is not None
        if quiet and prev_ref is not None and refs and prev_ref in refs:
            row = refs.index(prev_ref)
        elif quiet and row < 0:
            row = self._quiet_row
        self._report = report
        # A report is where the dictionary revision comes from: the memory's
        # staleness reading follows it, without waiting for the next caret stop.
        self._refresh_tm_panel()
        self._finding_refs = list(refs or [])
        self._finding_recheck = list(recheck or [False] * len(report["findings"]))
        self._finding_saved = list(saved or [False] * len(report["findings"]))
        self._set_review_state(state, request_id=report["request_id"])
        self.findings.blockSignals(True)
        self.findings.clear()
        for index, finding in enumerate(report["findings"]):
            source_span = finding.get("source_span") or {}
            target_span = finding.get("target_span") or {}
            target_text = target_span.get("text")
            verdict = sr.localized_verdict_label(
                finding["verdict"],
                finding.get("reason") if finding["verdict"] == "not_evaluated" else None,
            )
            if self._finding_saved[index]:
                # A saved result is not this session's verification: the row says
                # that instead of naming a verdict it cannot vouch for.
                verdict = strings.MANUSCRIPT_TERM_SAVED_NOTE
            second_line = f"{target_text} · {verdict}" if target_text else verdict
            choice = self._choice_at(index)
            if choice is not None:
                second_line += " · " + (strings.MANUSCRIPT_FINDING_CHOSEN if choice["confirmed"]
                                        else strings.MANUSCRIPT_FINDING_RECONFIRM)
            if self._finding_recheck[index]:
                second_line += " · " + strings.MANUSCRIPT_FINDING_RECHECK
            self.findings.addItem(f'{source_span.get("text", "")}\n{second_line}')
            review_states.mark(self.findings.item(index), review_states.for_verdict(
                finding["verdict"], saved=self._finding_saved[index], recheck=self._finding_recheck[index]))
        self.findings.blockSignals(False)

        if refs is not None:
            records = self._records() or []
            groups = sr.group_term_occurrences(
                records, self._current_pairs(),
                self._payload["source"], self._payload["target"], self._payload["direction"],
                alignments=self._payload["alignments"], basis=self._term_basis,
                features=self._controller.review_features(), file_features=self._payload.get("review_features"),
            )
        else:
            groups = self._build_document_term_groups(report)
        self._term_groups_data = groups
        matched_by_index: list[set[str]] = [set() for _ in report["findings"]]
        for grp in groups:
            keys = {key for key in _TERM_FILTER_KEYS if sr.group_matches_filter(grp, key)}
            for occ in grp["occurrences"]:
                f_idx = occ["finding_index"]
                if 0 <= f_idx < len(matched_by_index):
                    matched_by_index[f_idx] = keys
        if not groups and report["findings"]:
            for idx, f in enumerate(report["findings"]):
                if f["verdict"] == "not_evaluated":
                    status = "not_evaluated"
                elif f["verdict"] == "verified_constraint" and state == "current":
                    status = "confirmed"
                else:
                    status = "pending"
                matched_by_index[idx] = {"all", status} if status == "confirmed" else {"all", "unhandled", status}
        self._finding_group_status = matched_by_index

        preferred = row if 0 <= row < len(report["findings"]) and (quiet or self._term_mode == "segments") else -1
        self._refresh_term_lists(quiet=quiet, preferred_index=preferred)
        self._render_ambiguous()

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
        findings, refs, recheck, saved_flags, ambiguous = [], [], [], [], []
        evaluated = not_evaluated = shown = 0
        truncated = False
        features = self._controller.review_features()
        for index, record in enumerate(records):
            view = self._record_views.get(record, payload, basis, features)
            state = view.state
            if not view.content_current:
                continue
            report = sr.expand_result(record, source, target)
            for finding in report["findings"]:
                placed = {**finding, "source_span": _offset(finding["source_span"], record["source_start"]),
                          "target_span": _offset(finding["target_span"], record["target_start"])}
                ref = (index, finding["source_span"]["start"], finding["source_span"]["end"])
                if is_ambiguous_mention(finding):
                    # Listed apart, folded, never counted with the judged findings.
                    ambiguous.append((placed, ref))
                    not_evaluated -= 1
                    continue
                findings.append(placed)
                refs.append(ref)
                recheck.append(state == "stale")
                saved_flags.append(state == "saved")
            evaluated += report["coverage"]["evaluated"]
            not_evaluated += report["coverage"]["not_evaluated"]
            truncated = truncated or report["truncated"]
            shown += 1
        if not shown:
            return
        self._show_term_report({
            "request_id": None, "findings": findings,
            "coverage": {"evaluated": evaluated, "not_evaluated": max(0, not_evaluated),
                         "rules": list(sr.COVERAGE_RULES)},
            "truncated": truncated,
        }, state="saved" if basis is None else "current", refs=refs, recheck=recheck, saved=saved_flags,
            ambiguous=ambiguous)

    def _cancel_review(self) -> None:
        """The cancel button: pause what is in flight, in the order a press means
        it (see DocumentRunController.cancel_review)."""
        self._controller.cancel_review()

    # -- segmented term and semantic run --------------------------------------
    def _start_semantic_run(self) -> None:
        """Start the serial segmented run (see the controller)."""
        self._controller.start_semantic_run()

    def _stop_semantic(self, state: str | None = None) -> None:
        """Hard stop of the segmented run; results already stored stay."""
        self._controller.stop_semantic(state)

    def _clear_semantic(self) -> None:
        """A new document, direction or service: no semantic result survives."""
        self._controller.clear_semantic()

    def _basis(self) -> sessions.Basis:
        """The current basis of the semantic reuse rule (``sessions.Basis``)."""
        return self._controller.basis()

    def _alignments_for(self, source_core: tuple[int, int], target_core: tuple[int, int]) -> list | None:
        """The owner's correspondence for one segment's term request
        (``segment_records.alignment_list``): automatic stays automatic, an
        explicit list, even empty, never becomes automatic."""
        return self._controller.alignments_for(source_core, target_core)

    def _semantic_texts(self, payload: dict) -> list[dict]:
        return self._controller.semantic_texts(payload)

    # -- translation in the manuscript ---------------------------------------
    def _translate_ready(self) -> bool:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        return bool(self._api_client.is_configured and not self._busy() and not composing
                    and not _blank(self._payload["source"]))

    def _stop_translation(self) -> None:
        """Hard stop: nothing is written and nothing is offered afterwards."""
        self._controller.stop_translation()

    def _resumable(self) -> _Translation | None:
        """The stopped run, while the text is exactly what it left."""
        return self._controller.resumable()

    def _sync_translation(self) -> None:
        """An edit, an undo or a redo withdraws the offer to continue."""
        previous = self._controller.withdraw_translation_offer()
        if previous is not None and self.status_label.text() == previous:
            self.status_label.setText(strings.MANUSCRIPT_REPORT_NOTE)

    def _render_translate_offer(self) -> None:
        """Both translate buttons say what the next press does."""
        offer = {"paused": strings.MANUSCRIPT_TRANSLATE_CONTINUE,
                 "failed": strings.MANUSCRIPT_TRANSLATE_RETRY}.get(self._translate_state)
        button_offer = offer or (
            strings.MANUSCRIPT_TRANSLATE_RESUME if self._controller.has_resumable_remainder() else None
        )
        button = self.translate_button
        if button.text() != (button_offer or ""):
            button.setText(button_offer or "")
            if button_offer:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)  # Qt's QWIDGETSIZE_MAX: the icon button grows to fit its label
            else:
                button.setFixedWidth(34)
        button.setToolTip(f"{button_offer or strings.MANUSCRIPT_TRANSLATE}（Ctrl+Shift+T）")
        button.setAccessibleName(button_offer or strings.MANUSCRIPT_TRANSLATE)
        pair = self._current_segment()
        segment_default = (
            strings.MANUSCRIPT_RETRANSLATE_SEGMENT
            if pair is not None and pair.status == STATUS_READY
            else strings.MANUSCRIPT_TRANSLATE_SEGMENT
        )
        self.translate_segment_button.setText(offer or segment_default)

    def _translate(self) -> None:
        """Translate a blank translation or resume remaining untranslated paragraphs;
        never overwrite text the owner already has."""
        if not self._input_ready() or not self._translate_ready():
            return
        self._controller.start_translation()

    def _translate_segment(self) -> None:
        """Translate the selected untranslated segment or re-translate a ready one."""
        if not self._input_ready() or not self._translate_ready():
            return
        self._controller.start_translation(single_segment=True)

    def _render_mismatch_prompt(self) -> None:
        if not self._mismatch_prompt_active:
            if not self.mismatch_banner.isHidden():
                self.mismatch_banner.hide()
            return
        pairs = self._cached_pairs()
        if pairs is None:
            pairs = self._current_pairs()
        count = sum(1 for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT)
        if count <= 0:
            self._mismatch_prompt_active = False
            self.mismatch_banner.hide()
            return
        self.mismatch_label.setText(strings.MANUSCRIPT_TRANSLATED_MISMATCH.format(count=count))
        if self.mismatch_banner.isHidden():
            self.mismatch_banner.show()

    def _jump_first_mismatch(self) -> None:
        if not self._input_ready():
            return
        pairs = self._current_pairs()
        target_pair = next((p for p in pairs if p.status == STATUS_NEEDS_MANUAL_SPLIT), None)
        if target_pair is None:
            self._mismatch_prompt_active = False
            self.mismatch_banner.hide()
            self.status_label.setText(strings.MANUSCRIPT_NO_PENDING_SEGMENT)
            return
        if self.inspector.isHidden():
            self._inspector_requested = True
            self.inspector.show()
            self._update_inspector_button()
        self._show_track(TRACK_SEGMENTS)
        if self.segment_list.currentRow() == target_pair.index:
            self._segment_cursor = target_pair.index
            self._render_segment(target_pair)
            self._update_tracks()
            self._select_cores(target_pair)
        else:
            self.segment_list.setCurrentRow(target_pair.index)
        self.status_label.setText(strings.MANUSCRIPT_MISMATCH_JUMPED.format(number=target_pair.index + 1))

    def _select_finding(self, index: int) -> None:
        self._sync_group_row_to_finding(index)
        self._render_finding(index)
        if self._report is None or not 0 <= index < len(self._report["findings"]):
            self._update_controls()
            return
        finding = self._report["findings"][index]
        with self.scroll_sync.suspended():
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
        remap = remap_splice(value["alignments"], "target", value["source"], changed,
                             span["start"], span["end"], len(replacement))
        value["alignments"] = remap.alignments
        self._apply_edit(value, edits=[("target", span["start"], span["end"], len(replacement))], remap=remap)
        self.status_label.setText(strings.MANUSCRIPT_REPLACED)
        self._update_controls()

    def _undo_replace(self) -> None:
        self._step_history(undo=True)

    def _redo_edit(self) -> None:
        self._step_history(undo=False)

    def _step_history(self, *, undo: bool) -> None:
        """Undo and redo restore text, decisions and the semantic positions together."""
        if not (self._controller.can_undo() if undo else self._controller.can_redo()) or not self._input_ready():
            return
        outcome = (self._controller.undo(view_state=self._editor_state()) if undo
                   else self._controller.redo(view_state=self._editor_state()))
        if outcome is None:
            return
        self._suppress_find_refresh = True
        try:
            self._finish(outcome.edit, editors=True)
            self.status_label.setText(strings.MANUSCRIPT_UNDONE if undo else strings.MANUSCRIPT_REDONE)
            self._update_controls()
            if outcome.entry.view is not None:
                self._restore_editor_state(outcome.entry.view)
        finally:
            self._suppress_find_refresh = False
        if hasattr(self, "find_bar") and self.find_bar.isVisible():
            self._refresh_find_matches(preserve_current=True)

    def _set_payload(self, value: dict) -> None:
        self._updating = True
        try:
            with self.scroll_sync.suspended():
                self.source_edit.set_raw_text(value["source"])
                self.target_edit.set_raw_text(value["target"])
            self.direction_combo.setCurrentIndex(self.direction_combo.findData(value["direction"]))
            self._payload = _normalize_semantic(_copy_payload(value))
            self._document_revision += 1
        finally:
            self._updating = False
        # The status bar's counts follow a replaced text after the same pause.
        self._caret_timer.start()
        self._my_terms_timer.start()
        self._tm_timer.start()
        self._tm_fill_plan = []

    def _refresh_my_terms(self) -> None:
        """The person's own terms found in the source text now on screen."""
        self._my_terms_timer.stop()
        self.my_terms.set_context(self._payload["source"], self._payload["direction"])

    def show_termbase_dialog(
        self,
        _checked: bool = False,
        *,
        source: str = "",
        target: str = "",
        direction: str | None = None,
        kind: str | None = None,
        note: str = "",
    ) -> None:
        """Manage the termbase; the list of hits follows every change."""
        dialog = TermbaseDialog(self._termbase_store, self)
        if source or target or direction is not None or kind is not None or note:
            dialog.prefill(
                source=source,
                target=target,
                direction=direction or self._payload["direction"],
                kind=kind or "preferred_term",
                note=note,
            )
        dialog.changed.connect(self._refresh_my_terms)
        dialog.exec()
        self._refresh_my_terms()

    # -- translation memory (confirmed pairs, a reference) --------------------
    def show_tm_dialog(self, _checked: bool = False) -> None:
        """Manage the confirmed pairs; the listed candidates follow every change."""
        memory = self._translation_memory
        if memory is None:
            self.status_label.setText(strings.TM_PANEL_NO_MEMORY)
            return
        dialog = TranslationMemoryDialog(memory, self)
        dialog.changed.connect(self._refresh_tm_panel)
        dialog.exec()
        self._refresh_tm_panel()

    def _terms_db_revision(self) -> str | None:
        """The dictionary revision the open document's review named, if any.

        It is the only revision this client can know: the memory compares an
        entry's recorded revision with it, and an entry that cannot say which
        dictionary it was confirmed under is listed as a reference.
        """
        # A document report names it; a long manuscript keeps the basis per
        # record, and the controller's common basis is the same fact for the
        # dictionary. Either is the revision to compare an entry with.
        for source in (self._report, self._term_basis):
            if not isinstance(source, dict):
                continue
            dictionary = source.get("dictionary")
            revision = dictionary.get("revision") if isinstance(dictionary, dict) else None
            if isinstance(revision, str) and revision:
                return revision
        return None

    def _game_version(self) -> str | None:
        """The dictionary data/game version from the open document's review, if any.

        Uses ``game_version`` when present, otherwise ``dictionary.source_commit``
        (which ``/v1/reviews`` actually supplies). Kept consistent between storing
        confirmed entries and querying freshness so no false match is ever formed.
        """
        for source in (self._report, self._term_basis):
            if not isinstance(source, dict):
                continue
            dictionary = source.get("dictionary")
            if not isinstance(dictionary, dict):
                continue
            for key in ("game_version", "source_commit"):
                value = dictionary.get(key)
                if isinstance(value, str) and value.strip() and len(value) <= tm.REVISION_SCALARS[1]:
                    return value
        return None

    def _active_segment_pair(self):
        """The segment the memory is read for: the selected row, else the caret's."""
        pairs = self._current_pairs()
        if not pairs:
            return None
        pair = self._current_segment()
        if pair is not None:
            return pair
        side = self._caret_side
        return pairs[self._pair_index_at(pairs, side, self._caret_offset(side))]

    @staticmethod
    def _memory_text(text: str) -> str | None:
        """A text the store can hold, or None where it cannot.

        The store refuses a blank value and one longer than ``SOURCE_SCALARS``;
        such a neighbour is no context it can hash, which is what None means
        here. Passing the text anyway would raise a refused-value error, and the
        workbench would report the memory file as broken.
        """
        return text if text.strip() and len(text) <= tm.SOURCE_SCALARS[1] else None

    def _active_segment_context(self) -> ActiveSegment | None:
        """The active segment's texts and neighbours, as the memory wants them."""
        pair = self._active_segment_pair()
        if pair is None:
            return None
        pairs = self._current_pairs()
        source, target = self._payload["source"], self._payload["target"]
        index = pair.index

        def core(text: str, region) -> str:
            return text[region.core_start:region.core_end]

        def neighbour(position: int) -> str | None:
            if not 0 <= position < len(pairs):
                return None
            return self._memory_text(core(source, pairs[position].source))

        return ActiveSegment(
            source=core(source, pair.source),
            target=core(target, pair.target),
            direction=self._payload["direction"],
            prev_source=neighbour(index - 1),
            next_source=neighbour(index + 1),
            terms_db_revision=self._terms_db_revision(),
            game_version=self._game_version(),
        )

    def _refresh_tm_panel(self) -> None:
        """Re-read the memory for the active segment. Reads only."""
        self._tm_timer.stop()
        # A fill plan belongs to the text it counted; a refresh is a new reading.
        self._tm_fill_plan = []
        self.tm_panel.set_context(self._active_segment_context())

    def _memory_problem(self) -> str | None:
        """Why the memory cannot be written to, in the workbench's words."""
        memory = self._translation_memory
        if memory is None:
            return strings.TM_PANEL_NO_MEMORY
        if not memory.usable:
            return strings.TM_PANEL_UNUSABLE.format(reason=tm_refusal_text(memory.error))
        return None

    def _apply_tm_match(self, match) -> None:
        """Adopt one confirmed candidate into the active segment's target.

        One edit and one history step: Ctrl+Z puts the previous translation
        back, including the alignments the edit moved. A candidate that only
        references (its numbers changed, or it was confirmed under another
        dictionary) is still the person's choice; the status line says what
        the memory warned about it.
        """
        if not self._input_ready():
            return
        problem = self._memory_problem()
        if problem is not None:
            self.status_label.setText(problem)
            return
        pair = self._active_segment_pair()
        if pair is None:
            self.status_label.setText(strings.TM_PANEL_APPLY_NO_SEGMENT)
            return
        text = match.entry.target_text
        raw = self._payload["target"]
        start, end = pair.target.core_start, pair.target.core_end
        if start == end:
            # An untranslated segment: this is a fill, and a fill is what tells
            # the alignment which entry it completes (remap_for_target_fill). A
            # plain splice here would leave that entry's target empty and the
            # segment would come back as needing a manual split.
            left = "\n\n" if start > 0 and raw[start - 1] not in WHITESPACE else ""
            right = "\n\n" if start < len(raw) and raw[start] not in WHITESPACE else ""
            inserted = left + text + right
            changed = raw[:start] + inserted + raw[start:]
            fill_start = start + len(left)
            remap = remap_for_target_fill(self._payload["alignments"], self._payload["source"], changed,
                                          pair.source.start, pair.source.end, start,
                                          fill_start, fill_start + len(text), len(inserted))
            edit = ("target", start, start, len(inserted))
        else:
            changed = raw[:start] + text + raw[end:]
            remap = remap_splice(self._payload["alignments"], "target", self._payload["source"], changed,
                                 start, end, len(text))
            edit = ("target", start, end, len(text))
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value["target"] = changed
        value["alignments"] = remap.alignments
        self._apply_edit(value, edits=[edit], remap=remap)
        if match.needs_recheck:
            self.status_label.setText(strings.TM_PANEL_APPLIED_RECHECK.format(
                reasons="、".join(stale_labels(match.stale_reasons))))
        else:
            self.status_label.setText(strings.TM_PANEL_APPLIED)
        self._update_controls()

    def _confirm_segment_to_memory(self) -> None:
        """Record the active segment as a confirmed pair, because the person said so.

        This is the only path from the workbench into the memory: a finished
        translation, a review verdict or an accepted suggestion never writes
        one, so unconfirmed machine output cannot become a trusted pair.
        """
        if not self._input_ready():
            return
        problem = self._memory_problem()
        if problem is not None:
            self.status_label.setText(problem)
            return
        active = self._active_segment_context()
        if active is None:
            self.status_label.setText(strings.TM_PANEL_CONFIRM_NO_SEGMENT)
            return
        if not active.source.strip() or not active.target.strip():
            self.status_label.setText(strings.TM_PANEL_CONFIRM_EMPTY)
            return
        if len(active.source) > tm.SOURCE_SCALARS[1]:
            self.status_label.setText(strings.TM_PANEL_CONFIRM_TOO_LONG.format(
                length=len(active.source), max=tm.SOURCE_SCALARS[1]))
            return
        try:
            entry = self._translation_memory.add(
                active.source,
                active.target,
                active.direction,
                prev_source=active.prev_source,
                next_source=active.next_source,
                origin=tm.TM_ORIGIN_HUMAN,
                terms_db_revision=active.terms_db_revision,
                game_version=active.game_version,
            )
        except (tm.TmError, OSError) as error:
            self.status_label.setText(strings.TM_PANEL_CONFIRM_FAILED.format(reason=tm_refusal_text(error)))
            self._refresh_tm_panel()
            return
        self._refresh_tm_panel()
        self.status_label.setText(strings.TM_PANEL_CONFIRMED.format(count=entry.use_count))

    def _classify_reuse(self, sources: list[str]) -> list[tm.TmSegmentClassification | None]:
        """Classify the segments the store can hold, keeping each one's neighbours.

        ``classify_segments_for_tm`` refuses a blank or over-long source (a value
        it cannot store, which is not a broken file), so those segments are
        skipped and the rest are classified in runs. Each run is given its
        neighbours explicitly: a skipped neighbour cannot be hashed by the store
        either, so it is passed as none - the truth about the context - instead
        of the text that would be refused.
        """
        def readable(text: str) -> bool:
            return self._memory_text(text) is not None

        def neighbour(index: int) -> str | None:
            return self._memory_text(sources[index]) if 0 <= index < len(sources) else None

        classes: list[tm.TmSegmentClassification | None] = [None] * len(sources)
        run: list[int] = []

        def flush() -> None:
            if not run:
                return
            items = [{"source": sources[index], "prev_source": neighbour(index - 1),
                      "next_source": neighbour(index + 1)} for index in run]
            found = self._translation_memory.classify_segments_for_tm(
                items,
                self._payload["direction"],
                current_terms_db_revision=self._terms_db_revision(),
                current_game_version=self._game_version(),
            )
            for index, item in zip(run, found):
                classes[index] = item
            run.clear()

        for position, text in enumerate(sources):
            if readable(text):
                run.append(position)
            else:
                flush()
        flush()
        return classes

    def _check_memory_reuse(self) -> None:
        """Count what the memory offers for the current pairing. Reads the memory only.

        A segment counts as fresh when the same text (with the same neighbours,
        or anywhere) was confirmed under this document's dictionary revision;
        anything else is a reference, which is never filled silently.
        """
        problem = self._memory_problem()
        if problem is not None:
            self.status_label.setText(problem)
            return
        pairs = self._current_pairs()
        if not pairs:
            self.status_label.setText(strings.TM_PANEL_CHECK_EMPTY)
            return
        source = self._payload["source"]
        sources = [source[pair.source.core_start:pair.source.core_end] for pair in pairs]
        if all(not text.strip() for text in sources):
            self.status_label.setText(strings.TM_PANEL_CHECK_NO_SOURCE)
            return
        try:
            classes = self._classify_reuse(sources)
        except (tm.TmError, OSError) as error:
            self.status_label.setText(strings.TM_PANEL_UNUSABLE.format(reason=tm_refusal_text(error)))
            return
        classified = [item for item in classes if item is not None]
        # Only context-exact + verified-fresh entries qualify for trusted batch reuse;
        # context-less exact matches (TM_STATUS_EXACT_FRESH) stay reference-only.
        fresh = sum(1 for item in classified if item.status == tm.TM_STATUS_CONTEXT_EXACT_FRESH)
        stale = sum(
            1 for item in classified
            if item.status in (tm.TM_STATUS_EXACT_FRESH, tm.TM_STATUS_STALE_OR_NUMERIC_REFERENCE)
        )
        unmatched = len(classified) - fresh - stale
        skipped = len(classes) - len(classified)
        self._tm_fill_plan = []
        for item, pair in zip(classes, pairs):
            if item is None or item.status != tm.TM_STATUS_CONTEXT_EXACT_FRESH:
                continue
            # Only a segment with no translation at all: filling one that has
            # text would replace somebody's work without them choosing it.
            if pair.target.core_end > pair.target.core_start:
                continue
            text = item.match.entry.target_text if item.match is not None else ""
            if text.strip():
                self._tm_fill_plan.append((pair.target.core_start, pair.target.core_end, text,
                                           pair.source.start, pair.source.end))
        self.tm_panel.show_reuse(ReuseSummary(fresh=fresh, stale=stale, unmatched=unmatched,
                                              fillable=len(self._tm_fill_plan), skipped=skipped))
        note = strings.TM_PANEL_CHECK_SUMMARY.format(fresh=fresh, stale=stale, unmatched=unmatched)
        if skipped:
            note += strings.TM_PANEL_CHECK_SKIPPED.format(n=skipped, max=tm.SOURCE_SCALARS[1])
        self.status_label.setText(note)

    def _fill_from_memory(self) -> None:
        """Fill the empty targets the reuse check certified, as one undoable edit."""
        if not self._input_ready():
            return
        if not self._tm_fill_plan:
            self.status_label.setText(strings.TM_PANEL_CHECK_NOTHING)
            return
        # Target position, then the source position: several untranslated
        # segments can share one target offset, and sorting on the translation
        # text would write them in the wrong order (and fail the fill remap).
        plan = sorted(self._tm_fill_plan, key=lambda item: (item[0], item[1], item[3], item[4]))
        raw = self._payload["target"]
        source = self._payload["source"]
        changed = raw
        for start, end, text, _source_start, _source_end in reversed(plan):
            changed = changed[:start] + text + changed[end:]
        if not self.target_edit.can_display(changed):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        # Each fill is its own transaction against the text it lands in, in
        # order, as a translation run writes one segment at a time; what the
        # transactions invalidated is reported together, as one edit step.
        alignments = self._payload["alignments"]
        current = raw
        shift = 0
        invalidated: list[dict] = []
        emptied: list[dict] = []
        for start, end, text, source_start, source_end in plan:
            position = start + shift
            filled = current[:position] + text + current[end + shift:]
            remap = remap_for_target_fill(alignments, source, filled, source_start, source_end, position,
                                          position, position + len(text), len(text))
            alignments = remap.alignments
            invalidated.extend(remap.invalidated)
            emptied.extend(remap.emptied_targets)
            current = filled
            shift += len(text) - (end - start)
        value = self._draft_snapshot()
        value["target"] = changed
        value["alignments"] = alignments
        # Every edit is read in the text the ones before it left, so the list
        # runs from the last segment back to the first: a segment's own offset
        # is still the one the document gave it, and an insertion never moves
        # an edit that has already been applied.
        edits = [("target", start, end, len(text)) for start, end, text, _s, _e in reversed(plan)]
        self._apply_edit(value, edits=edits, remap=AlignmentRemap(alignments, tuple(invalidated), tuple(emptied)))
        self._tm_fill_plan = []
        self.tm_panel.clear_reuse()
        l1_started = self._controller.start_structure_check() if self._api_client.is_configured else False
        if l1_started:
            self.status_label.setText(strings.TM_PANEL_CHECK_FILLED_CHECKING.format(n=len(plan)))
        elif not self._api_client.is_configured:
            self.status_label.setText(strings.TM_PANEL_CHECK_FILLED_OFFLINE.format(n=len(plan)))
        elif self._controller.structure_known_unavailable():
            self.status_label.setText(strings.TM_PANEL_CHECK_FILLED_UNAVAILABLE.format(n=len(plan)))
        else:
            self.status_label.setText(strings.TM_PANEL_CHECK_FILLED.format(n=len(plan)))
        self._update_controls()

    def _promote_from_term_finding(self) -> None:
        if not self._input_ready():
            return
        index = self.findings.currentRow()
        if self._report is None or not 0 <= index < len(self._report["findings"]):
            return
        finding = self._report["findings"][index]
        direction = self._payload["direction"]
        source_text = (finding.get("source_span") or {}).get("text") or ""
        target_text = self.replacement_edit.text().strip()
        if not target_text:
            target_span = finding.get("target_span")
            if isinstance(target_span, dict) and isinstance(target_span.get("text"), str):
                target_text = target_span["text"]
            elif len(finding.get("candidates") or ()) == 1:
                target_text = finding["candidates"][0].get(direction, "")
        self.show_termbase_dialog(
            source=source_text,
            target=target_text,
            direction=direction,
            kind="preferred_term",
        )

    def _promote_from_semantic_issue(self) -> None:
        if not self._input_ready():
            return
        issue = self._current_issue()
        if issue is None:
            return
        direction = self._payload["direction"]
        source_text = (
            issue.source_span["text"]
            if issue.source_span and issue.source_span.get("text")
            else (issue.issue.get("source_excerpt") or "")
        )
        decision = sessions.decision_for(issue, self._controller.decision_index())
        option = self._selected_option(issue)
        if self.adjust_area.isVisible() and self.adjust_edit.toPlainText().strip():
            target_text = self.adjust_edit.toPlainText().strip()
        elif decision is not None and decision.get("applied_text"):
            target_text = decision["applied_text"]
        elif option is not None:
            target_text = option[0]
        elif issue.target_span and issue.target_span.get("text"):
            target_text = issue.target_span["text"]
        else:
            target_text = issue.issue.get("target_excerpt") or ""
        self.show_termbase_dialog(
            source=source_text,
            target=target_text,
            direction=direction,
            kind="preferred_term",
        )

    def _promote_from_editor(self, side: str | None = None) -> None:
        if not self._input_ready():
            return
        source_selected = self.source_edit.selected_raw_text().strip()
        target_selected = self.target_edit.selected_raw_text().strip()
        if not source_selected and not target_selected:
            match = self._search_controller.current_match()
            if match is not None:
                if match.side == "source":
                    source_selected = match.text.strip()
                else:
                    target_selected = match.text.strip()
            elif self.track_stack.currentIndex() == TRACK_SEMANTIC and self._current_issue() is not None:
                self._promote_from_semantic_issue()
                return
            elif self.track_stack.currentIndex() == TRACK_TERMS and 0 <= self.findings.currentRow() < (
                len(self._report["findings"]) if self._report is not None else 0
            ):
                self._promote_from_term_finding()
                return
        self.show_termbase_dialog(
            source=source_selected,
            target=target_selected,
            direction=self._payload["direction"],
            kind="preferred_term",
        )

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
            self.resolve_conflicts_save_button.setVisible(False)
            self.export_prose_button.setVisible(False)
            self._inspector_requested = True
            self.inspector.show()
            self._update_inspector_button()
            self._update_controls()
            return
        self.conflict_label.setText(strings.MANUSCRIPT_CHOICES_CONFLICT)
        self.remove_choice_button.setText(strings.MANUSCRIPT_REMOVE_CHOICE)
        groups = choice_conflict_groups(self._payload) if self._conflict_mode else []
        remaining_duplicates = 0
        if self._conflict_mode and not groups:
            _, remaining_duplicates = sanitize_duplicate_choices(self._payload)
        has_conflicts = self._conflict_mode and (bool(groups) or remaining_duplicates > 0)
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
        self.conflict_label.setVisible(has_conflicts)
        self.conflict_list.setVisible(bool(groups))
        self.remove_choice_button.setVisible(bool(groups))
        self.resolve_conflicts_save_button.setVisible(has_conflicts)
        self.export_prose_button.setVisible(has_conflicts)
        if has_conflicts:
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
            self._apply_parts_edit(self._controller.remove_history_choice(row),
                                   strings.MANUSCRIPT_HISTORY_CHOICE_REMOVED)
            self._refresh_conflicts()
            self.conflict_list.setCurrentRow(min(row, self.conflict_list.count() - 1))
            return
        self._commit(self._controller.remove_choice(self._conflict_indexes[row]), editors=True)
        self.status_label.setText(strings.MANUSCRIPT_CHOICE_REMOVED)

    def _resolve_conflicting_choices_and_save(self, _checked: bool = False) -> bool:
        """Undoably drop colliding historical choices and save the manuscript."""
        if not self._input_ready():
            return False
        outcome = self._controller.resolve_conflicting_choices_for_save()
        self._conflict_mode = False
        if outcome.committed:
            self._commit(outcome, editors=False)
        self._refresh_conflicts()
        saved = self.save_document()
        if saved and outcome.committed:
            self.status_label.setText(strings.MANUSCRIPT_CONFLICTS_RESOLVED_SAVED)
        return saved

    def export_prose(self, _checked: bool = False) -> bool:
        """Export current source and target prose as plain UTF-8 text (.txt)."""
        if not self._input_ready():
            return False
        name, _ = QFileDialog.getSaveFileName(
            self, strings.MANUSCRIPT_EXPORT_PROSE, "wuwaterm-prose.txt", strings.MANUSCRIPT_PROSE_FILTER
        )
        if not name:
            return False
        destination = Path(name)
        if self._path is not None and destination.resolve() == self._path.resolve():
            self.status_label.setText(strings.MANUSCRIPT_EXPORT_PROSE_OVER_MANUSCRIPT)
            return False
        source_text = self._payload["source"]
        target_text = self._payload["target"]
        sections = [source_text]
        if target_text:
            sections.append(f"---\n\n{target_text}")
        content = ("\n\n".join(sections)).encode("utf-8")
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
            self.status_label.setText(strings.MANUSCRIPT_EXPORT_PROSE_FAILED.format(reason=str(exc)))
            return False
        self.status_label.setText(strings.MANUSCRIPT_EXPORT_PROSE_DONE)
        return True

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
        # The review features the file records are now the manuscript's own.
        parts, features = sr.adopt_file_features(self._payload, written.get("review_features") or ())
        if features:
            self._payload["review_features"] = list(features)
        else:
            self._payload.pop("review_features", None)
        if parts.segments is not sr.parts_of(self._payload).segments:
            self._put_parts(parts)
        self._conflict_mode = False
        self._refresh_conflicts()
        self._path = path
        self._saved_payload = _copy_payload(self._payload)
        self._recovery_pending = False
        self._recovery_timer.stop()
        self._clear_recovery_draft()
        self.recovery_banner.clear()
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
        # The loaded texts are the versions on-file decisions usually refer to;
        # keep them so an edit before the next review does not lose them.
        self._controller.reset_known_texts(value)
        self._path = Path(name)
        # A file this client wrote normalizes to itself; anything else is
        # mapped now, before the baseline, so opening never looks edited.
        self._records()
        self._saved_payload = _copy_payload(self._payload)
        self._recovery_pending = False
        self._recovery_timer.stop()
        self._clear_recovery_draft()
        self.recovery_banner.clear()
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
        self._controller.reset_known_texts()
        self._saved_payload = _copy_payload(value)
        self._path = None
        self._recovery_pending = False
        self._recovery_timer.stop()
        self._clear_recovery_draft()
        self.recovery_banner.clear()
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
        self._controller.clear_history()
        self._conflict_mode = self._history_removal = False
        self._mismatch_prompt_active = False
        self._term_view_override = None
        self._term_filter = _DEFAULT_TERM_FILTER
        self._quiet_ref = None
        self.handled_toggle.blockSignals(True)
        self.handled_toggle.setChecked(False)
        self.handled_toggle.blockSignals(False)
        if hasattr(self, "find_bar") and self.find_bar.isVisible():
            self._close_find_bar(restore_focus=False)
        else:
            self._search_controller.clear()
        self._refresh_conflicts()
        self._set_payload(value)
        # Opening or clearing a document has not moved the caret: cancel the
        # follow timer started by _set_payload so a restored progress.current
        # (or None) is not overwritten by caret offset 0, and update counts now.
        self._caret_timer.stop()
        self._caret_side = "source"
        self._reset_term_session(cursor)
        self._session_snapshots = []
        # Clearing the runs above listed the outgoing manuscript's segments,
        # and an open caches the new pairing, so no refresh would follow: a
        # stale row selected its old offsets in the new text and marked its
        # index. Rebuilt after the cursor reset, so progress.current is the
        # row selected, never the old one.
        self._refresh_segments()
        self.workbenchStatusChanged.emit()

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
                        exported_finding: dict = {
                            "verdict": finding["verdict"],
                            "source_span": _offset(finding["source_span"], record["source_start"]),
                            "target_span": _offset(finding["target_span"], record["target_start"]),
                            "candidates": [{"zh": c["zh"], "en": c["en"]} for c in finding["candidates"]],
                            "candidates_truncated": finding["candidates_truncated"] is True,
                        }
                        if "reason" in finding:
                            exported_finding["reason"] = finding["reason"]
                        findings.append(exported_finding)
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
            # This session's own choices (an ambiguous name adopted or marked an
            # ordinary word) are the ones a document check sends.
            "reconciled": mdr.reconcile_choices(payload["choices"], draft, latest,
                                                own=self._controller.is_session_choice),
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

    def _build_find_bar(self) -> None:
        """Collapsible in-editor Find & Replace bar (Ctrl+F / Ctrl+H)."""
        bar = QFrame(self)
        bar.setObjectName("workbenchToolbar")
        bar.hide()
        self.find_bar = bar
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(6)

        find_row = QHBoxLayout()
        find_row.setContentsMargins(0, 0, 0, 0)
        find_row.setSpacing(6)

        self.find_query_edit = QLineEdit(bar)
        self.find_query_edit.setObjectName("searchField")
        self.find_query_edit.setPlaceholderText(strings.FIND_BAR_QUERY_PLACEHOLDER)
        self.find_query_edit.setAccessibleName(strings.FIND_BAR_QUERY_LABEL)
        self.find_query_edit.textChanged.connect(lambda _t: self._refresh_find_matches(preserve_current=False))

        self.find_side_combo = ChevronComboBox(bar)
        self.find_side_combo.addItem(strings.FIND_BAR_SIDE_TARGET, "target")
        self.find_side_combo.addItem(strings.FIND_BAR_SIDE_SOURCE, "source")
        self.find_side_combo.setAccessibleName(strings.FIND_BAR_SIDE_LABEL)
        self.find_side_combo.currentIndexChanged.connect(lambda _i: self._refresh_find_matches(preserve_current=False))

        self.find_case_check = QCheckBox(strings.FIND_BAR_CASE_SENSITIVE, bar)
        self.find_case_check.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_case_check.toggled.connect(lambda _c: self._refresh_find_matches(preserve_current=False))

        self.find_scope_check = QCheckBox(strings.FIND_BAR_SCOPE_SEGMENT, bar)
        self.find_scope_check.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_scope_check.toggled.connect(lambda _c: self._refresh_find_matches(preserve_current=False))

        self.find_count_label = QLabel(strings.FIND_BAR_COUNT_NONE, bar)
        self.find_count_label.setObjectName("secondaryText")

        self.find_prev_button = QPushButton(bar)
        self.find_prev_button.setObjectName("commandButton")
        self.find_prev_button.setIcon(icon("chevron-up"))
        self.find_prev_button.setIconSize(QSize(16, 16))
        self.find_prev_button.setFixedWidth(34)
        self.find_prev_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_prev_button.setToolTip(strings.FIND_BAR_PREV)
        self.find_prev_button.setAccessibleName(strings.FIND_BAR_PREV)
        self.find_prev_button.clicked.connect(self._find_prev)

        self.find_next_button = QPushButton(bar)
        self.find_next_button.setObjectName("commandButton")
        self.find_next_button.setIcon(icon("chevron-down"))
        self.find_next_button.setIconSize(QSize(16, 16))
        self.find_next_button.setFixedWidth(34)
        self.find_next_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_next_button.setToolTip(strings.FIND_BAR_NEXT)
        self.find_next_button.setAccessibleName(strings.FIND_BAR_NEXT)
        self.find_next_button.clicked.connect(self._find_next)

        self.find_replace_toggle = QPushButton(strings.FIND_BAR_TOGGLE_REPLACE, bar)
        self.find_replace_toggle.setObjectName("commandButton")
        self.find_replace_toggle.setCheckable(True)
        self.find_replace_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_replace_toggle.toggled.connect(self._set_find_replace_visible)

        self.find_close_button = QPushButton(bar)
        self.find_close_button.setObjectName("commandButton")
        self.find_close_button.setIcon(icon("x"))
        self.find_close_button.setIconSize(QSize(16, 16))
        self.find_close_button.setFixedWidth(34)
        self.find_close_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_close_button.setToolTip(strings.FIND_BAR_CLOSE)
        self.find_close_button.setAccessibleName(strings.FIND_BAR_CLOSE)
        self.find_close_button.clicked.connect(lambda: self._close_find_bar())

        find_row.addWidget(self.find_query_edit, 1)
        find_row.addWidget(self.find_side_combo)
        find_row.addWidget(self.find_case_check)
        find_row.addWidget(self.find_scope_check)
        find_row.addWidget(self.find_count_label)
        find_row.addWidget(self.find_prev_button)
        find_row.addWidget(self.find_next_button)
        find_row.addWidget(self.find_replace_toggle)
        find_row.addWidget(self.find_close_button)
        outer.addLayout(find_row)

        self.find_replace_row = QWidget(bar)
        self.find_replace_row.setObjectName("inspectorBody")
        replace_layout = QHBoxLayout(self.find_replace_row)
        replace_layout.setContentsMargins(0, 0, 0, 0)
        replace_layout.setSpacing(6)

        self.find_replace_edit = QLineEdit(self.find_replace_row)
        self.find_replace_edit.setPlaceholderText(strings.FIND_BAR_REPLACE_PLACEHOLDER)
        self.find_replace_edit.setAccessibleName(strings.FIND_BAR_REPLACE_LABEL)
        self.find_replace_edit.returnPressed.connect(self._find_replace_current)

        self.find_replace_one_button = QPushButton(strings.FIND_BAR_REPLACE_ONE, self.find_replace_row)
        self.find_replace_one_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_replace_one_button.clicked.connect(self._find_replace_current)

        self.find_replace_segment_button = QPushButton(strings.FIND_BAR_REPLACE_SEGMENT, self.find_replace_row)
        self.find_replace_segment_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_replace_segment_button.setToolTip(strings.FIND_BAR_REPLACE_SEGMENT_TOOLTIP)
        self.find_replace_segment_button.clicked.connect(self._find_replace_in_segment)

        replace_layout.addWidget(self.find_replace_edit, 1)
        replace_layout.addWidget(self.find_replace_one_button)
        replace_layout.addWidget(self.find_replace_segment_button)
        self.find_replace_row.hide()
        outer.addWidget(self.find_replace_row)

    def _set_find_replace_visible(self, visible: bool) -> None:
        self.find_replace_row.setVisible(visible)
        self._update_find_controls()

    def _find_segment_context(self, side: str, *, offset: int | None = None) -> tuple[list[tuple[int, int]], int | None]:
        pairs = self._current_pairs()
        if not pairs:
            return [], None
        ranges = [(getattr(pair, side).core_start, getattr(pair, side).core_end) for pair in pairs]
        resolved_offset = self._caret_offset(side) if offset is None else offset
        seg_idx = self._pair_index_at(pairs, side, resolved_offset)
        return ranges, seg_idx

    def _open_find_bar(self, *, replace: bool = False) -> None:
        if not self._input_ready():
            return
        if self.source_edit.hasFocus():
            side = "source"
        elif self.target_edit.hasFocus():
            side = "target"
        elif not self.find_bar.isVisible():
            side = self._last_side
        else:
            side = self.find_side_combo.currentData() or self._last_side

        selected = self._editor(side).selected_raw_text()
        if selected and "\n" not in selected and "\r" not in selected:
            self.find_query_edit.blockSignals(True)
            self.find_query_edit.setText(selected)
            self.find_query_edit.blockSignals(False)

        side_idx = self.find_side_combo.findData(side)
        if side_idx >= 0 and self.find_side_combo.currentIndex() != side_idx:
            self.find_side_combo.blockSignals(True)
            self.find_side_combo.setCurrentIndex(side_idx)
            self.find_side_combo.blockSignals(False)

        self.find_bar.show()
        if replace:
            self.find_replace_toggle.setChecked(True)
        elif not self.find_replace_row.isVisible():
            self.find_replace_toggle.setChecked(False)
        self._refresh_find_matches(preserve_current=True)
        self.find_query_edit.setFocus()
        self.find_query_edit.selectAll()

    def _close_find_bar(self, *, restore_focus: bool = True) -> None:
        self.find_bar.hide()
        self._search_controller.clear()
        self.source_edit.setExtraSelections([])
        self.target_edit.setExtraSelections([])
        self.find_count_label.setText(strings.FIND_BAR_COUNT_NONE)
        if restore_focus:
            self._editor(self._last_side).setFocus()

    def _refresh_find_matches(self, *, preserve_current: bool = True, cursor_offset: int | None = None) -> None:
        side = self.find_side_combo.currentData() or "target"
        ctrl = self._search_controller
        ctrl.query = self.find_query_edit.text()
        ctrl.replacement = self.find_replace_edit.text()
        ctrl.side = side
        ctrl.case_sensitive = self.find_case_check.isChecked()
        ctrl.scope = "segment" if self.find_scope_check.isChecked() else "all"
        anchor = self._caret_offset(side) if cursor_offset is None else cursor_offset
        ranges, seg_idx = self._find_segment_context(side, offset=anchor)
        ctrl.refresh(
            self._payload[side],
            segment_ranges=ranges,
            scope_segment=seg_idx,
            cursor_offset=anchor,
            preserve_current=preserve_current,
        )
        self._highlight_find_match()
        self._update_find_controls(seg_idx=seg_idx if ctrl.scope == "segment" else None)

    def _highlight_find_match(self) -> None:
        match = self._search_controller.current_match()
        if match is None:
            self.source_edit.setExtraSelections([])
            self.target_edit.setExtraSelections([])
            self.find_count_label.setText(strings.FIND_BAR_COUNT_NONE)
            return
        editor = self._editor(match.side)
        other = self.target_edit if match.side == "source" else self.source_edit
        other.setExtraSelections([])
        with self.scroll_sync.suspended():
            editor.select_raw_span(match.span(), move_cursor=True)
            editor.ensureCursorVisible()
        current = (self._search_controller.current_index or 0) + 1
        self.find_count_label.setText(strings.FIND_BAR_COUNT.format(current=current, total=len(self._search_controller.matches)))

    def _update_find_controls(self, *, seg_idx: int | None = None) -> None:
        composing = self.source_edit._ime_active or self.target_edit._ime_active
        has_matches = bool(self._search_controller.matches)
        side = self.find_side_combo.currentData() or "target"
        ranges, resolved_seg_idx = self._find_segment_context(side)
        if seg_idx is not None:
            resolved_seg_idx = seg_idx
        has_segment_match = False
        if (
            not composing
            and self._search_controller.query
            and resolved_seg_idx is not None
            and 0 <= resolved_seg_idx < len(ranges)
        ):
            seg_start, seg_end = ranges[resolved_seg_idx]
            has_segment_match = any(
                seg_start <= m.start and m.end <= seg_end for m in self._search_controller.matches
            )
        self.find_prev_button.setEnabled(not composing and has_matches)
        self.find_next_button.setEnabled(not composing and has_matches)
        self.find_replace_one_button.setEnabled(not composing and self._search_controller.current_match() is not None)
        self.find_replace_segment_button.setEnabled(not composing and has_segment_match)

    def _find_next(self) -> None:
        if not self.find_bar.isVisible():
            return
        if not self._search_controller.matches and self.find_query_edit.text():
            self._refresh_find_matches(preserve_current=False)
            return
        side = self.find_side_combo.currentData() or "target"
        self._search_controller.step(forward=True, cursor_offset=self._caret_offset(side))
        self._highlight_find_match()
        self._update_find_controls()

    def _find_prev(self) -> None:
        if not self.find_bar.isVisible():
            return
        if not self._search_controller.matches and self.find_query_edit.text():
            self._refresh_find_matches(preserve_current=False)
            return
        side = self.find_side_combo.currentData() or "target"
        self._search_controller.step(forward=False, cursor_offset=self._caret_offset(side))
        self._highlight_find_match()
        self._update_find_controls()

    def _find_replace_current(self) -> None:
        if not self._input_ready():
            return
        match = self._search_controller.current_match()
        if match is None:
            self.status_label.setText(strings.FIND_BAR_NO_MATCH)
            return
        side = match.side
        editor = self._editor(side)
        raw = self._payload[side]
        try:
            result = self._search_controller.replace_current(raw, self.find_replace_edit.text())
        except ValueError:
            self._refresh_find_matches(preserve_current=False)
            return
        if result is None:
            return
        if result.text == raw:
            editor.set_raw_cursor_offset(result.new_cursor_offset)
            self._refresh_find_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)
            return
        if not editor.can_display(result.text):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value[side] = result.text
        next_source = result.text if side == "source" else value["source"]
        next_target = result.text if side == "target" else value["target"]
        _, start, old_end, new_len = result.edit
        remap = remap_splice(value["alignments"], side, next_source, next_target, start, old_end, new_len)
        value["alignments"] = remap.alignments
        self._suppress_find_refresh = True
        try:
            self._apply_edit(value, edits=[result.edit], remap=remap)
        finally:
            self._suppress_find_refresh = False
        editor.set_raw_cursor_offset(result.new_cursor_offset)
        self.status_label.setText(strings.FIND_BAR_REPLACED_ONE)
        self._refresh_find_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)

    def _find_replace_in_segment(self) -> None:
        if not self._input_ready():
            return
        side = self.find_side_combo.currentData() or "target"
        editor = self._editor(side)
        ranges, seg_idx = self._find_segment_context(side)
        match = self._search_controller.current_match()
        if match is not None and match.segment_index is not None:
            seg_idx = match.segment_index
        if seg_idx is None or not 0 <= seg_idx < len(ranges):
            self.status_label.setText(strings.FIND_BAR_NO_SEGMENT)
            return
        raw = self._payload[side]
        self._search_controller.query = self.find_query_edit.text()
        self._search_controller.side = side
        self._search_controller.case_sensitive = self.find_case_check.isChecked()
        result = self._search_controller.replace_scoped_segment(
            raw,
            ranges[seg_idx],
            segment_index=seg_idx,
            replacement=self.find_replace_edit.text(),
        )
        if result is None:
            self.status_label.setText(strings.FIND_BAR_NO_MATCH)
            return
        if result.text == raw:
            editor.set_raw_cursor_offset(result.new_cursor_offset)
            self._refresh_find_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)
            return
        if not editor.can_display(result.text):
            self.status_label.setText(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value[side] = result.text
        next_source = result.text if side == "source" else value["source"]
        next_target = result.text if side == "target" else value["target"]
        _, start, old_end, new_len = result.edit
        remap = remap_splice(value["alignments"], side, next_source, next_target, start, old_end, new_len)
        value["alignments"] = remap.alignments
        self._suppress_find_refresh = True
        try:
            self._apply_edit(value, edits=[result.edit], remap=remap)
        finally:
            self._suppress_find_refresh = False
        editor.set_raw_cursor_offset(result.new_cursor_offset)
        self.status_label.setText(strings.FIND_BAR_REPLACED_SEGMENT.format(count=result.replaced_count))
        self._refresh_find_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)

    # -- file continuity: import, clipboard, work position -------------------
    def _guard_text_inputs(self) -> None:
        """Inspector text fields take a dropped file as a file, not as typed text,
        and a character typed with AltGr as text, not as a Ctrl+Alt shortcut.

        QLineEdit and the adjust box accept drops by default and would type
        the file's path into a field that can later be written into the draft.
        """
        for widget in (*self.findChildren(QLineEdit), self.adjust_edit,
                       self.find_bar, self.find_side_combo, self.find_case_check,
                       self.find_scope_check, self.find_prev_button, self.find_next_button,
                       self.find_replace_toggle, self.find_close_button,
                       self.find_replace_one_button, self.find_replace_segment_button,
                       self.source_edit, self.target_edit):
            widget.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            if (
                event.key() == Qt.Key.Key_Escape
                and self.find_bar.isVisible()
                and not (self.source_edit._ime_active or self.target_edit._ime_active)
            ):
                self._close_find_bar()
                return True
            if watched is self.find_query_edit and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._find_prev()
                else:
                    self._find_next()
                return True
        if watched in (self.source_edit, self.target_edit):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.ShortcutOverride and LosslessEditor.typed_with_altgr(event):
            event.accept()
            return True
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
        before_source, before_target = value["source"], value["target"]
        value[side] = text
        remap = remap_for_free_edit(value["alignments"], side, before_source, before_target,
                                    value["source"], value["target"])
        value["alignments"] = remap.alignments
        self._apply_edit(value, remap=remap)
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
        with self.scroll_sync.suspended():
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
