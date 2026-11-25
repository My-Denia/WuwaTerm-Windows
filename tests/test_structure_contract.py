"""Layer 1 structure checks (``structure-check-v1``, Core ADR 0022): the wire
rules, request batching, the Layer 1 precedence rule and the session views.

Pure Python: requires neither PySide6, httpx nor keyring. The wire key sets are
compared with the pinned Core ``docs/api/openapi.json`` when
``WUWATERM_CORE_ROOT`` names a checkout.
"""

from __future__ import annotations

import copy
import json

import pytest

from wuwaterm_client import structure
from wuwaterm_client.segmentation import STATUS_READY, Region, SegmentPair


def _request(*segments: tuple[str, str, str | None]) -> dict:
    out = []
    for source, target, segment_id in segments:
        item = {"source": source, "target": target}
        if segment_id is not None:
            item["segment_id"] = segment_id
        out.append(item)
    return {"check_version": "structure-check-v1", "segments": out}


def _response(request: dict, findings: list[list[dict]] | None = None) -> dict:
    findings = findings or [[] for _ in request["segments"]]
    return {"request_id": "req-l1", "check_version": "structure-check-v1", "checks": list(structure.STRUCTURE_CHECKS),
            "segments": [{"index": i, "segment_id": seg.get("segment_id"), "findings": found, "omitted_findings": 0,
                          "layout": {"source_paragraphs": 1, "target_paragraphs": 1, "source_lines": 1,
                                     "target_lines": 1}}
                         for i, (seg, found) in enumerate(zip(request["segments"], findings, strict=True))]}


def test_the_wire_key_sets_are_the_pinned_core_schemas(contract_source) -> None:
    """Every object the client sends or reads has exactly the keys Core's
    published OpenAPI gives it, and the rule and check names are Core's."""
    openapi = json.loads((contract_source("core") / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
    schemas = openapi["components"]["schemas"]
    for name, keys in structure.WIRE_KEYS.items():
        assert set(schemas[name]["properties"]) == set(keys), name
        assert schemas[name]["additionalProperties"] is False, name
    finding_rules = schemas["StructureFindingBody"]["properties"]["rule_id"]["enum"]
    assert tuple(finding_rules) == structure.STRUCTURE_RULES
    assert tuple(schemas["StructureCheckResponseBody"]["properties"]["checks"]["items"]["enum"]) == \
        structure.STRUCTURE_CHECKS
    segments = schemas["StructureCheckRequestBody"]["properties"]["segments"]
    assert (segments["minItems"], segments["maxItems"]) == (1, structure.MAX_SEGMENTS)
    assert schemas["StructureSegmentResultBody"]["properties"]["findings"]["maxItems"] == structure.MAX_FINDINGS
    side = schemas["StructureCheckSegmentBody"]["properties"]["source"]["maxLength"]
    assert side == structure.MAX_SIDE_SCALARS
    assert "/v1/structure-checks" in openapi["paths"]


def test_a_request_is_what_core_accepts() -> None:
    assert structure.valid_request(_request(("原文", "译文", "s0"), ("a", "b", None)))
    assert not structure.valid_request(_request())  # no segments
    assert not structure.valid_request({**_request(("a", "b", None)), "extra": 1})
    assert not structure.valid_request({**_request(("a", "b", None)), "check_version": "structure-check-v2"})
    assert not structure.valid_request(_request(("a", "b", "s0"), ("c", "d", "s0")))  # repeated id
    assert not structure.valid_request(_request(("a", "b", "has space")))
    assert not structure.valid_request(_request(("a" * 2001, "b", None)))
    assert not structure.valid_request(_request(*[("一" * 2000, "", None)] * 11))  # 22,000 scalars
    assert not structure.valid_request(_request(*[("a", "b", None)] * 65))
    assert not structure.valid_request(_request(("\ud800", "b", None)))


def test_a_response_is_accepted_only_for_exactly_the_segments_sent() -> None:
    source, target = "价格是 100 元。", "The price is 10 yuan."
    request = _request((source, target, "s0"), ("一", "", "s1"))
    good = _response(request, [
        [{"rule_id": "number_missing", "source_span": {"start": 4, "end": 7, "text": "100"}, "target_span": None},
         {"rule_id": "number_unexpected", "source_span": None, "target_span": {"start": 13, "end": 15, "text": "10"}}],
        [{"rule_id": "empty_target", "source_span": None, "target_span": None}],
    ])
    assert structure.valid_response(good, request)

    def broken(change) -> dict:
        value = copy.deepcopy(good)
        change(value)
        return value
    assert not structure.valid_response(broken(lambda v: v.update(extra=1)), request)
    assert not structure.valid_response(broken(lambda v: v.update(check_version="structure-check-v2")), request)
    assert not structure.valid_response(broken(lambda v: v.update(checks=["numbers", "numbers"])), request)
    assert not structure.valid_response(broken(lambda v: v.update(checks=["spelling"])), request)
    assert not structure.valid_response(broken(lambda v: v.update(request_id=" ")), request)
    assert not structure.valid_response(broken(lambda v: v["segments"].pop()), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0].update(index=1)), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0].update(index=True)), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][1].update(segment_id="s9")), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0].update(omitted_findings=-1)), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0]["layout"].pop("target_lines")), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0]["findings"][0].update(
        rule_id="spelling")), request)
    # A span must be the exact text of the side as it was sent.
    assert not structure.valid_response(broken(lambda v: v["segments"][0]["findings"][0]["source_span"].update(
        text="101")), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0]["findings"][1]["target_span"].update(
        end=99)), request)
    assert not structure.valid_response(broken(lambda v: v["segments"][0].update(
        findings=[{"rule_id": "empty_target", "source_span": None, "target_span": None}] * 17)), request)


def test_batches_respect_the_segment_scalar_and_byte_limits() -> None:
    items = [(f"s{i}", "原文。", "Source.") for i in range(130)]
    batches, skipped = structure.batches(items)
    assert [len(b["segments"]) for b in batches] == [64, 64, 2] and not skipped
    assert all(structure.valid_request(b) for b in batches)
    # 20,000 scalars across a request.
    items = [(f"s{i}", "一" * 1500, "a" * 1500) for i in range(8)]
    batches, _ = structure.batches(items, max_bytes=10**9)
    assert [len(b["segments"]) for b in batches] == [6, 2]
    # Text at the scalar limits can be larger than Core's 64 KiB body (four
    # UTF-8 bytes per astral scalar): bytes decide too.
    items = [(f"s{i}", "\U00020000" * 2000, "\U00020001" * 2000) for i in range(4)]
    batches, skipped = structure.batches(items)
    assert all(len(structure.encode(b)) <= structure.BATCH_BYTES for b in batches)
    assert [len(b["segments"]) for b in batches] == [3, 1] and not skipped
    # The size the batcher adds up is the size of the bytes sent.
    for body in batches:
        assert len(structure.encode(body)) == len(json.dumps(body, ensure_ascii=False,
                                                            separators=(",", ":")).encode("utf-8"))
    # A side over 2,000 scalars, or a segment alone over the byte cap, is skipped.
    batches, skipped = structure.batches([("s0", "a" * 2001, "b"), ("s1", "ok", "ok"), ("s2", "汉" * 1500, "x")],
                                         max_bytes=2000)
    assert skipped == ["s0", "s2"] and [s["segment_id"] for b in batches for s in b["segments"]] == ["s1"]


def test_layer_one_wins_only_an_overlap_it_restates() -> None:
    finding = {"rule_id": "number_missing", "source_span": {"start": 10, "end": 13, "text": "100"},
               "target_span": None}
    assert structure.covers("quantity_change", {"start": 8, "end": 14}, None, finding)
    assert structure.covers("omission", {"start": 12, "end": 20}, None, finding)
    assert not structure.covers("quantity_change", {"start": 13, "end": 20}, None, finding)  # half-open
    assert not structure.covers("meaning_shift", {"start": 8, "end": 14}, None, finding)
    empty = {"rule_id": "empty_target", "source_span": None, "target_span": None}
    assert structure.covers("omission", None, None, empty)
    assert not structure.covers("quantity_change", None, None, empty)
    extra = {"rule_id": "placeholder_unexpected", "source_span": None,
             "target_span": {"start": 4, "end": 7, "text": "{0}"}}
    assert structure.covers("unsupported_addition", None, {"start": 0, "end": 5}, extra)
    assert not structure.covers("unsupported_addition", {"start": 0, "end": 5}, None, extra)
    for rule in ("paragraph_count_mismatch", "line_count_mismatch", "tag_unbalanced"):
        assert not structure.covers("omission", {"start": 0, "end": 99}, {"start": 0, "end": 99},
                                    {"rule_id": rule, "source_span": None, "target_span": None})


def test_a_result_describes_only_the_texts_it_was_made_for() -> None:
    source = "价格是 100 元。\n\n她走了。\n\n一行。\n二行。"
    target = "The price is 10 yuan.\n\nShe left.\n\nOne line. Two lines."

    def pairs_of(source: str, target: str) -> list[SegmentPair]:
        """One pair per paragraph (the pairing would merge these short ones)."""
        s_cuts = [0, source.index("走了") - 1, source.index("一行"), len(source)]
        t_cuts = [0, target.index("She"), target.index("One"), len(target)]
        out = []
        for i in range(3):
            regions = []
            for text, cuts in ((source, s_cuts), (target, t_cuts)):
                core = text[cuts[i]:cuts[i + 1]].rstrip()
                regions.append(Region(cuts[i], cuts[i + 1], cuts[i], cuts[i] + len(core)))
            out.append(SegmentPair(i, STATUS_READY, False, *regions))
        return out
    pairs = pairs_of(source, target)
    items = structure.items(pairs, source, target)
    assert [i.segment_id for i in items] == ["s0", "s1", "s2"]
    batches, _ = structure.batches((i.segment_id, i.source, i.target) for i in items)
    response = _response(batches[0], [
        [{"rule_id": "number_missing", "source_span": {"start": 4, "end": 7, "text": "100"}, "target_span": None}],
        [], [{"rule_id": "line_count_mismatch", "source_span": None, "target_span": None}]])
    state = structure.State()
    structure.store(state, batches[0], response, {i.segment_id: i.key for i in items})
    views = structure.views(pairs, source, target, state)
    assert [v.flagged for v in views] == [True, False, True]
    first = views[0].result["findings"][0]["source_span"]
    assert source[first["start"]:first["end"]] == "100"  # whole-text offsets
    assert structure.summary(views) == structure.Summary(checked=3, with_findings=2, findings=2, omitted=0,
                                                         unchecked=0)
    # Editing the second paragraph drops its result and keeps the others.
    edited = source.replace("她走了", "他走了")
    pairs = pairs_of(edited, target)
    views = structure.views(pairs, edited, target, state)
    assert [v.result is not None for v in views] == [True, False, True]
    assert structure.summary(views).unchecked == 1


@pytest.mark.parametrize("rule", structure.STRUCTURE_RULES)
def test_every_rule_has_a_label(rule: str) -> None:
    from wuwaterm_client import strings
    assert strings.MANUSCRIPT_L1_RULES[rule]
