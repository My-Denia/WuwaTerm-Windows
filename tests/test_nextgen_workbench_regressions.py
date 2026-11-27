"""Regression tests for PR #69 independent review findings.

Covers:
1. Switching manuscripts preserves service-level ``live_review_dictionary`` while
   endpoint/credential changes still clear it.
2. ``ModelListView.clear()`` emits ``currentRowChanged(-1)`` once when a row was
   selected, zero times when unselected, and respects ``signalsBlocked()``.
3. Clearing extra selections cancels ``LosslessEditor``'s locate pulse timer so
   stale source highlights never resurrect after switching to a target finding.
4. ``scripts/benchmark_windows_perf.py`` isolates user profile/TM paths, reports
   ``window_construct_ms`` (not ``cold_start``), and returns ``None`` (never 0)
   when RSS cannot be queried.
5. Structure-check title does not misstate server-side execution as local rules.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from wuwaterm_client import strings  # noqa: E402
from wuwaterm_client import translation_memory as tm  # noqa: E402
from wuwaterm_client.api import ApiClient  # noqa: E402
from wuwaterm_client.manuscript import empty_workfile, save_workfile  # noqa: E402
from wuwaterm_client.ui.editor_widget import LosslessEditor  # noqa: E402
from wuwaterm_client.ui.manuscript_view import ManuscriptView  # noqa: E402
from wuwaterm_client.ui.review_models import ModelListView, ReviewRow  # noqa: E402

REVISION = hashlib.sha256(b"live-review-dict").hexdigest()
STAMP = "2026-11-30T00:00:00Z"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_switching_manuscript_preserves_live_review_dictionary_provenance(qapp, tmp_path: Path) -> None:
    memory = tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)
    memory.add(
        "Second doc.",
        "第二篇。",
        "en",
        prev_source="First line.",
        origin="human_confirmed",
        terms_db_revision=REVISION,
    )

    other_path = tmp_path / "other.json"
    other = empty_workfile()
    source = "First line. Second doc."
    target = "甲句。"
    other.update(
        source=source,
        target=target,
        direction="en",
        alignments=[
            {
                "source": {"start": 0, "end": len("First line."), "text": "First line."},
                "target": {"start": 0, "end": len(target), "text": target},
            },
            {
                "source": {
                    "start": source.index("Second doc."),
                    "end": source.index("Second doc.") + len("Second doc."),
                    "text": "Second doc.",
                },
                "target": None,
            },
        ],
    )
    save_workfile(other_path, other)

    view = ManuscriptView(ApiClient(None), translation_memory=memory)
    try:
        view._record_live_review_dictionary({"revision": REVISION, "term_count": 100})
        assert view._terms_db_revision() == REVISION
        assert view._controller._tm_provenance()[0] == REVISION

        # Creating a new manuscript or opening another workfile must keep service-level live provenance.
        assert view.new_document()
        assert view._live_review_dictionary is not None
        assert view._terms_db_revision() == REVISION

        assert view.open_path(other_path)
        assert view._live_review_dictionary is not None
        assert view._terms_db_revision() == REVISION
        view._check_memory_reuse()
        assert view.tm_panel.summary is not None
        assert view.tm_panel.summary.fresh == 1
        assert view.tm_panel.summary.fillable == 1

        # Endpoint / credential changes still clear live provenance.
        view.reset_for_endpoint_change()
        assert view._live_review_dictionary is None
        assert view._terms_db_revision() is None

        view._record_live_review_dictionary({"revision": REVISION, "term_count": 100})
        assert view._terms_db_revision() == REVISION
        view.reset_for_credential_change()
        assert view._live_review_dictionary is None
        assert view._terms_db_revision() is None
    finally:
        view.close()


def test_model_list_view_clear_emits_current_row_changed_once_and_respects_block_signals(qapp) -> None:
    listing = ModelListView()
    emitted: list[int] = []
    listing.currentRowChanged.connect(emitted.append)
    try:
        listing.set_rows([ReviewRow(text="Row 1"), ReviewRow(text="Row 2")])
        listing.setCurrentRow(0)
        emitted.clear()

        # Clearing with a selected row emits -1 exactly once.
        listing.clear()
        assert emitted == [-1]
        assert listing.currentRow() == -1

        # Clearing again when already empty / unselected emits nothing.
        emitted.clear()
        listing.clear()
        assert emitted == []

        # Clearing when nothing is selected emits nothing.
        listing.set_rows([ReviewRow(text="Row 1")])
        emitted.clear()
        listing.clear()
        assert emitted == []

        # Respects caller signalsBlocked().
        listing.set_rows([ReviewRow(text="Row 1"), ReviewRow(text="Row 2")])
        listing.setCurrentRow(1)
        emitted.clear()
        listing.blockSignals(True)
        listing.clear()
        listing.blockSignals(False)
        assert emitted == []
        assert listing.currentRow() == -1
    finally:
        listing.close()


def test_clearing_extra_selections_cancels_locate_pulse_timer(qapp) -> None:
    editor = LosslessEditor()
    try:
        editor.set_raw_text("Alpha term beta")
        span = {"start": 6, "end": 10, "text": "term"}
        assert editor.select_raw_span(span, move_cursor=True, pulse=True)
        assert editor._locate_pulse_timer.isActive()
        assert len(editor.extraSelections()) == 1

        # Clearing highlights cancels the pulse timer so nothing resurrects after 180 ms.
        editor.setExtraSelections([])
        assert not editor._locate_pulse_timer.isActive()
        assert editor.extraSelections() == []

        QTest.qWait(250)
        assert editor.extraSelections() == []
    finally:
        editor.close()


def test_switching_l1_source_finding_to_target_finding_does_not_resurrect_source_highlight(qapp) -> None:
    view = ManuscriptView(ApiClient(None))
    try:
        payload = empty_workfile()
        payload.update(source="Source 123 text", target="Target 456 text", direction="en")
        view._set_payload(payload)
        view._l1_rows = [
            (0, {"rule_id": "number_missing", "source_span": {"start": 7, "end": 10, "text": "123"}, "target_span": None}),
            (0, {"rule_id": "number_unexpected", "source_span": None, "target_span": {"start": 7, "end": 10, "text": "456"}}),
        ]

        view._select_l1_row(0)
        assert len(view.source_edit.extraSelections()) == 1
        assert view.source_edit._locate_pulse_timer.isActive()

        # Immediately jump to target-only finding: source highlight clears and stays cleared.
        view._select_l1_row(1)
        assert view.source_edit.extraSelections() == []
        assert not view.source_edit._locate_pulse_timer.isActive()
        assert len(view.target_edit.extraSelections()) == 1

        QTest.qWait(250)
        assert view.source_edit.extraSelections() == []
        assert len(view.target_edit.extraSelections()) == 1
    finally:
        view.close()


def test_benchmark_isolates_user_profile_and_reports_window_construct_and_none_rss(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_windows_perf.py"
    spec = importlib.util.spec_from_file_location("benchmark_windows_perf", script_path)
    assert spec is not None and spec.loader is not None
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    # Point real APPDATA at a sentinel directory; the benchmark must never create WuwaTerm/tm.sqlite3 there.
    sentinel_appdata = tmp_path / "real_appdata"
    sentinel_appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(sentinel_appdata))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(sentinel_appdata))

    results = bench.run_benchmark(quick=True, items=32)
    assert "window_construct_ms" in results
    assert "cold_start" not in results
    assert list(sentinel_appdata.iterdir()) == []
    assert os.environ.get("APPDATA") == str(sentinel_appdata)

    # When OS RSS lookup fails, _rss_mib returns None instead of 0.0.
    monkeypatch.setattr(bench.sys, "platform", "win32")
    monkeypatch.setattr(bench, "_windows_rss_mib", lambda: None)
    assert bench._rss_mib() is None


def test_structure_check_title_does_not_claim_local_rules() -> None:
    assert "本地规则" not in strings.MANUSCRIPT_L1_TITLE
    assert "提交当前段落" not in strings.MANUSCRIPT_L1_NOTE_DETAIL
    assert "分批核查当前稿件中符合条件的各段" in strings.MANUSCRIPT_L1_NOTE_DETAIL
