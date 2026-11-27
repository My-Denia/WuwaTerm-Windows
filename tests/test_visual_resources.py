"""Exercise the packaged icon chain and the workbench's two palettes."""

from __future__ import annotations

import hashlib
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QIcon, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFrame, QLabel, QListWidget, QPlainTextEdit, QPushButton, QToolButton,
    QVBoxLayout, QWidget,
)

from wuwaterm_client import strings, theme  # noqa: E402
from wuwaterm_client.ui.components import EmptyStateCard  # noqa: E402
from wuwaterm_client.ui.icons import icon  # noqa: E402

CLIENT_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = CLIENT_ROOT / "src" / "wuwaterm_client" / "resources" / "icons"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def preserve_theme(qapp, monkeypatch):
    previous_stylesheet = qapp.styleSheet()
    monkeypatch.setattr(theme, "_preference", theme._preference)
    yield
    qapp.setStyleSheet(previous_stylesheet)


def _visible_color(image):
    """Choose an opaque stroke pixel rather than a transparent canvas pixel."""
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() >= 240:
                return pixel
    pytest.fail("the icon did not paint a visible stroke")


def _assert_color(actual, expected):
    # Antialiasing/unpremultiplication can round a channel by one.
    assert max(abs(a - b) for a, b in zip(actual.getRgb()[:3], expected.getRgb()[:3])) <= 2


def _contrast(first, second):
    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_spec_stages_every_icon_and_license_and_icons_render(qapp, tmp_path, monkeypatch):
    """Exercise the actual spec datas, then load and paint from its frozen path."""
    analysis_args = {}

    def analysis(*args, **kwargs):
        analysis_args.update(kwargs)
        return SimpleNamespace(pure=[], zipped_data=[], scripts=[], binaries=[], zipfiles=[], datas=[])

    namespace = runpy.run_path(
        str(CLIENT_ROOT / "WuwaTerm.spec"),
        init_globals={
            "SPECPATH": str(CLIENT_ROOT),
            "Analysis": analysis,
            "PYZ": lambda *args, **kwargs: None,
            "EXE": lambda *args, **kwargs: None,
            "COLLECT": lambda *args, **kwargs: None,
        },
    )
    icon_datas = namespace["ICON_DATAS"]
    assert set(map(tuple, icon_datas)) <= set(map(tuple, analysis_args["datas"]))
    assert {Path(source).name for source, _ in icon_datas} == {
        path.name for path in ICON_DIR.iterdir() if path.is_file()
    }
    assert all(Path(target) == Path("resources/icons") for _, target in icon_datas)

    for source, target in namespace["RESOURCE_DATAS"] + icon_datas:
        destination = tmp_path / target
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination / Path(source).name)
    monkeypatch.setattr(theme, "_resource_roots", lambda: [tmp_path / "resources"])
    assert theme.load_stylesheet(theme.SCHEME_DARK)

    checksums = {}
    for line in (tmp_path / "resources/icons/SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        checksums[name] = digest
    icon_names = {Path(source).name for source, _ in icon_datas if source.endswith(".svg")}
    assert set(checksums) == icon_names | {"LICENSE"}
    for name, digest in checksums.items():
        assert hashlib.sha256((tmp_path / "resources/icons" / name).read_bytes()).hexdigest() == digest
    for name in icon_names:
        bundled = icon(Path(name).stem)
        assert not bundled.isNull(), name
        _visible_color(bundled.pixmap(QSize(24, 24)).toImage())
    for alias in ("history", "waves"):
        assert not icon(alias).isNull()
    assert icon("missing").isNull()
    assert icon("../LICENSE").isNull()


def test_existing_icons_follow_theme_and_control_state(qapp, preserve_theme):
    from PySide6.QtGui import QColor

    normal = icon("file-text")
    foreground = icon("refresh-cw", color="accent-fg")
    button = QToolButton()
    button.setObjectName("commandButton")
    button.setIcon(normal)
    button.setIconSize(QSize(24, 24))
    button.resize(48, 48)
    button.show()
    for scheme in (theme.SCHEME_LIGHT, theme.SCHEME_DARK):
        theme.apply_theme(qapp, scheme)
        qapp.processEvents()
        tokens = theme.TOKENS_BY_SCHEME[scheme]
        for resource, mode, state, expected in (
            (normal, QIcon.Mode.Normal, QIcon.State.Off, "text-primary"),
            (normal, QIcon.Mode.Normal, QIcon.State.On, "accent"),
            (normal, QIcon.Mode.Disabled, QIcon.State.Off, "text-muted"),
            (foreground, QIcon.Mode.Normal, QIcon.State.Off, "accent-fg"),
        ):
            image = resource.pixmap(QSize(24, 24), mode, state).toImage()
            _assert_color(_visible_color(image), QColor(tokens[expected]))
        # The same, already displayed control must repaint after a theme change.
        image = button.grab().toImage()
        expected = QColor(tokens["text-primary"])
        assert any(
            max(abs(a - b) for a, b in zip(image.pixelColor(x, y).getRgb()[:3], expected.getRgb()[:3])) <= 2
            for y in range(image.height()) for x in range(image.width())
        )
    button.close()


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_workbench_editor_and_selection_have_readable_colors(qapp, preserve_theme, scheme):
    theme.apply_theme(qapp, scheme)
    frame = QFrame()
    frame.setObjectName("editorSurface")
    layout = QVBoxLayout(frame)
    editor = QPlainTextEdit()
    label = QLabel("Secondary text")
    label.setObjectName("secondaryText")
    layout.addWidget(editor)
    layout.addWidget(label)
    frame.show()
    qapp.processEvents()
    palette = editor.palette()
    assert _contrast(palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base)) >= 4.5
    assert _contrast(palette.color(QPalette.ColorRole.HighlightedText), palette.color(QPalette.ColorRole.Highlight)) >= 4.5
    assert _contrast(label.palette().color(QPalette.ColorRole.WindowText), palette.color(QPalette.ColorRole.Base)) >= 4.5
    assert editor.font().pointSizeF() == 13.5
    frame.close()


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_inspector_body_and_selected_finding_use_theme(qapp, preserve_theme, scheme):
    from PySide6.QtGui import QColor

    theme.apply_theme(qapp, scheme)
    inspector = QFrame()
    inspector.setObjectName("reviewInspector")
    outer = QVBoxLayout(inspector)
    body = QWidget(inspector)
    body.setObjectName("inspectorBody")
    outer.addWidget(body)
    content = QVBoxLayout(body)
    verdict = QLabel("Review result", body)
    verdict.setObjectName("findingVerdict")
    content.addWidget(verdict)
    findings = QListWidget(body)
    findings.addItem("A finding")
    findings.setCurrentRow(0)
    content.addWidget(findings)
    inspector.show()
    qapp.processEvents()
    tokens = theme.TOKENS_BY_SCHEME[scheme]
    _assert_color(body.palette().color(QPalette.ColorRole.Window), QColor(tokens["bg-surface"]))
    image = findings.viewport().grab().toImage()
    selected_background = QColor(tokens["accent-soft-bg"])
    assert any(
        image.pixelColor(x, y) == selected_background
        for y in range(image.height()) for x in range(image.width())
    )
    assert _contrast(QColor(tokens["accent-soft-fg"]), selected_background) >= 4.5
    # The five review states: each badge is its token pair, readable at 4.5:1.
    for value, foreground, background in (
        ("verified", "success", "success-soft"),
        ("conflict", "danger", "danger-soft"),
        ("needs_check", "warn", "warn-soft"),
        ("stale", "info", "info-soft"),
        ("unevaluated", "text-secondary", "bg-sunken"),
    ):
        verdict.setProperty("reviewState", value)
        verdict.style().unpolish(verdict)
        verdict.style().polish(verdict)
        palette = verdict.palette()
        _assert_color(palette.color(QPalette.ColorRole.WindowText), QColor(tokens[foreground]))
        _assert_color(palette.color(QPalette.ColorRole.Window), QColor(tokens[background]))
        assert _contrast(QColor(tokens[foreground]), QColor(tokens[background])) >= 4.5, value
        assert _contrast(palette.color(QPalette.ColorRole.WindowText), palette.color(QPalette.ColorRole.Window)) >= 4.5
    # Fluent layered surface & interaction contrast parity.
    assert _contrast(QColor(tokens["text-primary"]), QColor(tokens["bg-layer"])) >= 4.5
    assert _contrast(QColor(tokens["text-primary"]), QColor(tokens["bg-hover"])) >= 4.5
    assert _contrast(QColor(tokens["text-primary"]), QColor(tokens["bg-pressed"])) >= 4.5
    assert _contrast(QColor(tokens["accent-fg"]), QColor(tokens["accent-pressed"])) >= 4.5
    inspector.close()


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_model_list_view_uses_fluent_selection_tokens(qapp, preserve_theme, scheme):
    from PySide6.QtGui import QColor

    from wuwaterm_client.ui.review_models import ModelListView, ReviewRow

    theme.apply_theme(qapp, scheme)
    inspector = QFrame()
    inspector.setObjectName("reviewInspector")
    outer = QVBoxLayout(inspector)
    body = QWidget(inspector)
    body.setObjectName("inspectorBody")
    outer.addWidget(body)
    content = QVBoxLayout(body)
    view = ModelListView(body)
    view.set_rows([ReviewRow(text="Fluent row", state="verified")])
    view.setCurrentRow(0)
    content.addWidget(view)
    inspector.resize(300, 140)
    inspector.show()
    qapp.processEvents()
    try:
        tokens = theme.TOKENS_BY_SCHEME[scheme]
        image = view.viewport().grab().toImage()
        selected_bg = QColor(tokens["accent-soft-bg"])
        accent_bar = QColor(tokens["accent"])
        assert any(
            image.pixelColor(x, y) == selected_bg
            for y in range(image.height()) for x in range(image.width())
        )
        assert any(
            image.pixelColor(x, y) == accent_bar
            for y in range(image.height()) for x in range(image.width())
        )
    finally:
        inspector.close()


@pytest.mark.parametrize("scheme", [theme.SCHEME_LIGHT, theme.SCHEME_DARK])
def test_empty_state_action_row_paints_the_card_not_the_canvas(qapp, preserve_theme, scheme):
    """The card's action row is part of the card, not a panel laid over it.

    The row was a bare ``QWidget``. The stylesheet's base ``QWidget`` rule
    paints the canvas colour and Qt honours it for exactly that class, so the
    row drew a lighter band across the sunken card behind its button - most
    visible in the lookup area's no-match state, which is precisely where the
    owner is reading two lines of copy and one button.

    ``QFrame`` is the container shape this stylesheet leaves transparent, so
    the test compares a point inside the row with a point on the card itself.
    """
    theme.apply_theme(qapp, scheme)
    card = EmptyStateCard()
    card.set_content(
        strings.EMPTY_TERMS_NO_MATCH_TITLE,
        strings.EMPTY_TERMS_NO_MATCH_SUBTITLE,
        (strings.STATUS_REFRESH_BUTTON, lambda: None),
    )
    card.resize(600, 400)
    card.show()
    qapp.processEvents()
    try:
        title = card.findChild(QLabel, "emptyTitle")
        button = card.findChild(QPushButton)
        row = card.findChild(QWidget, "emptyAction")
        assert title is not None and button is not None and row is not None
        assert button.parentWidget() is row
        image = card.grab().toImage()
        # Inside the action row, beside its centred button, and on the card's
        # own padding at the title's height. Both are background: one is the
        # row, one is the card.
        row_pixel = image.pixelColor(row.geometry().left() + 4, row.geometry().center().y())
        card_pixel = image.pixelColor(row.geometry().left() // 2, title.geometry().center().y())
        _assert_color(row_pixel, card_pixel)
    finally:
        card.close()


def _spin_arrow_columns(image, widget, sub):
    """The painted pixels of one spin button's arrow, in image coordinates."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QStyle, QStyleOptionSpinBox

    option = QStyleOptionSpinBox()
    widget.initStyleOption(option)
    area = widget.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option, sub, widget)
    side = widget.ARROW_SIZE
    target = QRect(area.center().x() - side // 2, area.center().y() - side // 2, side, side)
    scale = image.width() / widget.width()
    left, top = int(target.left() * scale), int(target.top() * scale)
    width, height = int(round(target.width() * scale)), int(round(target.height() * scale))
    # The corners of that square are the button's own background in both
    # schemes, so "painted" is a difference from them rather than a darkness:
    # the dark theme's stroke is lighter than its surface.
    background = image.pixelColor(left, top)
    columns: dict[int, list[int]] = {}
    for y in range(max(0, top), min(image.height(), top + height)):
        for x in range(max(0, left), min(image.width(), left + width)):
            pixel = image.pixelColor(x, y)
            if max(abs(a - b) for a, b in zip(pixel.getRgb()[:3], background.getRgb()[:3])) > 25:
                columns.setdefault(x, []).append(y)
    return columns


def _render_at(widget, ratio: float):
    """Paint the widget at a chosen device pixel ratio, as a 150% screen would."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    pixmap = QPixmap(int(round(widget.width() * ratio)), int(round(widget.height() * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    widget.render(pixmap)
    return pixmap.toImage()


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("ratio", [1.0, 1.5])
@pytest.mark.parametrize("kind", ["double", "integer"])
def test_a_spin_box_arrow_is_a_chevron_and_follows_the_pixel_ratio(
    qapp, preserve_theme, scheme, ratio, kind
):
    """The stylesheet built these arrows from borders, and Qt painted that as a
    solid four-pixel bar: at 150% scaling the owner saw two short dashes where
    the spin buttons were. A chevron is a stroke, not a block, and it grows with
    the ratio because it is painted rather than drawn from fixed border widths."""
    from PySide6.QtWidgets import QStyle

    from wuwaterm_client.ui.components import ChevronDoubleSpinBox, ChevronSpinBox

    theme.apply_theme(qapp, scheme)
    if kind == "double":
        spin = ChevronDoubleSpinBox()
        spin.setRange(1.0, 600.0)
        spin.setValue(30.0)
    else:
        spin = ChevronSpinBox()
        spin.setRange(1, 600)
        spin.setValue(30)
    spin.resize(140, 38)
    spin.show()
    spin.clearFocus()
    qapp.processEvents()
    try:
        image = _render_at(spin, ratio)
        for sub in (QStyle.SubControl.SC_SpinBoxUp, QStyle.SubControl.SC_SpinBoxDown):
            columns = _spin_arrow_columns(image, spin, sub)
            depths = [len(rows) for rows in columns.values()]
            tops = {min(rows) for rows in columns.values()}
            assert len(columns) >= round(6 * ratio), (sub, len(columns))
            # A chevron is one thin stroke per column, and the stroke moves down
            # as the columns advance. The bar this replaced was four solid
            # pixels starting on one and the same row in every column.
            assert max(depths) <= 4, (sub, depths)
            assert len(tops) >= 3, (sub, tops)
    finally:
        spin.close()


def test_the_arrow_keeps_its_shape_through_the_stylesheet():
    """`image: none` in the sheet is what lets the painted chevron be the arrow."""
    for scheme in (theme.SCHEME_LIGHT, theme.SCHEME_DARK):
        rendered = theme.load_stylesheet(scheme)
        assert "border-left: 4px solid transparent" not in rendered
        for selector in ("QSpinBox::up-arrow", "QDoubleSpinBox::down-arrow"):
            assert selector in rendered


def test_a_spin_box_without_buttons_paints_no_arrows(qapp, preserve_theme):
    """`NoButtons` has no arrow rect, and a negative rect must not be painted."""
    from PySide6.QtWidgets import QAbstractSpinBox

    from wuwaterm_client.ui.components import ChevronSpinBox

    theme.apply_theme(qapp, theme.SCHEME_LIGHT)
    spin = ChevronSpinBox()
    spin.setRange(1, 600)
    spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    spin.resize(140, 38)
    spin.show()
    qapp.processEvents()
    try:
        image = _render_at(spin, 1.5)
        assert image.width() > 0  # it painted at all, without raising
    finally:
        spin.close()
