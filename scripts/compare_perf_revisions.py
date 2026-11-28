"""Cross-revision offline performance benchmark for WuwaTerm Windows workbench.

Measures the 6 workbench interactivity and resource metrics under identical
methodology and synthetic fixtures across two revisions (e.g. ``e6835f3``
before the PR #69 modularization/Model-View refactor vs ``HEAD``):

1. Cold start (subprocess import + Qt/theme init + ``MainWindow`` ready)
2. Idle RSS (resident set size & ``tracemalloc`` immediately after idle startup)
3. Typing latency (500-segment keystroke dispatch & state-sync P50/P95)
4. Large manuscript list refresh (200 / 500 / 1000 segments load & filter refresh)
5. Scroll performance (1000-row viewport scroll traversal + delegate rendering)
6. Draft recovery (500-segment v5 ``draft.json`` deserialization & workbench restore)

Usage:
    QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py --quick --json
    QT_QPA_PLATFORM=offscreen python scripts/compare_perf_revisions.py \\
        --before-root /tmp/win-bench-before --after-root . --json
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROFILE_ENV_VARS = ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
_BENCH_SIZES = (200, 500, 1000)


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_rss_mib() -> float | None:
    """Query current process WorkingSetSize on Windows via psapi/kernel32."""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetCurrentProcess()
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        for dll_name, fn_name in (("psapi", "GetProcessMemoryInfo"), ("kernel32", "K32GetProcessMemoryInfo")):
            dll = getattr(ctypes.windll, dll_name, None)  # type: ignore[attr-defined]
            func = getattr(dll, fn_name, None) if dll is not None else None
            if func is None:
                continue
            func.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), ctypes.c_uint32]
            func.restype = ctypes.c_int
            if func(handle, ctypes.byref(counters), counters.cb) and counters.WorkingSetSize > 0:
                return round(counters.WorkingSetSize / (1024.0 * 1024.0), 2)
    except Exception:
        return None
    return None


def _rss_mib() -> float | None:
    """Return current process resident set size in MiB, or None if unavailable."""
    if sys.platform == "win32":
        return _windows_rss_mib()
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    val = int(parts[1])
                    if val > 0:
                        return round(val / 1024.0, 2)
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if usage > 0:
            divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
            return round(usage / divisor, 2)
    except Exception:
        return None
    return None


@contextlib.contextmanager
def isolated_profile_env() -> Iterator[Path]:
    """Isolate APPDATA/XDG profile directories and keyring in a temporary directory."""
    saved_env = {key: os.environ.get(key) for key in _PROFILE_ENV_VARS}
    saved_keyring = None
    keyring_mod = None
    try:
        import keyring
        from keyring.backend import KeyringBackend

        class _MemoryKeyring(KeyringBackend):
            priority = 1

            def __init__(self) -> None:
                self._values: dict[tuple[str, str], str] = {}

            def get_password(self, service: str, username: str) -> str | None:
                return self._values.get((service, username))

            def set_password(self, service: str, username: str, password: str) -> None:
                self._values[(service, username)] = password

            def delete_password(self, service: str, username: str) -> None:
                self._values.pop((service, username), None)

        keyring_mod = keyring
        saved_keyring = keyring.get_keyring()
        keyring.set_keyring(_MemoryKeyring())
    except Exception:
        keyring_mod = None

    with tempfile.TemporaryDirectory(prefix="wuwaterm-bench-") as tmp_dir:
        for key in _PROFILE_ENV_VARS:
            os.environ[key] = tmp_dir
        try:
            yield Path(tmp_dir)
        finally:
            for key, old_val in saved_env.items():
                if old_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_val
            if keyring_mod is not None and saved_keyring is not None:
                try:
                    keyring_mod.set_keyring(saved_keyring)
                except Exception:
                    pass


def _summarize(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_idx = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "min": round(ordered[0], 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[p95_idx], 3),
        "max": round(ordered[-1], 3),
    }


def _summarize_optional(values: list[float | None]) -> dict[str, float] | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return _summarize(present)


def _build_large_manuscript(segments: int) -> tuple[str, str]:
    """Build ``segments`` bilingual paragraphs sized (~485 src / ~1070 tgt chars) so
    ``pair_segments`` keeps a 1:1 pairing without merging or hard-cutting."""
    src_pad = "声骸共振记录校验。" * 50
    tgt_pad = " Echo resonance log check." * 38
    src_parts = [
        f"第{i + 1:04d}段：共鸣者漂泊者在今州城核对声骸共鸣频率与数值 {i + 10}。{src_pad}"
        for i in range(segments)
    ]
    tgt_parts = [
        f"Segment {i + 1:04d}: Rover verifies the Echo resonance frequency value {i + 10} in Jinzhou.{tgt_pad}"
        for i in range(segments)
    ]
    return "\n\n".join(src_parts), "\n\n".join(tgt_parts)


def _build_document_term_report(source: str, target: str, count: int) -> dict[str, Any]:
    """Build a synthetic document-level term report with ``count`` findings across the manuscript."""
    verdicts = ("verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated")
    src_blocks = source.split("\n\n")
    tgt_blocks = target.split("\n\n")
    findings: list[dict[str, Any]] = []
    src_offset = 0
    tgt_offset = 0
    for i in range(min(count, len(src_blocks), len(tgt_blocks))):
        src_block = src_blocks[i]
        tgt_block = tgt_blocks[i]
        term_src = "共鸣者" if i % 2 == 0 else "声骸"
        term_tgt = "Rover" if i % 2 == 0 else "Echo"
        s_pos = src_offset + src_block.index(term_src)
        t_pos = tgt_offset + tgt_block.index(term_tgt)
        verdict = verdicts[i % len(verdicts)]
        finding: dict[str, Any] = {
            "source_span": {"start": s_pos, "end": s_pos + len(term_src), "text": term_src},
            "target_span": {"start": t_pos, "end": t_pos + len(term_tgt), "text": term_tgt},
            "verdict": verdict,
            "candidates": [
                {"source": term_src, "target": term_tgt, "category": "character" if i % 2 == 0 else "system"}
            ],
            "candidates_truncated": False,
        }
        if verdict == "not_evaluated":
            finding["reason"] = "untranslated"
        findings.append(finding)
        src_offset += len(src_block) + 2
        tgt_offset += len(tgt_block) + 2

    evaluated = sum(1 for f in findings if f["verdict"] != "not_evaluated")
    return {
        "request_id": "bench-report-1",
        "direction": "en",
        "coverage": {
            "evaluated": evaluated,
            "not_evaluated": len(findings) - evaluated,
        },
        "truncated": False,
        "findings": findings,
    }


def _build_large_recovery_payload(segments: int = 500) -> dict[str, Any]:
    """Build a valid v5 workfile payload with ``segments`` (<= 512) populated records."""
    from wuwaterm_client import manuscript, segment_records as sr
    from wuwaterm_client.segmentation import pair_segments

    source, target = _build_large_manuscript(segments)
    basis = {
        "rule_version": "review-v2",
        "dictionary": {
            "schema_version": "1",
            "source_commit": "a" * 40,
            "term_count": 1200,
            "revision": "b" * 64,
        },
        "matcher_revision": "c" * 64,
    }
    payload = manuscript.empty_workfile()
    payload.update(source=source, target=target, direction="en")
    pairs = pair_segments(source, target)
    parts = sr.normalize(payload, pairs, basis)
    payload = sr.with_parts(payload, parts)

    verdicts = ("verified_constraint", "confirmed_conflict", "needs_review")
    for idx in range(len(pairs)):
        req = sr.plan_request(payload, idx, basis)
        term_src = "共鸣者" if idx % 2 == 0 else "声骸"
        term_tgt = "Rover" if idx % 2 == 0 else "Echo"
        s_pos = req.source.index(term_src)
        t_pos = req.target.index(term_tgt)
        verdict = verdicts[idx % len(verdicts)]
        report = {
            "request_id": f"bench-seg-{idx}",
            "rule_version": basis["rule_version"],
            "dictionary": basis["dictionary"],
            "matcher_revision": basis["matcher_revision"],
            "coverage": {
                "evaluated": 1,
                "not_evaluated": 0,
                "rules": list(sr.COVERAGE_RULES),
            },
            "truncated": False,
            "findings": [
                {
                    "rule_id": sr.TERM_PAIR_RULE,
                    "source_span": {"start": s_pos, "end": s_pos + len(term_src), "text": term_src},
                    "target_span": {"start": t_pos, "end": t_pos + len(term_tgt), "text": term_tgt},
                    "verdict": verdict,
                    "candidates": [
                        {
                            "candidate_id": f"{(idx % 2) + 1:064x}",
                            "zh": term_src,
                            "en": term_tgt,
                            "category": "character" if idx % 2 == 0 else "system",
                        }
                    ],
                    "candidates_truncated": False,
                }
            ],
        }
        applied = sr.apply_response(payload, req, report, basis)
        if applied.outcome != "stored":
            raise RuntimeError(f"failed to store synthetic segment {idx}: {applied.outcome}")
        payload = sr.with_parts(payload, applied.parts)
    payload["progress"] = {"current": segments // 2, "completed": list(range(0, segments, 2))}
    return payload


def _make_list_adapter(app_parent: Any = None):
    """Return a uniform adapter over ``ModelListView`` (after) or ``_WrappedListWidget`` (before)."""
    from PySide6.QtCore import Qt

    try:
        from wuwaterm_client.ui.review_models import ModelListView, ReviewRow  # type: ignore[import-not-found]

        class _ModelListAdapter:
            backend_name = "ModelListView (QAbstractListModel + CachedRowDelegate)"

            def __init__(self) -> None:
                self.widget = ModelListView(app_parent)

            def set_rows(self, specs: list[tuple[str, str, str, int]]) -> None:
                rows = [
                    ReviewRow(
                        text=text,
                        state=state,
                        tooltip=tooltip,
                        user_data=idx,
                        filter_keys=frozenset({"all", state}),
                    )
                    for text, state, tooltip, idx in specs
                ]
                self.widget.set_rows(rows)

            def apply_filter(self, key: str | None) -> None:
                self.widget.set_filter_key(key)

            def count(self) -> int:
                return self.widget.count()

            def jump_to(self, row: int) -> None:
                self.widget.setCurrentRow(row)
                self.widget.scrollToItem(self.widget.item(row))

        return _ModelListAdapter()
    except ImportError:
        from PySide6.QtWidgets import QListView, QListWidgetItem
        from wuwaterm_client.ui import review_states
        from wuwaterm_client.ui.manuscript_view import _WrappedListWidget  # type: ignore[attr-defined]

        class _LegacyListAdapter:
            backend_name = "_WrappedListWidget (QListWidget + _WordWrapDelegate)"

            def __init__(self) -> None:
                self.widget = _WrappedListWidget(app_parent)
                self.widget.setWordWrap(True)
                self.widget.setTextElideMode(Qt.TextElideMode.ElideNone)
                self.widget.setUniformItemSizes(False)
                self.widget.setResizeMode(QListView.ResizeMode.Adjust)
                self.widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self._states: list[str] = []

            def set_rows(self, specs: list[tuple[str, str, str, int]]) -> None:
                self.widget.clear()
                self._states = []
                for text, state, tooltip, idx in specs:
                    item = QListWidgetItem(text)
                    item.setToolTip(tooltip)
                    item.setData(Qt.ItemDataRole.UserRole, idx)
                    review_states.mark(item, review_states.for_verdict(state))
                    self.widget.addItem(item)
                    self._states.append(state)

            def apply_filter(self, key: str | None) -> None:
                for i, state in enumerate(self._states):
                    hide = key not in (None, "all") and state != key
                    self.widget.setRowHidden(i, hide)

            def count(self) -> int:
                return self.widget.count()

            def jump_to(self, row: int) -> None:
                self.widget.setCurrentRow(row)
                self.widget.scrollToItem(self.widget.item(row))

        return _LegacyListAdapter()


def _cold_start_child_script() -> str:
    """Self-contained snippet executed inside a fresh subprocess for Cold Start + Idle RSS."""
    return r"""
import ctypes, gc, json, os, sys, time, tracemalloc
from pathlib import Path

t_start = time.perf_counter()
tracemalloc.start()

import qasync
from PySide6.QtWidgets import QApplication
from wuwaterm_client import i18n, strings, theme
from wuwaterm_client.config import ClientConfig
from wuwaterm_client.ui.main_window import MainWindow

def _rss_mib():
    if sys.platform == "win32":
        try:
            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            k32 = ctypes.windll.kernel32
            h = k32.GetCurrentProcess()
            c = _PMC()
            c.cb = ctypes.sizeof(c)
            for dll_name, fn_name in (("psapi", "GetProcessMemoryInfo"), ("kernel32", "K32GetProcessMemoryInfo")):
                dll = getattr(ctypes.windll, dll_name, None)
                fn = getattr(dll, fn_name, None) if dll is not None else None
                if fn is None:
                    continue
                fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_uint32]
                fn.restype = ctypes.c_int
                if fn(h, ctypes.byref(c), c.cb) and c.WorkingSetSize > 0:
                    return round(c.WorkingSetSize / (1024.0 * 1024.0), 2)
        except Exception:
            return None
        return None
    status = Path("/proc/self/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and int(parts[1]) > 0:
                    return round(int(parts[1]) / 1024.0, 2)
    return None

app = QApplication([])
app.setApplicationName(strings.APP_TITLE)
app.setStyle("Fusion")
i18n.install_qt_translations(app)
theme.apply_theme(app, theme.SCHEME_LIGHT)
loop = qasync.QEventLoop(app)

t_win = time.perf_counter()
window = MainWindow(config=ClientConfig())
window.resize(1440, 900)
window.show()
app.processEvents()
t_ready = time.perf_counter()

gc.collect()
app.processEvents()
rss_mib = _rss_mib()
cur_bytes, peak_bytes = tracemalloc.get_traced_memory()
tracemalloc.stop()

window.manuscript_view.confirm_discard = lambda: True
window.close()
window.deleteLater()
app.processEvents()
loop.close()

print(json.dumps({
    "cold_start_ms": (t_ready - t_start) * 1000.0,
    "window_construct_ms": (t_ready - t_win) * 1000.0,
    "idle_rss_mib": rss_mib,
    "idle_tracemalloc_current_mib": round(cur_bytes / (1024.0 * 1024.0), 2),
    "idle_tracemalloc_peak_mib": round(peak_bytes / (1024.0 * 1024.0), 2),
}))
"""


def measure_cold_start_and_idle_rss(repo_root: Path, samples: int) -> dict[str, Any]:
    """Spawn ``samples`` clean subprocesses against ``repo_root/src`` to measure cold start and idle RSS."""
    cold_ms: list[float] = []
    win_ms: list[float] = []
    wall_ms: list[float] = []
    rss_vals: list[float | None] = []
    tm_cur_vals: list[float] = []
    tm_peak_vals: list[float] = []

    snippet = _cold_start_child_script()
    src_dir = str((repo_root / "src").resolve())

    for _ in range(samples):
        with tempfile.TemporaryDirectory(prefix="wuwaterm-cold-") as tmp_dir:
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else src_dir
            for key in _PROFILE_ENV_VARS:
                env[key] = tmp_dir
            t0 = time.perf_counter()
            proc = subprocess.run(
                [sys.executable, "-c", snippet],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            elapsed_wall = (time.perf_counter() - t0) * 1000.0
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            payload = json.loads(lines[-1])
            cold_ms.append(float(payload["cold_start_ms"]))
            win_ms.append(float(payload["window_construct_ms"]))
            wall_ms.append(elapsed_wall)
            rss_vals.append(payload.get("idle_rss_mib"))
            tm_cur_vals.append(float(payload["idle_tracemalloc_current_mib"]))
            tm_peak_vals.append(float(payload["idle_tracemalloc_peak_mib"]))

    return {
        "samples": samples,
        "cold_start_ms": _summarize(cold_ms),
        "window_construct_ms": _summarize(win_ms),
        "subprocess_wall_ms": _summarize(wall_ms),
        "idle_rss_mib": _summarize_optional(rss_vals),
        "idle_tracemalloc_current_mib": _summarize(tm_cur_vals),
        "idle_tracemalloc_peak_mib": _summarize(tm_peak_vals),
    }


def measure_typing_latency(app: Any, keystrokes: int) -> dict[str, Any]:
    """Measure 500-segment keystroke dispatch + debounced state-sync latency inside MainWindow."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from wuwaterm_client.config import ClientConfig
    from wuwaterm_client.manuscript import empty_workfile
    from wuwaterm_client.ui.main_window import MainWindow
    from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS

    source, target = _build_large_manuscript(500)
    window = MainWindow(config=ClientConfig())
    window.resize(1440, 900)
    window.show()
    view = window.manuscript_view
    payload = empty_workfile()
    payload.update(source=source, target=target, direction="en")
    view._set_payload(payload)
    view._show_track(TRACK_SEGMENTS)
    view._refresh_derived()
    view.target_edit.setFocus()
    view.target_edit.set_raw_cursor_offset(len(target) // 2)
    app.processEvents()

    dispatch_ms: list[float] = []
    sync_ms: list[float] = []
    combined_ms: list[float] = []
    try:
        for _ in range(keystrokes):
            t0 = time.perf_counter()
            QTest.keyClick(view.target_edit, Qt.Key.Key_A)
            app.processEvents()
            t1 = time.perf_counter()
            view._derived_timer.stop()
            view._caret_timer.stop()
            view._refresh_derived()
            view._follow_caret()
            app.processEvents()
            t2 = time.perf_counter()

            dispatch_ms.append((t1 - t0) * 1000.0)
            sync_ms.append((t2 - t1) * 1000.0)
            combined_ms.append((t2 - t0) * 1000.0)
    finally:
        view._recovery_timer.stop()
        view._derived_timer.stop()
        view._caret_timer.stop()
        view.confirm_discard = lambda: True
        window.close()
        window.deleteLater()
        app.processEvents()

    return {
        "segments": 500,
        "source_chars": len(source),
        "target_chars": len(target),
        "keystrokes": keystrokes,
        "keystroke_dispatch_ms": _summarize(dispatch_ms),
        "state_sync_ms": _summarize(sync_ms),
        "combined_ms": _summarize(combined_ms),
    }


def measure_large_manuscript_refresh(app: Any, samples: int, sizes: tuple[int, ...] = _BENCH_SIZES) -> dict[str, Any]:
    """Measure 200/500/1000-segment manuscript load & filter refresh on ManuscriptView and list widget."""
    from wuwaterm_client.api import ApiClient
    from wuwaterm_client.manuscript import empty_workfile
    from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, TRACK_TERMS, ManuscriptView

    by_size: dict[str, Any] = {}
    adapter_name = ""

    for seg_count in sizes:
        source, target = _build_large_manuscript(seg_count)
        report = _build_document_term_report(source, target, seg_count)
        row_specs = [
            (
                f"共鸣者术语 #{i + 1}\nResonator Term #{i + 1} · 第 {(i % seg_count) + 1} 段",
                ("verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated")[i % 4],
                f"Tooltip detail for item {i + 1}",
                i,
            )
            for i in range(seg_count)
        ]

        seg_load_ms: list[float] = []
        term_load_ms: list[float] = []
        view_filter_ms: list[float] = []
        widget_load_ms: list[float] = []
        widget_filter_ms: list[float] = []

        for _ in range(samples):
            view = ManuscriptView(ApiClient(None))
            view.resize(1280, 760)
            view.show()
            app.processEvents()
            try:
                payload = empty_workfile()
                payload.update(source=source, target=target, direction="en")

                t0 = time.perf_counter()
                view._set_payload(payload)
                view._show_track(TRACK_SEGMENTS)
                view._refresh_derived()
                app.processEvents()
                seg_load_ms.append((time.perf_counter() - t0) * 1000.0)

                t1 = time.perf_counter()
                view._show_track(TRACK_TERMS)
                view._show_term_report(report, state="current")
                app.processEvents()
                term_load_ms.append((time.perf_counter() - t1) * 1000.0)

                t2 = time.perf_counter()
                for target_key in ("all", "confirmed", "pending", "unhandled"):
                    idx = view.term_filter_combo.findData(target_key)
                    if idx >= 0:
                        view.term_filter_combo.setCurrentIndex(idx)
                        app.processEvents()
                view_filter_ms.append((time.perf_counter() - t2) * 1000.0)
            finally:
                view._derived_timer.stop()
                view._caret_timer.stop()
                view._recovery_timer.stop()
                view.close()
                view.deleteLater()
                app.processEvents()

            adapter = _make_list_adapter()
            adapter_name = adapter.backend_name
            adapter.widget.resize(320, 480)
            adapter.widget.show()
            app.processEvents()
            try:
                tw0 = time.perf_counter()
                adapter.set_rows(row_specs)
                app.processEvents()
                widget_load_ms.append((time.perf_counter() - tw0) * 1000.0)

                tw1 = time.perf_counter()
                for fkey in ("needs_review", "confirmed_conflict", "all"):
                    adapter.apply_filter(fkey)
                    app.processEvents()
                widget_filter_ms.append((time.perf_counter() - tw1) * 1000.0)
            finally:
                adapter.widget.close()
                adapter.widget.deleteLater()
                app.processEvents()

        by_size[str(seg_count)] = {
            "segments": seg_count,
            "source_chars": len(source),
            "target_chars": len(target),
            "segment_list_load_ms": _summarize(seg_load_ms),
            "term_report_load_ms": _summarize(term_load_ms),
            "view_filter_cycle_ms": _summarize(view_filter_ms),
            "widget_bulk_load_ms": _summarize(widget_load_ms),
            "widget_filter_cycle_ms": _summarize(widget_filter_ms),
        }

    return {
        "samples": samples,
        "list_backend": adapter_name,
        "sizes": by_size,
    }


def measure_scroll_perf(app: Any, item_count: int, samples: int, scroll_steps: int) -> dict[str, Any]:
    """Measure viewport scroll traversal, delegate paint/render, 5-jump scroll, and resize relayout."""
    from PySide6.QtGui import QPixmap

    adapter = _make_list_adapter()
    widget = adapter.widget
    widget.resize(320, 480)
    widget.show()
    app.processEvents()

    states = ("verified_constraint", "confirmed_conflict", "needs_review", "not_evaluated")
    row_specs = [
        (
            f"共鸣者术语 #{i + 1}：声骸共鸣校准词条\nResonator Term #{i + 1} · Segment {(i % 500) + 1} verification context",
            states[i % len(states)],
            f"Tooltip detail for item {i + 1}",
            i,
        )
        for i in range(item_count)
    ]

    viewport_scroll_ms: list[float] = []
    jump5_ms: list[float] = []
    resize_relayout_ms: list[float] = []
    try:
        adapter.set_rows(row_specs)
        app.processEvents()
        sb = widget.verticalScrollBar()
        vp_size = widget.viewport().size()
        canvas = QPixmap(max(1, vp_size.width()), max(1, vp_size.height()))

        for _ in range(samples):
            sb.setValue(sb.minimum())
            app.processEvents()

            t0 = time.perf_counter()
            sb_min = sb.minimum()
            sb_max = max(sb_min, sb.maximum())
            steps = max(2, scroll_steps)
            for s in range(steps):
                pos = sb_min + ((sb_max - sb_min) * s) // (steps - 1)
                sb.setValue(pos)
                app.processEvents()
                widget.viewport().render(canvas)
            viewport_scroll_ms.append((time.perf_counter() - t0) * 1000.0)

            t1 = time.perf_counter()
            for target_row in (0, item_count // 4, item_count // 2, (item_count * 3) // 4, item_count - 1):
                adapter.jump_to(target_row)
            app.processEvents()
            widget.viewport().render(canvas)
            jump5_ms.append((time.perf_counter() - t1) * 1000.0)

            t2 = time.perf_counter()
            widget.resize(380, 480)
            app.processEvents()
            widget.viewport().render(QPixmap(max(1, widget.viewport().width()), max(1, widget.viewport().height())))
            widget.resize(320, 480)
            app.processEvents()
            widget.viewport().render(canvas)
            resize_relayout_ms.append((time.perf_counter() - t2) * 1000.0)
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()

    return {
        "item_count": item_count,
        "scroll_steps": scroll_steps,
        "list_backend": adapter.backend_name,
        "viewport_scroll_render_ms": _summarize(viewport_scroll_ms),
        "scroll_5_jumps_ms": _summarize(jump5_ms),
        "resize_relayout_render_ms": _summarize(resize_relayout_ms),
    }


def measure_draft_recovery(app: Any, tmp_dir: Path, samples: int, segments: int = 500) -> dict[str, Any]:
    """Measure deserializing and restoring a large 500-segment v5 recovery draft into ManuscriptView."""
    from wuwaterm_client import recovery
    from wuwaterm_client.api import ApiClient
    from wuwaterm_client.ui.manuscript_view import ManuscriptView

    draft_path = tmp_dir / "recovery" / "draft.json"
    payload = _build_large_recovery_payload(segments)
    recovery.save_recovery(draft_path, payload, source_path=str(tmp_dir / "source-manuscript.json"))
    draft_bytes = draft_path.stat().st_size

    deserialize_ms: list[float] = []
    restore_workbench_ms: list[float] = []
    total_ms: list[float] = []

    for _ in range(samples):
        view = ManuscriptView(ApiClient(None), recovery_path=draft_path)
        view.resize(1280, 760)
        view.show()
        app.processEvents()
        try:
            t0 = time.perf_counter()
            state = recovery.load_recovery(draft_path)
            t1 = time.perf_counter()
            if state.status != "valid" or state.payload is None:
                raise RuntimeError(f"unexpected recovery state: {state.status} ({state.error})")
            view._restore_recovery_draft(state)
            app.processEvents()
            t2 = time.perf_counter()

            deserialize_ms.append((t1 - t0) * 1000.0)
            restore_workbench_ms.append((t2 - t1) * 1000.0)
            total_ms.append((t2 - t0) * 1000.0)
        finally:
            view._recovery_timer.stop()
            view._derived_timer.stop()
            view._caret_timer.stop()
            view.close()
            view.deleteLater()
            app.processEvents()

    return {
        "segments": segments,
        "draft_kib": round(draft_bytes / 1024.0, 2),
        "samples": samples,
        "deserialize_ms": _summarize(deserialize_ms),
        "restore_workbench_ms": _summarize(restore_workbench_ms),
        "total_ms": _summarize(total_ms),
    }


def measure_inprocess_suite(
    *,
    repo_root: Path,
    samples: int,
    keystrokes: int,
    scroll_items: int,
    scroll_steps: int,
    sizes: tuple[int, ...],
) -> dict[str, Any]:
    """Run metrics 3-6 inside the current Python process against ``repo_root/src``."""
    src_dir = str((repo_root / "src").resolve())
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    with isolated_profile_env() as tmp_dir:
        from PySide6.QtWidgets import QApplication
        from wuwaterm_client import theme

        app = QApplication.instance() or QApplication([])
        theme.apply_theme(app, theme.SCHEME_LIGHT)

        return {
            "typing_latency": measure_typing_latency(app, keystrokes),
            "large_manuscript_list_refresh": measure_large_manuscript_refresh(app, samples, sizes=sizes),
            "scroll_perf": measure_scroll_perf(app, scroll_items, samples, scroll_steps),
            "draft_recovery": measure_draft_recovery(app, tmp_dir, samples),
        }


def run_revision_suite(
    repo_root: Path,
    *,
    label: str,
    quick: bool = False,
    samples: int = 3,
    cold_samples: int = 5,
    keystrokes: int = 30,
    scroll_items: int = 1000,
    scroll_steps: int = 25,
) -> dict[str, Any]:
    """Measure all 6 metrics for ``repo_root`` using isolated subprocesses."""
    repo_root = repo_root.resolve()
    run_samples = 1 if quick else max(1, samples)
    run_cold = 1 if quick else max(1, cold_samples)
    run_keys = 6 if quick else max(1, keystrokes)
    run_scroll_steps = 5 if quick else max(2, scroll_steps)
    sizes = (200, 500, 1000)

    cold_and_idle = measure_cold_start_and_idle_rss(repo_root, run_cold)

    # Run metrics 3-6 in a clean subprocess so `sys.modules` never leaks across revisions.
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_worker-root",
        str(repo_root),
        "--samples",
        str(run_samples),
        "--keystrokes",
        str(run_keys),
        "--items",
        str(scroll_items),
        "--scroll-steps",
        str(run_scroll_steps),
        "--sizes",
        ",".join(str(s) for s in sizes),
    ]
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(
            f"worker benchmark failed ({proc.returncode}) for {repo_root}:\n{proc.stderr}\n{proc.stdout}"
        )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    inproc = json.loads(lines[-1])

    return {
        "label": label,
        "repo_root": str(repo_root),
        "platform": sys.platform,
        "qpa_platform": os.environ.get("QT_QPA_PLATFORM", "offscreen"),
        "mode": "quick" if quick else "standard",
        "cold_start": {
            "samples": cold_and_idle["samples"],
            "cold_start_ms": cold_and_idle["cold_start_ms"],
            "window_construct_ms": cold_and_idle["window_construct_ms"],
            "subprocess_wall_ms": cold_and_idle["subprocess_wall_ms"],
        },
        "idle_rss": {
            "samples": cold_and_idle["samples"],
            "rss_mib": cold_and_idle["idle_rss_mib"],
            "tracemalloc_current_mib": cold_and_idle["idle_tracemalloc_current_mib"],
            "tracemalloc_peak_mib": cold_and_idle["idle_tracemalloc_peak_mib"],
        },
        **inproc,
    }


def _export_git_ref(repo_root: Path, ref: str, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "-C", str(repo_root), "archive", ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        stdin=archive.stdout,
        capture_output=True,
        check=True,
    )
    archive.stdout.close()
    rc = archive.wait()
    if rc != 0:
        err = archive.stderr.read().decode("utf-8", errors="replace") if archive.stderr else ""
        raise RuntimeError(f"git archive {ref} failed ({rc}): {err} {extract.stderr.decode('utf-8', errors='replace')}")


def _resolve_git_sha(repo_root: Path, ref: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", ref],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _delta_pct(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return round(((after - before) / before) * 100.0, 2)


def build_comparison(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compute side-by-side summary rows for all 6 benchmark metrics."""
    b_rss = (before["idle_rss"]["rss_mib"] or {}).get("p50")
    a_rss = (after["idle_rss"]["rss_mib"] or {}).get("p50")

    rows = [
        {
            "metric": "cold_start_ms (import + MainWindow)",
            "before": before["cold_start"]["cold_start_ms"]["p50"],
            "after": after["cold_start"]["cold_start_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "window_construct_ms (MainWindow only)",
            "before": before["cold_start"]["window_construct_ms"]["p50"],
            "after": after["cold_start"]["window_construct_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "idle_rss_mib",
            "before": b_rss,
            "after": a_rss,
            "unit": "MiB",
        },
        {
            "metric": "typing_keystroke_p50_ms (500 seg)",
            "before": before["typing_latency"]["keystroke_dispatch_ms"]["p50"],
            "after": after["typing_latency"]["keystroke_dispatch_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "typing_keystroke_p95_ms (500 seg)",
            "before": before["typing_latency"]["keystroke_dispatch_ms"]["p95"],
            "after": after["typing_latency"]["keystroke_dispatch_ms"]["p95"],
            "unit": "ms",
        },
        {
            "metric": "typing_state_sync_p50_ms (500 seg)",
            "before": before["typing_latency"]["state_sync_ms"]["p50"],
            "after": after["typing_latency"]["state_sync_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "typing_state_sync_p95_ms (500 seg)",
            "before": before["typing_latency"]["state_sync_ms"]["p95"],
            "after": after["typing_latency"]["state_sync_ms"]["p95"],
            "unit": "ms",
        },
    ]

    for size_key in ("200", "500", "1000"):
        b_sz = before["large_manuscript_list_refresh"]["sizes"][size_key]
        a_sz = after["large_manuscript_list_refresh"]["sizes"][size_key]
        rows.extend([
            {
                "metric": f"manuscript_{size_key}_segment_load_ms",
                "before": b_sz["segment_list_load_ms"]["p50"],
                "after": a_sz["segment_list_load_ms"]["p50"],
                "unit": "ms",
            },
            {
                "metric": f"manuscript_{size_key}_term_load_ms",
                "before": b_sz["term_report_load_ms"]["p50"],
                "after": a_sz["term_report_load_ms"]["p50"],
                "unit": "ms",
            },
            {
                "metric": f"manuscript_{size_key}_filter_cycle_ms",
                "before": b_sz["view_filter_cycle_ms"]["p50"],
                "after": a_sz["view_filter_cycle_ms"]["p50"],
                "unit": "ms",
            },
            {
                "metric": f"widget_{size_key}_bulk_load_ms",
                "before": b_sz["widget_bulk_load_ms"]["p50"],
                "after": a_sz["widget_bulk_load_ms"]["p50"],
                "unit": "ms",
            },
            {
                "metric": f"widget_{size_key}_filter_cycle_ms",
                "before": b_sz["widget_filter_cycle_ms"]["p50"],
                "after": a_sz["widget_filter_cycle_ms"]["p50"],
                "unit": "ms",
            },
        ])

    rows.extend([
        {
            "metric": "scroll_viewport_render_1000_ms",
            "before": before["scroll_perf"]["viewport_scroll_render_ms"]["p50"],
            "after": after["scroll_perf"]["viewport_scroll_render_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "scroll_5_jumps_1000_ms",
            "before": before["scroll_perf"]["scroll_5_jumps_ms"]["p50"],
            "after": after["scroll_perf"]["scroll_5_jumps_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "scroll_resize_relayout_1000_ms",
            "before": before["scroll_perf"]["resize_relayout_render_ms"]["p50"],
            "after": after["scroll_perf"]["resize_relayout_render_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "draft_recovery_deserialize_500_ms",
            "before": before["draft_recovery"]["deserialize_ms"]["p50"],
            "after": after["draft_recovery"]["deserialize_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "draft_recovery_restore_500_ms",
            "before": before["draft_recovery"]["restore_workbench_ms"]["p50"],
            "after": after["draft_recovery"]["restore_workbench_ms"]["p50"],
            "unit": "ms",
        },
        {
            "metric": "draft_recovery_total_500_ms",
            "before": before["draft_recovery"]["total_ms"]["p50"],
            "after": after["draft_recovery"]["total_ms"]["p50"],
            "unit": "ms",
        },
    ])

    for row in rows:
        row["delta_pct"] = _delta_pct(row["before"], row["after"])

    return {
        "before_label": before["label"],
        "after_label": after["label"],
        "before": before,
        "after": after,
        "summary": rows,
    }


def format_comparison_text(comparison: dict[str, Any]) -> str:
    lines = [
        f"=== Before/After Workbench Benchmark ({comparison['before_label']} -> {comparison['after_label']}) ===",
        f"{'Metric':<42} | {'Before':>10} | {'After':>10} | {'Delta':>9}",
        "-" * 80,
    ]
    for row in comparison["summary"]:
        b_val = f"{row['before']:.2f} {row['unit']}" if row["before"] is not None else "N/A"
        a_val = f"{row['after']:.2f} {row['unit']}" if row["after"] is not None else "N/A"
        d_val = f"{row['delta_pct']:+.1f}%" if row["delta_pct"] is not None else "N/A"
        lines.append(f"{row['metric']:<42} | {b_val:>10} | {a_val:>10} | {d_val:>9}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare WuwaTerm Windows workbench performance across revisions")
    parser.add_argument("--_worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", type=Path, help="Measure a single checkout directory instead of comparing two")
    parser.add_argument("--before-root", type=Path, help="Existing checkout directory for the 'before' revision")
    parser.add_argument("--after-root", type=Path, help="Existing checkout directory for the 'after' revision")
    parser.add_argument("--before-ref", default="e6835f3", help="Git ref to export for 'before' when --before-root omitted")
    parser.add_argument("--after-ref", default="HEAD", help="Git ref label for 'after' (default: HEAD)")
    parser.add_argument("--quick", action="store_true", help="Fast single-sample smoke run")
    parser.add_argument("--samples", type=int, default=3, help="Sample count for list/scroll/recovery benchmarks")
    parser.add_argument("--cold-samples", type=int, default=5, help="Sample count for cold start / idle RSS subprocesses")
    parser.add_argument("--keystrokes", type=int, default=30, help="Keystroke count for typing latency")
    parser.add_argument("--items", type=int, default=1000, help="Row count for scroll benchmark (default: 1000)")
    parser.add_argument("--scroll-steps", type=int, default=25, help="Viewport scroll steps (default: 25)")
    parser.add_argument("--sizes", default="200,500,1000", help="Comma-separated segment counts for list refresh")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args(argv)

    if args._worker_root is not None:
        sizes = tuple(int(part) for part in args.sizes.split(",") if part.strip())
        payload = measure_inprocess_suite(
            repo_root=args._worker_root,
            samples=args.samples,
            keystrokes=args.keystrokes,
            scroll_items=args.items,
            scroll_steps=args.scroll_steps,
            sizes=sizes,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if args.repo_root is not None:
        single = run_revision_suite(
            args.repo_root,
            label=str(args.repo_root),
            quick=args.quick,
            samples=args.samples,
            cold_samples=args.cold_samples,
            keystrokes=args.keystrokes,
            scroll_items=args.items,
            scroll_steps=args.scroll_steps,
        )
        print(json.dumps(single, ensure_ascii=False, indent=2))
        return 0

    work_repo = Path(__file__).resolve().parents[1]
    after_root = args.after_root or work_repo
    after_label = _resolve_git_sha(work_repo, args.after_ref) if args.after_root is None else str(after_root)

    if args.before_root is not None:
        before_res = run_revision_suite(
            args.before_root,
            label=args.before_ref,
            quick=args.quick,
            samples=args.samples,
            cold_samples=args.cold_samples,
            keystrokes=args.keystrokes,
            scroll_items=args.items,
            scroll_steps=args.scroll_steps,
        )
    else:
        before_label = _resolve_git_sha(work_repo, args.before_ref)
        with tempfile.TemporaryDirectory(prefix="win-bench-before-") as tmp_before:
            before_dir = Path(tmp_before)
            _export_git_ref(work_repo, args.before_ref, before_dir)
            before_res = run_revision_suite(
                before_dir,
                label=before_label,
                quick=args.quick,
                samples=args.samples,
                cold_samples=args.cold_samples,
                keystrokes=args.keystrokes,
                scroll_items=args.items,
                scroll_steps=args.scroll_steps,
            )

    after_res = run_revision_suite(
        after_root,
        label=after_label,
        quick=args.quick,
        samples=args.samples,
        cold_samples=args.cold_samples,
        keystrokes=args.keystrokes,
        scroll_items=args.items,
        scroll_steps=args.scroll_steps,
    )
    comparison = build_comparison(before_res, after_res)
    if args.json:
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
    else:
        print(format_comparison_text(comparison))
        print(json.dumps(comparison, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
