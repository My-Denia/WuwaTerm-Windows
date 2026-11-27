"""Lossless plain-text editor widget for bilingual manuscripts.

A QTextDocument normalises several characters (CRLF, bare CR, NBSP, Unicode line
and paragraph separators). LosslessEditor retains the raw text and maps each
real edit through UTF-16 offsets so opening and saving without editing never
rewrites a manuscript.

Unlike constructing a throwaway QPlainTextEdit for every ``can_display`` probe,
this module uses a fast pure-Python check for ordinary text and reuses a single
lightweight child QTextDocument when edge-case characters are present.
"""
from __future__ import annotations

import bisect
import re

from PySide6.QtCore import QEvent, QMimeData, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette, QTextCursor, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")
_NEEDS_DISPLAY_MAP = re.compile("[\r\xa0\u2028\u2029]")
# Characters that Qt's QTextDocument never strips, alters or splits beyond
# what _display_map accounts for: everything except C0/C1 controls (other than
# \t, \n, \r), NBSP, Unicode line/paragraph separators, surrogates, and specials.
_FAST_SAFE_CHARS = re.compile(r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xa0\u2028\u2029\ud800-\udfff\ufffc-\uffff]*$")


def _display_map(raw: str) -> tuple[str, list[int]]:
    """Qt plain text and the corresponding raw offset at each boundary."""
    if not raw or _NEEDS_DISPLAY_MAP.search(raw) is None:
        return raw, list(range(len(raw) + 1))
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
    if index <= 0:
        return 0
    if _ASTRAL.search(text, 0, index) is None:
        return min(index, len(text))
    return len(text[:index].encode("utf-16-le")) // 2


def _scalar_boundary(text: str, position: int) -> int | None:
    if position < 0:
        return None
    if _ASTRAL.search(text) is None:
        return position if position <= len(text) else None
    units = 0
    for index, char in enumerate(text):
        if units == position:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > position:
            return None
    return len(text) if units == position else None


def _astral_indexes(text: str) -> list[int]:
    """Scalar indexes of the characters UTF-16 stores as two units."""
    return [match.start() for match in _ASTRAL.finditer(text)]


def _splice_raw(prefix: str, inserted: str, suffix: str) -> str:
    """Join ``prefix + inserted + suffix`` without collapsing adjacent CR and LF."""
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
    filesDropped = Signal(object, str)
    focused = Signal()
    promoteRequested = Signal()

    side = "source"

    def __init__(self, parent: QWidget | None = None, *, promote_label: str = "") -> None:
        super().__init__(parent)
        self._promote_label = promote_label
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
        self.last_edit: tuple[int, int, int] | None = None
        # Reused lightweight probe document for edge-case can_display() checks.
        self._probe_doc: QTextDocument | None = None
        # Brief subtle highlight pulse when jumping to a finding; owned by self.
        self._locate_pulse_timer = QTimer(self)
        self._locate_pulse_timer.setSingleShot(True)
        self._locate_pulse_timer.setInterval(180)
        self._locate_pulse_timer.timeout.connect(self._settle_locate_selection)
        self._locate_span_cursor: QTextCursor | None = None
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
        return {
            "position": cursor.position(),
            "anchor": cursor.anchor(),
            "vertical": self.verticalScrollBar().value(),
            "horizontal": self.horizontalScrollBar().value(),
        }

    def restore_view_state(self, state: dict) -> None:
        cursor = self.textCursor()
        end = self.document().characterCount() - 1
        cursor.setPosition(max(0, min(state["anchor"], end)))
        cursor.setPosition(max(0, min(state["position"], end)), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(state["vertical"])
        self.horizontalScrollBar().setValue(state["horizontal"])

    def can_display(self, raw: str) -> bool:
        """Check whether Qt represents ``raw`` losslessly without building a widget."""
        if not raw or _FAST_SAFE_CHARS.match(raw) is not None:
            return True
        shown, _ = _display_map(raw)
        doc = self._probe_doc
        if doc is None:
            doc = QTextDocument(self)
            doc.setUndoRedoEnabled(False)
            self._probe_doc = doc
        doc.setPlainText(raw)
        ok = doc.toPlainText() == shown
        doc.clear()
        return ok

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
        index = max(0, min(index, len(self._shown)))
        astral = self._astral()
        return index + (bisect.bisect_left(astral, index) if astral else 0)

    def raw_offset_at_position(self, position: int) -> int:
        return self._bounds[self._scalar_index_at(position)]

    def position_of_raw_offset(self, offset: int) -> int:
        offset = max(0, min(offset, len(self._raw)))
        return self._position_of(max(0, bisect.bisect_right(self._bounds, offset) - 1))

    def raw_offset_at_top(self) -> int | None:
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
        if self._promote_label:
            menu.addSeparator()
            promote = menu.addAction(self._promote_label)
            promote.triggered.connect(self.promoteRequested.emit)
        menu.exec(event.globalPos())

    @staticmethod
    def typed_with_altgr(event) -> bool:
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
        if self._ime_active and (
            event.matches(QKeySequence.StandardKey.Undo) or event.matches(QKeySequence.StandardKey.Redo)
        ):
            super().keyPressEvent(event)
            return
        if (
            event.key() == Qt.Key.Key_Z
            and event.modifiers() == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        ):
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
            if (
                not preliminary
                and left is not None
                and right is not None
                and old[:left] + inserted_shown + old[right:] == new
            ):
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
        if old == new:
            return
        start = _scalar_boundary(old, position)
        end = _scalar_boundary(old, position + removed)
        new_start = _scalar_boundary(new, position)
        new_end = _scalar_boundary(new, position + added)
        valid = (
            start is not None
            and end is not None
            and new_start is not None
            and new_end is not None
            and old[:start] == new[:new_start]
            and old[end:] == new[new_end:]
        )
        if valid:
            removed_part, inserted = old[start:end], new[new_start:new_end]
            prefix = 0
            while prefix < min(len(removed_part), len(inserted)) and removed_part[prefix] == inserted[prefix]:
                prefix += 1
            suffix = 0
            while (
                suffix < len(removed_part) - prefix
                and suffix < len(inserted) - prefix
                and removed_part[-1 - suffix] == inserted[-1 - suffix]
            ):
                suffix += 1
            left, right = start + prefix, end - suffix
            ins_mid = inserted[prefix : len(inserted) - suffix if suffix else None]
            raw = _splice_raw(self._raw[: self._bounds[left]], ins_mid, self._raw[self._bounds[right] :])

            suffix2 = 0
            while (
                suffix2 < min(len(removed_part), len(inserted))
                and removed_part[-1 - suffix2] == inserted[-1 - suffix2]
            ):
                suffix2 += 1
            prefix2 = 0
            while (
                prefix2 < len(removed_part) - suffix2
                and prefix2 < len(inserted) - suffix2
                and removed_part[prefix2] == inserted[prefix2]
            ):
                prefix2 += 1
            left2, right2 = start + prefix2, end - suffix2
            ins_mid2 = inserted[prefix2 : len(inserted) - suffix2 if suffix2 else None]
            raw2 = _splice_raw(self._raw[: self._bounds[left2]], ins_mid2, self._raw[self._bounds[right2] :])

            shown, bounds = _display_map(raw)
            if raw == raw2 and shown == new:
                old_start, old_end = self._bounds[start], self._bounds[end]
                self.last_edit = (old_start, old_end, len(raw) - len(self._raw) + old_end - old_start)
                self.last_edit_view = self._pending_view
                self._raw, self._shown, self._bounds = raw, shown, bounds
                self.rawChanged.emit()
                return
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

    def _settle_locate_selection(self) -> None:
        cursor = self._locate_span_cursor
        self._locate_span_cursor = None
        if cursor is None:
            return
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(self.palette().color(QPalette.ColorRole.Highlight))
        selection.format.setForeground(self.palette().color(QPalette.ColorRole.HighlightedText))
        self.setExtraSelections([selection])

    def select_raw_span(self, span: dict | None, *, move_cursor: bool = True, pulse: bool = False) -> bool:
        self._locate_pulse_timer.stop()
        self._locate_span_cursor = None
        self.setExtraSelections([])
        if not isinstance(span, dict):
            return False
        start, end, value = span.get("start"), span.get("end"), span.get("text")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not isinstance(value, str)
            or start < 0
            or end <= start
            or end > len(self._raw)
            or self._raw[start:end] != value
        ):
            return False
        try:
            left, right = self._bounds.index(start), self._bounds.index(end)
        except ValueError:
            return False
        cursor = self.textCursor()
        cursor.setPosition(_utf16_position(self._shown, right))
        if move_cursor:
            self.setTextCursor(cursor)
        cursor.setPosition(_utf16_position(self._shown, left), QTextCursor.MoveMode.KeepAnchor)
        if move_cursor:
            self.setTextCursor(cursor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        base_bg = self.palette().color(QPalette.ColorRole.Highlight)
        if pulse:
            accent = QColor(base_bg)
            accent.setAlpha(210)
            selection.format.setBackground(accent)
            selection.format.setForeground(self.palette().color(QPalette.ColorRole.HighlightedText))
            self.setExtraSelections([selection])
            self._locate_span_cursor = cursor
            self._locate_pulse_timer.start()
        else:
            selection.format.setBackground(base_bg)
            selection.format.setForeground(self.palette().color(QPalette.ColorRole.HighlightedText))
            self.setExtraSelections([selection])
        return True
