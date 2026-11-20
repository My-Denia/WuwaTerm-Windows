"""Long-manuscript segment records at runtime (public Web contracts/manuscript-v3/SPEC.md).

Pure: needs only pytest, plus the pinned Web checkout for the fixture checks.
The behaviour tests use synthetic text and a stand-in for Core's review-v2
responses; nothing is sent anywhere.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from wuwaterm_client import manuscript
from wuwaterm_client import segment_records as sr
from wuwaterm_client.segmentation import pair_segments

NOT_CURRENT_INPUTS = ("valid-v3-hash-not-current.json", "valid-v3-inputs-not-current.json",
                      "valid-v3-2000-core-8-offered.json")
NOT_SEGMENTATION = ("valid-v3-512-segments.json", "valid-v3-2000-core-8-offered.json")
EMPTY_LIST_DIGEST = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def _fixtures(contract_source) -> dict[str, dict]:
    """Every unpadded valid v3 fixture with records, read by the client's parser."""
    folder = contract_source("web") / "contracts" / "manuscript-v3"
    out = {}
    for case in json.loads((folder / "cases.json").read_text(encoding="utf-8"))["valid"]:
        if "pad_to_bytes" in case or case["file"] in out:
            continue
        value = manuscript.parse_workfile((folder / case["file"]).read_text(encoding="utf-8"))
        if value["format"] == manuscript.WORKFILE_FORMAT_V3 and value["segments"]:
            out[case["file"]] = value
    return out


def _cores(record: dict) -> tuple:
    return (record["source_start"], record["source_end"]), (record["target_start"], record["target_end"])


def _input(value: dict, record: dict) -> str:
    return sr.alignment_input(value["alignments"], value["source"], value["target"], *_cores(record))


def test_fixture_hashes_ranges_and_request_inputs(contract_source) -> None:
    """Reference test g: the stored hashes, ranges and inputs agree with this client's rules."""
    fixtures = _fixtures(contract_source)
    for name, value in fixtures.items():
        for record in value["segments"]:
            (a, b), (c, d) = _cores(record)
            now = sr.content_hash(value["source"][a:b]), sr.content_hash(value["target"][c:d])
            if (name, record["index"]) == ("valid-v3-hash-not-current.json", 1):
                assert record["source_hash"] != now[0] and record["target_hash"] == now[1]
            else:
                assert (record["source_hash"], record["target_hash"]) == now, (name, record["index"])
        if name not in NOT_SEGMENTATION:
            pairs = pair_segments(value["source"], value["target"], 1800, value["alignments"])
            assert [(p.status, (p.source.core_start, p.source.core_end), (p.target.core_start, p.target.core_end))
                    for p in pairs] == [("ready", *_cores(r)) for r in value["segments"]], name
        if name not in NOT_CURRENT_INPUTS:
            for record in value["segments"]:
                if record["status"] != "pending":
                    assert (record["direction"], record["alignment_input"]) == (value["direction"], _input(value, record))
    explicit = [r["alignment_input"] for r in fixtures["valid-v3-explicit-alignments.json"]["segments"]
                if r["status"] != "pending"]
    assert explicit and all(i != manuscript.ALIGNMENT_AUTOMATIC for i in explicit)
    inputs = fixtures["valid-v3-inputs-not-current.json"]
    one, two = inputs["segments"][1], inputs["segments"][2]
    assert one["direction"] != inputs["direction"] and one["alignment_input"] == _input(inputs, one)
    assert two["direction"] == inputs["direction"] and two["alignment_input"] != _input(inputs, two)
    spec = (contract_source("web") / "contracts" / "manuscript-v3" / "SPEC.md").read_text(encoding="utf-8")
    assert EMPTY_LIST_DIGEST in spec


def test_fixture_reuse_candidates_and_reopening(contract_source) -> None:
    """Only the not-current fixture records fail the content and input check, and
    normalizing a file changes only what is not current."""
    not_candidates = {("valid-v3-inputs-not-current.json", 1), ("valid-v3-inputs-not-current.json", 2),
                      ("valid-v3-hash-not-current.json", 1)}
    seen = set()
    for name, value in _fixtures(contract_source).items():
        for record in value["segments"]:
            if record["status"] == "pending":
                continue
            # A stale record is never a candidate; with its status set aside, the
            # hashes, direction and alignment input alone decide.
            candidate = sr.reuse_candidate({**record, "status": "done"}, value)
            assert candidate == ((name, record["index"]) not in not_candidates), (name, record["index"])
            assert sr.reuse_candidate(record, value) == (candidate and record["status"] == "done")
            seen.add((name, record["index"]))
        if name in NOT_SEGMENTATION:
            continue
        parts = sr.normalize(value, pair_segments(value["source"], value["target"], 1800, value["alignments"]))
        statuses = [r["status"] for r in parts.segments]
        if name == "valid-v3-inputs-not-current.json":
            assert statuses == ["pending", "pending", "stale"] and parts.progress["completed"] == [2]
        elif name == "valid-v3-hash-not-current.json":
            assert statuses == ["pending", "pending", "stale"]
        else:
            assert parts == sr.parts_of(value), name
    assert not_candidates <= seen


def test_alignment_input_rule_example() -> None:
    """Reference test g2."""
    def span(start: int, end: int, text: str) -> dict:
        return {"start": start, "end": end, "text": text}

    source, target, cores = "abcdefghij", "ABCDEFGHIJKLMNOP", ((2, 6), (3, 9))
    alignments = [{"source": span(0, 4, "abcd"), "target": span(0, 5, "ABCDE")},
                  {"source": span(4, 8, "efgh"), "target": None},
                  {"source": span(8, 10, "ij"), "target": span(12, 14, "MN")}]
    expected = [{"source": span(0, 2, "cd"), "target": span(0, 2, "DE")}, {"source": span(2, 4, "ef"), "target": None}]
    assert sr.alignment_list(alignments, source, target, *cores) == expected
    minified = ('[{"source":{"start":0,"end":2,"text":"cd"},"target":{"start":0,"end":2,"text":"DE"}},'
                '{"source":{"start":2,"end":4,"text":"ef"},"target":null}]')
    assert sr.alignment_input(alignments, source, target, *cores) == hashlib.sha256(minified.encode()).hexdigest()
    outside = [{"source": span(2, 6, "cdef"), "target": span(10, 12, "KL")}]
    assert sr.alignment_input(outside, source, target, *cores) == EMPTY_LIST_DIGEST
    assert sr.alignment_input([], source, target, *cores) == EMPTY_LIST_DIGEST
    assert sr.alignment_input(None, source, target, *cores) == "automatic"


# ---- behaviour, with a stand-in for Core ----

def _candidate(zh: str, en: str, category: str = "character") -> dict:
    return {"candidate_id": hashlib.sha256(f"{zh}|{en}|{category}".encode()).hexdigest(),
            "zh": zh, "en": en, "category": category}


TERMS = {"甲术": [_candidate("甲术", "Alpha")], "乙术": [_candidate("乙术", "Beta"), _candidate("乙术", "Bet", "item")],
         "丙术": [_candidate("丙术", "Gamma")], "丁术": [_candidate("丁术", "Delta")]}
B1 = {"rule_version": "review-v2", "dictionary": {"schema_version": "test-schema", "source_commit": "test-commit",
                                                  "term_count": 4, "revision": "1" * 64}, "matcher_revision": "2" * 64}
B2 = {**B1, "matcher_revision": "3" * 64}
B3 = {**B1, "dictionary": {**B1["dictionary"], "revision": "4" * 64}}


def core_report(request: sr.Request, server: dict, terms: dict = TERMS, truncated: tuple = (), *,
                verdict: str = "needs_review", reason: str | None = None) -> dict:
    """What Core answers for the request under the server's basis: one finding per occurrence.

    ``verdict`` is what the stand-in says about them; a first candidate's English
    rendering that the target core really holds becomes the finding's target span."""
    found = sorted((start, surface) for surface in terms
                   for start in range(len(request.source)) if request.source.startswith(surface, start))
    findings = []
    for source_start, term in found:
        at = request.target.find(terms[term][0]["en"])
        finding = {"id": f"{source_start}:{source_start + len(term)}:{term}", "verdict": verdict,
                   "rule_id": "review.term_pair",
                   "source_span": {"start": source_start, "end": source_start + len(term), "text": term},
                   "target_span": None if at < 0 else {
                       "start": at, "end": at + len(terms[term][0]["en"]), "text": terms[term][0]["en"]},
                   "candidates": [{"zh": c["zh"], "en": c["en"], "category": c["category"], "sources": [],
                                   "candidate_id": c["candidate_id"]} for c in terms[term]],
                   "candidates_truncated": term in truncated}
        if reason is not None:
            finding["reason"] = reason
        findings.append(finding)
    not_evaluated = sum(1 for f in findings if f["verdict"] == "not_evaluated")
    return {"request_id": "synthetic", "source_revision": sr.content_hash(request.source),
            "target_revision": sr.content_hash(request.target), "rule_version": "review-v2",
            "matcher_revision": server["matcher_revision"], "dictionary": dict(server["dictionary"]),
            "coverage": {"evaluated": len(findings) - not_evaluated, "not_evaluated": not_evaluated,
                         "rules": list(sr.COVERAGE_RULES)},
            "findings": findings, "truncated": False}


def manuscript_of(sizes: list[int], words: dict[int, str]) -> dict:
    """Paragraphs of the given source sizes; ``words`` puts terms at the start of a paragraph."""
    def paragraph(i: int, size: int) -> str:
        head = f"段{i:03d}{words.get(i, '')}"
        return head + "文" * (size - len(head))
    return {"source": "\n\n".join(paragraph(i, n) for i, n in enumerate(sizes)),
            "target": "\n\n".join(f"Paragraph {i:03d} ".ljust(400, "x") for i in range(len(sizes))),
            "direction": "en", "alignments": None, "choices": [], "history": []}


def pairs_of(payload: dict) -> list:
    return pair_segments(payload["source"], payload["target"], 1800, payload["alignments"] or None)


def check(payload: dict, index: int, basis: dict | None, server: dict, **report) -> tuple[dict, dict, sr.Applied]:
    """One segment check as the view runs it: the planned request, then one more when ``again``."""
    request = sr.plan_request(payload, index, basis)
    for _ in range(3):
        applied = sr.apply_response(payload, request, core_report(request, server, **report), basis)
        payload, basis = sr.with_parts(payload, applied.parts), applied.basis
        if not applied.again:
            break
        request = sr.plan_request(payload, index, basis)
    return payload, basis, applied


def review_all(payload: dict, server: dict, basis: dict | None = None, **report) -> tuple[dict, dict]:
    payload = sr.with_parts(payload, sr.normalize(payload, pairs_of(payload), basis))
    attempted: set[int] = set()
    while (index := sr.next_index(payload, pairs_of(payload), basis, attempted)) is not None:
        payload, basis, _ = check(payload, index, basis, server, **report)
        attempted.add(index)
    return payload, basis


def choose_all(payload: dict, index: int, choices: list[tuple]) -> dict:
    for start, choice, candidate_id in choices:
        payload = sr.with_parts(payload, sr.choose(payload, index, start, start + 2, choice, candidate_id))
    return payload


def statuses(payload: dict) -> list[str]:
    return [r["status"] for r in sr.parts_of(payload).segments]


def test_reopening_costs_one_probe_and_a_matcher_change_makes_done_records_stale() -> None:
    payload, basis = review_all(manuscript_of([500] * 12, {1: "甲术"}), B1)
    assert basis == B1 and statuses(payload) == ["done"] * 4
    parts = sr.set_current(sr.parts_of(payload), 2)
    payload = sr.with_parts(payload, sr.set_finished(sr.set_finished(parts, 0, True), 1, True))
    reopened = manuscript.parse_workfile(manuscript.serialize_workfile(payload))
    pairs = pairs_of(reopened)
    assert sr.normalize(reopened, pairs) == sr.parts_of(reopened)
    # No basis yet: nothing needs a check, so the probe goes to progress.current.
    probe = sr.plan_request(reopened, sr.next_index(reopened, pairs, None), None)
    assert probe.index == 2 and probe.resolutions == [] and probe.resolution_context is None
    applied = sr.apply_response(reopened, probe, core_report(probe, B1), None)
    assert (applied.outcome, applied.basis, applied.parts) == ("current", B1, sr.parts_of(reopened))
    assert sr.next_index(reopened, pairs, B1) is None
    # Only the matcher revision changes: that basis becomes current, the other done records turn stale.
    request = sr.plan_request(reopened, 3, B1)
    applied = sr.apply_response(reopened, request, core_report(request, B2), B1)
    after = sr.with_parts(reopened, applied.parts)
    assert applied.basis == B2 and statuses(after) == ["stale", "stale", "stale", "done"]
    assert [sr.needs_check(r, after, B2) for r in applied.parts.segments] == [True, True, True, False]
    assert applied.parts.progress == {"current": 2, "completed": [0, 1]}
    assert sr.next_index(after, pairs, B2) == 0
    assert sr.next_index(after, pairs, None) == 2   # a probe prefers progress.current when it needs a check
    # A reused record renders as the report it was compacted from, without request id and sources.
    rebuilt = sr.expand_result(applied.parts.segments[3], after["source"], after["target"])
    assert rebuilt == {**core_report(request, B2), "request_id": None}


def test_realignment_keeps_matching_choices_confirms_them_and_never_drops_one() -> None:
    payload, basis = review_all(manuscript_of([500] * 12, {0: "甲术乙术丙术丁术"}), B1)
    payload = choose_all(payload, 0, [(4, sr.OFFICIAL_PAIR, TERMS["甲术"][0]["candidate_id"]), (6, sr.NOT_A_TERM, None),
                                      (8, sr.OFFICIAL_PAIR, TERMS["丙术"][0]["candidate_id"]),
                                      (10, sr.OFFICIAL_PAIR, TERMS["丁术"][0]["candidate_id"])])
    assert statuses(payload)[0] == "stale"
    payload, basis, applied = check(payload, 0, basis, B1)
    assert applied.outcome == "stored" and statuses(payload)[0] == "done"
    # Core's dictionary moved on: 丙术 has another rendering, 丁术 is no longer a term, and
    # 乙术 is now reported with candidates_truncated. Core rejects the stale context.
    terms = {"甲术": TERMS["甲术"], "乙术": TERMS["乙术"], "丙术": [_candidate("丙术", "Gamma Prime")]}
    assert len(sr.plan_request(payload, 0, basis).resolutions) == 4
    fallback = sr.plan_request(payload, 0, basis, allow_resolutions=False)
    assert fallback.resolutions == [] and fallback.resolution_context is None and fallback.realign
    applied = sr.apply_response(payload, fallback, core_report(fallback, B3, terms, ("乙术",)), basis)
    record = applied.parts.segments[0]
    assert [(c["source_span"]["text"], c["confirmed"]) for c in record["choices"]] == [
        ("甲术", True), ("乙术", True), ("丙术", False), ("丁术", False)]
    assert record["status"] == "stale" and applied.again and applied.basis == B3
    payload = sr.with_parts(payload, applied.parts)
    again = sr.plan_request(payload, 0, B3)
    assert again.resolutions == [
        {"mention_id": "4:6:甲术", "choice": "official_pair", "candidate_id": TERMS["甲术"][0]["candidate_id"]},
        {"mention_id": "6:8:乙术", "choice": "not_a_term"}]
    assert again.resolution_context == {"source_revision": sr.content_hash(again.source), "rule_version": "review-v2",
                                        "dictionary_revision": "4" * 64, "matcher_revision": "2" * 64}
    final = sr.apply_response(payload, again, core_report(again, B3, terms, ("乙术",)), B3)
    assert final.parts.segments[0]["status"] == "done" and not final.again
    assert len(final.parts.segments[0]["choices"]) == 4
    # Removing an unconfirmed choice leaves the result exact; removing a confirmed one does not.
    payload = sr.with_parts(payload, final.parts)
    assert sr.unchoose(payload, 0, 8, 10).segments[0]["status"] == "done"
    assert sr.unchoose(payload, 0, 4, 6).segments[0]["status"] == "stale"


def _fixture_after_target_edit(contract_source, at_core_end: bool) -> tuple[dict, dict, dict]:
    """valid-v3-all-parts.json read by the real parser, with record 1's choice for
    示例名 unconfirmed (its finding and candidate still exist; 示例物 stays
    confirmed, 示例城 has no finding), then one scalar typed in record 1's
    target and the parts normalized as the view does.
    Returns the payload, the session basis (record 1's) and a Core stand-in's
    term table taken from the fixture's own result."""
    folder = contract_source("web") / "contracts" / "manuscript-v3"
    raw = json.loads((folder / "valid-v3-all-parts.json").read_text(encoding="utf-8"))
    choice = raw["segments"][1]["choices"][0]
    assert choice["source_span"]["text"] == "示例名" and choice["confirmed"]
    choice["confirmed"] = False
    payload = manuscript.parse_workfile(json.dumps(raw, ensure_ascii=False))
    record = payload["segments"][1]
    basis = record["basis"]
    assert statuses(payload) == ["pending", "done", "stale"]
    assert [c["confirmed"] for c in record["choices"]] == [False, True, False]
    core = payload["source"][record["source_start"]:record["source_end"]]
    terms = {core[f["source"][0]:f["source"][1]]: f["candidates"] for f in record["result"]["findings"]}
    assert sorted(terms) == ["示例名", "示例物"]
    before = payload["target"]
    # Typed right after the core is no hit for apply_edit: only normalize sees the core grow.
    at = record["target_end"] if at_core_end else record["target_start"] + 40
    edited = sr.with_parts({**payload, "target": before[:at] + "加" + before[at:]},
                           sr.apply_edit(sr.parts_of(payload), before, "target", at, at, 1))
    edited = sr.with_parts(edited, sr.normalize(edited, pairs_of(edited), basis))
    assert statuses(edited) == ["pending", "pending", "stale"]
    # SPEC step 6: the choices keep their confirmed values until the re-check ...
    assert [c["confirmed"] for c in edited["segments"][1]["choices"]] == [False, True, False]
    # ... and the file written meanwhile holds a pending record with its choices unconfirmed.
    meanwhile = manuscript.parse_workfile(manuscript.serialize_workfile(edited))
    assert meanwhile["segments"][1]["status"] == "pending"
    assert [c["confirmed"] for c in meanwhile["segments"][1]["choices"]] == [False, False, False]
    assert "prior" not in manuscript.serialize_workfile(edited)
    return edited, basis, terms


def _recheck_record_1(payload: dict, basis: dict, terms: dict) -> tuple[dict, dict, list]:
    """Record 1's check as the view runs it: the planned request, then one more while ``again``."""
    sent = []
    request = sr.plan_request(payload, 1, basis)
    for _ in range(3):
        sent.append(request)
        applied = sr.apply_response(payload, request, core_report(request, basis, terms, ("示例物",)), basis)
        assert applied.outcome == "stored"
        payload, basis = sr.with_parts(payload, applied.parts), applied.basis
        if not applied.again:
            break
        request = sr.plan_request(payload, 1, basis)
    return payload, basis, sent


@pytest.mark.parametrize("at_core_end", [False, True], ids=["typed-inside-the-core", "typed-at-the-core-end"])
def test_a_target_edit_re_check_never_confirms_a_choice_that_was_unconfirmed(contract_source, at_core_end) -> None:
    """SPEC step 6, the target-only paragraph: the re-check realigns, but a choice
    that was unconfirmed before the edit stays unconfirmed even though its
    finding and candidate still exist; one that was confirmed and still
    matches is confirmed again."""
    payload, basis, terms = _fixture_after_target_edit(contract_source, at_core_end)
    payload, basis, sent = _recheck_record_1(payload, basis, terms)
    assert statuses(payload) == ["pending", "done", "stale"]
    written = manuscript.parse_workfile(manuscript.serialize_workfile(payload))
    record = written["segments"][1]
    assert record["status"] == "done"
    assert [(c["source_span"]["text"], c["confirmed"]) for c in record["choices"]] == [
        ("示例名", False), ("示例物", True), ("示例城", False)]
    # This client first realigns without resolutions, then sends what was confirmed before the edit.
    assert [r.resolutions for r in sent] == [[], [{"mention_id": "6:9:示例物", "choice": "not_a_term"}]]


def test_a_new_direction_ends_what_a_target_edit_kept_even_when_it_is_switched_back(contract_source) -> None:
    """A direction change leaves every choice unconfirmed (Web drops what it remembers),
    so after switching back the re-check realigns by step 7: every matching choice is confirmed."""
    payload, basis, terms = _fixture_after_target_edit(contract_source, False)
    for direction in ("zh", "en"):
        payload = {**payload, "direction": direction}
        payload = sr.with_parts(payload, sr.normalize(payload, pairs_of(payload), basis))
        assert [c["confirmed"] for c in payload["segments"][1]["choices"]] == [False, False, False]
        assert "prior" not in payload["segments"][1]
    payload, _, sent = _recheck_record_1(payload, basis, terms)
    written = manuscript.parse_workfile(manuscript.serialize_workfile(payload))
    assert [(c["source_span"]["text"], c["confirmed"]) for c in written["segments"][1]["choices"]] == [
        ("示例名", True), ("示例物", True), ("示例城", False)]


def test_an_edit_inside_one_segment_makes_only_that_record_pending() -> None:
    payload, basis = review_all(manuscript_of([500] * 12, {4: "甲术丙术"}), B1)
    # Paragraph 4 is the second of segment 1, so its terms start at 502 + 4 in the core.
    payload = choose_all(payload, 1, [(506, sr.OFFICIAL_PAIR, TERMS["甲术"][0]["candidate_id"]),
                                      (508, sr.OFFICIAL_PAIR, TERMS["丙术"][0]["candidate_id"])])
    payload, basis, _ = check(payload, 1, basis, B1)
    payload = sr.with_parts(payload, sr.set_finished(sr.set_finished(sr.parts_of(payload), 0, True), 1, True))
    old = sr.parts_of(payload)
    before = payload["source"]
    # A scalar typed right after core 0 is no hit, so only normalize finds that core 0 grew.
    end = old.segments[0]["source_end"]
    typed = sr.with_parts({**payload, "source": before[:end] + "加" + before[end:]},
                          sr.apply_edit(old, before, "source", end, end, 1))
    with pytest.raises(ValueError):
        sr.next_index(typed, pairs_of(typed), basis)
    at = old.segments[1]["source_start"] + 507   # inside the chosen 甲术
    parts = sr.apply_edit(old, before, "source", at, at + 1, 0)
    edited = sr.with_parts({**payload, "source": before[:at] + before[at + 1:]}, parts)
    assert statuses(edited) == ["done", "pending", "done", "done"] and parts.segments[0] is old.segments[0]
    assert parts.segments[2]["source_start"] == old.segments[2]["source_start"] - 1
    assert parts.segments[2]["result"] is old.segments[2]["result"]
    gamma = old.segments[1]["choices"][1]
    assert parts.segments[1]["choices"] == [
        {**gamma, "source_span": {"start": 507, "end": 509, "text": "丙术"}, "confirmed": False}]
    (a, b), _ = _cores(old.segments[1])
    assert parts.history == [{"source_hash": sr.content_hash(before[a:b]), "source_span": {"start": 506, "end": 508,
                              "text": "甲术"}, "choice": "official_pair", "candidate": TERMS["甲术"][0], "offered": []}]
    assert parts.progress["completed"] == [0]
    normalized = sr.normalize(edited, pairs_of(edited), basis)
    assert statuses(sr.with_parts(edited, normalized)) == ["done", "pending", "done", "done"]
    assert normalized.segments[1]["choices"] == parts.segments[1]["choices"]
    assert normalized.segments[2] is parts.segments[2]
    assert sr.normalize(sr.with_parts(edited, normalized), pairs_of(edited), basis) == normalized
    # The re-check realigns the kept choice against its findings and confirms it in a second request.
    checked, _, applied = check(sr.with_parts(edited, normalized), 1, basis, B1)
    assert applied.outcome == "stored" and statuses(checked)[1] == "done"
    assert [c["confirmed"] for c in sr.parts_of(checked).segments[1]["choices"]] == [True]
    # Typing the scalar back makes the core read as before, so the history entry can come back.
    retyped = sr.with_parts({**edited, "source": before}, sr.apply_edit(normalized, edited["source"], "source", at, at, 1))
    retyped = sr.with_parts(retyped, sr.normalize(retyped, pairs_of(retyped), basis))
    assert sr.restorable(retyped) == [(0, 1)]
    restored = sr.restore(retyped, 0, 1)
    assert restored.history == [] and [(c["source_span"]["start"], c["confirmed"])
                                       for c in restored.segments[1]["choices"]] == [(508, False), (506, False)]


def test_normalize_maps_records_across_a_boundary_shift_and_is_idempotent() -> None:
    # Segments: paragraphs 0-2, paragraph 3 alone (1,700 scalars), paragraphs 4-6.
    payload, basis = review_all(manuscript_of([500, 500, 500, 1700, 500, 500, 500], {2: "甲术", 3: "丙术"}), B1)
    payload = choose_all(payload, 0, [(1008, sr.OFFICIAL_PAIR, TERMS["甲术"][0]["candidate_id"])])
    payload = choose_all(payload, 1, [(4, sr.OFFICIAL_PAIR, TERMS["丙术"][0]["candidate_id"])])
    for index in (0, 1):
        payload, basis, _ = check(payload, index, basis, B1)
    payload = sr.with_parts(payload, sr.set_finished(sr.set_finished(sr.parts_of(payload), 0, True), 1, True))
    # 400 scalars into paragraph 0 push paragraph 2 into a segment of its own.
    before = payload["source"]
    parts = sr.apply_edit(sr.parts_of(payload), before, "source", 100, 100, 400)
    edited = sr.with_parts({**payload, "source": before[:100] + "增" * 400 + before[100:]}, parts)
    pairs = pairs_of(edited)
    assert len(pairs) == 4
    normalized = sr.normalize(edited, pairs, basis)
    after = sr.with_parts(edited, normalized)
    assert statuses(after) == ["pending", "pending", "done", "done"]
    assert normalized.segments[2] == {**parts.segments[1], "index": 2}
    assert normalized.segments[3] == {**parts.segments[2], "index": 3}
    alpha = parts.segments[0]["choices"][0]
    assert normalized.segments[1]["choices"] == [{**alpha, "source_span": {"start": 4, "end": 6, "text": "甲术"}}]
    assert normalized.segments[0]["choices"] == [] and normalized.history == []
    assert normalized.progress == {"current": None, "completed": [2]}
    assert sr.normalize(after, pairs, basis) == normalized


def _short_after_edit() -> tuple[dict, sr.Parts]:
    """A two-segment manuscript with a choice in each, then cut below 2,000 scalars per side."""
    payload, basis = review_all(manuscript_of([700, 700, 700], {0: "甲术", 2: "丙术"}), B1)
    payload = choose_all(payload, 0, [(4, sr.OFFICIAL_PAIR, TERMS["甲术"][0]["candidate_id"])])
    payload = choose_all(payload, 1, [(4, sr.OFFICIAL_PAIR, TERMS["丙术"][0]["candidate_id"])])
    before = payload["source"]
    parts = sr.apply_edit(sr.parts_of(payload), before, "source", 1000, 1200, 0)
    return sr.with_parts({**payload, "source": before[:1000] + before[1200:]}, parts), parts


def test_leaving_long_mode_moves_every_choice_to_history() -> None:
    edited, parts = _short_after_edit()
    assert not sr.long_mode(edited["source"], edited["target"])
    normalized = sr.normalize(edited, pairs_of(edited))
    hashes = [sr.content_hash(edited["source"][r["source_start"]:r["source_end"]]) for r in parts.segments]
    assert normalized.segments == [] and normalized.progress == {"current": None, "completed": []}
    assert [(e["source_hash"], e["source_span"]["text"]) for e in normalized.history] == [
        (hashes[0], "甲术"), (hashes[1], "丙术")]
    saved = sr.with_parts(edited, normalized)
    assert json.loads(manuscript.serialize_workfile(saved))["format"] == manuscript.WORKFILE_FORMAT_V3
    assert "segments" not in sr.with_parts(saved, sr.Parts([], [], {"current": None, "completed": []}))


def test_more_than_512_history_entries_fail_with_the_code() -> None:
    edited, _ = _short_after_edit()
    entry = {"source_hash": "0" * 64, "source_span": {"start": 0, "end": 2, "text": "甲术"}, "choice": "not_a_term",
             "candidate": None, "offered": []}
    full = {**edited, "segment_choice_history": [entry] * 511}
    with pytest.raises(manuscript.WorkfileError) as error:
        sr.normalize(full, pairs_of(full))
    assert error.value.code == "workfile_too_many_entries"
    assert len(sr.normalize(full, pairs_of(full), check_limits=False).history) == 513


# ---- saved results: a reopened manuscript before its live basis check ----
# P2 #2: a saved result was computed under a basis this session has not checked
# against the live server. It is listed as usual, but it is never settled: its
# places count as pending work and its group is never 已核 (``confirmed``).

def _reviewed_term(surface: str = "椿", en: str = "Camellya", *,
                   verdict: str = "verified_constraint", reason: str | None = None) -> tuple[dict, dict]:
    """A long manuscript holding exactly one ``surface`` term, reviewed under B1 by a
    stand-in Core that reports ``verdict`` for it."""
    payload = {
        "source": f"庭院深处，{surface}静静注视着枝头。" + "今汐在城楼远望。" * 300,
        "target": f"Deep in the courtyard, {en} quietly watched the branch. " + "Jinhsi looked afar. " * 300,
        "direction": "en", "alignments": None, "choices": [], "history": [],
    }
    terms = {surface: [_candidate(surface, en)]}
    return review_all(payload, B1, terms=terms, verdict=verdict, reason=reason)


def _term_groups(payload: dict, basis: dict | None) -> list[dict]:
    return sr.group_term_occurrences(sr.parts_of(payload).segments, pairs_of(payload),
                                     payload["source"], payload["target"], payload["direction"],
                                     alignments=payload["alignments"], basis=basis)


def test_a_saved_result_is_pending_until_the_live_basis_is_checked() -> None:
    """P2 #2: 椿 -> Camellya reads the same in all three states — but only the checked
    basis may call it settled. Saved: 待处理 with one pending place, never 已核."""
    payload, basis = _reviewed_term()
    assert basis == B1 and statuses(payload) == ["done"] * len(pairs_of(payload))

    (current,) = _term_groups(payload, B1)
    assert (current["status"], current["pending_count"]) == ("confirmed", 0)
    assert current["saved_count"] == 0
    assert sr.group_matches_filter(current, "confirmed") and not sr.group_matches_filter(current, "pending")
    assert [(o["freshness"], o["attention"]) for o in current["occurrences"]] == [("current", "ok")]

    (saved,) = _term_groups(payload, None)
    assert (saved["status"], saved["pending_count"]) == ("pending", 1)
    assert sr.group_matches_filter(saved, "pending") and not sr.group_matches_filter(saved, "confirmed")
    assert [(o["freshness"], o["attention"]) for o in saved["occurrences"]] == [("saved", "pending")]
    assert saved["saved_count"] == 1
    assert saved["confirmed_count"] == 0
    # Still displayed: the group, its count, its distribution and the place itself.
    assert (saved["surface"], saved["count"], saved["segments"]) == ("椿", 1, [1])
    assert saved["distribution_text"] == current["distribution_text"] == "Camellya ×1"
    assert [(o["surface"], o["actual_translation"]) for o in saved["occurrences"]] == [("椿", "Camellya")]

    (stale,) = _term_groups(payload, B2)
    assert (stale["status"], stale["pending_count"]) == ("pending", 1)
    assert stale["saved_count"] == 0
    assert sr.group_matches_filter(stale, "pending") and not sr.group_matches_filter(stale, "confirmed")
    assert [(o["freshness"], o["attention"]) for o in stale["occurrences"]] == [("stale", "pending")]


def test_a_saved_group_is_never_confirmed_even_with_a_confirmed_choice() -> None:
    """A choice confirmed under a saved result does not settle the place: the choice
    is recorded, but the basis it was confirmed against is not the live one."""
    payload, basis = _reviewed_term()
    terms = {"椿": [_candidate("椿", "Camellya")]}
    start = sr.parts_of(payload).segments[0]["result"]["findings"][0]["source"][0]
    payload = sr.with_parts(payload, sr.choose(payload, 0, start, start + 1, sr.NOT_A_TERM, None))
    payload, basis, applied = check(payload, 0, basis, B1, terms=terms, verdict="verified_constraint")
    assert applied.outcome == "stored" and statuses(payload)[0] == "done"
    assert [(c["choice"], c["confirmed"]) for c in sr.parts_of(payload).segments[0]["choices"]] == [
        (sr.NOT_A_TERM, True)]

    (current,) = _term_groups(payload, B1)
    assert (current["status"], current["pending_count"]) == ("confirmed", 0)
    assert [o["attention"] for o in current["occurrences"]] == ["settled"]

    (saved,) = _term_groups(payload, None)
    # The saved place keeps its confirmed choice and still waits for the check.
    assert saved["occurrences"][0]["choice_state"] == "confirmed"
    assert (saved["status"], saved["pending_count"]) == ("pending", 1)
    assert saved["saved_count"] == 1
    assert [o["attention"] for o in saved["occurrences"]] == ["pending"]
    assert sr.group_matches_filter(saved, "pending") and not sr.group_matches_filter(saved, "confirmed")


def test_a_saved_conflict_is_still_a_decision_to_make() -> None:
    """A recorded conflict is not a settled place either, but it is not work for the
    basis check alone: the group stays 不一致 so the decision is not hidden."""
    payload, _ = _reviewed_term(verdict="confirmed_conflict")
    (current,) = _term_groups(payload, B1)
    assert current["status"] == "inconsistent" and current["pending_count"] == 1
    (saved,) = _term_groups(payload, None)
    assert [o["attention"] for o in saved["occurrences"]] == ["conflict"]
    assert saved["status"] == "inconsistent"
    assert saved["saved_count"] == 1
    assert sr.group_matches_filter(saved, "pending") and not sr.group_matches_filter(saved, "confirmed")


def test_a_saved_place_the_live_check_never_evaluated_still_waits_for_the_check() -> None:
    """A saved result whose place was not evaluated (普通词义) is not 未评估 work either:
    nothing in a saved result is verified, so the check comes first."""
    payload, _ = _reviewed_term(verdict="not_evaluated", reason="ordinary_word")
    (current,) = _term_groups(payload, B1)
    assert (current["status"], current["not_evaluated_count"]) == ("not_evaluated", 1)
    assert sr.group_matches_filter(current, "not_evaluated")
    (saved,) = _term_groups(payload, None)
    assert (saved["status"], saved["pending_count"]) == ("pending", 1)
    assert saved["saved_count"] == 1
    assert saved["not_evaluated_count"] == 0 and saved["occurrences"][0]["verdict"] == "not_evaluated"
    assert saved["occurrences"][0]["verdict_label"] == "未评估（普通词义）"
    assert sr.group_matches_filter(saved, "pending") and not sr.group_matches_filter(saved, "confirmed")


def test_showing_a_saved_result_changes_nothing_in_the_file() -> None:
    """The saved state is runtime only: grouping reads the records and leaves the v3
    payload as it was, so reopening a manuscript never clears or rewrites a report."""
    payload, _ = _reviewed_term()
    before = manuscript.serialize_workfile(payload)
    for basis in (None, B1, B2):
        groups = _term_groups(payload, basis)
        assert [g["count"] for g in groups] == [len(g["occurrences"]) for g in groups] == [1]
    assert manuscript.serialize_workfile(payload) == before
    assert json.loads(before)["format"] == manuscript.WORKFILE_FORMAT_V3
    assert statuses(payload) == ["done"] * len(pairs_of(payload))
    assert sr.parts_of(payload).segments[0]["result"] is not None
