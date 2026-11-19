"""The Markdown report is the public Web's report, rendered by this client.

The cross-product test renders one synthetic input with this module and with
the Web's own renderer (``lib/markdown-report.ts`` at the pinned commit) and
compares them line by line, together with choice reconciliation and report
comparison. No Qt; it runs in the pure CI job, which has Node.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from wuwaterm_client import markdown_report as mdr

SOURCE = "甲术在此。`乙术`也在。\n第二行甲术。"
TARGET = "Alpha is here. Bett too.\nLine two."


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span(text: str, quote: str, occurrence: int = 0) -> dict:
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(quote, start + 1)
    return {"start": start, "end": start + len(quote), "text": quote}


def candidate(zh: str, en: str, category: str = "resonator", sources: bool = True) -> dict:
    return {"candidate_id": digest(zh + en), "zh": zh, "en": en, "category": category,
            "sources": [{"source_file": "TextMap/example.json", "source_id": "id`1"}] if sources else []}


def finding(quote, occurrence, verdict, target_quote=None, candidates=(), truncated=False) -> dict:
    source_span = span(SOURCE, quote, occurrence)
    return {"id": mdr.mention_id(source_span), "rule_id": "review.term_pair", "verdict": verdict,
            "source_span": source_span, "target_span": span(TARGET, target_quote) if target_quote else None,
            "candidates": list(candidates), "candidates_truncated": truncated}


DICTIONARY = {"schema_version": "1", "source_commit": "c" * 40, "term_count": 3, "revision": "b" * 64}


def report(findings: list, request_id: str) -> dict:
    return {"request_id": request_id, "source_revision": digest(SOURCE), "target_revision": digest(TARGET),
            "rule_version": "review-v2", "dictionary": DICTIONARY,
            "coverage": {"evaluated": 2, "not_evaluated": 1, "rules": ["review.term_pair", "review.sentence_meaning"]},
            "findings": findings, "truncated": False, "matcher_revision": "a" * 64}


def sample() -> dict:
    alpha = finding("甲术", 0, "confirmed_conflict", "Alpha", [candidate("甲术", "Alpha")])
    beta = finding("乙术", 0, "needs_review", "Bett",
                   [candidate("乙术", "Beta", "item", sources=False), candidate("乙术", "Bett")], truncated=True)
    later = finding("甲术", 1, "verified_constraint", None, [candidate("甲术", "Alpha")])
    snapshot = {"source": SOURCE, "target": TARGET, "direction": "en", "alignments": None, "trusted": True}
    previous = {**snapshot, "report": report([alpha, beta], "req-1"), "resolutions": []}
    latest = {**snapshot, "report": report([{**alpha, "verdict": "verified_constraint"}, beta, later], "req-2"),
              "resolutions": [{"mention_id": alpha["id"], "choice": "official_pair",
                               "candidate_id": alpha["candidates"][0]["candidate_id"]}]}
    basis = {"rule_version": "review-v2", "dictionary": DICTIONARY, "matcher_revision": "a" * 64}
    choices = [
        {"source": SOURCE, "direction": "en", "source_span": alpha["source_span"], "scope": "positional",
         "choice": "official_pair", "basis": basis,
         "candidate": {k: alpha["candidates"][0][k] for k in ("candidate_id", "zh", "en", "category")}},
        {"source": SOURCE, "direction": "en", "source_span": beta["source_span"], "scope": "positional",
         "choice": "not_a_term", "candidate": None, "basis": basis},
    ]
    draft = {"source": SOURCE, "target": TARGET, "direction": "en", "alignments": None}
    return {
        "draft": draft, "latest": latest, "previous": previous, "currency": "current", "choices": choices,
        "reconciled": mdr.reconcile_choices(choices, draft, latest, imported=False, submits=True),
        "comparison": mdr.compare_reports(previous, latest),
        "semantic": {"items": [{"issue_id": "1" * 32, "category": "omission", "confidence": "medium",
                                "segment_index": 0, "source_excerpt": "甲术在此", "target_excerpt": None,
                                "rationale": "理由`x`", "suggestion": "Alpha is", "suggestion_withheld": None,
                                "status": "ignored"}],
                     "resolved": 1, "segments": {"total": 2, "reviewed": 1}},
        "segment_terms": [
            {"index": 0, "state": "current", "stale": False,
             "findings": [{"verdict": "needs_review", "source_span": beta["source_span"], "target_span": None,
                           "candidates": [{"zh": "乙术", "en": "Beta"}]}]},
            {"index": 1, "state": "saved", "stale": True, "findings": []},
        ],
    }


def truncated_long_sample() -> dict:
    """A long-manuscript export: a truncated segment, a cut candidate list and
    blank lines that a fence must keep exactly."""
    source = "first\n\n\n\nsecond 甲术"
    hit = span(source, "甲术")
    return {
        "draft": {"source": source, "target": "one\n\n\ntwo", "direction": "en", "alignments": None},
        "latest": None, "previous": None, "currency": "stale", "choices": [], "reconciled": [], "comparison": None,
        "semantic": None,
        "segment_terms": [
            {"index": 0, "state": "current", "stale": False, "truncated": True,
             "findings": [{"verdict": "needs_review", "source_span": hit, "target_span": None,
                           "candidates": [{"zh": "甲术", "en": "Alpha"}], "candidates_truncated": True}]},
            {"index": 1, "state": "stale", "stale": True, "truncated": True, "findings": []},
        ],
    }


def test_the_report_matches_the_web_renderer_line_for_line(contract_source) -> None:
    web_root = contract_source("web")
    if not shutil.which("node"):
        if os.environ.get("WUWATERM_CONTRACTS_REQUIRED") == "1":
            pytest.fail("Node is required for the Web report contract")
        pytest.skip("Node is required for the Web report contract")
    data = sample()
    long = truncated_long_sample()
    heads = [mdr.render(data), "\ufeff# WuwaTerm terminology review report\r\nx", "# WuwaTerm 术语审校报告 \n",
             "正文\n# WuwaTerm 术语审校报告", ""]
    program = """
      import { readFileSync } from 'node:fs';
      import { isMarkdownReportExport, renderMarkdownReport } from './lib/markdown-report.ts';
      import { compareReports, reconcileChoices } from './lib/manuscript.js';
      const { data, long, heads } = JSON.parse(readFileSync(0, 'utf8'));
      const strip = c => Object.fromEntries(Object.entries(c).map(([k, v]) => [k, v.map(x => ({ mention: x.mention, code: x.code }))]));
      process.stdout.write(JSON.stringify({
        markdown: renderMarkdownReport({ ...data, segmentTerms: data.segment_terms }, 'zh'),
        long: renderMarkdownReport({ ...long, segmentTerms: long.segment_terms }, 'zh'),
        reconciled: reconcileChoices(data.choices, data.draft, data.latest).map(r => [r.status, r.code]),
        comparison: strip(compareReports(data.previous, data.latest)),
        reports: heads.map(isMarkdownReportExport),
      }));
    """
    result = subprocess.run(["node", "--no-warnings", "--experimental-strip-types", "--input-type=module", "-e", program],
                            cwd=web_root, input=json.dumps({"data": data, "long": long, "heads": heads}, ensure_ascii=False), text=True, encoding="utf-8",
                            capture_output=True, check=True, timeout=60)
    web = json.loads(result.stdout)
    for rendered, theirs in ((mdr.render(data), web["markdown"]), (mdr.render(long), web["long"])):
        ours, theirs = rendered.split("\n"), theirs.split("\n")
        assert len(ours) == len(theirs)
        # The one wording change: a desktop window, not a web page, makes the snapshot.
        different = [i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b]
        assert different == [2] and ours[2] == mdr.MD["snapshotLine"]
    text = mdr.render(long)
    assert "```\nfirst\n\n\n\nsecond 甲术\n```" in text and "one\n\n\ntwo" in text, "fenced text is exact"
    assert text.count(mdr.MD["truncatedLine"]) == 2 and " · " + mdr.MD["candidatesTruncatedLine"] in text
    assert web["reconciled"] == [[r["status"], r["code"]] for r in data["reconciled"]]
    assert web["comparison"] == data["comparison"]
    assert web["reports"] == [mdr.is_report_export(head) for head in heads] == [True, True, False, False, False]


def test_a_desktop_short_manuscript_never_reports_a_file_choice_as_applicable() -> None:
    data = sample()
    reconciled = mdr.reconcile_choices(data["choices"], data["draft"], data["latest"])
    # The same inputs the Web would submit: Windows keeps them but never sends them.
    assert [(r["status"], r["code"]) for r in reconciled] == [
        ("pending", "desktop_not_submitted"), ("pending", "imported_not_a_term")]


def test_segment_choices_follow_their_segment_and_user_text_stays_literal() -> None:
    data = sample()
    choice = {"source_span": {"start": 6, "end": 8, "text": "乙术"}, "choice": "not_a_term", "candidate": None,
              "offered": [], "confirmed": False}
    data["segment_terms"].append({"index": 2, "state": "pending", "stale": True, "findings": [],
                                  "choices": [choice]})
    data["segment_choice_history"] = 2
    text = mdr.render(data)
    assert ("### 第 3 段 · 尚未核对\n\n此段还没有核对结果。\n\n- 术语选择 · `乙术` · 原文位置 7–8 · 这里不是术语 · "
            "等待此段核对后生效") in text
    assert "选择历史：2 条" in text
    # Backticks in user text are fenced with a longer run, never closed early.
    assert "``` 理由`x` ```" not in text and "`` 理由`x` ``" in text


def test_choices_of_a_pending_segment_wait_and_only_a_checked_segment_asks_for_reconfirmation() -> None:
    """A pending record has no result: its choices, confirmed or not, wait for the
    segment's check, which realigns them (SPEC "Runtime" step 7), as the workbench
    shows them. Only an unconfirmed choice of a record with a result needs
    reconfirmation and is not applied."""
    data = sample()

    def choice(start: int, end: int, confirmed: bool) -> dict:
        return {"source_span": {"start": start, "end": end, "text": SOURCE[start:end]}, "choice": "not_a_term",
                "candidate": None, "offered": [], "confirmed": confirmed}

    choices = [choice(0, 2, True), choice(6, 8, False)]
    data["segment_terms"] = [{"index": 0, "state": "pending", "stale": True, "findings": [], "choices": choices},
                             {"index": 1, "state": "stale", "stale": True, "findings": [], "choices": choices}]
    lines = [line for line in mdr.render(data).split("\n") if line.startswith("- 术语选择 · ")]
    assert [line.rsplit(" · ", 1)[1] for line in lines] == [
        "等待此段核对后生效", "等待此段核对后生效", "已确认，随核对提交", "需重新确认，不会自动应用"]


def test_an_exported_report_is_recognized_by_its_first_line_as_on_the_web() -> None:
    text = mdr.render(sample())
    assert mdr.is_report_export(text) and mdr.is_report_export("\ufeff" + text.replace("\n", "\r\n"))
    assert mdr.is_report_export("# WuwaTerm terminology review report\nbody")
    assert not mdr.is_report_export("# WuwaTerm 术语审校报告（副本）\n") and not mdr.is_report_export("正文\n" + text)


@pytest.mark.parametrize("reason,expected", list(mdr.NOT_EVALUATED_REASONS.items()))
def test_markdown_report_renders_all_five_not_evaluated_reasons(reason: str, expected: str) -> None:
    data = sample()
    item = finding("乙术", 0, "not_evaluated", None, [])
    item["reason"] = reason
    data["latest"]["report"]["findings"] = [item]
    data["segment_terms"] = [{
        "index": 0, "state": "current", "stale": False,
        "findings": [{
            "verdict": "not_evaluated", "source_span": item["source_span"], "target_span": None,
            "candidates": [], "candidates_truncated": False, "reason": reason,
        }],
    }]
    rendered = mdr.render(data)
    assert rendered.count(f"未评估原因：{expected}") == 2

