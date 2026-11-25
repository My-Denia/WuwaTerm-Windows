"""The five review states, shown the same way on every row and badge.

Segments, term findings, term groups and semantic issues each have their own
vocabulary of states. To the owner they collapse to five, with the meanings the
Web workbench gives them:

* ``verified`` 已核 - checked against the current basis and in order;
* ``conflict`` 冲突 - the translation contradicts a chosen or official term;
* ``needs_check`` 需核对 - something has to be looked at or decided;
* ``stale`` 待复核 - a result exists but the text or basis moved since, or it was
  read from a file and this session has not checked its basis yet;
* ``unevaluated`` 未评估 - nothing was evaluated yet (not reviewed, not
  translated, or deliberately left out of the check).

Each state has a colour pair taken from the theme tokens and a shape, so a row
reads the same without colour: a check, a cross, an exclamation mark, a
circular arrow, a hollow ring. A saved result never reads as ``verified``
before this session has checked its basis.
"""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .. import strings, theme

VERIFIED = "verified"
CONFLICT = "conflict"
NEEDS_CHECK = "needs_check"
STALE = "stale"
UNEVALUATED = "unevaluated"
STATES = (VERIFIED, CONFLICT, NEEDS_CHECK, STALE, UNEVALUATED)

LABELS = {
    VERIFIED: strings.REVIEW_STATE_VERIFIED,
    CONFLICT: strings.REVIEW_STATE_CONFLICT,
    NEEDS_CHECK: strings.REVIEW_STATE_NEEDS_CHECK,
    STALE: strings.REVIEW_STATE_STALE,
    UNEVALUATED: strings.REVIEW_STATE_UNEVALUATED,
}

# Theme tokens per state: (strong colour for the shape and ring, soft fill).
_TOKENS = {
    VERIFIED: ("success", "success-soft"),
    CONFLICT: ("danger", "danger-soft"),
    NEEDS_CHECK: ("warn", "warn-soft"),
    STALE: ("info", "info-soft"),
    UNEVALUATED: ("text-secondary", "bg-sunken"),
}

# The role a list item carries its state in, for tests and accessibility.
STATE_ROLE = Qt.ItemDataRole.UserRole + 7


def for_verdict(verdict: str | None, *, saved: bool = False, recheck: bool = False) -> str:
    """A term finding's verdict (review-v2), as one of the five states."""
    if saved or recheck:
        return STALE
    return {
        "verified_constraint": VERIFIED,
        "confirmed_conflict": CONFLICT,
        "needs_review": NEEDS_CHECK,
    }.get(verdict or "", UNEVALUATED)


def for_group(status: str, *, saved: bool = False) -> str:
    """A term group's status (``segment_records.group_status``)."""
    if saved and status == "pending":
        return STALE
    return {"confirmed": VERIFIED, "inconsistent": CONFLICT, "pending": NEEDS_CHECK}.get(status, UNEVALUATED)


def for_segment(status: str) -> str:
    """A segment row's state (``strings.MANUSCRIPT_SEGMENT_STATUS`` keys)."""
    return {
        "reviewed": VERIFIED,
        "saved": STALE,
        "stale": STALE,
        "failed": NEEDS_CHECK,
        "not_reviewed": NEEDS_CHECK,
        "needs_manual_split": NEEDS_CHECK,
        "unpaired_target": NEEDS_CHECK,
    }.get(status, UNEVALUATED)


def for_issue(state: str) -> str:
    """A semantic issue's state (``semantic_session.state_of``)."""
    return {
        "pending": NEEDS_CHECK,
        "needs_reconfirm": STALE,
        "stale": STALE,
        "accepted": VERIFIED,
        "adjusted": VERIFIED,
    }.get(state, UNEVALUATED)


def colors(state: str, scheme: str | None = None) -> tuple[QColor, QColor]:
    """The (strong, soft) colours of ``state`` in the rendered scheme."""
    tokens = theme.TOKENS_BY_SCHEME.get(scheme or theme.current_scheme(), theme.LIGHT_TOKENS)
    strong, soft = _TOKENS.get(state, _TOKENS[UNEVALUATED])
    return QColor(tokens[strong]), QColor(tokens[soft])


def _shape(state: str, size: float) -> QPainterPath:
    path = QPainterPath()
    c = size / 2
    r = size * 0.22
    if state == VERIFIED:
        path.moveTo(c - r, c + r * 0.05)
        path.lineTo(c - r * 0.25, c + r * 0.75)
        path.lineTo(c + r, c - r * 0.7)
    elif state == CONFLICT:
        path.moveTo(c - r * 0.8, c - r * 0.8)
        path.lineTo(c + r * 0.8, c + r * 0.8)
        path.moveTo(c + r * 0.8, c - r * 0.8)
        path.lineTo(c - r * 0.8, c + r * 0.8)
    elif state == NEEDS_CHECK:
        path.moveTo(c, c - r)
        path.lineTo(c, c + r * 0.25)
        path.moveTo(c, c + r * 0.85)
        path.lineTo(c, c + r * 0.9)
    elif state == STALE:
        rect = QRectF(c - r, c - r, 2 * r, 2 * r)
        path.arcMoveTo(rect, 60)
        path.arcTo(rect, 60, 270)
        tip = path.currentPosition()
        path.moveTo(tip)
        path.lineTo(tip + QPointF(r * 0.55, -r * 0.05))
        path.moveTo(tip)
        path.lineTo(tip + QPointF(r * 0.05, -r * 0.6))
    return path


@lru_cache(maxsize=32)
def _icon(state: str, scheme: str) -> QIcon:
    strong, soft = colors(state, scheme)
    icon = QIcon()
    for ratio in (1, 2, 3):
        size = 16 * ratio
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QPen(strong, 1.2 * ratio)
        painter.setPen(ring)
        painter.setBrush(soft if state != UNEVALUATED else Qt.BrushStyle.NoBrush)
        inset = 1.5 * ratio
        painter.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
        if state != UNEVALUATED:
            pen = QPen(strong, 1.8 * ratio)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(_shape(state, size))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def icon(state: str) -> QIcon:
    """The state's badge icon in the rendered scheme (cached per scheme)."""
    return _icon(state if state in STATES else UNEVALUATED, theme.current_scheme())


def mark(item, state: str, *, tooltip_prefix: bool = True) -> None:
    """Give a list row its state's icon, its state data and a tooltip naming it.

    The row text is left alone: the icon and the tooltip carry the state, so
    the words the row already says are not repeated.
    """
    state = state if state in STATES else UNEVALUATED
    item.setIcon(icon(state))
    item.setData(STATE_ROLE, state)
    if tooltip_prefix:
        existing = item.toolTip()
        label = LABELS[state]
        item.setToolTip(f"{label}\n{existing}" if existing else label)
