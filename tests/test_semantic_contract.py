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


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _decision(kind: str, source: str, target: str, source_span, target_span, *, fp="f" * 32) -> dict:
    return {"issue_id": "1" * 32, "fingerprint": fp, "category": "omission", "segment_index": 0,
            "source_revision": _sha(source), "target_revision": _sha(target),
            "source_span": source_span, "target_span": target_span, "decision": kind,
            "applied_text": None if kind == "ignored" else "x",
            "basis": {"review_version": "semantic-review-v1", "term_basis": {"dictionary_revision": "a" * 64,
                                                                             "matcher_revision": "a" * 64}}}


def _issue(source_span, target_span, *, ident="i" * 32, fp="f" * 32) -> dict:
    return {"id": ident, "fingerprint": fp, "category": "omission",
            "source_span": source_span, "target_span": target_span}


def test_reconcile_follows_the_web_merge_rules() -> None:
    """Web lib/semantic-decisions.js: only a uniquely recovered ignore with
    exactly one candidate at the recovered place carries over."""
    from wuwaterm_client.semantic_session import reconcile, verified_texts

    old_s, old_t = "第一行\n甲乙", "line one\nAB"
    ss, ts = {"start": 4, "end": 5, "text": "甲"}, {"start": 9, "end": 10, "text": "A"}
    ignored = _decision("ignored", old_s, old_t, ss, ts)
    here = _issue(ss, ts)

    def code(decisions, issues, s=old_s, t=old_t, texts=None):
        return [r["code"] for r in reconcile(decisions, texts or {}, s, t, issues)]

    assert code([ignored], [here]) == ["carried"]
    assert code([], [here]) == []
    assert code([ignored], []) == ["not_reported"]
    # An edit on another line: the old texts from semantic_texts recover the spans.
    new_s, new_t = "改过的第一行\n甲乙", "edited\nAB"
    moved = _issue({"start": 7, "end": 8, "text": "甲"}, {"start": 7, "end": 8, "text": "A"})
    texts = {_sha(old_s): old_s, _sha(old_t): old_t}
    assert code([ignored], [moved], new_s, new_t, texts) == ["carried"]
    assert code([ignored], [moved], new_s, new_t) == ["old_text_unavailable"]
    damaged = verified_texts([{"revision": _sha(old_s), "text": old_s}, {"revision": _sha(old_t), "text": "damaged"}])
    assert code([ignored], [moved], new_s, new_t, damaged) == ["old_text_unavailable"]
    assert code([ignored], [moved], "甲乙\n甲乙", new_t, texts) == ["source_span_unrecoverable"]
    assert code([ignored], [here, _issue(ss, None, ident="j" * 32)]) == ["multiple_candidates"]
    assert code([ignored], [_issue({"start": 5, "end": 6, "text": "乙"}, ts)]) == ["candidate_elsewhere"]
    twin = dict(ignored, issue_id="2" * 32)
    assert code([ignored, twin], [here]) == ["converged_on_one_issue"] * 2
    accepted = _decision("accepted", old_s, old_t, ss, ts)
    assert code([accepted], [here]) == ["previously_applied"]
    assert code([accepted], [_issue({"start": 0, "end": 1, "text": "第"}, ts)]) == ["applied_not_carried"]


def test_semantic_texts_keep_only_verified_texts_ignored_decisions_need() -> None:
    from wuwaterm_client.semantic_session import semantic_texts_for, verified_texts

    old, current = "old", "new"
    ignored = _decision("ignored", old, current, {"start": 0, "end": 1, "text": "o"}, None)
    accepted = _decision("accepted", "gone", current, {"start": 0, "end": 1, "text": "g"}, None)
    known = verified_texts([{"revision": _sha(old), "text": old}, {"revision": _sha("gone"), "text": "bad"}])
    assert known == {_sha(old): old}
    assert semantic_texts_for([ignored, accepted], known, current, current) == [{"revision": _sha(old), "text": old}]
    many = [_decision("ignored", f"t{i}", current, {"start": 0, "end": 1, "text": "t"}, None) for i in range(20)]
    texts = semantic_texts_for(many, {_sha(f"t{i}"): f"t{i}" for i in range(20)}, current, current)
    assert [t["text"] for t in texts] == [f"t{i}" for i in range(4, 20)], "the 16 newest are kept"


# -- manuscript v2 ---------------------------------------------------------


def _v2_payload(fixtures):
    request, report = _pair(fixtures, "minimal")
    prefix_s, prefix_t = "第一段。\n\n", "First.\n\n"
    source, target = prefix_s + request["source"], prefix_t + request["target"]
    issue = next(i for i in report["issues"] if i["suggestion"])
    ts = issue["target_span"]
    span = {"start": ts["start"] + len(prefix_t), "end": ts["end"] + len(prefix_t), "text": ts["text"]}
    decision = _decision("ignored", "旧原文", "old target", None, span)
    decision.update(issue_id=issue["id"], fingerprint=issue["fingerprint"], category=issue["category"],
                    segment_index=1, basis={"review_version": "semantic-review-v1", "term_basis": report["term_basis"]})
    snapshot = {"source": source, "target": target, "direction": request["direction"], "segments": [{
        "segment_index": 1, "source_start": len(prefix_s), "source_end": len(source),
        "target_start": len(prefix_t), "target_end": len(target), "report": report}]}
    payload = manuscript.empty_workfile()
    payload.update(source=source, target=target, semantic_decisions=[decision],
                   semantic_texts=[{"revision": _sha("旧原文"), "text": "旧原文"},
                                   {"revision": _sha("unused"), "text": "unused"}],
                   semantic_history=[snapshot])
    return payload, len(prefix_s)


def test_v2_roundtrip_key_order_and_unreferenced_texts_dropped(fixtures, tmp_path) -> None:
    payload, _ = _v2_payload(fixtures)
    content = manuscript.serialize_workfile(payload)
    data = json.loads(content)
    assert list(data) == ["format", "source", "target", "direction", "alignments", "choices", "history",
                          "semantic_decisions", "semantic_texts", "semantic_history"]
    assert data["format"] == manuscript.WORKFILE_FORMAT_V2
    assert data["semantic_texts"] == [{"revision": _sha("旧原文"), "text": "旧原文"}]
    path = tmp_path / "draft.json"
    manuscript.save_workfile(path, payload)
    loaded = manuscript.load_workfile(path)
    assert loaded["semantic_decisions"] == payload["semantic_decisions"]
    assert loaded["semantic_history"] == payload["semantic_history"]


def test_without_semantic_data_the_file_is_exactly_v1(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)
    plain = {k: v for k, v in payload.items() if not k.startswith("semantic_")}
    v1 = manuscript.serialize_workfile(plain)
    empty = {**plain, "semantic_decisions": [], "semantic_texts": [], "semantic_history": []}
    assert manuscript.serialize_workfile(empty) == v1
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
    invalid = "workfile_invalid_content"
    assert rejected(lambda b: d(b).update(extra=1)) == invalid
    assert rejected(lambda b: d(b).update(applied_text="x")) == invalid
    assert rejected(lambda b: d(b).update(source_revision="A" * 64)) == invalid
    assert rejected(lambda b: d(b).update(source_span=None, target_span=None)) == invalid
    assert rejected(lambda b: d(b)["target_span"].update(text="longer text")) == invalid
    snapshot = lambda b: b["semantic_history"][0]["segments"][0]  # noqa: E731
    assert rejected(lambda b: snapshot(b)["report"].update(source_revision="0" * 64)) == invalid
    assert rejected(lambda b: snapshot(b).update(target_end=10**6)) == invalid
    # The writer keeps the last two snapshots; a file with three is refused.
    three = json.loads(manuscript.serialize_workfile(payload))
    assert len(json.loads(manuscript.serialize_workfile({**payload, "semantic_history": payload["semantic_history"] * 3}))["semantic_history"]) == 2
    three["semantic_history"] *= 3
    with pytest.raises(manuscript.WorkfileError) as raised:
        manuscript.parse_workfile(json.dumps(three, ensure_ascii=False))
    assert raised.value.code == invalid
    assert rejected(lambda b: b.update(semantic_decisions=[d(b)] * 129)) == "workfile_too_many_entries"
    assert rejected(lambda b: b.update(semantic_decisions=[d(b), dict(d(b), issue_id="3" * 32)])) == \
        "workfile_duplicate_choices"
    text = {"revision": _sha("旧原文"), "text": "旧原文"}
    bad = json.loads(manuscript.serialize_workfile(payload))
    bad["semantic_texts"] = [text, text]
    with pytest.raises(manuscript.WorkfileError):
        manuscript.parse_workfile(json.dumps(bad, ensure_ascii=False))


def test_a_damaged_old_text_is_kept_as_structure_and_not_trusted(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)
    data = json.loads(manuscript.serialize_workfile(payload))
    data["semantic_texts"][0]["text"] = "被改动的旧文"
    loaded = manuscript.parse_workfile(json.dumps(data, ensure_ascii=False))
    from wuwaterm_client.semantic_session import verified_texts
    assert loaded["semantic_decisions"] == payload["semantic_decisions"]
    assert verified_texts(loaded["semantic_texts"]) == {}


def test_oversized_files_drop_oldest_history_and_never_a_decision(fixtures) -> None:
    payload, _ = _v2_payload(fixtures)
    request, report = _pair(fixtures, "max")
    filler = "填" * 150_000
    big = {"source": filler + request["source"], "target": request["target"], "direction": request["direction"],
           "segments": [{"segment_index": 0, "source_start": len(filler), "source_end": len(filler) + len(request["source"]),
                         "target_start": 0, "target_end": len(request["target"]), "report": report}]}
    payload["semantic_history"] = [big, copy.deepcopy(big)]
    written = json.loads(manuscript.serialize_workfile(payload))
    assert len(written["semantic_history"]) == 1
    assert written["semantic_decisions"] == payload["semantic_decisions"]
    payload["source"] = payload["target"] = "长" * 400_000
    payload["semantic_history"] = []
    with pytest.raises(manuscript.WorkfileError) as raised:
        manuscript.serialize_workfile(payload)
    assert raised.value.code == "workfile_too_large"
