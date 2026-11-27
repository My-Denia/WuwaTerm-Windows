"""The inspector section: confirmed translations for the segment at the caret.

The panel is a reference surface. It reads the local memory for one segment and
names every candidate it finds: how it matches (the same text in the same
neighbours, the same text anywhere, the same text with its numbers changed, or
the same text once whitespace is collapsed) and why it may no longer hold (the
terms revision or the game version moved, or the numbers differ).

It never writes text, the manuscript file or the memory by itself. The two
writes belong to the workbench, because both are edits of the open document and
one of them must be undoable: adopting a candidate is a single ``EditHistory``
step, and recording the active segment is the person saying so. The panel asks
for them with ``apply_requested``, ``confirm_requested``, ``check_requested``
and ``fill_requested`` and then shows what came back.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .. import strings
from .. import translation_memory as tm
from .icons import icon
from .tm_dialog import origin_label, refusal_text

# Five candidates, as in the terms panel: enough to pick from, few enough that
# the inspector does not become a list to scroll past.
MAX_LISTED_MATCHES = 5
# The longest source the store holds (``tm.SOURCE_SCALARS``); a longer one is
# refused as a value, which is not the same thing as a broken file.
MAX_SOURCE = tm.SOURCE_SCALARS[1]

_MATCH_LABELS = {
    tm.TM_MATCH_CONTEXT_EXACT: strings.TM_MATCH_CONTEXT_EXACT,
    tm.TM_MATCH_EXACT: strings.TM_MATCH_EXACT,
    tm.TM_MATCH_NUMERIC_VARIANT: strings.TM_MATCH_NUMERIC,
    tm.TM_MATCH_NORMALIZED_EXACT: strings.TM_MATCH_NORMALIZED,
}
_STALE_LABELS = {
    tm.TM_STALE_TERMS_REVISION: strings.TM_STALE_TERMS,
    tm.TM_STALE_GAME_VERSION: strings.TM_STALE_GAME,
    tm.TM_STALE_NUMERIC: strings.TM_STALE_NUMERIC,
}


def match_kind_label(kind: str) -> str:
    """The badge for one match kind, in the workbench's words."""
    return _MATCH_LABELS.get(kind, kind)


def stale_labels(reasons: tuple[str, ...]) -> list[str]:
    """The warnings one match carries, in the order the memory recorded them."""
    return [_STALE_LABELS.get(reason, reason) for reason in reasons]


@dataclass(frozen=True)
class ActiveSegment:
    """What a lookup needs to know about the segment at the caret.

    ``prev_source`` and ``next_source`` are the neighbours' source text (None at
    either end), ``terms_db_revision`` the revision the open document's last
    review named, when this session has one.
    """

    source: str
    target: str
    direction: str
    prev_source: str | None = None
    next_source: str | None = None
    terms_db_revision: str | None = None


@dataclass(frozen=True)
class ReuseSummary:
    """What ``classify_segments_for_tm`` found, counted for the heading line."""

    fresh: int
    stale: int
    unmatched: int
    fillable: int
    skipped: int = 0


class TranslationMemoryPanel(QFrame):
    """What the memory holds for the active segment, and the two writes it asks for."""

    manage_requested = Signal()
    apply_requested = Signal(object)
    confirm_requested = Signal()
    check_requested = Signal()
    fill_requested = Signal()

    def __init__(self, memory: tm.TranslationMemory | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tmPanel")
        self._memory = memory
        self._active: ActiveSegment | None = None
        self._matches: list[tm.TmMatch] = []
        self._summary: ReuseSummary | None = None

        self.title_label = QLabel(strings.TM_PANEL_TITLE, self)
        self.title_label.setObjectName("sectionTitle")
        # Wraps in the narrowest inspector instead of widening it.
        self.title_label.setWordWrap(True)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("secondaryText")
        self.count_label.setWordWrap(True)
        self.manage_button = QPushButton(strings.TM_PANEL_MANAGE, self)
        self.manage_button.setObjectName("commandButton")
        self.manage_button.setToolTip(strings.TM_DIALOG_TITLE)
        self.manage_button.clicked.connect(self.manage_requested.emit)
        self.toggle_button = QPushButton(self)
        self.toggle_button.setObjectName("commandButton")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setFixedWidth(34)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.toggle_button.setAccessibleName(strings.TM_PANEL_TITLE)
        self.toggle_button.setIconSize(QSize(16, 16))
        self.toggle_button.toggled.connect(self.set_expanded)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self.toggle_button)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.manage_button)

        self.badge_label = QLabel(strings.TM_PANEL_BADGE, self)
        self.badge_label.setObjectName("secondaryText")
        self.badge_label.setWordWrap(True)
        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("secondaryText")
        self.notice_label.setWordWrap(True)
        self.reuse_label = QLabel(self)
        self.reuse_label.setObjectName("secondaryText")
        self.reuse_label.setWordWrap(True)
        self.reuse_label.hide()
        # A long source or target wraps inside the inspector; it never widens it.
        for label in (self.count_label, self.badge_label, self.notice_label, self.reuse_label):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._hits_host = QWidget(self)
        self._hits_host.setObjectName("inspectorBody")
        self._hits_layout = QVBoxLayout(self._hits_host)
        self._hits_layout.setContentsMargins(0, 0, 0, 0)
        self._hits_layout.setSpacing(5)

        self.confirm_button = QPushButton(strings.TM_PANEL_CONFIRM, self)
        self.confirm_button.setObjectName("secondaryButton")
        self.confirm_button.setToolTip(strings.TM_PANEL_CONFIRM_TOOLTIP)
        self.confirm_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.confirm_button.clicked.connect(self.confirm_requested.emit)
        self.check_button = QPushButton(strings.TM_PANEL_CHECK, self)
        self.check_button.setObjectName("secondaryButton")
        self.check_button.setToolTip(strings.TM_PANEL_CHECK_TOOLTIP)
        self.check_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.check_button.clicked.connect(self.check_requested.emit)
        self.fill_button = QPushButton(self)
        self.fill_button.setObjectName("secondaryButton")
        self.fill_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.fill_button.setVisible(False)
        self.fill_button.clicked.connect(self.fill_requested.emit)
        # One action per row, hugging its own text: both labels are longer than
        # half the narrowest inspector, and a button whose text is elided is not
        # a button.
        actions = QVBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        for button in (self.confirm_button, self.check_button):
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            actions.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)

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
        # The two writes come before the list: the confirmation is the frequent
        # action, and it is not worth scrolling past five candidates to reach.
        body.addLayout(actions)
        body.addWidget(self.badge_label)
        body.addWidget(self.notice_label)
        body.addWidget(self._hits_host)
        body.addWidget(self.reuse_label)
        body.addWidget(self.fill_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._body)
        self.set_expanded(True)
        self.refresh()

    def set_expanded(self, expanded: bool) -> None:
        """The candidates and their notes shown, or only the title line and the count."""
        self._body.setVisible(expanded)
        self.toggle_button.setIcon(icon("chevron-down" if expanded else "chevron-right"))
        self.toggle_button.setToolTip(strings.TM_PANEL_COLLAPSE if expanded else strings.TM_PANEL_EXPAND)
        if self.toggle_button.isChecked() != expanded:
            self.toggle_button.setChecked(expanded)

    def is_expanded(self) -> bool:
        return self.toggle_button.isChecked()

    # -- what the panel shows ----------------------------------------------------

    @property
    def memory(self) -> tm.TranslationMemory | None:
        return self._memory

    @property
    def matches(self) -> list[tm.TmMatch]:
        return list(self._matches)

    @property
    def summary(self) -> ReuseSummary | None:
        return self._summary

    def hit_texts(self) -> list[str]:
        """The visible text of every listed candidate, in order (for tests and accessibility)."""
        out = []
        for index in range(self._hits_layout.count()):
            widget = self._hits_layout.itemAt(index).widget()
            if widget is not None and widget.objectName() == "tmMatch":
                out.append("\n".join(label.text() for label in widget.findChildren(QLabel)))
        return out

    def match_at(self, index: int) -> tm.TmMatch | None:
        return self._matches[index] if 0 <= index < len(self._matches) else None

    def set_context(self, active: ActiveSegment | None) -> None:
        """The segment the matches are read for, or None when there is none."""
        self._active = active
        self.refresh()

    def refresh(self) -> None:
        """Re-read the memory for the current segment. Reads only.

        The notice goes back to the state of this segment: an outcome line (a
        confirmation, a refusal) belongs to the moment it was shown, and the
        workbench sets it again after a refresh when it still applies.
        """
        self._matches = []
        self._summary = None
        self.reuse_label.hide()
        self.fill_button.hide()
        self._clear_hits()
        memory = self._memory
        if memory is None:
            self.count_label.setText("")
            self._set_notice(strings.TM_PANEL_NO_MEMORY)
            self._set_actions_enabled(False, False)
            return
        self.count_label.setText(strings.TM_PANEL_COUNT.format(n=len(memory) if memory.usable else 0))
        if not memory.usable:
            self._set_notice(strings.TM_PANEL_UNUSABLE.format(reason=refusal_text(memory.error)))
            self._set_actions_enabled(False, False)
            return
        active = self._active
        if active is None:
            self._set_notice(strings.TM_PANEL_NO_SEGMENT)
            self._set_actions_enabled(False, True)
            return
        if not active.source.strip():
            # A target-only gap (a pairing's unpaired target): nothing to look
            # up, and a lookup of blank text is a refused value, not a broken
            # file. Saying the file is broken here would be a false alarm.
            self._set_notice(strings.TM_PANEL_NO_SOURCE)
            self._set_actions_enabled(False, True)
            return
        if len(active.source) > MAX_SOURCE:
            # Longer than the store holds: the value would be refused, so it is
            # not looked up and not reported as a file problem. Checking reuse
            # still works, because it skips only the segments it cannot read.
            self._set_notice(strings.TM_PANEL_SOURCE_TOO_LONG.format(
                length=len(active.source), max=MAX_SOURCE))
            self._set_actions_enabled(False, True)
            return
        self._set_actions_enabled(bool(active.target.strip()), True)
        try:
            self._matches = memory.lookup_segment(
                active.source,
                active.direction,
                prev_source=active.prev_source,
                next_source=active.next_source,
                current_terms_db_revision=active.terms_db_revision,
                limit=MAX_LISTED_MATCHES,
            )
        except (tm.TmError, OSError) as error:
            self._set_notice(strings.TM_PANEL_UNUSABLE.format(reason=refusal_text(error)))
            self._set_actions_enabled(False, False)
            return
        if not self._matches:
            self._set_notice(strings.TM_PANEL_NO_MATCH)
            return
        self.count_label.setText(
            strings.TM_PANEL_COUNT.format(n=len(memory))
            + " · " + strings.TM_PANEL_MATCH_COUNT.format(n=len(self._matches)))
        self._set_notice("")
        for index, match in enumerate(self._matches):
            self._hits_layout.addWidget(self._match_card(match, index))

    def show_notice(self, text: str) -> None:
        """One line about what just happened (a confirmation, a refusal)."""
        self._set_notice(text)

    def _set_notice(self, text: str) -> None:
        # An empty label still reserves its line height; a gap where a sentence
        # would be reads as a missing sentence.
        self.notice_label.setText(text)
        self.notice_label.setVisible(bool(text))

    def show_reuse(self, summary: ReuseSummary) -> None:
        """List what the reuse check found, with the fill action it allows."""
        self._summary = summary
        lines = [strings.TM_PANEL_CHECK_SUMMARY.format(
            fresh=summary.fresh, stale=summary.stale, unmatched=summary.unmatched)]
        if summary.skipped:
            lines.append(strings.TM_PANEL_CHECK_SKIPPED.format(n=summary.skipped, max=MAX_SOURCE))
        self.reuse_label.setText("\n".join(lines))
        self.reuse_label.show()
        if summary.fillable:
            self.fill_button.setText(strings.TM_PANEL_CHECK_FILL.format(n=summary.fillable))
            self.fill_button.show()

    def clear_reuse(self) -> None:
        self._summary = None
        self.reuse_label.hide()
        self.fill_button.hide()

    # -- internals ---------------------------------------------------------------

    def _set_actions_enabled(self, confirm: bool, check: bool) -> None:
        self.confirm_button.setEnabled(confirm)
        self.check_button.setEnabled(check)

    def _clear_hits(self) -> None:
        while self._hits_layout.count():
            item = self._hits_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _match_card(self, match: tm.TmMatch, index: int) -> QFrame:
        entry = match.entry
        card = QFrame(self._hits_host)
        card.setObjectName("tmMatch")
        card.setToolTip(strings.TM_PANEL_BADGE)
        lines = QVBoxLayout(card)
        lines.setContentsMargins(10, 4, 10, 4)
        lines.setSpacing(1)
        badges = [match_kind_label(match.match_kind)]
        warnings = stale_labels(match.stale_reasons)
        if warnings:
            badges.append(" · ".join(warnings))
        kind_label = QLabel(" · ".join(badges), card)
        kind_label.setTextFormat(Qt.TextFormat.PlainText)
        kind_label.setWordWrap(True)
        preview = QLabel(entry.target_text, card)
        preview.setTextFormat(Qt.TextFormat.PlainText)
        preview.setWordWrap(True)
        detail = QLabel(strings.TM_MATCH_DETAIL.format(count=entry.use_count, origin=origin_label(entry.origin)), card)
        detail.setObjectName("secondaryText")
        detail.setWordWrap(True)
        lines.addWidget(kind_label)
        lines.addWidget(preview)
        if match.needs_recheck:
            recheck = QLabel(strings.TM_MATCH_RECHECK.format(reasons="、".join(stale_labels(match.stale_reasons))),
                             card)
            recheck.setObjectName("secondaryText")
            recheck.setTextFormat(Qt.TextFormat.PlainText)
            recheck.setWordWrap(True)
            recheck.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lines.addWidget(recheck)
        apply_button = QPushButton(strings.TM_MATCH_APPLY, card)
        apply_button.setObjectName("secondaryButton")
        apply_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        apply_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        apply_button.clicked.connect(lambda _checked=False, position=index: self.apply_at(position))
        # The provenance and the button on one row: three wrapped lines of a
        # candidate and then its button made the inspector a scrolling list.
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        footer.setSpacing(6)
        footer.addWidget(detail, 1)
        footer.addWidget(apply_button, 0, Qt.AlignmentFlag.AlignRight)
        lines.addLayout(footer)
        for label in card.findChildren(QLabel):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return card

    def apply_at(self, index: int) -> None:
        """The person chose one candidate: ask the workbench to write it."""
        match = self.match_at(index)
        if match is not None:
            self.apply_requested.emit(match)
