"""The workbench's runs, exercised without a widget (``document_controller``).

Pure Python, like the module it tests: no PySide6 and no HTTP. ``FakeClient``
stands in for ``ApiClient``, and its term and semantic answers are the shapes
``segment_records`` and ``semantic_session`` accept, so the reuse rules, the
retry/realign steps and the commits are the shipped code. ``StubHost`` is the
view's side of the protocol: it records what a run asked it to show, and applies
the one write a run cannot do itself (the target text).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

from wuwaterm_client import document_controller as dc
from wuwaterm_client import segment_records as sr
from wuwaterm_client import semantic, strings
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.segmentation import SEGMENT_LIMIT, segment_text
from wuwaterm_client.errors import ClientError

TERM = "椿"
CANDIDATE = {"candidate_id": hashlib.sha256(b"candidate").hexdigest(), "zh": TERM, "en": "Camellya",
             "category": "resonator"}
BASIS = {"rule_version": "review-v2",
         "dictionary": {"schema_version": "test-schema", "source_commit": "test-commit",
                        "term_count": 1, "revision": "1" * 64},
         "matcher_revision": "2" * 64}
OTHER_BASIS = {**BASIS, "matcher_revision": "3" * 64}


# -- what Core would answer --------------------------------------------------

def term_report(source: str, target: str, *, basis: dict = BASIS, findings: bool = True,
                verdict: str = "needs_review") -> dict:
    """A review-v2 response for exactly these cores, in the shape both the
    document review's workfile check and ``segment_records.compact_result``
    accept."""
    items = []
    start = source.find(TERM) if findings else -1
    if start >= 0:
        at = target.find(CANDIDATE["en"])
        items.append({"id": hashlib.sha256(f"{start}".encode()).hexdigest()[:32],
                      "verdict": verdict, "rule_id": "review.term_pair",
                      "source_span": {"start": start, "end": start + len(TERM), "text": TERM},
                      "target_span": None if at < 0 else {
                          "start": at, "end": at + len(CANDIDATE["en"]), "text": CANDIDATE["en"]},
                      "candidates": [{"zh": CANDIDATE["zh"], "en": CANDIDATE["en"],
                                      "category": CANDIDATE["category"], "sources": [],
                                      "candidate_id": CANDIDATE["candidate_id"]}],
                      "candidates_truncated": False})
    return {"request_id": "term", "source_revision": sr.content_hash(source),
            "target_revision": sr.content_hash(target), "rule_version": "review-v2",
            "matcher_revision": basis["matcher_revision"], "dictionary": dict(basis["dictionary"]),
            "coverage": {"evaluated": len(items),
                         "not_evaluated": sum(1 for item in items if item["verdict"] == "not_evaluated"),
                         "rules": list(sr.COVERAGE_RULES)},
            "findings": items, "truncated": False}


def semantic_report(source: str, target: str) -> dict:
    """A complete semantic-review-v1 answer about exactly these cores."""
    return {"request_id": "semantic", "review_version": semantic.REVIEW_VERSION,
            "source_revision": semantic.text_revision(source),
            "target_revision": semantic.text_revision(target),
            "term_basis": {"dictionary_revision": "1" * 64, "matcher_revision": "2" * 64},
            "status": "complete", "status_reason": None, "dropped_unanchored": 0,
            "checked": list(semantic.CATEGORIES), "issues": [], "truncated": False,
            "truncated_reason": None, "omitted_issues": 0}


class TranslationResult:
    """What ``ApiClient.translate`` returns, as far as a run reads it."""

    def __init__(self, text: str, direction: str = "en") -> None:
        self.text, self.direction = text, direction


def manuscript(sizes=(900, 900, 900), words: dict | None = None) -> dict:
    """A long manuscript (either side over 2,000 scalars): one ready pair per
    paragraph, the same fixture shape ``segment_records`` tests use."""
    words = words or {}
    def paragraph(index: int, size: int) -> str:
        head = f"段{index:03d}{words.get(index, '')}"
        return head + "文" * (size - len(head))
    return {"source": "\n\n".join(paragraph(i, n) for i, n in enumerate(sizes)),
            "target": "\n\n".join(f"Paragraph {i:03d} ".ljust(400, "x") for i in range(len(sizes))),
            "direction": "en", "alignments": None, "choices": [], "history": []}


SOURCE = "\n\n".join(f"{name}段。" * 400 for name in "甲乙丙丁")
PIECES = segment_text(SOURCE, SEGMENT_LIMIT)
CORES = [SOURCE[piece.core_start:piece.core_end] for piece in PIECES]


def written(count: int) -> str:
    """What a translation run writes for its first ``count`` pieces."""
    return "".join(SOURCE[p.start:p.core_start] + f"[{SOURCE[p.core_start:p.core_end]}]" + SOURCE[p.core_end:p.end]
                   for p in PIECES[:count])


# -- the two stand-ins -------------------------------------------------------

class FakeClient:
    """The four async calls a run makes, answering from a script.

    ``fail`` queues answers per method: an exception raises, ``None`` succeeds.
    ``hold`` maps a method to ``(call number, event)`` and holds that call and
    every later one until the test releases it, which is how a cancellation or a
    superseded generation is made deterministic.
    """

    def __init__(self, *, capabilities=("semantic-review-v1",)) -> None:
        self.capabilities = frozenset(capabilities)
        self.configured = True
        self.term = lambda call: term_report(call["source"], call["target"])
        self.semantic = lambda call: semantic_report(call["source"], call["target"])
        self.translation = lambda call: TranslationResult(f"[{call['text']}]")
        self.fail: dict[str, list] = {}
        self.hold: dict[str, tuple[int, asyncio.Event]] = {}
        self.counts: dict[str, int] = {}
        self.review_calls: list[dict] = []
        self.semantic_calls: list[dict] = []
        self.translation_calls: list[dict] = []
        self.capability_calls = 0

    async def _before(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1
        hold = self.hold.get(name)
        if hold is not None and self.counts[name] >= hold[0]:
            await hold[1].wait()
        queue = self.fail.get(name)
        if queue:
            failure = queue.pop(0)
            if failure is not None:
                raise failure

    async def get_capabilities(self):
        self.capability_calls += 1
        return self.capabilities

    async def review_pair(self, source, target, direction, *, alignments=None, resolutions=None,
                          resolution_context=None) -> dict:
        call = {"source": source, "target": target, "direction": direction, "alignments": alignments,
                "resolutions": resolutions, "resolution_context": resolution_context}
        self.review_calls.append(call)
        await self._before("review_pair")
        return self.term(call)

    async def semantic_review(self, source, target, direction, *, before=None, precedents=None) -> dict:
        call = {"source": source, "target": target, "direction": direction, "before": before,
                "precedents": precedents}
        self.semantic_calls.append(call)
        await self._before("semantic_review")
        return self.semantic(call)

    async def translate(self, text, *, to, context=None) -> TranslationResult:
        call = {"text": text, "to": to, "context": context}
        self.translation_calls.append(call)
        await self._before("translate")
        return self.translation(call)


class StubHost:
    """``WorkbenchHost`` as a record of what a run asked the view to do."""

    def __init__(self) -> None:
        self.controller: dc.DocumentRunController | None = None
        self.calls: list[str] = []
        self.statuses: list[str] = []
        self.reports: list[dict] = []
        self.review_states: list[tuple[str, str, str | None]] = []
        self.snapshots: list[dict] = []
        self.writes: list[tuple[str, tuple]] = []
        self.completed: list[int] = []
        self.unstorable: list[str] = []
        self.precedents = True
        self.segment = None
        self.refuse_write = False

    def _record(self, name: str) -> None:
        self.calls.append(name)

    def refresh_semantic(self) -> None:
        self._record("refresh_semantic")

    def refresh_segments(self) -> None:
        self._record("refresh_segments")

    def refresh_segments_if_shown(self) -> None:
        self._record("refresh_segments_if_shown")

    def update_controls(self) -> None:
        self._record("update_controls")

    def render_semantic_progress(self) -> None:
        self._record("render_semantic_progress")

    def show_segment_term_report(self) -> None:
        self._record("show_segment_term_report")

    def show_term_report(self, report: dict) -> None:
        self.reports.append(report)

    def render_review_state(self) -> None:
        self.review_states.append((self.controller.review_state, self.controller.review_detail,
                                   self.controller.request_id))

    def clear_report(self) -> None:
        self._record("clear_report")

    def report_status(self, text: str) -> None:
        self.statuses.append(text)

    def report_unstorable_report(self, code: str) -> None:
        self.unstorable.append(code)

    def report_translation_complete(self, mismatched: int) -> None:
        self.completed.append(mismatched)

    def trust_review_snapshot(self, snapshot: dict) -> None:
        self.snapshots.append(snapshot)

    def precedents_selected(self) -> bool:
        return self.precedents

    def selected_segment(self):
        return self.segment

    def apply_target_write(self, changed: str, edit: tuple) -> bool:
        if self.refuse_write:
            return False
        self.writes.append((changed, edit))
        self.controller.payload["target"] = changed
        return True


def make(client: FakeClient | None = None, payload: dict | None = None) -> tuple[dc.DocumentRunController, FakeClient,
                                                                              StubHost]:
    client = client or FakeClient()
    host = StubHost()
    controller = dc.DocumentRunController(client, host)
    host.controller = controller
    if payload is not None:
        controller.payload = payload
    return controller, client, host


async def settle(controller: dc.DocumentRunController, *, limit: int = 2000) -> None:
    """Wait until no run is in flight and its completion callbacks have run."""
    for _ in range(limit):
        running = [task for task in (controller.task, controller.semantic_task, controller.translate_task)
                   if task is not None and not task.done()]
        if running:
            await asyncio.wait(running, timeout=0.05)
            continue
        await asyncio.sleep(0)
        if all(task is None for task in (controller.task, controller.semantic_task, controller.translate_task)):
            return
    raise AssertionError("the controller did not settle")


async def until(predicate, *, limit: int = 2000) -> None:
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the expected state never arrived")


def document_draft(source: str = f"Jinhsi {TERM}", target: str = "今汐") -> dict:
    value = empty_workfile()
    value.update(source=source, target=target, direction="en")
    return value


def translation_draft(target: str = "") -> dict:
    value = empty_workfile()
    value.update(source=SOURCE, target=target, direction="en")
    return value


# -- the module is the desktop-free half -------------------------------------

def test_the_controller_module_takes_no_desktop_dependency() -> None:
    module = importlib.import_module("wuwaterm_client.document_controller")
    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("PySide6", "QtCore", "QtWidgets", "import httpx", "from .api import", "keyring"):
        assert forbidden not in source, f"{forbidden} leaked into the controller"


def test_importing_the_controller_loads_no_desktop_or_http_module() -> None:
    """The pure CI job has neither installed; this also holds where they are."""
    program = (
        "import sys\n"
        "import wuwaterm_client.document_controller\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        "raise SystemExit(1 if loaded & {'PySide6', 'httpx', 'keyring'} else 0)\n"
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run([sys.executable, "-c", program],
                               env={**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")},
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr


# -- the document review -----------------------------------------------------

def test_a_document_review_commits_its_report_snapshot_and_phase() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=())
        controller, client, host = make(client, document_draft(source="Jinhsi"))

        controller.start_review()
        assert controller.review_state == "reviewing" and controller.task is not None
        await settle(controller)

        assert controller.review_state == "current" and controller.request_id == "term"
        assert controller.term_mode == "document"
        # The report, its snapshot and the document revision are committed once.
        assert len(controller.payload["history"]) == 1
        assert controller.payload["history"][0]["report"] == host.reports[-1]
        assert host.snapshots == [controller.payload["history"][0]]
        assert controller.document_revision == 1
        # A short manuscript with no semantic capability runs no segmented review.
        assert controller.semantic_task is None and client.semantic_calls == []
        assert client.review_calls[0]["source"] == "Jinhsi"
    asyncio.run(scenario())


def test_an_answer_from_a_superseded_generation_is_ignored() -> None:
    async def scenario() -> None:
        controller, client, host = make(FakeClient(capabilities=()), document_draft())
        controller.generation = 7  # a newer run owns the document now

        await controller.run_review(6, "Jinhsi", "今汐", "en", None)

        assert controller.review_state == "none", "the stale run must not report a phase"
        assert controller.payload["history"] == [] and controller.document_revision == 0
        assert host.reports == [] and host.snapshots == []
    asyncio.run(scenario())


def test_cancelling_a_document_review_keeps_the_draft_and_says_cancelled() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=())
        gate = asyncio.Event()
        client.hold["review_pair"] = (1, gate)
        controller, client, host = make(client, document_draft())
        before = dict(controller.payload)

        controller.start_review()
        await until(lambda: client.review_calls)
        controller.cancel_review()
        gate.set()
        await settle(controller)

        assert controller.review_state == "cancelled"
        assert controller.payload == before and controller.document_revision == 0
        assert host.reports == []
        # A cancelled review is still a review that was asked for once.
        assert len(client.review_calls) == 1
    asyncio.run(scenario())


# -- the segmented term and semantic run -------------------------------------

async def long_run(client: FakeClient, payload: dict) -> tuple[dc.DocumentRunController, FakeClient, StubHost]:
    controller, client, host = make(client, payload)
    controller.start_review()
    await settle(controller)
    return controller, client, host


def test_a_long_manuscript_probes_the_basis_then_reviews_every_segment() -> None:
    async def scenario() -> None:
        controller, client, host = await long_run(FakeClient(), manuscript(words={0: TERM}))

        assert controller.term_mode == "segments" and controller.semantic_state == "done"
        assert controller.term_basis == BASIS
        statuses = [record["status"] for record in controller.payload["segments"]]
        assert statuses == ["done", "done", "done"]
        # One term check per segment, all after the first response supplied the basis.
        assert len(client.review_calls) == 3
        assert all(call["resolutions"] is None for call in client.review_calls)
        assert [call["source"] for call in client.semantic_calls] == [
            controller.payload["source"][a:b] for a, b in
            [(record["source_start"], record["source_end"]) for record in controller.payload["segments"]]]
        # One snapshot of this run, holding all three segments.
        assert len(controller.payload["semantic_history"]) == 1
        assert len(controller.payload["semantic_history"][0]["segments"]) == 3
        assert controller.segment_failures == {}
        assert host.reports == []
    asyncio.run(scenario())


def test_a_reopened_manuscript_probes_the_live_basis_once_and_sends_no_term_rechecks() -> None:
    async def scenario() -> None:
        first, _client, _host = await long_run(FakeClient(), manuscript(words={0: TERM}))
        saved = first.payload

        # Reopened: the records are saved, but this session has no basis yet.
        controller, client, host = await long_run(FakeClient(), saved)

        assert controller.term_basis == BASIS
        # Exactly one term request: the probe for the first segment. Nothing
        # saved is current before that answer.
        assert len(client.review_calls) == 1
        assert client.review_calls[0]["resolutions"] is None
        # The other segments needed no term check at all, only their semantics.
        assert len(client.semantic_calls) == 3
        assert [record["status"] for record in controller.payload["segments"]] == ["done", "done", "done"]
    asyncio.run(scenario())


def test_a_rejected_request_with_resolutions_falls_back_once_without_them() -> None:
    async def scenario() -> None:
        payload = manuscript(words={0: TERM})
        first = (await long_run(FakeClient(), payload))[0]
        # The owner confirms the only candidate, so the next check carries a resolution.
        record = first.payload["segments"][0]
        finding = record["result"]["findings"][0]
        start, end = finding["source"]
        first.put_parts(sr.choose(first.payload, 0, start, end, sr.OFFICIAL_PAIR,
                                  CANDIDATE["candidate_id"]))
        assert first.payload["segments"][0]["status"] == "stale"
        saved = first.payload

        client = FakeClient()
        # Core refuses the resolutions once; the fallback (no resolutions) succeeds.
        refusals = [ClientError("invalid_request")]
        client.fail["review_pair"] = refusals
        controller, client, host = make(client, saved)
        controller.term_basis = BASIS
        controller.start_review()
        await settle(controller)

        assert len(client.review_calls) == 3, client.review_calls
        assert client.review_calls[0]["resolutions"]
        assert client.review_calls[1]["resolutions"] is None, "the fallback carries no resolutions"
        assert client.review_calls[2]["resolutions"], "the kept choice goes out once more"
        assert controller.payload["segments"][0]["status"] == "done"
    asyncio.run(scenario())


def test_a_fatal_code_stops_the_run_where_it_happened() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.fail["review_pair"] = [ClientError("forbidden")]
        controller, client, _host = await long_run(client, manuscript(words={0: TERM}))

        assert controller.semantic_state == "error"
        assert controller.semantic_detail == ClientError("forbidden").message
        assert len(client.review_calls) == 1, "nothing else may be asked after a fatal code"
        assert all(record["status"] == "pending" for record in controller.payload["segments"] if record["result"] is None)
    asyncio.run(scenario())


def test_the_same_failure_twice_is_the_service_so_the_run_stops() -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.fail["review_pair"] = [ClientError("internal"), ClientError("internal")]
        controller, client, _host = await long_run(client, manuscript(words={0: TERM}))

        assert controller.semantic_state == "error"
        assert len(client.review_calls) == 2, "the streak stops at two, not three"
        failures = list(controller.segment_failures.values())
        assert failures == ["failed", "failed"]
    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["rate_limited", "llm_budget_exhausted"])
def test_a_quota_answer_pauses_the_run_and_keeps_what_it_has(code: str) -> None:
    async def scenario() -> None:
        client = FakeClient()
        client.fail["review_pair"] = [None, ClientError(code)]
        controller, client, _host = await long_run(client, manuscript(words={0: TERM}))

        assert controller.semantic_state == "quota"
        records = controller.payload["segments"]
        assert records[0]["status"] == "done", "the segment that finished is still finished"
        assert records[0]["result"] is not None
        assert len(client.semantic_calls) == 1, "one segment was fully reviewed"
    asyncio.run(scenario())


def test_cancelling_mid_segment_keeps_the_stored_results_and_stops_the_loop() -> None:
    async def scenario() -> None:
        client = FakeClient()
        gate = asyncio.Event()
        client.hold["semantic_review"] = (1, gate)
        controller, client, host = make(client, manuscript(words={0: TERM}))
        controller.start_review()
        await until(lambda: client.semantic_calls)

        controller.cancel_review()
        assert controller.semantic_state == "paused"
        gate.set()
        await settle(controller)

        # The first segment's term result is committed; its semantic answer never is.
        assert controller.payload["segments"][0]["status"] == "done"
        assert controller.payload["segments"][1]["result"] is None
        assert len(client.semantic_calls) == 1
        assert controller.payload.get("semantic_history") is None
        assert "refresh_semantic" in host.calls and "update_controls" in host.calls
    asyncio.run(scenario())


# -- one translation run -----------------------------------------------------

def test_a_whole_manuscript_run_writes_every_piece_as_one_edit() -> None:
    async def scenario() -> None:
        controller, client, host = make(FakeClient(), translation_draft())

        controller.start_translation()
        assert controller.translate_state == "waiting"
        await settle(controller)

        assert [call["text"] for call in client.translation_calls] == CORES
        assert controller.payload["target"] == written(4)
        assert len(host.writes) == 1, "one undoable edit, not one per piece"
        changed, edit = host.writes[0]
        assert changed == written(4) and edit == ("target", 0, 0, len(changed))
        assert controller.translate_state == "idle" and controller.translate_run is None
        assert host.completed == [0]
    asyncio.run(scenario())


def test_pausing_keeps_the_finished_pieces_and_the_next_press_resumes_there() -> None:
    async def scenario() -> None:
        client = FakeClient()
        gate = asyncio.Event()
        client.hold["translate"] = (2, gate)
        controller, client, host = make(client, translation_draft())

        controller.start_translation()
        await until(lambda: len(client.translation_calls) == 2)  # the second piece is held
        controller.cancel_review()  # the cancel button while a translation runs

        # What finished is written at once, as one edit.
        assert controller.payload["target"] == written(1)
        assert controller.translate_state == "paused"
        run = controller.resumable()
        assert run is not None and run.next_piece == 1 and run.written

        gate.set()
        controller.start_translation()
        await settle(controller)

        # The resumed run asks for the held piece again and finishes the rest.
        assert [call["text"] for call in client.translation_calls] == CORES[:2] + CORES[1:]
        assert controller.payload["target"] == written(4)
        assert len(host.writes) == 2
    asyncio.run(scenario())


def test_a_hard_stop_rejects_the_answer_of_the_abandoned_run() -> None:
    async def scenario() -> None:
        client = FakeClient()
        gate = asyncio.Event()
        client.hold["translate"] = (1, gate)
        controller, client, host = make(client, translation_draft())

        controller.start_translation()
        await until(lambda: client.translation_calls)
        controller.stop_translation()
        gate.set()
        await settle(controller)

        assert controller.payload["target"] == "", "nothing the stopped run asked for is written"
        assert controller.translate_state == "idle" and controller.translate_run is None
        assert host.writes == []
    asyncio.run(scenario())


def test_an_editor_that_cannot_show_the_result_writes_nothing() -> None:
    async def scenario() -> None:
        controller, client, host = make(FakeClient(), translation_draft())
        host.refuse_write = True

        controller.start_translation()
        await settle(controller)

        assert controller.payload["target"] == ""
        assert controller.translate_state == "idle"
        assert host.statuses[-1] == strings.MANUSCRIPT_EDIT_REJECTED
    asyncio.run(scenario())
