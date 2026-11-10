"""segmentation-v1 port: Core's shared fixtures plus the client helpers.

Pure stdlib; must run without Qt, httpx or keyring.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from wuwaterm_client import segmentation as seg

ASYMMETRIC = (
    "asymmetric_source_over_limit_no_boundary",
    "asymmetric_target_over_limit_no_boundary",
    "asymmetric_source_over_limit_with_commas",
    "asymmetric_target_over_limit_with_commas",
)


def _load(contract_source):
    path = contract_source("core") / "contracts" / "segmentation-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def pytest_generate_tests(metafunc):
    # Case names are needed at collection time, so read the fixture directly
    # when the Core checkout is configured; the tests themselves still go
    # through contract_source for the pin check and the required-or-skip rule.
    import os
    from pathlib import Path

    root = os.environ.get("WUWATERM_CORE_ROOT")
    data = None
    if root:
        path = Path(root) / "contracts" / "segmentation-v1.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
    for fixture, key in (("segment_name", "segment_cases"), ("pair_name", "pair_cases")):
        if fixture in metafunc.fixturenames:
            names = [c["name"] for c in data[key]] if data else ["<unconfigured>"]
            metafunc.parametrize(fixture, names)


def _case(contract_source, key, name):
    data = _load(contract_source)
    for case in data[key]:
        if case["name"] == name:
            return case
    pytest.fail(f"fixture case {name} not found")


def _pairs(case):
    return seg.pair_segments(case["source"], case["target"], seg.SEGMENT_LIMIT, case["alignments"])


def test_fixture_header_matches_module_constants(contract_source):
    data = _load(contract_source)
    assert data["version"] == "segmentation-v1"
    assert data["limit"] == seg.SEGMENT_LIMIT
    assert data["hard_cut_window"] == seg.HARD_CUT_WINDOW
    assert data["boundary_punctuation"] == seg.BOUNDARY_PUNCTUATION
    assert data["whitespace"] == sorted(ord(c) for c in seg.WHITESPACE)


def test_segment_case_matches_fixture(contract_source, segment_name):
    case = _case(contract_source, "segment_cases", segment_name)
    segments = seg.segment_text(case["text"], seg.SEGMENT_LIMIT)
    assert [s.as_dict() for s in segments] == case["segments"]
    # Partition: contiguous cover whose concatenation is the text.
    text = case["text"]
    pos = 0
    for s in segments:
        assert s.start == pos and s.end >= s.start
        pos = s.end
    assert pos == len(text)
    assert "".join(text[s.start : s.end] for s in segments) == text


def test_pair_case_matches_fixture(contract_source, pair_name):
    case = _case(contract_source, "pair_cases", pair_name)
    pairs = _pairs(case)
    assert [p.as_dict() for p in pairs] == case["pairs"]
    for side, text in (("source", case["source"]), ("target", case["target"])):
        pos = 0
        for p in pairs:
            region = getattr(p, side)
            assert region.start == pos
            pos = region.end
        if pairs:
            assert pos == len(text)
    for p in pairs:
        if p.status == seg.STATUS_READY:
            for region in (p.source, p.target):
                assert 0 < region.core_end - region.core_start <= seg.SEGMENT_LIMIT
            assert seg.pair_is_sendable(p, case["source"], case["target"])


@pytest.mark.parametrize("name", ASYMMETRIC)
def test_asymmetric_cases(contract_source, name):
    case = _case(contract_source, "pair_cases", name)
    pairs = _pairs(case)
    if name.endswith("no_boundary"):
        assert [p.status for p in pairs] == [seg.STATUS_NEEDS_MANUAL_SPLIT]
    else:
        assert [p.status for p in pairs] == [seg.STATUS_READY, seg.STATUS_READY]
        assert all(p.hard_cut for p in pairs)
    for p in pairs:
        if p.status == seg.STATUS_READY:
            assert p.source.core_end - p.source.core_start <= seg.SEGMENT_LIMIT
            assert p.target.core_end - p.target.core_start <= seg.SEGMENT_LIMIT


def _ready_pair(source: str, target: str) -> seg.SegmentPair:
    pairs = seg.pair_segments(source, target)
    assert [p.status for p in pairs] == [seg.STATUS_READY]
    return pairs[0]


def test_pair_is_sendable_accepts_a_ready_pair():
    assert seg.pair_is_sendable(_ready_pair("你好。", "Hello."), "你好。", "Hello.")


@pytest.mark.parametrize(
    ("source", "target", "status"),
    [
        ("你好。", "", seg.STATUS_UNTRANSLATED),
        ("", "Hello.", seg.STATUS_UNPAIRED_TARGET),
        ("甲" * 2000, "x" * 2000, seg.STATUS_NEEDS_MANUAL_SPLIT),
    ],
)
def test_pair_is_sendable_rejects_non_ready_statuses(source, target, status):
    pairs = seg.pair_segments(source, target)
    assert [p.status for p in pairs] == [status]
    assert not seg.pair_is_sendable(pairs[0], source, target)


def test_pair_is_sendable_rejects_hand_built_violations():
    source, target = "你好。", "Hello."
    good = _ready_pair(source, target)
    over = replace(good, source=replace(good.source, core_end=good.source.core_start + 2))
    assert seg.pair_is_sendable(over, source, target)  # still inside the limit
    assert not seg.pair_is_sendable(over, source, target, limit=1)
    long = "甲" * (seg.SEGMENT_LIMIT + 1)
    forged = seg.SegmentPair(
        0, seg.STATUS_READY, False,
        seg.Region(0, len(long), 0, len(long)), seg.Region(0, 6, 0, 6),
    )
    assert not seg.pair_is_sendable(forged, long, target)
    empty_core = replace(good, target=replace(good.target, core_end=good.target.core_start))
    assert not seg.pair_is_sendable(empty_core, source, target)
    outside = replace(good, target=replace(good.target, end=len(target) + 5))
    assert not seg.pair_is_sendable(outside, source, target)


def test_utf16_helpers_with_astral_and_crlf():
    text = "a😀b\r\nc"
    assert [seg.utf16_index(text, i) for i in range(len(text) + 1)] == [0, 1, 3, 4, 5, 6, 7]
    assert seg.scalar_index(text, 0) == 0
    assert seg.scalar_index(text, 1) == 1
    assert seg.scalar_index(text, 2) is None  # inside the surrogate pair
    assert seg.scalar_index(text, 3) == 2
    assert seg.scalar_index(text, 5) == 4  # LF after CR
    assert seg.scalar_index(text, 7) == len(text)
    assert seg.scalar_index(text, 8) is None
    assert seg.scalar_index(text, -1) is None
    for i in range(len(text) + 1):
        assert seg.scalar_index(text, seg.utf16_index(text, i)) == i
    with pytest.raises(IndexError):
        seg.utf16_index(text, len(text) + 1)
