"""Lightweight unit tests for the Windows workbench performance benchmark scripts."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPARE_SCRIPT = REPO_ROOT / "scripts" / "compare_perf_revisions.py"
BENCH_SCRIPT = REPO_ROOT / "scripts" / "benchmark_windows_perf.py"


def _load_compare_module():
    spec = importlib.util.spec_from_file_location("compare_perf_revisions", COMPARE_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_comparison_computes_deltas_and_summary_schema() -> None:
    mod = _load_compare_module()

    def _stats(val: float) -> dict[str, float]:
        return {"min": val, "p50": val, "p95": val * 1.1, "max": val * 1.1}

    def _sample_revision(label: str, scale: float) -> dict:
        return {
            "label": label,
            "cold_start": {
                "samples": 1,
                "cold_start_ms": _stats(1000.0 * scale),
                "window_construct_ms": _stats(400.0 * scale),
                "subprocess_wall_ms": _stats(1100.0 * scale),
            },
            "idle_rss": {
                "samples": 1,
                "rss_mib": _stats(150.0 * scale),
                "tracemalloc_current_mib": _stats(30.0 * scale),
                "tracemalloc_peak_mib": _stats(31.0 * scale),
            },
            "typing_latency": {
                "segments": 500,
                "keystrokes": 6,
                "keystroke_dispatch_ms": _stats(100.0 * scale),
                "state_sync_ms": _stats(80.0 * scale),
                "combined_ms": _stats(180.0 * scale),
            },
            "large_manuscript_list_refresh": {
                "samples": 1,
                "list_backend": "test",
                "sizes": {
                    size: {
                        "segment_list_load_ms": _stats(50.0 * scale),
                        "term_report_load_ms": _stats(20.0 * scale),
                        "view_filter_cycle_ms": _stats(40.0 * scale),
                        "widget_bulk_load_ms": _stats(10.0 * scale),
                        "widget_filter_cycle_ms": _stats(12.0 * scale),
                    }
                    for size in ("200", "500", "1000")
                },
            },
            "scroll_perf": {
                "item_count": 1000,
                "scroll_steps": 5,
                "viewport_scroll_render_ms": _stats(40.0 * scale),
                "scroll_5_jumps_ms": _stats(4.0 * scale),
                "resize_relayout_render_ms": _stats(50.0 * scale),
            },
            "draft_recovery": {
                "segments": 500,
                "draft_kib": 1000.0,
                "deserialize_ms": _stats(20.0 * scale),
                "restore_workbench_ms": _stats(300.0 * scale),
                "total_ms": _stats(320.0 * scale),
            },
        }

    comparison = mod.build_comparison(_sample_revision("before", 1.0), _sample_revision("after", 0.8))
    assert comparison["before_label"] == "before"
    assert comparison["after_label"] == "after"
    by_metric = {row["metric"]: row for row in comparison["summary"]}
    assert by_metric["cold_start_ms (import + MainWindow)"]["delta_pct"] == -20.0
    assert by_metric["idle_rss_mib"]["delta_pct"] == -20.0
    assert by_metric["typing_keystroke_p50_ms (500 seg)"]["delta_pct"] == -20.0
    assert by_metric["manuscript_1000_segment_load_ms"]["delta_pct"] == -20.0
    assert by_metric["scroll_viewport_render_1000_ms"]["delta_pct"] == -20.0
    assert by_metric["draft_recovery_total_500_ms"]["delta_pct"] == -20.0

    rendered = mod.format_comparison_text(comparison)
    assert "Before/After Workbench Benchmark (before -> after)" in rendered
    assert "draft_recovery_total_500_ms" in rendered


def test_benchmark_windows_perf_quick_json_cli(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    sentinel = tmp_path / "sentinel_appdata"
    sentinel.mkdir()

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["APPDATA"] = str(sentinel)
    env["XDG_CONFIG_HOME"] = str(sentinel)

    proc = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--quick", "--items", "32", "--json"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "quick"
    for key in (
        "window_construct_ms",
        "editor_keystrokes_500_segments",
        "review_list_1024",
        "finding_locate_500_segments",
        "memory",
    ):
        assert key in data
    assert list(sentinel.iterdir()) == []


def test_compare_perf_revisions_quick_json_cli(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")
    sentinel = tmp_path / "sentinel_appdata"
    sentinel.mkdir()

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["APPDATA"] = str(sentinel)
    env["XDG_CONFIG_HOME"] = str(sentinel)

    proc = subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--repo-root",
            str(REPO_ROOT),
            "--quick",
            "--sizes",
            "50,150",
            "--json",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "quick"
    for key in (
        "cold_start",
        "idle_rss",
        "typing_latency",
        "large_manuscript_list_refresh",
        "scroll_perf",
        "draft_recovery",
    ):
        assert key in data
    assert set(data["large_manuscript_list_refresh"]["sizes"].keys()) == {"50", "150"}
    assert data["large_manuscript_list_refresh"]["sizes"]["50"]["term_group_count"] > 0
    assert data["large_manuscript_list_refresh"]["sizes"]["150"]["term_group_count"] > 0
    assert data["typing_latency"]["keystroke_dispatch_ms"]["p50"] > 0
    assert data["typing_latency"]["state_sync_ms"]["p95"] > 0
    assert data["draft_recovery"]["segments"] == 500
    assert list(sentinel.iterdir()) == []


def test_cold_start_child_script_disables_tracemalloc_by_default() -> None:
    mod = _load_compare_module()
    cold_only = mod._cold_start_child_script(enable_tracemalloc=False)
    assert "import tracemalloc" not in cold_only
    assert "tracemalloc.start()" not in cold_only
    with_tm = mod._cold_start_child_script(enable_tracemalloc=True)
    assert "import tracemalloc" in with_tm
    assert "tracemalloc.start()" in with_tm


def test_model_list_adapter_maps_verdict_states_and_keeps_filter_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    src_dir = str(REPO_ROOT / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    QApplication.instance() or QApplication([])

    mod = _load_compare_module()
    adapter = mod._make_list_adapter()
    specs = [
        ("Row 0", "verified_constraint", "tip0", 0),
        ("Row 1", "confirmed_conflict", "tip1", 1),
        ("Row 2", "needs_review", "tip2", 2),
        ("Row 3", "not_evaluated", "tip3", 3),
    ]
    adapter.set_rows(specs)
    try:
        states = [adapter.widget.source_model.row_at(i).state for i in range(4)]
        assert states == ["verified", "conflict", "needs_check", "unevaluated"]
        assert any(state != "unevaluated" for state in states)

        adapter.apply_filter("confirmed_conflict")
        assert adapter.count() == 1
        assert adapter.widget.item(0).data() == 1
        adapter.apply_filter("all")
        assert adapter.count() == 4
    finally:
        adapter.widget.close()
        adapter.widget.deleteLater()


def test_main_exports_after_ref_and_passes_custom_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_compare_module()
    exported: list[tuple[str, Path]] = []
    called_suites: list[tuple[Path, str, tuple[int, ...]]] = []

    def fake_export(repo_root: Path, ref: str, dest: Path) -> None:
        exported.append((ref, dest))
        dest.mkdir(parents=True, exist_ok=True)

    def fake_resolve(_repo_root: Path, ref: str) -> str:
        return f"sha-{ref}"

    def fake_run_suite(
        repo_root: Path,
        *,
        label: str,
        sizes: tuple[int, ...] = (200, 500, 1000),
        **_kwargs,
    ) -> dict:
        called_suites.append((repo_root, label, sizes))
        stats = {"min": 1.0, "p50": 1.0, "p95": 1.0, "max": 1.0}
        return {
            "label": label,
            "cold_start": {"samples": 1, "cold_start_ms": stats, "window_construct_ms": stats, "subprocess_wall_ms": stats},
            "idle_rss": {"samples": 1, "rss_mib": stats, "tracemalloc_current_mib": stats, "tracemalloc_peak_mib": stats},
            "typing_latency": {"keystroke_dispatch_ms": stats, "state_sync_ms": stats},
            "large_manuscript_list_refresh": {
                "sizes": {
                    str(sz): {
                        "segment_list_load_ms": stats,
                        "term_report_load_ms": stats,
                        "view_filter_cycle_ms": stats,
                        "widget_bulk_load_ms": stats,
                        "widget_filter_cycle_ms": stats,
                    }
                    for sz in sizes
                }
            },
            "scroll_perf": {
                "viewport_scroll_render_ms": stats,
                "scroll_5_jumps_ms": stats,
                "resize_relayout_render_ms": stats,
            },
            "draft_recovery": {"deserialize_ms": stats, "restore_workbench_ms": stats, "total_ms": stats},
        }

    monkeypatch.setattr(mod, "_export_git_ref", fake_export)
    monkeypatch.setattr(mod, "_resolve_git_sha", fake_resolve)
    monkeypatch.setattr(mod, "run_revision_suite", fake_run_suite)

    rc = mod.main(["--before-ref", "e6835f3", "--after-ref", "bd239c7", "--sizes", "40,120", "--quick", "--json"])
    assert rc == 0
    assert [ref for ref, _ in exported] == ["e6835f3", "bd239c7"]
    assert exported[0][1] != exported[1][1]
    assert called_suites[0][2] == (40, 120)
    assert called_suites[1][2] == (40, 120)
    assert called_suites[1][0] == exported[1][1]

    out = json.loads(capsys.readouterr().out)
    metrics = [row["metric"] for row in out["summary"]]
    assert "manuscript_40_segment_load_ms" in metrics
    assert "manuscript_120_segment_load_ms" in metrics
    assert "manuscript_200_segment_load_ms" not in metrics
