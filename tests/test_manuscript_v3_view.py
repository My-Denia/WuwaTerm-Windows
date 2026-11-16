"""Resumable long-manuscript term review in the workbench (manuscript v3).

Real widgets, the real ApiClient and parser; a MockTransport stands in for
Core and honours review-v2 resolutions the way Core does: a resolved finding
comes back verified (or not evaluated), and a stale context or an unknown
mention is refused with 400. All text is synthetic.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from test_manuscript_semantic import issue as semantic_issue, semantic_report
from wuwaterm_client import strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import load_workfile
from wuwaterm_client.markdown_report import MD
from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, TRACK_SEMANTIC, TRACK_TERMS, ManuscriptView


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate(zh: str, en: str) -> dict:
    return {"zh": zh, "en": en, "category": "resonator", "sources": [], "candidate_id": digest(zh + en)}


TERMS = {"甲术": [candidate("甲术", "Alpha")], "乙术": [candidate("乙术", "Beta"), candidate("乙术", "Bett")]}
# Six paragraphs, two per segment: three segments, the source well over 2,000 scalars.
SOURCE = "\n\n".join(f"甲术段{i:02d}" + ("乙术" if i == 3 else "") + "文" * 690 for i in range(6))
TARGET = "\n\n".join(f"Paragraph {i:02d} ".ljust(300, "x") for i in range(6))


class Core:
    """Counts requests and answers like review-v2 for the exact cores sent."""

    def __init__(self, capabilities=(), issues=None) -> None:
        self.capabilities, self.issues = capabilities, issues
        self.matcher, self.revision = "a" * 64, "b" * 64
        self.fail_once: set[str] = set()  # source-core prefixes refused once with 500
        self.requests: list[tuple[str, dict | None]] = []

    def sent(self, path: str = "/v1/reviews") -> list[dict]:
        return [body for p, body in self.requests if p == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body))
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"api_version": "v1", "capabilities": list(self.capabilities),
                                             "request_id": "caps"})
        if request.url.path == "/v1/semantic-reviews":
            return httpx.Response(200, json=semantic_report(body["source"], body["target"],
                                                            self.issues(body["source"], body["target"])))
        assert request.url.path == "/v1/reviews" and request.headers["x-wuwaterm-matcher-basis"] == "1"
        return self.review(body)

    def refuse(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "x"}, "request_id": "e"})

    def review(self, body: dict) -> httpx.Response:
        source, target = body["source"], body["target"]
        prefix = next((p for p in self.fail_once if source.startswith(p)), None)
        if prefix is not None:
            self.fail_once.discard(prefix)
            return self.refuse(500, "internal")
        resolved = {item["mention_id"]: item for item in body.get("resolutions", [])}
        if resolved and body["resolution_context"] != {
                "source_revision": digest(source), "rule_version": "review-v2",
                "dictionary_revision": self.revision, "matcher_revision": self.matcher}:
            return self.refuse(400, "invalid_request")
        findings = []
        for start in range(len(source)):
            for surface, candidates in TERMS.items():
                if not source.startswith(surface, start):
                    continue
                mention = f"{start}:{start + len(surface)}:{surface}"
                choice = resolved.pop(mention, None)
                verdict = "confirmed_conflict"
                if choice is not None and choice["choice"] == "not_a_term":
                    verdict = "not_evaluated"
                elif choice is not None:
                    if choice["candidate_id"] not in [c["candidate_id"] for c in candidates]:
                        return self.refuse(400, "invalid_request")
                    verdict = "verified_constraint"
                findings.append({"id": mention, "rule_id": "review.term_pair", "verdict": verdict,
                                 "source_span": {"start": start, "end": start + len(surface), "text": surface},
                                 "target_span": None, "candidates": candidates, "candidates_truncated": False})
        if resolved:
            return self.refuse(400, "invalid_request")
        return httpx.Response(200, json={
            "request_id": "term", "source_revision": digest(source), "target_revision": digest(target),
            "rule_version": "review-v2", "matcher_revision": self.matcher,
            "dictionary": {"schema_version": "1", "source_commit": "c" * 40, "term_count": 3, "revision": self.revision},
            "coverage": {"evaluated": len(findings), "not_evaluated": 0,
                         "rules": ["review.term_pair", "review.sentence_meaning"]},
            "findings": findings, "truncated": False})


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make_view(core: Core, source: str = SOURCE, target: str = TARGET) -> ManuscriptView:
    view = ManuscriptView(ApiClient("https://example.invalid", token_provider=lambda: "test-token",
                                    _test_transport=httpx.MockTransport(core)))
    value = {"format": "wuwaterm-manuscript-v1", "source": source, "target": target, "direction": "en",
             "alignments": None, "choices": [], "history": []}
    view._set_payload(value)
    view._saved_payload = view._payload.copy()
    view._update_controls()
    return view


async def review(view: ManuscriptView) -> None:
    view._start_review()
    for _ in range(200):
        tasks = [t for t in (view._task, view._semantic_task) if t is not None and not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            view._refresh_derived()
            return
        await asyncio.wait(tasks)
    raise AssertionError("the review did not settle")


def reopen(view: ManuscriptView, path, core: Core) -> ManuscriptView:
    """Save, then open the file again in the same window, as the owner would."""
    view._path = path
    assert view.save_document()
    view.new_document()
    assert view.open_path(path)
    view._refresh_derived()
    return view


def statuses(view: ManuscriptView) -> list[str]:
    return [view._segment_status(pair) for pair in view._current_pairs()]


def choose(view: ManuscriptView, segment: int, occurrence: int, option: int = 0) -> None:
    """Pick ``option`` (a candidate, or the last: not a term) at a finding of ``segment``."""
    view._show_track(TRACK_TERMS)
    rows = [row for row, ref in enumerate(view._finding_refs) if ref[0] == segment]
    view.findings.setCurrentRow(rows[occurrence])
    view.choice_combo.setCurrentIndex(option)
    assert view.choice_button.isEnabled()
    view.choice_button.click()


def select_segment(view: ManuscriptView, index: int) -> None:
    view._show_track(TRACK_SEGMENTS)
    view.segment_list.setCurrentRow(index)


def type_at(editor, position: int, text: str) -> None:
    cursor = editor.textCursor()
    cursor.setPosition(position)
    editor.setTextCursor(cursor)
    editor.insertPlainText(text)


def test_reopening_restores_choices_marks_and_position_and_one_probe_confirms_everything(qapp, tmp_path) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        assert statuses(view) == ["reviewed"] * 3 and len(core.sent()) == 3
        choose(view, 1, 0)
        await review(view)
        select_segment(view, 0)
        view.finish_button.click()
        select_segment(view, 2)
        assert strings.MANUSCRIPT_SEGMENT_FINISHED_MARK in view.segment_list.item(0).text()
        sent = len(core.requests)
        reopened = reopen(view, tmp_path / "long.json", core)
        assert len(core.requests) == sent, "opening sends nothing"
        assert reopened._payload["progress"] == {"current": 2, "completed": [0]}
        assert [c["confirmed"] for c in reopened._payload["segments"][1]["choices"]] == [True]
        assert statuses(reopened) == ["saved"] * 3 and reopened._review_state == "saved"
        assert reopened.report_label.text() == strings.MANUSCRIPT_SAVED_REPORT.format(count=7)
        reopened._show_track(TRACK_SEGMENTS)
        assert reopened.segment_list.currentRow() == 2
        assert not reopened.choice_button.isEnabled(), "no choice against a result not checked yet"
        before = len(core.sent())
        await review(reopened)
        [probe] = core.sent()[before:]
        assert "resolutions" not in probe and probe["source"].startswith("甲术段04"), "the probe is progress.current"
        assert statuses(reopened) == ["reviewed"] * 3 and not reopened.document_dirty
    asyncio.run(scenario())


def test_a_choice_goes_out_as_a_resolution_and_is_not_raised_again_after_reopening(qapp, tmp_path) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        choose(view, 1, 0)
        assert view._payload["segments"][1]["status"] == "stale"
        assert view.status_label.text() == strings.MANUSCRIPT_TERM_CHOICE_RECORDED
        before = len(core.sent())
        await review(view)
        [sent] = core.sent()[before:]
        assert sent["resolutions"] == [{"mention_id": "0:2:甲术", "choice": "official_pair",
                                        "candidate_id": TERMS["甲术"][0]["candidate_id"]}]
        assert sent["resolution_context"] == {"source_revision": digest(sent["source"]), "rule_version": "review-v2",
                                              "dictionary_revision": core.revision, "matcher_revision": core.matcher}
        reopened = reopen(view, tmp_path / "chosen.json", core)
        await review(reopened)
        row = next(row for row, ref in enumerate(reopened._finding_refs) if ref == (1, 0, 2))
        assert reopened._report["findings"][row]["verdict"] == "verified_constraint"
        assert strings.MANUSCRIPT_VERDICT_CONFLICT not in reopened.findings.item(row).text()
    asyncio.run(scenario())


def test_a_matcher_change_re_checks_every_record_keeps_marks_and_sends_choices_again(qapp, tmp_path) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        choose(view, 1, 0)
        await review(view)
        select_segment(view, 0)
        view.finish_button.click()
        reopened = reopen(view, tmp_path / "matcher.json", core)
        core.matcher = "d" * 64
        before = len(core.sent())
        await review(reopened)
        again = core.sent()[before:]
        # The probe, the two other segments, and the kept choice sent once more.
        assert len(again) == 4 and "resolutions" not in again[0]
        [resolved] = [body for body in again if "resolutions" in body]
        assert resolved["source"].startswith("甲术段02") and resolved["resolution_context"] == {
            "source_revision": digest(resolved["source"]), "rule_version": "review-v2",
            "dictionary_revision": core.revision, "matcher_revision": "d" * 64}
        assert statuses(reopened) == ["reviewed"] * 3
        assert reopened._payload["progress"]["completed"] == [0]
    asyncio.run(scenario())


def test_a_refused_resolution_context_falls_back_once_and_the_choice_goes_out_again(qapp) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        choose(view, 1, 0)
        core.matcher = "e" * 64  # Core moved on after this session's last check
        before = len(core.sent())
        await review(view)
        sent = core.sent()[before:]
        # Refused (stale context), the same segment without resolutions, then
        # with the kept choice under the new basis; the other two re-checked.
        assert ["resolutions" in body for body in sent] == [True, False, True, False, False]
        assert sent[2]["resolution_context"]["matcher_revision"] == "e" * 64
        assert statuses(view) == ["reviewed"] * 3
        assert [c["confirmed"] for c in view._payload["segments"][1]["choices"]] == [True]
    asyncio.run(scenario())


def test_an_edit_inside_one_segment_makes_only_it_pending_and_drops_only_its_mark(qapp) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        for index in (0, 1):
            select_segment(view, index)
            view.finish_button.click()
        choose(view, 1, 2, option=2)  # 乙术 in paragraph 3: not a term
        records = list(view._payload["segments"])
        assert view._draft_snapshot()["segments"] is view._payload["segments"], "snapshots share the records"
        at = SOURCE.index("甲术段02") + 20  # in segment 1, before the choice
        type_at(view.source_edit, at, "改")
        assert view._payload["segments"][0] is records[0], "a keystroke copies no untouched record"
        view._refresh_derived()
        assert view.source_edit.raw_cursor_offset() == at + 1, "the refreshed list does not move the caret"
        assert statuses(view) == ["reviewed", "pending", "reviewed"]
        assert view._payload["progress"]["completed"] == [0]
        [kept] = view._payload["segments"][1]["choices"]
        start = view._payload["segments"][1]["source_start"] + kept["source_span"]["start"]
        assert view._payload["source"][start:start + 2] == "乙术" and not kept["confirmed"], "kept, to reconfirm"
        view.segment_list.setCurrentRow(2)
        view._next_pending()
        assert view.segment_list.currentRow() == 1
        view._undo_replace()
        view._refresh_derived()
        # Back to the choice just made: its segment awaits the check that sends it.
        assert statuses(view) == ["reviewed", "stale", "reviewed"]
        assert view._payload["progress"]["completed"] == [0, 1]
    asyncio.run(scenario())


def test_undo_after_a_review_keeps_the_results_of_segments_the_undo_does_not_change(qapp) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        view._refresh_derived()
        type_at(view.source_edit, SOURCE.index("甲术段02") + 20, "改")
        await review(view)
        view._undo_replace()
        view._refresh_derived()
        assert statuses(view) == ["reviewed", "pending", "reviewed"]
    asyncio.run(scenario())


def test_a_failed_segment_keeps_the_others_and_the_next_run_continues_from_it(qapp) -> None:
    async def scenario():
        core = Core()
        core.fail_once = {"甲术段02"}
        view = make_view(core)
        await review(view)
        assert statuses(view) == ["reviewed", "failed", "reviewed"]
        before = len(core.sent())
        await review(view)
        assert [body["source"][:5] for body in core.sent()[before:]] == ["甲术段02"]
        assert statuses(view) == ["reviewed"] * 3
    asyncio.run(scenario())


def test_leaving_long_mode_moves_choices_to_history_and_the_file_stays_v3(qapp, tmp_path) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        choose(view, 0, 0)
        cursor = view.source_edit.textCursor()
        cursor.setPosition(SOURCE.index("甲术段02"))
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        view.source_edit.setTextCursor(cursor)
        cursor.removeSelectedText()
        view._refresh_derived()
        assert "segments" not in view._payload or view._payload["segments"] == []
        [entry] = view._payload["segment_choice_history"]
        assert entry["source_span"]["text"] == "甲术" and entry["choice"] == "official_pair"
        view._path = tmp_path / "short.json"
        assert view.save_document()
        assert load_workfile(view._path)["format"] == "wuwaterm-manuscript-v3"
    asyncio.run(scenario())


def test_automatic_alignment_says_how_many_segment_results_are_checked_again(qapp) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        view._payload["alignments"] = []
        await review(view)
        view._use_auto_alignment()
        view._refresh_derived()
        assert view.status_label.text() == strings.MANUSCRIPT_AUTO_ALIGN_RECHECK.format(count=3)
        assert statuses(view) == ["stale"] * 3
    asyncio.run(scenario())


def test_a_save_over_the_history_cap_changes_nothing_and_lists_entries_to_remove(qapp, tmp_path) -> None:
    async def scenario():
        view = make_view(Core())
        view._refresh_derived()
        entry = {"source_hash": "0" * 64, "source_span": {"start": 0, "end": 2, "text": "甲术"},
                 "choice": "not_a_term", "candidate": None, "offered": []}
        view._payload["segment_choice_history"] = [entry] * 513
        path = tmp_path / "full.json"
        view._path = path
        assert not view.save_document() and not path.exists()
        assert "512" in view.status_label.text() and view.conflict_list.count() == 513
        view.conflict_list.setCurrentRow(0)
        view.remove_choice_button.click()
        assert len(view._payload["segment_choice_history"]) == 512 and view.conflict_list.isHidden()
        assert view.save_document() and path.exists()
    asyncio.run(scenario())


def test_a_handled_semantic_suggestion_stays_handled_after_reopening_a_v3_file(qapp, tmp_path) -> None:
    def issues(source, target):
        if not source.startswith("甲术段02"):
            return []
        return [semantic_issue(source, target, source_quote="甲术段02", target_quote="Paragraph 02",
                               replace="Paragraph 02", replacement="Paragraph two")]

    async def scenario():
        core = Core(capabilities=("semantic-review-v1",), issues=issues)
        view = make_view(core)
        await review(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        view.ignore_button.click()
        reopened = reopen(view, tmp_path / "semantic.json", core)
        assert load_workfile(tmp_path / "semantic.json")["format"] == "wuwaterm-manuscript-v3"
        await review(reopened)
        [live] = reopened._session.issues()
        assert reopened._issue_state(live) == "ignored_carried"
        assert reopened.semantic_track_button.text() == strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=0)
        # The two tracks keep their own names and counts: term findings are not suggestions.
        assert reopened.terms_track_button.text() == strings.MANUSCRIPT_TRACK_TERMS.format(count=7)
    asyncio.run(scenario())


class GatedCore(Core):
    """Refuses the first request that carries resolutions with 400, but only
    once the test opens ``gate``, so the test can edit while it is out."""

    def __init__(self) -> None:
        super().__init__()
        self.gate, self.waiting = asyncio.Event(), False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        if request.url.path == "/v1/reviews" and body.get("resolutions") and not self.waiting:
            self.requests.append((request.url.path, body))
            self.waiting = True
            await self.gate.wait()
            return self.refuse(400, "invalid_request")
        return Core.__call__(self, request)


def test_an_edit_while_a_refused_request_is_out_sends_no_fallback_for_the_old_segment(qapp) -> None:
    async def scenario():
        core = GatedCore()
        view = make_view(core)
        await review(view)
        choose(view, 1, 0)
        view._start_review()
        for _ in range(500):
            if core.waiting:
                break
            await asyncio.sleep(0)
        assert core.waiting, "the request with the choice went out"
        sent = len(core.sent())
        # The owner deletes segments 1 and 2 while Core is still answering.
        cursor = view.source_edit.textCursor()
        cursor.setPosition(SOURCE.index("甲术段02") - 2)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        core.gate.set()
        for _ in range(200):
            tasks = [t for t in (view._task, view._semantic_task) if t is not None and not t.done()]
            if not tasks:
                break
            await asyncio.wait(tasks)
        assert view._semantic_state != "error"
        assert len(core.sent()) == sent, "no fallback for a segment that is gone"
    asyncio.run(scenario())


class HeldCore(Core):
    """Holds every review request until the test opens ``gate``."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.gate.set()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/reviews":
            await self.gate.wait()
        return Core.__call__(self, request)


def test_the_running_label_names_the_segment_in_flight_not_the_count_plus_one(qapp) -> None:
    async def scenario():
        core = HeldCore()
        view = make_view(core)
        await review(view)
        type_at(view.source_edit, 3, "改")  # inside segment 1 of 3; the other two stay current
        view._refresh_derived()
        assert statuses(view) == ["pending", "reviewed", "reviewed"]
        core.gate.clear()
        before = len(core.sent())
        view._start_review()
        for _ in range(500):
            if view._session.pending is not None:
                break
            await asyncio.sleep(0)
        view._render_semantic_progress()
        # Two of three are already reviewed; the one being checked is the first.
        assert view.semantic_progress.text().startswith(
            strings.MANUSCRIPT_SEMANTIC_RUNNING.format(index=1, count=3, done=2, total=3))
        core.gate.set()
        for _ in range(200):
            tasks = [t for t in (view._task, view._semantic_task) if t is not None and not t.done()]
            if not tasks:
                break
            await asyncio.wait(tasks)
        assert len(core.sent()) == before + 1 and statuses(view) == ["reviewed"] * 3
    asyncio.run(scenario())


def exported(view: ManuscriptView, path, monkeypatch) -> str:
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(path), ""))
    assert view.export_report()
    return path.read_text(encoding="utf-8")


def test_a_report_export_sends_nothing_changes_nothing_and_is_not_a_manuscript(qapp, tmp_path, monkeypatch) -> None:
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        assert "当前报告：与当前文本" in exported(view, tmp_path / "first.md", monkeypatch), "every segment current"
        choose(view, 1, 0)  # 甲术 in segment 2: the official pair; the segment awaits its re-check
        view._path = tmp_path / "work.json"
        assert view.save_document()
        sent, saved = len(core.requests), view._saved_payload
        report = tmp_path / "report.md"
        text = exported(view, report, monkeypatch)
        assert "需要重新核对：报告与当前文本或选择已不一致" in text
        assert len(core.requests) == sent and view._saved_payload is saved and not view.document_dirty
        assert view.document_path == tmp_path / "work.json"
        assert "## 逐段术语核对（3 段）" in text and "### 第 1 段\n" in text
        assert "### 第 2 段 · 需重新核对：文字、对应方式、选择或服务依据已变化" in text
        start = SOURCE.index("甲术段02")
        assert f"- 术语选择 · `甲术` · 原文位置 {start + 1}–{start + 2} · `甲术` / `Alpha` · 已确认，随核对提交" in text
        # A report never opens or imports as a manuscript; the open document stays.
        assert not view.open_path(report)
        assert view.status_label.text() == strings.MANUSCRIPT_REPORT_NOT_MANUSCRIPT
        assert not view.import_files([str(report)], "source")
        assert view.status_label.text() == strings.MANUSCRIPT_REPORT_NOT_MANUSCRIPT
        assert view.document_path == tmp_path / "work.json" and view._payload["source"] == SOURCE
        view.new_document()
        assert view.open_path(tmp_path / "work.json")
        assert "### 第 1 段 · 来自已保存的稿件，尚未与当前服务确认" in exported(view, report, monkeypatch)
        assert core.requests[sent:] == [], "neither the exports nor the reopening sent anything"
    asyncio.run(scenario())


def test_a_short_manuscript_report_states_current_stale_or_imported(qapp, tmp_path, monkeypatch) -> None:
    async def scenario():
        core = Core()
        view = make_view(core, "甲术在此。", "Alpha here.")
        report = tmp_path / "report.md"
        assert "当前没有审校报告" in exported(view, report, monkeypatch)
        await review(view)
        text = exported(view, report, monkeypatch)
        assert "当前报告：与当前文本、方向和已提交的局部选择一致。" in text and "### 1. 与所选词对冲突 · `甲术`" in text
        type_at(view.target_edit, 0, "x")
        assert "需要重新核对：报告与当前文本或选择已不一致" in exported(view, report, monkeypatch)
        view._path = tmp_path / "short.json"
        assert view.save_document()
        view.new_document()
        assert view.open_path(tmp_path / "short.json")
        assert "导入的历史报告：来自稿件文件，未经本次会话验证" in exported(view, report, monkeypatch)
    asyncio.run(scenario())


class TruncatingCore(Core):
    """Core's answer for the segment holding ``marker`` is truncated, and its
    findings' candidate lists are cut too."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def review(self, body: dict) -> httpx.Response:
        response = super().review(body)
        if response.status_code != 200 or self.marker not in body["source"]:
            return response
        data = response.json()
        data["truncated"] = True
        for finding in data["findings"]:
            finding["candidates_truncated"] = True
        return httpx.Response(200, json=data)


def test_a_truncated_segment_says_it_has_more_findings_and_still_takes_choices(qapp) -> None:
    async def scenario():
        view = make_view(TruncatingCore("甲术段02"))
        await review(view)
        view._show_track(TRACK_SEGMENTS)
        rows = [view.segment_list.item(i).text() for i in range(view.segment_list.count())]
        shown = len(view._payload["segments"][1]["result"]["findings"])
        mark = strings.MANUSCRIPT_SEGMENT_TRUNCATED_MARK.format(count=shown)
        assert [mark in row for row in rows] == [False, True, False]
        note = strings.MANUSCRIPT_SEGMENT_TERMS_TRUNCATED.format(number=2, count=shown)
        select_segment(view, 1)
        assert note in view.segment_hint.text()
        view._show_track(TRACK_TERMS)
        row = next(r for r, ref in enumerate(view._finding_refs) if ref[0] == 1)
        view.findings.setCurrentRow(row)
        notes = view.candidates_label.text()
        assert strings.MANUSCRIPT_CANDIDATES_TRUNCATED in notes and note in notes
        choose(view, 1, 0)  # the shown finding still takes a choice
        assert [c["confirmed"] for c in view._payload["segments"][1]["choices"]] == [True]
        other = next(r for r, ref in enumerate(view._finding_refs) if ref[0] == 0)
        view.findings.setCurrentRow(other)
        assert note not in view.candidates_label.text()
    asyncio.run(scenario())


def test_a_long_manuscript_report_leaves_out_a_document_report_from_a_shorter_text(qapp, tmp_path, monkeypatch) -> None:
    async def scenario():
        view = make_view(Core(), "甲术在此。", "Alpha here.")
        await review(view)
        assert view._payload["history"], "the short text has a document report"
        type_at(view.source_edit, len("甲术在此。"), "\n\n" + "文" * 2100)
        view._refresh_derived()
        text = exported(view, tmp_path / "report.md", monkeypatch)
        assert "## 结论摘要" not in text and "当前没有审校报告" in text
    asyncio.run(scenario())


class FailingCore(Core):
    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    def review(self, body: dict) -> httpx.Response:
        return self.refuse(500, "internal") if self.failing else super().review(body)


def test_a_short_report_is_current_only_while_the_workbench_says_so(qapp, tmp_path, monkeypatch) -> None:
    async def scenario():
        core = FailingCore()
        view = make_view(core, "甲术在此。", "Alpha here.")
        report = tmp_path / "report.md"
        await review(view)
        assert "当前报告：与当前文本" in exported(view, report, monkeypatch)
        # Another service: the report was checked in this session, but not by it.
        view.reset_for_endpoint_change()
        assert view._review_state == "stale"
        assert "需要重新核对：报告与当前文本或选择已不一致" in exported(view, report, monkeypatch)
        core.failing = True
        await review(view)
        assert view._review_state == "error"
        text = exported(view, report, monkeypatch)
        assert "当前报告：与当前文本" not in text and "需要重新核对" in text
    asyncio.run(scenario())


def test_a_report_never_overwrites_the_open_manuscript(qapp, tmp_path, monkeypatch) -> None:
    view = make_view(Core(), "甲术在此。", "Alpha here.")
    view._path = tmp_path / "work.json"
    assert view.save_document()
    before = view._path.read_bytes()
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *args, **kwargs: (str(tmp_path / "." / "work.json"), ""))
    assert not view.export_report()
    assert view._path.read_bytes() == before and not view.document_dirty
    assert view.status_label.text() == strings.MANUSCRIPT_EXPORT_OVER_MANUSCRIPT


def test_two_short_checks_in_a_session_compare_as_automatic_checks(qapp, tmp_path, monkeypatch) -> None:
    async def scenario():
        view = make_view(Core(), "甲术在此。", "Alpha here.")
        await review(view)
        type_at(view.target_edit, len("Alpha here."), " Again.")  # only the translation changes
        await review(view)
        # Both checks sent no resolutions; the file and the comparison say so.
        assert [snapshot["resolutions"] for snapshot in view._payload["history"]] == [[], []]
        text = exported(view, tmp_path / "report.md", monkeypatch)
        assert "### 仍待确认（1）" in text and "前后选择不同或选择记录未知" not in text
    asyncio.run(scenario())


def test_an_exported_long_report_says_which_segment_was_truncated(qapp, tmp_path, monkeypatch) -> None:
    async def scenario():
        view = make_view(TruncatingCore("甲术段02"))
        await review(view)
        text = exported(view, tmp_path / "report.md", monkeypatch)
        sections = text.split("### 第 ")
        second = next(part for part in sections if part.startswith("2 段"))
        assert MD["truncatedLine"] in second and " · " + MD["candidatesTruncatedLine"] in second
        assert not any(MD["truncatedLine"] in part for part in sections if part.startswith(("1 段", "3 段")))
    asyncio.run(scenario())


@pytest.mark.parametrize("before", [0, 2], ids=["forward", "back"])
def test_a_segment_jump_shows_the_start_of_a_segment_taller_than_the_editor(qapp, before) -> None:
    """Native acceptance at 150%: jumping back to a segment taller than the editor left
    only its last line on screen. Forward and back, both editors show the core's first
    line, and the core stays selected."""
    view = make_view(Core())
    view.resize(900, 520)
    view.show()
    qapp.processEvents()
    view._refresh_derived()
    pairs = view._current_pairs()

    def at(editor, position: int):
        cursor = QTextCursor(editor.document())
        cursor.setPosition(position)  # SOURCE and TARGET are BMP text without CR: scalar == Qt position
        return editor.cursorRect(cursor)

    select_segment(view, before)
    select_segment(view, 1)
    qapp.processEvents()
    for editor, region in ((view.source_edit, pairs[1].source), (view.target_edit, pairs[1].target)):
        cursor = editor.textCursor()
        assert (cursor.selectionStart(), cursor.selectionEnd()) == (region.core_start, region.core_end)
        assert editor.viewport().rect().contains(at(editor, region.core_start)), editor.side
    assert not view.source_edit.viewport().rect().contains(at(view.source_edit, pairs[1].source.core_end)), \
        "the source segment is taller than the editor"
    view.close()


WAITING = "等待此段核对后生效"  # the Web's ``choiceWaiting`` (lib/semantic-messages.ts)


def choice_labels(view: ManuscriptView, index: int) -> tuple[str, list[str]]:
    """Segment ``index``'s row in the segment list and its choice rows, freshly rendered."""
    view._refresh_derived()
    select_segment(view, 1 if index == 0 else 0)
    select_segment(view, index)
    return (view.segment_list.item(index).text(),
            [view.segment_choices.item(row).text() for row in range(view.segment_choices.count())])


def test_choices_of_a_pending_segment_wait_for_its_check_and_are_not_raised_for_reconfirmation(qapp, tmp_path) -> None:
    """SPEC: a pending record's choices wait for the next check, which realigns them;
    「待重新确认」 is for done or stale records, and 重新确认 needs a result. As on the
    Web, every choice of a pending segment shows as waiting, in the session after a
    translation edit (still confirmed) and after reopening (written unconfirmed)."""
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        choose(view, 1, 0)
        await review(view)
        type_at(view.target_edit, TARGET.index("Paragraph 02") + 20, "改")  # the translation alone

        def assert_waiting(confirmed: list[bool]) -> None:
            record = view._payload["segments"][1]
            assert record["status"] == "pending" and [c["confirmed"] for c in record["choices"]] == confirmed
            row, items = choice_labels(view, 1)
            assert WAITING in row and "待重新确认" not in row
            assert items == [f"甲术 · {view._describe_choice(record['choices'][0])}（{WAITING}）"]
            view.segment_choices.setCurrentRow(0)
            assert not view.segment_choice_action.isEnabled()

        assert_waiting([True])
        assert reopen(view, tmp_path / "waiting.json", core) is view
        assert_waiting([False])
        await review(view)
        record = view._payload["segments"][1]
        assert record["status"] == "done" and [c["confirmed"] for c in record["choices"]] == [True]
        row, items = choice_labels(view, 1)
        assert WAITING not in row and "待重新确认" not in row
        assert items == [strings.MANUSCRIPT_SEGMENT_CHOICE_ITEM.format(
            text="甲术", choice=view._describe_choice(record["choices"][0]))]
    asyncio.run(scenario())


def test_an_unconfirmed_choice_of_a_checked_segment_still_asks_for_reconfirmation(qapp, tmp_path, monkeypatch) -> None:
    """The candidate the choice named is gone after Core's dictionary moved on: the
    re-check leaves the choice unconfirmed in a done record, which asks for
    reconfirmation; so does the same record reopened and shown as saved."""
    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        choose(view, 1, 0)
        await review(view)
        monkeypatch.setitem(TERMS, "甲术", [candidate("甲术", "Alpha Prime")])
        core.matcher = "f" * 64
        reopen(view, tmp_path / "moved.json", core)
        await review(view)

        def assert_reconfirm(status: str) -> None:
            record = view._payload["segments"][1]
            assert statuses(view)[1] == status and record["status"] == "done"
            assert [c["confirmed"] for c in record["choices"]] == [False]
            row, items = choice_labels(view, 1)
            assert strings.MANUSCRIPT_SEGMENT_RECONFIRM_MARK.format(count=1) in row and WAITING not in row
            assert items == [strings.MANUSCRIPT_SEGMENT_CHOICE_UNCONFIRMED.format(
                text="甲术", choice=view._describe_choice(record["choices"][0]))]

        assert_reconfirm("reviewed")
        reopen(view, tmp_path / "moved.json", core)
        assert_reconfirm("saved")
    asyncio.run(scenario())


def test_opening_another_long_manuscript_lists_its_segments_and_marks_the_one_selected(qapp, tmp_path, monkeypatch) -> None:
    """Native acceptance of 0.5.0: with 「段落」 showing, opening a three-segment manuscript
    after a five-segment one kept the five old rows (「0 / 5」 under 「段落 3」); a stale
    row selected its old range across two segments of the new text, and 「标记完成」
    marked that index in the new file. Each open, and a new document, lists exactly its
    own segments, so the selected row is the segment that gets marked."""
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: pytest.fail("unexpected unsaved-changes prompt"))
    # One paragraph per segment, cores 905 scalars long against 1,392 in SOURCE.
    five = ("\n\n".join(f"甲术节{i:02d}" + "字" * 900 for i in range(5)),
            "\n\n".join(f"Section {i:02d} ".ljust(400, "y") for i in range(5)))

    def assert_lists(view: ManuscriptView, count: int) -> None:
        pairs, records = view._current_pairs(), view._records()
        assert len(pairs) == count
        assert [view.segment_list.item(row).text() for row in range(view.segment_list.count())] == [
            strings.MANUSCRIPT_SEGMENT_ITEM.format(
                number=pair.index + 1, status=view._segment_row_status(pair, records),
                source=pair.source.core_end - pair.source.core_start,
                target=pair.target.core_end - pair.target.core_start) for pair in pairs]
        row = view.segment_list.currentRow()
        assert view.finding_count.text() == strings.MANUSCRIPT_FINDING_COUNT.format(
            current=row + 1 if row >= 0 else 0, total=count)
        assert view.segments_track_button.text() == strings.MANUSCRIPT_TRACK_SEGMENTS.format(count=count)

    async def scenario():
        core = Core()
        for name, (source, target) in (("five.json", five), ("three.json", (SOURCE, TARGET))):
            author = make_view(core, source, target)
            await review(author)
            author._path = tmp_path / name
            assert author.save_document()
        view = make_view(core, "", "")
        view._show_track(TRACK_SEGMENTS)
        for name, count in (("five.json", 5), ("three.json", 3)):
            assert view.open_path(tmp_path / name)
            assert_lists(view, count)
        pair = view._current_pairs()[1]
        assert view.segment_list.currentRow() != 1
        view.segment_list.setCurrentRow(1)  # as a click: no track switch that would rebuild the list
        for editor, region in ((view.source_edit, pair.source), (view.target_edit, pair.target)):
            cursor = editor.textCursor()  # BMP text without CR: scalar == Qt position
            assert (cursor.selectionStart(), cursor.selectionEnd()) == (region.core_start, region.core_end), editor.side
        assert view.finish_button.isEnabled()
        view.finish_button.click()
        assert view._payload["progress"]["completed"] == [1]
        assert [strings.MANUSCRIPT_SEGMENT_FINISHED_MARK in view.segment_list.item(row).text()
                for row in range(view.segment_list.count())] == [False, True, False]
        assert view.save_document()
        assert load_workfile(tmp_path / "three.json")["progress"]["completed"] == [1]
        assert view.new_document()
        assert_lists(view, 0)
    asyncio.run(scenario())


def test_a_row_listed_before_an_undo_into_long_mode_does_nothing_until_the_list_follows(qapp) -> None:
    """Undo back into long mode keeps the short text's rows listed until the debounced
    refresh. Clicking one paired the text itself and enabled 「标记完成」 for a row whose
    offsets and index were the short text's. Such a row now selects and marks nothing;
    once the list follows, the selected row is the segment that gets marked."""
    async def scenario():
        view = make_view(Core())
        await review(view)
        view._show_track(TRACK_SEGMENTS)
        assert view._replace_side("source", "\n\n".join(f"甲术段{i:02d}" + "文" * 100 for i in range(6)))
        view._refresh_derived()
        assert not view._long_now() and view.segment_list.count() > 1
        view._undo_replace()
        assert view._long_now() and view.segment_list.currentRow() != 1
        view.segment_list.setCurrentRow(1)  # within the debounce: the short text's row
        assert not view.finish_button.isEnabled()
        assert not view.source_edit.textCursor().hasSelection() and not view.target_edit.textCursor().hasSelection()
        view._refresh_derived()
        pair = view._current_pairs()[1]
        view.segment_list.setCurrentRow(0)
        view.segment_list.setCurrentRow(1)
        for editor, region in ((view.source_edit, pair.source), (view.target_edit, pair.target)):
            cursor = editor.textCursor()
            assert (cursor.selectionStart(), cursor.selectionEnd()) == (region.core_start, region.core_end), editor.side
        assert view.finish_button.isEnabled()
        view.finish_button.click()
        assert view._payload["progress"]["completed"] == [1]
    asyncio.run(scenario())


def test_a_row_clicked_before_the_list_follows_an_edit_is_the_selected_segment_afterwards(qapp, tmp_path, monkeypatch) -> None:
    """A click between an edit and the list's refresh selects nothing at once; once the
    list follows, that row is the selected segment: both editors select its cores and a
    save writes it as progress.current, not the row selected before the edit."""
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: pytest.fail("unexpected unsaved-changes prompt"))

    async def scenario():
        core = Core()
        view = make_view(core)
        await review(view)
        path = tmp_path / "position.json"
        reopen(view, path, core)
        view._show_track(TRACK_SEGMENTS)
        view.segment_list.setCurrentRow(0)
        type_at(view.target_edit, TARGET.index("Paragraph 00") + 20, "改")
        view.segment_list.setCurrentRow(2)  # within the debounce
        view._refresh_derived()
        assert view.segment_list.currentRow() == 2
        pair = view._current_pairs()[2]
        for editor, region in ((view.source_edit, pair.source), (view.target_edit, pair.target)):
            cursor = editor.textCursor()
            assert (cursor.selectionStart(), cursor.selectionEnd()) == (region.core_start, region.core_end), editor.side
        assert view.save_document()
        assert load_workfile(path)["progress"]["current"] == 2
    asyncio.run(scenario())
