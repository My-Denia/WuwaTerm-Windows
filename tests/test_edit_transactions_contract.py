"""The shared edit-transaction contract (contracts/workbench-state/edit-transactions.json).

Every case runs through the pure transaction (``document_edits.apply_edit_transaction``)
and, for free edits and direction changes, through the controller the
workbench commits with; every trace replays its undo, redo, typing and
composition steps on the controller with a stepped clock. No case may reach the
safety net that clears every alignment (``fallback_all``).

Pure Python: no Qt, no HTTP.
"""
from __future__ import annotations

import copy
import json

import pytest

from wuwaterm_client import document_controller as dc
from wuwaterm_client import document_edits as de
from wuwaterm_client.manuscript import empty_workfile, parse_workfile, serialize_workfile


@pytest.fixture
def fixture(contract_source) -> dict:
    path = contract_source("web") / "contracts" / "workbench-state" / "edit-transactions.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _Host:
    """The view's side of the protocol: only the editor state is asked for."""

    def editor_state(self) -> dict:
        return {}

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000_000.0

    def at(self, ms: float) -> None:
        self.now = 1_700_000_000_000.0 + ms

    def __call__(self) -> float:
        return self.now


def _loaded(draft: dict) -> tuple[dc.DocumentRunController, _Clock]:
    clock = _Clock()
    controller = dc.DocumentRunController(object(), _Host(), clock=clock)
    payload = empty_workfile()
    payload.update(source=draft["source"], target=draft["target"], direction=draft["direction"],
                   alignments=copy.deepcopy(draft["alignments"]))
    controller.payload = payload
    return controller, clock


def _draft(controller: dc.DocumentRunController) -> dict:
    payload = controller.payload
    return {key: payload[key] for key in ("source", "target", "direction", "alignments")}


def _snake(draft: dict, remap: de.AlignmentRemap) -> dict:
    return {
        "source": draft["source"], "target": draft["target"], "direction": draft["direction"],
        "alignments": draft["alignments"],
        "invalidated": list(remap.invalidated), "emptied_targets": list(remap.emptied_targets),
        "fallback_all": remap.fallback_all,
    }


def test_the_fixture_names_its_schema_reasons_and_undo_limits_as_windows_implements_them(fixture) -> None:
    assert fixture["schema"] == "wuwaterm-edit-transaction-cases-v1"
    assert fixture["reasons"] == list(de.INVALIDATION_REASONS)
    assert fixture["undo"] == {"limit": de.HISTORY_LIMIT, "coalesce_ms": de.TYPING_COALESCE_MS}
    assert len(fixture["cases"]) == 19 and len(fixture["traces"]) == 3
    covered = {rule for item in fixture["cases"] + fixture["traces"] for rule in item["rules"]}
    assert {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"} <= covered
    for reason in de.INVALIDATION_REASONS:
        assert any(x["reason"] == reason for c in fixture["cases"] for x in c["expect"]["invalidated"]), reason


def test_every_case_gives_the_expected_draft_and_entries_and_never_falls_back(fixture) -> None:
    for case in fixture["cases"]:
        draft, remap = de.apply_edit_transaction(case["draft"], case["transaction"])
        want = {key: value for key, value in case["expect"].items() if key != "alignment_notice"}
        assert _snake(draft, remap) == want, case["id"]
        assert remap.fallback_all is False, f"{case['id']}: fallback_all must not trigger"
        if case["transaction"]["kind"] != "direction":
            assert remap.notice(case["draft"]["alignments"]) == case["expect"]["alignment_notice"], case["id"]
        if case.get("splice"):
            side = case["splice"]["side"]
            start, old_end, new_len = de.compute_minimal_splice(case["draft"][side], case["expect"][side])
            assert {"side": side, "start": start, "old_end": old_end, "new_end": start + new_len} == case["splice"], (
                f"{case['id']}: splice")
        if case.get("utf16_splice"):
            side = case["splice"]["side"]

            def utf16(text: str, scalar: int) -> int:
                return len(text[:scalar].encode("utf-16-le")) // 2

            assert {"side": side, "start": utf16(case["draft"][side], case["splice"]["start"]),
                    "old_end": utf16(case["draft"][side], case["splice"]["old_end"]),
                    "new_end": utf16(case["expect"][side], case["splice"]["new_end"])} == case["utf16_splice"], (
                f"{case['id']}: UTF-16 reference")


def test_the_sixty_four_alignment_case_touches_one_entry_and_shifts_the_rest(fixture) -> None:
    case = next(c for c in fixture["cases"] if c["id"] == "sixty-four-alignments-one-affected")
    draft, remap = de.apply_edit_transaction(case["draft"], case["transaction"])
    assert len(draft["alignments"]) == 63
    assert [item["original_index"] for item in remap.invalidated] == [31]
    assert draft["alignments"][:31] == case["draft"]["alignments"][:31]
    assert ([item["target"]["text"] for item in draft["alignments"][31:]]
            == [item["target"]["text"] for item in case["draft"]["alignments"][32:]])


def test_free_edit_and_direction_cases_through_the_controller(fixture) -> None:
    """The controller commits the same alignments, says the same notice and keeps
    the same session list; one undo restores the draft and every entry."""
    for case in fixture["cases"]:
        tx = case["transaction"]
        if tx["kind"] not in ("edit", "direction"):
            continue
        controller, _clock = _loaded(case["draft"])
        if tx["kind"] == "edit":
            outcome = controller.edit_text(tx["side"], tx["text"])
        else:
            outcome = controller.set_direction(tx["direction"])
        expect = case["expect"]
        assert _draft(controller) == {key: expect[key] for key in ("source", "target", "direction", "alignments")}, (
            case["id"])
        assert outcome.committed, case["id"]
        assert outcome.alignment_notice == expect["alignment_notice"], f"{case['id']}: notice"
        assert controller.alignment_invalidations == expect["invalidated"], f"{case['id']}: session list"
        assert outcome.emptied_targets == len(expect["emptied_targets"]), case["id"]
        controller.undo()
        assert _draft(controller) == case["draft"], f"{case['id']}: undo"
        assert controller.alignment_invalidations == [], f"{case['id']}: undo session list"
        controller.redo()
        assert controller.alignment_invalidations == expect["invalidated"], f"{case['id']}: redo session list"


def test_an_editor_reported_range_wider_than_the_change_is_narrowed_before_judging(fixture) -> None:
    """Qt can report a whole block for a one-character edit; the transaction
    judges only the text that changed, so nothing is invalidated by accident."""
    case = next(c for c in fixture["cases"] if c["id"] == "source-inside-edit-resizes-and-shifts")
    controller, _clock = _loaded(case["draft"])
    before = case["draft"]["source"]
    after = case["transaction"]["text"]
    controller.edit_text("source", after, edit=(0, len(before), len(after)))
    assert controller.payload["alignments"] == case["expect"]["alignments"]
    assert controller.alignment_invalidations == []


def test_a_saved_manuscript_holds_only_the_kept_alignments(fixture) -> None:
    """R9: the session list never reaches the workfile."""
    for case in fixture["cases"]:
        tx = case["transaction"]
        if tx["kind"] != "edit" or not case["expect"]["invalidated"]:
            continue
        controller, _clock = _loaded(case["draft"])
        controller.edit_text(tx["side"], tx["text"])
        assert controller.alignment_invalidations
        written = serialize_workfile(controller.payload)
        assert parse_workfile(written)["alignments"] == case["expect"]["alignments"], case["id"]
        for word in ("invalidat", "original_index", "fallback", "emptied"):
            assert word not in written, case["id"]


def test_every_trace_steps_undo_redo_typing_and_composition_through_the_controller(fixture) -> None:
    for trace in fixture["traces"]:
        controller, clock = _loaded(trace["initial"])
        notice = None
        for index, step in enumerate(trace["steps"]):
            label = f"{trace['id']} step {index} ({step['op']})"
            if step["op"] == "edit":
                clock.at(step["at_ms"])
                outcome = controller.edit_text(step["side"], step["text"], composing=step.get("composing", False))
                notice = outcome.alignment_notice
            elif step["op"] == "undo":
                controller.undo()
            elif step["op"] == "redo":
                controller.redo()
            else:
                assert step["op"] == "check", label
            expect = step.get("expect")
            if not expect:
                continue
            payload = controller.payload
            if "undo_depth" in expect:
                assert len(controller.history.undo) == expect["undo_depth"], f"{label}: undo depth"
            if "redo_depth" in expect:
                assert len(controller.history.redo) == expect["redo_depth"], f"{label}: redo depth"
            if "alignments_count" in expect:
                assert len(payload["alignments"]) == expect["alignments_count"], f"{label}: alignments"
            if "alignments" in expect:
                assert payload["alignments"] == expect["alignments"], f"{label}: alignments"
            if "session_invalidated" in expect:
                assert ([item["reason"] for item in controller.alignment_invalidations]
                        == expect["session_invalidated"]), f"{label}: session"
            for key in ("source", "target"):
                if key in expect:
                    assert payload[key] == expect[key], f"{label}: {key}"
            if "alignment_notice" in expect:
                assert notice == expect["alignment_notice"], f"{label}: notice"


def test_a_composition_judges_alignments_against_the_text_before_it() -> None:
    draft = {
        "source": "Alpha Beta", "target": "甲 乙", "direction": "en",
        "alignments": [
            {"source": {"start": 0, "end": 5, "text": "Alpha"}, "target": {"start": 0, "end": 1, "text": "甲"}},
            {"source": {"start": 6, "end": 10, "text": "Beta"}, "target": {"start": 2, "end": 3, "text": "乙"}},
        ],
    }
    controller, _clock = _loaded(draft)
    # An intermediate state crosses a boundary on its own...
    controller.edit_text("source", "AlphaXeta", composing=True)
    assert len(controller.payload["alignments"]) == 1
    # ...but the committed text only inserts inside Alpha.
    controller.edit_text("source", "AlpXha Beta", composing=True)
    controller.edit_text("source", "AlpXha Beta")
    assert controller.payload["alignments"] == [
        {"source": {"start": 0, "end": 6, "text": "AlpXha"}, "target": {"start": 0, "end": 1, "text": "甲"}},
        {"source": {"start": 7, "end": 11, "text": "Beta"}, "target": {"start": 2, "end": 3, "text": "乙"}},
    ]
    assert controller.alignment_invalidations == []
    assert len(controller.history.undo) == 1
    controller.undo()
    assert _draft(controller) == draft
