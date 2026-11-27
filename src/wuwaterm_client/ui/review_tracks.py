"""Modular right-inspector sub-track widgets and compact Fluent controls.

Splits the inspector's four visual areas out of ``manuscript_view.py``:
* :class:`LayersCardWidget` — quick deterministic format/number checks and style toggle
* :class:`TermsTrackWidget` — term consistency groups, ordered findings, ambiguous names, detail & replace
* :class:`SemanticTrackWidget` — semantic suspicion review, accept/adjust/ignore controls
* :class:`SegmentsTrackWidget` — paragraph pairing list, completion marks, choices, single-segment translation
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from .. import strings
from . import review_states
from .components import ChevronComboBox
from .icons import icon
from .review_models import ModelListView


class _ElidingButton(QPushButton):
    """A button that elides when narrower than its text so it never widens the inspector."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        metrics = self.fontMetrics()
        icon_w = self.iconSize().width() + 6 if not self.icon().isNull() else 0
        least = icon_w + metrics.horizontalAdvance(self.text()[:1] + "…") + 26
        return QSize(min(hint.width(), least), hint.height())

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.width() >= self.sizeHint().width():
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        room = contents.width() - (option.iconSize.width() + 6 if not option.icon.isNull() else 0)
        lines = option.text.split("\n")
        elided = "\n".join(
            option.fontMetrics.elidedText(line, Qt.TextElideMode.ElideRight, max(0, room)) for line in lines
        )
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
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideMiddle, max(0, self.contentsRect().width())
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_full_text"):
            self._update_elision()


class LayersCardWidget(QFrame):
    """Quick deterministic format/number check card and semantic overview."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("reviewStatus")
        self.setProperty("tone", "neutral")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.layers_title = QLabel(strings.MANUSCRIPT_LAYERS_TITLE, self)
        self.layers_title.setObjectName("sectionTitle")
        layout.addWidget(self.layers_title)

        self.l1_title = QLabel(strings.MANUSCRIPT_L1_TITLE, self)
        self.l1_title.setObjectName("statusText")
        self.l1_title.setWordWrap(True)
        self.l1_title.setToolTip(strings.MANUSCRIPT_L1_NOTE_DETAIL)
        layout.addWidget(self.l1_title)

        self.l1_note = QLabel(strings.MANUSCRIPT_L1_NOTE, self)
        self.l1_note.setObjectName("secondaryText")
        self.l1_note.setWordWrap(True)
        self.l1_note.setToolTip(strings.MANUSCRIPT_L1_NOTE_DETAIL)
        layout.addWidget(self.l1_note)

        self.l1_run_button = _ElidingButton(strings.MANUSCRIPT_L1_RUN.format(count=0), self)
        self.l1_run_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.l1_run_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.l1_run_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.l1_status = QLabel(strings.MANUSCRIPT_L1_NOT_RUN, self)
        self.l1_status.setObjectName("secondaryText")
        self.l1_status.setTextFormat(Qt.TextFormat.PlainText)
        self.l1_status.setWordWrap(True)
        layout.addWidget(self.l1_status)

        self.l1_list = ModelListView(self, fit_rows=3)
        self.l1_list.setMinimumHeight(52)
        self.l1_list.setAccessibleName(strings.MANUSCRIPT_L1_LIST)
        self.l1_list.hide()
        layout.addWidget(self.l1_list)

        self.l2_overview = QLabel(self)
        self.l2_overview.setObjectName("secondaryText")
        self.l2_overview.setTextFormat(Qt.TextFormat.PlainText)
        self.l2_overview.setWordWrap(True)
        self.l2_overview.hide()
        layout.addWidget(self.l2_overview)

        self.l3_title = QLabel(strings.MANUSCRIPT_L3_TITLE, self)
        self.l3_title.setObjectName("statusText")
        self.l3_title.setWordWrap(True)
        layout.addWidget(self.l3_title)

        self.l3_check = QCheckBox(strings.MANUSCRIPT_L3_TOGGLE, self)
        self.l3_check.setChecked(False)
        self.l3_check.setAccessibleName(strings.MANUSCRIPT_L3_TITLE)
        layout.addWidget(self.l3_check)

        self.l3_note = QLabel(strings.MANUSCRIPT_L3_OFF, self)
        self.l3_note.setObjectName("secondaryText")
        self.l3_note.setWordWrap(True)
        layout.addWidget(self.l3_note)


class TermsTrackWidget(QWidget):
    """Term review sub-track: grouped/ordered ModelListViews, ambiguous names, details & replace."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        promote_label: str = "",
        promote_tooltip: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorBody")
        term_details = QVBoxLayout(self)
        term_details.setContentsMargins(0, 0, 0, 0)
        term_details.setSpacing(8)

        self.terms_note = QLabel(strings.MANUSCRIPT_TERMS_NOTE, self)
        self.terms_note.setObjectName("secondaryText")
        self.terms_note.setWordWrap(True)
        term_details.addWidget(self.terms_note)

        self.term_toolbar = QWidget(self)
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
        term_bar.addWidget(self.term_view_combo, 1)
        term_bar.addWidget(self.term_filter_combo, 1)
        self.term_toolbar.hide()
        term_details.addWidget(self.term_toolbar)

        self.term_groups = ModelListView(self)
        self.term_groups.setMinimumHeight(140)
        self.term_groups.setMaximumHeight(175)
        self.term_groups.setAccessibleName(strings.MANUSCRIPT_INSPECTOR_TITLE)
        self.term_groups.hide()
        term_details.addWidget(self.term_groups)

        self.findings = ModelListView(self)
        self.findings.setMinimumHeight(76)
        self.findings.setMaximumHeight(220)
        self.findings.setAccessibleName(strings.MANUSCRIPT_INSPECTOR_TITLE)
        term_details.addWidget(self.findings)

        self.ambiguous_toggle = _ElidingButton(parent=self)
        self.ambiguous_toggle.setObjectName("commandButton")
        self.ambiguous_toggle.setCheckable(True)
        self.ambiguous_toggle.setIconSize(QSize(16, 16))
        self.ambiguous_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.ambiguous_toggle.hide()
        term_details.addWidget(self.ambiguous_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.ambiguous_area = QWidget(self)
        self.ambiguous_area.setObjectName("inspectorBody")
        ambiguous = QVBoxLayout(self.ambiguous_area)
        ambiguous.setContentsMargins(0, 0, 0, 0)
        ambiguous.setSpacing(6)
        self.ambiguous_note = QLabel(strings.MANUSCRIPT_AMBIGUOUS_NOTE, self.ambiguous_area)
        self.ambiguous_note.setObjectName("secondaryText")
        self.ambiguous_note.setWordWrap(True)
        ambiguous.addWidget(self.ambiguous_note)

        self.ambiguous_list = ModelListView(self.ambiguous_area, fit_rows=3)
        self.ambiguous_list.setMinimumHeight(52)
        self.ambiguous_list.setAccessibleName(strings.MANUSCRIPT_AMBIGUOUS_LIST)
        ambiguous.addWidget(self.ambiguous_list)

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
        self.ambiguous_ignore_button = _ElidingButton(strings.MANUSCRIPT_AMBIGUOUS_IGNORE, self.ambiguous_actions)
        for button in (self.ambiguous_adopt_button, self.ambiguous_ignore_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
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
        self.finding_title = QLabel(strings.MANUSCRIPT_FINDING_PROMPT, self)
        self.finding_title.setObjectName("findingTitle")
        self.finding_title.setWordWrap(True)
        pair.addWidget(self.finding_title)
        self.inspector_hint = QLabel(self)
        self.inspector_hint.setObjectName("secondaryText")
        self.inspector_hint.setWordWrap(True)
        pair.addWidget(self.inspector_hint)
        self.finding_pair = QLabel(self)
        self.finding_pair.setObjectName("findingPair")
        self.finding_pair.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_pair.setWordWrap(True)
        summary.addLayout(pair, 1)
        self.finding_verdict = QLabel(self)
        self.finding_verdict.setObjectName("findingVerdict")
        self.finding_verdict.setWordWrap(True)
        summary.addWidget(self.finding_verdict, 0, Qt.AlignmentFlag.AlignTop)
        term_details.addLayout(summary)
        term_details.addWidget(self.finding_pair)

        self.finding_reason = QLabel(self)
        self.finding_reason.setObjectName("secondaryText")
        self.finding_reason.setTextFormat(Qt.TextFormat.PlainText)
        self.finding_reason.setWordWrap(True)
        self.finding_reason.hide()
        term_details.addWidget(self.finding_reason)

        self.finding_rule = QLabel(self)
        self.finding_rule.setObjectName("secondaryText")
        self.finding_rule.setWordWrap(True)
        self.finding_positions = QLabel(self)
        self.finding_positions.setObjectName("secondaryText")
        self.finding_positions.setWordWrap(True)
        self.candidates_label = QLabel(self)
        self.candidates_label.setWordWrap(True)
        term_details.addWidget(self.candidates_label)

        self.choice_area = QWidget(self)
        self.choice_area.setObjectName("inspectorBody")
        choice_layout = QVBoxLayout(self.choice_area)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        choice_layout.setSpacing(6)
        choice_label = QLabel(strings.MANUSCRIPT_TERM_CHOICE_LABEL, self.choice_area)
        self.choice_combo = ChevronComboBox(self.choice_area)
        self.choice_combo.setAccessibleName(strings.MANUSCRIPT_TERM_CHOICE_LABEL)
        self.choice_combo.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.choice_combo.setMinimumContentsLength(8)
        self.choice_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        choice_label.setBuddy(self.choice_combo)
        self.choice_button = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_RECORD, self.choice_area)
        self.unchoice_button = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_REMOVE, self.choice_area)
        choice_buttons = QHBoxLayout()
        choice_buttons.setSpacing(6)
        for button in (self.choice_button, self.unchoice_button):
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

        self.lookup_button = _ElidingButton(strings.NAV_ITEM_TERMS, self)
        self.lookup_button.setIcon(icon("external-link"))
        self.lookup_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.replace_toggle = _ElidingButton(strings.MANUSCRIPT_REPLACE, self)
        self.replace_toggle.setCheckable(True)
        self.replace_toggle.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        term_actions = QHBoxLayout()
        term_actions.setSpacing(6)
        term_actions.addWidget(self.replace_toggle)
        term_actions.addWidget(self.lookup_button)
        term_actions.addStretch(1)
        term_details.addLayout(term_actions)

        self.promote_term_button = _ElidingButton(promote_label, self)
        self.promote_term_button.setToolTip(promote_tooltip)
        self.promote_term_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.promote_term_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        term_details.addWidget(self.promote_term_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.manual_edit_label = QLabel(strings.MANUSCRIPT_MANUAL_EDIT, self)
        self.manual_edit_label.setWordWrap(True)
        term_details.addWidget(self.manual_edit_label)

        self.replacement_area = QWidget(self)
        self.replacement_area.setObjectName("inspectorBody")
        replacement = QVBoxLayout(self.replacement_area)
        replacement.setContentsMargins(0, 0, 0, 0)
        replacement_label = QLabel(strings.MANUSCRIPT_REPLACEMENT, self.replacement_area)
        self.replacement_edit = QLineEdit(self.replacement_area)
        self.replacement_edit.setPlaceholderText(strings.MANUSCRIPT_REPLACEMENT_HINT)
        replacement_label.setBuddy(self.replacement_edit)
        self.replace_button = QPushButton(strings.MANUSCRIPT_REPLACE, self.replacement_area)
        replacement.addWidget(replacement_label)
        replacement.addWidget(self.replacement_edit)
        replacement.addWidget(self.replace_button)
        self.replace_toggle.toggled.connect(self.replacement_area.setVisible)
        term_details.addWidget(self.replacement_area)
        term_details.addStretch(1)


class SemanticTrackWidget(QWidget):
    """Semantic suspicion review sub-track with accept/adjust/ignore workflow."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        promote_label: str = "",
        promote_tooltip: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorBody")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.l2_title = QLabel(strings.MANUSCRIPT_L2_TITLE, self)
        self.l2_title.setObjectName("secondaryText")
        self.l2_title.setWordWrap(True)
        layout.addWidget(self.l2_title)

        self.semantic_badge = QLabel(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL, self)
        self.semantic_badge.setObjectName("experimentalBadge")
        self.semantic_badge.setToolTip(strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL_NOTE)
        layout.addWidget(self.semantic_badge, 0, Qt.AlignmentFlag.AlignLeft)

        self.l2_note = QLabel(strings.MANUSCRIPT_L2_NOTE, self)
        self.l2_note.setObjectName("secondaryText")
        self.l2_note.setWordWrap(True)
        layout.addWidget(self.l2_note)

        self.l2_covered = QLabel(self)
        self.l2_covered.setObjectName("secondaryText")
        self.l2_covered.setWordWrap(True)
        self.l2_covered.hide()
        layout.addWidget(self.l2_covered)

        self.semantic_note = QLabel(strings.MANUSCRIPT_SEMANTIC_NOTE, self)
        self.semantic_note.setObjectName("secondaryText")
        self.semantic_note.setWordWrap(True)
        layout.addWidget(self.semantic_note)

        self.semantic_progress = QLabel(strings.MANUSCRIPT_SEMANTIC_NOT_RUN, self)
        self.semantic_progress.setObjectName("statusText")
        self.semantic_progress.setWordWrap(True)

        heading = QHBoxLayout()
        self.issue_title = QLabel(strings.MANUSCRIPT_SEMANTIC_PROMPT, self)
        self.issue_title.setObjectName("findingTitle")
        self.issue_title.setTextFormat(Qt.TextFormat.PlainText)
        self.issue_title.setWordWrap(True)
        heading.addWidget(self.issue_title, 1)
        self.issue_state = QLabel(self)
        self.issue_state.setObjectName("findingVerdict")
        self.issue_state.setProperty("reviewState", review_states.UNEVALUATED)
        heading.addWidget(self.issue_state, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        self.issue_confidence = QLabel(self)
        self.issue_confidence.setObjectName("secondaryText")
        self.issue_confidence.setToolTip(strings.MANUSCRIPT_CONFIDENCE_NOTE)
        self.issue_excerpts = QLabel(self)
        self.issue_excerpts.setObjectName("findingPair")
        self.issue_rationale = QLabel(self)
        self.issue_suggestion = QLabel(self)
        self.issue_suggestion.setObjectName("suggestionText")
        self.issue_term_effect = QLabel(self)
        self.issue_term_effect.setObjectName("secondaryText")
        self.issue_notice = QLabel(self)
        self.issue_notice.setObjectName("noticeText")
        for label in (
            self.issue_confidence,
            self.issue_excerpts,
            self.issue_rationale,
            self.issue_suggestion,
            self.issue_term_effect,
            self.issue_notice,
        ):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)

        self.issue_alternative_button = QPushButton(self)
        self.issue_alternative_button.setObjectName("linkButton")
        self.issue_alternative_button.setCheckable(True)
        self.issue_alternative_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.issue_alternative_button, 0, Qt.AlignmentFlag.AlignLeft)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.accept_button = _ElidingButton(strings.MANUSCRIPT_ACCEPT, self)
        self.accept_button.setObjectName("primaryButton")
        self.accept_button.setToolTip(f"{strings.MANUSCRIPT_ACCEPT}（Ctrl+Enter）")
        self.adjust_button = _ElidingButton(strings.MANUSCRIPT_ADJUST, self)
        self.adjust_button.setToolTip(f"{strings.MANUSCRIPT_ADJUST}（Ctrl+Shift+E）")
        self.ignore_button = _ElidingButton(strings.MANUSCRIPT_IGNORE, self)
        self.ignore_button.setToolTip(f"{strings.MANUSCRIPT_IGNORE}（Ctrl+Shift+I）")
        for button in (self.accept_button, self.adjust_button, self.ignore_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.semantic_promote_button = _ElidingButton(promote_label, self)
        self.semantic_promote_button.setToolTip(promote_tooltip)
        self.semantic_promote_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.semantic_promote_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.semantic_promote_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.adjust_area = QWidget(self)
        self.adjust_area.setObjectName("inspectorBody")
        adjust = QVBoxLayout(self.adjust_area)
        adjust.setContentsMargins(0, 0, 0, 0)
        adjust_label = QLabel(strings.MANUSCRIPT_ADJUST_LABEL, self.adjust_area)
        self.adjust_edit = QPlainTextEdit(self.adjust_area)
        self.adjust_edit.setMaximumHeight(90)
        self.adjust_edit.setAccessibleName(strings.MANUSCRIPT_ADJUST_LABEL)
        adjust_label.setBuddy(self.adjust_edit)
        self.adjust_apply_button = QPushButton(strings.MANUSCRIPT_ADJUST_APPLY, self.adjust_area)
        adjust.addWidget(adjust_label)
        adjust.addWidget(self.adjust_edit)
        adjust.addWidget(self.adjust_apply_button)
        self.adjust_area.hide()
        layout.addWidget(self.adjust_area)

        self.semantic_list = ModelListView(self)
        self.semantic_list.setMinimumHeight(76)
        self.semantic_list.setMaximumHeight(180)
        self.semantic_list.setAccessibleName(strings.MANUSCRIPT_TRACK_SEMANTIC.format(count="").strip())
        layout.addWidget(self.semantic_list)

        self.low_toggle = _ElidingButton(parent=self)
        self.low_toggle.setObjectName("commandButton")
        self.low_toggle.setCheckable(True)
        self.low_toggle.setIconSize(QSize(16, 16))
        self.low_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.low_toggle.hide()
        layout.addWidget(self.low_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.handled_toggle = _ElidingButton(parent=self)
        self.handled_toggle.setObjectName("commandButton")
        self.handled_toggle.setCheckable(True)
        self.handled_toggle.setChecked(False)
        self.handled_toggle.setIconSize(QSize(16, 16))
        self.handled_toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.handled_toggle.hide()
        layout.addWidget(self.handled_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.shortcuts_label = QLabel(strings.MANUSCRIPT_SHORTCUTS, self)
        self.shortcuts_label.setObjectName("captionText")
        self.shortcuts_label.setWordWrap(True)
        layout.addWidget(self.shortcuts_label)
        layout.addStretch(1)


class SegmentsTrackWidget(QWidget):
    """Paragraph pairing & segment progress sub-track."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorBody")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.segments_note = QLabel(strings.MANUSCRIPT_SEGMENTS_NOTE, self)
        self.segments_note.setObjectName("secondaryText")
        self.segments_note.setWordWrap(True)
        layout.addWidget(self.segments_note)

        self.segment_list = ModelListView(self)
        self.segment_list.setMinimumHeight(76)
        self.segment_list.setMaximumHeight(220)
        self.segment_list.setAccessibleName(strings.MANUSCRIPT_TRACK_SEGMENTS.format(count="").strip())
        layout.addWidget(self.segment_list)

        self.segment_hint = QLabel(self)
        self.segment_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.segment_hint.setWordWrap(True)
        layout.addWidget(self.segment_hint)

        segment_actions = QHBoxLayout()
        segment_actions.setSpacing(6)
        self.finish_button = _ElidingButton(strings.MANUSCRIPT_SEGMENT_FINISH, self)
        self.next_pending_button = _ElidingButton(strings.MANUSCRIPT_NEXT_PENDING, self)
        for button in (self.finish_button, self.next_pending_button):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            segment_actions.addWidget(button)
        segment_actions.addStretch(1)
        layout.addLayout(segment_actions)

        self.segment_choices = ModelListView(self)
        self.segment_choices.setMaximumHeight(120)
        self.segment_choices.setAccessibleName(strings.MANUSCRIPT_SEGMENT_CHOICES)
        layout.addWidget(self.segment_choices)

        choice_actions = QHBoxLayout()
        choice_actions.setSpacing(6)
        self.segment_choice_action = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_RECONFIRM, self)
        self.segment_choice_remove = _ElidingButton(strings.MANUSCRIPT_TERM_CHOICE_REMOVE, self)
        for button in (self.segment_choice_action, self.segment_choice_remove):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            choice_actions.addWidget(button)
        choice_actions.addStretch(1)
        layout.addLayout(choice_actions)

        self.translate_segment_button = _ElidingButton(strings.MANUSCRIPT_TRANSLATE_SEGMENT, self)
        self.translate_segment_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.translate_segment_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.translate_segment_button)
        layout.addStretch(1)
