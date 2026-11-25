"""Layered review and review features in the workbench's runs, without a widget.

Pure Python (no PySide6, no HTTP), like ``test_document_controller``, whose
stand-ins it reuses: ``FakeClient`` answers as Core would, ``StubHost`` records
what a run asked the view to do. Covered here: the Layer 1 structure check (it
asks for capabilities only when pressed, honours their absence, and never
reaches the manuscript, the undo history or a written file), the session's
review features on document and segment checks, and the document choices an
ambiguous one-character name takes, sent as resolutions with the next check.
"""
from __future__ import annotations

import asyncio
import copy
import json

from test_document_controller import BASIS, CANDIDATE, TERM, FakeClient, StubHost, make, manuscript, settle

from wuwaterm_client import document_controller as dc
from wuwaterm_client import segment_records as sr
from wuwaterm_client import strings
from wuwaterm_client.errors import ERROR_INVALID_REQUEST, ClientError
from wuwaterm_client.segmentation import GAP_SEGMENTATION_VERSION
from wuwaterm_client.manuscript import (
    REVIEW_FEATURE_AMBIGUOUS_MENTION as AMB, WORKFILE_FORMAT_V5, empty_workfile, parse_workfile, serialize_workfile,
)

STRUCTURE = "structure-check-v1"
SHORT_SOURCE = "椿和今汐在城楼上。椿看着远方。"
SHORT_TARGET = "Camellya and Jinhsi stood on the tower. Camellya looked afar."


def short_payload() -> dict:
    payload = empty_workfile()
    payload.update(source=SHORT_SOURCE, target=SHORT_TARGET)
    return payload


def ambiguous_report(call: dict) -> dict:
    """Core's review-v2 answer for a request that asked for ambiguous-mention-v1:
    the second 椿 is an unsettled one-character name until a resolution names it."""
    source, target = call["source"], call["target"]
    at = source.index(TERM, 1)
    resolved = any(r["mention_id"] == f"{at}:{at + 1}:{TERM}" for r in call["resolutions"] or ())
    finding = {"id": f"{at}:{at + 1}:{TERM}", "rule_id": "review.term_pair",
               "verdict": "verified_constraint" if resolved else "not_evaluated",
               "source_span": {"start": at, "end": at + 1, "text": TERM},
               "target_span": None, "candidates": [{**CANDIDATE, "sources": []}], "candidates_truncated": False}
    if not resolved:
        finding["reason"] = "ambiguous_mention"
    return {"request_id": "term", "source_revision": sr.content_hash(source),
            "target_revision": sr.content_hash(target), "rule_version": "review-v2",
            "matcher_revision": BASIS["matcher_revision"], "dictionary": dict(BASIS["dictionary"]),
            "coverage": {"evaluated": 1 if resolved else 0, "not_evaluated": 0 if resolved else 1,
                         "rules": list(sr.COVERAGE_RULES)},
            "findings": [finding], "truncated": False}


def structure_answer(findings_for):
    def answer(body: dict) -> dict:
        return {"request_id": "l1", "check_version": STRUCTURE,
                "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
                "segments": [{"index": i, "segment_id": seg["segment_id"], "findings": findings_for(seg),
                              "omitted_findings": 0,
                              "layout": {"source_paragraphs": 1, "target_paragraphs": 1, "source_lines": 1,
                                         "target_lines": 1}}
                             for i, seg in enumerate(body["segments"])]}
    return answer


def missing_number(seg: dict) -> list[dict]:
    """A number_missing finding on every segment whose source holds 段0."""
    at = seg["source"].find("段0")
    if at < 0:
        return []
    return [{"rule_id": "number_missing", "source_span": {"start": at + 1, "end": at + 2, "text": "0"},
             "target_span": None}]


# -- Layer 1 -------------------------------------------------------------------

def test_the_structure_check_asks_for_capabilities_only_when_pressed() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=())
        controller, client, host = make(client, short_payload())
        assert controller.capabilities is None and client.capability_calls == 0
        assert controller.start_structure_check()
        await settle(controller)
        # The server does not list the capability: nothing is sent, and it says so.
        assert client.capability_calls == 1 and client.structure_calls == []
        assert controller.structure.phase == "failed"
        assert controller.structure.error == (strings.MANUSCRIPT_L1_UNAVAILABLE, None)
        assert controller.structure_known_unavailable()
        assert not controller.start_structure_check()
    asyncio.run(scenario())


def test_structure_results_follow_the_text_and_never_reach_the_manuscript() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(STRUCTURE,))
        client.structure = structure_answer(missing_number)
        payload = {**empty_workfile(), **manuscript()}
        controller, client, host = make(client, payload)
        before_bytes = serialize_workfile(controller.payload)
        before_payload = copy.deepcopy(controller.payload)
        revision = controller.document_revision
        assert controller.start_structure_check()
        await settle(controller)
        assert controller.structure.phase == "done"
        assert len(client.structure_calls) == 1
        sent = client.structure_calls[0]["segments"]
        assert [s["segment_id"] for s in sent] == ["s0", "s1", "s2"]
        views = controller.structure_views()
        assert [v.flagged for v in views] == [True, True, True]
        # Nothing of it is in the manuscript, the undo history or the written bytes.
        assert controller.payload == before_payload
        assert serialize_workfile(controller.payload) == before_bytes
        assert not controller.can_undo() and controller.document_revision == revision
        # Editing the second paragraph drops its result at once; the others stay.
        source = controller.payload["source"]
        at = source.index("段001") + 4
        controller.edit_text("source", source[:at] + "改" + source[at:], edit=(at, at, 1))
        assert [v.result is not None for v in controller.structure_views()] == [True, False, True]
        # Undo restores the text, and with it the result that was made for it.
        controller.undo()
        assert [v.result is not None for v in controller.structure_views()] == [True, True, True]
        assert "render_structure" in host.calls
    asyncio.run(scenario())


def test_a_new_document_drops_the_structure_results() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(STRUCTURE,))
        client.structure = structure_answer(missing_number)
        controller, client, _host = make(client, {**empty_workfile(), **manuscript()})
        controller.start_structure_check()
        await settle(controller)
        assert controller.structure.results
        controller.reset_term_session()
        assert controller.structure.results == {} and controller.structure.phase == "idle"
    asyncio.run(scenario())


def test_a_failed_batch_reports_the_service_error() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(STRUCTURE,))
        client.fail["structure_check"] = [ClientError("rate_limited", request_id="rid", status_code=429)]
        controller, client, _host = make(client, {**empty_workfile(), **manuscript()})
        controller.start_structure_check()
        await settle(controller)
        assert controller.structure.phase == "failed"
        assert controller.structure.error[1] == "rid"
        assert controller.structure.results == {}
    asyncio.run(scenario())


# -- review features and ambiguous one-character names ----------------------------

def test_a_document_check_asks_for_the_feature_and_sends_this_sessions_choice() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(AMB,))
        client.term = ambiguous_report
        controller, client, _host = make(client, short_payload())
        controller.start_review()
        await settle(controller)
        first = client.review_calls[0]
        assert first["features"] == (AMB,) and first["resolutions"] is None
        finding = controller.payload["history"][-1]["report"]["findings"][0]
        assert finding["reason"] == "ambiguous_mention"
        # The file records the feature its results were made with.
        assert json.loads(serialize_workfile(controller.payload))["format"] == WORKFILE_FORMAT_V5

        outcome = controller.choose_document(finding, CANDIDATE)
        assert outcome.committed
        choice = controller.payload["choices"][0]
        assert choice["choice"] == sr.OFFICIAL_PAIR and choice["candidate"] == CANDIDATE
        assert controller.is_session_choice(choice)
        assert controller.document_choice_at(finding) == choice

        controller.start_review()
        await settle(controller)
        second = client.review_calls[1]
        report = controller.payload["history"][-2]["report"]
        assert second["resolutions"] == [{"mention_id": finding["id"], "choice": "official_pair",
                                          "candidate_id": CANDIDATE["candidate_id"]}]
        assert second["resolution_context"] == {
            "source_revision": report["source_revision"], "rule_version": "review-v2",
            "dictionary_revision": BASIS["dictionary"]["revision"], "matcher_revision": BASIS["matcher_revision"]}
        latest = controller.payload["history"][-1]
        assert latest["resolutions"] == second["resolutions"]
        assert latest["report"]["findings"][0]["verdict"] == "verified_constraint"
        # Undo and redo keep it this session's own choice.
        controller.undo()
        assert controller.payload["choices"] == []
        controller.redo()
        assert controller.is_session_choice(controller.payload["choices"][0])
    asyncio.run(scenario())


def test_a_refused_resolution_is_checked_once_more_without_it() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(AMB,))
        client.term = ambiguous_report
        controller, client, _host = make(client, short_payload())
        controller.start_review()
        await settle(controller)
        finding = controller.payload["history"][-1]["report"]["findings"][0]
        controller.choose_document(finding, None)
        assert controller.payload["choices"][0]["choice"] == sr.NOT_A_TERM
        client.fail["review_pair"] = [ClientError(ERROR_INVALID_REQUEST, status_code=400)]
        controller.start_review()
        await settle(controller)
        sent = [call["resolutions"] for call in client.review_calls]
        assert sent == [None, [{"mention_id": finding["id"], "choice": "not_a_term"}], None]
        assert controller.review_state == "current"
        assert controller.payload["history"][-1]["resolutions"] == []
        # The choice stays and waits for a report that names its place.
        assert controller.payload["choices"]
    asyncio.run(scenario())


def test_the_report_tells_this_sessions_choice_from_a_files() -> None:
    """``markdown_report.reconcile_choices``: a choice made in this session goes
    out with the next check; one read from a file stays on file."""
    from wuwaterm_client import markdown_report as mdr

    async def scenario() -> None:
        client = FakeClient(capabilities=(AMB,))
        client.term = ambiguous_report
        controller, client, _host = make(client, short_payload())
        controller.start_review()
        await settle(controller)
        finding = controller.payload["history"][-1]["report"]["findings"][0]
        controller.choose_document(finding, CANDIDATE)
        latest = {**controller.payload["history"][-1], "trusted": True}
        choices = controller.payload["choices"]
        mine = mdr.reconcile_choices(choices, controller.payload, latest, own=controller.is_session_choice)
        assert [(r["status"], r["code"]) for r in mine] == [("applicable", "position_basis_match")]
        as_file = mdr.reconcile_choices(choices, controller.payload, latest)
        assert [(r["status"], r["code"]) for r in as_file] == [("pending", "desktop_not_submitted")]
    asyncio.run(scenario())


def test_a_choice_read_from_a_file_is_never_sent() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(AMB,))
        client.term = ambiguous_report
        controller, client, _host = make(client, short_payload())
        controller.start_review()
        await settle(controller)
        finding = controller.payload["history"][-1]["report"]["findings"][0]
        controller.choose_document(finding, CANDIDATE)
        # Save and open again: the choice is the file's now, not this session's.
        reopened = parse_workfile(serialize_workfile(controller.payload))
        controller2, client2, _ = make(FakeClient(capabilities=(AMB,)), reopened)
        client2.term = ambiguous_report
        controller2.start_review()
        await settle(controller2)
        assert client2.review_calls[0]["resolutions"] is None
        assert reopened["review_features"] == [AMB]
    asyncio.run(scenario())


def test_without_the_capability_no_feature_is_asked_for() -> None:
    async def scenario() -> None:
        controller, client, _host = make(FakeClient(capabilities=()), short_payload())
        controller.start_review()
        await settle(controller)
        assert client.review_calls[0]["features"] == ()
        assert controller.review_features() == ()
    asyncio.run(scenario())


def test_segment_checks_ask_for_the_feature_and_store_it_with_the_result() -> None:
    async def scenario() -> None:
        client = FakeClient(capabilities=(AMB,))
        controller, client, _host = make(client, {**empty_workfile(), **manuscript(words={1: TERM})})
        controller.start_review()
        await settle(controller)
        assert client.review_calls and all(call["features"] == (AMB,) for call in client.review_calls)
        records = controller.records()
        assert [r["status"] for r in records] == ["done", "done", "done"]
        assert all(r["features"] == (AMB,) for r in records)
        written = json.loads(serialize_workfile(controller.payload))
        assert written["format"] == WORKFILE_FORMAT_V5 and written["review_features"] == [AMB]
        assert all("features" not in r for r in written["segments"])
        # The same results under a session without the feature are checked again.
        views = sr.RecordViews()
        assert all(views.get(r, controller.payload, controller.term_basis, ()).state == "stale" for r in records)
    asyncio.run(scenario())


def test_the_pairing_label_reaches_the_records() -> None:
    """MY-496 in the controller: a manuscript whose alignments leave its middle
    paragraph uncovered is paired with a gap, and its records say so."""
    payload = {**empty_workfile(), **manuscript()}
    source, target = payload["source"], payload["target"]
    paragraphs = source.split("\n\n")
    targets = target.split("\n\n")

    def span(text: str, part: str) -> dict:
        start = text.index(part)
        return {"start": start, "end": start + len(part), "text": part}
    payload["alignments"] = [{"source": span(source, paragraphs[i]), "target": span(target, targets[i])}
                             for i in (0, 2)]
    controller, _client, _host = make(FakeClient(), payload)
    records = controller.records()
    assert controller.pairs_version == GAP_SEGMENTATION_VERSION
    assert [p.status for p in controller.current_pairs()] == ["ready", "needs_manual_split", "ready"]
    assert {r["segmentation_version"] for r in records} == {"segmentation-v1-gaps"}
    assert json.loads(serialize_workfile(controller.payload))["format"] == WORKFILE_FORMAT_V5


def test_the_host_protocol_names_the_layer_one_render() -> None:
    assert isinstance(StubHost(), dc.WorkbenchHost)
    assert callable(dc.WorkbenchHost.render_structure)
