"""Segmented translation in the manuscript workbench keeps finished pieces.

Requests go through the real ApiClient and an httpx MockTransport that echoes
each text as ``[text]`` and records every request, so the pieces, their
separators and the context sent with each one are the shipped code. The source
is synthetic: four paragraphs of 1,200 scalars, each its own 1,800-scalar piece.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from wuwaterm_client import strings
from wuwaterm_client.api import ApiClient
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.segmentation import SEGMENT_LIMIT, segment_text
from wuwaterm_client.ui.manuscript_view import TRACK_SEGMENTS, ManuscriptView

SOURCE = "\n\n".join(f"{name}段。" * 400 for name in "甲乙丙丁")
PIECES = segment_text(SOURCE, SEGMENT_LIMIT)
CORES = [SOURCE[p.core_start:p.core_end] for p in PIECES]


def written(count: int) -> str:
    """What a run writes for its first ``count`` pieces, separators included."""
    return "".join(SOURCE[p.start:p.core_start] + f"[{SOURCE[p.core_start:p.core_end]}]" + SOURCE[p.core_end:p.end]
                   for p in PIECES[:count])


def failure(code: str = "internal", status: int = 500) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "x"}, "request_id": "e"})


class Transport:
    """Answers capabilities and translations. ``fail`` maps the nth translation
    request (1-based) to an error; ``gate`` to an event it waits on first."""

    def __init__(self, fail=None, gate=None) -> None:
        self.fail, self.gate = fail or {}, gate or {}
        self.sent: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"api_version": "v1", "capabilities": ["translation-context-v1"],
                                             "request_id": "caps"})
        assert request.url.path == "/v1/translations"
        body = json.loads(request.content)
        self.sent.append(body)
        number = len(self.sent)
        if number in self.gate:
            await self.gate[number].wait()
        if number in self.fail:
            return self.fail[number]
        return httpx.Response(200, json={"kind": "llm", "text": f"[{body['text']}]", "direction": "en",
                                         "dictionary_miss": False, "request_id": "tr"})

    async def reached(self, number: int) -> None:
        for _ in range(500):
            if len(self.sent) >= number:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"request {number} was never sent")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make_view(transport: Transport, source: str = SOURCE, target: str = "", alignments=None) -> ManuscriptView:
    client = ApiClient("https://example.invalid", token_provider=lambda: "test-token",
                       _test_transport=httpx.MockTransport(transport))
    view = ManuscriptView(client)
    value = empty_workfile()
    value.update(source=source, target=target, direction="en")
    if alignments is not None:
        value["alignments"] = alignments
    view._set_payload(value)
    view._saved_payload = view._payload.copy()
    view._update_controls()
    return view


async def settle(view: ManuscriptView) -> None:
    for _ in range(200):
        if view._translate_task is None or view._translate_task.done():
            await asyncio.sleep(0)
            return
        await asyncio.wait([view._translate_task])
    raise AssertionError("translation did not settle")


async def uninterrupted() -> tuple[str, list[dict]]:
    transport = Transport()
    view = make_view(transport)
    view._translate()
    await settle(view)
    return view._payload["target"], transport.sent


def failed_text(index: int, done: int) -> str:
    return strings.MANUSCRIPT_TRANSLATE_FAILED.format(
        index=index, total=len(PIECES), done=done, reason=strings.ERROR_MSG_INTERNAL)


def test_the_source_is_cut_into_four_pieces() -> None:
    assert len(PIECES) == 4 and all(len(core) <= SEGMENT_LIMIT for core in CORES)


def test_a_failure_at_piece_three_keeps_pieces_one_and_two_and_a_retry_resumes_there(qapp) -> None:
    async def scenario():
        reference, reference_sent = await uninterrupted()
        assert reference == written(4)
        gate = asyncio.Event()
        transport = Transport(fail={3: failure()}, gate={4: gate})
        view = make_view(transport)
        view._translate()
        await settle(view)
        # Pieces 1 and 2 are in the target, with their separators, as one edit.
        assert view._payload["target"] == written(2) and written(2).endswith("\n\n")
        assert len(view._undo_history) == 1
        assert view._translate_state == "failed"
        assert view.status_label.text() == failed_text(3, 2)
        assert view.translate_button.text() == strings.MANUSCRIPT_TRANSLATE_RETRY
        assert view.translate_button.isEnabled()

        view.translate_button.click()
        await transport.reached(4)
        assert view._translate_state == "retrying"
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_RETRYING.format(index=3, total=4)
        gate.set()
        await settle(view)
        # The retried request is piece 3, with the same context an uninterrupted run sends.
        assert transport.sent[3]["text"] == CORES[2] and transport.sent[3] == reference_sent[2]
        assert [b["text"] for b in transport.sent] == CORES[:3] + CORES[2:]
        assert len(transport.sent) == len(PIECES) + 1
        assert view._payload["target"] == reference
        assert len(view._undo_history) == 2, "the remainder is a second single edit"
        assert view._translate_state == "idle" and view.status_label.text() == strings.MANUSCRIPT_TRANSLATED
        assert view.translate_button.text() == ""
    asyncio.run(scenario())


def test_pausing_during_piece_three_keeps_pieces_one_and_two_and_continue_resumes_there(qapp) -> None:
    async def scenario():
        reference, _ = await uninterrupted()
        resumed = asyncio.Event()
        transport = Transport(gate={3: asyncio.Event(), 4: resumed})
        view = make_view(transport)
        view._translate()
        await transport.reached(3)
        assert view._translate_state == "waiting"
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATING.format(index=3, total=4)
        assert view.cancel_button.text() == strings.MANUSCRIPT_REVIEW_PAUSE and not view.cancel_button.isHidden()
        view.cancel_button.click()
        # Written at once, not when the cancelled request unwinds.
        assert view._payload["target"] == written(2)
        assert view._translate_state == "paused"
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_PAUSED.format(done=2, total=4)
        assert view.translate_button.text() == strings.MANUSCRIPT_TRANSLATE_CONTINUE
        # Continue at once, before the cancelled request has unwound.
        view.translate_button.click()
        await transport.reached(4)
        for _ in range(3):
            await asyncio.sleep(0)  # the cancelled request unwinds meanwhile
        assert view._translate_state == "waiting" and view._translating(), "the old task's end must not stop the new run"
        resumed.set()
        await settle(view)
        assert len(transport.sent) == 5
        assert [b["text"] for b in transport.sent] == CORES[:3] + CORES[2:]
        assert view._payload["target"] == reference
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["rate_limited", "llm_budget_exhausted"])
def test_a_quota_answer_pauses_instead_of_failing(qapp, code) -> None:
    async def scenario():
        reference, _ = await uninterrupted()
        transport = Transport(fail={2: failure(code, 429)})
        view = make_view(transport)
        view._translate()
        await settle(view)
        assert view._translate_state == "paused"
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_QUOTA.format(done=1, total=4)
        assert view._payload["target"] == written(1)
        assert view.translate_button.text() == strings.MANUSCRIPT_TRANSLATE_CONTINUE
        view.translate_button.click()
        await settle(view)
        assert transport.sent[2]["text"] == CORES[1] and view._payload["target"] == reference
    asyncio.run(scenario())


def test_the_four_states_and_their_buttons_read_differently(qapp) -> None:
    async def scenario():
        first, third = asyncio.Event(), asyncio.Event()
        transport = Transport(fail={2: failure()}, gate={1: first, 3: third})
        view = make_view(transport)
        idle = (view.translate_button.text(), view.translate_button.toolTip())
        seen = {}

        def look(state):
            assert view._translate_state == state
            seen[state] = (view.status_label.text(), view.cancel_button.text(), view.translate_button.text(),
                           view.translate_button.accessibleName(), view.translate_segment_button.text())

        view._translate()
        await transport.reached(1)
        look("waiting")
        first.set()
        await settle(view)
        look("failed")
        view.translate_button.click()
        await transport.reached(3)
        look("retrying")
        view.cancel_button.click()
        look("paused")

        expected = {
            "waiting": strings.MANUSCRIPT_TRANSLATING.format(index=1, total=4),
            "retrying": strings.MANUSCRIPT_TRANSLATE_RETRYING.format(index=2, total=4),
            "paused": strings.MANUSCRIPT_TRANSLATE_PAUSED.format(done=1, total=4),
            "failed": failed_text(2, 1),
        }
        assert {state: seen[state][0] for state in seen} == expected
        assert len(set(expected.values())) == 4
        # While a request is in flight the cancel button reads pause; once it
        # stopped, the translate button offers the next step.
        assert seen["waiting"][1] == seen["retrying"][1] == strings.MANUSCRIPT_REVIEW_PAUSE
        assert seen["paused"][2:] == (strings.MANUSCRIPT_TRANSLATE_CONTINUE,) * 3
        assert seen["failed"][2:] == (strings.MANUSCRIPT_TRANSLATE_RETRY,) * 3
        assert idle[0] == "" and strings.MANUSCRIPT_TRANSLATE in idle[1]
        actions = {strings.MANUSCRIPT_REVIEW_PAUSE, strings.MANUSCRIPT_TRANSLATE_CONTINUE,
                   strings.MANUSCRIPT_TRANSLATE_RETRY, strings.MANUSCRIPT_TRANSLATE}
        assert len(actions) == 4
    asyncio.run(scenario())


def test_one_undo_removes_the_partial_write_and_the_next_press_starts_over(qapp) -> None:
    async def scenario():
        reference, _ = await uninterrupted()
        transport = Transport(fail={3: failure()})
        view = make_view(transport)
        view._translate()
        await settle(view)
        assert view._payload["target"] == written(2)
        view._undo_replace()
        assert view._payload["target"] == "" and not view._undo_history
        assert view._translate_state == "idle" and view.translate_button.text() == ""
        assert view.status_label.text() != failed_text(3, 2)
        view._redo_edit()
        assert view._payload["target"] == written(2)
        assert view._translate_state == "idle", "a redo does not bring the offer back"
        view._translate()
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK and len(transport.sent) == 3
        view._undo_replace()
        view._translate()
        await settle(view)
        assert [b["text"] for b in transport.sent] == CORES[:3] + CORES
        assert view._payload["target"] == reference
    asyncio.run(scenario())


@pytest.mark.parametrize("side", ["source", "target"])
def test_editing_after_a_failure_withdraws_the_offer(qapp, side) -> None:
    async def scenario():
        transport = Transport(fail={3: failure()})
        view = make_view(transport)
        view._translate()
        await settle(view)
        assert view._translate_state == "failed"
        editor = view.source_edit if side == "source" else view.target_edit
        editor.moveCursor(QTextCursor.MoveOperation.End)
        editor.insertPlainText("丁")
        assert view._translate_state == "idle" and view.translate_button.text() == ""
        assert view.status_label.text() != failed_text(3, 2)
        view._translate()
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_ONLY_BLANK and len(transport.sent) == 3
    asyncio.run(scenario())


def test_text_changed_while_a_request_is_in_flight_keeps_todays_refusal(qapp) -> None:
    async def scenario():
        gate = asyncio.Event()
        transport = Transport(fail={3: failure()}, gate={3: gate})
        view = make_view(transport)
        view._translate()
        await transport.reached(3)
        view.source_edit.moveCursor(QTextCursor.MoveOperation.End)
        view.source_edit.insertPlainText("丁")
        gate.set()
        await settle(view)
        assert view._payload["target"] == "" and len(view._undo_history) == 1, "only the owner's own edit"
        assert view._translate_state == "idle"
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_CHANGED
    asyncio.run(scenario())


def test_a_failed_single_segment_is_offered_again(qapp) -> None:
    source, target = "甲。\n\n乙。\n\n丙。", "A.\n\nC."

    def span(text, quote):
        start = text.index(quote)
        return {"start": start, "end": start + len(quote), "text": quote}
    alignments = [{"source": span(source, "甲。"), "target": span(target, "A.")},
                  {"source": span(source, "乙。"), "target": None},
                  {"source": span(source, "丙。"), "target": span(target, "C.")}]

    async def scenario():
        transport = Transport(fail={1: failure()})
        view = make_view(transport, source, target, alignments)
        view._show_track(TRACK_SEGMENTS)
        view.segment_list.setCurrentRow([p.status for p in view._segment_rows].index("untranslated"))
        view.translate_segment_button.click()
        await settle(view)
        assert view._translate_state == "failed" and view._payload["target"] == target
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_SEGMENT_FAILED.format(
            index=1, total=1, done=0, reason=strings.ERROR_MSG_INTERNAL)
        assert view.translate_segment_button.text() == strings.MANUSCRIPT_TRANSLATE_RETRY
        view.translate_segment_button.click()
        await settle(view)
        assert [b["text"] for b in transport.sent] == ["乙。", "乙。"]
        assert view._payload["target"] == "A.\n\n[乙。]\n\nC."
        assert view.translate_segment_button.text() == strings.MANUSCRIPT_TRANSLATE_SEGMENT
    asyncio.run(scenario())


def test_a_two_piece_segment_keeps_its_first_piece_across_a_failed_second_one(qapp) -> None:
    """A single untranslated segment longer than one piece writes nothing until
    it is complete (a partial write would stop it being untranslated), so the
    finished piece is kept with the run and written with the rest."""
    first, second = "甲段。" * 400, "乙段。" * 400
    long_paragraph = first + "\n\n" + second
    source, target = "开头。\n\n" + long_paragraph + "\n\n结尾。", "Start.\n\nEnd."

    def span(text, quote):
        start = text.index(quote)
        return {"start": start, "end": start + len(quote), "text": quote}
    alignments = [{"source": span(source, "开头。"), "target": span(target, "Start.")},
                  {"source": span(source, long_paragraph), "target": None},
                  {"source": span(source, "结尾。"), "target": span(target, "End.")}]

    async def scenario():
        transport = Transport(fail={2: failure()})
        view = make_view(transport, source, target, alignments)
        view._show_track(TRACK_SEGMENTS)
        view.segment_list.setCurrentRow([p.status for p in view._segment_rows].index("untranslated"))
        view.translate_segment_button.click()
        await settle(view)
        assert view._translate_state == "failed" and view._payload["target"] == target, "nothing written yet"
        assert view.status_label.text() == strings.MANUSCRIPT_TRANSLATE_SEGMENT_FAILED.format(
            index=2, total=2, done=1, reason=strings.ERROR_MSG_INTERNAL)
        view.translate_segment_button.click()
        await settle(view)
        assert [b["text"] for b in transport.sent] == [first, second, second]
        assert f"[{first}]\n\n[{second}]" in view._payload["target"]
        assert view._translate_state == "idle" and len(view._undo_history) == 1, "one write, once complete"
    asyncio.run(scenario())
