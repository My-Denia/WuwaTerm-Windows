"""The local translation memory, format ``wuwaterm-tm-v1``.

A translation memory here is a store of CONFIRMED translations: pairs a person
accepted, or confirmed pairs imported from another machine. It exists so a
later pass over a changed manuscript can reuse what was already trusted instead
of asking for the same translation again.

Three things never happen, and this module is written so none of them can:

* A memory is never written into a ``.wuwaterm`` manuscript. Nothing here
  imports ``manuscript`` and nothing in ``manuscript`` imports this module.
* A memory never leaves the machine by itself. This module imports no HTTP,
  socket or API code; it reads and writes one local SQLite file and the files
  the person picks.
* A match never changes a translation, a review verdict or a dictionary
  result. It is a reference with a reason attached: a source that differs only
  in its numbers is never certified, and a match recorded under a different
  terms revision or game version says so.

Confirmed means confirmed. ``origin`` is an allow-list of two values -
``human_confirmed`` and ``imported_confirmed`` - and machine output, drafts and
anything else are refused by the database itself as well as by this module.

The store is SQLite so a changed manuscript does not rewrite the whole file for
every confirmed segment: an entry is one row and a lookup is an indexed read.
The journal is WAL and every commit is synchronous, so a crash part-way through
a write leaves the previous state rather than a half-written file. It lives
next to ``config.json`` in the per-user app data directory, in ``tm.sqlite3``,
and shares nothing with the device credential.

The same source text can hold several confirmed translations: one per distinct
target and one per distinct surrounding context (the hashes of the previous and
next segment). A new candidate never overwrites a different one; asking again
for the exact same candidate only records that it was used again.

An exported file carries those neighbour hashes, not the neighbours' text: a
lookup hashes the actual neighbour it is given, so the file stays portable
without copying a surrounding paragraph into it.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

TM_FORMAT: Final = "wuwaterm-tm-v1"
TM_SCHEMA_VERSION: Final = 1
TM_FILE_NAME: Final = "tm.sqlite3"

TM_DIRECTION_VALUES: Final[tuple[str, ...]] = ("en", "zh")
TM_ORIGIN_HUMAN: Final = "human_confirmed"
TM_ORIGIN_IMPORTED: Final = "imported_confirmed"
# The only origins a memory accepts. A machine translation, a saved draft or a
# rejected candidate is not a memory and has no way in.
TM_ORIGIN_VALUES: Final[tuple[str, ...]] = (TM_ORIGIN_HUMAN, TM_ORIGIN_IMPORTED)

TM_MATCH_CONTEXT_EXACT: Final = "context_exact"
TM_MATCH_EXACT: Final = "exact"
TM_MATCH_NUMERIC_VARIANT: Final = "numeric_variant"
TM_MATCH_NORMALIZED_EXACT: Final = "normalized_exact"

TM_STATUS_CONTEXT_EXACT_FRESH: Final = "context_exact_fresh"
TM_STATUS_EXACT_FRESH: Final = "exact_fresh"
TM_STATUS_STALE_OR_NUMERIC_REFERENCE: Final = "stale_or_numeric_reference"
TM_STATUS_UNMATCHED: Final = "unmatched"

TM_STALE_TERMS_REVISION: Final = "terms_revision_changed"
TM_STALE_GAME_VERSION: Final = "game_version_changed"
TM_STALE_NUMERIC: Final = "numeric_changed"

# A segment core is 1..2000 scalars in the manuscript contract; a memory stores
# the same text, so it uses the same bound rather than inventing a second one.
SOURCE_SCALARS: Final = (1, 2_000)
TARGET_SCALARS: Final = (1, 2_000)
NOTE_SCALARS: Final = (0, 160)
REVISION_SCALARS: Final = (1, 128)
MAX_TM_ENTRIES: Final = 10_000
MAX_TM_BYTES: Final = 8 * 1_048_576
MAX_LOOKUP_LIMIT: Final = 50
# Rows read per match kind before ranking; a numbered template can make every
# entry in the table a rank-3 candidate, and the useful answer is the most used
# and most recent handful, not ten thousand references.
MAX_LOOKUP_CANDIDATES: Final = 200
_MAX_USE_COUNT: Final = 2_147_483_647

TM_ERROR_CODES: Final[tuple[str, ...]] = (
    "tm_not_json",
    "tm_too_large",
    "tm_too_many_entries",
    "tm_duplicate_key",
    "tm_forbidden_key",
    "tm_nul",
    "tm_surrogate",
    "tm_non_finite",
    "tm_invalid_content",
    "tm_unconfirmed_origin",
    "tm_duplicate_entry",
    "tm_schema_mismatch",
    "tm_unreadable",
    "tm_unavailable",
    "tm_store_error",
)

TmDirection = Literal["en", "zh"]
TmOrigin = Literal["human_confirmed", "imported_confirmed"]


class TmError(ValueError):
    """A refused value, file or entry. ``detail`` names the location (for
    example ``entries[3].source_text``); it never carries file content."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# -- timestamps ---------------------------------------------------------------

_TIMESTAMP = re.compile(
    r"(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(?:\.\d{1,9})?Z"
)


def _days_in(year: int, month: int) -> int:
    if month == 2:
        return 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def is_tm_timestamp(value: object) -> bool:
    """An RFC 3339 UTC instant: uppercase ``T`` and ``Z``, an optional fraction
    of one to nine digits, and a day that exists in its month."""
    if not isinstance(value, str):
        return False
    match = _TIMESTAMP.fullmatch(value)
    # ``\d`` would also accept non-ASCII digits; the stored form is ASCII only.
    if match is None or not value.isascii():
        return False
    return int(match.group(3)) <= _days_in(int(match.group(1)), int(match.group(2)))


def tm_now(now: datetime.datetime | None = None) -> str:
    """The current UTC instant to the second, the form every timestamp takes."""
    moment = now if now is not None else datetime.datetime.now(datetime.UTC)
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- the local file -----------------------------------------------------------


def default_tm_path(base_dir: Path | None = None) -> Path:
    """``%APPDATA%/WuwaTerm/tm.sqlite3`` beside ``config.json``; never a place the credential lives."""
    if base_dir is None:
        # Imported here so the engine loads without the HTTP client the
        # configuration module pulls in.
        from .config import app_data_dir

        base_dir = app_data_dir()
    return base_dir / TM_FILE_NAME


# -- normalisation, hashing and the numeric mask ------------------------------


def source_hash(text: str) -> str:
    """The SHA-256 of the exact source text, as lowercase hexadecimal."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_source(text: str) -> str:
    """Line endings, tabs and runs of spaces collapsed to single spaces, without
    leading or trailing whitespace. This is the lookup key for rank 4."""
    return " ".join(text.split())


_NUMBERS = re.compile(r"[0-9]+")


def numeric_mask(text: str) -> str:
    """Every run of ASCII digits replaced by one marker, for comparing two
    sources that differ ONLY in their numbers."""
    return _NUMBERS.sub("\u0000", text)


# -- strict JSON reader -------------------------------------------------------

_FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})


def _finite_float(literal: str) -> float:
    number = float(literal)
    if math.isinf(number) or math.isnan(number):
        raise TmError("tm_non_finite", "number out of range")
    return number


def _read_json(text: str) -> object:
    """RFC 8259 JSON with the refusals a memory file needs, each with its code.

    ``json.loads`` parses in one pass, so the refusal order between two
    independent faults is the library's; every fault on its own still gets its
    own code, which is what the export is checked against.
    """

    def pairs(items: Sequence[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in _FORBIDDEN_KEYS:
                raise TmError("tm_forbidden_key", key[:40])
            if "\u0000" in key:
                raise TmError("tm_nul", "key")
            if _has_lone_surrogate(key):
                raise TmError("tm_surrogate", "key")
            if key in result:
                raise TmError("tm_duplicate_key", key[:40])
            result[key] = value
        return result

    def constant(word: str) -> float:
        raise TmError("tm_non_finite", word)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=_finite_float)
    except TmError:
        raise
    except json.JSONDecodeError as exc:
        raise TmError("tm_not_json", f"character {exc.pos} of {len(text)}") from None
    except RecursionError:
        raise TmError("tm_invalid_content", "nesting deeper than a translation memory") from None


# -- validation ---------------------------------------------------------------

_TOP_KEYS = ("format", "schema_version", "exported_at", "entries")
_ENTRY_KEYS = (
    "direction",
    "source_text",
    "target_text",
    "prev_source_hash",
    "next_source_hash",
    "origin",
    "terms_db_revision",
    "game_version",
    "note",
    "use_count",
    "created_at",
    "updated_at",
)
_HASH = re.compile(r"[0-9a-f]{64}")


def _invalid(path: str, why: str) -> TmError:
    return TmError("tm_invalid_content", f"{path}: {why}")


def _exact_keys(value: Mapping[str, object], keys: Sequence[str], path: str) -> None:
    for key in value:
        if key not in keys:
            raise _invalid(f"{path}.{key}", "unknown key")
    for key in keys:
        if key not in value:
            raise _invalid(f"{path}.{key}", "missing key")


def _has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _bounded_string(value: object, path: str, bounds: tuple[int, int]) -> str:
    if not isinstance(value, str):
        raise _invalid(path, "expected a string")
    if "\u0000" in value:
        raise TmError("tm_nul", path)
    if _has_lone_surrogate(value):
        raise TmError("tm_surrogate", path)
    # Python counts Unicode scalars directly, as the rest of the client does.
    count = len(value)
    low, high = bounds
    if count < low or count > high:
        raise _invalid(path, f"expected {low}..{high} characters, got {count}")
    return value


def _optional_string(value: object, path: str, bounds: tuple[int, int]) -> str | None:
    return None if value is None else _bounded_string(value, path, bounds)


def _hash_or_none(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise _invalid(path, "expected a lowercase SHA-256 digest or null")
    return value


def _direction(value: object, path: str) -> TmDirection:
    if not isinstance(value, str) or value not in TM_DIRECTION_VALUES:
        raise _invalid(path, 'expected "en" or "zh"')
    return value  # type: ignore[return-value]


def _origin(value: object, path: str) -> TmOrigin:
    if not isinstance(value, str) or value not in TM_ORIGIN_VALUES:
        raise TmError("tm_unconfirmed_origin", path)
    return value  # type: ignore[return-value]


def _instant(value: object, path: str) -> str:
    if not is_tm_timestamp(value):
        raise _invalid(path, "expected an RFC 3339 UTC timestamp")
    return value  # type: ignore[return-value]


def _use_count(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_USE_COUNT:
        raise _invalid(path, f"expected an integer in 0..{_MAX_USE_COUNT}")
    return value


@dataclass(frozen=True)
class TmJsonEntry:
    """One entry read from a ``wuwaterm-tm-v1`` file, already checked."""

    direction: TmDirection
    source_text: str
    target_text: str
    prev_source_hash: str | None
    next_source_hash: str | None
    origin: TmOrigin
    terms_db_revision: str | None
    game_version: str | None
    note: str
    use_count: int
    created_at: str
    updated_at: str


def _validated_json_entry(value: object, index: int) -> TmJsonEntry:
    path = f"entries[{index}]"
    if not isinstance(value, dict):
        raise _invalid(path, "expected an object")
    _exact_keys(value, _ENTRY_KEYS, path)
    source_text = _bounded_string(value["source_text"], f"{path}.source_text", SOURCE_SCALARS)
    target_text = _bounded_string(value["target_text"], f"{path}.target_text", TARGET_SCALARS)
    if not normalize_source(source_text) or not normalize_source(target_text):
        raise _invalid(path, "expected non-blank source and target text")
    return TmJsonEntry(
        direction=_direction(value["direction"], f"{path}.direction"),
        source_text=source_text,
        target_text=target_text,
        prev_source_hash=_hash_or_none(value["prev_source_hash"], f"{path}.prev_source_hash"),
        next_source_hash=_hash_or_none(value["next_source_hash"], f"{path}.next_source_hash"),
        origin=_origin(value["origin"], f"{path}.origin"),
        terms_db_revision=_optional_string(value["terms_db_revision"], f"{path}.terms_db_revision",
                                           REVISION_SCALARS),
        game_version=_optional_string(value["game_version"], f"{path}.game_version", REVISION_SCALARS),
        note=_bounded_string(value["note"], f"{path}.note", NOTE_SCALARS),
        use_count=_use_count(value["use_count"], f"{path}.use_count"),
        created_at=_instant(value["created_at"], f"{path}.created_at"),
        updated_at=_instant(value["updated_at"], f"{path}.updated_at"),
    )


def validate_tm_export(value: object) -> tuple[str, list[TmJsonEntry]]:
    """Checks a whole exported file and returns its ``exported_at`` and entries;
    anything else raises ``TmError``."""
    if not isinstance(value, dict):
        raise _invalid("$", "expected an object")
    _exact_keys(value, _TOP_KEYS, "$")
    if value["format"] != TM_FORMAT or not isinstance(value["format"], str):
        raise _invalid("$.format", f'expected "{TM_FORMAT}"')
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != TM_SCHEMA_VERSION:
        raise _invalid("$.schema_version", f"expected {TM_SCHEMA_VERSION}")
    exported_at = _instant(value["exported_at"], "$.exported_at")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise _invalid("$.entries", "expected an array")
    if len(entries) > MAX_TM_ENTRIES:
        raise TmError("tm_too_many_entries", f"{len(entries)} entries")
    seen: set[tuple[str, str, str, str, str]] = set()
    clean: list[TmJsonEntry] = []
    for index, item in enumerate(entries):
        entry = _validated_json_entry(item, index)
        identity = (
            entry.direction,
            source_hash(entry.source_text),
            entry.prev_source_hash or "",
            entry.next_source_hash or "",
            entry.target_text,
        )
        if identity in seen:
            raise _invalid(f"entries[{index}]", "duplicate candidate")
        seen.add(identity)
        clean.append(entry)
    return exported_at, clean


def parse_tm_json_bytes(data: bytes) -> list[TmJsonEntry]:
    """Reads an exported memory from the file's exact bytes. The 8 MiB limit
    applies to these bytes, before anything is decoded."""
    if len(data) > MAX_TM_BYTES:
        raise TmError("tm_too_large", f"{len(data)} bytes")
    try:
        # A byte order mark stays as U+FEFF and ``json.loads`` refuses it: the format is plain UTF-8.
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TmError("tm_not_json", "not valid UTF-8") from None
    if "\u0000" in text:
        raise TmError("tm_nul", "NUL byte")
    _, entries = validate_tm_export(_read_json(text))
    return entries


def read_tm_file_bytes(path: Path) -> bytes:
    """The file's bytes, at most one byte past the limit, so an oversized file
    is refused by ``parse_tm_json_bytes`` without being loaded whole."""
    with Path(path).open("rb") as handle:
        return handle.read(MAX_TM_BYTES + 1)


def write_tm_file_atomically(path: Path, data: bytes) -> None:
    """Writes ``data`` through a temporary file in the same directory, flushed
    and fsynced before one ``os.replace``; a failure leaves the previous file."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            handle = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def serialize_tm_export(exported_at: str, entries: Iterable[TmJsonEntry]) -> bytes:
    """The canonical file: key order as in ``_ENTRY_KEYS``, two-space indent, LF,
    one trailing newline, UTF-8 without a byte order mark."""
    clean = list(entries)
    if len(clean) > MAX_TM_ENTRIES:
        raise TmError("tm_too_many_entries", f"{len(clean)} entries")
    text = (
        json.dumps(
            {
                "format": TM_FORMAT,
                "schema_version": TM_SCHEMA_VERSION,
                "exported_at": exported_at,
                "entries": [
                    {
                        "direction": entry.direction,
                        "source_text": entry.source_text,
                        "target_text": entry.target_text,
                        "prev_source_hash": entry.prev_source_hash,
                        "next_source_hash": entry.next_source_hash,
                        "origin": entry.origin,
                        "terms_db_revision": entry.terms_db_revision,
                        "game_version": entry.game_version,
                        "note": entry.note,
                        "use_count": entry.use_count,
                        "created_at": entry.created_at,
                        "updated_at": entry.updated_at,
                    }
                    for entry in clean
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    data = text.encode("utf-8")
    if len(data) > MAX_TM_BYTES:
        raise TmError("tm_too_large", f"{len(data)} bytes")
    return data


# -- the records a lookup returns ---------------------------------------------


@dataclass(frozen=True)
class TmEntry:
    id: int
    direction: TmDirection
    source_text: str
    source_norm: str
    source_masked: str
    source_hash: str
    target_text: str
    prev_source_hash: str | None
    next_source_hash: str | None
    origin: TmOrigin
    terms_db_revision: str | None
    game_version: str | None
    note: str
    use_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TmMatch:
    entry: TmEntry
    match_kind: str
    stale_reasons: tuple[str, ...]
    numeric_changed: bool
    needs_recheck: bool


@dataclass(frozen=True)
class TmSegmentClassification:
    index: int
    source: str
    status: str
    match: TmMatch | None


_RANK = {
    TM_MATCH_CONTEXT_EXACT: 0,
    TM_MATCH_EXACT: 1,
    TM_MATCH_NUMERIC_VARIANT: 2,
    TM_MATCH_NORMALIZED_EXACT: 3,
}

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS tm_schema (
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
)
"""
_ENTRIES_TABLE = """
CREATE TABLE IF NOT EXISTS tm_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  direction TEXT NOT NULL CHECK (direction IN ('en', 'zh')),
  source_text TEXT NOT NULL,
  source_norm TEXT NOT NULL,
  source_masked TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  target_text TEXT NOT NULL,
  prev_source_hash TEXT,
  next_source_hash TEXT,
  origin TEXT NOT NULL CHECK (origin IN ('human_confirmed', 'imported_confirmed')),
  terms_db_revision TEXT,
  game_version TEXT,
  note TEXT NOT NULL DEFAULT '',
  use_count INTEGER NOT NULL DEFAULT 1 CHECK (use_count >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
"""
# SQLite compares NULLs as distinct in a UNIQUE index, so two context-free
# candidates would not collide. The empty string stands in for "no context".
_CANDIDATE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS tm_entries_candidate ON tm_entries (
  direction,
  source_hash,
  COALESCE(prev_source_hash, ''),
  COALESCE(next_source_hash, ''),
  target_text
)
"""
_NORM_INDEX = "CREATE INDEX IF NOT EXISTS tm_entries_norm ON tm_entries (direction, source_norm)"
# Rank 3 compares sources with their numbers masked; the mask is derived from
# ``source_text``, never exported, and indexed so a lookup is not a table scan.
_MASK_INDEX = "CREATE INDEX IF NOT EXISTS tm_entries_masked ON tm_entries (direction, source_masked)"


def _entry_from_row(row: sqlite3.Row) -> TmEntry:
    return TmEntry(
        id=int(row["id"]),
        direction=row["direction"],
        source_text=row["source_text"],
        source_norm=row["source_norm"],
        source_masked=row["source_masked"],
        source_hash=row["source_hash"],
        target_text=row["target_text"],
        prev_source_hash=row["prev_source_hash"],
        next_source_hash=row["next_source_hash"],
        origin=row["origin"],
        terms_db_revision=row["terms_db_revision"],
        game_version=row["game_version"],
        note=row["note"],
        use_count=int(row["use_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _draft(
    source_text: str,
    target_text: str,
    direction: str,
    prev_source: str | None,
    next_source: str | None,
    origin: str,
    terms_db_revision: str | None,
    game_version: str | None,
    note: str,
) -> dict[str, Any]:
    """The checked column values for one entry, with derived keys computed."""
    source = _bounded_string(source_text, "source_text", SOURCE_SCALARS)
    target = _bounded_string(target_text, "target_text", TARGET_SCALARS)
    if not normalize_source(source):
        raise _invalid("source_text", "expected non-blank text")
    if not normalize_source(target):
        raise _invalid("target_text", "expected non-blank text")
    return {
        "direction": _direction(direction, "direction"),
        "source_text": source,
        "source_norm": normalize_source(source),
        "source_masked": numeric_mask(source),
        "source_hash": source_hash(source),
        "target_text": target,
        "prev_source_hash": _context_hash(prev_source, "prev_source"),
        "next_source_hash": _context_hash(next_source, "next_source"),
        "origin": _origin(origin, "origin"),
        "terms_db_revision": _optional_string(terms_db_revision, "terms_db_revision", REVISION_SCALARS),
        "game_version": _optional_string(game_version, "game_version", REVISION_SCALARS),
        "note": _bounded_string(note, "note", NOTE_SCALARS),
    }


def _context_hash(value: str | None, path: str) -> str | None:
    if value is None:
        return None
    text = _bounded_string(value, path, (0, SOURCE_SCALARS[1]))
    # An empty neighbour is no context, not the hash of an empty segment.
    return None if not text.strip() else source_hash(text)


class TranslationMemory:
    """Confirmed translations in one local SQLite file.

    Every mutation is one transaction; a refused value or a failed write leaves
    the previous state. Reads open a short-lived connection each time, so the
    file can be copied or reopened while the application runs.
    """

    def __init__(self, path: Path | None = None, *, clock: Callable[[], str] = tm_now) -> None:
        self.path = Path(path) if path is not None else default_tm_path()
        self._clock = clock
        # A refused or unreadable file is kept as it is: ``error`` says why and
        # every read is empty until the person replaces the file or clears it.
        self.error: TmError | OSError | None = None
        if self.path.exists():
            try:
                if not self._verify_existing():
                    # An empty file (a crash before the first write) has no
                    # schema yet; create it now rather than fail every query.
                    connection = self._write_connection()
                    connection.close()
            except (TmError, OSError) as exc:
                self.error = exc

    @property
    def usable(self) -> bool:
        return self.error is None

    def _require_usable(self) -> None:
        if self.error is not None:
            raise self.error

    # -- connections -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _verify_existing(self) -> bool:
        """Refuses a file that is not this format instead of writing into it.

        Returns False for an empty file, which has no schema to verify yet.
        """
        try:
            connection = self._connect()
        except sqlite3.Error as exc:
            raise TmError("tm_unreadable", type(exc).__name__) from None
        try:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not names:
                return False
            if "tm_schema" not in names or "tm_entries" not in names:
                raise TmError("tm_schema_mismatch", "not a translation memory")
            meta = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM tm_schema")}
            if meta.get("format") != TM_FORMAT:
                raise TmError("tm_schema_mismatch", "format")
            if meta.get("schema_version") != str(TM_SCHEMA_VERSION):
                raise TmError("tm_schema_mismatch", f"schema version {meta.get('schema_version')!r}")
            return True
        except sqlite3.Error as exc:
            raise TmError("tm_unreadable", type(exc).__name__) from None
        finally:
            connection.close()

    def _write_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise TmError("tm_unavailable", "the file system does not support WAL")
            connection.execute("PRAGMA synchronous = FULL")
            self._ensure_schema(connection)
        except sqlite3.Error as exc:
            connection.close()
            raise TmError("tm_store_error", type(exc).__name__) from None
        except BaseException:
            connection.close()
            raise
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if names:
            if "tm_schema" not in names or "tm_entries" not in names:
                raise TmError("tm_schema_mismatch", "not a translation memory")
            meta = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM tm_schema")}
            if meta.get("format") != TM_FORMAT:
                raise TmError("tm_schema_mismatch", "format")
            if meta.get("schema_version") != str(TM_SCHEMA_VERSION):
                raise TmError("tm_schema_mismatch", f"schema version {meta.get('schema_version')!r}")
            with connection:
                connection.execute(_CANDIDATE_INDEX)
                connection.execute(_NORM_INDEX)
                connection.execute(_MASK_INDEX)
            return
        with connection:
            connection.execute(_SCHEMA_TABLE)
            connection.execute(_ENTRIES_TABLE)
            connection.execute(_CANDIDATE_INDEX)
            connection.execute(_NORM_INDEX)
            connection.execute(_MASK_INDEX)
            connection.execute("INSERT INTO tm_schema (key, value) VALUES (?, ?)", ("format", TM_FORMAT))
            connection.execute("INSERT INTO tm_schema (key, value) VALUES (?, ?)",
                               ("schema_version", str(TM_SCHEMA_VERSION)))

    # -- reading -----------------------------------------------------------

    def _rows(self, sql: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
        if self.error is not None or not self.path.exists():
            return []
        try:
            connection = self._connect()
        except sqlite3.Error as exc:
            raise TmError("tm_unreadable", type(exc).__name__) from None
        try:
            return connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise TmError("tm_store_error", type(exc).__name__) from None
        finally:
            connection.close()

    def __len__(self) -> int:
        rows = self._rows("SELECT COUNT(*) FROM tm_entries")
        return int(rows[0][0]) if rows else 0

    def entry(self, entry_id: int) -> TmEntry | None:
        rows = self._rows("SELECT * FROM tm_entries WHERE id = ?", (int(entry_id),))
        return _entry_from_row(rows[0]) if rows else None

    def entries(
        self,
        *,
        query: str | None = None,
        direction: str | None = None,
        limit: int | None = None,
    ) -> list[TmEntry]:
        """Entries for the manager, newest first, filtered by text and direction.

        ``query`` is a case-insensitive substring of the source or the target,
        compared in Python because SQLite's ``lower()`` only folds ASCII.
        """
        if direction is not None:
            _direction(direction, "direction")
        needle = None
        if query is not None:
            text = _bounded_string(query, "query", (0, 400))
            needle = text.strip().casefold() or None
        if limit is not None:
            _bounded_limit(limit)
        sql = "SELECT * FROM tm_entries"
        parameters: tuple[object, ...] = ()
        if direction is not None:
            sql += " WHERE direction = ?"
            parameters = (direction,)
        sql += " ORDER BY updated_at DESC, id DESC"
        found = []
        for row in self._rows(sql, parameters):
            entry = _entry_from_row(row)
            if needle is not None and needle not in entry.source_text.casefold() \
                    and needle not in entry.target_text.casefold():
                continue
            found.append(entry)
        return found if limit is None else found[:limit]

    # -- writing -----------------------------------------------------------

    def _stored(self, connection: sqlite3.Connection, entry_id: int) -> TmEntry:
        row = connection.execute("SELECT * FROM tm_entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:  # pragma: no cover - the id came from this transaction
            raise KeyError(entry_id)
        return _entry_from_row(row)

    def add(
        self,
        source_text: str,
        target_text: str,
        direction: str,
        *,
        prev_source: str | None = None,
        next_source: str | None = None,
        origin: str = TM_ORIGIN_HUMAN,
        terms_db_revision: str | None = None,
        game_version: str | None = None,
        note: str = "",
    ) -> TmEntry:
        """Records one confirmed candidate, or counts a repeat use of it.

        A different target for the same source, or the same target under a
        different surrounding context, is a separate candidate and is kept.
        """
        values = _draft(source_text, target_text, direction, prev_source, next_source, origin,
                        terms_db_revision, game_version, note)
        self._require_usable()
        now = self._clock()
        connection = self._write_connection()
        try:
            with _sqlite_guard(), connection:
                row = connection.execute(
                    "SELECT * FROM tm_entries WHERE direction = ? AND source_hash = ?"
                    " AND COALESCE(prev_source_hash, '') = ? AND COALESCE(next_source_hash, '') = ?"
                    " AND target_text = ?",
                    (values["direction"], values["source_hash"], values["prev_source_hash"] or "",
                     values["next_source_hash"] or "", values["target_text"]),
                ).fetchone()
                if row is not None:
                    # A repeat use refreshes what the caller knows and keeps what
                    # it does not: an omitted revision, note or human origin is
                    # not erased by a pass that did not have them.
                    existing = _entry_from_row(row)
                    connection.execute(
                        "UPDATE tm_entries SET origin = ?, terms_db_revision = ?, game_version = ?,"
                        " note = ?, use_count = use_count + 1, updated_at = ? WHERE id = ?",
                        (
                            TM_ORIGIN_HUMAN if TM_ORIGIN_HUMAN in (existing.origin, values["origin"])
                            else TM_ORIGIN_IMPORTED,
                            values["terms_db_revision"] if values["terms_db_revision"] is not None
                            else existing.terms_db_revision,
                            values["game_version"] if values["game_version"] is not None
                            else existing.game_version,
                            values["note"] or existing.note,
                            now,
                            existing.id,
                        ),
                    )
                    entry_id = existing.id
                else:
                    count = int(connection.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0])
                    if count >= MAX_TM_ENTRIES:
                        raise TmError("tm_too_many_entries", f"{count} entries")
                    cursor = connection.execute(
                        "INSERT INTO tm_entries (direction, source_text, source_norm, source_masked, source_hash,"
                        " target_text, prev_source_hash, next_source_hash, origin, terms_db_revision, game_version,"
                        " note, use_count, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                        (values["direction"], values["source_text"], values["source_norm"], values["source_masked"],
                         values["source_hash"], values["target_text"], values["prev_source_hash"],
                         values["next_source_hash"], values["origin"], values["terms_db_revision"],
                         values["game_version"], values["note"], now, now),
                    )
                    entry_id = int(cursor.lastrowid or 0)
                stored = self._stored(connection, entry_id)
            return stored
        finally:
            connection.close()

    def update(
        self,
        entry_id: int,
        source_text: str,
        target_text: str,
        direction: str,
        *,
        prev_source: str | None = None,
        next_source: str | None = None,
        origin: str = TM_ORIGIN_HUMAN,
        terms_db_revision: str | None = None,
        game_version: str | None = None,
        note: str = "",
    ) -> TmEntry:
        """Rewrites an entry's text and provenance, keeping its use count and
        creation time. Editing one candidate into another that exists is refused."""
        values = _draft(source_text, target_text, direction, prev_source, next_source, origin,
                        terms_db_revision, game_version, note)
        self._require_usable()
        entry_id = int(entry_id)
        connection = self._write_connection()
        try:
            with _sqlite_guard(), connection:
                if connection.execute("SELECT id FROM tm_entries WHERE id = ?", (entry_id,)).fetchone() is None:
                    raise KeyError(entry_id)
                clash = connection.execute(
                    "SELECT id FROM tm_entries WHERE direction = ? AND source_hash = ?"
                    " AND COALESCE(prev_source_hash, '') = ? AND COALESCE(next_source_hash, '') = ?"
                    " AND target_text = ? AND id <> ?",
                    (values["direction"], values["source_hash"], values["prev_source_hash"] or "",
                     values["next_source_hash"] or "", values["target_text"], entry_id),
                ).fetchone()
                if clash is not None:
                    raise TmError("tm_duplicate_entry", f"entries[{entry_id}]")
                connection.execute(
                    "UPDATE tm_entries SET direction = ?, source_text = ?, source_norm = ?, source_masked = ?,"
                    " source_hash = ?, target_text = ?, prev_source_hash = ?, next_source_hash = ?, origin = ?,"
                    " terms_db_revision = ?, game_version = ?, note = ?, updated_at = ? WHERE id = ?",
                    (values["direction"], values["source_text"], values["source_norm"], values["source_masked"],
                     values["source_hash"], values["target_text"], values["prev_source_hash"],
                     values["next_source_hash"], values["origin"], values["terms_db_revision"],
                     values["game_version"], values["note"], self._clock(), entry_id),
                )
                stored = self._stored(connection, entry_id)
            return stored
        finally:
            connection.close()

    def remove(self, entry_id: int) -> None:
        self._require_usable()
        entry_id = int(entry_id)
        connection = self._write_connection()
        try:
            with _sqlite_guard(), connection:
                cursor = connection.execute("DELETE FROM tm_entries WHERE id = ?", (entry_id,))
                if cursor.rowcount == 0:
                    raise KeyError(entry_id)
        finally:
            connection.close()

    def clear(self) -> int:
        """Deletes every entry and returns how many were deleted."""
        self._require_usable()
        connection = self._write_connection()
        try:
            with _sqlite_guard(), connection:
                count = int(connection.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0])
                connection.execute("DELETE FROM tm_entries")
            return count
        finally:
            connection.close()

    # -- lookup ------------------------------------------------------------

    def lookup_segment(
        self,
        source: str,
        direction: str,
        *,
        prev_source: str | None = None,
        next_source: str | None = None,
        current_terms_db_revision: str | None = None,
        current_game_version: str | None = None,
        limit: int = 5,
    ) -> list[TmMatch]:
        """Confirmed candidates for ``source``, best kind first.

        Rank 1 is the same text in the same surrounding context, rank 2 the same
        text anywhere, rank 3 a source that differs only in its numbers (a
        reference, never certified), rank 4 the same text once whitespace and
        line endings are collapsed. Every match carries the reasons it may not
        still hold; ``limit`` caps the list after ranking.
        """
        text = _bounded_string(source, "source", SOURCE_SCALARS)
        if not normalize_source(text):
            raise _invalid("source", "expected non-blank source text")
        direction = _direction(direction, "direction")
        prev_hash = _context_hash(prev_source, "prev_source")
        next_hash = _context_hash(next_source, "next_source")
        terms = _optional_string(current_terms_db_revision, "current_terms_db_revision", REVISION_SCALARS)
        game = _optional_string(current_game_version, "current_game_version", REVISION_SCALARS)
        limit = _bounded_limit(limit)
        exact_hash = source_hash(text)
        norm = normalize_source(text)
        masked = numeric_mask(text)
        # Three indexed reads rather than one OR: SQLite's planner otherwise
        # takes only ``direction`` from an index and scans every row of it.
        row_limit = min(MAX_LOOKUP_CANDIDATES, limit * 8 + 8)
        candidates = self._rows(
            "SELECT * FROM tm_entries WHERE direction = ? AND source_hash = ? LIMIT ?",
            (direction, exact_hash, row_limit),
        )
        candidates += self._rows(
            "SELECT * FROM tm_entries WHERE direction = ? AND source_norm = ? AND source_hash <> ? LIMIT ?",
            (direction, norm, exact_hash, row_limit),
        )
        candidates += self._rows(
            "SELECT * FROM tm_entries WHERE direction = ? AND source_masked = ?"
            " AND source_hash <> ? AND source_norm <> ?"
            " ORDER BY use_count DESC, updated_at DESC, id DESC LIMIT ?",
            (direction, masked, exact_hash, norm, row_limit),
        )
        best: list[TmMatch] = []
        for row in candidates:
            entry = _entry_from_row(row)
            if entry.source_hash == exact_hash:
                kind = _exact_kind(entry, prev_hash, next_hash)
            elif entry.source_norm == norm:
                kind = TM_MATCH_NORMALIZED_EXACT
            elif entry.source_masked == masked:
                kind = TM_MATCH_NUMERIC_VARIANT
            else:  # pragma: no cover - every candidate came from one of the three keys
                continue
            best.append(self._match(entry, kind, terms, game))
        ordered = best
        ordered.sort(key=lambda match: match.entry.id)
        ordered.sort(key=lambda match: match.entry.updated_at, reverse=True)
        ordered.sort(key=lambda match: match.entry.use_count, reverse=True)
        ordered.sort(key=lambda match: _RANK[match.match_kind])
        return ordered[:limit]

    def classify_segments_for_tm(
        self,
        segments: Iterable[str | Mapping[str, object]],
        direction: str,
        *,
        current_terms_db_revision: str | None = None,
        current_game_version: str | None = None,
    ) -> list[TmSegmentClassification]:
        """Classifies each segment of a revised manuscript against the memory.

        A plain sequence of strings uses its neighbours as the context, which is
        what a manuscript pass wants. A mapping may instead name
        ``prev_source`` and ``next_source`` explicitly (``None`` clears the
        context). Statuses:

        * ``context_exact_fresh`` - the same text with the same neighbours,
          recorded under the current terms revision and game version;
        * ``exact_fresh`` - the same text, but a different or unknown context;
        * ``stale_or_numeric_reference`` - a reference only: the numbers changed,
          or the entry was recorded under another revision, or the text matches
          only after normalising whitespace;
        * ``unmatched`` - nothing to reuse.
        """
        direction = _direction(direction, "direction")
        terms = _optional_string(current_terms_db_revision, "current_terms_db_revision", REVISION_SCALARS)
        game = _optional_string(current_game_version, "current_game_version", REVISION_SCALARS)
        sources: list[str] = []
        explicit: list[tuple[object, object]] = []
        for index, item in enumerate(segments):
            if isinstance(item, str):
                raw_source: object = item
                previous, following = _UNSPECIFIED, _UNSPECIFIED
            elif isinstance(item, Mapping):
                raw_source = item["source"] if "source" in item else item.get("source_text")
                previous = item["prev_source"] if "prev_source" in item else _UNSPECIFIED
                following = item["next_source"] if "next_source" in item else _UNSPECIFIED
                for path, value in ((f"segments[{index}].prev_source", previous),
                                    (f"segments[{index}].next_source", following)):
                    if value is _UNSPECIFIED or value is None:
                        continue
                    _bounded_string(value, path, (0, SOURCE_SCALARS[1]))
            else:
                raise _invalid(f"segments[{index}]", "expected a string or an object")
            source = _bounded_string(raw_source, f"segments[{index}].source", SOURCE_SCALARS)
            if not normalize_source(source):
                raise _invalid(f"segments[{index}].source", "expected non-blank text")
            sources.append(source)
            explicit.append((previous, following))
        results: list[TmSegmentClassification] = []
        for index, source in enumerate(sources):
            previous, following = explicit[index]
            if previous is _UNSPECIFIED:
                previous = sources[index - 1] if index > 0 else None
            if following is _UNSPECIFIED:
                following = sources[index + 1] if index + 1 < len(sources) else None
            matches = self.lookup_segment(
                source,
                direction,
                prev_source=previous if isinstance(previous, str) else None,
                next_source=following if isinstance(following, str) else None,
                current_terms_db_revision=terms,
                current_game_version=game,
                limit=1,
            )
            best = matches[0] if matches else None
            if best is None:
                status = TM_STATUS_UNMATCHED
            elif best.match_kind == TM_MATCH_CONTEXT_EXACT and not best.stale_reasons:
                status = TM_STATUS_CONTEXT_EXACT_FRESH
            elif best.match_kind == TM_MATCH_EXACT and not best.stale_reasons:
                status = TM_STATUS_EXACT_FRESH
            else:
                status = TM_STATUS_STALE_OR_NUMERIC_REFERENCE
            results.append(TmSegmentClassification(index=index, source=source, status=status, match=best))
        return results

    def _match(self, entry: TmEntry, kind: str, terms: str | None, game: str | None) -> TmMatch:
        reasons: list[str] = []
        if terms is not None and entry.terms_db_revision != terms:
            reasons.append(TM_STALE_TERMS_REVISION)
        if game is not None and entry.game_version != game:
            reasons.append(TM_STALE_GAME_VERSION)
        numeric = kind == TM_MATCH_NUMERIC_VARIANT
        if numeric:
            reasons.append(TM_STALE_NUMERIC)
        return TmMatch(
            entry=entry,
            match_kind=kind,
            stale_reasons=tuple(reasons),
            numeric_changed=numeric,
            needs_recheck=bool(reasons),
        )

    # -- portable files ----------------------------------------------------

    def export_tm_json(self) -> bytes:
        """The whole memory as one canonical ``wuwaterm-tm-v1`` file."""
        self._require_usable()
        rows = self._rows(
            "SELECT * FROM tm_entries ORDER BY direction, source_hash,"
            " COALESCE(prev_source_hash, ''), COALESCE(next_source_hash, ''), target_text"
        )
        entries = [
            TmJsonEntry(
                direction=entry.direction,
                source_text=entry.source_text,
                target_text=entry.target_text,
                prev_source_hash=entry.prev_source_hash,
                next_source_hash=entry.next_source_hash,
                origin=entry.origin,
                terms_db_revision=entry.terms_db_revision,
                game_version=entry.game_version,
                note=entry.note,
                use_count=entry.use_count,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
            )
            for entry in (_entry_from_row(row) for row in rows)
        ]
        return serialize_tm_export(self._clock(), entries)

    def import_tm_json_bytes(self, data: bytes, *, replace: bool = False) -> int:
        """Merges a checked ``wuwaterm-tm-v1`` file into the memory (or replaces
        it) and returns how many entries the file held.

        Merging twice gives the same memory: a candidate that is already there
        keeps the larger use count, the earlier creation time and the later
        update, and a human-confirmed origin is never downgraded to imported.
        """
        entries = parse_tm_json_bytes(data)
        self._require_usable()
        connection = self._write_connection()
        try:
            with _sqlite_guard(), connection:
                if replace:
                    connection.execute("DELETE FROM tm_entries")
                for entry in entries:
                    identity = (entry.direction, source_hash(entry.source_text),
                                entry.prev_source_hash or "", entry.next_source_hash or "", entry.target_text)
                    row = connection.execute(
                        "SELECT * FROM tm_entries WHERE direction = ? AND source_hash = ?"
                        " AND COALESCE(prev_source_hash, '') = ? AND COALESCE(next_source_hash, '') = ?"
                        " AND target_text = ?",
                        identity,
                    ).fetchone()
                    if row is not None:
                        existing = _entry_from_row(row)
                        connection.execute(
                            "UPDATE tm_entries SET origin = ?, terms_db_revision = ?, game_version = ?,"
                            " note = ?, use_count = ?, created_at = ?, updated_at = ? WHERE id = ?",
                            (
                                TM_ORIGIN_HUMAN
                                if TM_ORIGIN_HUMAN in (existing.origin, entry.origin) else TM_ORIGIN_IMPORTED,
                                existing.terms_db_revision if existing.terms_db_revision is not None
                                else entry.terms_db_revision,
                                existing.game_version if existing.game_version is not None else entry.game_version,
                                existing.note or entry.note,
                                max(existing.use_count, entry.use_count),
                                _earlier(existing.created_at, entry.created_at),
                                _later(existing.updated_at, entry.updated_at),
                                existing.id,
                            ),
                        )
                        continue
                    count = int(connection.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0])
                    if count >= MAX_TM_ENTRIES:
                        raise TmError("tm_too_many_entries", f"{count} entries")
                    connection.execute(
                        "INSERT INTO tm_entries (direction, source_text, source_norm, source_masked, source_hash,"
                        " target_text, prev_source_hash, next_source_hash, origin, terms_db_revision, game_version,"
                        " note, use_count, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (entry.direction, entry.source_text, normalize_source(entry.source_text),
                         numeric_mask(entry.source_text), source_hash(entry.source_text), entry.target_text,
                         entry.prev_source_hash, entry.next_source_hash, entry.origin, entry.terms_db_revision,
                         entry.game_version, entry.note, entry.use_count, entry.created_at, entry.updated_at),
                    )
            return len(entries)
        finally:
            connection.close()


def export_tm_file(memory: TranslationMemory, path: Path) -> None:
    """Writes the canonical file to a path the person chose, atomically."""
    write_tm_file_atomically(Path(path), memory.export_tm_json())


_UNSPECIFIED: Final = object()


def _instant_key(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _earlier(first: str, second: str) -> str:
    return first if _instant_key(first) <= _instant_key(second) else second


def _later(first: str, second: str) -> str:
    return first if _instant_key(first) >= _instant_key(second) else second


def _exact_kind(entry: TmEntry, prev_hash: str | None, next_hash: str | None) -> str:
    checks = []
    if prev_hash is not None:
        checks.append(entry.prev_source_hash == prev_hash)
    if next_hash is not None:
        checks.append(entry.next_source_hash == next_hash)
    return TM_MATCH_CONTEXT_EXACT if checks and all(checks) else TM_MATCH_EXACT


def _bounded_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LOOKUP_LIMIT:
        raise _invalid("limit", f"expected an integer in 1..{MAX_LOOKUP_LIMIT}")
    return value


@contextlib.contextmanager
def _sqlite_guard():
    """Turns a SQLite failure into a code the screens can name, without a path."""
    try:
        yield
    except sqlite3.Error as exc:
        raise TmError("tm_store_error", type(exc).__name__) from None
