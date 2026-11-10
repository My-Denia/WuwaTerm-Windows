"""Capture the workbench in fixed states for visual review.

    python scripts/capture_workbench.py OUT_DIR [--scale 1.5] [--size 1440x900]

Every request goes to an in-process mock transport and the configuration lives
in a temporary directory: nothing leaves the machine, no credential store is
touched, and the owner's settings are never read. The texts are synthetic.

Offscreen captures are simulated evidence. They show layout, hierarchy and
colour at a device pixel ratio; they do not show a real Windows monitor, a
real IME candidate window or ClearType.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


def _parse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--scale", default="1")
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--themes", default="light,dark")
    return parser.parse_args()


ARGS = _parse()
_ORIGINAL_CWD = Path.cwd()
# Before any Qt import: the scale factor and platform are read at startup.
os.environ["QT_SCALE_FACTOR"] = ARGS.scale
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="wuwaterm-capture-")
# The default offscreen screen is 800x800, smaller than the workbench's own
# default size. Describe a desktop that still leaves room for the requested
# logical size once the scale factor divides it.
_scale = float(ARGS.scale)
_width, _height = (int(v) for v in ARGS.size.split("x"))
_screen = Path(os.environ["APPDATA"]) / "offscreen.json"
_screen.write_text(json.dumps({"screens": [{
    "name": "capture", "x": 0, "y": 0,
    "width": int((_width + 200) * _scale), "height": int((_height + 200) * _scale),
    "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}), encoding="utf-8")
# Relative to the working directory: a Windows drive letter's colon would end
# the platform argument early.
os.chdir(_screen.parent)
os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=offscreen.json"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from wuwaterm_client import semantic, theme  # noqa: E402
from wuwaterm_client.api import ApiClient  # noqa: E402
from wuwaterm_client.config import ClientConfig  # noqa: E402
from wuwaterm_client.manuscript import empty_workfile  # noqa: E402
from wuwaterm_client.ui import main_window as main_window_module  # noqa: E402
from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, TRACK_SEMANTIC  # noqa: E402


class _MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.values: dict = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


HEX = "c" * 64
SOURCE = (
    "共鸣者在第三天清晨不会出发，而是留在今州城等候消息。\n\n"
    "漂泊者提醒众人：声骸的频率仍不稳定，任何人都不要单独靠近残象潮。\n\n"
    "当夜，鸣钟之龟的回响穿过整座城市。"
)
TARGET = (
    "The Resonators will leave Jinzhou on the morning of the second day.\n\n"
    "The Rover reminded everyone that the Echoes' frequency was still unstable, and that no one "
    "should approach the Tacet Discord tide alone.\n\n"
    "That night, the echo of the Bell-Borne Geochelone swept across the whole city."
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _span(text: str, quote: str) -> dict:
    start = text.index(quote)
    return {"start": start, "end": start + len(quote), "text": quote}


def _issue(source, target, category, confidence, sq, tq, replace=None, replacement=None,
           alternative=None, withheld=None, seed="0") -> dict:
    suggestion = None
    if replace is not None and withheld is None:
        suggestion = {"target_span": _span(target, replace), "replacement": replacement, "term_effect": "none"}
    return {
        "id": hashlib.sha256(f"id{seed}".encode()).hexdigest()[:32],
        "fingerprint": hashlib.sha256(f"fp{seed}".encode()).hexdigest()[:32],
        "category": category, "origin": "model", "confidence": confidence,
        "source_span": _span(source, sq) if sq else None,
        "target_span": _span(target, tq) if tq else None,
        "source_excerpt": semantic.excerpt(sq) if sq else None,
        "target_excerpt": semantic.excerpt(tq) if tq else None,
        "rationale": "原文说的是第三天，译文写成了第二天；数量或日期可能被改动。",
        "suggestion": suggestion, "suggestion_withheld": withheld,
        "alternatives": ([{"replacement": alternative, "term_effect": "alters_official_match"}]
                         if alternative and suggestion else []),
    }


def _issues(source: str, target: str) -> list[dict]:
    out = []
    if "第三天" in source and "second day" in target:
        out.append(_issue(source, target, "quantity_change", "high", "第三天", "second day",
                          replace="second day", replacement="third day", alternative="3rd day", seed="1"))
    if "不会出发" in source and "will leave" in target:
        out.append(_issue(source, target, "negation_change", "medium", "不会出发", "will leave",
                          replace="will leave", replacement="will not leave", seed="2"))
    if "单独" in source and "alone" in target:
        out.append(_issue(source, target, "omission", "low", "单独靠近", "approach", seed="3"))
    return out


def _candidate(zh: str, en: str, category: str) -> dict:
    return {"zh": zh, "en": en, "category": category, "candidate_id": _digest(zh + en),
            "sources": [{"source_file": "TextMap/zh/MultiText.json", "source_id": "capture"}]}


def _finding(source: str, target: str, sq: str, tq: str | None, verdict: str, candidate: dict) -> dict:
    source_span = _span(source, sq)
    return {"id": f'{source_span["start"]}:{source_span["end"]}:{sq}', "rule_id": "review.term_pair",
            "verdict": verdict, "source_span": source_span,
            "target_span": _span(target, tq) if tq and tq in target else None,
            "candidates": [candidate], "candidates_truncated": False}


def _term_report(source: str, target: str) -> dict:
    findings = []
    if "共鸣者" in source:
        findings.append(_finding(source, target, "共鸣者", "Resonators", "verified_constraint",
                                 _candidate("共鸣者", "Resonator", "core_term")))
    if "鸣钟之龟" in source:
        findings.append(_finding(source, target, "鸣钟之龟", "Bell-Borne Geochelone", "needs_review",
                                 _candidate("鸣钟之龟", "Bell-Borne Geochelone", "echo")))
    return {
        "request_id": "req-term-0001", "source_revision": _digest(source), "target_revision": _digest(target),
        "rule_version": "review-v2", "matcher_revision": "a" * 64,
        "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 1, "revision": "a" * 64},
        "coverage": {"evaluated": len(findings), "not_evaluated": 0, "rules": ["term"]},
        "findings": findings, "truncated": False,
    }


class _Server:
    """``semantic`` mirrors the server switch: semantic review is an
    experimental option, off by default, while translation context stays."""

    def __init__(self, semantic: bool, quota_after: int | None = None) -> None:
        self.semantic = semantic
        self.quota_after = quota_after
        self.calls = 0

    def _quota(self) -> httpx.Response | None:
        self.calls += 1
        if self.quota_after is not None and self.calls > self.quota_after:
            return httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"},
                                             "request_id": "req-quota"})
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        if path == "/v1/capabilities":
            names = ["translation-context-v1"] + (["semantic-review-v1"] if self.semantic else [])
            return httpx.Response(200, json={"api_version": "v1", "request_id": "caps", "capabilities": names})
        if path == "/v1/reviews":
            if not self.semantic and (refused := self._quota()) is not None:
                return refused
            return httpx.Response(200, json=_term_report(body["source"], body["target"]))
        if path == "/v1/semantic-reviews":
            if (refused := self._quota()) is not None:
                return refused
            source, target = body["source"], body["target"]
            return httpx.Response(200, json={
                "request_id": "req-sem-0001", "review_version": "semantic-review-v1",
                "source_revision": _digest(source), "target_revision": _digest(target),
                "term_basis": {"dictionary_revision": HEX, "matcher_revision": HEX},
                "status": "complete", "status_reason": None, "dropped_unanchored": 0,
                "checked": list(semantic.CATEGORIES), "issues": _issues(source, target),
                "truncated": False, "truncated_reason": None, "omitted_issues": 0,
            })
        raise AssertionError(path)


def _window(server: _Server, configured: bool = True):
    def factory(config):
        return ApiClient(config.base_url, token_provider=lambda: "capture-token",
                         _test_transport=httpx.MockTransport(server))
    main_window_module.ApiClient.from_config = staticmethod(factory)  # type: ignore[method-assign]
    config = ClientConfig(base_url="https://wuwaterm.example.invalid" if configured else None)
    window = main_window_module.MainWindow(config)
    width, height = (int(v) for v in ARGS.size.split("x"))
    window.resize(width, height)
    window.show()
    return window


def _load(window, source: str, target: str) -> None:
    view = window.manuscript_view
    value = empty_workfile()
    value.update(source=source, target=target, direction="en")
    view._set_payload(value)
    view._saved_payload = dict(view._payload)
    view._update_controls()


async def _settle(view) -> None:
    for _ in range(400):
        tasks = [t for t in (view._task, view._semantic_task, view._translate_task) if t is not None and not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            return
        await asyncio.wait(tasks)


def _review(window) -> None:
    view = window.manuscript_view

    async def run() -> None:
        view._start_review()
        await _settle(view)
    asyncio.run(run())


def _states(semantic: bool):
    """(name, builder) pairs; each builder returns a window ready to grab."""
    long_source = "\n\n".join([SOURCE] * 24)
    long_target = "\n\n".join([TARGET] * 24)

    def drafted():
        window = _window(_Server(semantic))
        _load(window, SOURCE, TARGET)
        return window

    def terms():
        window = drafted()
        _review(window)
        window.manuscript_view._show_track(0)
        window.manuscript_view.findings.setCurrentRow(1)
        return window

    def semantic_issue():
        window = drafted()
        _review(window)
        view = window.manuscript_view
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        return window

    def long_reviewed():
        window = _window(_Server(semantic))
        _load(window, long_source, long_target)
        _review(window)
        return window

    def quota():
        window = _window(_Server(semantic, quota_after=1))
        _load(window, long_source, long_target)
        _review(window)
        window.manuscript_view._show_track(TRACK_SEMANTIC if semantic else 0)
        return window

    def segments():
        window = quota()
        window.manuscript_view._show_track(TRACK_SEGMENTS)
        window.manuscript_view.segment_list.setCurrentRow(1)
        return window

    if not semantic:
        return [("01-empty", lambda: _window(_Server(False))),
                ("02-unconfigured", lambda: _window(_Server(False), configured=False)),
                ("03-draft", drafted), ("04-terms", terms), ("05-long-reviewed", long_reviewed),
                ("06-quota-paused", quota), ("07-segments", segments)]
    return [("04-terms", terms), ("05-semantic-issue", semantic_issue),
            ("06-quota-paused", quota), ("07-segments", segments)]


def _capture(app, build, path: Path) -> bool:
    window = build()
    for _ in range(5):
        app.processEvents()
    image = window.grab()
    saved = image.save(str(path))
    print(f"{path.name} {image.width()}x{image.height()} px" if saved else f"failed to save {path}")
    window.manuscript_view._saved_payload = window.manuscript_view._payload
    window.close()
    window.deleteLater()
    app.processEvents()
    return saved


def main() -> int:
    keyring.set_keyring(_MemoryKeyring())
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    ARGS.out = ARGS.out.resolve() if ARGS.out.is_absolute() else (_ORIGINAL_CWD / ARGS.out).resolve()
    ARGS.out.mkdir(parents=True, exist_ok=True)
    scale_tag = ARGS.scale.replace(".", "")
    for scheme in ARGS.themes.split(","):
        theme.apply_theme(app, scheme)
        for mode, semantic in (("plain", False), ("semantic", True)):
            for name, build in _states(semantic):
                path = ARGS.out / f"{mode}-{name}-{scheme}-{scale_tag}x.png"
                if not _capture(app, build, path):
                    return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
