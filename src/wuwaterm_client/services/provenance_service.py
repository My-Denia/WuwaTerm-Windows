"""Single authoritative dictionary provenance service (pure Python).

Consolidates live ``GET /v1/meta``, ``GET /v1/dictionary``, and ``POST /v1/reviews``
dictionary provenance arbitration into one stateful service with monotonic request
sequencing. Previously, both ``ManuscriptView`` and ``DocumentRunController`` kept
parallel copies of these fields and synchronised them ad-hoc; this service is now
the sole source of truth.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .. import semantic
from .. import translation_memory as tm
from ..errors import ERROR_CANCELLED, ClientError

IDENTITY_FRESH = "fresh"
IDENTITY_UNSUPPORTED = "unsupported"
IDENTITY_FAILED = "failed"
IDENTITY_SUPERSEDED = "superseded"


class DictionaryIdentity(Protocol):
    """One ``GET /v1/dictionary`` answer, as far as provenance reads it."""

    revision: str
    source_game_version: str | None


class MetaSnapshot(Protocol):
    """One ``GET /v1/meta`` answer, as far as provenance reads it."""

    schema_version: str
    source_commit: str
    term_count: int
    source_game_version: str | None


@dataclass(frozen=True)
class MetaUpdateOutcome:
    changed: bool = False
    reset_capabilities: bool = False


@dataclass(frozen=True)
class ReviewDictionaryOutcome:
    changed: bool = False
    request_meta_refresh: bool = False


def normalized_game_version(value: object) -> str | None:
    return tm.normalize_provenance_value(value)


def dictionary_matches_meta(dictionary: Mapping[str, Any], meta: Any) -> bool:
    term_count = dictionary.get("term_count")
    if isinstance(term_count, bool) or not isinstance(term_count, (int, float)):
        return False
    return (
        dictionary.get("schema_version") == meta.schema_version
        and dictionary.get("source_commit") == meta.source_commit
        and term_count == meta.term_count
    )


def identity_matches_meta(identity: Any, meta: Any) -> bool:
    return (
        getattr(identity, "schema_version", None) == meta.schema_version
        and getattr(identity, "source_commit", None) == meta.source_commit
        and getattr(identity, "term_count", None) == meta.term_count
        and normalized_game_version(getattr(identity, "source_game_version", None))
        == normalized_game_version(getattr(meta, "source_game_version", None))
    )


class DictionaryProvenanceService:
    """Single source of truth for live served-dictionary provenance and sequencing."""

    def __init__(
        self,
        *,
        on_changed: Callable[[], None] | None = None,
        on_meta_refresh_requested: Callable[[], None] | None = None,
    ) -> None:
        self._on_changed = on_changed
        self._on_meta_refresh_requested = on_meta_refresh_requested
        self.service_meta: Any | None = None
        self.service_dictionary: DictionaryIdentity | None = None
        self.live_review_dictionary: dict[str, Any] | None = None
        self.dictionary_request_seq = 0
        self.applied_dictionary_seq = 0
        self.identity_probe_state: str | None = None

    def set_callbacks(
        self,
        *,
        on_changed: Callable[[], None] | None = None,
        on_meta_refresh_requested: Callable[[], None] | None = None,
    ) -> None:
        self._on_changed = on_changed
        self._on_meta_refresh_requested = on_meta_refresh_requested

    def _notify_changed(self) -> None:
        if self._on_changed is not None:
            self._on_changed()

    def _notify_meta_refresh(self) -> None:
        if self._on_meta_refresh_requested is not None:
            self._on_meta_refresh_requested()

    def retire_requests(self) -> None:
        """Make every pending identity answer older than this decision."""
        self.dictionary_request_seq += 1
        self.applied_dictionary_seq = self.dictionary_request_seq

    def begin_identity_request(self) -> int:
        """Reserve the next monotonic request ordinal for an identity request."""
        self.dictionary_request_seq += 1
        return self.dictionary_request_seq

    def forget_service_dictionary(self) -> bool:
        """Drop the live identity statement and retire every pending request."""
        self.retire_requests()
        if self.service_dictionary is None:
            return False
        self.service_dictionary = None
        return True

    def _drop_conflicting_review_dictionary(self, revision: str | None) -> bool:
        dictionary = self.live_review_dictionary
        if not isinstance(dictionary, dict) or revision is None:
            return False
        recorded = tm.normalize_provenance_value(dictionary.get("revision"))
        if recorded is None or recorded == revision:
            return False
        self.live_review_dictionary = None
        self._notify_meta_refresh()
        return True

    def _write_service_dictionary(self, identity: DictionaryIdentity | None) -> None:
        if identity is self.service_dictionary or identity == self.service_dictionary:
            return
        self.service_dictionary = identity
        if identity is not None:
            self._drop_conflicting_review_dictionary(getattr(identity, "revision", None))
        self._notify_changed()

    def apply_identity(self, identity: DictionaryIdentity | None, *, seq: int | None = None) -> bool:
        """Install an identity answer unless a newer request already decided it."""
        if seq is not None and seq < self.applied_dictionary_seq:
            return False
        self.applied_dictionary_seq = max(
            self.applied_dictionary_seq,
            self.dictionary_request_seq if seq is None else seq,
        )
        self._write_service_dictionary(identity)
        return True

    def set_service_dictionary(self, identity: DictionaryIdentity | None) -> None:
        self.apply_identity(identity, seq=self.begin_identity_request())

    def set_service_meta(self, meta: Any | None) -> MetaUpdateOutcome:
        """Update live ``GET /v1/meta`` and retire conflicting cached identity/review state."""
        if meta is None:
            changed = (
                self.service_meta is not None
                or self.live_review_dictionary is not None
                or self.service_dictionary is not None
            )
            self.service_meta = None
            self.live_review_dictionary = None
            changed = self.forget_service_dictionary() or changed
            if changed:
                self._notify_changed()
            return MetaUpdateOutcome(changed=changed, reset_capabilities=False)

        previous_meta = self.service_meta
        changed = previous_meta != meta
        reset_capabilities = changed
        self.service_meta = meta
        review_mismatch = (
            self.live_review_dictionary is not None
            and not dictionary_matches_meta(self.live_review_dictionary, meta)
        )
        if review_mismatch:
            self.live_review_dictionary = None
            changed = True
        identity = self.service_dictionary
        identity_mismatch = identity is not None and not identity_matches_meta(identity, meta)
        if identity_mismatch:
            changed = self.forget_service_dictionary() or changed
        elif review_mismatch or (
            previous_meta is not None
            and (
                previous_meta.schema_version != meta.schema_version
                or previous_meta.source_commit != meta.source_commit
                or previous_meta.term_count != meta.term_count
                or normalized_game_version(previous_meta.source_game_version)
                != normalized_game_version(meta.source_game_version)
            )
        ):
            self.retire_requests()
        if changed:
            self._notify_changed()
        return MetaUpdateOutcome(changed=changed, reset_capabilities=reset_capabilities)

    def clear_live_provenance(self, *, seq: int | None = None) -> bool:
        """Forget both live statements about the served dictionary unless superseded."""
        if seq is not None and seq < self.applied_dictionary_seq:
            return False
        if seq is None:
            self.retire_requests()
        else:
            self.applied_dictionary_seq = max(self.applied_dictionary_seq, seq)
        self.service_dictionary = None
        self.live_review_dictionary = None
        self._notify_changed()
        return True

    def record_live_review_dictionary(self, dictionary: Mapping[str, Any] | None) -> ReviewDictionaryOutcome:
        """Record dictionary metadata returned by a live ``POST /v1/reviews`` call."""
        if not isinstance(dictionary, Mapping):
            return ReviewDictionaryOutcome(changed=False, request_meta_refresh=False)
        recorded = copy.deepcopy(dict(dictionary))
        identity = self.service_dictionary
        if identity is not None:
            review_revision = tm.normalize_provenance_value(recorded.get("revision"))
            if review_revision is not None and review_revision != identity.revision:
                self.live_review_dictionary = None
                self.forget_service_dictionary()
                self._notify_meta_refresh()
                self._notify_changed()
                return ReviewDictionaryOutcome(changed=True, request_meta_refresh=True)
        if self.service_meta is not None and not dictionary_matches_meta(recorded, self.service_meta):
            self.live_review_dictionary = None
            self.retire_requests()
            self._notify_meta_refresh()
            self._notify_changed()
            return ReviewDictionaryOutcome(changed=True, request_meta_refresh=True)
        self.live_review_dictionary = recorded
        self._notify_changed()
        return ReviewDictionaryOutcome(changed=True, request_meta_refresh=False)

    def reset_for_endpoint_change(self) -> None:
        self.service_meta = None
        self.live_review_dictionary = None
        self.forget_service_dictionary()
        self.identity_probe_state = None
        self._notify_changed()

    def reset_for_credential_change(self) -> None:
        self.service_meta = None
        self.live_review_dictionary = None
        self.forget_service_dictionary()
        self._notify_changed()

    def terms_db_revision(self) -> str | None:
        """The verified live dictionary revision (fail-closed)."""
        identity = self.service_dictionary
        if identity is not None:
            self._drop_conflicting_review_dictionary(getattr(identity, "revision", None))
            return tm.normalize_provenance_value(getattr(identity, "revision", None))
        dictionary = self.live_review_dictionary
        if not isinstance(dictionary, dict):
            return None
        if self.service_meta is not None and not dictionary_matches_meta(dictionary, self.service_meta):
            return None
        return tm.normalize_provenance_value(dictionary.get("revision"))

    def game_version(self) -> str | None:
        """The live game version from identity, meta, or live review dictionary."""
        identity = self.service_dictionary
        if identity is not None:
            return normalized_game_version(getattr(identity, "source_game_version", None))
        if self.service_meta is not None:
            return normalized_game_version(getattr(self.service_meta, "source_game_version", None))
        dictionary = self.live_review_dictionary
        if isinstance(dictionary, dict):
            for key in ("game_version", "source_game_version"):
                version = normalized_game_version(dictionary.get(key))
                if version is not None:
                    return version
        return None

    def tm_provenance(self) -> tuple[str | None, str | None]:
        return self.terms_db_revision(), self.game_version()

    async def refresh_live_dictionary_identity(
        self,
        api_client: Any,
        *,
        is_configured: bool,
        on_capabilities: Callable[[frozenset[str]], None],
        begin_request: Callable[[], int | None] | None = None,
        apply_identity: Callable[..., bool] | None = None,
        clear_provenance: Callable[..., bool] | None = None,
    ) -> DictionaryIdentity | None:
        """Probe ``/v1/capabilities`` and ``/v1/dictionary`` with monotonic sequencing."""
        self.identity_probe_state = None
        if not is_configured:
            return None
        getter = getattr(api_client, "get_dictionary", None)
        _begin = begin_request or self.begin_identity_request
        _apply = apply_identity or self.apply_identity
        _clear = clear_provenance or self.clear_live_provenance
        seq = _begin()
        try:
            capabilities = await api_client.get_capabilities()
            on_capabilities(capabilities)
            if semantic.CAPABILITY_DICTIONARY_IDENTITY not in capabilities or not callable(getter):
                if not _apply(None, seq=seq):
                    self.identity_probe_state = IDENTITY_SUPERSEDED
                    return None
                self.identity_probe_state = IDENTITY_UNSUPPORTED
                return None
            seq = _begin()
            identity = await getter()
        except ClientError as exc:
            if exc.code == ERROR_CANCELLED:
                return None
            if not _clear(seq=seq):
                self.identity_probe_state = IDENTITY_SUPERSEDED
                return None
            self.identity_probe_state = IDENTITY_FAILED
            return None
        except Exception:  # noqa: BLE001 - fail-closed probe
            if not _clear(seq=seq):
                self.identity_probe_state = IDENTITY_SUPERSEDED
                return None
            self.identity_probe_state = IDENTITY_FAILED
            return None
        if identity is None:
            if not _apply(None, seq=seq):
                self.identity_probe_state = IDENTITY_SUPERSEDED
                return None
            self.identity_probe_state = IDENTITY_UNSUPPORTED
            return None
        if not _apply(identity, seq=seq):
            self.identity_probe_state = IDENTITY_SUPERSEDED
            return None
        self.identity_probe_state = IDENTITY_FRESH
        return identity
