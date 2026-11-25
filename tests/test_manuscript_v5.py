"""Manuscript v5 (``wuwaterm-manuscript-v5``), gap-tolerant segment records and
the review features a result was made with.

Pure Python: requires neither PySide6 nor keyring, and uses only synthetic /
public Wuthering Waves game terms. The Web fixture cases run when
``WUWATERM_WEB_ROOT`` names a WuwaTerm-Web checkout; every other test here runs
without one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from wuwaterm_client import manuscript
from wuwaterm_client import segment_records as sr
from wuwaterm_client import structure
from wuwaterm_client.segmentation import (
    GAP_SEGMENTATION_VERSION, SEGMENTATION_VERSION, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY, STATUS_UNPAIRED_TARGET,
    STATUS_UNTRANSLATED, alignments_leave_gaps, pair_segments, pair_segments_with_gaps,
)

AMB = manuscript.REVIEW_FEATURE_AMBIGUOUS_MENTION
BASIS = {
    "rule_version": "review-v2",
    "dictionary": {"schema_version": "1", "source_commit": "a" * 40, "term_count": 12, "revision": "b" * 64},
    "matcher_revision": "c" * 64,
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cand(zh: str = "椿", en: str = "Camellya", category: str = "resonator") -> dict:
    return {"candidate_id": _sha(f"{zh}|{en}|{category}"), "zh": zh, "en": en, "category": category}


def _err(raw: str) -> str:
    with pytest.raises(manuscript.WorkfileError) as exc_info:
        manuscript.parse_workfile(raw)
    return exc_info.value.code


def _doc_finding(source: str, start: int, end: int, *, verdict: str = "needs_review", reason: str | None = None,
                 target: dict | None = None) -> dict:
    span = {"start": start, "end": end, "text": source[start:end]}
    item = {"id": f"{start}:{end}:{span['text']}", "rule_id": "review.term_pair", "verdict": verdict,
            "source_span": span, "target_span": target,
            "candidates": [{"zh": "椿", "en": "Camellya", "category": "resonator",
                            "sources": [{"source_file": "TextMap/zh/MultiText.json", "source_id": "term_1"}],
                            "candidate_id": _cand()["candidate_id"]}],
            "candidates_truncated": False}
    if reason is not None:
        item["reason"] = reason
    return item


def _doc_report(source: str, target: str, findings: list[dict]) -> dict:
    not_eval = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    return {"request_id": "req-v5", "source_revision": _sha(source), "target_revision": _sha(target),
            "rule_version": "review-v2", "dictionary": copy.deepcopy(BASIS["dictionary"]),
            "coverage": {"evaluated": len(findings) - not_eval, "not_evaluated": not_eval,
                         "rules": ["review.term_pair", "review.sentence_meaning"]},
            "findings": findings, "truncated": False, "matcher_revision": BASIS["matcher_revision"]}


def _document_payload(findings_reason: str | None = "ambiguous_mention") -> dict:
    source, target = "椿与今汐同行。椿在前面。", "Camellya walked with Jinhsi. She led."
    findings = [_doc_finding(source, 0, 1, verdict="verified_constraint",
                             target={"start": 0, "end": 8, "text": "Camellya"})]
    if findings_reason is not None:
        findings.append(_doc_finding(source, 7, 8, verdict="not_evaluated", reason=findings_reason))
    payload = manuscript.empty_workfile()
    payload.update(source=source, target=target)
    payload["history"] = [{"source": source, "target": target, "direction": "en", "alignments": None,
                           "report": _doc_report(source, target, findings), "resolutions": []}]
    return payload


def _record(index: int, source: str, target: str, pair, findings: list[dict], *, status: str = "done",
            version: str = SEGMENTATION_VERSION, alignment_input: str = "automatic") -> dict:
    a, b = pair.source.core_start, pair.source.core_end
    c, d = pair.target.core_start, pair.target.core_end
    not_eval = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    live = status != "pending"
    return {"index": index, "segmentation_version": version, "source_start": a, "source_end": b,
            "target_start": c, "target_end": d, "source_hash": _sha(source[a:b]), "target_hash": _sha(target[c:d]),
            "status": status, "direction": "en" if live else None,
            "alignment_input": alignment_input if live else None,
            "basis": copy.deepcopy(BASIS) if live else None,
            "result": {"truncated": False, "evaluated": len(findings) - not_eval, "not_evaluated": not_eval,
                       "findings": findings} if live else None,
            "choices": []}


# Three paragraphs; every side over 2,000 scalars in all, so the manuscript is long.
PARAGRAPHS_SOURCE = ["今汐在城楼远望。" * 90, "椿穿过回廊，声骸低鸣。" * 70, "守岸人负责回能。" * 90]
PARAGRAPHS_TARGET = ["Jinhsi looked afar from the tower. " * 30, "Camellya crossed the corridor. " * 34,
                     "The Shorekeeper restored energy. " * 32]


def _long_draft() -> tuple[str, str, list[dict]]:
    source = "\n\n".join(PARAGRAPHS_SOURCE)
    target = "\n\n".join(PARAGRAPHS_TARGET)

    def span(text: str, part: str) -> dict:
        start = text.index(part)
        return {"start": start, "end": start + len(part), "text": part}
    alignments = [{"source": span(source, s), "target": span(target, t)}
                  for s, t in zip(PARAGRAPHS_SOURCE, PARAGRAPHS_TARGET, strict=True)]
    return source, target, alignments


# ---- the Web fixture index ---------------------------------------------------
# ``contracts/sources.json`` pins these files; the case list is needed at
# collection time, so it is read from the checkout ``WUWATERM_WEB_ROOT`` names.

def _v5_root() -> Path | None:
    root = os.environ.get("WUWATERM_WEB_ROOT")
    if not root:
        return None
    candidate = Path(root) / "contracts" / "manuscript-v5"
    return candidate if (candidate / "cases.json").is_file() else None


def _v5_cases() -> list[dict]:
    root = _v5_root()
    if root is None:
        return []
    data = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    return ([{**case, "valid": True} for case in data["valid"]]
            + [{**case, "valid": False} for case in data["invalid"]])


def _case_id(case: dict | None) -> str:
    if case is None:
        return "<unconfigured>"
    pad = f"+pad{case['pad_to_bytes']}" if case.get("pad_to_bytes") else ""
    return f'{case["file"]}{pad}'


def pytest_generate_tests(metafunc):
    if "v5_case" in metafunc.fixturenames:
        cases = _v5_cases()
        metafunc.parametrize("v5_case", cases or [None], ids=[_case_id(c) for c in cases] or ["<unconfigured>"])


def _fixture_bytes(root: Path, case: dict) -> str:
    raw = (root / case["file"]).read_text(encoding="utf-8")
    pad = case.get("pad_to_bytes")
    if pad:
        raw += " " * (pad - len(raw.encode("utf-8")))
    return raw


def test_web_v5_fixture_is_canonical_bytes_or_exact_error_code(v5_case, contract_source) -> None:
    root = contract_source("web") / "contracts" / "manuscript-v5"
    raw = _fixture_bytes(root, v5_case)
    if v5_case["valid"]:
        document = manuscript.parse_workfile(raw)
        canonical = (root / v5_case["canonical"]).read_text(encoding="utf-8")
        assert manuscript.serialize_workfile(document) == canonical, v5_case["note"]
    else:
        assert _err(raw) == v5_case["error"], v5_case["note"]


def test_web_v5_fixture_index_is_complete(contract_source) -> None:
    """Every fixture file is a listed case: 11 valid (one padded) and 16 invalid."""
    root = contract_source("web") / "contracts" / "manuscript-v5"
    cases = _v5_cases()
    listed = {case["file"] for case in cases}
    on_disk = {path.name for path in root.glob("*.json")} - {"cases.json", "schema.json"}
    assert on_disk <= listed, sorted(on_disk - listed)
    assert len([c for c in cases if c["valid"]]) == 11
    assert len([c for c in cases if not c["valid"]]) == 16
    index = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    assert tuple(index["reasons"]) == manuscript.REASON_VALUES_V5
    assert tuple(index["review_features"]) == manuscript.REVIEW_FEATURE_VALUES
    assert tuple(index["segmentation_versions"]) == manuscript.SEGMENTATION_VERSIONS_V5
    assert index["limits"]["review_features"] == manuscript.MAX_REVIEW_FEATURES


def test_web_gap_fixture_records_are_this_pairing(contract_source) -> None:
    """The port of Web ``pairSegmentsWithGaps`` cuts the fixture's draft into
    exactly the ranges and the label its records carry."""
    root = contract_source("web") / "contracts" / "manuscript-v5"
    document = manuscript.parse_workfile((root / "valid-v5-gaps.json").read_text(encoding="utf-8"))
    pairs, version = pair_segments_with_gaps(document["source"], document["target"],
                                             alignments=document["alignments"])
    assert version == GAP_SEGMENTATION_VERSION
    assert [(p.source.core_start, p.source.core_end, p.target.core_start, p.target.core_end) for p in pairs] == [
        (r["source_start"], r["source_end"], r["target_start"], r["target_end"]) for r in document["segments"]]
    assert {r["segmentation_version"] for r in document["segments"]} == {version}
    assert [p.status for p in pairs] == [STATUS_READY, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY]


# ---- v4 stays strict ----------------------------------------------------------

def test_the_v4_reader_still_refuses_each_v5_addition() -> None:
    """A file labelled v4 may hold none of the three v5 additions."""
    written = json.loads(manuscript.serialize_workfile(_document_payload()))
    assert written["format"] == manuscript.WORKFILE_FORMAT_V5
    assert written["review_features"] == [AMB]
    as_v4 = {**written, "format": manuscript.WORKFILE_FORMAT_V4}
    assert _err(json.dumps(as_v4, ensure_ascii=False)) == "workfile_unsupported_format"  # 14 keys
    del as_v4["review_features"]
    assert _err(json.dumps(as_v4, ensure_ascii=False)) == "workfile_invalid_content"  # the reason


def test_v1_to_v4_files_still_read_and_carry_no_features() -> None:
    v1 = manuscript.serialize_workfile(_document_payload(findings_reason=None))
    assert json.loads(v1)["format"] == manuscript.WORKFILE_FORMAT
    assert "review_features" not in manuscript.parse_workfile(v1)
    v4 = manuscript.serialize_workfile(_document_payload(findings_reason="ordinary_word"))
    assert json.loads(v4)["format"] == manuscript.WORKFILE_FORMAT_V4
    assert "review_features" not in manuscript.parse_workfile(v4)


# ---- the writer (v5 SPEC "Writing") -------------------------------------------

def test_an_ambiguous_finding_alone_makes_the_file_v5_and_records_the_feature() -> None:
    content = manuscript.serialize_workfile(_document_payload())
    value = json.loads(content)
    assert list(value)[-1] == "review_features" and len(value) == 14
    assert value["review_features"] == [AMB]
    assert manuscript.serialize_workfile(manuscript.parse_workfile(content)) == content


def _long_payload(records_features: list | None, *, file_features: list | None = None,
                  ambiguous: bool = False) -> tuple[dict, list]:
    source, target, alignments = _long_draft()
    pairs = pair_segments(source, target)
    assert len(pairs) == 3 and all(p.status == STATUS_READY for p in pairs)
    records = []
    for i, pair in enumerate(pairs):
        findings = []
        if ambiguous and i == 1:
            core = source[pair.source.core_start:pair.source.core_end]
            at = core.index("椿")
            findings = [{"verdict": "not_evaluated", "source": [at, at + 1], "target": None,
                         "candidates": [_cand()], "candidates_truncated": False, "reason": "ambiguous_mention"}]
        record = _record(i, source, target, pair, findings)
        if records_features is not None and records_features[i] is not None:
            record["features"] = records_features[i]
        records.append(record)
    payload = manuscript.empty_workfile()
    payload.update(source=source, target=target, segments=records, segment_choice_history=[],
                   progress={"current": None, "completed": []})
    if file_features is not None:
        payload["review_features"] = file_features
    return payload, pairs


def test_records_made_under_the_feature_make_v5_and_mixed_features_write_stale() -> None:
    # Every done record made with the feature: v5, all done.
    payload, _ = _long_payload([[AMB], [AMB], [AMB]])
    value = json.loads(manuscript.serialize_workfile(payload))
    assert value["format"] == manuscript.WORKFILE_FORMAT_V5 and value["review_features"] == [AMB]
    assert [r["status"] for r in value["segments"]] == ["done", "done", "done"]
    # The runtime key never reaches the file.
    assert all("features" not in r for r in value["segments"])
    # One record re-checked with the feature, two read from a v4 file: the two
    # are written stale, so no reader reuses them under the feature.
    payload, _ = _long_payload([None, [AMB], None])
    value = json.loads(manuscript.serialize_workfile(payload))
    assert value["review_features"] == [AMB]
    assert [r["status"] for r in value["segments"]] == ["stale", "done", "stale"]
    # No feature anywhere: the lowest format, v3.
    payload, _ = _long_payload([None, None, None])
    assert json.loads(manuscript.serialize_workfile(payload))["format"] == manuscript.WORKFILE_FORMAT_V3
    # A file's features describe records without their own key.
    payload, _ = _long_payload(None, file_features=[AMB])
    value = json.loads(manuscript.serialize_workfile(payload))
    assert value["review_features"] == [AMB] and [r["status"] for r in value["segments"]] == ["done"] * 3


def test_an_ambiguous_record_finding_needs_the_feature_on_read() -> None:
    payload, _ = _long_payload([[AMB], [AMB], [AMB]], ambiguous=True)
    content = manuscript.serialize_workfile(payload)
    assert manuscript.serialize_workfile(manuscript.parse_workfile(content)) == content
    value = json.loads(content)
    value["review_features"] = []
    assert _err(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) == "workfile_segment_invalid"


# ---- review features decide reuse (v5 SPEC "Review features") ----------------

def test_a_result_is_current_only_under_the_features_it_was_made_with() -> None:
    v4_payload, pairs = _long_payload(None)  # read from a v4 file: no features
    record = v4_payload["segments"][0]
    assert sr.record_view(record, v4_payload, BASIS, ()).state == "current"
    # The session asks for ambiguous-mention-v1: checked again, once.
    assert sr.record_view(record, v4_payload, BASIS, (AMB,)).state == "stale"
    # Capabilities unknown: never current, a reuse candidate at most.
    assert sr.record_view(record, v4_payload, BASIS, None).state == "saved"
    v5_payload, _ = _long_payload(None, file_features=[AMB])
    assert sr.record_view(v5_payload["segments"][0], v5_payload, BASIS, (AMB,)).state == "current"
    assert sr.record_view(v5_payload["segments"][0], v5_payload, BASIS, ()).state == "stale"
    # The plan follows the same rule, and the cached views see a feature change.
    views = sr.RecordViews()
    assert sr.term_plan(v4_payload, pairs, BASIS, views=views, features=()).due == ()
    assert sr.term_plan(v4_payload, pairs, BASIS, views=views, features=(AMB,)).due == (0, 1, 2)


def test_a_stored_response_carries_the_features_it_was_asked_with() -> None:
    payload, pairs = _long_payload(None)
    payload["segments"] = [{**r, "status": "stale"} for r in payload["segments"]]
    request = sr.plan_request(payload, 1, BASIS, features=(AMB, "unknown-feature"))
    assert request.features == (AMB,)
    core = request.source
    at = core.index("椿")
    span = {"start": at, "end": at + 1, "text": "椿"}
    report = _doc_report(request.source, request.target, [
        {**_doc_finding(core, at, at + 1, verdict="not_evaluated", reason="ambiguous_mention"), "source_span": span}])
    applied = sr.apply_response(payload, request, report, BASIS)
    assert applied.outcome == "stored"
    stored = applied.parts.segments[1]
    assert stored["features"] == (AMB,)
    assert stored["result"]["findings"][0]["reason"] == "ambiguous_mention"
    after = sr.with_parts(payload, applied.parts)
    assert sr.is_current(stored, after, BASIS, (AMB,))
    assert not sr.is_current(stored, after, BASIS, ())


def test_ambiguous_names_are_no_place_in_a_term_group_and_are_listed_apart() -> None:
    payload, pairs = _long_payload([[AMB], [AMB], [AMB]], ambiguous=True)
    groups = sr.group_term_occurrences(payload["segments"], pairs, payload["source"], payload["target"], "en",
                                       basis=BASIS, features=(AMB,))
    assert groups == []
    places = sr.ambiguous_places(payload["segments"], pairs, payload["source"], payload["target"], "en",
                                 basis=BASIS, features=(AMB,))
    assert [(p["segment_number"], p["source_span"]["text"], p["freshness"]) for p in places] == [(2, "椿", "current")]
    assert payload["source"][places[0]["source_span"]["start"]] == "椿"


# ---- segmentation-v1-gaps (MY-496) -------------------------------------------

def _span(text: str, part: str) -> dict:
    start = text.index(part)
    return {"start": start, "end": start + len(part), "text": part}


def test_gap_pairing_matches_segmentation_v1_when_alignments_cover_the_text() -> None:
    source, target = "甲一。\n\n甲二。\n\n甲三。", "One.\n\nTwo.\n\nThree."
    covering = [{"source": _span(source, s), "target": _span(target, t)}
                for s, t in (("甲一。", "One."), ("甲二。", "Two."), ("甲三。", "Three."))]
    for alignments in (None, [], covering):
        pairs, version = pair_segments_with_gaps(source, target, alignments=alignments)
        assert version == SEGMENTATION_VERSION
        assert pairs == pair_segments(source, target, alignments=alignments)
        assert not alignments_leave_gaps(source, target, alignments)


def test_a_gap_keeps_every_aligned_region_and_is_never_ready() -> None:
    """Web ``review-features.test.mjs``: the same cases, the same answers."""
    source, target = "甲一。\n\n甲二。\n\n甲三。", "One.\n\nTwo.\n\nThree."
    covering = [{"source": _span(source, s), "target": _span(target, t)}
                for s, t in (("甲一。", "One."), ("甲二。", "Two."), ("甲三。", "Three."))]
    gapped = [covering[0], covering[2]]
    assert [p.status for p in pair_segments(source, target, alignments=gapped)] == [STATUS_NEEDS_MANUAL_SPLIT]
    pairs, version = pair_segments_with_gaps(source, target, alignments=gapped)
    assert version == GAP_SEGMENTATION_VERSION
    assert [p.status for p in pairs] == [STATUS_READY, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY]
    assert [(source[p.source.core_start:p.source.core_end], target[p.target.core_start:p.target.core_end])
            for p in pairs] == [("甲一。", "One."), ("甲二。", "Two."), ("甲三。", "Three.")]
    # Source text only in the gap: untranslated. Target text only: unpaired target.
    short = "One.\n\nThree."
    pairs, _ = pair_segments_with_gaps(source, short, alignments=[
        {"source": _span(source, "甲一。"), "target": _span(short, "One.")},
        {"source": _span(source, "甲三。"), "target": _span(short, "Three.")}])
    assert [p.status for p in pairs] == [STATUS_READY, STATUS_UNTRANSLATED, STATUS_READY]
    extra, two = "One.\n\nExtra.\n\nThree.", "甲一。\n\n甲三。"
    pairs, _ = pair_segments_with_gaps(two, extra, alignments=[
        {"source": _span(two, "甲一。"), "target": _span(extra, "One.")},
        {"source": _span(two, "甲三。"), "target": _span(extra, "Three.")}])
    assert [p.status for p in pairs] == [STATUS_READY, STATUS_UNPAIRED_TARGET, STATUS_READY]
    # A target gap next to an alignment without a target: one manual gap.
    with_null = [covering[0], {"source": _span(source, "甲二。"), "target": None}, covering[2]]
    pairs, _ = pair_segments_with_gaps(source, target, alignments=with_null)
    assert [p.status for p in pairs] == [STATUS_READY, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY]


def test_one_invalidated_alignment_leaves_the_other_records_done() -> None:
    """MY-496: an edit that invalidates one alignment of three used to make the
    whole manuscript one needs_manual_split pair, so every record went pending.
    Now only the gap is a region of its own: the two aligned records keep their
    results, and the file records the pairing as segmentation-v1-gaps."""
    source, target, alignments = _long_draft()
    pairs, version = pair_segments_with_gaps(source, target, alignments=alignments)
    assert version == SEGMENTATION_VERSION and [p.status for p in pairs] == [STATUS_READY] * 3
    inputs = [sr.alignment_input(alignments, source, target, (p.source.core_start, p.source.core_end),
                                 (p.target.core_start, p.target.core_end)) for p in pairs]
    payload = manuscript.empty_workfile()
    payload.update(source=source, target=target, alignments=alignments,
                   segments=[_record(i, source, target, p, [], alignment_input=inputs[i]) for i, p in enumerate(pairs)],
                   segment_choice_history=[], progress={"current": None, "completed": [0, 1, 2]})
    parts = sr.normalize(payload, pairs, BASIS)
    assert [r["status"] for r in parts.segments] == ["done"] * 3

    # The middle alignment is invalidated (as an edit crossing its boundary does).
    payload["alignments"] = [alignments[0], alignments[2]]
    # Before: segmentation-v1 had one whole-manuscript manual pair, so nothing stayed done.
    before = sr.normalize(payload, pair_segments(source, target, alignments=payload["alignments"]), BASIS)
    assert [r["status"] for r in before.segments] == ["pending"]
    # Now: the two aligned paragraphs keep their done records; the gap waits for a manual split.
    pairs, version = pair_segments_with_gaps(source, target, alignments=payload["alignments"])
    assert version == GAP_SEGMENTATION_VERSION
    assert [p.status for p in pairs] == [STATUS_READY, STATUS_NEEDS_MANUAL_SPLIT, STATUS_READY]
    parts = sr.normalize(payload, pairs, BASIS, version=version)
    assert [r["status"] for r in parts.segments] == ["done", "pending", "done"]
    assert {r["segmentation_version"] for r in parts.segments} == {GAP_SEGMENTATION_VERSION}
    assert parts.progress["completed"] == [0, 2]
    content = manuscript.serialize_workfile(sr.with_parts(payload, parts))
    value = json.loads(content)
    assert value["format"] == manuscript.WORKFILE_FORMAT_V5 and value["review_features"] == []
    assert manuscript.serialize_workfile(manuscript.parse_workfile(content)) == content
    # The alignment comes back (undo): segmentation-v1 again, and the file is v3.
    payload["alignments"] = alignments
    pairs, version = pair_segments_with_gaps(source, target, alignments=alignments)
    parts = sr.normalize(sr.with_parts(payload, parts), pairs, BASIS, version=version)
    assert {r["segmentation_version"] for r in parts.segments} == {SEGMENTATION_VERSION}
    assert json.loads(manuscript.serialize_workfile(sr.with_parts(payload, parts)))["format"] == \
        manuscript.WORKFILE_FORMAT_V3


# ---- Layer 1 is never stored (ADR 0022, v5 SPEC) ------------------------------

def test_structure_results_are_never_part_of_a_written_manuscript() -> None:
    """The same manuscript state is written to the same bytes with or without
    structure findings: the writer has nowhere to put them."""
    payload, pairs = _long_payload([[AMB], [AMB], [AMB]], ambiguous=True)
    before = manuscript.serialize_workfile(payload)
    state = structure.State()
    items = structure.items(pairs, payload["source"], payload["target"])
    batches, skipped = structure.batches((i.segment_id, i.source, i.target) for i in items)
    assert not skipped and batches
    response = {"request_id": "l1", "check_version": "structure-check-v1", "checks": list(structure.STRUCTURE_CHECKS),
                "segments": [{"index": n, "segment_id": seg["segment_id"],
                              "findings": [{"rule_id": "empty_target", "source_span": None, "target_span": None}],
                              "omitted_findings": 0,
                              "layout": {"source_paragraphs": 1, "target_paragraphs": 1,
                                         "source_lines": 1, "target_lines": 1}}
                             for n, seg in enumerate(batches[0]["segments"])]}
    assert structure.valid_response(response, batches[0])
    structure.store(state, batches[0], response, {i.segment_id: i.key for i in items})
    assert sum(1 for v in structure.views(pairs, payload["source"], payload["target"], state) if v.flagged) == 3
    assert manuscript.serialize_workfile(payload) == before
    assert "structure" not in before and "empty_target" not in before
