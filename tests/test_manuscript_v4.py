"""Manuscript v4 contract (``wuwaterm-manuscript-v4``) and ``group_term_occurrences``.

Pure Python: requires neither PySide6 nor keyring, and uses only synthetic /
public Wuthering Waves game terms.
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
from wuwaterm_client.segmentation import pair_segments


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cand(zh: str, en: str, category: str = "resonator", *, with_sources: bool = False) -> dict:
    item = {
        "candidate_id": _sha(f"{zh}|{en}|{category}"),
        "zh": zh,
        "en": en,
        "category": category,
    }
    if with_sources:
        return {
            "zh": zh,
            "en": en,
            "category": category,
            "sources": [{"source_file": "TextMap/zh/MultiText.json", "source_id": "term_1"}],
            "candidate_id": item["candidate_id"],
        }
    return item


BASIS = {
    "rule_version": "review-v2",
    "dictionary": {
        "schema_version": "1",
        "source_commit": "a" * 40,
        "term_count": 12,
        "revision": "b" * 64,
    },
    "matcher_revision": "c" * 64,
}


def _reversed_keys(value):
    if isinstance(value, list):
        return [_reversed_keys(item) for item in value]
    if isinstance(value, dict):
        return {k: _reversed_keys(v) for k, v in reversed(list(value.items()))}
    return value


def _err(raw: str) -> str:
    with pytest.raises(manuscript.WorkfileError) as exc_info:
        manuscript.parse_workfile(raw)
    return exc_info.value.code


def _dense_source_and_target(count: int = 128) -> tuple[str, str]:
    # 128 distinct 3-char occurrences separated by spaces: < 800 scalars total.
    source = " ".join(f"椿{i:03d}" for i in range(count))
    target = " ".join(f"Camellya{i:03d}" for i in range(count))
    return source, target


def _doc_finding(
    source: str,
    target: str,
    s_start: int,
    s_end: int,
    *,
    t_span: tuple[int, int] | None = None,
    verdict: str = "needs_review",
    candidates: list[dict] | None = None,
    reason: str | None = None,
) -> dict:
    s_obj = {"start": s_start, "end": s_end, "text": source[s_start:s_end]}
    t_obj = None if t_span is None else {"start": t_span[0], "end": t_span[1], "text": target[t_span[0]:t_span[1]]}
    item = {
        "id": f"{s_start}:{s_end}:{s_obj['text']}",
        "rule_id": "review.term_pair",
        "verdict": verdict,
        "source_span": s_obj,
        "target_span": t_obj,
        "candidates": candidates if candidates is not None else [_cand("椿", "Camellya", with_sources=True)],
        "candidates_truncated": False,
    }
    if reason is not None:
        item["reason"] = reason
    return item


def _doc_choice(source: str, s_start: int, s_end: int) -> dict:
    cand = _cand("椿", "Camellya")
    return {
        "source": source,
        "direction": "en",
        "source_span": {"start": s_start, "end": s_end, "text": source[s_start:s_end]},
        "scope": "positional",
        "choice": "official_pair",
        "candidate": cand,
        "basis": copy.deepcopy(BASIS),
    }


def _doc_report(source: str, target: str, findings: list[dict]) -> dict:
    not_eval = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    return {
        "request_id": "req-v4",
        "source_revision": _sha(source),
        "target_revision": _sha(target),
        "rule_version": "review-v2",
        "dictionary": copy.deepcopy(BASIS["dictionary"]),
        "coverage": {
            "evaluated": len(findings) - not_eval,
            "not_evaluated": not_eval,
            "rules": ["review.term_pair", "review.sentence_meaning"],
        },
        "findings": findings,
        "truncated": False,
        "matcher_revision": BASIS["matcher_revision"],
    }


def _v4_base(source: str = "", target: str = "") -> dict:
    if not source and not target:
        source, target = _dense_source_and_target(40)
    return {
        "format": manuscript.WORKFILE_FORMAT_V4,
        "source": source,
        "target": target,
        "direction": "en",
        "alignments": None,
        "choices": [],
        "history": [],
        "semantic_decisions": [],
        "semantic_texts": [],
        "semantic_history": [],
        "segments": [],
        "segment_choice_history": [],
        "progress": {"current": None, "completed": []},
    }


def _segment_record(
    index: int,
    source: str,
    target: str,
    s_range: tuple[int, int],
    t_range: tuple[int, int],
    findings: list[dict],
    choices: list[dict] | None = None,
    *,
    status: str = "done",
) -> dict:
    s_core = source[s_range[0]:s_range[1]]
    t_core = target[t_range[0]:t_range[1]]
    not_eval = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    return {
        "index": index,
        "segmentation_version": "segmentation-v1",
        "source_start": s_range[0],
        "source_end": s_range[1],
        "target_start": t_range[0],
        "target_end": t_range[1],
        "source_hash": _sha(s_core),
        "target_hash": _sha(t_core),
        "status": status,
        "direction": "en",
        "alignment_input": "automatic",
        "basis": copy.deepcopy(BASIS),
        "result": {
            "truncated": False,
            "evaluated": len(findings) - not_eval,
            "not_evaluated": not_eval,
            "findings": findings,
        },
        "choices": choices or [],
    }


def test_v4_constants_and_top_level_key_order() -> None:
    assert manuscript.WORKFILE_FORMAT_V4 == "wuwaterm-manuscript-v4"
    assert manuscript.MAX_V4_CHOICES == 128
    assert manuscript.MAX_V4_FINDINGS == 128
    assert manuscript.REASON_VALUES == (
        "not_a_term",
        "ordinary_word",
        "no_target_region",
        "found_outside_region",
        "ambiguous_repeat",
    )

    source, target = "香椿发芽了。", "Chinese toon sprouted."
    payload = _v4_base(source, target)
    finding = _doc_finding(source, target, 0, 2, verdict="not_evaluated", candidates=[], reason="ordinary_word")
    payload["history"] = [{
        "source": source,
        "target": target,
        "direction": "en",
        "alignments": None,
        "report": _doc_report(source, target, [finding]),
        "resolutions": [],
    }]
    serialized = manuscript.serialize_workfile(_reversed_keys(payload))
    assert not serialized.endswith("\n")
    assert " " not in serialized.split('"source"')[0]
    decoded = json.loads(serialized)
    assert tuple(decoded.keys()) == (
        "format", "source", "target", "direction", "alignments", "choices", "history",
        "semantic_decisions", "semantic_texts", "semantic_history",
        "segments", "segment_choice_history", "progress",
    )
    assert decoded["format"] == "wuwaterm-manuscript-v4"
    assert tuple(decoded["history"][0]["report"]["findings"][0].keys()) == (
        "reason", "candidates_truncated", "candidates", "target_span", "source_span",
        "verdict", "rule_id", "id",
    )


def test_v1_v2_and_v3_still_enforce_32_cap_and_reject_reason() -> None:
    source, target = _dense_source_and_target(33)
    choices_33 = [_doc_choice(source, i * 5, i * 5 + 1) for i in range(33)]
    findings_33 = [_doc_finding(source, target, i * 5, i * 5 + 1) for i in range(33)]
    reason_finding = _doc_finding(
        source, target, 0, 1, verdict="not_evaluated", candidates=[], reason="ordinary_word"
    )

    for fmt in (manuscript.WORKFILE_FORMAT, manuscript.WORKFILE_FORMAT_V2, manuscript.WORKFILE_FORMAT_V3):
        if fmt == manuscript.WORKFILE_FORMAT_V3:
            base = {**_v4_base(source, target), "format": fmt}
        else:
            base = {
                "format": fmt, "source": source, "target": target, "direction": "en",
                "alignments": None, "choices": [], "history": [],
                **({"semantic_decisions": [], "semantic_texts": [], "semantic_history": []}
                   if fmt == manuscript.WORKFILE_FORMAT_V2 else {}),
            }
        with_33_choices = {**copy.deepcopy(base), "choices": choices_33}
        assert _err(json.dumps(with_33_choices, ensure_ascii=False)) == "workfile_invalid_content"

        with_33_findings = {
            **copy.deepcopy(base),
            "history": [{
                "source": source, "target": target, "direction": "en", "alignments": None,
                "report": _doc_report(source, target, findings_33), "resolutions": [],
            }],
        }
        assert _err(json.dumps(with_33_findings, ensure_ascii=False)) == "workfile_invalid_content"

        with_reason = {
            **copy.deepcopy(base),
            "history": [{
                "source": source, "target": target, "direction": "en", "alignments": None,
                "report": _doc_report(source, target, [reason_finding]), "resolutions": [],
            }],
        }
        assert _err(json.dumps(with_reason, ensure_ascii=False)) == "workfile_invalid_content"

    # Long-mode v3 also rejects 33 segment findings/choices and CompactFinding.reason.
    v3_seg = _v4_base(source, target)
    v3_seg["format"] = manuscript.WORKFILE_FORMAT_V3
    compact_reason = {
        "verdict": "not_evaluated", "source": [0, 1], "target": None,
        "candidates": [], "candidates_truncated": False, "reason": "not_a_term",
    }
    v3_seg["segments"] = [_segment_record(0, source, target, (0, len(source)), (0, len(target)), [compact_reason])]
    assert _err(json.dumps(v3_seg, ensure_ascii=False)) == "workfile_segment_invalid"


@pytest.mark.parametrize("reason", manuscript.REASON_VALUES)
def test_v4_accepts_all_five_not_evaluated_reasons_in_document_and_segment_modes(reason: str) -> None:
    source = "椿与红椿蕊，还有香椿。" + "文" * 2050
    target = "Camellya and Red Camellia, plus Chinese toon. " + "x" * 2050
    doc_s, doc_t = "椿与红椿蕊，还有香椿。", "Camellya and Red Camellia, plus Chinese toon."

    # Document history finding with reason + long segment CompactFinding with reason.
    doc_f = _doc_finding(doc_s, doc_t, 8, 10, verdict="not_evaluated", candidates=[], reason=reason)
    no_reason_f = _doc_finding(doc_s, doc_t, 0, 1, verdict="not_evaluated", candidates=[])
    cand = _cand("红椿蕊", "Red Camellia", "item")
    seg_findings = [
        {
            "verdict": "not_evaluated", "source": [8, 10], "target": None,
            "candidates": [], "candidates_truncated": False, "reason": reason,
        },
        {
            "verdict": "not_evaluated", "source": [0, 1], "target": None,
            "candidates": [cand], "candidates_truncated": False,
        },
    ]
    payload = _v4_base(source, target)
    payload["history"] = [{
        "source": doc_s, "target": doc_t, "direction": "en", "alignments": None,
        "report": _doc_report(doc_s, doc_t, [doc_f, no_reason_f]), "resolutions": [],
    }]
    payload["segments"] = [
        _segment_record(0, source, target, (0, 20), (0, 45), seg_findings),
    ]
    written = manuscript.serialize_workfile(_reversed_keys(payload))
    parsed = manuscript.parse_workfile(written)
    assert parsed["format"] == manuscript.WORKFILE_FORMAT_V4
    compact = json.loads(written)["segments"][0]["result"]["findings"]
    assert tuple(compact[0].keys()) == (
        "verdict", "source", "target", "candidates", "candidates_truncated", "reason"
    )
    assert tuple(compact[1].keys()) == (
        "verdict", "source", "target", "candidates", "candidates_truncated"
    )
    assert compact[0]["reason"] == reason


@pytest.mark.parametrize("bad_verdict", ["verified_constraint", "confirmed_conflict", "needs_review"])
def test_v4_rejects_reason_on_evaluated_verdicts_and_invalid_reason_values(bad_verdict: str) -> None:
    source, target = "椿在庭院里。", "Camellya is in the courtyard."
    base = _v4_base(source, target)

    # Document mode -> workfile_invalid_content
    for bad_reason in ("not_a_term", "unknown_code", None):
        verdict = bad_verdict if bad_reason == "not_a_term" else "not_evaluated"
        f = _doc_finding(source, target, 0, 1, verdict=verdict)
        f["reason"] = bad_reason
        draft = copy.deepcopy(base)
        draft["history"] = [{
            "source": source, "target": target, "direction": "en", "alignments": None,
            "report": _doc_report(source, target, [f]), "resolutions": [],
        }]
        assert _err(json.dumps(draft, ensure_ascii=False)) == "workfile_invalid_content"

    # Segment mode -> workfile_segment_invalid
    for bad_reason in ("ordinary_word", "unknown_code", None, 123):
        verdict = bad_verdict if bad_reason == "ordinary_word" else "not_evaluated"
        cf = {
            "verdict": verdict, "source": [0, 1], "target": None,
            "candidates": [_cand("椿", "Camellya")], "candidates_truncated": False,
            "reason": bad_reason,
        }
        draft = copy.deepcopy(base)
        draft["segments"] = [_segment_record(0, source, target, (0, len(source)), (0, len(target)), [cf])]
        assert _err(json.dumps(draft, ensure_ascii=False)) == "workfile_segment_invalid"


def test_v4_128_caps_and_129_overflow_across_document_and_segment_parts() -> None:
    source, target = _dense_source_and_target(129)
    cand = _cand("椿", "Camellya")

    # 128 choices, 128 resolutions, 128 doc findings, 128 segment findings & 128 segment choices -> valid v4
    choices_128 = [_doc_choice(source, i * 5, i * 5 + 1) for i in range(128)]
    doc_findings_128 = [_doc_finding(source, target, i * 5, i * 5 + 1) for i in range(128)]
    resolutions_128 = [
        {"mention_id": f"{i * 5}:{i * 5 + 1}:椿", "choice": "official_pair", "candidate_id": cand["candidate_id"]}
        for i in range(128)
    ]
    seg_findings_128 = [
        {
            "verdict": "verified_constraint", "source": [i * 5, i * 5 + 1], "target": None,
            "candidates": [cand], "candidates_truncated": False,
        }
        for i in range(128)
    ]
    seg_choices_128 = [
        {
            "source_span": {"start": i * 5, "end": i * 5 + 1, "text": "椿"},
            "choice": "official_pair", "candidate": cand, "offered": [], "confirmed": True,
        }
        for i in range(128)
    ]

    valid = _v4_base(source, target)
    valid["choices"] = choices_128
    valid["history"] = [{
        "source": source, "target": target, "direction": "en", "alignments": None,
        "report": _doc_report(source, target, doc_findings_128),
        "resolutions": resolutions_128,
    }]
    valid["segments"] = [_segment_record(0, source, target, (0, len(source)), (0, len(target)),
                                         seg_findings_128, seg_choices_128)]
    written = manuscript.serialize_workfile(valid)
    parsed = manuscript.parse_workfile(written)
    assert parsed["format"] == manuscript.WORKFILE_FORMAT_V4
    assert len(parsed["choices"]) == 128
    assert len(parsed["history"][0]["report"]["findings"]) == 128
    assert len(parsed["history"][0]["resolutions"]) == 128
    assert len(parsed["segments"][0]["result"]["findings"]) == 128
    assert len(parsed["segments"][0]["choices"]) == 128

    # 129 in each capped list -> workfile_too_many_entries
    over_doc_choices = copy.deepcopy(valid)
    over_doc_choices["choices"] = [*choices_128, _doc_choice(source, 128 * 5, 128 * 5 + 1)]
    assert _err(json.dumps(over_doc_choices, ensure_ascii=False)) == "workfile_too_many_entries"

    over_doc_findings = copy.deepcopy(valid)
    over_doc_findings["history"][0]["report"]["findings"].append(_doc_finding(source, target, 128 * 5, 128 * 5 + 1))
    assert _err(json.dumps(over_doc_findings, ensure_ascii=False)) == "workfile_too_many_entries"

    over_resolutions = copy.deepcopy(valid)
    over_resolutions["history"][0]["resolutions"].append(
        {"mention_id": f"{128 * 5}:{128 * 5 + 1}:椿", "choice": "not_a_term"}
    )
    assert _err(json.dumps(over_resolutions, ensure_ascii=False)) == "workfile_too_many_entries"

    over_seg_findings = copy.deepcopy(valid)
    over_seg_findings["segments"][0]["result"]["findings"].append({
        "verdict": "needs_review", "source": [128 * 5, 128 * 5 + 1], "target": None,
        "candidates": [cand], "candidates_truncated": False,
    })
    assert _err(json.dumps(over_seg_findings, ensure_ascii=False)) == "workfile_too_many_entries"

    over_seg_choices = copy.deepcopy(valid)
    over_seg_choices["segments"][0]["choices"].append({
        "source_span": {"start": 128 * 5, "end": 128 * 5 + 1, "text": "椿"},
        "choice": "not_a_term", "candidate": None, "offered": [], "confirmed": False,
    })
    assert _err(json.dumps(over_seg_choices, ensure_ascii=False)) == "workfile_too_many_entries"


def test_three_tier_lowest_format_writer_rule(tmp_path: Path) -> None:
    source, target = _dense_source_and_target(35)
    cand = _cand("椿", "Camellya")

    # Tier 1: <= 32, no segments, no reason -> v1 pretty JSON
    t1 = manuscript.empty_workfile()
    t1.update(source="椿在今州。", target="Camellya is in Jinzhou.")
    out_v1 = manuscript.serialize_workfile(t1)
    assert out_v1.startswith('{\n  "format": "wuwaterm-manuscript-v1"')

    # Tier 2: has segments, all counts <= 32, no reason -> v3 minified JSON
    t2 = _v4_base(source, target)
    t2["segments"] = [_segment_record(
        0, source, target, (0, len(source)), (0, len(target)),
        [{"verdict": "needs_review", "source": [0, 1], "target": None,
          "candidates": [cand], "candidates_truncated": False}],
    )]
    out_v3 = manuscript.serialize_workfile(t2)
    assert json.loads(out_v3)["format"] == manuscript.WORKFILE_FORMAT_V3

    # Tier 3a: document mode without segments, 33 choices -> v4 minified JSON
    t3_doc = manuscript.empty_workfile()
    t3_doc.update(
        source=source,
        target=target,
        choices=[_doc_choice(source, i * 5, i * 5 + 1) for i in range(33)],
    )
    out_v4_doc = manuscript.serialize_workfile(t3_doc)
    assert json.loads(out_v4_doc)["format"] == manuscript.WORKFILE_FORMAT_V4

    # Tier 3b: document mode without segments, 1 finding with reason -> v4 minified JSON
    t3_reason = manuscript.empty_workfile()
    t3_reason.update(
        source=source,
        target=target,
        history=[{
            "source": source, "target": target, "direction": "en", "alignments": None,
            "report": _doc_report(source, target, [
                _doc_finding(source, target, 0, 1, verdict="not_evaluated", candidates=[], reason="no_target_region")
            ]),
            "resolutions": [],
        }],
    )
    out_v4_reason = manuscript.serialize_workfile(t3_reason)
    assert json.loads(out_v4_reason)["format"] == manuscript.WORKFILE_FORMAT_V4

    # Tier 3c: segment mode with 33 findings -> v4; roundtrip via save_workfile/load_workfile
    t3_seg = _v4_base(source, target)
    t3_seg["segments"] = [_segment_record(
        0, source, target, (0, len(source)), (0, len(target)),
        [{"verdict": "needs_review", "source": [i * 5, i * 5 + 1], "target": None,
          "candidates": [cand], "candidates_truncated": False} for i in range(33)],
    )]
    path = tmp_path / "dense.json"
    manuscript.save_workfile(path, t3_seg)
    reloaded = manuscript.load_workfile(path)
    assert reloaded["format"] == manuscript.WORKFILE_FORMAT_V4
    assert len(reloaded["segments"][0]["result"]["findings"]) == 33


def test_segment_records_runtime_supports_128_choices_and_preserves_reason() -> None:
    source, target = _dense_source_and_target(128)
    cand_with_src = _cand("椿", "Camellya", with_sources=True)
    findings = []
    for i in range(128):
        if i == 0:
            findings.append(_doc_finding(
                source, target, 0, 1,
                verdict="not_evaluated", candidates=[cand_with_src], reason="ambiguous_repeat",
            ))
        else:
            findings.append(_doc_finding(
                source, target, i * 5, i * 5 + 1,
                t_span=(i * 12, i * 12 + 8), verdict="needs_review", candidates=[cand_with_src],
            ))
    live_report = _doc_report(source, target, findings)
    compact = sr.compact_result(live_report, source, target)
    assert compact is not None
    assert len(compact["findings"]) == 128
    assert compact["findings"][0]["reason"] == "ambiguous_repeat"
    assert "reason" not in compact["findings"][1]

    # Build a long payload and record 128 choices via sr.choose
    pad = "\n\n" + "今汐在今州议事。" * 260
    pad_t = "\n\n" + "Jinhsi meets in Jinzhou. " * 120
    full_s, full_t = source + pad, target + pad_t
    pairs = pair_segments(full_s, full_t)
    payload = _v4_base(full_s, full_t)
    payload = sr.with_parts(payload, sr.normalize(payload, pairs))
    req = sr.plan_request(payload, 0, None)
    applied = sr.apply_response(payload, req, _doc_report(req.source, req.target, findings), None)
    assert applied.outcome == "stored"
    payload = sr.with_parts(payload, applied.parts)

    expanded = sr.expand_result(payload["segments"][0], full_s, full_t)
    assert expanded["findings"][0]["reason"] == "ambiguous_repeat"

    cand_id = cand_with_src["candidate_id"]
    for i in range(128):
        payload = sr.with_parts(payload, sr.choose(payload, 0, i * 5, i * 5 + 1, sr.OFFICIAL_PAIR, cand_id))
    assert len(payload["segments"][0]["choices"]) == 128
    assert json.loads(manuscript.serialize_workfile(payload))["format"] == manuscript.WORKFILE_FORMAT_V4


def test_group_term_occurrences_homographs_plural_merge_and_snippets() -> None:
    # ZH -> EN homographs: 椿 (character) vs 红椿蕊 (item) vs 香椿 (ordinary_word) + 今汐 (confirmed)
    s1 = "庭院深处，椿静静注视着枝头的红椿蕊。今汐在城楼远望。" + "声骸低鸣。" * 180
    t1 = ("Deep in the courtyard, Camellya quietly watched the Red Camellia on the branch. "
          "Jinhsi looked afar from the tower. " + "Echoes hummed softly. " * 50)
    s2 = "雨后清晨，椿再次穿过花径，厨下正备着一碟香椿。" + "今汐翻阅卷宗。" * 180
    t2 = ("After the morning rain, Camellya crossed the path again while Chinese toon was prepared. "
          "Jinhsi read the scrolls. " + "The records stayed dry. " * 45)
    s3 = "夜幕降临时，椿没有现身，只留下一枚枯瓣。" + "风声穿过回廊。" * 180
    t3 = ("When night fell, the camellia figure did not appear, leaving only a petal. "
          + "Wind passed the corridor. " * 50)
    source = f"{s1}\n\n{s2}\n\n{s3}"
    target = f"{t1}\n\n{t2}\n\n{t3}"
    pairs = pair_segments(source, target)
    assert len(pairs) == 3

    c_cam = _cand("椿", "Camellya", "resonator")
    c_red = _cand("红椿蕊", "Red Camellia", "item")
    c_jin = _cand("今汐", "Jinhsi", "resonator")

    def rec(idx: int, findings: list[dict], choices: list[dict] | None = None) -> dict:
        p = pairs[idx]
        return _segment_record(
            idx, source, target,
            (p.source.core_start, p.source.core_end),
            (p.target.core_start, p.target.core_end),
            findings, choices,
        )

    def span_in(text: str, sub: str) -> list[int]:
        pos = text.index(sub)
        return [pos, pos + len(sub)]

    sc1 = source[pairs[0].source.core_start:pairs[0].source.core_end]
    tc1 = target[pairs[0].target.core_start:pairs[0].target.core_end]
    sc2 = source[pairs[1].source.core_start:pairs[1].source.core_end]
    tc2 = target[pairs[1].target.core_start:pairs[1].target.core_end]
    sc3 = source[pairs[2].source.core_start:pairs[2].source.core_end]

    records = [
        rec(0, [
            {"verdict": "verified_constraint", "source": span_in(sc1, "椿"), "target": span_in(tc1, "Camellya"),
             "candidates": [c_cam], "candidates_truncated": False},
            {"verdict": "needs_review", "source": span_in(sc1, "红椿蕊"), "target": span_in(tc1, "Red Camellia"),
             "candidates": [c_red], "candidates_truncated": False},
            {"verdict": "verified_constraint", "source": span_in(sc1, "今汐"), "target": span_in(tc1, "Jinhsi"),
             "candidates": [c_jin], "candidates_truncated": False},
        ]),
        rec(1, [
            {"verdict": "verified_constraint", "source": span_in(sc2, "椿"), "target": span_in(tc2, "Camellya"),
             "candidates": [c_cam], "candidates_truncated": False},
            {"verdict": "not_evaluated", "source": span_in(sc2, "香椿"), "target": None,
             "candidates": [], "candidates_truncated": False, "reason": "ordinary_word"},
            {"verdict": "verified_constraint", "source": span_in(sc2, "今汐"), "target": span_in(tc2, "Jinhsi"),
             "candidates": [c_jin], "candidates_truncated": False},
        ]),
        rec(2, [
            {"verdict": "confirmed_conflict", "source": span_in(sc3, "椿"), "target": None,
             "candidates": [c_cam], "candidates_truncated": False},
        ]),
    ]

    groups = sr.group_term_occurrences(records, pairs, source, target, "en", basis=BASIS)
    by_surface = {g["surface"]: g for g in groups}
    assert set(by_surface) == {"椿", "红椿蕊", "香椿", "今汐"}

    cam = by_surface["椿"]
    assert cam["count"] == 3
    assert cam["segments"] == [1, 2, 3]
    assert cam["distribution_text"] == "Camellya ×2 · 未命中 ×1"
    assert cam["status"] == "inconsistent"
    # Missed occurrence (#3) still gets a compact target sentence excerpt.
    miss_occ = cam["occurrences"][2]
    assert miss_occ["segment_number"] == 3
    assert miss_occ["target_span"] is None
    assert "camellia" in miss_occ["target_context"]
    assert 10 <= len(miss_occ["source_context"]) <= 45

    assert by_surface["红椿蕊"]["status"] == "pending"
    assert by_surface["香椿"]["status"] == "not_evaluated"
    assert by_surface["香椿"]["occurrences"][0]["reason"] == "ordinary_word"
    assert "普通词义" in by_surface["香椿"]["occurrences"][0]["verdict_label"]
    assert by_surface["今汐"]["status"] == "confirmed"
    assert by_surface["今汐"]["distribution_text"] == "Jinhsi ×2"

    # EN -> ZH singular/plural merging: Tuner + Tuners -> Tuner (调谐器 ×2 · 调音师 ×1)
    en_s = "Use one Tuner to calibrate the Echo.\n\nTwo Tuners were left on the bench.\n\nAsk the Tuner in Jinzhou."
    zh_t = "用一枚调谐器校准声骸。\n\n工作台上留着两枚调谐器。\n\n去问今州城里的调音师。"
    c_tuner_1 = _cand("调谐器", "Tuner", "item")
    c_tuner_2 = _cand("调音师", "Tuner", "speaker")
    en_pairs = pair_segments(en_s, zh_t)
    # Single merged pair for short text; test across 3 explicit segment records:
    en_paras = en_s.split("\n\n")
    zh_paras = zh_t.split("\n\n")
    s_offsets = [0, len(en_paras[0]) + 2, len(en_paras[0]) + 2 + len(en_paras[1]) + 2]
    t_offsets = [0, len(zh_paras[0]) + 2, len(zh_paras[0]) + 2 + len(zh_paras[1]) + 2]
    en_records = []
    for idx, (ep, zp, so, to, surf, rend) in enumerate([
        (en_paras[0], zh_paras[0], s_offsets[0], t_offsets[0], "Tuner", "调谐器"),
        (en_paras[1], zh_paras[1], s_offsets[1], t_offsets[1], "Tuners", "调谐器"),
        (en_paras[2], zh_paras[2], s_offsets[2], t_offsets[2], "Tuner", "调音师"),
    ]):
        r = _segment_record(
            idx, en_s, zh_t, (so, so + len(ep)), (to, to + len(zp)),
            [{"verdict": "needs_review", "source": span_in(ep, surf), "target": span_in(zp, rend),
              "candidates": [c_tuner_1, c_tuner_2], "candidates_truncated": False}],
        )
        r["direction"] = "zh"
        en_records.append(r)

    en_groups = sr.group_term_occurrences(en_records, [], en_s, zh_t, "zh", basis=BASIS)
    assert len(en_groups) == 1
    tuner_grp = en_groups[0]
    assert tuner_grp["surface"] == "Tuner"
    assert tuner_grp["surfaces"] == ["Tuner", "Tuners"]
    assert tuner_grp["count"] == 3
    assert tuner_grp["distribution_text"] == "调谐器 ×2 · 调音师 ×1"
    assert tuner_grp["status"] == "inconsistent"


# ---- Web's shared v4 fixture index -----------------------------------------
# ``contracts/sources.json`` pins these fixtures, but the case list is needed at
# collection time and ``contract_source`` is a test fixture, so the index is read
# straight from the checkout ``WUWATERM_WEB_ROOT`` names. Without that checkout
# the case list is empty and the test skips: the standalone tests above still
# cover the same rules.

def _v4_contract_root() -> Path | None:
    root = os.environ.get("WUWATERM_WEB_ROOT")
    if not root:
        return None
    candidate = Path(root) / "contracts" / "manuscript-v4"
    return candidate if (candidate / "cases.json").is_file() else None


def _v4_cases() -> list[dict]:
    root = _v4_contract_root()
    if root is None:
        return []
    data = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    return ([{**case, "valid": True} for case in data["valid"]]
            + [{**case, "valid": False} for case in data["invalid"]])


def _v4_case_id(case: dict | None) -> str:
    if case is None:
        return "<unconfigured>"
    pad = f"+pad{case['pad_to_bytes']}" if case.get("pad_to_bytes") else ""
    return f'{case["file"]}{pad}'


def pytest_generate_tests(metafunc):
    # The case names are needed at collection time, so read the index directly.
    if "v4_case" in metafunc.fixturenames:
        cases = _v4_cases()
        metafunc.parametrize("v4_case", cases or [None], ids=[_v4_case_id(c) for c in cases] or ["<unconfigured>"])


def _fixture_bytes(root: Path, case: dict) -> str:
    raw = (root / case["file"]).read_text(encoding="utf-8")
    pad = case.get("pad_to_bytes")
    if pad:
        # Web pads with spaces; the pad only ever pushes a case over a size cap.
        raw += " " * (pad - len(raw.encode("utf-8")))
    return raw


def test_web_v4_fixture_is_canonical_bytes_or_exact_error_code(v4_case) -> None:
    root = _v4_contract_root()
    if root is None:
        pytest.skip("set WUWATERM_WEB_ROOT to a WuwaTerm-Web checkout that has contracts/manuscript-v4")
    raw = _fixture_bytes(root, v4_case)
    if v4_case["valid"]:
        document = manuscript.parse_workfile(raw)
        canonical = (root / v4_case["canonical"]).read_text(encoding="utf-8")
        assert manuscript.serialize_workfile(document) == canonical, v4_case["note"]
    else:
        assert _err(raw) == v4_case["error"], v4_case["note"]


def test_web_v4_fixture_index_is_complete() -> None:
    """The index lists every file in the directory, so a new fixture cannot sit
    unread: 9 valid cases (one padded) and 18 invalid ones today."""
    root = _v4_contract_root()
    if root is None:
        pytest.skip("set WUWATERM_WEB_ROOT to a WuwaTerm-Web checkout that has contracts/manuscript-v4")
    cases = _v4_cases()
    listed = {case["file"] for case in cases}
    on_disk = {path.name for path in root.glob("*.json")} - {"cases.json"}
    assert on_disk <= listed, sorted(on_disk - listed)
    assert len([c for c in cases if c["valid"]]) == 9
    assert len([c for c in cases if not c["valid"]]) == 18


def test_writer_counts_only_the_two_written_history_snapshots() -> None:
    """SPEC "Writing": only the last two document reports are written, so only
    they count when the writer picks the lowest format. A dense third-from-last
    snapshot is dropped, never a reason to write v4."""
    source, target = _dense_source_and_target(41)
    dense = [*_dense_findings(source, target, 40), _doc_finding(
        source, target, 200, 201, verdict="not_evaluated", candidates=[], reason="ordinary_word")]
    assert len(dense) == 41

    def snapshot(findings: list[dict]) -> dict:
        return {
            "source": source, "target": target, "direction": "en", "alignments": None,
            "report": _doc_report(source, target, findings), "resolutions": [],
        }

    clean = manuscript.empty_workfile()
    clean.update(source=source, target=target)
    clean["history"] = [snapshot([_doc_finding(source, target, 0, 1)])]

    # The dropped snapshot is the dense one: v1 is still the lowest format.
    dropped = copy.deepcopy(clean)
    dropped["history"] = [snapshot(dense)] + copy.deepcopy(clean["history"]) * 2
    written = json.loads(manuscript.serialize_workfile(dropped))
    assert written["format"] == manuscript.WORKFILE_FORMAT
    assert len(written["history"]) == 2
    assert '"reason"' not in manuscript.serialize_workfile(dropped)

    # The same snapshot kept (it is one of the last two): v4.
    kept = copy.deepcopy(clean)
    kept["history"] = [copy.deepcopy(clean["history"][0]), snapshot(dense)]
    assert json.loads(manuscript.serialize_workfile(kept))["format"] == manuscript.WORKFILE_FORMAT_V4

    # With segment state the same rule lands on v3 instead of v1.
    segmented = copy.deepcopy(clean)
    segmented["segments"] = [_segment_record(
        0, source, target, (0, len(source)), (0, len(target)),
        [{"verdict": "needs_review", "source": [0, 1], "target": None,
          "candidates": [_cand("椿", "Camellya")], "candidates_truncated": False}],
    )]
    segmented["history"] = [snapshot(dense)] + copy.deepcopy(clean["history"]) * 2
    assert json.loads(manuscript.serialize_workfile(segmented))["format"] == manuscript.WORKFILE_FORMAT_V3


def _dense_findings(source: str, target: str, count: int) -> list[dict]:
    return [_doc_finding(source, target, i * 5, i * 5 + 1) for i in range(count)]


def test_group_filter_matches_a_place_whatever_the_group_status_is() -> None:
    """``group_matches_filter`` (Web ``groupMatch``): the 待处理 filter shows a
    group that holds a pending place, even when the group's own status is the
    more urgent ``inconsistent``."""
    source = ("庭院深处，椿静静注视着枝头。" + "今汐在城楼远望。" * 130
              + "\n\n" + "雨后清晨，椿穿过回廊。" + "声骸在远处低鸣。" * 130)
    target = ("Deep in the courtyard, Camellya watched the branch. " + "Jinhsi looked afar. " * 130
              + "\n\n" + "After the rain, Camelia crossed the corridor. " + "Echoes hummed. " * 130)
    # Only the first two pairs matter; a long paragraph may split further.
    pairs = pair_segments(source, target)[:2]
    assert len(pairs) == 2

    def span_in(text: str, sub: str) -> list[int]:
        at = text.index(sub)
        return [at, at + len(sub)]

    def rec(index: int, verdict: str, rendering: str) -> dict:
        pair = pairs[index]
        s_core = source[pair.source.core_start:pair.source.core_end]
        t_core = target[pair.target.core_start:pair.target.core_end]
        return _segment_record(
            index, source, target,
            (pair.source.core_start, pair.source.core_end),
            (pair.target.core_start, pair.target.core_end),
            [{"verdict": verdict, "source": span_in(s_core, "椿"), "target": span_in(t_core, rendering),
              "candidates": [_cand("椿", "Camellya")], "candidates_truncated": False}],
        )

    groups = sr.group_term_occurrences(
        [rec(0, "verified_constraint", "Camellya"), rec(1, "needs_review", "Camelia")],
        pairs, source, target, "en", basis=BASIS,
    )
    assert len(groups) == 1
    group = groups[0]
    assert group["status"] == "inconsistent"
    assert group["pending_count"] == 1
    # The invariant: pending work is never hidden behind the group's status word.
    assert sr.group_matches_filter(group, "pending")
    assert sr.group_matches_filter(group, "inconsistent")
    assert not sr.group_matches_filter(group, "confirmed")
    assert group["unverified_segments"] == []


def test_a_group_is_never_confirmed_while_a_ready_segment_owes_a_check() -> None:
    """Web ``unverifiedSegments``: a ready segment whose record is pending holds
    one of the group's spellings but no place in it yet, so the group is not
    settled — the header must not claim 已核 across the manuscript."""
    source = ("庭院深处，调谐器静静注视着枝头。" + "今汐在城楼远望。" * 130
              + "\n\n" + "雨后清晨，调谐器穿过回廊。" + "声骸在远处低鸣。" * 130)
    target = ("Deep in the courtyard, the Tuner watched the branch. " + "Jinhsi looked afar. " * 130
              + "\n\n" + "After the rain, the Tuner crossed the corridor. " + "Echoes hummed. " * 130)
    # Only the first two pairs matter; a long paragraph may split further.
    pairs = pair_segments(source, target)[:2]
    assert len(pairs) == 2
    s_core = source[pairs[0].source.core_start:pairs[0].source.core_end]
    t_core = target[pairs[0].target.core_start:pairs[0].target.core_end]
    tuner = _cand("调谐器", "Tuner", "item")
    finding = {"verdict": "verified_constraint", "source": [s_core.index("调谐器"), s_core.index("调谐器") + 3],
               "target": [t_core.index("Tuner"), t_core.index("Tuner") + 5],
               "candidates": [tuner], "candidates_truncated": False}
    done = _segment_record(0, source, target,
                           (pairs[0].source.core_start, pairs[0].source.core_end),
                           (pairs[0].target.core_start, pairs[0].target.core_end), [finding])

    # Only segment 1 has a record: segment 2 is ready but unchecked.
    groups = sr.group_term_occurrences([done], pairs, source, target, "en", basis=BASIS)
    group = groups[0]
    assert group["status"] == "pending", group
    assert group["unverified_segments"] == [2]
    assert sr.group_matches_filter(group, "pending")
    assert not sr.group_matches_filter(group, "confirmed")

    # With both records the group is settled, and says so.
    second = _segment_record(1, source, target,
                             (pairs[1].source.core_start, pairs[1].source.core_end),
                             (pairs[1].target.core_start, pairs[1].target.core_end),
                             [{**finding,
                               "source": [source[pairs[1].source.core_start:pairs[1].source.core_end].index("调谐器"),
                                          source[pairs[1].source.core_start:pairs[1].source.core_end].index("调谐器") + 3],
                               "target": [target[pairs[1].target.core_start:pairs[1].target.core_end].index("Tuner"),
                                          target[pairs[1].target.core_start:pairs[1].target.core_end].index("Tuner") + 5]}])
    settled = sr.group_term_occurrences([done, second], pairs, source, target, "en", basis=BASIS)[0]
    assert settled["status"] == "confirmed", settled
    assert settled["unverified_segments"] == []
    assert sr.group_matches_filter(settled, "confirmed")
