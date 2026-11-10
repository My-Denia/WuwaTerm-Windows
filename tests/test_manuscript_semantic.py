"""Semantic review in the manuscript workbench, exercised through real widgets.

Requests go through the real ApiClient and httpx MockTransport, so the report
validation, the segmentation-v1 pairing and the apply path are the shipped
code. Model reports are synthetic and built for the exact texts sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from wuwaterm_client import semantic, strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import WORKFILE_FORMAT_V2, empty_workfile, load_workfile
from wuwaterm_client.segmentation import SEGMENT_LIMIT
from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, TRACK_SEMANTIC, ManuscriptView

HEX = "c" * 64


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span(text: str, quote: str | None, occurrence: int = 0) -> dict | None:
    if quote is None:
        return None
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"start": start, "end": start + len(quote), "text": quote}


def issue(source: str, target: str, *, category="negation_change", confidence="medium",
          source_quote=None, target_quote=None, replace=None, replacement=None, alternative=None,
          withheld=None, seed="0") -> dict:
    source_span, target_span = span(source, source_quote), span(target, target_quote)
    suggestion = None
    if replace is not None and withheld is None:
        suggestion = {"target_span": span(target, replace), "replacement": replacement, "term_effect": "none"}
    return {
        "id": hashlib.sha256(f"id{seed}{source}{target}".encode()).hexdigest()[:32],
        "fingerprint": hashlib.sha256(f"fp{seed}{category}{source_quote}{target_quote}".encode()).hexdigest()[:32],
        "category": category, "origin": "model", "confidence": confidence,
        "source_span": source_span, "target_span": target_span,
        "source_excerpt": None if source_span is None else semantic.excerpt(source_quote),
        "target_excerpt": None if target_span is None else semantic.excerpt(target_quote),
        "rationale": "模型的理由。", "suggestion": suggestion, "suggestion_withheld": withheld,
        "alternatives": ([{"replacement": alternative, "term_effect": "alters_official_match"}]
                         if alternative is not None and suggestion is not None else []),
    }


def semantic_report(source: str, target: str, issues=(), *, status="complete") -> dict:
    return {
        "request_id": "semantic", "review_version": "semantic-review-v1",
        "source_revision": digest(source), "target_revision": digest(target),
        "term_basis": {"dictionary_revision": HEX, "matcher_revision": HEX},
        "status": status, "status_reason": None if status == "complete" else "invalid_model_output",
        "dropped_unanchored": 0,
        "checked": list(semantic.CATEGORIES) if status == "complete" else [],
        "issues": list(issues), "truncated": False, "truncated_reason": None, "omitted_issues": 0,
    }


def term_report(source: str, target: str) -> dict:
    return {
        "request_id": "term", "source_revision": digest(source), "target_revision": digest(target),
        "rule_version": "review-v2", "matcher_revision": "a" * 64,
        "dictionary": {"schema_version": "1", "source_commit": "b" * 40, "term_count": 1, "revision": "a" * 64},
        "coverage": {"evaluated": 0, "not_evaluated": 0, "rules": ["term"]}, "findings": [], "truncated": False,
    }


class Server:
    """Records every request; answers capabilities, reviews and semantic reviews."""

    def __init__(self, capabilities=("semantic-review-v1",), issues=None):
        self.capabilities = capabilities
        self.issues = issues or (lambda source, target: [])
        self.requests: list[tuple[str, dict | None]] = []
        self.semantic_errors: list[httpx.Response] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, body))
        if request.url.path == "/v1/capabilities":
            if self.capabilities is None:
                return httpx.Response(404, json={"error": {"code": "invalid_request", "message": "x"}, "request_id": "r"})
            return httpx.Response(200, json={"api_version": "v1", "capabilities": list(self.capabilities),
                                             "request_id": "caps"})
        if request.url.path == "/v1/reviews":
            return httpx.Response(200, json=term_report(body["source"], body["target"]))
        if request.url.path == "/v1/semantic-reviews":
            if self.semantic_errors:
                return self.semantic_errors.pop(0)
            return httpx.Response(200, json=semantic_report(
                body["source"], body["target"], self.issues(body["source"], body["target"])))
        if request.url.path == "/v1/translations":
            return httpx.Response(200, json={"kind": "llm", "text": f"[{body['text']}]", "direction": body.get("to") or "en",
                                             "dictionary_miss": False, "request_id": "tr"})
        raise AssertionError(request.url.path)

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]

    def sent(self, path: str) -> list[dict]:
        return [body for p, body in self.requests if p == path]


def make_view(server: Server, source: str, target: str, direction: str = "en") -> ManuscriptView:
    client = ApiClient("https://example.invalid", token_provider=lambda: "test-token",
                       _test_transport=httpx.MockTransport(server))
    view = ManuscriptView(client)
    value = empty_workfile()
    value.update(source=source, target=target, direction=direction)
    view._set_payload(value)
    view._saved_payload = view._payload.copy()
    view._update_controls()
    return view


async def settle(view: ManuscriptView) -> None:
    for _ in range(200):
        tasks = [t for t in (view._task, view._semantic_task, view._translate_task) if t is not None and not t.done()]
        if not tasks:
            await asyncio.sleep(0)
            return
        await asyncio.wait(tasks)
    raise AssertionError("tasks did not settle")


SOURCE = "甲乙在第三天不会出发，丙丁随后到达。"
TARGET = "Jia and Yi will leave on the second day; Bing and Ding arrive later."


def two_issues(source, target):
    return [
        issue(source, target, category="quantity_change", confidence="high", source_quote="第三天",
              target_quote="second day", replace="second", replacement="third", alternative="3rd", seed="1"),
        issue(source, target, source_quote="不会出发", target_quote="will leave", replace="will leave",
              replacement="will not leave", seed="2"),
    ]


def test_old_core_keeps_the_term_review_only(qapp) -> None:
    async def scenario():
        server = Server(capabilities=None)
        view = make_view(server, SOURCE, TARGET)
        view._start_review()
        await settle(view)
        assert server.paths() == ["/v1/capabilities", "/v1/reviews"]
        assert view._review_state == "current"
        assert view.semantic_progress.text() == strings.MANUSCRIPT_SEMANTIC_UNAVAILABLE
        assert view.semantic_list.count() == 0
        # Semantic review is off by default on servers: no empty track or card.
        assert view.semantic_track_button.isHidden()
        assert view.review_status.isHidden()
    asyncio.run(scenario())


def test_long_manuscript_without_semantic_shows_neutral_segment_progress(qapp) -> None:
    """The usual production state: segments are reviewed for terms only, and
    the progress says so without naming a semantic review that never ran."""
    source = ("第一段。" * 100 + "\n\n" + "第二段。" * 100 + "\n\n" + "第三段。" * 100) * 2
    target = ("One. " * 100 + "\n\n" + "Two. " * 100 + "\n\n" + "Three. " * 100) * 2

    async def scenario():
        server = Server(capabilities=("translation-context-v1",))
        view = make_view(server, source, target)
        view._start_review()
        await settle(view)
        assert "/v1/semantic-reviews" not in server.paths()
        done, total = view._progress()
        assert done == total >= 2
        assert view.semantic_progress.text() == strings.MANUSCRIPT_SEGMENTS_PROGRESS.format(done=done, total=total)
        assert not view.review_status.isHidden() and view.semantic_track_button.isHidden()
    asyncio.run(scenario())


def test_semantic_track_appears_with_the_capability_and_says_it_is_experimental(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        assert view.semantic_track_button.isHidden()
        view._start_review()
        await settle(view)
        assert not view.semantic_track_button.isHidden()
        assert view.semantic_badge.text() == strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL
        assert strings.MANUSCRIPT_SEMANTIC_EXPERIMENTAL in view.semantic_progress.text()
    asyncio.run(scenario())


def test_tracks_stay_apart_and_issues_express_uncertainty(qapp) -> None:
    async def scenario():
        server = Server(issues=two_issues)
        view = make_view(server, SOURCE, TARGET)
        view._start_review()
        await settle(view)
        assert server.paths() == ["/v1/capabilities", "/v1/reviews", "/v1/semantic-reviews"]
        request = server.sent("/v1/semantic-reviews")[0]
        assert (request["source"], request["target"], request["precedents"]) == (SOURCE, TARGET, [])
        assert "context" not in request
        # Term constraints and model issues are counted and listed separately.
        assert view.terms_track_button.text() == strings.MANUSCRIPT_TRACK_TERMS.format(count=0)
        assert view.semantic_track_button.text() == strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=2)
        view._show_track(TRACK_SEMANTIC)
        assert view.semantic_list.count() == 2
        view.semantic_list.setCurrentRow(0)
        assert view.issue_title.text() == strings.MANUSCRIPT_CATEGORY["quantity_change"]
        assert view.issue_confidence.text() == strings.MANUSCRIPT_CONFIDENCE["high"]
        assert strings.MANUSCRIPT_SEMANTIC_NOTE in view.semantic_note.text()
        assert view.issue_rationale.textFormat() == Qt.TextFormat.PlainText
        assert view.target_edit.textCursor().selectedText() == "second day"
        [snapshot] = view._payload["semantic_history"]
        assert (snapshot["source"], snapshot["target"]) == (SOURCE, TARGET)
        assert snapshot["segments"][0]["report"]["request_id"] == "semantic"
    asyncio.run(scenario())


def test_accept_advances_and_undo_redo_restore_text_and_decision(qapp) -> None:
    async def scenario():
        server = Server(issues=two_issues)
        view = make_view(server, SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        view.issue_alternative_button.setChecked(True)
        assert strings.MANUSCRIPT_TERM_EFFECT["alters_official_match"] == view.issue_term_effect.text()
        view.issue_alternative_button.setChecked(False)
        view.accept_button.click()
        accepted = TARGET.replace("second", "third")
        assert view._payload["target"] == accepted
        [decision] = view._payload["semantic_decisions"]
        assert (decision["decision"], decision["applied_text"]) == ("accepted", "third")
        assert decision["target_revision"] == digest(TARGET)
        # Automatically on to the next issue still waiting for a decision.
        assert view.semantic_list.currentRow() == 1
        assert view.target_edit.textCursor().selectedText() == "will leave"
        review_state = view._review_state
        view.ignore_button.click()
        assert [d["decision"] for d in view._payload["semantic_decisions"]] == ["accepted", "ignored"]
        assert view._review_state == review_state, "ignoring changes no text and leaves the term report alone"
        view._undo_replace()
        view._undo_replace()
        assert view._payload["target"] == TARGET and view._payload["semantic_decisions"] == []
        states = [view._issue_state(i) for i in view._issue_rows]
        assert states == ["pending", "pending"]
        view._redo_edit()
        assert view._payload["target"] == accepted
        assert [view._issue_state(i) for i in view._issue_rows] == ["accepted", "pending"]
    asyncio.run(scenario())


def test_withheld_suggestion_offers_only_adjust_and_ignore(qapp) -> None:
    source = "原文" + "长" * 300
    target = "Start " + "x" * 300 + " end."

    def issues(s, t):
        return [issue(s, t, category="meaning_shift", source_quote="长" * 300, target_quote="x" * 300,
                      replace="x" * 300, replacement="y", withheld="span_too_long")]

    async def scenario():
        view = make_view(Server(issues=issues), source, target)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        assert view.accept_button.isHidden()
        assert strings.MANUSCRIPT_ISSUE_WITHHELD in view.issue_notice.text()
        assert view.adjust_button.isEnabled() and view.ignore_button.isEnabled()
        view.adjust_button.click()
        assert view.adjust_edit.toPlainText() == "x" * 300
        view.adjust_edit.setPlainText("rewritten")
        view.adjust_apply_button.click()
        assert view._payload["target"] == "Start rewritten end."
        assert view._payload["semantic_decisions"][0]["decision"] == "adjusted"
    asyncio.run(scenario())


def test_a_truncated_span_is_never_executed(qapp) -> None:
    """Plan §2.3 probe: a 300-scalar range whose text was cut to 200, plus a
    240-scalar replacement, must be refused instead of leaving 100 old scalars."""
    head = "Lead "
    old = "".join(chr(0x4E00 + i) for i in range(300))
    target = head + old + " tail"
    replacement = "新" * 240

    async def scenario():
        # Layer 1: such a report never reaches the panel.
        def truncated(s, t):
            item = issue(s, t, source_quote=s[:3], target_quote=old[:200], replace=old[:200], replacement=replacement)
            for key in ("target_span",):
                item[key]["end"] = item[key]["start"] + 300
            item["suggestion"]["target_span"]["end"] = item["suggestion"]["target_span"]["start"] + 300
            return [item]
        server = Server(issues=truncated)
        view = make_view(server, "原文三个字", target)
        view._start_review()
        await settle(view)
        assert view._session.issues() == []
        assert view._segment_failures, "the malformed report is reported as a failed segment"

        # Layer 2: an in-memory issue whose recorded text no longer matches.
        def honest(s, t):
            return [issue(s, t, source_quote=s[:3], target_quote=old[:200], replace=old[:200], replacement=replacement)]
        server = Server(issues=honest)
        view = make_view(server, "原文三个字", target)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        [live] = view._session.issues()
        live.suggestion_span = {"start": len(head), "end": len(head) + 300, "text": old[:200]}
        view._render_issue(live)
        assert not view.accept_button.isEnabled()
        view.target_edit.setFocus()
        QTest.keyClick(view.target_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        view._accept_current()
        assert view._payload["target"] == target
        assert len(view._payload["target"]) != len(target) + 40
        with pytest.raises(semantic.StaleSpan):
            semantic.apply_span(target, live.suggestion_span, replacement)
        assert view._payload["semantic_decisions"] == []
    asyncio.run(scenario())


def test_editing_inside_an_issue_needs_reconfirmation_and_shifts_later_issues(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        cursor = view.target_edit.textCursor()
        cursor.setPosition(0)
        view.target_edit.setTextCursor(cursor)
        view.target_edit.insertPlainText("So, ")
        first, second = view._issue_rows
        assert first.target_span["start"] == TARGET.index("second day") + 4
        assert view._issue_state(first) == "pending" and view._issue_state(second) == "pending"
        start = view._payload["target"].index("will")
        cursor.setPosition(start + 1)
        view.target_edit.setTextCursor(cursor)
        view.target_edit.insertPlainText("!")
        assert view._issue_state(second) == "stale"
        view.semantic_list.setCurrentRow(1)
        assert not view.accept_button.isEnabled() and not view.adjust_button.isEnabled()
        assert strings.MANUSCRIPT_ISSUE_STALE in view.issue_notice.text()
        assert view.ignore_button.isEnabled()
    asyncio.run(scenario())


def test_shortcuts_drive_the_issue_flow_without_editing_the_editor(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view.show()
        try:
            view.target_edit.setFocus()
            qapp.processEvents()
            QTest.keyClick(view.target_edit, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            await settle(view)
            assert view._session.issues()
            QTest.keyClick(view.target_edit, Qt.Key.Key_F8)
            assert view.semantic_list.currentRow() == 0
            view.target_edit.setFocus()
            QTest.keyClick(view.target_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            assert view._payload["target"] == TARGET.replace("second", "third"), "no newline, one replacement"
            QTest.keyClick(view.target_edit, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            assert [d["decision"] for d in view._payload["semantic_decisions"]] == ["accepted", "ignored"]
            QTest.keyClick(view.target_edit, Qt.Key.Key_F8, Qt.KeyboardModifier.ShiftModifier)
            assert strings.MANUSCRIPT_ALL_HANDLED in view.status_label.text()
        finally:
            view.close()
    asyncio.run(scenario())


def test_decisions_persist_in_v2_and_re_review_applies_the_merge_rules(qapp, tmp_path, monkeypatch) -> None:
    path = tmp_path / "draft.json"

    async def review_reopened(source=SOURCE, target=TARGET) -> tuple[ManuscriptView, Server]:
        server = Server(issues=two_issues)
        view = make_view(server, source, target)
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
        view.open_document()
        view._start_review()
        await settle(view)
        assert "/v1/semantic-reviews" in server.paths()
        return view, server

    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        view._path = path
        assert view.save_document()
        assert json.loads(path.read_text(encoding="utf-8"))["format"] == WORKFILE_FORMAT_V2

        # Same texts and spans, one issue with that fingerprint: the ignore carries over.
        reopened, _ = await review_reopened()
        assert [reopened._issue_state(i) for i in reopened._issue_rows] == ["pending", "ignored_carried"]

        # An edit on another line: the saved old text lets the ignore recover.
        cursor = reopened.target_edit.textCursor()
        cursor.setPosition(0)
        reopened.target_edit.setTextCursor(cursor)
        reopened.target_edit.insertPlainText("Intro.\n")
        assert reopened.save_document()
        saved = load_workfile(path)
        assert [t["revision"] for t in saved["semantic_texts"]] == [digest(TARGET)]
        edited, _ = await review_reopened()
        assert [edited._issue_state(i) for i in edited._issue_rows] == ["pending", "ignored_carried"]

        # A change inside the reported line: the old decision is only a reference.
        loaded = load_workfile(path)
        loaded["target"] = "Intro.\n" + TARGET + " Then they rest."
        from wuwaterm_client.manuscript import save_workfile
        save_workfile(path, loaded)
        changed, _ = await review_reopened()
        second = changed._issue_rows[1]
        assert changed._issue_state(second) == "needs_reconfirm" and second.previous["decision"] == "ignored"
    asyncio.run(scenario())


def _fixture_case(core, name):
    data = json.loads((core / "contracts" / "segmentation-v1.json").read_text(encoding="utf-8"))
    return next(case for case in data["pair_cases"] if case["name"] == name)


@pytest.mark.parametrize("name,ready", [
    ("asymmetric_source_over_limit_no_boundary", 0),
    ("asymmetric_target_over_limit_no_boundary", 0),
    ("asymmetric_source_over_limit_with_commas", 2),
    ("asymmetric_target_over_limit_with_commas", 2),
])
def test_long_asymmetric_pairs_never_send_an_oversized_side(qapp, contract_source, name, ready) -> None:
    case = _fixture_case(contract_source("core"), name)

    async def scenario():
        server = Server()
        view = make_view(server, case["source"], case["target"])
        view._start_review()
        await settle(view)
        semantic_requests = server.sent("/v1/semantic-reviews")
        term_requests = server.sent("/v1/reviews")
        assert len(semantic_requests) == len(term_requests) == ready
        for body in semantic_requests + term_requests:
            assert len(body["source"]) <= SEGMENT_LIMIT and len(body["target"]) <= SEGMENT_LIMIT
        if ready:
            joined = "".join(b["source"] for b in semantic_requests)
            assert joined == case["source"].strip()
        view._show_track(TRACK_SEGMENTS)
        statuses = [view._segment_status(p) for p in view._segment_rows]
        if not ready:
            assert statuses == ["needs_manual_split"]
            view.segment_list.setCurrentRow(0)
            assert strings.MANUSCRIPT_SEGMENT_HINT["needs_manual_split"] in view.segment_hint.text()
        else:
            assert statuses == ["reviewed", "reviewed"]
            view.segment_list.setCurrentRow(0)
            assert strings.MANUSCRIPT_SEGMENT_HINT["hard_cut"] in view.segment_hint.text()
    asyncio.run(scenario())


def test_later_segment_issue_is_located_through_utf16_and_crlf(qapp) -> None:
    first_source = ("🌊句子。\r\n" * 200).rstrip()
    first_target = ("😀sentence.\r\n" * 200).rstrip()
    source = first_source + "\r\n\r\n" + "第二段不会出发。"
    target = first_target + "\r\n\r\n" + "Part two will leave."

    def issues(s, t):
        if "will leave" not in t:
            return []
        return [issue(s, t, source_quote="不会出发", target_quote="will leave", replace="will leave",
                      replacement="will not leave")]

    async def scenario():
        server = Server(issues=issues)
        view = make_view(server, source, target)
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/semantic-reviews")) == 2
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        [live] = view._issue_rows
        assert live.target_span["start"] == target.index("will leave")
        cursor = view.target_edit.textCursor()
        assert cursor.selectedText() == "will leave"
        assert cursor.selectionStart() == len(target[:target.index("will leave")].replace("\r\n", "\n").encode("utf-16-le")) // 2
        view.accept_button.click()
        assert view._payload["target"].endswith("Part two will not leave.")
        assert view._payload["target"].startswith(first_target)
    asyncio.run(scenario())


def test_quota_pauses_with_progress_and_continue_skips_done_segments(qapp) -> None:
    source = "第一段。" * 100 + "\n\n" + "第二段。" * 100 + "\n\n" + "第三段。" * 100
    target = "One. " * 100 + "\n\n" + "Two. " * 100 + "\n\n" + "Three. " * 100
    source, target = source * 2, target * 2  # long enough to be cut into several pairs

    async def scenario():
        server = Server()
        server.semantic_errors = [httpx.Response(429, json={"error": {"code": "rate_limited", "message": "x"},
                                                            "request_id": "q"})]
        view = make_view(server, source, target)
        view._start_review()
        await settle(view)
        done, total = view._progress()
        assert view._semantic_state == "quota" and done == 0 and total >= 2
        assert view.semantic_progress.text() == strings.MANUSCRIPT_SEMANTIC_QUOTA.format(done=0, total=total)
        assert view.review_button.text() == strings.MANUSCRIPT_REVIEW_CONTINUE
        view._start_review()
        await settle(view)
        assert view._progress() == (total, total)
        first_run = len(server.sent("/v1/semantic-reviews"))
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/semantic-reviews")) == first_run, "unchanged segments are not sent again"
    asyncio.run(scenario())


def test_a_segment_edited_while_its_request_is_in_flight_is_reviewed_again(qapp) -> None:
    gate = asyncio.Event()

    async def scenario():
        server = Server(issues=two_issues)
        seen = []

        async def gated(request):
            if request.url.path == "/v1/semantic-reviews":
                seen.append(json.loads(request.content)["target"])
                if len(seen) == 1:
                    await gate.wait()
            return server(request)
        client = ApiClient("https://example.invalid", token_provider=lambda: "test-token",
                           _test_transport=httpx.MockTransport(gated))
        view = ManuscriptView(client)
        value = empty_workfile()
        value.update(source=SOURCE, target=TARGET, direction="en")
        view._set_payload(value)
        view._update_controls()
        view._start_review()
        while not seen:
            await asyncio.sleep(0)
        cursor = view.target_edit.textCursor()
        cursor.setPosition(len(TARGET))
        view.target_edit.setTextCursor(cursor)
        view.target_edit.insertPlainText(" Done")
        assert view._semantic_running(), "an edit does not cancel the model run"
        gate.set()
        await settle(view)
        assert seen == [TARGET, TARGET + " Done"]
        assert all(i.target_span is None or view._payload["target"][i.target_span["start"]:i.target_span["end"]]
                   == i.target_span["text"] for i in view._session.issues())
    asyncio.run(scenario())


def test_translation_fills_only_a_blank_target_and_keeps_separators(qapp) -> None:
    source = "  First paragraph.\n\nSecond paragraph.\n"

    async def scenario():
        server = Server(capabilities=("translation-context-v1",))
        view = make_view(server, source, "   ", direction="zh")
        view._translate()
        await settle(view)
        # Both paragraphs fit one segment; the separators around it are copied.
        assert view._payload["target"] == "  [First paragraph.\n\nSecond paragraph.]\n"
        sent = server.sent("/v1/translations")
        assert [b["text"] for b in sent] == ["First paragraph.\n\nSecond paragraph."]
        view._undo_replace()
        assert view._payload["target"] == "   "

        # Existing text is never overwritten.
        server = Server()
        view = make_view(server, source, "已有译文")
        view._translate()
        await settle(view)
        assert server.sent("/v1/translations") == []
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK
    asyncio.run(scenario())


def test_translation_sends_context_only_when_the_server_lists_it(qapp) -> None:
    source = ("第一句。" * 300) + "\n\n" + ("第二句。" * 300)

    async def scenario():
        for capabilities, expect_context in ((("translation-context-v1",), True), ((), False)):
            server = Server(capabilities=capabilities)
            view = make_view(server, source, "", direction="en")
            view._translate()
            await settle(view)
            sent = server.sent("/v1/translations")
            assert len(sent) == 2 and "context" not in sent[0]
            assert ("context" in sent[1]) is expect_context
            if expect_context:
                assert set(sent[1]["context"]) == {"before"}, "no precedents until the M′ gates pass"
                assert len(sent[1]["context"]["before"]["source"]) <= 400
    asyncio.run(scenario())


LONG_SOURCE = "\n\n".join(["第一段。" * 300, "第二段。" * 300, "第三段。" * 300])
LONG_TARGET = "\n\n".join(["One. " * 300, "Two. " * 300, "Three. " * 300])


def test_reviewing_an_unchanged_long_manuscript_again_keeps_its_term_results(qapp) -> None:
    async def scenario():
        server = Server(capabilities=None)
        view = make_view(server, LONG_SOURCE, LONG_TARGET)
        view._start_review()
        await settle(view)
        sent = len(server.sent("/v1/reviews"))
        ready = [pair for pair in view._current_pairs() if pair.status == "ready"]
        assert sent == len(ready) > 1 and view._review_state == "current"
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/reviews")) == sent, "unchanged segments are not sent again"
        assert view._review_state == "current" and view._report is not None
    asyncio.run(scenario())


def test_the_same_service_failure_on_consecutive_segments_stops_the_run(qapp) -> None:
    async def scenario():
        server = Server()
        failure = httpx.Response(500, json={"error": {"code": "internal", "message": "x"}, "request_id": "e"})
        server.semantic_errors = [failure, failure, failure]
        view = make_view(server, LONG_SOURCE, LONG_TARGET)
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/semantic-reviews")) == 2
        assert view._semantic_state == "error"
        assert view.semantic_progress.text() == view._semantic_detail
    asyncio.run(scenario())


# -- occurrence identity and edit invalidation (review of a33f8b4) ---------
# Identical paragraphs produce identical request-local ids and texts, so
# identity must be the whole-document position plus the text version, and an
# edit's range must come from the edit itself rather than a text difference.

REPEATED_SOURCE = "\n\n".join(["不会出发。" * 220] * 3)
REPEATED_TARGET = "\n\n".join(["will leave. " * 110] * 3)


def repeated_issues(s, t):
    return [issue(s, t, source_quote="不会", target_quote="will leave", replace="will leave",
                  replacement="will not leave")]


def test_identical_paragraphs_are_each_reviewed_once(qapp) -> None:
    async def scenario():
        for capabilities in (("semantic-review-v1",), None):
            server = Server(capabilities=capabilities, issues=repeated_issues)
            view = make_view(server, REPEATED_SOURCE, REPEATED_TARGET)
            assert len([p for p in view._current_pairs() if p.status == "ready"]) == 3
            view._start_review()
            await settle(view)
            assert view._progress() == (3, 3)
            path = "/v1/semantic-reviews" if capabilities else "/v1/reviews"
            assert len(server.sent(path)) == 3
    asyncio.run(scenario())


def test_deciding_one_identical_occurrence_leaves_the_others_pending(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=repeated_issues), REPEATED_SOURCE, REPEATED_TARGET)
        view._start_review()
        await settle(view)
        assert len({i.id for i in view._session.issues()}) == 1, "same request-local id in every paragraph"
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        view.ignore_button.click()
        assert [view._issue_state(i) for i in view._issue_rows] == ["ignored", "pending", "pending"]
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        assert len(view._decisions()) == 2, "two occurrences, two decisions"
    asyncio.run(scenario())


def test_an_ignore_on_one_identical_paragraph_never_carries_to_another(qapp, tmp_path, monkeypatch) -> None:
    path = tmp_path / "repeated.json"

    async def scenario():
        view = make_view(Server(issues=repeated_issues), REPEATED_SOURCE, REPEATED_TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        view._path = path
        assert view.save_document()
        reopened = make_view(Server(issues=repeated_issues), "", "")
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
        reopened.open_document()
        reopened._start_review()
        await settle(reopened)
        # Three candidates for one decision: nothing carries over (Web rule).
        assert [reopened._issue_state(i) for i in reopened._issue_rows] == ["needs_reconfirm"] * 3
    asyncio.run(scenario())


def test_deleting_a_reviewed_line_does_not_retarget_its_twin(qapp) -> None:
    async def scenario():
        target = "will leave\nwill leave"
        view = make_view(Server(issues=lambda s, t: [issue(
            s, t, source_quote="不会", target_quote="will leave", replace="will leave", replacement="will not leave")]),
            "甲不会出发。乙会出发。", target)
        view._start_review()
        await settle(view)
        cursor = view.target_edit.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(len("will leave\n"), QTextCursor.MoveMode.KeepAnchor)
        view.target_edit.setTextCursor(cursor)
        cursor.removeSelectedText()
        assert view._payload["target"] == "will leave"
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        assert view._issue_state(view._issue_rows[0]) == "stale"
        assert not view.accept_button.isEnabled()
        view._accept_current()
        assert view._payload["target"] == "will leave"
    asyncio.run(scenario())


def test_rewriting_an_issue_with_identical_text_still_needs_reconfirmation(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view._start_review()
        await settle(view)
        start = TARGET.index("will leave")
        cursor = view.target_edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(start + len("will leave"), QTextCursor.MoveMode.KeepAnchor)
        view.target_edit.setTextCursor(cursor)
        mime = QMimeData()
        mime.setText("will leave")
        view.target_edit.insertFromMimeData(mime)
        assert view._payload["target"] == TARGET
        assert [view._issue_state(i) for i in view._issue_rows] == ["pending", "stale"]
    asyncio.run(scenario())


def test_editing_inside_an_ignored_issue_needs_reconfirmation_and_undo_restores_it(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        cursor = view.target_edit.textCursor()
        cursor.setPosition(TARGET.index("will") + 1)
        view.target_edit.setTextCursor(cursor)
        view.target_edit.insertPlainText("!")
        second = view._issue_rows[1]
        assert view._issue_state(second) == "stale"
        assert [d["decision"] for d in view._decisions()] == ["ignored"], "the decision stays as history"
        view.semantic_list.setCurrentRow(1)
        assert not view.ignore_button.isEnabled() and not view.accept_button.isEnabled()
        view._undo_replace()
        assert view._payload["target"] == TARGET
        assert view._issue_state(second) == "ignored"
    asyncio.run(scenario())


def test_saving_again_after_history_was_trimmed_away_keeps_memory_and_file_in_step(qapp, tmp_path) -> None:
    async def scenario():
        source = "\n\n".join(["甲。" * 550] * 125)
        target = "\n\n".join(["One. " * 220] * 125)
        view = make_view(Server(), source, target)
        view._start_review()
        await settle(view)
        assert view._payload["semantic_history"][0]["segments"]
        view._path = tmp_path / "trimmed.json"
        assert view.save_document()
        written = load_workfile(view._path)
        # Web's writer drops history to fit 1 MiB and still writes v2.
        assert written["format"] == "wuwaterm-manuscript-v2" and written["semantic_history"] == []
        assert view._payload["semantic_history"] == [], "memory holds what was written"
        assert not view.document_dirty
        assert view.save_document()
        assert load_workfile(view._path)["source"] == source
    asyncio.run(scenario())


def test_a_failed_semantic_segment_keeps_its_term_results_and_retries_only_the_model(qapp) -> None:
    async def scenario():
        server = Server()
        server.semantic_errors = [httpx.Response(500, json={"error": {"code": "internal", "message": "x"},
                                                            "request_id": "e"})]
        view = make_view(server, LONG_SOURCE, LONG_TARGET)
        view._start_review()
        await settle(view)
        ready = [p for p in view._current_pairs() if p.status == "ready"]
        terms = len(server.sent("/v1/reviews"))
        assert terms == len(ready), "every segment's term review was kept"
        assert view._review_state == "current"
        assert view._progress() == (len(ready) - 1, len(ready))
        semantic_before = len(server.sent("/v1/semantic-reviews"))
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/reviews")) == terms, "the term track is not requested again"
        assert len(server.sent("/v1/semantic-reviews")) == semantic_before + 1
        assert view._progress() == (len(ready), len(ready))
    asyncio.run(scenario())


def test_re_reviewing_an_edited_segment_replaces_its_old_issues(qapp) -> None:
    def issues(s, t):
        return [issue(s, t, source_quote=s[:2], target_quote=t[:3], seed=t[:4])]

    async def scenario():
        view = make_view(Server(issues=issues), LONG_SOURCE, LONG_TARGET)
        view._start_review()
        await settle(view)
        count = len(view._session.issues())
        cursor = view.source_edit.textCursor()
        cursor.setPosition(LONG_SOURCE.index("第二段") + 1)
        view.source_edit.setTextCursor(cursor)
        view.source_edit.insertPlainText("加长" * 5)
        assert sum(view._issue_state(i) == "stale" for i in view._session.issues()) == 1
        view._start_review()
        await settle(view)
        assert len(view._session.issues()) == count
        assert all(view._issue_state(i) == "pending" for i in view._session.issues())
    asyncio.run(scenario())


def test_segmented_term_review_keeps_the_owners_alignment_choice(qapp) -> None:
    async def scenario():
        for alignments, expected in ((None, None), ([], [])):
            server = Server(capabilities=None)
            view = make_view(server, LONG_SOURCE, LONG_TARGET)
            view._payload["alignments"] = alignments
            view._start_review()
            await settle(view)
            sent = server.sent("/v1/reviews")
            assert sent and all(body.get("alignments") == expected for body in sent)
            assert all(("alignments" in body) is (expected is not None) for body in sent)
    asyncio.run(scenario())


def test_growing_into_segmented_review_requests_every_missing_term_report(qapp) -> None:
    first, second = "第一段。" * 300, "第二段。" * 175
    first_t, second_t = "One. " * 200, "Two. " * 150  # both sides within 2,000: document review first

    async def scenario():
        server = Server()
        view = make_view(server, f"{first}\n\n{second}", f"{first_t}\n\n{second_t}")
        view._start_review()
        await settle(view)
        assert view._term_mode == "document" and len(server.sent("/v1/semantic-reviews")) == 2
        cursor = view.source_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        view.source_edit.setTextCursor(cursor)
        view.source_edit.insertPlainText("\n\n" + "第三段。" * 450)  # too long to merge
        cursor = view.target_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        view.target_edit.setTextCursor(cursor)
        view.target_edit.insertPlainText("\n\n" + "Three. " * 250)
        view._start_review()
        await settle(view)
        ready = [p for p in view._current_pairs() if p.status == "ready"]
        assert view._term_mode == "segments"
        assert len(server.sent("/v1/reviews")) == 1 + len(ready), "every segment gets a term report"
        assert len(server.sent("/v1/semantic-reviews")) == 2 + 1, "only the new segment goes to the model"
        assert view._progress() == (len(ready), len(ready))
    asyncio.run(scenario())


def test_choosing_automatic_alignment_redoes_cached_segment_term_reviews(qapp) -> None:
    async def scenario():
        server = Server()
        view = make_view(server, LONG_SOURCE, LONG_TARGET)
        view._payload["alignments"] = []
        view._start_review()
        await settle(view)
        terms, models = len(server.sent("/v1/reviews")), len(server.sent("/v1/semantic-reviews"))
        assert all(body["alignments"] == [] for body in server.sent("/v1/reviews"))
        view._use_auto_alignment()
        assert view._payload["alignments"] is None
        view._start_review()
        await settle(view)
        again = server.sent("/v1/reviews")[terms:]
        assert len(again) == terms and all("alignments" not in body for body in again)
        assert len(server.sent("/v1/semantic-reviews")) == models, "the model results are kept"
    asyncio.run(scenario())


# -- the segment reuse rule (sessions.Basis) --------------------------------


def test_an_edit_that_merges_two_segments_leaves_no_duplicate_issues(qapp) -> None:
    first, second = "甲" * 1200, "乙" * 700
    source = f"{first}\n\n{second}"
    target = "\n\n".join(["One. " * 120, "Two. " * 120])

    def issues(s, t):
        return [issue(s, t, source_quote=s[:2], target_quote=t[:3], seed=s[:1] + t[:4])]

    async def scenario():
        server = Server(issues=issues)
        view = make_view(server, source, target)
        view._start_review()
        await settle(view)
        assert len(view._current_pairs()) == 2 and len(view._session.issues()) == 2
        cursor = view.source_edit.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(300, QTextCursor.MoveMode.KeepAnchor)
        view.source_edit.setTextCursor(cursor)
        cursor.removeSelectedText()
        assert [p.status for p in view._current_pairs()] == ["ready"], "the edit merged the pairs"
        view._start_review()
        await settle(view)
        assert len(view._session.results) == 1
        assert len(view._session.issues()) == 1
        assert [view._issue_state(i) for i in view._issue_rows] == ["pending"]
    asyncio.run(scenario())


def test_term_only_segments_get_a_semantic_request_once_the_server_offers_it(qapp) -> None:
    async def scenario():
        server = Server(capabilities=None)
        view = make_view(server, LONG_SOURCE, LONG_TARGET)
        view._start_review()
        await settle(view)
        terms = len(server.sent("/v1/reviews"))
        ready = [p for p in view._current_pairs() if p.status == "ready"]
        assert server.sent("/v1/semantic-reviews") == [] and view._progress() == (len(ready), len(ready))
        server.capabilities = ("semantic-review-v1",)
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/semantic-reviews")) == len(ready)
        assert len(server.sent("/v1/reviews")) == terms, "valid term reports are reused"
        assert view._progress() == (len(ready), len(ready))
    asyncio.run(scenario())


def test_an_ignore_survives_reopening_and_an_unrelated_edit_before_the_next_review(qapp, tmp_path, monkeypatch) -> None:
    path = tmp_path / "draft.json"

    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        view._path = path
        assert view.save_document()
        assert load_workfile(path)["semantic_texts"] == [], "nothing old is needed at save time"
        reopened = make_view(Server(issues=two_issues), "", "")
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
        reopened.open_document()
        cursor = reopened.target_edit.textCursor()
        cursor.setPosition(0)
        reopened.target_edit.setTextCursor(cursor)
        reopened.target_edit.insertPlainText("Intro.\n")
        reopened._start_review()
        await settle(reopened)
        assert [reopened._issue_state(i) for i in reopened._issue_rows] == ["pending", "ignored_carried"]
    asyncio.run(scenario())


# -- coordinate rule: compare at current positions, identify by original ----


def _reopen(path, server, monkeypatch) -> ManuscriptView:
    view = make_view(server, "", "")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    view.open_document()
    return view


def _prepend(editor, text: str) -> None:
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)
    editor.insertPlainText(text)


def test_a_carried_ignore_survives_an_earlier_edit_and_a_re_review(qapp, tmp_path, monkeypatch) -> None:
    source = "甲" * 1200 + "\n\n" + "乙不会出发。" + "乙" * 900
    target = "A" * 1200 + "\n\nHe will leave. " + "B" * 1000

    def issues(s, t):
        return [issue(s, t, source_quote="不会", target_quote="will leave", replace="will leave",
                      replacement="will not leave")] if "不会" in s else []

    async def scenario():
        view = make_view(Server(issues=issues), source, target)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        view.ignore_button.click()
        view._path = tmp_path / "ignore.json"
        assert view.save_document()
        server = Server(issues=issues)
        reopened = _reopen(view._path, server, monkeypatch)
        reopened._start_review()
        await settle(reopened)
        [live] = reopened._session.issues()
        assert reopened._issue_state(live) == "ignored_carried"
        sent = len(server.sent("/v1/semantic-reviews"))
        _prepend(reopened.source_edit, "新")
        reopened._start_review()
        await settle(reopened)
        assert len(server.sent("/v1/semantic-reviews")) == sent + 1, "only the edited segment"
        assert live.source_span["start"] == live.issue["source_span"]["start"] + 1
        assert reopened._issue_state(live) == "ignored_carried"
    asyncio.run(scenario())


def test_an_ignore_never_carries_to_another_occurrence_through_original_coordinates(qapp, tmp_path, monkeypatch) -> None:
    a_s, b_s = "甲不会出发。".ljust(1200, "甲"), "乙不会出发。".ljust(1200, "乙")
    a_t, b_t = "A will leave. ".ljust(1200, "A"), "B will leave. ".ljust(1200, "B")
    source, target = a_s + "\n\n" + b_s, a_t + "\n\n" + b_t

    def issue_for(s, t):
        reported = issue(s, t, source_quote="不会", target_quote="will leave", replace="will leave",
                         replacement="will not leave")
        reported["fingerprint"] = "e" * 32  # A and B share a fingerprint
        return [reported]

    async def scenario():
        view = make_view(Server(issues=lambda s, t: issue_for(s, t) if s.startswith("甲") else []), source, target)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        view.ignore_button.click()  # A only
        view._path = tmp_path / "ignore-A.json"
        assert view.save_document()
        # The next run reports only B.
        server = Server(issues=lambda s, t: issue_for(s, t) if s.startswith("乙") else [])
        reopened = _reopen(view._path, server, monkeypatch)
        reopened._start_review()
        await settle(reopened)
        [live] = reopened._session.issues()
        assert reopened._issue_state(live) == "needs_reconfirm"
        # After a prepended paragraph, A's recovered place equals B's original
        # coordinates; comparing at current positions keeps B unhandled.
        _prepend(reopened.source_edit, "新" * 1200 + "\n\n")
        _prepend(reopened.target_edit, "C" * 1200 + "\n\n")
        reopened._start_review()
        await settle(reopened)
        assert len(reopened._current_pairs()) == 3 and len(server.sent("/v1/semantic-reviews")) == 3
        assert reopened._issue_state(live) == "needs_reconfirm"
        assert reopened.semantic_track_button.text() == strings.MANUSCRIPT_TRACK_SEMANTIC.format(count=1)
    asyncio.run(scenario())


# -- explicit alignments in segment term requests ---------------------------

ALIGNED_SOURCE = "  " + "甲" * 1200 + "\n\n" + "乙" * 1200 + "\n"
ALIGNED_TARGET = " " + "A" * 1200 + "\n\n" + "B" * 1200 + "\n"


def _span(text: str, start: int, end: int) -> dict:
    return {"start": start, "end": end, "text": text[start:end]}


def test_alignments_with_surrounding_whitespace_reach_the_segment_term_requests(qapp) -> None:
    alignments = [{"source": _span(ALIGNED_SOURCE, 0, 1204), "target": _span(ALIGNED_TARGET, 0, 1203)},
                  {"source": _span(ALIGNED_SOURCE, 1204, len(ALIGNED_SOURCE)),
                   "target": _span(ALIGNED_TARGET, 1203, len(ALIGNED_TARGET))}]

    async def scenario():
        server = Server(capabilities=None)
        view = make_view(server, ALIGNED_SOURCE, ALIGNED_TARGET)
        view._payload["alignments"] = alignments
        view._start_review()
        await settle(view)
        sent = server.sent("/v1/reviews")
        assert [p.status for p in view._current_pairs()] == ["ready", "ready"] and len(sent) == 2
        for body in sent:
            [item] = body["alignments"]
            assert item["source"] == _span(body["source"], 0, 1200)
            assert item["target"] == _span(body["target"], 0, 1200)
    asyncio.run(scenario())


def test_alignment_spans_are_clipped_to_the_core_or_left_out(qapp) -> None:
    view = make_view(Server(), ALIGNED_SOURCE, ALIGNED_TARGET)
    first, second = ((2, 1202), (1, 1201)), ((1204, 2404), (1203, 2403))
    # Crossing the core boundary: the part inside each core is kept.
    view._payload["alignments"] = [{"source": _span(ALIGNED_SOURCE, 1000, 1500),
                                    "target": _span(ALIGNED_TARGET, 1100, 1300)}]
    assert view._alignments_for(*first) == [{"source": _span(ALIGNED_SOURCE[2:1202], 998, 1200),
                                             "target": _span(ALIGNED_TARGET[1:1201], 1099, 1200)}]
    assert view._alignments_for(*second) == [{"source": _span(ALIGNED_SOURCE[1204:2404], 0, 296),
                                              "target": _span(ALIGNED_TARGET[1203:2403], 0, 97)}]
    # Entirely outside one side's core: the whole alignment is left out there.
    view._payload["alignments"] = [{"source": _span(ALIGNED_SOURCE, 1300, 1400),
                                    "target": _span(ALIGNED_TARGET, 100, 200)},
                                   {"source": _span(ALIGNED_SOURCE, 1500, 1600), "target": None}]
    assert view._alignments_for(*first) == []
    assert view._alignments_for(*second) == [{"source": _span(ALIGNED_SOURCE[1204:2404], 296, 396),
                                              "target": None}]


def test_a_workfile_with_float_alignment_offsets_reviews_in_segments(qapp, tmp_path, monkeypatch) -> None:
    path = tmp_path / "floats.json"
    cuts_s, cuts_t = (0, 600, 1204, len(ALIGNED_SOURCE)), (0, 600, 1203, len(ALIGNED_TARGET))

    def floating(text, a, b):
        return {**_span(text, a, b), "start": float(a), "end": float(b)}
    draft = empty_workfile()
    draft.update(source=ALIGNED_SOURCE, target=ALIGNED_TARGET, alignments=[
        {"source": floating(ALIGNED_SOURCE, cuts_s[i], cuts_s[i + 1]),
         "target": floating(ALIGNED_TARGET, cuts_t[i], cuts_t[i + 1])} for i in range(3)])
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    async def scenario():
        server = Server(capabilities=None)
        view = _reopen(path, server, monkeypatch)
        assert type(view._payload["alignments"][0]["source"]["end"]) is int
        view._start_review()
        await settle(view)
        assert view._semantic_state == "done"
        sent = server.sent("/v1/reviews")
        assert len(sent) == 2
        core_s, core_t = ALIGNED_SOURCE[2:1202], ALIGNED_TARGET[1:1201]
        assert sent[0]["alignments"] == [
            {"source": _span(core_s, 0, 598), "target": _span(core_t, 0, 599)},
            {"source": _span(core_s, 598, 1200), "target": _span(core_t, 599, 1200)}]
    asyncio.run(scenario())


def test_withdrawn_semantic_capability_leaves_old_issues_stale_on_a_short_manuscript(qapp) -> None:
    async def scenario():
        server = Server(issues=two_issues)
        view = make_view(server, SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        server.capabilities = ()
        view._start_review()
        await settle(view)
        assert len(server.sent("/v1/semantic-reviews")) == 1
        assert [view._issue_state(i) for i in view._issue_rows] == ["stale", "stale"]
        view.semantic_list.setCurrentRow(0)
        assert not view.accept_button.isEnabled() and not view.adjust_button.isEnabled()
        assert [d["decision"] for d in view._decisions()] == ["ignored"], "decisions stay as history"
    asyncio.run(scenario())


@pytest.mark.parametrize("long_manuscript", [False, True])
def test_undo_never_revives_issues_whose_semantic_basis_was_withdrawn(qapp, long_manuscript) -> None:
    source, target = SOURCE, TARGET
    if long_manuscript:
        source = SOURCE.ljust(1200, "甲") + "\n\n" + "乙" * 1200
        target = TARGET.ljust(1200, "A") + "\n\n" + "B" * 1200

    async def scenario():
        server = Server(issues=lambda s, t: two_issues(s, t) if "第三天" in s else [])
        view = make_view(server, source, target)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(1)
        view.ignore_button.click()
        server.capabilities = ()
        view._start_review()
        await settle(view)
        assert [view._issue_state(i) for i in view._issue_rows] == ["stale", "stale"]
        view._undo_replace()  # takes back the ignore, not the lost basis
        assert view._decisions() == []
        assert [view._issue_state(i) for i in view._issue_rows] == ["stale", "stale"]
        view.semantic_list.setCurrentRow(0)
        assert not view.accept_button.isEnabled() and not view.adjust_button.isEnabled()
        view.accept_button.setEnabled(True)  # even a stale enabled state cannot execute
        view._accept_current()
        assert view._payload["target"] == target
        assert view.status_label.text() == strings.MANUSCRIPT_RECONFIRM_NEEDED
        view._redo_edit()
        assert [d["decision"] for d in view._decisions()] == ["ignored"]
    asyncio.run(scenario())


def test_accepting_one_issue_leaves_the_other_issues_of_its_segment_executable(qapp) -> None:
    async def scenario():
        view = make_view(Server(issues=two_issues), SOURCE, TARGET)
        view._start_review()
        await settle(view)
        view._show_track(TRACK_SEMANTIC)
        view.semantic_list.setCurrentRow(0)
        view.accept_button.click()
        assert view.semantic_list.currentRow() == 1
        assert view.accept_button.isEnabled() and view.adjust_button.isEnabled()
        view.accept_button.click()
        both = TARGET.replace("second", "third").replace("will leave", "will not leave")
        assert view._payload["target"] == both
        assert [view._issue_state(i) for i in view._issue_rows] == ["accepted", "accepted"]
        view._undo_replace()
        assert view._payload["target"] == TARGET.replace("second", "third")
        assert [view._issue_state(i) for i in view._issue_rows] == ["accepted", "pending"]
        view.semantic_list.setCurrentRow(1)
        assert view.accept_button.isEnabled()
        view._undo_replace()
        assert view._payload["target"] == TARGET
        assert [view._issue_state(i) for i in view._issue_rows] == ["pending", "pending"]
        view._redo_edit()
        view._redo_edit()
        assert view._payload["target"] == both
        assert [view._issue_state(i) for i in view._issue_rows] == ["accepted", "accepted"]
    asyncio.run(scenario())


def _many_issues_view(n_segments: int = 64, per_segment: int = 16, n_decisions: int = 128) -> ManuscriptView:
    """1,024 live issues and 128 decisions, built directly in the session."""
    from wuwaterm_client import semantic_session as sessions
    para_s, para_t = "甲" * 1000, "A" * 1000
    source, target = "\n\n".join([para_s] * n_segments), "\n\n".join([para_t] * n_segments)
    view = make_view(Server(), source, target)
    view._capabilities = frozenset({"semantic-review-v1"})
    revisions = sessions.revision(source), sessions.revision(target)
    basis = {"dictionary_revision": "a" * 64, "matcher_revision": "a" * 64}
    for seg in range(n_segments):
        start = seg * 1002
        result = sessions.SegmentResult(
            sent_source=para_s, sent_target=para_t, source_core=(start, start + 1000),
            target_core=(start, start + 1000), hard_cut=False, index=seg,
            report={"status": "complete", "review_version": "semantic-review-v1"})
        for k in range(per_segment):
            at = start + k * 50
            ss, ts = _span(source, at, at + 3), _span(target, at, at + 3)
            record = {"id": f"{seg:016x}{k:016x}", "fingerprint": f"{k:032x}", "category": "omission",
                      "confidence": "low", "source_span": ss, "target_span": ts, "source_excerpt": ss["text"],
                      "target_excerpt": ts["text"], "rationale": "r", "suggestion": None,
                      "suggestion_withheld": None, "alternatives": [], "segment_index": seg}
            result.issues.append(sessions.LiveIssue(
                issue=record, segment_index=seg, source_revision=revisions[0], target_revision=revisions[1],
                term_basis=basis, source_span=dict(ss), target_span=dict(ts), suggestion_span=None,
                preexisting=frozenset()))
        view._session.add(result)
    issues = view._session.issues()
    view._payload.update(semantic_decisions=[sessions.make_decision(i, "ignored", None) for i in issues[:n_decisions]],
                         semantic_texts=[], semantic_history=[])
    return view


def test_status_queries_stay_fast_with_many_issues_and_decisions(qapp) -> None:
    """1,024 issues x 128 decisions: one full rebuild of the issue list and one
    keystroke each stay well inside an interactive budget (it was seconds)."""
    import time
    view = _many_issues_view()
    view._show_track(TRACK_SEMANTIC)
    started = time.perf_counter()
    view._issue_rows = []  # force a full rebuild
    view._refresh_semantic()
    view._update_controls()
    rebuild = time.perf_counter() - started
    states = [view._issue_state(i) for i in view._issue_rows]
    assert states.count("ignored") == 128 and states.count("pending") == 896
    cursor = view.target_edit.textCursor()
    cursor.setPosition(1)  # inside the first issue's target span
    view.target_edit.setTextCursor(cursor)
    started = time.perf_counter()
    view.target_edit.insertPlainText("x")
    keystroke = time.perf_counter() - started
    assert view._issue_state(view._issue_rows[0]) == "stale"
    assert rebuild < 0.2 and keystroke < 0.2, (rebuild, keystroke)


def test_reopening_restores_the_selected_occurrence_of_a_repeated_issue(qapp) -> None:
    """Identical paragraphs give identical fingerprints; the stored position
    must bring back the copy the owner was on, not the first one."""
    async def scenario():
        source = "\n\n".join([SOURCE + "甲" * 1500] * 2)
        target = "\n\n".join([TARGET + "A" * 1500] * 2)
        view = make_view(Server(issues=two_issues), source, target)
        view._start_review()
        await settle(view)
        assert len(view._issue_rows) == 4
        view.semantic_list.setCurrentRow(3)
        selected = view._current_issue()
        assert selected.segment_index == 1
        position = view.work_position()
        reopened = make_view(Server(issues=two_issues), source, target)
        reopened.restore_work_position(position)
        reopened._start_review()
        await settle(reopened)
        restored = reopened._current_issue()
        assert restored is not None and restored.segment_index == 1
        assert restored.target_span == selected.target_span
    asyncio.run(scenario())
