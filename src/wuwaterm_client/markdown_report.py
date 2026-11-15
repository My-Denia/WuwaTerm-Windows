"""The Markdown term review report, in the public Web's format.

A port of the Web's ``lib/markdown-report.ts`` (``renderMarkdownReport``) with
its Chinese text, plus the two helpers it is fed by there: choice
reconciliation (``reconcileChoice`` in ``lib/manuscript.js``) and the report
comparison (``compareReports``). The field meanings and the currency rule
are the Web's: a report is ``current`` only when it was checked in this
session for exactly the current text; ``stale`` when it was checked in this
session and the text changed since; ``imported`` when it came from a file.

A report is a read-only snapshot. It is not a manuscript and cannot be
opened to resume work: the manuscript file is what "保存稿件" writes.

One addition to the Web's format: long-manuscript segments also list their
term choices (and the choice history count), so the report shows the
owner's per-segment decisions next to the findings they answer.

Every user-derived string is written inside a code span or a code fence
that its content cannot close, so it never becomes report structure.

Pure Python: no Qt, no HTTP.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .manuscript import recover_span

CONTEXT_RADIUS = 48
_HEX = re.compile(r"^[a-f0-9]{64}$")

MD = {
    "title": "WuwaTerm 术语审校报告",
    "snapshotLine": "本文件是只读本地快照，由工作台当前状态生成，不发起任何请求；不构成任何认证。",
    "currencyHeading": "报告当前性",
    "currencyCurrent": "当前报告：与当前文本、方向和已提交的局部选择一致。",
    "currencyStale": "需要重新核对：报告与当前文本或选择已不一致（编辑后尚未重新核对）。",
    "currencyImported": "导入的历史报告：来自稿件文件，未经本次会话验证，不是当前认证。",
    "directionLabel": "翻译方向",
    "directionToEn": "中文 → 英文（译文为英文）",
    "directionToZh": "英文 → 中文（译文为中文）",
    "reportDirectionLabel": "报告翻译方向（来自报告本身）",
    "directionChangedNote": "注意：当前稿件方向已改为 {direction}，与该报告不一致；报告内容未经重新核对。",
    "basisHeading": "报告依据（来自报告本身，非当前应用版本）",
    "ruleLabel": "规则版本",
    "revisionLabel": "词典 revision",
    "commitLabel": "词典来源提交",
    "schemaLabel": "数据库结构版本",
    "termCountLabel": "词条数",
    "requestIdLabel": "请求编号",
    "sourceRevisionLabel": "原文修订（SHA-256）",
    "targetRevisionLabel": "译文修订（SHA-256）",
    "notProvided": "未提供",
    "coverageHeading": "覆盖范围",
    "evaluatedLabel": "已评估",
    "notEvaluatedLabel": "未评估",
    "rulesLabel": "适用规则",
    "zeroCoverageLine": "零覆盖：没有任何内容被评估。零发现不等于翻译通过。",
    "truncatedLine": "报告已截断：以下不是一次完整核对。",
    "summaryHeading": "结论摘要",
    "sourceHeading": "原文",
    "targetHeading": "译文",
    "findingsHeading": "术语发现",
    "noReportLine": "当前没有审校报告；以下仅包含稿件文本与本地选择。",
    "zeroFindingsLine": "本次没有术语发现；零发现不等于翻译通过，整句语义未被评估。",
    "spanLabel": "原文位置 {start}–{end}",
    "targetSpanLabel": "译文位置 {start}–{end}",
    "noTargetSpan": "无对应译文位置。",
    "excerptLabel": "上下文",
    "excerptMissing": "上下文无法定位（文本已变化）。",
    "ruleLabelShort": "规则",
    "candidatesHeading": "官方候选词对",
    "noCandidates": "无候选词对。",
    "candidatesTruncatedLine": "候选词对已截断：可能仍有未显示的官方候选。",
    "sourceRecordsLabel": "来源记录",
    "decisionsHeading": "随本报告提交的局部决定",
    "noDecisions": "本报告未随附任何局部决定。",
    "decisionOfficial": "采用官方词对",
    "decisionNotTerm": "标记为不是术语",
    "choicesHeading": "本地保留的局部选择",
    "noChoices": "没有保留的局部选择。",
    "choiceApplicable": "可沿用，需纳入下次核对",
    "choicePending": "待确认",
    "basisLine": "依据：规则 {rule} · 词典 revision {rev} · 来源提交 {commit}",
    "comparisonHeading": "与上一份报告比较",
    "comparisonSingle": "只有一份报告，没有可比较的上一份。",
    "comparisonNone": "尚未取得报告，无法比较。",
    "comparisonNote": "发现消失不代表问题解决；通过术语约束也不代表整句语义正确。",
    "notCertifiedLine": "本报告不包含整句质量评分，也不构成对整句语义的认证。",
    "semanticHeading": "语义疑点（模型，可能有误、请自行判断）",
    "segmentTermsHeading": "逐段术语核对（{n} 段）",
    "segmentTermsNote": ("长稿按段核对的术语结果；位置为全文中的字符位置。这份报告只是快照，不能导入续作；"
                         "逐段结果与术语选择随「保存稿件」写入稿件文件。"),
    "segmentTermsLabel": "第 {n} 段",
    "segmentTermsStale": "需重新核对：文字、对应方式、选择或服务依据已变化",
    "segmentTermsSaved": "来自已保存的稿件，尚未与当前服务确认",
    "segmentTermsNone": "此段没有术语发现。",
    # Windows addition: the segment's term choices.
    "segmentTermsUnchecked": "尚未核对",
    "segmentTermsNoResult": "此段还没有核对结果。",
    "segmentChoiceLabel": "术语选择",
    "segmentChoiceConfirmed": "已确认，随核对提交",
    "segmentChoiceUnconfirmed": "需重新确认，不会自动应用",
    "segmentChoiceHistory": "选择历史：{n} 条（原位置已不在任何段落中，不会应用；保留在稿件中）。",
    "semanticCoverage": "已审校段落：{reviewed} / {total}",
    "semanticResolved": "已处理 {n} 条；这不代表整句语义已认证。",
    "semanticNone": "没有语义疑点记录。",
    "semanticSegment": "第 {n} 段",
    "semanticSourceLabel": "原文",
    "semanticTargetLabel": "译文",
    "semanticRationaleLabel": "理由",
    "semanticSuggestionLabel": "建议",
    "semanticWithheld": "范围较长，请人工调整",
    "semanticNoSuggestion": "无具体建议",
    "semanticStatusLabel": "处理状态",
    "semanticConfidenceLabel": "置信度",
}
SEMANTIC_CATEGORIES = {
    "omission": "漏译", "unsupported_addition": "无依据添加", "meaning_shift": "含义偏移", "negation_change": "否定改变",
    "quantity_change": "数量改变", "reference_inconsistency": "指代不一致", "term_sense": "术语义项",
}
SEMANTIC_CONFIDENCES = {"high": "高", "medium": "中", "low": "低"}
SEMANTIC_STATUSES = {
    "pending": "待处理", "accepted": "已采纳", "adjusted": "已调整后采纳", "ignored": "已忽略",
    "needs_reconfirmation": "需重新确认", "previously_applied": "此前已应用", "stale": "文本已变，已失效",
}
VERDICTS = {"verified_constraint": "术语约束已核", "confirmed_conflict": "与所选词对冲突",
            "needs_review": "需要核对", "not_evaluated": "未评估"}
CATEGORIES = {"core_term": "核心术语", "resonator": "共鸣者", "weapon": "武器", "echo": "声骸", "skill": "技能",
              "sonata_effect": "合鸣效果", "location": "地区", "item": "物品", "speaker": "角色"}
NOT_TERM = "这里不是术语"
CATEGORY = "类别"
CHOICE_REASONS = {
    "direction_changed": "翻译方向已变化，请重新选择",
    "source_block_changed": "原文段落已改变、删除或重复，不能迁移位置",
    "scope_changed": "分段对应已变化，请重新确认",
    "awaiting_fresh_basis": "等待当前原文的新依据",
    "basis_changed": "词典或规则依据已变化，请重新确认",
    "finding_missing_or_truncated": "本次发现缺失或报告截断，请重新确认",
    "imported_not_a_term": "文件中的“不是术语”是历史决定，请重新确认",
    "user_not_a_term": "用户决定；此处不评估术语",
    "candidate_gone": "原词对或其来源已不在当前候选中",
    "candidate_partial_visible": "展示来源不完整；具体词对身份与当前依据一致，提交时由服务端复核",
    "position_basis_match": "位置与当前官方候选依据一致",
    "duplicate_position": "多份历史选择汇聚到同一位置，请移除冲突记录后重新选择",
    # Windows: short manuscripts keep a file's choices but never submit them.
    "desktop_not_submitted": "桌面端的短稿核对不提交文件中的选择；它保留在稿件中，可在 Web 中确认",
}
COMPARISON_LABELS = (("new", "新发现"), ("resolved", "已解决的术语约束"),
                     ("pending", "仍待确认"), ("incomparable", "无法比较"))
COMPARISON_MENTION = "{text}（原文 {n}）"
COMPARISON_MENTION_HISTORICAL = "{text}（历史原文 {n}）"
COMPARISON_REASONS = {
    "imported_history_unverified": "导入的历史报告未经本次验证",
    "decision_changed_or_unknown": "前后选择不同或选择记录未知，不能视为同一约束已解决",
    "scope_position_incomparable": "位置、依据、覆盖或对应范围不可比；消失不代表解决",
    "resolved_same_constraint": "同一可比位置的新检查确认术语约束已满足，未评估句意",
    "still_needs_review": "本次仍需人工核对",
    "new_finding": "本次发现；没有可比的旧位置",
}
SUMMARY = {
    "prefix": "本次检查：词典术语与基础结构；", "verified": "约束已核 {n}", "conflicts": "冲突 {n}",
    "needs": "需核对 {n}", "notEvaluated": "未评估 {n}", "truncated": "结果已截断，不能当作完整核对",
    "zeroCoverage": "零覆盖", "noSentenceMeaning": "未评估完整句意", "suffix": "。",
    "headlineFail": "未通过", "headlineNeedsCheck": "需核对", "headlinePartial": "部分已核",
}
COLON, SEMICOLON = "：", "；"


def fill(template: str, **values: Any) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


# -- literals ------------------------------------------------------------------
def _backtick_run(text: str) -> int:
    return max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)


def inline(text: str) -> str:
    """Single-line user text inside a code span no content can break out of."""
    flat = re.sub(r"\r\n|\r|\n", "⏎", text)
    if flat == "":
        return "`∅`"
    run = _backtick_run(flat)
    all_space = flat.strip() == ""
    edge_space = not all_space and (flat.startswith(" ") or flat.endswith(" "))
    if run == 0 and not edge_space:
        return "`" + flat + "`"
    delimiter = "`" * (run + 1)
    return delimiter + " " + flat + " " + delimiter


def block(text: str) -> list[str]:
    fence = "`" * max(3, _backtick_run(text) + 1)
    return [fence, text, fence]


def _bullets(lines: Sequence[str]) -> list[str]:
    return ["- " + line for line in lines if line]


def _counted(label: str, count: int) -> str:
    return f"{label}（{count}）"


# -- report helpers (Web lib/review-report.js, review-navigation.js) ---------------
def summarize(report: Mapping | None) -> dict | None:
    if not report or not isinstance(report.get("findings"), list) or not report.get("coverage"):
        return None
    findings = report["findings"]
    truncated = report.get("truncated") is True or any(f.get("candidates_truncated") is True for f in findings)
    not_evaluated = int(report["coverage"].get("not_evaluated") or 0)
    verified = sum(1 for f in findings if f["verdict"] == "verified_constraint")
    conflicts = sum(1 for f in findings if f["verdict"] == "confirmed_conflict")
    needs = sum(1 for f in findings if f["verdict"] == "needs_review")
    headline = ("fail" if conflicts > 0 else "needs_check"
                if needs > 0 or not_evaluated > 0 or truncated or not findings else "partial_verified")
    return {"verified": verified, "conflicts": conflicts, "needs": needs, "notEvaluated": not_evaluated,
            "truncated": truncated, "zeroCoverage": not findings or report["coverage"]["evaluated"] == 0,
            "headline": headline}


def _summary_sentence(parts: Mapping) -> str:
    segments = [fill(SUMMARY["verified"], n=parts["verified"]), fill(SUMMARY["conflicts"], n=parts["conflicts"]),
                fill(SUMMARY["needs"], n=parts["needs"])]
    if parts["notEvaluated"] > 0:
        segments.append(fill(SUMMARY["notEvaluated"], n=parts["notEvaluated"]))
    if parts["truncated"]:
        segments.append(SUMMARY["truncated"])
    if parts["zeroCoverage"]:
        segments.append(SUMMARY["zeroCoverage"])
    segments.append(SUMMARY["noSentenceMeaning"])
    return SUMMARY["prefix"] + SEMICOLON.join(segments) + SUMMARY["suffix"]


def _summary_headline(parts: Mapping) -> str:
    return {"fail": SUMMARY["headlineFail"], "needs_check": SUMMARY["headlineNeedsCheck"]}.get(
        parts["headline"], SUMMARY["headlinePartial"])


def compact_context(text: str, span: Mapping | None, radius: int = CONTEXT_RADIUS) -> dict | None:
    if not span or not isinstance(text, str):
        return None
    start, end = span.get("start"), span.get("end")
    if (not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text)
            or text[start:end] != span.get("text")):
        return None
    before_start, after_end = max(0, start - radius), min(len(text), end + radius)
    return {"before": text[before_start:start], "hit": text[start:end], "after": text[end:after_end],
            "clippedBefore": before_start > 0, "clippedAfter": after_end < len(text)}


# -- choices and comparison (Web lib/manuscript.js) -------------------------------------
def mention_id(span: Mapping) -> str:
    return f'{span["start"]}:{span["end"]}:{span["text"]}'


def basis_of(report: Mapping) -> dict:
    basis = {"rule_version": report["rule_version"], "dictionary": dict(report["dictionary"])}
    if "matcher_revision" in report:
        basis["matcher_revision"] = report["matcher_revision"]
    return basis


def _matcher_id(basis: Mapping | None):
    if not basis or "matcher_revision" not in basis:
        return None
    value = basis["matcher_revision"]
    return value if isinstance(value, str) and _HEX.match(value) else False


def same_basis(a: Mapping | None, b: Mapping | None) -> bool:
    def text(value) -> bool:
        return isinstance(value, str) and len(value) <= 4096
    if not a or not b or a.get("rule_version") != "review-v2" or b.get("rule_version") != "review-v2":
        return False
    da, db = a.get("dictionary") or {}, b.get("dictionary") or {}
    left, right = _matcher_id(a), _matcher_id(b)
    return (text(da.get("schema_version")) and text(db.get("schema_version"))
            and text(da.get("source_commit")) and text(db.get("source_commit"))
            and isinstance(da.get("revision"), str) and bool(_HEX.match(da["revision"]))
            and da["revision"] == db.get("revision") and da["source_commit"] == db["source_commit"]
            and da["schema_version"] == db["schema_version"] and da.get("term_count") == db.get("term_count")
            and left is not False and right is not False and left == right)


def _js(value: Any) -> str:
    """``JSON.stringify`` of plain JSON data."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def scope_of(alignments: list | None, span: Mapping) -> str:
    if alignments is None:
        return "positional"
    region = next((a for a in alignments if a["source"]["start"] <= span["start"] and a["source"]["end"] >= span["end"]),
                  None)
    return _js(region) if region is not None else "unmapped"


def reconcile_choices(choices: Sequence[Mapping], draft: Mapping, fresh: Mapping | None, *,
                      imported: bool = True, submits: bool = False) -> list[dict]:
    """Web ``reconcileChoices``. ``fresh`` is the latest snapshot with its
    ``trusted`` flag. Windows short manuscripts never submit a file's choices,
    so unless ``submits``, an applicable choice is reported as pending."""
    def one(choice: Mapping) -> dict:
        span = recover_span(choice["source"], choice["source_span"], draft["source"])

        def pending(code: str) -> dict:
            return {"status": "pending", "code": code, "reason": CHOICE_REASONS[code], "span": span}
        if choice["direction"] != draft["direction"]:
            return pending("direction_changed")
        if span is None:
            return pending("source_block_changed")
        if choice["scope"] != scope_of(draft["alignments"], span):
            return pending("scope_changed")
        if (not fresh or fresh["source"] != draft["source"] or fresh["direction"] != draft["direction"]
                or not fresh.get("trusted")):
            return pending("awaiting_fresh_basis")
        if not same_basis(choice["basis"], basis_of(fresh["report"])):
            return pending("basis_changed")
        finding = next((f for f in fresh["report"]["findings"] if f["id"] == mention_id(span)), None)
        if finding is None or fresh["report"]["truncated"]:
            return pending("finding_missing_or_truncated")
        if choice["choice"] == "not_a_term":
            if imported:
                return pending("imported_not_a_term")
            code = "user_not_a_term"
        else:
            wanted = choice["candidate"]
            if not any(c["candidate_id"] == wanted["candidate_id"] and c["zh"] == wanted["zh"]
                       and c["en"] == wanted["en"] and c["category"] == wanted["category"]
                       for c in finding["candidates"]):
                return pending("candidate_gone")
            code = "candidate_partial_visible" if finding["candidates_truncated"] else "position_basis_match"
        if not submits:
            return pending("desktop_not_submitted")
        return {"status": "applicable", "code": code, "reason": CHOICE_REASONS[code], "span": span}
    results = [one(choice) for choice in choices]
    counts: dict[str, int] = {}
    for result in results:
        if result["span"] is not None:
            key = mention_id(result["span"])
            counts[key] = counts.get(key, 0) + 1
    return [{**r, "status": "pending", "code": "duplicate_position", "reason": CHOICE_REASONS["duplicate_position"]}
            if r["span"] is not None and counts[mention_id(r["span"])] > 1 else r for r in results]


def _positional_source_region(source: str, span: Mapping) -> str | None:
    ranges, start = [], 0
    for i, char in enumerate(source):
        decimal = (char == "." and i > 0 and source[i - 1].isascii() and source[i - 1].isdigit()
                   and i + 1 < len(source) and source[i + 1].isascii() and source[i + 1].isdigit())
        if not decimal and char in "。．.！？!?\n":
            region = source[start:i + 1]
            if re.search(r"[^。．.！？!?\n\s]", region):
                ranges.append((start, i + 1, region))
            start = i + 1
    if start < len(source):
        ranges.append((start, len(source), source[start:]))
    return next((text for a, b, text in ranges if a <= span["start"] and b >= span["end"]), None)


def _comparable_scope(previous: Mapping, before_span: Mapping, current: Mapping, span: Mapping) -> bool:
    if previous["alignments"] is None and current["alignments"] is None:
        before = _positional_source_region(previous["source"], before_span)
        return before is not None and before == _positional_source_region(current["source"], span)
    return scope_of(previous["alignments"], before_span) == scope_of(current["alignments"], span)


def _submitted(snapshot: Mapping, finding: Mapping) -> tuple[str | None, Mapping | None]:
    resolutions = snapshot.get("resolutions")
    if not isinstance(resolutions, list):
        return None, None
    item = next((r for r in resolutions if r["mention_id"] == finding["id"]), None)
    if item is None:
        return "automatic", None
    candidate = (next((c for c in finding["candidates"] if c["candidate_id"] == item.get("candidate_id")), None)
                 if item["choice"] == "official_pair" else None)
    return _js([item["choice"], item.get("candidate_id")]), candidate


def compare_reports(previous: Mapping | None, current: Mapping) -> dict:
    """Web ``compareReports``: snapshots carry ``trusted`` (checked in this session)."""
    result: dict[str, list] = {"new": [], "resolved": [], "pending": [], "incomparable": []}
    if not previous:
        return result
    old_findings, new_findings = previous["report"]["findings"], current["report"]["findings"]
    compatible = (previous.get("trusted") and current.get("trusted") and previous["direction"] == current["direction"]
                  and same_basis(basis_of(previous["report"]), basis_of(current["report"]))
                  and not previous["report"]["truncated"] and not current["report"]["truncated"])
    seen = set()
    for before in old_findings:
        span = recover_span(previous["source"], before["source_span"], current["source"])
        after = (next((f for f in new_findings if f["id"] == mention_id(span) and f["rule_id"] == before["rule_id"]), None)
                 if span else None)
        scopes = bool(span) and _comparable_scope(previous, before["source_span"], current, span)
        old_descriptor, old_candidate = _submitted(previous, before)
        new_descriptor, new_candidate = _submitted(current, after) if after else (None, None)
        decisions_match = old_descriptor is not None and old_descriptor == new_descriptor
        same_candidate = bool(decisions_match and old_candidate and new_candidate and all(
            old_candidate[k] == new_candidate[k] for k in ("candidate_id", "zh", "en", "category")))
        if after:
            seen.add(after["id"])
        mention = {"text": before["source_span"]["text"], "start": (span or before["source_span"])["start"],
                   "historical": not span}
        if (not compatible or not after or not scopes or not decisions_match
                or ((before["candidates_truncated"] or after["candidates_truncated"]) and not same_candidate)
                or before["verdict"] == "not_evaluated" or after["verdict"] == "not_evaluated"):
            code = ("imported_history_unverified" if not previous.get("trusted") else
                    "decision_changed_or_unknown" if not decisions_match else "scope_position_incomparable")
            result["incomparable"].append({"mention": mention, "code": code})
        elif before["verdict"] in ("needs_review", "confirmed_conflict") and after["verdict"] == "verified_constraint":
            result["resolved"].append({"mention": mention, "code": "resolved_same_constraint"})
        elif after["verdict"] in ("needs_review", "confirmed_conflict"):
            result["pending"].append({"mention": mention, "code": "still_needs_review"})
    for finding in new_findings:
        if finding["id"] not in seen:
            result["new"].append({"mention": {"text": finding["source_span"]["text"],
                                              "start": finding["source_span"]["start"], "historical": False},
                                  "code": "new_finding"})
    return result


# -- the report --------------------------------------------------------------------
def _direction(direction: str) -> str:
    return MD["directionToEn"] if direction == "en" else MD["directionToZh"]


def _span_label(span: Mapping, key: str = "spanLabel") -> str:
    return fill(MD[key], start=span["start"] + 1, end=span["end"])


def _pair(candidate: Mapping | None) -> str:
    return inline(candidate["zh"]) + " / " + inline(candidate["en"]) if candidate else NOT_TERM


def render(data: Mapping[str, Any]) -> str:
    """The Markdown report. ``data`` holds ``draft``, ``latest``, ``previous``,
    ``currency``, ``choices``, ``reconciled``, ``comparison``, ``semantic``
    and ``segment_terms``, with the Web's meanings (see the module docstring)."""
    out: list[str] = []

    def heading(depth: int, text: str) -> None:
        out.extend(["#" * depth + " " + text, ""])

    def paragraph(text: str) -> None:
        out.extend([text, ""])

    out.extend(["# " + MD["title"], ""])
    paragraph(MD["snapshotLine"])
    latest, draft = data.get("latest"), data["draft"]
    report = latest["report"] if latest else None
    parts = summarize(report)

    heading(2, MD["currencyHeading"])
    currency = data["currency"]
    paragraph({"current": MD["currencyCurrent"], "stale": MD["currencyStale"],
               "imported": MD["currencyImported"]}.get(currency, MD["noReportLine"]))
    if latest:
        paragraph(MD["reportDirectionLabel"] + COLON + _direction(latest["direction"]))
        if draft["direction"] != latest["direction"]:
            paragraph(fill(MD["directionChangedNote"], direction=_direction(draft["direction"])))
    else:
        paragraph(MD["directionLabel"] + COLON + _direction(draft["direction"]))

    heading(2, MD["sourceHeading"])
    out.extend([*block(draft["source"]), ""])
    heading(2, MD["targetHeading"])
    out.extend([*block(draft["target"]), ""])

    if report and parts:
        heading(2, MD["summaryHeading"])
        paragraph(_summary_headline(parts) + COLON + _summary_sentence(parts))
        heading(2, MD["basisHeading"])
        dictionary = report["dictionary"]

        def given(value) -> str:
            return inline(value if value is not None else MD["notProvided"])
        out.extend([*_bullets([
            MD["ruleLabel"] + COLON + inline(report["rule_version"]),
            MD["revisionLabel"] + COLON + given(dictionary.get("revision")),
            MD["commitLabel"] + COLON + given(dictionary.get("source_commit")),
            MD["schemaLabel"] + COLON + given(dictionary.get("schema_version")),
            MD["termCountLabel"] + COLON + str(dictionary["term_count"]),
            MD["requestIdLabel"] + COLON + given(report.get("request_id")),
            MD["sourceRevisionLabel"] + COLON + inline(report["source_revision"]),
            MD["targetRevisionLabel"] + COLON + inline(report["target_revision"]),
        ]), ""])
        heading(2, MD["coverageHeading"])
        rules = report["coverage"]["rules"]
        out.extend([*_bullets([
            MD["evaluatedLabel"] + COLON + str(report["coverage"]["evaluated"]),
            MD["notEvaluatedLabel"] + COLON + str(report["coverage"]["not_evaluated"]),
            MD["rulesLabel"] + COLON + (" · ".join(inline(r) for r in rules) if rules else MD["notProvided"]),
        ]), ""])
        if parts["zeroCoverage"]:
            paragraph(MD["zeroCoverageLine"])
        if report["truncated"]:
            paragraph(MD["truncatedLine"])

        heading(2, _counted(MD["findingsHeading"], len(report["findings"])))
        if not report["findings"]:
            paragraph(MD["zeroFindingsLine"])
        for number, finding in enumerate(report["findings"], 1):
            source_span, target_span = finding["source_span"], finding["target_span"]
            out.extend(["### " + str(number) + ". " + VERDICTS.get(finding["verdict"], finding["verdict"]) + " · "
                        + inline(source_span["text"]), ""])
            out.extend([*_bullets([
                _span_label(source_span) + " · " + MD["ruleLabelShort"] + COLON + inline(finding["rule_id"]),
                (_span_label(target_span, "targetSpanLabel") + COLON + inline(target_span["text"])
                 if target_span else MD["noTargetSpan"]),
            ]), ""])
            context = compact_context(latest["source"], source_span)
            if context:
                excerpt = (("…" if context["clippedBefore"] else "") + context["before"] + context["hit"]
                           + context["after"] + ("…" if context["clippedAfter"] else ""))
                out.extend([MD["excerptLabel"] + COLON, *block(excerpt), ""])
            else:
                paragraph(MD["excerptLabel"] + COLON + MD["excerptMissing"])
            paragraph(_counted(MD["candidatesHeading"], len(finding["candidates"])))
            if not finding["candidates"]:
                paragraph(MD["noCandidates"])
            for candidate in finding["candidates"]:
                out.append("- " + inline(candidate["zh"]) + " / " + inline(candidate["en"]) + " · " + CATEGORY + COLON
                           + inline(CATEGORIES.get(candidate["category"], candidate["category"])))
                sources = candidate.get("sources") or []
                out.append("  - " + MD["sourceRecordsLabel"] + COLON + (SEMICOLON.join(
                    inline(item["source_file"]) + " · " + inline(item["source_id"]) for item in sources)
                    if sources else MD["notProvided"]))
            if finding["candidates_truncated"]:
                paragraph(MD["candidatesTruncatedLine"])
            out.append("")

        heading(2, MD["decisionsHeading"])
        resolutions = latest.get("resolutions") or []
        if not resolutions:
            paragraph(MD["noDecisions"])
        else:
            lines = []
            for resolution in resolutions:
                index = next((i for i, f in enumerate(report["findings"]) if f["id"] == resolution["mention_id"]), -1)
                where = f" #{index + 1}" if index >= 0 else ""
                official = resolution["choice"] == "official_pair"
                lines.append((MD["decisionOfficial"] if official else MD["decisionNotTerm"]) + where
                             + (" · " + inline(resolution["candidate_id"]) if official and resolution.get("candidate_id")
                                else ""))
            out.extend([*_bullets(lines), ""])

    choices, reconciled = data.get("choices") or [], data.get("reconciled") or []
    heading(2, _counted(MD["choicesHeading"], len(choices)))
    if not choices:
        paragraph(MD["noChoices"])
    else:
        lines = []
        for index, choice in enumerate(choices):
            result = reconciled[index] if index < len(reconciled) else None
            status = MD["choiceApplicable"] if result and result["status"] == "applicable" else MD["choicePending"]
            reason = (CHOICE_REASONS.get(result.get("code") or "", result["reason"]) if result else "")
            span = (result or {}).get("span") or choice["source_span"]
            lines.append(inline(choice["source_span"]["text"]) + " · " + _span_label(span) + " · "
                         + _pair(choice["candidate"]) + " · " + status + (" · " + reason if reason else ""))
        out.extend([*_bullets(lines), ""])
        out.extend([*_bullets([fill(MD["basisLine"], rule=inline(c["basis"]["rule_version"]),
                                    rev=inline(c["basis"]["dictionary"]["revision"]),
                                    commit=inline(c["basis"]["dictionary"]["source_commit"] or MD["notProvided"]))
                               for c in choices]), ""])

    heading(2, MD["comparisonHeading"])
    comparison = data.get("comparison")
    if not latest:
        paragraph(MD["comparisonNone"])
    elif not data.get("previous"):
        paragraph(MD["comparisonSingle"])
    elif comparison:
        for key, label in COMPARISON_LABELS:
            items = comparison[key]
            if not items:
                continue
            heading(3, _counted(label, len(items)))
            lines = []
            for item in items:
                mention = item["mention"]
                template = COMPARISON_MENTION_HISTORICAL if mention["historical"] else COMPARISON_MENTION
                before, after = template.split("{text}")
                lines.append(before + inline(mention["text"]) + fill(after, n=mention["start"] + 1) + COLON
                             + COMPARISON_REASONS[item["code"]])
            out.extend([*_bullets(lines), ""])
        paragraph(MD["comparisonNote"])

    segment_terms = data.get("segment_terms")
    if segment_terms:
        heading(2, fill(MD["segmentTermsHeading"], n=len(segment_terms)))
        paragraph(MD["segmentTermsNote"])
        for segment in segment_terms:
            state = segment.get("state")
            mark = (MD["segmentTermsUnchecked"] if state == "pending" else MD["segmentTermsSaved"] if state == "saved"
                    else MD["segmentTermsStale"] if segment["stale"] else "")
            out.extend(["### " + fill(MD["segmentTermsLabel"], n=segment["index"] + 1) + (" · " + mark if mark else ""),
                        ""])
            if state == "pending":
                paragraph(MD["segmentTermsNoResult"])
            elif not segment["findings"]:
                paragraph(MD["segmentTermsNone"])
            else:
                out.extend([*_bullets([
                    VERDICTS.get(f["verdict"], f["verdict"]) + " · " + inline(f["source_span"]["text"]) + " · "
                    + _span_label(f["source_span"])
                    + (" · " + _span_label(f["target_span"], "targetSpanLabel") + COLON + inline(f["target_span"]["text"])
                       if f["target_span"] else " · " + MD["noTargetSpan"])
                    + (" · " + SEMICOLON.join(inline(c["zh"]) + " / " + inline(c["en"]) for c in f["candidates"])
                       if f["candidates"] else "")
                    for f in segment["findings"]]), ""])
            if segment.get("choices"):
                out.extend([*_bullets([
                    MD["segmentChoiceLabel"] + " · " + inline(c["source_span"]["text"]) + " · " + _span_label(c["source_span"])
                    + " · " + _pair(c["candidate"]) + " · "
                    + (MD["segmentChoiceConfirmed"] if c["confirmed"] else MD["segmentChoiceUnconfirmed"])
                    for c in segment["choices"]]), ""])
    if data.get("segment_choice_history"):
        paragraph(fill(MD["segmentChoiceHistory"], n=data["segment_choice_history"]))

    semantic = data.get("semantic")
    if semantic:
        heading(2, MD["semanticHeading"])
        paragraph(fill(MD["semanticCoverage"], reviewed=semantic["segments"]["reviewed"],
                       total=semantic["segments"]["total"]))
        paragraph(fill(MD["semanticResolved"], n=semantic["resolved"]))
        if not semantic["items"]:
            paragraph(MD["semanticNone"])
        for number, item in enumerate(semantic["items"], 1):
            where = "" if item["segment_index"] is None else fill(MD["semanticSegment"], n=item["segment_index"] + 1) + " · "
            out.extend(["### " + str(number) + ". " + where
                        + SEMANTIC_CATEGORIES.get(item["category"], inline(item["category"])), ""])
            suggestion = (inline(item["suggestion"]) if item["suggestion"] is not None
                          else MD["semanticWithheld"] if item["suggestion_withheld"] is not None
                          else MD["semanticNoSuggestion"])
            out.extend([*_bullets([
                MD["semanticConfidenceLabel"] + COLON + SEMANTIC_CONFIDENCES.get(item["confidence"], inline(item["confidence"])),
                "" if item["source_excerpt"] is None else MD["semanticSourceLabel"] + COLON + inline(item["source_excerpt"]),
                "" if item["target_excerpt"] is None else MD["semanticTargetLabel"] + COLON + inline(item["target_excerpt"]),
                MD["semanticRationaleLabel"] + COLON + inline(item["rationale"]),
                MD["semanticSuggestionLabel"] + COLON + suggestion,
                MD["semanticStatusLabel"] + COLON + SEMANTIC_STATUSES.get(item["status"], inline(item["status"])),
            ]), ""])

    paragraph(MD["notCertifiedLine"])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip() + "\n"
