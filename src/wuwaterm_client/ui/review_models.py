"""Qt Model/View list architecture with width-cached size hints for review lists.

Replaces O(N) QListWidget `.clear() + addItem()` rebuilds and uncached
`_WordWrapDelegate` re-measurement with:

* :class:`ReviewRow` — immutable row payload
* :class:`ReviewListModel` (`QAbstractListModel`) — bulk model reset & targeted row updates
* :class:`ReviewFilterProxyModel` (`QSortFilterProxyModel`) — non-destructive key filtering
* :class:`CachedRowDelegate` (`QStyledItemDelegate`) — width/font-keyed `sizeHint` cache + Fluent active indicator
* :class:`ModelListView` (`QListView`) — drop-in view supporting whole-row height fitting
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QRect,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate, QWidget

from .. import theme
from . import review_states

AUX_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_MAX_SIZE_CACHE = 4096


@dataclass(frozen=True)
class ReviewRow:
    """One display row in a review list model."""

    text: str
    state: str | None = None
    state_label: str | None = None
    tooltip: str = ""
    user_data: Any = None
    aux_data: Any = None
    selectable: bool = True
    filter_keys: frozenset[str] = frozenset({"all"})

    def resolved_tooltip(self) -> str:
        if self.state is None:
            return self.tooltip
        valid_state = self.state if self.state in review_states.STATES else review_states.UNEVALUATED
        name = self.state_label or review_states.LABELS[valid_state]
        return f"{name}\n{self.tooltip}" if self.tooltip else name


class ReviewListModel(QAbstractListModel):
    """Lightweight QAbstractListModel storing immutable ReviewRow items."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ReviewRow] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def row_at(self, row: int) -> ReviewRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def set_rows(self, rows: Sequence[ReviewRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def clear(self) -> None:
        if not self._rows:
            return
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def append_rows(self, rows: Sequence[ReviewRow]) -> None:
        if not rows:
            return
        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()

    def update_row(self, row: int, item: ReviewRow) -> None:
        if not 0 <= row < len(self._rows):
            return
        self._rows[row] = item
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.text
        if role == Qt.ItemDataRole.DecorationRole:
            return review_states.icon(row.state) if row.state is not None else None
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.resolved_tooltip()
        if role == review_states.STATE_ROLE:
            return row.state if row.state in review_states.STATES else (
                review_states.UNEVALUATED if row.state is not None else None
            )
        if role == Qt.ItemDataRole.UserRole:
            return row.user_data
        if role == AUX_ROLE:
            return row.aux_data
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return Qt.ItemFlag.NoItemFlags
        if self._rows[index.row()].selectable:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled


class ReviewFilterProxyModel(QSortFilterProxyModel):
    """Non-destructive key filter over a ReviewListModel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._filter_key: str | None = None

    def filter_key(self) -> str | None:
        return self._filter_key

    def set_filter_key(self, key: str | None) -> None:
        normalized = None if key in (None, "all") else key
        if normalized == self._filter_key:
            return
        self._filter_key = normalized
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        if self._filter_key is None:
            return True
        source = self.sourceModel()
        if not isinstance(source, ReviewListModel):
            return True
        row = source.row_at(source_row)
        if row is None:
            return False
        return self._filter_key in row.filter_keys


class CachedRowDelegate(QStyledItemDelegate):
    """Word-wrapping item delegate with width-keyed size-hint cache and Fluent active bar."""

    def __init__(self, parent: QListView) -> None:
        super().__init__(parent)
        self._size_cache: dict[tuple[int, str, float, bool, str], QSize] = {}
        self._last_width = -1
        self._last_font_key: tuple[str, float] | None = None

    def clear_cache(self) -> None:
        self._size_cache.clear()

    def _measure(self, view: QListView, font_metrics, font_key: tuple[str, float], text: str, has_icon: bool) -> QSize:
        width = view.viewport().width()
        if width <= 40:
            width = max(180, view.width() - 2 * view.frameWidth())
        if width != self._last_width or font_key != self._last_font_key:
            self._size_cache.clear()
            self._last_width = width
            self._last_font_key = font_key
        cache_key = (width, font_key[0], font_key[1], has_icon, text)
        cached = self._size_cache.get(cache_key)
        if cached is not None:
            return cached
        icon_width = 0
        if has_icon:
            size = view.iconSize()
            icon_width = (size.width() if size.isValid() else 16) + 10
        rect = font_metrics.boundingRect(
            0, 0, max(20, width - 22 - icon_width), 10_000, Qt.TextFlag.TextWordWrap, text
        )
        result = QSize(width, rect.height() + 14)
        if len(self._size_cache) >= _MAX_SIZE_CACHE:
            self._size_cache.clear()
        self._size_cache[cache_key] = result
        return result

    def size_for_index(self, index: QModelIndex) -> QSize:
        view = self.parent()
        if not isinstance(view, QListView) or not index.isValid():
            return QSize(0, 0)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(text, str) or not text:
            return QSize(max(40, view.viewport().width()), 28)
        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        has_icon = isinstance(decoration, QIcon) and not decoration.isNull()
        font = view.font()
        font_key = (font.family(), float(font.pointSizeF()))
        return self._measure(view, view.fontMetrics(), font_key, text, has_icon)

    def sizeHint(self, option, index: QModelIndex) -> QSize:  # noqa: N802
        view = self.parent()
        if not isinstance(view, QListView):
            return super().sizeHint(option, index)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not isinstance(text, str) or not text:
            return super().sizeHint(option, index)
        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        has_icon = isinstance(decoration, QIcon) and not decoration.isNull()
        font_key = (option.font.family(), float(option.font.pointSizeF()))
        return self._measure(view, option.fontMetrics, font_key, text, has_icon)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        if option.state & QStyle.StateFlag.State_Selected:
            tokens = theme.TOKENS_BY_SCHEME.get(theme.current_scheme(), theme.LIGHT_TOKENS)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens["accent"]))
            bar = QRect(option.rect.left() + 1, option.rect.top() + 4, 3, max(8, option.rect.height() - 8))
            painter.drawRoundedRect(bar, 1.5, 1.5)
            painter.restore()


class _ModelRowProxy:
    """Lightweight value accessor over one row of a ModelListView."""

    __slots__ = ("_view", "_row")

    def __init__(self, view: "ModelListView", row: int) -> None:
        self._view = view
        self._row = row

    def _index(self) -> QModelIndex:
        return self._view.model().index(self._row, 0)

    def text(self) -> str:
        val = self._index().data(Qt.ItemDataRole.DisplayRole)
        return val if isinstance(val, str) else ""

    def toolTip(self) -> str:  # noqa: N802
        val = self._index().data(Qt.ItemDataRole.ToolTipRole)
        return val if isinstance(val, str) else ""

    def icon(self) -> QIcon:
        val = self._index().data(Qt.ItemDataRole.DecorationRole)
        return val if isinstance(val, QIcon) else QIcon()

    def data(self, role: int = Qt.ItemDataRole.UserRole) -> Any:
        return self._index().data(role)

    def flags(self) -> Qt.ItemFlag:
        return self._view.model().flags(self._index())

    def setText(self, text: str) -> None:  # noqa: N802
        src_row = self._view._source_row(self._row)
        existing = self._view.source_model.row_at(src_row)
        if existing is not None:
            self._view.source_model.update_row(src_row, replace(existing, text=text))

    def setToolTip(self, tooltip: str) -> None:  # noqa: N802
        src_row = self._view._source_row(self._row)
        existing = self._view.source_model.row_at(src_row)
        if existing is not None:
            self._view.source_model.update_row(src_row, replace(existing, tooltip=tooltip))


class ModelListView(QListView):
    """QListView backed by ReviewListModel + ReviewFilterProxyModel + CachedRowDelegate."""

    currentRowChanged = Signal(int)
    itemClicked = Signal(object)

    def __init__(self, parent: QWidget | None = None, *, fit_rows: int | None = None) -> None:
        super().__init__(parent)
        self.source_model = ReviewListModel(self)
        self.proxy_model = ReviewFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)
        self.setModel(self.proxy_model)
        self.row_delegate = CachedRowDelegate(self)
        self.setItemDelegate(self.row_delegate)
        self.fit_rows = fit_rows
        self._emitted_row = -1
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.selectionModel().currentChanged.connect(self._on_current_changed)
        self.clicked.connect(self._on_clicked)

    def _source_row(self, proxy_row: int) -> int:
        idx = self.proxy_model.index(proxy_row, 0)
        if not idx.isValid():
            return -1
        return self.proxy_model.mapToSource(idx).row()

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        row = current.row() if current.isValid() else -1
        if row != self._emitted_row:
            self._emitted_row = row
            self.currentRowChanged.emit(row)

    def _on_clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self.itemClicked.emit(_ModelRowProxy(self, index.row()))

    # -- Model/View bulk API ------------------------------------------------

    def set_rows(self, rows: Sequence[ReviewRow]) -> None:
        self._emitted_row = -1
        self.source_model.set_rows(rows)
        self.clearSelection()
        self.setCurrentIndex(QModelIndex())
        if self.fit_rows:
            self.fit_height()

    def update_row(self, row: int, item: ReviewRow) -> None:
        self.source_model.update_row(row, item)

    def set_filter_key(self, key: str | None) -> None:
        self.proxy_model.set_filter_key(key)
        if self.fit_rows:
            self.fit_height()

    # -- Convenience row API (compatible with existing view & test callers) -

    def count(self) -> int:
        return self.proxy_model.rowCount()

    def currentRow(self) -> int:  # noqa: N802
        idx = self.currentIndex()
        return idx.row() if idx.isValid() else -1

    def setCurrentRow(self, row: int) -> None:  # noqa: N802
        if 0 <= row < self.count():
            idx = self.proxy_model.index(row, 0)
            self.setCurrentIndex(idx)
            if self.signalsBlocked():
                self._emitted_row = row
        else:
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
            if self.signalsBlocked():
                self._emitted_row = -1
            elif self._emitted_row != -1:
                self._emitted_row = -1
                self.currentRowChanged.emit(-1)

    def item(self, row: int) -> _ModelRowProxy | None:
        if not 0 <= row < self.count():
            return None
        return _ModelRowProxy(self, row)

    def scrollToItem(self, item: _ModelRowProxy | None) -> None:  # noqa: N802
        if item is None:
            return
        idx = self.proxy_model.index(item._row, 0)
        if idx.isValid():
            self.scrollTo(idx)

    def visualItemRect(self, item: _ModelRowProxy | None) -> QRect:  # noqa: N802
        if item is None:
            return QRect()
        idx = self.proxy_model.index(item._row, 0)
        if not idx.isValid():
            return QRect()
        return self.visualRect(idx)

    def clear(self) -> None:
        had_current = self._emitted_row != -1 or self.currentRow() != -1
        self.set_rows(())
        if had_current and not self.signalsBlocked():
            self.currentRowChanged.emit(-1)

    def addItem(self, item: str | ReviewRow) -> None:  # noqa: N802
        row = item if isinstance(item, ReviewRow) else ReviewRow(text=str(item))
        self.source_model.append_rows((row,))
        if self.fit_rows:
            self.fit_height()

    def addItems(self, texts: Sequence[str]) -> None:  # noqa: N802
        self.source_model.append_rows([ReviewRow(text=str(t)) for t in texts])
        if self.fit_rows:
            self.fit_height()

    def sizeHintForRow(self, row: int) -> int:  # noqa: N802
        if not 0 <= row < self.count():
            return -1
        return self.row_delegate.size_for_index(self.proxy_model.index(row, 0)).height()

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
        if event.size().width() != event.oldSize().width():
            self.row_delegate.clear_cache()
            self.scheduleDelayedItemsLayout()
            self.fit_height()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self.row_delegate.clear_cache()
            self.viewport().update()
