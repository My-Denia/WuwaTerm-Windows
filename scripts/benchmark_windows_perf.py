"""Repeatable offline performance benchmark for WuwaTerm Windows workbench.

Measures five core desktop interactivity metrics under ``QT_QPA_PLATFORM=offscreen``
without touching the network, real user config, or Windows Credential Manager:

1. Cold start / ``MainWindow`` construction latency
2. 10,000-character / 500-segment manuscript keystroke latency
3. 1,024-item review list (``ModelListView``) bulk refresh latency
4. List scroll & finding locate latency
5. Process RSS & Python ``tracemalloc`` peak memory

Usage:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/benchmark_windows_perf.py --quick
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/benchmark_windows_perf.py --samples 5 --json
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from wuwaterm_client import theme  # noqa: E402
from wuwaterm_client.api import ApiClient  # noqa: E402
from wuwaterm_client.config import ClientConfig  # noqa: E402
from wuwaterm_client.manuscript import empty_workfile  # noqa: E402
from wuwaterm_client.ui.main_window import MainWindow  # noqa: E402
from wuwaterm_client.ui.manuscript_view import ManuscriptView  # noqa: E402
from wuwaterm_client.ui.review_models import ModelListView, ReviewRow  # noqa: E402


def _rss_mib() -> float:
    """Return current process resident set size in MiB (best-effort cross-platform)."""
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return round(int(parts[1]) / 1024.0, 2)
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(usage / 1024.0, 2)
    except Exception:
        return 0.0


def _build_large_manuscript(segments: int = 500) -> tuple[str, str]:
    src_parts = [f"第{i + 1:03d}段：共鸣者漂泊者在今州城核对声骸共鸣频率与数值 {i + 10}。" for i in range(segments)]
    tgt_parts = [f"Segment {i + 1:03d}: Rover verifies the Echo resonance value {i + 10} in Jinzhou." for i in range(segments)]
    return "\n\n".join(src_parts), "\n\n".join(tgt_parts)


def _summarize(values_ms: list[float]) -> dict[str, float]:
    ordered = sorted(values_ms)
    p95_idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_idx], 3),
        "max_ms": round(ordered[-1], 3),
    }


def bench_cold_start(app: QApplication, samples: int) -> dict[str, float]:
    timings: list[float] = []
    for _ in range(samples):
        gc.collect()
        t0 = time.perf_counter()
        window = MainWindow(config=ClientConfig())
        window.resize(1280, 800)
        window.show()
        app.processEvents()
        elapsed = (time.perf_counter() - t0) * 1000.0
        timings.append(elapsed)
        window.close()
        window.deleteLater()
        app.processEvents()
    return _summarize(timings)


def bench_editor_keystrokes(app: QApplication, keystrokes: int) -> dict[str, float]:
    source, target = _build_large_manuscript(500)
    view = ManuscriptView(ApiClient(None))
    view.resize(1280, 760)
    view.show()
    payload = empty_workfile()
    payload.update(source=source, target=target, direction="en")
    view._set_payload(payload)
    view._refresh_derived()
    view.target_edit.setFocus()
    view.target_edit.set_raw_cursor_offset(len(target) // 2)
    app.processEvents()

    timings: list[float] = []
    try:
        for _ in range(keystrokes):
            t0 = time.perf_counter()
            QTest.keyClick(view.target_edit, Qt.Key.Key_A)
            app.processEvents()
            timings.append((time.perf_counter() - t0) * 1000.0)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
    return {
        **_summarize(timings),
        "source_chars": len(source),
        "target_chars": len(target),
        "keystrokes": keystrokes,
    }


def bench_review_list_refresh(app: QApplication, item_count: int, samples: int) -> dict[str, float]:
    view = ModelListView()
    view.resize(320, 480)
    view.show()
    app.processEvents()

    states = ("verified", "conflict", "needs_check", "stale", "unevaluated")
    rows = [
        ReviewRow(
            text=f"共鸣者术语 #{i + 1}\nResonator Term #{i + 1} · 第 {(i % 500) + 1} 段",
            state=states[i % len(states)],
            tooltip=f"Tooltip detail for item {i + 1}",
            user_data=i,
        )
        for i in range(item_count)
    ]

    refresh_ms: list[float] = []
    scroll_ms: list[float] = []
    try:
        for _ in range(samples):
            t0 = time.perf_counter()
            view.set_rows(rows)
            app.processEvents()
            refresh_ms.append((time.perf_counter() - t0) * 1000.0)

            t1 = time.perf_counter()
            for target_row in (0, item_count // 4, item_count // 2, (item_count * 3) // 4, item_count - 1):
                view.setCurrentRow(target_row)
                view.scrollToItem(view.item(target_row))
            app.processEvents()
            scroll_ms.append((time.perf_counter() - t1) * 1000.0)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()

    return {
        "item_count": item_count,
        "bulk_refresh": _summarize(refresh_ms),
        "scroll_and_select_5_jumps": _summarize(scroll_ms),
    }


def bench_finding_locate(app: QApplication, samples: int) -> dict[str, float]:
    source, target = _build_large_manuscript(500)
    view = ManuscriptView(ApiClient(None))
    view.resize(1280, 760)
    view.show()
    payload = empty_workfile()
    payload.update(source=source, target=target, direction="en")
    view._set_payload(payload)
    app.processEvents()

    offsets = [source.index(f"第{k:03d}段") for k in (1, 125, 250, 375, 500)]
    timings: list[float] = []
    try:
        for _ in range(samples):
            for off in offsets:
                span = {"start": off, "end": off + 6, "text": source[off : off + 6]}
                t0 = time.perf_counter()
                ok = view.source_edit.select_raw_span(span, move_cursor=True, pulse=True)
                app.processEvents()
                if not ok:
                    raise RuntimeError(f"failed to locate span at offset {off}")
                timings.append((time.perf_counter() - t0) * 1000.0)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
    return _summarize(timings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WuwaTerm Windows workbench offline performance benchmark")
    parser.add_argument("--quick", action="store_true", help="Single-sample smoke run for fast local checks")
    parser.add_argument("--samples", type=int, default=3, help="Sample iterations per benchmark (default: 3)")
    parser.add_argument("--items", type=int, default=1024, help="Review list row count (default: 1024)")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args(argv)

    samples = 1 if args.quick else max(1, args.samples)
    keystrokes = 8 if args.quick else 30

    tracemalloc.start()
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, theme.SCHEME_LIGHT)

    results = {
        "mode": "quick" if args.quick else "standard",
        "platform": sys.platform,
        "qpa_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "cold_start": bench_cold_start(app, samples),
        "editor_keystrokes_500_segments": bench_editor_keystrokes(app, keystrokes),
        "review_list_1024": bench_review_list_refresh(app, args.items, samples),
        "finding_locate_500_segments": bench_finding_locate(app, samples),
    }
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results["memory"] = {
        "rss_mib": _rss_mib(),
        "tracemalloc_current_mib": round(current_bytes / (1024 * 1024), 2),
        "tracemalloc_peak_mib": round(peak_bytes / (1024 * 1024), 2),
    }

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("=== WuwaTerm Windows Workbench Offline Benchmark ===")
        print(f"Mode: {results['mode']} | QPA: {results['qpa_platform']} | Platform: {results['platform']}")
        print(f"1. Cold start (MainWindow)       : median {results['cold_start']['median_ms']:.2f} ms (p95 {results['cold_start']['p95_ms']:.2f} ms)")
        ks = results["editor_keystrokes_500_segments"]
        print(
            f"2. Keystroke (500 seg / {ks['source_chars']}ch): median {ks['median_ms']:.2f} ms (p95 {ks['p95_ms']:.2f} ms)"
        )
        rl = results["review_list_1024"]
        print(
            f"3. Review list ({rl['item_count']} rows)      : refresh median {rl['bulk_refresh']['median_ms']:.2f} ms | "
            f"5-jump scroll median {rl['scroll_and_select_5_jumps']['median_ms']:.2f} ms"
        )
        loc = results["finding_locate_500_segments"]
        print(f"4. Finding locate (500 seg)      : median {loc['median_ms']:.2f} ms (p95 {loc['p95_ms']:.2f} ms)")
        mem = results["memory"]
        print(
            f"5. Memory                        : RSS {mem['rss_mib']:.2f} MiB | "
            f"tracemalloc peak {mem['tracemalloc_peak_mib']:.2f} MiB"
        )
        print(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
