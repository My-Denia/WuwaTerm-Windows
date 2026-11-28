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
            "--json",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
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
    assert set(data["large_manuscript_list_refresh"]["sizes"].keys()) == {"200", "500", "1000"}
    assert data["typing_latency"]["keystroke_dispatch_ms"]["p50"] > 0
    assert data["typing_latency"]["state_sync_ms"]["p95"] > 0
    assert data["draft_recovery"]["segments"] == 500
    assert list(sentinel.iterdir()) == []
