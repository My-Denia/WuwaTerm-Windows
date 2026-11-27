"""Immutable pairing snapshots and paragraph-alignment state (pure Python).

Owns how manuscript source/target text and explicit alignments turn into cached
``segmentation-v1`` / ``segmentation-v1-gaps`` segment pairs, segment keys,
per-segment alignment lists, and adjacent-segment context. Holds no Qt and no
HTTP dependencies.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import segment_records as sr
from .. import semantic
from ..document_edits import (
    AlignmentRemap,
    build_target_fill_transaction,
    remap_for_edit,
    remap_for_free_edit,
    remap_for_target_fill,
    remap_splice,
)
from ..segmentation import (
    SEGMENT_LIMIT,
    SEGMENTATION_VERSION,
    STATUS_READY,
    pair_segments,
    pair_segments_with_gaps,
)

__all__ = [
    "AlignmentRemap",
    "AlignmentState",
    "PairingSnapshot",
    "build_target_fill_transaction",
    "remap_for_edit",
    "remap_for_free_edit",
    "remap_for_target_fill",
    "remap_splice",
]


@dataclass(frozen=True)
class PairingSnapshot:
    """Immutable snapshot of a manuscript's computed segment pairing."""

    key: tuple[str, str, str]
    pairs: tuple[Any, ...]
    version: str
    long_mode: bool

    def as_list(self) -> list[Any]:
        return list(self.pairs)


class AlignmentState:
    """Cached paragraph pairing and alignment queries over the active payload."""

    def __init__(self) -> None:
        self._snapshot: PairingSnapshot | None = None
        self._pairs_list: list[Any] = []

    @staticmethod
    def key_for(payload: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            payload["source"],
            payload["target"],
            repr(payload.get("alignments") or None),
        )

    @property
    def pairs_key(self) -> tuple[str, str, str] | None:
        return None if self._snapshot is None else self._snapshot.key

    @pairs_key.setter
    def pairs_key(self, value: tuple[str, str, str] | None) -> None:
        if value is None:
            self._snapshot = None
            self._pairs_list = []
        elif self._snapshot is not None:
            self._snapshot = PairingSnapshot(
                key=value,
                pairs=self._snapshot.pairs,
                version=self._snapshot.version,
                long_mode=self._snapshot.long_mode,
            )

    @property
    def pairs(self) -> list[Any]:
        return self._pairs_list

    @pairs.setter
    def pairs(self, value: Sequence[Any]) -> None:
        items = list(value)
        self._pairs_list = items
        if self._snapshot is not None:
            self._snapshot = PairingSnapshot(
                key=self._snapshot.key,
                pairs=tuple(items),
                version=self._snapshot.version,
                long_mode=self._snapshot.long_mode,
            )

    @property
    def pairs_version(self) -> str:
        return SEGMENTATION_VERSION if self._snapshot is None else self._snapshot.version

    @pairs_version.setter
    def pairs_version(self, value: str) -> None:
        if self._snapshot is not None:
            self._snapshot = PairingSnapshot(
                key=self._snapshot.key,
                pairs=self._snapshot.pairs,
                version=value,
                long_mode=self._snapshot.long_mode,
            )

    def invalidate(self) -> None:
        self._snapshot = None
        self._pairs_list = []

    def snapshot(self, payload: Mapping[str, Any]) -> PairingSnapshot:
        """Compute (or return cached) immutable ``PairingSnapshot`` for ``payload``."""
        key = self.key_for(payload)
        if self._snapshot is not None and self._snapshot.key == key:
            return self._snapshot
        source, target = payload["source"], payload["target"]
        alignments = payload.get("alignments") or None
        try:
            # Explicit alignments that leave text uncovered keep every
            # aligned region; only the gap needs a manual split (MY-496).
            pairs, version = pair_segments_with_gaps(source, target, SEGMENT_LIMIT, alignments)
        except (ValueError, TypeError, AssertionError):
            # Alignments the rule cannot use fall back to automatic pairing.
            pairs, version = pair_segments(source, target, SEGMENT_LIMIT, None), SEGMENTATION_VERSION
        self._pairs_list = list(pairs)
        self._snapshot = PairingSnapshot(
            key=key,
            pairs=tuple(self._pairs_list),
            version=version,
            long_mode=sr.long_mode(source, target),
        )
        return self._snapshot

    def cached_snapshot(self, payload: Mapping[str, Any]) -> PairingSnapshot | None:
        """Return cached snapshot if it matches ``payload``; never computes."""
        if self._snapshot is not None and self._snapshot.key == self.key_for(payload):
            return self._snapshot
        return None

    def current_pairs(self, payload: Mapping[str, Any]) -> list[Any]:
        self.snapshot(payload)
        return self._pairs_list

    def cached_pairs(self, payload: Mapping[str, Any]) -> list[Any] | None:
        return self._pairs_list if self.cached_snapshot(payload) is not None else None

    @staticmethod
    def segment_key(pair: Any, payload: Mapping[str, Any]) -> tuple[Any, ...]:
        """A pair's identity: where its cores are and what they say."""
        return sr.pair_key(pair, payload["source"], payload["target"])

    @staticmethod
    def long_now(payload: Mapping[str, Any]) -> bool:
        return sr.long_mode(payload["source"], payload["target"])

    @staticmethod
    def alignments_for(
        payload: Mapping[str, Any],
        source_core: tuple[int, int],
        target_core: tuple[int, int],
    ) -> list[dict[str, Any]] | None:
        """The owner's correspondence for one segment's term request."""
        return sr.alignment_list(
            payload.get("alignments"),
            payload["source"],
            payload["target"],
            source_core,
            target_core,
        )

    def context_for(self, payload: Mapping[str, Any], pair: Any) -> dict[str, str] | None:
        if pair.index == 0:
            return None
        previous = self.current_pairs(payload)[pair.index - 1]
        source, target = payload["source"], payload["target"]
        return semantic.context_before(
            source[previous.source.core_start:previous.source.core_end],
            target[previous.target.core_start:previous.target.core_end],
        )

    @staticmethod
    def adjacent_before_context(
        payload: Mapping[str, Any],
        pairs: Sequence[Any],
        target_pair: Any,
    ) -> tuple[str, str] | None:
        idx = list(pairs).index(target_pair)
        source, target = payload["source"], payload["target"]
        for prev in reversed(pairs[:idx]):
            if prev.status == STATUS_READY and prev.target.core_end > prev.target.core_start:
                return (
                    source[prev.source.core_start:prev.source.core_end],
                    target[prev.target.core_start:prev.target.core_end],
                )
        return None

    @staticmethod
    def records_with_input_change(payload: Mapping[str, Any], alignments: list[dict[str, Any]] | None) -> int:
        """How many segment term results a change to ``alignments`` sends back for re-check."""
        source, target = payload["source"], payload["target"]
        return sum(
            1
            for record in payload.get("segments") or ()
            if record["result"] is not None
            and sr.alignment_input(
                alignments,
                source,
                target,
                (record["source_start"], record["source_end"]),
                (record["target_start"], record["target_end"]),
            )
            != record["alignment_input"]
        )
