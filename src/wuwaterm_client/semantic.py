"""Client side of Core's ``semantic-review-v1`` wire contract (Core 7122ea7).

Standard library only, so the manuscript module and the API client can import
it and a pure test environment can run it. It owns three things: encoding the
request exactly (including the byte budget), refusing any response that is not
the contract's for the text that was sent, and the small deterministic rules
the review UI relies on (stale-span guard, precedent selection, re-review
merge). Offsets are Unicode scalar indices (Python str indices).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

REVIEW_VERSION = "semantic-review-v1"
CAPABILITY_SEMANTIC_REVIEW = "semantic-review-v1"
CAPABILITY_TRANSLATION_CONTEXT = "translation-context-v1"

CATEGORIES = (
    "omission",
    "unsupported_addition",
    "meaning_shift",
    "negation_change",
    "quantity_change",
    "reference_inconsistency",
    "term_sense",
)
CONFIDENCES = ("high", "medium", "low")
TERM_EFFECTS = ("none", "alters_official_match", "introduces_official_form", "not_checked")
PRECEDENT_CHOICES = ("official_pair", "not_a_term", "user_rendering")
EXPLAIN_LANGUAGES = ("zh", "en")
DIRECTIONS = ("en", "zh")
WITHHELD_REASONS = ("span_too_long", "replacement_too_long")
TRUNCATED_REASONS = ("issue_limit", "byte_limit")

MAX_SIDE_SCALARS = 2000
MAX_CONTEXT_SCALARS = 400
MAX_PRECEDENTS = 12
MAX_PRECEDENT_SURFACE_SCALARS = 32
MAX_PRECEDENT_RENDERING_SCALARS = 48
MAX_PRECEDENT_SNIPPET_SCALARS = 80
REQUEST_BYTE_LIMIT = 32 * 1024

MAX_EXCERPT_SCALARS = 200
EXCERPT_HEAD_SCALARS = 95
EXCERPT_TAIL_SCALARS = 95
EXCERPT_JOINER = "…"
MAX_RATIONALE_SCALARS = 240
MAX_SUGGESTION_SPAN_SCALARS = 200
MAX_REPLACEMENT_SCALARS = 240
MAX_ALTERNATIVES = 1
MAX_ISSUES = 16

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def text_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(text: str) -> str:
    """Core's display-only excerpt rule."""
    if len(text) <= MAX_EXCERPT_SCALARS:
        return text
    return text[:EXCERPT_HEAD_SCALARS] + EXCERPT_JOINER + text[-EXCERPT_TAIL_SCALARS:]


def _is_int(value: Any) -> bool:
    return type(value) is int


def _str(value: Any, maximum: int | None = None) -> bool:
    """A string without isolated surrogates, optionally bounded in scalars."""
    if not isinstance(value, str):
        return False
    if maximum is not None and len(value) > maximum:
        return False
    return not any("\ud800" <= c <= "\udfff" for c in value)


def _keys(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and value.keys() == fields


def _hex(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


# -- capabilities ----------------------------------------------------------


def parse_capabilities(data: Any) -> frozenset[str]:
    """The capability names a server lists. Unknown names are kept."""
    if not _keys(data, {"api_version", "capabilities", "request_id"}):
        raise ValueError("capabilities body has the wrong keys")
    if not _str(data["api_version"]) or not _str(data["request_id"]):
        raise ValueError("capabilities body has a non-string field")
    names = data["capabilities"]
    if not isinstance(names, list) or not all(_str(n) for n in names):
        raise ValueError("capabilities is not a list of strings")
    return frozenset(names)


# -- response validation ---------------------------------------------------


def _span(span: Any, side: str, *, maximum: int | None = None) -> bool:
    if not _keys(span, {"start", "end", "text"}):
        return False
    start, end = span["start"], span["end"]
    if not (_is_int(start) and _is_int(end) and 0 <= start < end <= len(side)):
        return False
    if maximum is not None and end - start > maximum:
        return False
    return _str(span["text"]) and span["text"] == side[start:end]


def _excerpt_matches(value: Any, span: Any) -> bool:
    if span is None:
        return value is None
    return isinstance(value, str) and value == excerpt(span["text"])


def _issue(issue: Any, source: str, target: str) -> bool:
    fields = {
        "id", "fingerprint", "category", "origin", "confidence", "source_span",
        "target_span", "source_excerpt", "target_excerpt", "rationale", "suggestion",
        "suggestion_withheld", "alternatives",
    }
    if not _keys(issue, fields):
        return False
    source_span, target_span = issue["source_span"], issue["target_span"]
    if not (_hex(issue["id"], _HEX32) and _hex(issue["fingerprint"], _HEX32)
            and issue["category"] in CATEGORIES and issue["origin"] == "model"
            and issue["confidence"] in CONFIDENCES):
        return False
    if source_span is None and target_span is None:
        return False
    if source_span is not None and not _span(source_span, source):
        return False
    if target_span is not None and not _span(target_span, target):
        return False
    if not (_excerpt_matches(issue["source_excerpt"], source_span)
            and _excerpt_matches(issue["target_excerpt"], target_span)):
        return False
    if not _str(issue["rationale"], MAX_RATIONALE_SCALARS):
        return False
    suggestion, withheld, alternatives = (
        issue["suggestion"], issue["suggestion_withheld"], issue["alternatives"],
    )
    if withheld is not None:
        if (not isinstance(withheld, str) or withheld not in WITHHELD_REASONS
                or suggestion is not None or alternatives != []):
            return False
    if suggestion is not None:
        if not (_keys(suggestion, {"target_span", "replacement", "term_effect"})
                and _span(suggestion["target_span"], target, maximum=MAX_SUGGESTION_SPAN_SCALARS)
                and _str(suggestion["replacement"], MAX_REPLACEMENT_SCALARS)
                and suggestion["term_effect"] in TERM_EFFECTS):
            return False
    if not isinstance(alternatives, list) or len(alternatives) > MAX_ALTERNATIVES:
        return False
    return all(
        _keys(alt, {"replacement", "term_effect"})
        and _str(alt["replacement"], MAX_REPLACEMENT_SCALARS)
        and alt["term_effect"] in TERM_EFFECTS
        for alt in alternatives
    )


def valid_semantic_report(data: Any, source: str, target: str) -> bool:
    """True only for a contract response about exactly this source and target."""
    try:
        return _valid_semantic_report(data, source, target)
    except (UnicodeEncodeError, TypeError):
        return False


def _valid_semantic_report(data: Any, source: str, target: str) -> bool:
    fields = {
        "request_id", "review_version", "source_revision", "target_revision", "term_basis",
        "status", "status_reason", "dropped_unanchored", "checked", "issues", "truncated",
        "truncated_reason", "omitted_issues",
    }
    if not _keys(data, fields) or not isinstance(source, str) or not isinstance(target, str):
        return False
    basis = data["term_basis"]
    if not (_str(data["request_id"]) and data["review_version"] == REVIEW_VERSION
            and data["source_revision"] == text_revision(source)
            and data["target_revision"] == text_revision(target)
            and _keys(basis, {"dictionary_revision", "matcher_revision"})
            and _hex(basis["dictionary_revision"], _HEX64)
            and _hex(basis["matcher_revision"], _HEX64)):
        return False
    status, reason = data["status"], data["status_reason"]
    checked, issues = data["checked"], data["issues"]
    if status not in ("complete", "not_reviewed") or not isinstance(status, str):
        return False
    if not isinstance(checked, list) or not all(
        isinstance(c, str) and c in CATEGORIES for c in checked
    ):
        return False
    if not isinstance(issues, list) or len(issues) > MAX_ISSUES:
        return False
    if status == "not_reviewed":
        if reason != "invalid_model_output" or checked != [] or issues != []:
            return False
    elif reason is not None:
        return False
    if not (_is_int(data["dropped_unanchored"]) and data["dropped_unanchored"] >= 0
            and _is_int(data["omitted_issues"]) and data["omitted_issues"] >= 0
            and isinstance(data["truncated"], bool)):
        return False
    truncated_reason = data["truncated_reason"]
    if data["truncated"]:
        if not isinstance(truncated_reason, str) or truncated_reason not in TRUNCATED_REASONS:
            return False
    elif truncated_reason is not None:
        return False
    if not all(_issue(i, source, target) for i in issues):
        return False
    ids = [i["id"] for i in issues]
    return len(set(ids)) == len(ids)


# -- request encoding ------------------------------------------------------


def _precedent_ok(item: Any) -> bool:
    if not _keys(item, {"surface", "choice", "rendering", "snippet", "anchor", "scope"}):
        return False
    rendering, anchor = item["rendering"], item["anchor"]
    if not (_str(item["surface"], MAX_PRECEDENT_SURFACE_SCALARS) and item["surface"]
            and item["choice"] in PRECEDENT_CHOICES and item["scope"] == "occurrence"
            and _str(item["snippet"], MAX_PRECEDENT_SNIPPET_SCALARS)):
        return False
    if (item["choice"] == "not_a_term") != (rendering is None):
        return False
    if rendering is not None and not (
        _str(rendering, MAX_PRECEDENT_RENDERING_SCALARS) and rendering
    ):
        return False
    return (_keys(anchor, {"segment_index", "source_start", "source_end"})
            and all(_is_int(anchor[k]) and anchor[k] >= 0 for k in anchor)
            and anchor["source_start"] < anchor["source_end"])


def _dump(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_request(
    source: str,
    target: str,
    direction: str,
    *,
    explain_language: str = "zh",
    before: Mapping[str, str] | None = None,
    precedents: Sequence[Mapping[str, Any]] = (),
) -> bytes:
    """The exact request body bytes. Precedents are given nearest-first.

    Over the 32 KiB budget it drops the farthest precedents one at a time, then
    keeps only the tail of each ``before`` side (halving) until it fits.
    """
    if not _str(source, MAX_SIDE_SCALARS) or not _str(target, MAX_SIDE_SCALARS):
        raise ValueError("source/target must be strings of at most 2000 scalars")
    if direction not in DIRECTIONS or explain_language not in EXPLAIN_LANGUAGES:
        raise ValueError("unsupported direction or explain_language")
    if before is not None and not (
        _keys(before, {"source", "target"})
        and _str(before["source"], MAX_CONTEXT_SCALARS)
        and _str(before["target"], MAX_CONTEXT_SCALARS)
    ):
        raise ValueError("before must be {source, target}, each at most 400 scalars")
    kept = list(precedents)
    if len(kept) > MAX_PRECEDENTS or not all(_precedent_ok(p) for p in kept):
        raise ValueError("invalid precedents")

    def body(prec: list[Any], keep: int | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": source, "target": target, "direction": direction,
            "review_version": REVIEW_VERSION, "explain_language": explain_language,
        }
        if before is not None:
            if keep is None:
                out["context"] = {"before": {"source": before["source"], "target": before["target"]}}
            else:
                out["context"] = {"before": {
                    side: before[side][len(before[side]) - min(keep, len(before[side])):]
                    for side in ("source", "target")
                }}
        out["precedents"] = prec
        return out

    data = _dump(body(kept, None))
    while len(data) > REQUEST_BYTE_LIMIT and kept:
        kept.pop()
        data = _dump(body(kept, None))
    if len(data) > REQUEST_BYTE_LIMIT and before is not None:
        keep = max(len(before["source"]), len(before["target"]))
        while len(data) > REQUEST_BYTE_LIMIT and keep > 0:
            keep //= 2
            data = _dump(body(kept, keep))
    if len(data) > REQUEST_BYTE_LIMIT:
        raise ValueError("request cannot be made to fit the byte limit")
    return data


# -- stale-span guard ------------------------------------------------------


class StaleSpan(ValueError):
    """A span no longer describes the text it would be applied to."""


def apply_span(text: str, span: Mapping[str, Any], replacement: str) -> str:
    """Replace ``text[start:end]`` only if it is still exactly ``span["text"]``."""
    try:
        start, end, expected = span["start"], span["end"], span["text"]
    except (KeyError, TypeError):
        raise StaleSpan("span is malformed") from None
    if not (_is_int(start) and _is_int(end) and isinstance(expected, str)
            and isinstance(replacement, str)):
        raise StaleSpan("span is malformed")
    if not 0 <= start < end <= len(text):
        raise StaleSpan("span is outside the text")
    if text[start:end] != expected:
        raise StaleSpan("span text no longer matches the document")
    return text[:start] + replacement + text[end:]


# -- document context ------------------------------------------------------


def context_before(prev_source_core: str, prev_target_core: str) -> dict[str, str] | None:
    """The previous segment's tails (at most 400 scalars each), or None."""
    if not prev_source_core and not prev_target_core:
        return None
    return {
        "source": prev_source_core[-MAX_CONTEXT_SCALARS:],
        "target": prev_target_core[-MAX_CONTEXT_SCALARS:],
    }


def select_precedents(
    candidates: Sequence[tuple[int, Mapping[str, Any]]],
    current_source_core: str,
    current_start: int,
    limit: int = MAX_PRECEDENTS,
) -> list[dict[str, Any]]:
    """Pick at most ``limit`` precedents, best first.

    ``candidates`` are ``(position, precedent)`` pairs, ``position`` being the
    full-document end offset of the decided span. Only precedents ending at or
    before ``current_start`` qualify; those whose surface occurs in the current
    segment come first, then the nearest (largest position) first.
    """
    eligible = [(pos, p) for pos, p in candidates if pos <= current_start]
    eligible.sort(key=lambda c: (0 if c[1]["surface"] in current_source_core else 1, -c[0]))
    return [dict(p) for _, p in eligible[:limit]]


# -- re-review merge -------------------------------------------------------


def reconcile(
    decisions: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    source: str,
    target: str,
) -> list[dict[str, Any]]:
    """One state per current issue (full-document coordinates), in order.

    ``pending``: no usable history, or an earlier accept/adjust that the review
    still reports. ``ignored_carried``: an earlier ignore whose whole text and
    spans are unchanged and that is unambiguous. ``needs_reconfirm``: an earlier
    ignore that cannot be carried. When several decisions share a fingerprint
    the latest one in ``decisions`` is reported as ``previous``.
    """
    source_revision, target_revision = text_revision(source), text_revision(target)
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["fingerprint"]] = counts.get(issue["fingerprint"], 0) + 1
    states: list[dict[str, Any]] = []
    for issue in issues:
        candidates = [d for d in decisions if d["fingerprint"] == issue["fingerprint"]]
        applied = [d for d in candidates if d["decision"] in ("accepted", "adjusted")]
        if applied:
            states.append({"state": "pending", "previous": applied[-1], "previously_applied": True})
            continue
        if not candidates:
            states.append({"state": "pending", "previous": None, "previously_applied": False})
            continue
        carried = None
        if counts[issue["fingerprint"]] == 1:
            for d in candidates:
                if (d["source_revision"] == source_revision
                        and d["target_revision"] == target_revision
                        and d["source_span"] == issue["source_span"]
                        and d["target_span"] == issue["target_span"]):
                    carried = d
        if carried is not None:
            states.append({"state": "ignored_carried", "previous": carried, "previously_applied": False})
        else:
            states.append({"state": "needs_reconfirm", "previous": candidates[-1], "previously_applied": False})
    return states
