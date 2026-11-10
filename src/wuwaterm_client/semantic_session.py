"""Runtime state of a segmented semantic review, kept apart from widgets.

A semantic report belongs to the exact segment texts that were sent. The
workbench keeps those results while the owner edits the manuscript. Every edit
arrives as the range the editor (or the workbench itself) actually replaced; it
is never guessed from a text difference, because repeated text makes such a
guess ambiguous. Positions after the edit shift; anything the edit overlaps is
invalidated for good and needs reconfirmation. Undo restores a snapshot of
these positions together with the text.

Identity is a whole-document occurrence plus a text version: an issue's
reported spans and the revisions of the whole texts it was reported against.
A request-local issue id or the text content is never an identity, because
identical paragraphs produce identical ids and texts.

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


def _shift(start: int, end: int, p: int, old_end: int, new_end: int) -> tuple[int, int, bool]:
    """Move ``[start, end)`` across the edit ``[p, old_end) -> [p, new_end)``.

    Returns the new range and whether the edit overlapped it. An insertion at
    either boundary does not overlap; one strictly inside does.
    """
    if end <= p and not (start == end == p):
        return start, end, False
    if start >= old_end:
        delta = new_end - old_end
        return start + delta, end + delta, False
    return start, end, True


def shift_span(span: dict | None, p: int, old_end: int, new_end: int) -> tuple[dict | None, bool]:
    if span is None:
        return None, False
    start, end, hit = _shift(span["start"], span["end"], p, old_end, new_end)
    return {"start": start, "end": end, "text": span["text"]}, hit


def span_live(span: dict | None, text: str) -> bool:
    if span is None:
        return True
    start, end = span["start"], span["end"]
    return 0 <= start < end <= len(text) and text[start:end] == span["text"]


def _offset(span: dict | None, by: int) -> dict | None:
    if span is None:
        return None
    return {"start": span["start"] + by, "end": span["end"] + by, "text": span["text"]}


@dataclass(eq=False)
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
    # report by themselves.
    preexisting: frozenset | None = None
    # An edit overlapped this occurrence: it needs reconfirmation for good.
    invalid: bool = False

    @property
    def id(self) -> str:
        return self.issue["id"]

    @property
    def occurrence(self) -> str:
        """Whole-document occurrence plus text version (Web ``occurrenceKey``)."""
        from .manuscript import occurrence_key
        return occurrence_key({"category": self.issue["category"], "source_revision": self.source_revision,
                               "target_revision": self.target_revision, "source_span": self.issue["source_span"],
                               "target_span": self.issue["target_span"]})

    def live(self, source: str, target: str) -> bool:
        return (span_live(self.source_span, source) and span_live(self.target_span, target)
                and span_live(self.suggestion_span, target))

    def edit_span(self) -> dict | None:
        """The executable target range an adjustment replaces."""
        return self.suggestion_span or self.target_span


@dataclass(eq=False)
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
    # The alignments the term request carried (segment coordinates, or None
    # for automatic): a term report is reused only under the same setting.
    term_alignments: list | None = None
    invalid: bool = False

    def current(self, source: str, target: str) -> bool:
        (a, b), (c, d) = self.source_core, self.target_core
        return (not self.invalid and source[a:b] == self.sent_source and target[c:d] == self.sent_target)

    def matches(self, pair, source: str, target: str) -> bool:
        return (self.source_core == (pair.source.core_start, pair.source.core_end)
                and self.target_core == (pair.target.core_start, pair.target.core_end)
                and self.current(source, target))


class Session:
    """Semantic results for the open manuscript."""

    def __init__(self) -> None:
        self.results: list[SegmentResult] = []
        # The pair whose request is in flight; edits move it like a result.
        self.pending: SegmentResult | None = None

    def clear(self) -> None:
        self.results.clear()
        self.pending = None

    # -- edits -----------------------------------------------------------
    def apply_edit(self, side: str, start: int, old_end: int, new_len: int,
                   exempt: LiveIssue | None = None) -> None:
        """One replacement the editor or the workbench actually performed.

        ``exempt`` is the issue whose own accept or adjust made this edit.
        """
        p, new_end = start, start + new_len
        source_side = side == "source"
        for result in self.results + ([self.pending] if self.pending is not None else []):
            core = result.source_core if source_side else result.target_core
            a, b, hit = _shift(core[0], core[1], p, old_end, new_end)
            if source_side:
                result.source_core = (a, b)
            else:
                result.target_core = (a, b)
            result.invalid = result.invalid or hit
            for issue in result.issues:
                keys = ("source_span",) if source_side else ("target_span", "suggestion_span")
                for key in keys:
                    span, hit = shift_span(getattr(issue, key), p, old_end, new_end)
                    setattr(issue, key, span)
                    if hit and issue is not exempt:
                        issue.invalid = True
            kept = []
            key = "source_span" if source_side else "target_span"
            for finding in result.term_findings:
                span, hit = shift_span(finding.get(key), p, old_end, new_end)
                if not hit:
                    kept.append({**finding, key: span})
            result.term_findings = kept

    def snapshot(self) -> dict:
        """Positions and validity, so undo can restore them with the text."""
        state: dict[int, Any] = {}
        for result in self.results + ([self.pending] if self.pending is not None else []):
            state[id(result)] = (result, result.source_core, result.target_core, result.invalid,
                                 list(result.term_findings))
            for issue in result.issues:
                state[id(issue)] = (issue, issue.source_span, issue.target_span, issue.suggestion_span, issue.invalid)
        return state

    def restore(self, state: dict) -> None:
        """Objects created after the snapshot cannot be mapped back: invalid."""
        for result in self.results + ([self.pending] if self.pending is not None else []):
            saved = state.get(id(result))
            if saved is None or saved[0] is not result:
                result.invalid = True
                for issue in result.issues:
                    issue.invalid = True
                continue
            _, result.source_core, result.target_core, result.invalid, findings = saved
            result.term_findings = list(findings)
            for issue in result.issues:
                kept = state.get(id(issue))
                if kept is None or kept[0] is not issue:
                    issue.invalid = True
                else:
                    _, issue.source_span, issue.target_span, issue.suggestion_span, issue.invalid = kept

    # -- results ---------------------------------------------------------
    def add(self, result: SegmentResult) -> None:
        self.results.append(result)
        self.results.sort(key=lambda r: r.source_core)

    def rebuild(self, pairs, source: str, target: str, basis: "Basis") -> None:
        """Apply the segment reuse rule (see ``Basis``) to every cached result."""
        kept = []
        for result in self.results:
            if not any(p.status == "ready" and result.matches(p, source, target) for p in pairs):
                continue
            if result.term_report is not None and not term_part_valid(result, basis):
                result.term_report, result.term_alignments, result.term_findings = None, None, []
            if result.report is not None and not (basis.semantic_required and semantic_part_valid(result)):
                result.report, result.issues = None, []
            if result.term_report is not None or result.report is not None:
                kept.append(result)
        self.results = kept

    def result_for(self, pair, source: str, target: str) -> SegmentResult | None:
        if pair.status != "ready":
            return None
        for result in self.results:
            if result.matches(pair, source, target):
                return result
        return None

    def issues(self) -> list[LiveIssue]:
        return [issue for result in self.results for issue in result.issues]


@dataclass(frozen=True)
class Basis:
    """What a segment result must have been produced under to be reused.

    SEGMENT REUSE RULE (the one place it is defined; README and PR quote it):
    a cached segment result (term report, semantic report, failure or note,
    or the answer to a request in flight) is used only when

    1. its core ranges equal those of a current *ready* pair exactly, and
       both sides' texts at those ranges are the ones that were sent (text
       version) and no edit has overlapped them since; and
    2. its basis equals the current basis: the alignments the segment's term
       request carried (``None`` for automatic), the Core capabilities (when
       the server lists semantic-review-v1 a segment is complete only with a
       complete semantic report; when it does not, no semantic report is
       reused), and the contract versions (review-v2, semantic-review-v1).

    At the start of every run the plan is rebuilt from the current pairing
    and everything else is dropped. Decisions are not segment results: they
    stay in the manuscript and follow the occurrence rules.
    """

    term_required: bool            # long manuscripts: one term report per segment
    semantic_required: bool        # the server lists semantic-review-v1
    alignments: Any                # (source_core, target_core) -> list | None

    def needs(self, result: "SegmentResult | None") -> tuple[bool, bool]:
        """(needs a term request, needs a semantic request) for one ready pair."""
        if result is None:
            return self.term_required, self.semantic_required
        return (self.term_required and not term_part_valid(result, self),
                self.semantic_required and not semantic_part_valid(result))


# COORDINATE RULE (alongside the reuse rule above; one place):
# * Identity is the original report coordinates plus the text versions they
#   were reported against: ``LiveIssue.issue`` spans with ``source_revision``/
#   ``target_revision`` (``LiveIssue.occurrence``, decision records,
#   ``make_decision``, ``_matches_decision``). These never change.
# * Every position comparison uses current coordinates: ``LiveIssue``
#   ``source_span``/``target_span``/``suggestion_span`` and the result cores,
#   which edits shift (``Session.apply_edit``) and undo/redo restore
#   (``Session.snapshot``/``restore``). Re-review compares recovered decision
#   spans with ``_current_view`` of each issue; highlighting, liveness and
#   accept/adjust use the current spans.
# A decision's own spans are compared only against the text version they
# belong to (recovered with ``recover_span`` first).

TERM_CONTRACT = "review-v2"


def term_part_valid(result: SegmentResult, basis: Basis) -> bool:
    return (result.term_report is not None and result.term_report["rule_version"] == TERM_CONTRACT
            and result.term_alignments == basis.alignments(result.source_core, result.target_core))


def semantic_part_valid(result: SegmentResult) -> bool:
    return (result.report is not None and result.report["status"] == "complete"
            and result.report["review_version"] == semantic.REVIEW_VERSION)


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
        # Issues are referred to by their index in ``issues``: identical
        # paragraphs produce identical request-local ids.
        candidates = [index for index, i in enumerate(issues) if i["fingerprint"] == decision["fingerprint"]
                      and i["category"] == decision["category"]]

        def outcome(status: str, code: str, index: int | None = None) -> dict:
            return {"decision": decision, "status": status, "code": code,
                    "issue": index, "candidates": list(candidates)}
        if not candidates:
            results.append(outcome("not_reported", "not_reported"))
            continue
        old_source = text_for(decision["source_revision"], source, source_revision)
        if decision["decision"] != "ignored":
            here = (recover_span(old_source, decision["source_span"], source)
                    if old_source is not None and decision["source_span"] is not None else None)
            match = next((c for c in candidates if here and issues[c]["source_span"]
                          and _same_range(issues[c]["source_span"], here)), None)
            results.append(outcome("previously_applied", "previously_applied", match) if match is not None
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
        elif not (_same_range(spans[0], issues[candidates[0]]["source_span"])
                  and _same_range(spans[1], issues[candidates[0]]["target_span"])):
            results.append(outcome("needs_reconfirmation", "candidate_elsewhere"))
        else:
            results.append(outcome("carried", "carried", candidates[0]))
    counts: dict[int, int] = {}
    for r in results:
        if r["status"] == "carried":
            counts[r["issue"]] = counts.get(r["issue"], 0) + 1
    for r in results:
        if r["status"] == "carried" and counts[r["issue"]] > 1:
            r.update(status="needs_reconfirmation", code="converged_on_one_issue", issue=None)
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
    # Compare at current positions (see COORDINATE RULE); identity stays with
    # the original report coordinates and versions held in ``issue.issue``.
    outcomes = reconcile(prior, texts, source, target, [_current_view(issue) for issue in issues])
    rank = {STATE_IGNORED_CARRIED: 3, "previously_applied": 2, STATE_NEEDS_RECONFIRM: 1, STATE_PENDING: 0}
    best: dict[int, tuple[str, dict]] = {}

    def offer(index: int, state: str, decision: dict) -> None:
        if index not in best or rank[state] > rank[best[index][0]]:
            best[index] = (state, decision)
    for r in outcomes:
        if r["status"] == "carried":
            offer(r["issue"], STATE_IGNORED_CARRIED, r["decision"])
        elif r["status"] == "previously_applied":
            offer(r["issue"], "previously_applied", r["decision"])
        elif r["status"] == "needs_reconfirmation":
            for index in r["candidates"]:
                offer(index, STATE_NEEDS_RECONFIRM, r["decision"])
    for index, issue in enumerate(issues):
        state, decision = best.get(index, (STATE_PENDING, None))
        issue.previously_applied = state == "previously_applied"
        issue.base_state = STATE_PENDING if issue.previously_applied else state
        issue.previous = decision


def _current_view(issue: LiveIssue) -> dict:
    """The issue as it sits in the current text, for position comparisons."""
    return {**issue.issue, "source_span": issue.source_span, "target_span": issue.target_span}


def _key(decision: dict) -> str:
    return json.dumps(decision, sort_keys=True, ensure_ascii=False)


def _matches_decision(issue: LiveIssue, decision: dict) -> bool:
    """Same whole-document occurrence and text version; never the request-local id."""
    from .manuscript import occurrence_key
    return occurrence_key(decision) == issue.occurrence


def decision_for(issue: LiveIssue, decisions: list[dict]) -> dict | None:
    """The decision made on this report's issue, if any."""
    for decision in reversed(decisions):
        if _matches_decision(issue, decision) and _key(decision) not in (issue.preexisting or ()):
            return decision
    return None


def state_of(issue: LiveIssue, decisions: list[dict], source: str, target: str) -> str:
    """Validity first: an occurrence an edit touched needs reconfirmation,
    whatever was decided there before (the decision stays as history)."""
    if issue.invalid:
        return STATE_STALE
    decision = decision_for(issue, decisions)
    if decision is not None and decision["decision"] != "ignored":
        # Accept and adjust replaced this occurrence themselves.
        return decision["decision"]
    if not issue.live(source, target):
        return STATE_STALE
    if decision is not None:
        return decision["decision"]
    if issue.base_state in (STATE_IGNORED_CARRIED, STATE_NEEDS_RECONFIRM):
        return issue.base_state
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
