"""semantic-review-v1 client contract: response validation, request bytes,
stale-span guard, re-review merge and manuscript v2. Pure stdlib (no Qt/httpx).
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from wuwaterm_client import manuscript, semantic
from wuwaterm_client.semantic import StaleSpan


@pytest.fixture
def fixtures(contract_source):
    root = contract_source("core") / "contracts" / "semantic-review-v1"

    def load(name: str):
        return json.loads((root / name).read_text(encoding="utf-8"))

    return root, load


def _cases(fixtures):
    root, load = fixtures
    return [c for c in load("cases.json")["cases"] if c["response"]]


def _pair(fixtures, name: str):
    root, load = fixtures
    case = next(c for c in load("cases.json")["cases"] if c["name"] == name)
    return load(case["request"]), load(case["response"])


def test_every_core_response_validates_against_its_request(fixtures) -> None:
    cases = _cases(fixtures)
    assert {c["name"] for c in cases} >= {"max", "byte-limit", "not-reviewed", "minimal"}
    _, load = fixtures
    for case in cases:
        request, response = load(case["request"]), load(case["response"])
        assert semantic.valid_semantic_report(response, request["source"], request["target"]), case["name"]
        # And only for those exact texts.
        assert not semantic.valid_semantic_report(response, request["source"] + "x", request["target"])


def test_max_response_is_the_byte_budget_fixture(fixtures) -> None:
    root, load = fixtures
    assert len((root / "max-response.json").read_bytes()) == 56_651


def test_response_mutations_are_rejected(fixtures) -> None:
    request, report = _pair(fixtures, "minimal")
    source, target = request["source"], request["target"]

    def broken(edit) -> bool:
        data = copy.deepcopy(report)
        edit(data)
        return semantic.valid_semantic_report(data, source, target)

    assert semantic.valid_semantic_report(report, source, target)
    issue_with_suggestion = next(i for i, v in enumerate(report["issues"]) if v["suggestion"])
    cases = {
        "span text differs": lambda d: d["issues"][0]["target_span"].update(text="x"),
        "extra key": lambda d: d.update(extra=1),
        "extra issue key": lambda d: d["issues"][0].update(extra=1),
        "revision": lambda d: d.update(source_revision="0" * 64),
        "excerpt": lambda d: d["issues"][0].update(source_excerpt="other"),
        "bool offset": lambda d: d["issues"][0]["source_span"].update(start=False),
        "duplicate id": lambda d: d["issues"][1].update(id=d["issues"][0]["id"]),
        "withheld with suggestion": lambda d: d["issues"][issue_with_suggestion].update(
            suggestion_withheld="span_too_long"),
        "unknown category": lambda d: d["issues"][0].update(category="other"),
        "truncated without reason": lambda d: d.update(truncated=True),
        "isolated surrogate": lambda d: d["issues"][0].update(rationale="\ud800"),
        "rationale too long": lambda d: d["issues"][0].update(rationale="x" * 241),
    }
    for name, edit in cases.items():
        assert not broken(edit), name


def test_not_reviewed_with_issues_is_rejected(fixtures) -> None:
    request, report = _pair(fixtures, "not-reviewed")
    source, target = request["source"], request["target"]
    _, minimal = _pair(fixtures, "minimal")
    bad = copy.deepcopy(report)
    bad["issues"] = []
    bad["checked"] = ["omission"]
    assert not semantic.valid_semantic_report(bad, source, target)
    bad = copy.deepcopy(report)
    bad["status_reason"] = None
    assert not semantic.valid_semantic_report(bad, source, target)


def test_truncated_executable_span_is_rejected(fixtures) -> None:
    request, report = _pair(fixtures, "suggestion-span-too-long")
    source, target = request["source"], request["target"]
    issue = report["issues"][0]
    span = issue["target_span"]
    assert span["end"] - span["start"] == 300 and issue["suggestion_withheld"] == "span_too_long"
    cut = copy.deepcopy(report)
    cut["issues"][0]["target_span"]["text"] = span["text"][:200]
    assert not semantic.valid_semantic_report(cut, source, target)
    # A suggestion whose range is over 200 scalars is never valid, even whole.
    whole = copy.deepcopy(report)
    whole["issues"][0].update(
        suggestion_withheld=None,
        suggestion={"target_span": dict(span), "replacement": "x", "term_effect": "none"},
    )
    assert not semantic.valid_semantic_report(whole, source, target)


def test_capabilities_parsing() -> None:
    body = {"api_version": "v1", "capabilities": ["semantic-review-v1", "future-thing"], "request_id": "r"}
    assert semantic.parse_capabilities(body) == {"semantic-review-v1", "future-thing"}
    for bad in (None, {**body, "x": 1}, {**body, "capabilities": "semantic-review-v1"},
                {**body, "capabilities": [1]}, {"api_version": "v1", "capabilities": [], "request_id": 3}):
        with pytest.raises(ValueError):
            semantic.parse_capabilities(bad)


def test_near_limit_request_re_encodes_to_the_same_bytes(fixtures) -> None:
    root, load = fixtures
    raw = (root / "near-limit-request.json").read_bytes()
    body = json.loads(raw)
    assert len(raw) == 32_766
    again = semantic.encode_request(
        body["source"], body["target"], body["direction"],
        explain_language=body["explain_language"], before=body["context"]["before"],
        precedents=body["precedents"],
    )
    assert again == raw


def _wide_precedent(n: int) -> dict:
    return {"surface": "字" * 32, "choice": "user_rendering", "rendering": "字" * 48,
            "snippet": "字" * 80, "anchor": {"segment_index": n, "source_start": 0, "source_end": 32},
            "scope": "occurrence"}


def test_oversized_request_drops_farthest_precedents_first() -> None:
    # Control characters cost six bytes each, so two full sides use 24,000 bytes.
    heavy = "\x01" * 2000
    before = {"source": "甲" * 400, "target": "乙" * 400}
    precedents = [_wide_precedent(n) for n in range(12)]
    data = semantic.encode_request(heavy, heavy, "en", before=before, precedents=precedents)
    sent = json.loads(data)
    assert len(data) <= semantic.REQUEST_BYTE_LIMIT
    assert 0 < len(sent["precedents"]) < 12
    assert sent["precedents"] == precedents[: len(sent["precedents"])]  # the nearest survive
    assert sent["context"]["before"] == before


def test_context_is_shortened_only_after_precedents_are_gone(monkeypatch) -> None:
    # With valid inputs the real limit is always met once precedents are dropped,
    # so exercise the second stage with a smaller limit.
    monkeypatch.setattr(semantic, "REQUEST_BYTE_LIMIT", 700)
    before = {"source": "a" * 399 + "Z", "target": "b" * 400}
    data = semantic.encode_request("s", "t", "en", before=before, precedents=[_wide_precedent(0)])
    sent = json.loads(data)
    assert sent["precedents"] == []
    kept = sent["context"]["before"]
    assert len(data) <= 700 and kept["source"].endswith("Z") and 0 < len(kept["source"]) < 400
    assert before["source"].endswith(kept["source"]) and before["target"].endswith(kept["target"])
    monkeypatch.setattr(semantic, "REQUEST_BYTE_LIMIT", 100)
    with pytest.raises(ValueError):
        semantic.encode_request("s" * 50, "t" * 50, "en")


def test_request_caller_bugs_raise() -> None:
    precedent = _wide_precedent(0)
    for bad in (
        dict(source="s" * 2001, target="t"),
        dict(source="s", target="t", before={"source": "x" * 401, "target": ""}),
        dict(source="s", target="t", precedents=[{**precedent, "rendering": None}]),
        dict(source="s", target="t", precedents=[precedent] * 13),
        dict(source="s\ud800", target="t"),
    ):
        with pytest.raises(ValueError):
            semantic.encode_request(bad.pop("source"), bad.pop("target"), "en", **bad)
    assert b"context" not in semantic.encode_request("s", "t", "zh")
    assert json.loads(semantic.encode_request("s", "t", "zh"))["precedents"] == []


def test_apply_span_refuses_a_truncated_span(fixtures) -> None:
    request, report = _pair(fixtures, "suggestion-span-too-long")
    target = request["target"]
    span = report["issues"][0]["target_span"]
    truncated = {"start": span["start"], "end": span["end"], "text": span["text"][:200]}
    with pytest.raises(StaleSpan):
        semantic.apply_span(target, truncated, "好" * 240)
    exact = semantic.apply_span(target, span, "好" * 240)
    assert len(exact) == len(target) - 300 + 240
    for bad in ({"start": True, "end": 3, "text": "a"}, {"start": 0, "end": 99, "text": "a"},
                {"start": 2, "end": 2, "text": ""}, {"start": 0}):
        with pytest.raises(StaleSpan):
            semantic.apply_span("abc", bad, "x")
    assert semantic.apply_span("abc", {"start": 1, "end": 2, "text": "b"}, "XY") == "aXYc"


def _precedent(surface: str) -> dict:
    return {"surface": surface, "choice": "not_a_term", "rendering": None, "snippet": surface,
            "anchor": {"segment_index": 0, "source_start": 0, "source_end": len(surface)},
            "scope": "occurrence"}


def test_precedent_selection_and_context() -> None:
    cands = [(10, _precedent("甲")), (50, _precedent("乙")), (30, _precedent("丙")), (500, _precedent("丁"))]
    chosen = semantic.select_precedents(cands, "这里有丙", 100)
    assert [p["surface"] for p in chosen] == ["丙", "乙", "甲"]  # 丁 ends after the segment starts
    assert len(semantic.select_precedents([(i, _precedent("甲")) for i in range(30)], "", 100)) == 12
    assert semantic.context_before("", "") is None
    assert semantic.context_before("a" * 500, "b") == {"source": "a" * 400, "target": "b"}


def _decision(fp: str, kind: str, source: str, target: str, source_span, target_span) -> dict:
    return {"fingerprint": fp, "decision": kind,
            "source_revision": hashlib.sha256(source.encode()).hexdigest(),
            "target_revision": hashlib.sha256(target.encode()).hexdigest(),
            "source_span": source_span, "target_span": target_span}


def test_reconcile_rules() -> None:
    source, target = "甲乙", "AB"
    ss = {"start": 0, "end": 1, "text": "甲"}
    ts = {"start": 0, "end": 1, "text": "A"}
    issue = {"fingerprint": "f" * 32, "source_span": ss, "target_span": ts, "segment_index": 0}
    ignored = _decision("f" * 32, "ignored", source, target, ss, ts)
    state = lambda decisions, issues=(issue,), s=source, t=target: semantic.reconcile(decisions, list(issues), s, t)  # noqa: E731

    assert state([ignored])[0]["state"] == "ignored_carried"
    assert state([])[0] == {"state": "pending", "previous": None, "previously_applied": False}
    assert state([_decision("e" * 32, "ignored", source, target, ss, ts)])[0]["state"] == "pending"
    assert state([ignored], s="甲乙。")[0]["state"] == "needs_reconfirm"
    assert state([ignored], t="AB!")[0]["state"] == "needs_reconfirm"
    moved = dict(issue, source_span={"start": 1, "end": 2, "text": "乙"})
    assert state([ignored], (moved,))[0]["state"] == "needs_reconfirm"
    twin = dict(issue, target_span=None)
    assert [x["state"] for x in state([ignored], (issue, twin))] == ["needs_reconfirm"] * 2
    applied = _decision("f" * 32, "accepted", source, target, ss, ts)
    result = state([ignored, applied])[0]
    assert result["state"] == "pending" and result["previously_applied"] and result["previous"] is applied


# -- manuscript v2 ---------------------------------------------------------


def _v2_payload(fixtures):
    request, report = _pair(fixtures, "minimal")
    source, target = "第一段。\n\n" + request["source"], "First.\n\n" + request["target"]
    issue = next(i for i in report["issues"] if i["suggestion"])
    offset_s, offset_t = len("第一段。\n\n"), len("First.\n\n")
    ts = issue["target_span"]
    span = {"start": ts["start"] + offset_t, "end": ts["end"] + offset_t, "text": ts["text"]}
    decision = {
        "issue_id": issue["id"], "fingerprint": issue["fingerprint"], "category": issue["category"],
        "segment_index": 1,
        "source_revision": hashlib.sha256(source.encode()).hexdigest(),
        "target_revision": hashlib.sha256(target.encode()).hexdigest(),
        "source_span": None, "target_span": span, "decision": "accepted",
        "applied_text": issue["suggestion"]["replacement"],
        "basis": {"review_version": "semantic-review-v1", "term_basis": report["term_basis"]},
    }
    snapshot = {"segment_index": 1, "source": request["source"], "target": request["target"],
                "direction": request["direction"], "report": report}
    payload = manuscript.empty_workfile()
    payload.update(source=source, target=target, semantic_decisions=[decision], semantic_history=[snapshot])
    return payload, offset_s


def test_v2_roundtrip_and_key_order(fixtures, tmp_path) -> None:
    payload, _ = _v2_payload(fixtures)
    content = manuscript.serialize_workfile(payload)
    assert list(json.loads(content)) == ["format", "source", "target", "direction", "alignments",
                                         "choices", "history", "semantic_decisions", "semantic_history"]
    assert json.loads(content)["format"] == manuscript.WORKFILE_FORMAT_V2
    path = tmp_path / "draft.json"
    manuscript.save_workfile(path, payload)
    loaded = manuscript.load_workfile(path)
    assert loaded["semantic_decisions"] == payload["semantic_decisions"]
    assert loaded["semantic_history"] == payload["semantic_history"]
    assert manuscript.semantic_payload_fits(payload)


def test_without_semantic_data_the_file_is_exactly_v1(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)
    plain = {k: v for k, v in payload.items() if not k.startswith("semantic_")}
    v1 = manuscript.serialize_workfile(plain)
    assert manuscript.serialize_workfile({**plain, "semantic_decisions": [], "semantic_history": []}) == v1
    assert list(json.loads(v1)) == ["format", "source", "target", "direction", "alignments", "choices", "history"]
    assert json.loads(v1)["format"] == manuscript.WORKFILE_FORMAT
    assert manuscript.parse_workfile(v1) == plain


def test_invalid_v2_content_is_rejected(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)

    def rejected(edit) -> str:
        bad = copy.deepcopy(payload)
        edit(bad)
        with pytest.raises(manuscript.WorkfileError) as raised:
            manuscript.serialize_workfile(bad)
        return raised.value.code

    d = lambda bad: bad["semantic_decisions"][0]  # noqa: E731
    assert rejected(lambda b: d(b).update(extra=1)) == "workfile_invalid_semantic"
    assert rejected(lambda b: d(b).update(applied_text=None)) == "workfile_invalid_semantic"
    assert rejected(lambda b: d(b).update(decision="ignored")) == "workfile_invalid_semantic"
    assert rejected(lambda b: d(b).update(category="nope")) == "workfile_invalid_semantic"
    assert rejected(lambda b: d(b).update(source_span=None, target_span=None)) == "workfile_invalid_semantic"
    # Same revision as the current text, but the span no longer matches it.
    assert rejected(lambda b: d(b)["target_span"].update(text="X")) == "workfile_invalid_semantic"
    assert rejected(lambda b: b["semantic_history"][0]["report"].update(source_revision="0" * 64)) == "workfile_invalid_semantic"
    assert rejected(lambda b: b.update(semantic_history=b["semantic_history"] * 3)) == "workfile_invalid_semantic"
    assert rejected(lambda b: b.update(semantic_decisions=[d(b)] * 129)) in ("workfile_too_many_entries", "workfile_invalid_semantic")
    assert not manuscript.semantic_payload_fits({**payload, "semantic_history": "x"})


def test_decision_for_older_text_is_kept_without_span_check(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)
    payload["semantic_decisions"][0]["target_revision"] = "1" * 64
    payload["semantic_decisions"][0]["target_span"]["text"] = "已经改过的文字"
    assert manuscript.parse_workfile(manuscript.serialize_workfile(payload))["semantic_decisions"]


def test_v1_file_cannot_carry_semantic_keys(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)
    data = json.loads(manuscript.serialize_workfile(payload))
    data["format"] = manuscript.WORKFILE_FORMAT
    with pytest.raises(manuscript.WorkfileError) as raised:
        manuscript.parse_workfile(json.dumps(data))
    assert raised.value.code == "workfile_unsupported_format"


def test_two_maximum_size_snapshots_fit_the_workfile_limits(fixtures) -> None:
    request, report = _pair(fixtures, "max")
    snapshot = {"segment_index": 0, "source": request["source"], "target": request["target"],
                "direction": request["direction"], "report": report}
    payload = manuscript.empty_workfile()
    payload.update(semantic_history=[snapshot, copy.deepcopy(snapshot)])
    assert manuscript.semantic_payload_fits(payload)
