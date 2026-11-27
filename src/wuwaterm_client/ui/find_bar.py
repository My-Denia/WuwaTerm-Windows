"""Collapsible in-editor Find & Scoped Replace bar (Ctrl+F / Ctrl+H)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .. import strings
from ..document_edits import remap_splice
from ..search_replace import SearchReplaceController
from .components import ChevronComboBox
from .icons import icon


class FindReplaceBar(QFrame):
    """Self-contained Find & Scoped Replace bar operating over the two editors."""

    def __init__(
        self,
        parent: QWidget,
        *,
        search_controller: SearchReplaceController,
        get_payload: Callable[[], dict[str, Any]],
        get_editor: Callable[[str], Any],
        get_last_side: Callable[[], str],
        get_segment_context: Callable[..., tuple[list[tuple[int, int]], int | None]],
        get_caret_offset: Callable[[str], int],
        is_composing: Callable[[], bool],
        is_input_ready: Callable[[], bool],
        suspend_scroll_sync: Callable[[], Any],
        set_status: Callable[[str], None],
        draft_snapshot: Callable[[], dict[str, Any]],
        apply_edit: Callable[..., None],
    ) -> None:
        super().__init__(parent)
        self.setObjectName("findBar")
        self.hide()
        self._controller = search_controller
        self._get_payload = get_payload
        self._get_editor = get_editor
        self._get_last_side = get_last_side
        self._get_segment_context = get_segment_context
        self._get_caret_offset = get_caret_offset
        self._is_composing = is_composing
        self._is_input_ready = is_input_ready
        self._suspend_scroll_sync = suspend_scroll_sync
        self._set_status = set_status
        self._draft_snapshot = draft_snapshot
        self._apply_edit = apply_edit
        self.suppress_refresh = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(6)

        find_row = QHBoxLayout()
        find_row.setContentsMargins(0, 0, 0, 0)
        find_row.setSpacing(6)

        self.find_query_edit = QLineEdit(self)
        self.find_query_edit.setObjectName("searchField")
        self.find_query_edit.setPlaceholderText(strings.FIND_BAR_QUERY_PLACEHOLDER)
        self.find_query_edit.setAccessibleName(strings.FIND_BAR_QUERY_LABEL)
        self.find_query_edit.textChanged.connect(lambda _t: self.refresh_matches(preserve_current=False))

        self.find_side_combo = ChevronComboBox(self)
        self.find_side_combo.addItem(strings.FIND_BAR_SIDE_TARGET, "target")
        self.find_side_combo.addItem(strings.FIND_BAR_SIDE_SOURCE, "source")
        self.find_side_combo.setAccessibleName(strings.FIND_BAR_SIDE_LABEL)
        self.find_side_combo.currentIndexChanged.connect(lambda _i: self.refresh_matches(preserve_current=False))

        self.find_case_check = QCheckBox(strings.FIND_BAR_CASE_SENSITIVE, self)
        self.find_case_check.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_case_check.toggled.connect(lambda _c: self.refresh_matches(preserve_current=False))

        self.find_scope_check = QCheckBox(strings.FIND_BAR_SCOPE_SEGMENT, self)
        self.find_scope_check.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_scope_check.toggled.connect(lambda _c: self.refresh_matches(preserve_current=False))

        self.find_count_label = QLabel(strings.FIND_BAR_COUNT_NONE, self)
        self.find_count_label.setObjectName("secondaryText")

        self.find_prev_button = QPushButton(self)
        self.find_prev_button.setObjectName("commandButton")
        self.find_prev_button.setIcon(icon("chevron-up"))
        self.find_prev_button.setIconSize(QSize(16, 16))
        self.find_prev_button.setFixedWidth(34)
        self.find_prev_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_prev_button.setToolTip(strings.FIND_BAR_PREV)
        self.find_prev_button.setAccessibleName(strings.FIND_BAR_PREV)
        self.find_prev_button.clicked.connect(self.find_prev)

        self.find_next_button = QPushButton(self)
        self.find_next_button.setObjectName("commandButton")
        self.find_next_button.setIcon(icon("chevron-down"))
        self.find_next_button.setIconSize(QSize(16, 16))
        self.find_next_button.setFixedWidth(34)
        self.find_next_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_next_button.setToolTip(strings.FIND_BAR_NEXT)
        self.find_next_button.setAccessibleName(strings.FIND_BAR_NEXT)
        self.find_next_button.clicked.connect(self.find_next)

        self.find_replace_toggle = QPushButton(strings.FIND_BAR_TOGGLE_REPLACE, self)
        self.find_replace_toggle.setObjectName("commandButton")
        self.find_replace_toggle.setCheckable(True)
        self.find_replace_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_replace_toggle.toggled.connect(self.set_replace_visible)

        self.find_close_button = QPushButton(self)
        self.find_close_button.setObjectName("commandButton")
        self.find_close_button.setIcon(icon("x"))
        self.find_close_button.setIconSize(QSize(16, 16))
        self.find_close_button.setFixedWidth(34)
        self.find_close_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_close_button.setToolTip(strings.FIND_BAR_CLOSE)
        self.find_close_button.setAccessibleName(strings.FIND_BAR_CLOSE)
        self.find_close_button.clicked.connect(lambda: self.close_bar())

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

        self.find_replace_row = QWidget(self)
        self.find_replace_row.setObjectName("inspectorBody")
        replace_layout = QHBoxLayout(self.find_replace_row)
        replace_layout.setContentsMargins(0, 0, 0, 0)
        replace_layout.setSpacing(6)

        self.find_replace_edit = QLineEdit(self.find_replace_row)
        self.find_replace_edit.setPlaceholderText(strings.FIND_BAR_REPLACE_PLACEHOLDER)
        self.find_replace_edit.setAccessibleName(strings.FIND_BAR_REPLACE_LABEL)
        self.find_replace_edit.returnPressed.connect(self.replace_current)

        self.find_replace_one_button = QPushButton(strings.FIND_BAR_REPLACE_ONE, self.find_replace_row)
        self.find_replace_one_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_replace_one_button.clicked.connect(self.replace_current)

        self.find_replace_segment_button = QPushButton(strings.FIND_BAR_REPLACE_SEGMENT, self.find_replace_row)
        self.find_replace_segment_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.find_replace_segment_button.setToolTip(strings.FIND_BAR_REPLACE_SEGMENT_TOOLTIP)
        self.find_replace_segment_button.clicked.connect(self.replace_in_segment)

        replace_layout.addWidget(self.find_replace_edit, 1)
        replace_layout.addWidget(self.find_replace_one_button)
        replace_layout.addWidget(self.find_replace_segment_button)
        self.find_replace_row.hide()
        outer.addWidget(self.find_replace_row)

    def guarded_widgets(self) -> tuple[QWidget, ...]:
        return (
            self,
            self.find_side_combo,
            self.find_case_check,
            self.find_scope_check,
            self.find_prev_button,
            self.find_next_button,
            self.find_replace_toggle,
            self.find_close_button,
            self.find_replace_one_button,
            self.find_replace_segment_button,
        )

    def set_replace_visible(self, visible: bool) -> None:
        self.find_replace_row.setVisible(visible)
        self.update_controls()

    def open_bar(self, *, replace: bool = False) -> None:
        if not self._is_input_ready():
            return
        source_edit = self._get_editor("source")
        target_edit = self._get_editor("target")
        if source_edit.hasFocus():
            side = "source"
        elif target_edit.hasFocus():
            side = "target"
        elif not self.isVisible():
            side = self._get_last_side()
        else:
            side = self.find_side_combo.currentData() or self._get_last_side()

        selected = self._get_editor(side).selected_raw_text()
        if selected and "\n" not in selected and "\r" not in selected:
            self.find_query_edit.blockSignals(True)
            self.find_query_edit.setText(selected)
            self.find_query_edit.blockSignals(False)

        side_idx = self.find_side_combo.findData(side)
        if side_idx >= 0 and self.find_side_combo.currentIndex() != side_idx:
            self.find_side_combo.blockSignals(True)
            self.find_side_combo.setCurrentIndex(side_idx)
            self.find_side_combo.blockSignals(False)

        self.show()
        if replace:
            self.find_replace_toggle.setChecked(True)
        elif not self.find_replace_row.isVisible():
            self.find_replace_toggle.setChecked(False)
        self.refresh_matches(preserve_current=True)
        self.find_query_edit.setFocus()
        self.find_query_edit.selectAll()

    def close_bar(self, *, restore_focus: bool = True) -> None:
        self.hide()
        self._controller.clear()
        self._get_editor("source").setExtraSelections([])
        self._get_editor("target").setExtraSelections([])
        self.find_count_label.setText(strings.FIND_BAR_COUNT_NONE)
        if restore_focus:
            self._get_editor(self._get_last_side()).setFocus()

    def refresh_matches(self, *, preserve_current: bool = True, cursor_offset: int | None = None) -> None:
        side = self.find_side_combo.currentData() or "target"
        ctrl = self._controller
        ctrl.query = self.find_query_edit.text()
        ctrl.replacement = self.find_replace_edit.text()
        ctrl.side = side
        ctrl.case_sensitive = self.find_case_check.isChecked()
        ctrl.scope = "segment" if self.find_scope_check.isChecked() else "all"
        anchor = self._get_caret_offset(side) if cursor_offset is None else cursor_offset
        ranges, seg_idx = self._get_segment_context(side, offset=anchor)
        ctrl.refresh(
            self._get_payload()[side],
            segment_ranges=ranges,
            scope_segment=seg_idx,
            cursor_offset=anchor,
            preserve_current=preserve_current,
        )
        self.highlight_match()
        self.update_controls(seg_idx=seg_idx if ctrl.scope == "segment" else None)

    def highlight_match(self) -> None:
        match = self._controller.current_match()
        if match is None:
            self._get_editor("source").setExtraSelections([])
            self._get_editor("target").setExtraSelections([])
            self.find_count_label.setText(strings.FIND_BAR_COUNT_NONE)
            return
        editor = self._get_editor(match.side)
        other = self._get_editor("target" if match.side == "source" else "source")
        other.setExtraSelections([])
        with self._suspend_scroll_sync():
            editor.select_raw_span(match.span(), move_cursor=True)
            editor.ensureCursorVisible()
        current = (self._controller.current_index or 0) + 1
        self.find_count_label.setText(strings.FIND_BAR_COUNT.format(current=current, total=len(self._controller.matches)))

    def update_controls(self, *, seg_idx: int | None = None) -> None:
        composing = self._is_composing()
        has_matches = bool(self._controller.matches)
        side = self.find_side_combo.currentData() or "target"
        ranges, resolved_seg_idx = self._get_segment_context(side)
        if seg_idx is not None:
            resolved_seg_idx = seg_idx
        has_segment_match = False
        if (
            not composing
            and self._controller.query
            and resolved_seg_idx is not None
            and 0 <= resolved_seg_idx < len(ranges)
        ):
            seg_start, seg_end = ranges[resolved_seg_idx]
            has_segment_match = any(seg_start <= m.start and m.end <= seg_end for m in self._controller.matches)
        self.find_prev_button.setEnabled(not composing and has_matches)
        self.find_next_button.setEnabled(not composing and has_matches)
        self.find_replace_one_button.setEnabled(not composing and self._controller.current_match() is not None)
        self.find_replace_segment_button.setEnabled(not composing and has_segment_match)

    def find_next(self) -> None:
        if not self.isVisible():
            return
        if not self._controller.matches and self.find_query_edit.text():
            self.refresh_matches(preserve_current=False)
            return
        side = self.find_side_combo.currentData() or "target"
        self._controller.step(forward=True, cursor_offset=self._get_caret_offset(side))
        self.highlight_match()
        self.update_controls()

    def find_prev(self) -> None:
        if not self.isVisible():
            return
        if not self._controller.matches and self.find_query_edit.text():
            self.refresh_matches(preserve_current=False)
            return
        side = self.find_side_combo.currentData() or "target"
        self._controller.step(forward=False, cursor_offset=self._get_caret_offset(side))
        self.highlight_match()
        self.update_controls()

    def replace_current(self) -> None:
        if not self._is_input_ready():
            return
        match = self._controller.current_match()
        if match is None:
            self._set_status(strings.FIND_BAR_NO_MATCH)
            return
        side = match.side
        editor = self._get_editor(side)
        raw = self._get_payload()[side]
        try:
            result = self._controller.replace_current(raw, self.find_replace_edit.text())
        except ValueError:
            self.refresh_matches(preserve_current=False)
            return
        if result is None:
            return
        if result.text == raw:
            editor.set_raw_cursor_offset(result.new_cursor_offset)
            self.refresh_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)
            return
        if not editor.can_display(result.text):
            self._set_status(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value[side] = result.text
        next_source = result.text if side == "source" else value["source"]
        next_target = result.text if side == "target" else value["target"]
        _, start, old_end, new_len = result.edit
        remap = remap_splice(value["alignments"], side, next_source, next_target, start, old_end, new_len)
        value["alignments"] = remap.alignments
        self.suppress_refresh = True
        try:
            self._apply_edit(value, edits=[result.edit], remap=remap)
        finally:
            self.suppress_refresh = False
        editor.set_raw_cursor_offset(result.new_cursor_offset)
        self._set_status(strings.FIND_BAR_REPLACED_ONE)
        self.refresh_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)

    def replace_in_segment(self) -> None:
        if not self._is_input_ready():
            return
        side = self.find_side_combo.currentData() or "target"
        editor = self._get_editor(side)
        ranges, seg_idx = self._get_segment_context(side)
        match = self._controller.current_match()
        if match is not None and match.segment_index is not None:
            seg_idx = match.segment_index
        if seg_idx is None or not 0 <= seg_idx < len(ranges):
            self._set_status(strings.FIND_BAR_NO_SEGMENT)
            return
        raw = self._get_payload()[side]
        self._controller.query = self.find_query_edit.text()
        self._controller.side = side
        self._controller.case_sensitive = self.find_case_check.isChecked()
        result = self._controller.replace_scoped_segment(
            raw,
            ranges[seg_idx],
            segment_index=seg_idx,
            replacement=self.find_replace_edit.text(),
        )
        if result is None:
            self._set_status(strings.FIND_BAR_NO_MATCH)
            return
        if result.text == raw:
            editor.set_raw_cursor_offset(result.new_cursor_offset)
            self.refresh_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)
            return
        if not editor.can_display(result.text):
            self._set_status(strings.MANUSCRIPT_EDIT_REJECTED)
            return
        value = self._draft_snapshot()
        value[side] = result.text
        next_source = result.text if side == "source" else value["source"]
        next_target = result.text if side == "target" else value["target"]
        _, start, old_end, new_len = result.edit
        remap = remap_splice(value["alignments"], side, next_source, next_target, start, old_end, new_len)
        value["alignments"] = remap.alignments
        self.suppress_refresh = True
        try:
            self._apply_edit(value, edits=[result.edit], remap=remap)
        finally:
            self.suppress_refresh = False
        editor.set_raw_cursor_offset(result.new_cursor_offset)
        self._set_status(strings.FIND_BAR_REPLACED_SEGMENT.format(count=result.replaced_count))
        self.refresh_matches(preserve_current=False, cursor_offset=result.new_cursor_offset)
