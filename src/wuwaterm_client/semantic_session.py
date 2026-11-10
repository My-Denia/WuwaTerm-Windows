"""Runtime state of a segmented semantic review, kept apart from widgets.

A semantic report belongs to the exact segment texts that were sent. The
workbench keeps those results while the owner edits the manuscript: every edit
is reduced to one replaced range, positions after it shift, and a position that
overlapped it stays where it was. Nothing is trusted from a position alone; an
issue or a segment is usable only while its recorded text still equals the
current slice. This keeps model results across an accepted suggestion (and its
undo) without ever executing a range that no longer matches.

Offsets are Unicode scalar indices into the whole manuscript side. Report
offsets are relative to the segment core that was sent and are converted once,
on arrival, by adding the core start.

Pure Python: no Qt, no HTTP.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import semantic

DECIDED = ("accepted", "adjusted", "ignored")
# Display states, derived on demand from the decisions in the payload.
STATE_PENDING = "pending"
STATE_NEEDS_RECONFIRM = "needs_reconfirm"
STATE_STALE = "stale"
STATE_IGNORED_CARRIED = "ignored_carried"
UNHANDLED = (STATE_PENDING, STATE_NEEDS_RECONFIRM, STATE_STALE)


def revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def changed_range(old: str, new: str) -> tuple[int, int, int]:
    """One replaced range ``old[p:old_end] -> new[p:new_end]``.

    Bisects on slice equality so a keystroke in a long manuscript compares
    in C rather than scalar by scalar in Python.
    """
    limit = min(len(old), len(new))
    low, high = 0, limit
    while low < high:
        mid = (low + high + 1) // 2
        if old[:mid] == new[:mid]:
            low = mid
        else:
            high = mid - 1
    p = low
    low, high = 0, limit - p
    while low < high:
        mid = (low + high + 1) // 2
        if old[len(old) - mid:] == new[len(new) - mid:]:
            low = mid
        else:
            high = mid - 1
    return p, len(old) - low, len(new) - low


def _shift(start: int, end: int, p: int, old_end: int, new_end: int) -> tuple[int, int]:
    if end <= p and not (start == end == p):
        return start, end
    if start >= old_end:
        delta = new_end - old_end
        return start + delta, end + delta
    # Overlapping: keep the position; liveness is decided by the slice check.
    return start, end


def shift_span(span: dict | None, p: int, old_end: int, new_end: int) -> dict | None:
    if span is None:
        return None
    start, end = _shift(span["start"], span["end"], p, old_end, new_end)
    return {"start": start, "end": end, "text": span["text"]}


def span_live(span: dict | None, text: str) -> bool:
    if span is None:
        return True
    start, end = span["start"], span["end"]
    return 0 <= start < end <= len(text) and text[start:end] == span["text"]


def _offset(span: dict | None, by: int) -> dict | None:
    if span is None:
        return None
    return {"start": span["start"] + by, "end": span["end"] + by, "text": span["text"]}


@dataclass
class LiveIssue:
    """One model issue in whole-manuscript coordinates."""

    issue: dict[str, Any]          # full-coordinate copy as reported (record)
    segment_index: int
    source_revision: str           # whole source when the report arrived
    target_revision: str           # whole target the issue was reported against
    term_basis: dict[str, str]
    source_span: dict | None       # current positions (shifted by edits)
    target_span: dict | None
    suggestion_span: dict | None
    base_state: str = STATE_PENDING
    previous: dict | None = None
    previously_applied: bool = False
    # Decisions already on file when the report arrived. They inform the base
    # state through the merge rules; they never count as a decision on this
    # report by themselves (an identical issue id is not a new decision).
    preexisting: frozenset | None = None

    @property
    def id(self) -> str:
        return self.issue["id"]

    def live(self, source: str, target: str) -> bool:
        return (span_live(self.source_span, source) and span_live(self.target_span, target)
                and span_live(self.suggestion_span, target))

    def edit_span(self) -> dict | None:
        """The executable target range an adjustment replaces."""
        return self.suggestion_span or self.target_span


@dataclass
class SegmentResult:
    """A semantic (and, for long manuscripts, term) result for one sent pair."""

    sent_source: str
    sent_target: str
    source_core: tuple[int, int]   # current positions of the sent cores
    target_core: tuple[int, int]
    hard_cut: bool
    report: dict[str, Any] | None
    index: int = 0
    issues: list[LiveIssue] = field(default_factory=list)
    term_findings: list[dict] = field(default_factory=list)  # full coordinates
    term_report: dict[str, Any] | None = None

    def current(self, source: str, target: str) -> bool:
        (a, b), (c, d) = self.source_core, self.target_core
        return source[a:b] == self.sent_source and target[c:d] == self.sent_target

    def matches(self, pair, source: str, target: str) -> bool:
        return (self.source_core == (pair.source.core_start, pair.source.core_end)
                and self.target_core == (pair.target.core_start, pair.target.core_end)
                and self.current(source, target))


def _shift_core(core: tuple[int, int], p: int, old_end: int, new_end: int) -> tuple[int, int]:
    return _shift(core[0], core[1], p, old_end, new_end)


class Session:
    """Semantic results for the open manuscript."""

    def __init__(self) -> None:
        self.results: list[SegmentResult] = []
        # The pair whose request is in flight; edits shift it like a result.
        self.pending: SegmentResult | None = None

    def clear(self) -> None:
        self.results.clear()
        self.pending = None

    # -- edits -----------------------------------------------------------
    def text_changed(self, side: str, old: str, new: str) -> None:
        if old == new or not (self.results or self.pending is not None):
            return
        p, old_end, new_end = changed_range(old, new)
        for result in self.results + ([self.pending] if self.pending is not None else []):
            if side == "source":
                result.source_core = _shift_core(result.source_core, p, old_end, new_end)
            else:
                result.target_core = _shift_core(result.target_core, p, old_end, new_end)
            for issue in result.issues:
                if side == "source":
                    issue.source_span = shift_span(issue.source_span, p, old_end, new_end)
                else:
                    issue.target_span = shift_span(issue.target_span, p, old_end, new_end)
                    issue.suggestion_span = shift_span(issue.suggestion_span, p, old_end, new_end)
            shifted = []
            for finding in result.term_findings:
                finding = dict(finding)
                key = "source_span" if side == "source" else "target_span"
                finding[key] = shift_span(finding.get(key), p, old_end, new_end)
                shifted.append(finding)
            result.term_findings = shifted

    # -- results ---------------------------------------------------------
    def replace_result(self, result: SegmentResult) -> None:
        """Keep at most one result per sent source core position."""
        self.results = [r for r in self.results if r.source_core != result.source_core]
        self.results.append(result)
        self.results.sort(key=lambda r: r.source_core)

    def prune(self, pairs, source: str, target: str) -> None:
        """Drop results no current ready pair can use any more."""
        self.results = [r for r in self.results if any(r.matches(p, source, target) for p in pairs)]

    def result_for(self, pair, source: str, target: str) -> SegmentResult | None:
        for result in self.results:
            if result.matches(pair, source, target):
                return result
        return None

    def issues(self) -> list[LiveIssue]:
        return [issue for result in self.results for issue in result.issues]

    def term_findings(self, source: str, target: str) -> list[dict]:
        out = []
        for result in self.results:
            if not result.current(source, target):
                continue
            for finding in result.term_findings:
                if span_live(finding.get("source_span"), source) and span_live(finding.get("target_span"), target):
                    out.append(finding)
        return out


def live_issues_from_report(report: dict, *, segment_index: int, source_offset: int, target_offset: int,
                            source_revision: str, target_revision: str) -> list[LiveIssue]:
    issues = []
    for raw in report["issues"]:
        full = copy.deepcopy(raw)
        full["segment_index"] = segment_index
        full["source_span"] = _offset(raw["source_span"], source_offset)
        full["target_span"] = _offset(raw["target_span"], target_offset)
        if raw["suggestion"] is not None:
            full["suggestion"]["target_span"] = _offset(raw["suggestion"]["target_span"], target_offset)
        suggestion_span = full["suggestion"]["target_span"] if full["suggestion"] is not None else None
        issues.append(LiveIssue(
            issue=full, segment_index=segment_index,
            source_revision=source_revision, target_revision=target_revision,
            term_basis=dict(report["term_basis"]),
            source_span=copy.deepcopy(full["source_span"]), target_span=copy.deepcopy(full["target_span"]),
            suggestion_span=copy.deepcopy(suggestion_span),
        ))
    return issues


def term_findings_from_report(report: dict, *, source_offset: int, target_offset: int) -> list[dict]:
    findings = []
    for raw in report["findings"]:
        finding = copy.deepcopy(raw)
        finding["source_span"] = _offset(raw["source_span"], source_offset)
        finding["target_span"] = _offset(raw["target_span"], target_offset)
        findings.append(finding)
    return findings


def _same_range(a: dict | None, b: dict | None) -> bool:
    if a is None:
        return b is None
    return b is not None and a["start"] == b["start"] and a["end"] == b["end"]


def verified_texts(entries) -> dict[str, str]:
    """``semantic_texts`` entries whose sha256 matches; a damaged entry is unusable."""
    return {e["revision"]: e["text"] for e in entries if revision(e["text"]) == e["revision"]}


def reconcile(decisions: list[dict], texts: dict[str, str], source: str, target: str,
              issues: list[dict]) -> list[dict]:
    """Web ``reconcileSemanticDecisions``, one outcome per decision.

    The fingerprint only finds candidates. Only an ``ignored`` decision carries
    over, when both old texts are available and verified, every span recovers
    uniquely (``recover_span``), exactly one candidate exists in the whole
    report, and it sits at the recovered place. Accepted and adjusted decisions
    never carry; reported again at the same source place they are
    ``previously_applied``. Everything else needs reconfirmation.
    """
    from .manuscript import recover_span

    source_revision, target_revision = revision(source), revision(target)

    def text_for(rev: str, current: str, current_rev: str) -> str | None:
        return current if rev == current_rev else texts.get(rev)

    results = []
    for decision in decisions:
        candidates = [i for i in issues if i["fingerprint"] == decision["fingerprint"]
                      and i["category"] == decision["category"]]

        def outcome(status: str, code: str, issue: dict | None = None) -> dict:
            return {"decision": decision, "status": status, "code": code,
                    "issue_id": None if issue is None else issue["id"],
                    "candidates": [c["id"] for c in candidates]}
        if not candidates:
            results.append(outcome("not_reported", "not_reported"))
            continue
        old_source = text_for(decision["source_revision"], source, source_revision)
        if decision["decision"] != "ignored":
            here = (recover_span(old_source, decision["source_span"], source)
                    if old_source is not None and decision["source_span"] is not None else None)
            match = next((c for c in candidates if here and c["source_span"] and _same_range(c["source_span"], here)), None)
            results.append(outcome("previously_applied", "previously_applied", match) if match
                           else outcome("needs_reconfirmation", "applied_not_carried"))
            continue
        old_target = text_for(decision["target_revision"], target, target_revision)
        if old_source is None or old_target is None:
            results.append(outcome("needs_reconfirmation", "old_text_unavailable"))
            continue
        spans = []
        failed = None
        for span, old, current, code in ((decision["source_span"], old_source, source, "source_span_unrecoverable"),
                                         (decision["target_span"], old_target, target, "target_span_unrecoverable")):
            recovered = None if span is None else recover_span(old, span, current)
            if span is not None and recovered is None:
                failed = code
                break
            spans.append(recovered)
        if failed:
            results.append(outcome("needs_reconfirmation", failed))
        elif len(candidates) != 1:
            results.append(outcome("needs_reconfirmation", "multiple_candidates"))
        elif not (_same_range(spans[0], candidates[0]["source_span"]) and _same_range(spans[1], candidates[0]["target_span"])):
            results.append(outcome("needs_reconfirmation", "candidate_elsewhere"))
        else:
            results.append(outcome("carried", "carried", candidates[0]))
    counts: dict[str, int] = {}
    for r in results:
        if r["status"] == "carried":
            counts[r["issue_id"]] = counts.get(r["issue_id"], 0) + 1
    for r in results:
        if r["status"] == "carried" and counts[r["issue_id"]] > 1:
            r.update(status="needs_reconfirmation", code="converged_on_one_issue", issue_id=None)
    return results


def apply_reconcile(issues: list[LiveIssue], decisions: list[dict], texts: dict[str, str],
                    source: str, target: str) -> None:
    """Set every issue's base state from the decisions made before it arrived.

    ``issues`` is the whole current report (all segments), so "exactly one
    candidate" means one in the manuscript, as on the Web.
    """
    for issue in issues:
        if issue.preexisting is None:
            issue.preexisting = frozenset(_key(d) for d in decisions if _matches_decision(issue, d))
    # A decision made on an issue of this session is that issue's own state,
    # not history to merge.
    own = {_key(d) for d in decisions for issue in issues
           if _matches_decision(issue, d) and _key(d) not in issue.preexisting}
    prior = [d for d in decisions if _key(d) not in own]
    outcomes = reconcile(prior, texts, source, target, [issue.issue for issue in issues])
    rank = {STATE_IGNORED_CARRIED: 3, "previously_applied": 2, STATE_NEEDS_RECONFIRM: 1, STATE_PENDING: 0}
    best: dict[str, tuple[str, dict]] = {}

    def offer(issue_id: str, state: str, decision: dict) -> None:
        if issue_id not in best or rank[state] > rank[best[issue_id][0]]:
            best[issue_id] = (state, decision)
    for r in outcomes:
        if r["status"] == "carried":
            offer(r["issue_id"], STATE_IGNORED_CARRIED, r["decision"])
        elif r["status"] == "previously_applied":
            offer(r["issue_id"], "previously_applied", r["decision"])
        elif r["status"] == "needs_reconfirmation":
            for issue_id in r["candidates"]:
                offer(issue_id, STATE_NEEDS_RECONFIRM, r["decision"])
    for issue in issues:
        state, decision = best.get(issue.id, (STATE_PENDING, None))
        issue.previously_applied = state == "previously_applied"
        issue.base_state = STATE_PENDING if issue.previously_applied else state
        issue.previous = decision


def _key(decision: dict) -> str:
    return json.dumps(decision, sort_keys=True, ensure_ascii=False)


def _matches_decision(issue: LiveIssue, decision: dict) -> bool:
    return (decision.get("issue_id") == issue.id and decision.get("source_revision") == issue.source_revision
            and decision.get("target_revision") == issue.target_revision)


def decision_for(issue: LiveIssue, decisions: list[dict]) -> dict | None:
    """The decision made on this report's issue, if any."""
    for decision in reversed(decisions):
        if _matches_decision(issue, decision) and _key(decision) not in (issue.preexisting or ()):
            return decision
    return None


def state_of(issue: LiveIssue, decisions: list[dict], source: str, target: str) -> str:
    decision = decision_for(issue, decisions)
    if decision is not None:
        return decision["decision"]
    if issue.base_state == STATE_IGNORED_CARRIED:
        return STATE_IGNORED_CARRIED
    if not issue.live(source, target):
        return STATE_STALE
    if issue.base_state == STATE_NEEDS_RECONFIRM:
        return STATE_NEEDS_RECONFIRM
    return STATE_PENDING


def make_decision(issue: LiveIssue, decision: str, applied_text: str | None) -> dict:
    if decision not in DECIDED:
        raise ValueError(decision)
    record = issue.issue
    return {
        "issue_id": record["id"], "fingerprint": record["fingerprint"], "category": record["category"],
        "segment_index": issue.segment_index,
        "source_revision": issue.source_revision, "target_revision": issue.target_revision,
        "source_span": copy.deepcopy(record["source_span"]), "target_span": copy.deepcopy(record["target_span"]),
        "decision": decision, "applied_text": applied_text if decision != "ignored" else None,
        "basis": {"review_version": semantic.REVIEW_VERSION, "term_basis": dict(issue.term_basis)},
    }


def with_decision(decisions: list[dict], issue: LiveIssue, record: dict) -> list[dict]:
    """Replace this issue's earlier record and an ``ignored`` decision it supersedes.

    Accepted and adjusted records already changed the text; they stay as
    history even when the same place is reported again.
    """
    from .manuscript import occurrence_key

    superseded = issue.previous if issue.previous and issue.previous.get("decision") == "ignored" else None
    occurrence = occurrence_key(record)
    kept = [d for d in decisions if not _matches_decision(issue, d) and d != superseded
            and occurrence_key(d) != occurrence]
    return kept + [record]


def _snippet(source: str, start: int, end: int) -> str:
    width = semantic.MAX_PRECEDENT_SNIPPET_SCALARS
    left = max(0, start - max(0, width - (end - start)) // 2)
    right = min(len(source), left + width)
    left = max(0, right - width)
    return source[left:right]


def precedent_candidates(payload: dict, pairs) -> list[tuple[int, dict]]:
    """Human decisions, as ``(end position, precedent)`` for ``select_precedents``.

    Only human actions qualify: an official-pair or not-a-term choice, or a
    rendering the owner accepted or adjusted. Automatic matches and review
    results never become precedents (ADR 0019). Each precedent states one
    occurrence and is anchored to the segment that contains it.
    """
    from .manuscript import _recovered_mention

    source = payload["source"]
    direction = payload["direction"]

    def anchor(start: int, end: int) -> dict | None:
        for pair in pairs:
            if pair.source.core_start <= start and end <= pair.source.core_end:
                return {"segment_index": pair.index, "source_start": start - pair.source.core_start,
                        "source_end": end - pair.source.core_start}
        return None

    def candidate(start: int, end: int, choice: str, rendering: str | None) -> tuple[int, dict] | None:
        surface = source[start:end]
        if not 0 < len(surface) <= semantic.MAX_PRECEDENT_SURFACE_SCALARS:
            return None
        if rendering is not None and not 0 < len(rendering) <= semantic.MAX_PRECEDENT_RENDERING_SCALARS:
            return None
        where = anchor(start, end)
        if where is None:
            return None
        return end, {"surface": surface, "choice": choice, "rendering": rendering,
                     "snippet": _snippet(source, start, end), "anchor": where, "scope": "occurrence"}

    out: list[tuple[int, dict]] = []
    for choice in payload.get("choices", []):
        mention = _recovered_mention(choice, source)
        if mention is None:
            continue
        start_text, end_text, _ = mention.split(":", 2)
        start, end = int(start_text), int(end_text)
        if choice["choice"] == "official_pair":
            item = candidate(start, end, "official_pair", choice["candidate"][direction])
        else:
            item = candidate(start, end, "not_a_term", None)
        if item is not None:
            out.append(item)
    current = revision(source)
    for decision in payload.get("semantic_decisions", []):
        span = decision["source_span"]
        if (decision["decision"] in ("accepted", "adjusted") and span is not None
                and decision["source_revision"] == current and source[span["start"]:span["end"]] == span["text"]):
            item = candidate(span["start"], span["end"], "user_rendering", decision["applied_text"])
            if item is not None:
                out.append(item)
    return out


def semantic_texts_for(decisions: list[dict], known: dict[str, str], source: str, target: str,
                       limit: int = 16) -> list[dict]:
    """Web ``buildSemanticTexts``: the verified old texts that ``ignored``
    decisions need beyond the current texts, newest decisions first kept."""
    current = {revision(source), revision(target)}
    wanted: list[str] = []
    for decision in reversed(decisions):
        if decision["decision"] != "ignored":
            continue
        for rev in (decision["source_revision"], decision["target_revision"]):
            if rev not in current and rev in known and rev not in wanted:
                wanted.append(rev)
    return [{"revision": rev, "text": known[rev]} for rev in reversed(wanted[:limit])]
