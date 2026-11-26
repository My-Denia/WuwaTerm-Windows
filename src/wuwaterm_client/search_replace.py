"""Pure-Python in-editor search and scoped replace for manuscript text.

No Qt, no HTTP: offsets are Unicode scalar indices matching Python ``str`` and
``document_edits`` edit transactions, so astral characters (emoji) and CRLF
sequences never shift match boundaries or undo splices.

Replacement is intentionally either:

* **Single-occurrence** (``replace_match`` / ``replace_current``), or
* **Segment-scoped** (``replace_in_segment`` / ``replace_scoped_segment``),

never a blind global homograph replace across the whole manuscript.
"""
from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .document_edits import compute_minimal_splice

SearchSide = Literal["source", "target"]
SearchScope = Literal["all", "segment"]


@dataclass(frozen=True)
class SearchMatch:
    """One match of a query in ``source`` or ``target`` using scalar offsets."""

    side: SearchSide
    start: int
    end: int
    text: str
    segment_index: int | None = None

    def span(self) -> dict[str, int | str]:
        """Span mapping accepted by ``LosslessEditor.select_raw_span``."""
        return {"start": self.start, "end": self.end, "text": self.text}


@dataclass(frozen=True)
class ReplaceResult:
    """Result of one single-occurrence or segment-scoped replacement.

    ``edit`` is ``(side, start, old_end, new_length)`` in scalar offsets so the
    caller can pass it straight to ``DocumentRunController.apply_edit`` and
    ``remap_splice``, keeping ``EditHistory`` undo/redo exact.
    """

    side: SearchSide
    text: str
    edit: tuple[SearchSide, int, int, int]
    replaced_count: int
    new_cursor_offset: int


def _validate_side(side: str) -> SearchSide:
    if side not in ("source", "target"):
        raise ValueError(f"unsupported search side: {side!r}")
    return side  # type: ignore[return-value]


def _segment_index_for(
    start: int,
    end: int,
    segment_ranges: Sequence[tuple[int, int]] | None,
    starts: list[int] | None,
) -> int | None:
    if not segment_ranges or starts is None:
        return None
    pos = bisect.bisect_right(starts, start) - 1
    if 0 <= pos < len(segment_ranges):
        seg_start, seg_end = segment_ranges[pos]
        if seg_start <= start and end <= seg_end:
            return pos
    return None


def find_matches(
    text: str,
    query: str,
    *,
    side: SearchSide = "target",
    case_sensitive: bool = False,
    segment_ranges: Sequence[tuple[int, int]] | None = None,
    scope_segment: int | None = None,
) -> list[SearchMatch]:
    """Find non-overlapping occurrences of ``query`` in ``text``.

    When ``scope_segment`` is given, only occurrences wholly inside
    ``segment_ranges[scope_segment]`` are returned. Offsets are always scalar
    indexes into the full ``text``.
    """
    side = _validate_side(side)
    if not query or not text:
        return []

    if scope_segment is not None:
        if segment_ranges is None or not 0 <= scope_segment < len(segment_ranges):
            return []
        seg_start, seg_end = segment_ranges[scope_segment]
        seg_start = max(0, min(seg_start, len(text)))
        seg_end = max(seg_start, min(seg_end, len(text)))
        region = text[seg_start:seg_end]
        base = seg_start
        starts = None
    else:
        region = text
        base = 0
        starts = [r[0] for r in segment_ranges] if segment_ranges else None

    matches: list[SearchMatch] = []
    if case_sensitive:
        step = len(query)
        pos = 0
        while True:
            found = region.find(query, pos)
            if found < 0:
                break
            start = base + found
            end = start + step
            seg_idx = (
                scope_segment
                if scope_segment is not None
                else _segment_index_for(start, end, segment_ranges, starts)
            )
            matches.append(SearchMatch(side=side, start=start, end=end, text=text[start:end], segment_index=seg_idx))
            pos = found + step
        return matches

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    for m in pattern.finditer(region):
        start = base + m.start()
        end = base + m.end()
        if end <= start:
            continue
        seg_idx = (
            scope_segment
            if scope_segment is not None
            else _segment_index_for(start, end, segment_ranges, starts)
        )
        matches.append(SearchMatch(side=side, start=start, end=end, text=text[start:end], segment_index=seg_idx))
    return matches


def next_match_index(
    matches: Sequence[SearchMatch],
    *,
    current_index: int | None = None,
    cursor_offset: int | None = None,
    forward: bool = True,
) -> int | None:
    """Choose the next or previous match index, wrapping around at the ends."""
    if not matches:
        return None
    count = len(matches)
    if current_index is not None and 0 <= current_index < count:
        return (current_index + (1 if forward else -1)) % count
    if cursor_offset is not None:
        if forward:
            for idx, match in enumerate(matches):
                if match.start >= cursor_offset:
                    return idx
            return 0
        for idx in range(count - 1, -1, -1):
            if matches[idx].start < cursor_offset:
                return idx
        return count - 1
    return 0 if forward else count - 1


def replace_match(text: str, match: SearchMatch, replacement: str) -> ReplaceResult:
    """Replace a single ``match`` in ``text`` and return its undoable splice."""
    if not (0 <= match.start <= match.end <= len(text)) or text[match.start:match.end] != match.text:
        raise ValueError("match span is stale or out of range")
    changed = text[:match.start] + replacement + text[match.end:]
    return ReplaceResult(
        side=match.side,
        text=changed,
        edit=(match.side, match.start, match.end, len(replacement)),
        replaced_count=1,
        new_cursor_offset=match.start + len(replacement),
    )


def replace_in_segment(
    text: str,
    query: str,
    replacement: str,
    *,
    side: SearchSide = "target",
    segment_range: tuple[int, int],
    case_sensitive: bool = False,
    segment_index: int | None = None,
) -> ReplaceResult | None:
    """Replace all occurrences of ``query`` inside one explicit ``segment_range``.

    Never touches text outside ``segment_range``. Returns ``None`` if there are
    no matches or if the replacement leaves ``text`` unchanged.
    """
    side = _validate_side(side)
    seg_start, seg_end = segment_range
    seg_start = max(0, min(seg_start, len(text)))
    seg_end = max(seg_start, min(seg_end, len(text)))
    matches = find_matches(
        text,
        query,
        side=side,
        case_sensitive=case_sensitive,
        segment_ranges=[(seg_start, seg_end)],
        scope_segment=0,
    )
    if not matches:
        return None

    parts: list[str] = []
    cursor = 0
    delta = 0
    last_end = seg_start
    for match in matches:
        parts.append(text[cursor:match.start])
        parts.append(replacement)
        cursor = match.end
        delta += len(replacement) - (match.end - match.start)
        last_end = match.end + delta
    parts.append(text[cursor:])
    changed = "".join(parts)
    splice = compute_minimal_splice(text, changed)
    if splice is None:
        return None
    return ReplaceResult(
        side=side,
        text=changed,
        edit=(side, splice[0], splice[1], splice[2]),
        replaced_count=len(matches),
        new_cursor_offset=last_end,
    )


class SearchReplaceController:
    """Stateful pure-Python search/replace coordinator for one workbench."""

    def __init__(self) -> None:
        self.query: str = ""
        self.replacement: str = ""
        self.side: SearchSide = "target"
        self.case_sensitive: bool = False
        self.scope: SearchScope = "all"
        self.matches: tuple[SearchMatch, ...] = ()
        self.current_index: int | None = None

    def clear(self) -> None:
        self.matches = ()
        self.current_index = None

    def current_match(self) -> SearchMatch | None:
        if self.current_index is None or not (0 <= self.current_index < len(self.matches)):
            return None
        return self.matches[self.current_index]

    def refresh(
        self,
        text: str,
        *,
        segment_ranges: Sequence[tuple[int, int]] | None = None,
        scope_segment: int | None = None,
        cursor_offset: int | None = None,
        preserve_current: bool = True,
    ) -> tuple[SearchMatch, ...]:
        """Recompute matches for the current settings and select an active match."""
        previous = self.current_match() if preserve_current else None
        active_scope = scope_segment if self.scope == "segment" else None
        if self.scope == "segment" and active_scope is None:
            found: list[SearchMatch] = []
        else:
            found = find_matches(
                text,
                self.query,
                side=self.side,
                case_sensitive=self.case_sensitive,
                segment_ranges=segment_ranges,
                scope_segment=active_scope,
            )
        self.matches = tuple(found)
        if not self.matches:
            self.current_index = None
            return self.matches

        if previous is not None and previous.side == self.side:
            for idx, match in enumerate(self.matches):
                if match.start == previous.start and match.end == previous.end:
                    self.current_index = idx
                    return self.matches
            anchor = previous.start
        else:
            anchor = cursor_offset

        self.current_index = next_match_index(self.matches, cursor_offset=anchor, forward=True)
        return self.matches

    def step(self, *, forward: bool = True, cursor_offset: int | None = None) -> SearchMatch | None:
        """Advance to the next or previous match and return it."""
        if not self.matches:
            self.current_index = None
            return None
        self.current_index = next_match_index(
            self.matches,
            current_index=self.current_index,
            cursor_offset=cursor_offset,
            forward=forward,
        )
        return self.current_match()

    def replace_current(self, text: str, replacement: str | None = None) -> ReplaceResult | None:
        """Replace only the currently selected match."""
        match = self.current_match()
        if match is None:
            return None
        value = self.replacement if replacement is None else replacement
        return replace_match(text, match, value)

    def replace_scoped_segment(
        self,
        text: str,
        segment_range: tuple[int, int],
        *,
        segment_index: int | None = None,
        replacement: str | None = None,
    ) -> ReplaceResult | None:
        """Replace all matches inside ``segment_range`` only."""
        value = self.replacement if replacement is None else replacement
        return replace_in_segment(
            text,
            self.query,
            value,
            side=self.side,
            segment_range=segment_range,
            case_sensitive=self.case_sensitive,
            segment_index=segment_index,
        )
