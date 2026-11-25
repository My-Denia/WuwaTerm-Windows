"""The keyboard shortcut sheet (F1): every binding, in Chinese and in English.

One table feeds the sheet, and a test checks it against the shortcuts the
window and the workbench actually register, so the sheet cannot promise a key
that does nothing or leave out one that does something.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import strings

# (keys as shown, the key sequences they stand for, Chinese, English). The
# sequences are Qt's portable text form, exactly as the code registers them.
SHORTCUTS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("Alt+↑ / Alt+↓", ("Alt+Up", "Alt+Down"), "上一段 / 下一段", "Previous / next segment"),
    ("Alt+← / Alt+→", ("Alt+Left", "Alt+Right"), "切到原文 / 译文编辑器", "Focus the source / target editor"),
    ("F7 / Shift+F7", ("F7", "Shift+F7"), "下一个 / 上一个术语问题", "Next / previous term issue"),
    ("F8 / Shift+F8", ("F8", "Shift+F8"), "下一条 / 上一条语义疑点", "Next / previous semantic issue"),
    ("Ctrl+Alt+1 / 2 / 3", ("Ctrl+Alt+1", "Ctrl+Alt+2", "Ctrl+Alt+3"),
     "术语约束 / 语义疑点 / 段落轨道", "Terms / semantic / segments track"),
    ("Ctrl+Shift+M", ("Ctrl+Shift+M",), "定位首处待切分段", "Jump to the first segment to split"),
    ("Ctrl+Enter", ("Ctrl+Return", "Ctrl+Enter"), "采纳建议", "Accept the suggestion"),
    ("Ctrl+Shift+E", ("Ctrl+Shift+E",), "调整后采纳", "Adjust and apply"),
    ("Ctrl+Shift+I", ("Ctrl+Shift+I",), "忽略疑点", "Ignore the issue"),
    ("Ctrl+Shift+R", ("Ctrl+Shift+R",), "审校", "Review"),
    ("Ctrl+Shift+T", ("Ctrl+Shift+T",), "翻译", "Translate"),
    ("Ctrl+Z / Ctrl+Shift+Z", (), "撤销修订 / 重做（最近 50 步）", "Undo / redo (the last 50 steps)"),
    ("Ctrl+N / Ctrl+O", ("Ctrl+N", "Ctrl+O"), "新稿 / 打开稿件", "New / open manuscript"),
    ("Ctrl+S / Ctrl+Shift+S", ("Ctrl+S", "Ctrl+Shift+S"), "保存 / 另存稿件", "Save / save as"),
    ("Ctrl+Shift+C", ("Ctrl+Shift+C",), "复制译文", "Copy the translation"),
    ("Ctrl+1 / 2 / 3 / 4", ("Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4"),
     "术语查词 / 文本翻译 / 服务状态 / 稿件审校", "Terms / translate / status / manuscript"),
    ("Ctrl+K", ("Ctrl+K",), "聚焦当前区域的输入框", "Focus the current input"),
    ("F1", ("F1",), "打开本速查表", "Open this sheet"),
)


def registered_sequences() -> set[str]:
    """Every key sequence the sheet lists."""
    return {sequence for _keys, sequences, _zh, _en in SHORTCUTS for sequence in sequences}


class ShortcutSheet(QDialog):
    """A read-only table of the shortcuts, sized to its content."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(strings.SHORTCUT_SHEET_TITLE)
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        self.table = QTableWidget(len(SHORTCUTS), 3, self)
        self.table.setHorizontalHeaderLabels(
            [strings.SHORTCUT_SHEET_KEYS, strings.SHORTCUT_SHEET_ACTION_ZH, strings.SHORTCUT_SHEET_ACTION_EN])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setWordWrap(True)
        self.table.setAlternatingRowColors(True)
        for row, (keys, _sequences, zh, en) in enumerate(SHORTCUTS):
            for column, text in enumerate((keys, zh, en)):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, column, item)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table, 1)
        note = QLabel(f"{strings.SHORTCUT_SHEET_NOTE}\n{strings.SHORTCUT_SHEET_NOTE_EN}", self)
        note.setObjectName("secondaryText")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(strings.CLOSE_BUTTON)
        layout.addWidget(buttons)
        self._fit()

    def _fit(self) -> None:
        """Wide enough for every row on one line, within the screen."""
        self.table.resizeColumnsToContents()
        width = sum(self.table.columnWidth(column) for column in range(3)) + 48
        height = (self.table.horizontalHeader().height()
                  + sum(self.table.rowHeight(row) for row in range(self.table.rowCount())) + 140)
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        if screen is not None:
            width = min(width, int(screen.width() * 0.9))
            height = min(height, int(screen.height() * 0.85))
        self.resize(max(520, width), max(360, height))
