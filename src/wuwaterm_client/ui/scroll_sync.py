"""Synchronized vertical scrolling of the source and target editors.

When the owner scrolls one editor, the other follows so the passage at the top
of one stays beside its translation in the other. The mapping goes through
anchors - pairs of raw offsets ``(source, target)`` where the two texts are
known to correspond, the starts of the paired segments - and interpolates
between the two anchors around the top line. Without anchors (a pairing not
computed yet, or one that is not monotonic) it falls back to the scroll ratio.

Three things keep it from fighting the owner:

* a reentrancy lock: the follower's own scroll signal never syncs back;
* ``suspended()``: commands that position both editors themselves (undo,
  selecting a finding, restoring a work position) do so without the first
  scroll dragging the second editor somewhere else;
* a switch, on by default, that the owner can turn off.

The editors only need two methods, ``raw_offset_at_top()`` and
``scroll_to_raw_offset(offset)``; one without them is synced by ratio.
"""
from __future__ import annotations

import bisect
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QPlainTextEdit

Anchors = Sequence[tuple[int, int]]


def monotonic_anchors(anchors: Anchors | None) -> list[tuple[int, int]] | None:
    """``anchors`` sorted, deduplicated and checked: both coordinates must never
    go down, or the mapping between them is not a function."""
    if not anchors:
        return None
    points = sorted(set((int(a), int(b)) for a, b in anchors))
    for (a0, b0), (a1, b1) in zip(points, points[1:]):
        if a1 < a0 or b1 < b0:
            return None
    return points


def map_offset(anchors: list[tuple[int, int]], offset: int, *, forward: bool) -> int:
    """The offset on the other side that corresponds to ``offset``.

    ``forward`` maps source offsets to target offsets; otherwise the reverse.
    Between two anchors the position is interpolated linearly; outside them it
    is the nearest anchor (the anchors end with the two texts' ends).
    """
    origin = [a if forward else b for a, b in anchors]
    other = [b if forward else a for a, b in anchors]
    index = bisect.bisect_right(origin, offset) - 1
    if index < 0:
        return other[0]
    if index >= len(origin) - 1:
        return other[-1]
    span = origin[index + 1] - origin[index]
    if span <= 0:
        return other[index]
    fraction = (offset - origin[index]) / span
    return round(other[index] + fraction * (other[index + 1] - other[index]))


class ScrollSync(QObject):
    """Keep two editors' vertical scroll positions in step."""

    toggled = Signal(bool)

    def __init__(self, source: QPlainTextEdit, target: QPlainTextEdit,
                 anchors: Callable[[], Anchors | None] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._target = target
        self._anchors = anchors or (lambda: None)
        self._enabled = True
        self._lock = False
        self._suspended = 0
        source.verticalScrollBar().valueChanged.connect(lambda _value: self._follow(source))
        target.verticalScrollBar().valueChanged.connect(lambda _value: self._follow(target))

    # -- the switch --------------------------------------------------------
    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self.toggled.emit(enabled)
        if enabled:
            # Turning it on lines the target up with the source at once.
            self._follow(self._source)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Scroll both editors on purpose without either one dragging the other."""
        self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1

    # -- following ---------------------------------------------------------
    def _follow(self, origin: QPlainTextEdit) -> None:
        if not self._enabled or self._lock or self._suspended:
            return
        follower = self._target if origin is self._source else self._source
        self._lock = True
        try:
            if not self._by_anchor(origin, follower):
                self._by_ratio(origin, follower)
        finally:
            self._lock = False

    def _by_anchor(self, origin: QPlainTextEdit, follower: QPlainTextEdit) -> bool:
        top = getattr(origin, "raw_offset_at_top", None)
        scroll = getattr(follower, "scroll_to_raw_offset", None)
        if top is None or scroll is None:
            return False
        anchors = monotonic_anchors(self._anchors())
        if anchors is None or len(anchors) < 2:
            return False
        offset = top()
        if offset is None:
            return False
        scroll(map_offset(anchors, offset, forward=origin is self._source))
        return True

    @staticmethod
    def _by_ratio(origin: QPlainTextEdit, follower: QPlainTextEdit) -> None:
        source_bar, follower_bar = origin.verticalScrollBar(), follower.verticalScrollBar()
        span = source_bar.maximum() - source_bar.minimum()
        ratio = 0.0 if span <= 0 else (source_bar.value() - source_bar.minimum()) / span
        follower_bar.setValue(round(follower_bar.minimum()
                                    + ratio * (follower_bar.maximum() - follower_bar.minimum())))
