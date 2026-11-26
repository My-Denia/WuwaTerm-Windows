"""Pure-Python tests for in-editor search and scoped replace (no Qt)."""
from __future__ import annotations

import pytest

from wuwaterm_client.document_controller import DocumentRunController
from wuwaterm_client.document_edits import remap_splice
from wuwaterm_client.manuscript import empty_workfile
from wuwaterm_client.search_replace import (
    SearchMatch,
    SearchReplaceController,
    find_matches,
    next_match_index,
    replace_in_segment,
    replace_match,
)


class _DummyApi:
    is_configured = False


class _DummyHost:
    def editor_state(self) -> dict:
        return {"cursor": 0}

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


def test_find_matches_case_sensitivity_and_both_sides() -> None:
    text = "Echo echoes ECHO in the valley."
    insensitive = find_matches(text, "echo", side="target", case_sensitive=False)
    assert [(m.start, m.end, m.text) for m in insensitive] == [
        (0, 4, "Echo"),
        (5, 9, "echo"),
        (12, 16, "ECHO"),
    ]
    assert all(m.side == "target" for m in insensitive)

    sensitive = find_matches(text, "echo", side="source", case_sensitive=True)
    assert [(m.start, m.end, m.text) for m in sensitive] == [(5, 9, "echo")]
    assert sensitive[0].side == "source"

    assert find_matches(text, "", side="target") == []
    assert find_matches("", "echo", side="target") == []


def test_find_matches_literal_regex_metacharacters() -> None:
    text = "Check [HP] + (100%) and [hp]."
    matches = find_matches(text, "[hp]", side="target", case_sensitive=False)
    assert [(m.start, m.end, m.text) for m in matches] == [
        (6, 10, "[HP]"),
        (24, 28, "[hp]"),
    ]


def test_astral_unicode_scalar_offsets_and_replace() -> None:
    text = "😀 Camelia and 🚀 Camelia end"
    matches = find_matches(text, "Camelia", side="target")
    assert [(m.start, m.end, m.text) for m in matches] == [
        (2, 9, "Camelia"),
        (16, 23, "Camelia"),
    ]
    result = replace_match(text, matches[1], "Camellya")
    assert result.text == "😀 Camelia and 🚀 Camellya end"
    assert result.edit == ("target", 16, 23, len("Camellya"))
    assert result.replaced_count == 1
    assert result.new_cursor_offset == 16 + len("Camellya")


def test_next_and_prev_navigation_wraps_and_respects_cursor() -> None:
    text = "one two one three one"
    matches = find_matches(text, "one", side="target")
    assert [m.start for m in matches] == [0, 8, 18]

    assert next_match_index(matches, cursor_offset=5, forward=True) == 1
    assert next_match_index(matches, cursor_offset=19, forward=True) == 0
    assert next_match_index(matches, cursor_offset=10, forward=False) == 1
    assert next_match_index(matches, cursor_offset=0, forward=False) == 2

    ctrl = SearchReplaceController()
    ctrl.query = "one"
    ctrl.side = "target"
    ctrl.refresh(text, cursor_offset=0)
    assert ctrl.current_index == 0
    assert ctrl.step(forward=True).start == 8
    assert ctrl.step(forward=True).start == 18
    assert ctrl.step(forward=True).start == 0
    assert ctrl.step(forward=False).start == 18
    assert ctrl.step(forward=False).start == 8


def test_scoped_segment_search_and_replace_never_touches_other_segments() -> None:
    seg0 = "Camelia waited."
    seg1 = "Camelia smiled and Camelia waved."
    seg2 = "Camelia slept."
    text = f"{seg0}\n\n{seg1}\n\n{seg2}"
    ranges = [
        (0, len(seg0)),
        (len(seg0) + 2, len(seg0) + 2 + len(seg1)),
        (len(seg0) + 2 + len(seg1) + 2, len(text)),
    ]

    ctrl = SearchReplaceController()
    ctrl.query = "camelia"
    ctrl.side = "target"
    ctrl.scope = "segment"
    matches = ctrl.refresh(text, segment_ranges=ranges, scope_segment=1)
    assert len(matches) == 2
    assert all(m.segment_index == 1 for m in matches)

    scoped = ctrl.replace_scoped_segment(text, ranges[1], segment_index=1, replacement="Camellya")
    assert scoped is not None
    assert scoped.replaced_count == 2
    assert scoped.text == f"{seg0}\n\nCamellya smiled and Camellya waved.\n\n{seg2}"
    # Only segment 1 changed; segment 0 and segment 2 homographs remain untouched.
    assert scoped.text.startswith(seg0)
    assert scoped.text.endswith(seg2)
    assert scoped.edit[0] == "target"
    assert ranges[1][0] <= scoped.edit[1] <= scoped.edit[2] <= ranges[1][1]


def test_single_replace_and_scoped_replace_preserve_edit_history_and_alignments() -> None:
    source = "椿在城楼。\n\n椿穿过回廊。椿停下。"
    target = "Camelia at tower.\n\nCamelia walked. Camelia stopped."
    seg0_t_end = target.index("\n\n")
    seg1_t_start = seg0_t_end + 2
    alignments = [
        {
            "source": {"start": 0, "end": 5, "text": "椿在城楼。"},
            "target": {"start": 0, "end": seg0_t_end, "text": target[:seg0_t_end]},
        },
        {
            "source": {"start": 7, "end": len(source), "text": "椿穿过回廊。椿停下。"},
            "target": {"start": seg1_t_start, "end": len(target), "text": target[seg1_t_start:]},
        },
    ]

    doc = empty_workfile()
    doc.update(source=source, target=target, direction="en", alignments=alignments)
    controller = DocumentRunController(_DummyApi(), _DummyHost())
    controller.restore_draft(doc)

    ctrl = SearchReplaceController()
    ctrl.query = "Camelia"
    ctrl.side = "target"
    matches = ctrl.refresh(
        controller.payload["target"],
        segment_ranges=[(0, seg0_t_end), (seg1_t_start, len(target))],
    )
    assert len(matches) == 3

    # Single-occurrence replace on match 0 only
    res1 = replace_match(controller.payload["target"], matches[0], "Camellya")
    remap1 = remap_splice(
        controller.payload["alignments"], "target", controller.payload["source"], res1.text,
        res1.edit[1], res1.edit[2], res1.edit[3],
    )
    draft1 = controller.draft_snapshot()
    draft1["target"] = res1.text
    draft1["alignments"] = remap1.alignments
    outcome1 = controller.apply_edit(draft1, edits=[res1.edit], remap=remap1)
    assert outcome1.committed
    assert outcome1.invalidated == 0
    assert controller.payload["target"].startswith("Camellya at tower.")
    # Other occurrences untouched
    assert controller.payload["target"].count("Camelia") == 2

    # Scoped segment replace on segment 1 only
    seg1_range = (
        controller.payload["alignments"][1]["target"]["start"],
        controller.payload["alignments"][1]["target"]["end"],
    )
    res2 = replace_in_segment(
        controller.payload["target"],
        "Camelia",
        "Camellya",
        side="target",
        segment_range=seg1_range,
        segment_index=1,
    )
    assert res2 is not None and res2.replaced_count == 2
    remap2 = remap_splice(
        controller.payload["alignments"], "target", controller.payload["source"], res2.text,
        res2.edit[1], res2.edit[2], res2.edit[3],
    )
    draft2 = controller.draft_snapshot()
    draft2["target"] = res2.text
    draft2["alignments"] = remap2.alignments
    outcome2 = controller.apply_edit(draft2, edits=[res2.edit], remap=remap2)
    assert outcome2.committed
    assert outcome2.invalidated == 0
    assert controller.payload["target"] == "Camellya at tower.\n\nCamellya walked. Camellya stopped."

    # Undo reverses scoped replace in one step, then single replace in one step.
    assert controller.undo() is not None
    assert controller.payload["target"].count("Camelia") == 2
    assert controller.undo() is not None
    assert controller.payload["target"] == target
    assert controller.payload["alignments"] == alignments

    # Redo reapplies cleanly.
    assert controller.redo() is not None
    assert controller.payload["target"].startswith("Camellya at tower.")


def test_replace_match_rejects_stale_span() -> None:
    stale = SearchMatch(side="target", start=0, end=3, text="xyz")
    with pytest.raises(ValueError, match="stale"):
        replace_match("abc", stale, "def")
