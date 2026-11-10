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
from PySide6.QtCore import Qt
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
        assert view._payload["semantic_history"][-1]["report"]["request_id"] == "semantic"
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

        # Any change to the manuscript means the old decision is only a reference.
        loaded = load_workfile(path)
        loaded["target"] = TARGET + " Then they rest."
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
