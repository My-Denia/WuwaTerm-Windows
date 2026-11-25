"""The local user termbase, format ``wuwaterm-termbase-v1``.

The contract is Web's ``contracts/termbase-v1/SPEC.md`` (pinned in
``contracts/sources.json``) and Core's ADR 0021. This module is the Windows
reader, writer and reference matcher, plus the one file the desktop keeps.

What a termbase is: a list of terms one person keeps for themself. A match is
shown to that person as their own reference. Three things never happen, and
this module is written so none of them can:

* A termbase is never written into a ``.wuwaterm`` manuscript. Nothing here
  imports ``manuscript`` and nothing in ``manuscript`` imports this module.
* A termbase never leaves the machine. This module imports no HTTP, socket or
  API code; it reads and writes one local file and takes text as an argument.
* A match never changes an official dictionary result, a review verdict or a
  translation, and never locks a term. Matching is a pure function of the
  entries and the text.

The file lives next to ``config.json`` in the per-user app data directory and
shares nothing with the device credential: no import of ``credentials`` or
``keyring``, no token in any structure here.

The reader is hand-written rather than built on ``json.loads`` because the
contract fixes WHICH refusal a file gets when it breaks several rules (the
first one met while reading), and ``json.loads`` reports duplicate keys and
non-finite numbers through hooks that run in a different order.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypedDict

TERMBASE_FORMAT: Final = "wuwaterm-termbase-v1"
TERMBASE_SCHEMA_VERSION: Final = 1
TERMBASE_FILE_NAME: Final = "termbase.json"

TERMBASE_DIRECTION_VALUES: Final[tuple[str, ...]] = ("en", "zh")
TERMBASE_KIND_VALUES: Final[tuple[str, ...]] = ("community_term", "preferred_term", "do_not_translate")

MAX_TERMBASE_BYTES: Final = 1_048_576
MAX_TERMBASE_ENTRIES: Final = 2000
ID_SCALARS: Final = (1, 64)
SOURCE_SCALARS: Final = (1, 120)
TARGET_SCALARS: Final = (1, 160)
NOTE_SCALARS: Final = (0, 160)
# Nesting deeper than a termbase can legitimately be (file, entries, entry).
_MAX_DEPTH: Final = 8

TERMBASE_ERROR_CODES: Final[tuple[str, ...]] = (
    "termbase_not_json",
    "termbase_too_large",
    "termbase_too_many_entries",
    "termbase_duplicate_key",
    "termbase_forbidden_key",
    "termbase_nul",
    "termbase_surrogate",
    "termbase_non_finite",
    "termbase_invalid_content",
)

TermbaseDirection = Literal["en", "zh"]
TermbaseKind = Literal["community_term", "preferred_term", "do_not_translate"]


class TermbaseEntry(TypedDict):
    id: str
    source: str
    target: str
    direction: TermbaseDirection
    kind: TermbaseKind
    note: str
    updated_at: str


class Termbase(TypedDict):
    format: Literal["wuwaterm-termbase-v1"]
    schema_version: Literal[1]
    updated_at: str
    entries: list[TermbaseEntry]


class TermbaseError(ValueError):
    """A refused file. ``detail`` names the offending location (for example
    ``entries[3].source``); it never carries file content."""

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


def is_termbase_timestamp(value: object) -> bool:
    """An RFC 3339 UTC instant: uppercase ``T`` and ``Z``, an optional fraction
    of one to nine digits, and a day that exists in its month."""
    if not isinstance(value, str):
        return False
    match = _TIMESTAMP.fullmatch(value)
    # ``\d`` would also accept non-ASCII digits; the contract is ASCII only.
    if match is None or not value.isascii():
        return False
    return int(match.group(3)) <= _days_in(int(match.group(1)), int(match.group(2)))


def termbase_now(now: datetime.datetime | None = None) -> str:
    """The current UTC instant to the second, the form every timestamp written here takes."""
    moment = now if now is not None else datetime.datetime.now(datetime.UTC)
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- strict JSON reader -------------------------------------------------------

_FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})
_WHITESPACE = frozenset(" \t\n\r")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_LOW_SURROGATE_ESCAPE = re.compile(r"\\u([dD][c-fC-F][0-9a-fA-F]{2})")


def _not_json(text: str, at: int, what: str) -> TermbaseError:
    return TermbaseError("termbase_not_json", f"{what} at character {at} of {len(text)}")


def _read_json(text: str):
    """RFC 8259 JSON with the refusals a termbase needs, each with its own code."""
    length = len(text)
    position = 0

    def skip() -> None:
        nonlocal position
        while position < length and text[position] in _WHITESPACE:
            position += 1

    def read_string() -> str:
        nonlocal position
        position += 1  # opening quote
        out: list[str] = []
        while True:
            if position >= length:
                raise _not_json(text, position, "unterminated string")
            ch = text[position]
            if ch == '"':
                position += 1
                return "".join(out)
            if ch == "\u0000":
                raise TermbaseError("termbase_nul", f"character {position}")
            if ch < " ":
                raise _not_json(text, position, "control character in string")
            if ch != "\\":
                out.append(ch)
                position += 1
                continue
            escape = text[position + 1] if position + 1 < length else None
            if escape is None:
                raise _not_json(text, position, "unterminated escape")
            if escape != "u":
                if escape not in _ESCAPES:
                    raise _not_json(text, position, "unknown escape")
                out.append(_ESCAPES[escape])
                position += 2
                continue
            digits = text[position + 2 : position + 6]
            if not _HEX4.fullmatch(digits):
                raise _not_json(text, position, "bad unicode escape")
            unit = int(digits, 16)
            position += 6
            if unit == 0:
                raise TermbaseError("termbase_nul", f"escape at character {position - 6}")
            if 0xD800 <= unit <= 0xDBFF:
                following = _LOW_SURROGATE_ESCAPE.match(text, position)
                if following is None:
                    raise TermbaseError("termbase_surrogate", f"escape at character {position - 6}")
                low = int(following.group(1), 16)
                out.append(chr(0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)))
                position += 6
            elif 0xDC00 <= unit <= 0xDFFF:
                raise TermbaseError("termbase_surrogate", f"escape at character {position - 6}")
            else:
                out.append(chr(unit))

    def read_value(depth: int):
        nonlocal position
        if depth > _MAX_DEPTH:
            raise TermbaseError("termbase_invalid_content", f"nesting deeper than {_MAX_DEPTH}")
        skip()
        if position >= length:
            raise _not_json(text, position, "unexpected end")
        ch = text[position]
        if ch == "{":
            position += 1
            result: dict[str, object] = {}
            skip()
            if position < length and text[position] == "}":
                position += 1
                return result
            while True:
                skip()
                if position >= length or text[position] != '"':
                    raise _not_json(text, position, "expected a key")
                key = read_string()
                if key in _FORBIDDEN_KEYS:
                    raise TermbaseError("termbase_forbidden_key", key)
                if key in result:
                    raise TermbaseError("termbase_duplicate_key", key[:40])
                skip()
                if position >= length or text[position] != ":":
                    raise _not_json(text, position, "expected a colon")
                position += 1
                result[key] = read_value(depth + 1)
                skip()
                if position < length and text[position] == ",":
                    position += 1
                    continue
                if position < length and text[position] == "}":
                    position += 1
                    return result
                raise _not_json(text, position, "expected a comma or a closing brace")
        if ch == "[":
            position += 1
            items: list[object] = []
            skip()
            if position < length and text[position] == "]":
                position += 1
                return items
            while True:
                items.append(read_value(depth + 1))
                skip()
                if position < length and text[position] == ",":
                    position += 1
                    continue
                if position < length and text[position] == "]":
                    position += 1
                    return items
                raise _not_json(text, position, "expected a comma or a closing bracket")
        if ch == '"':
            return read_string()
        for word, value in (("true", True), ("false", False), ("null", None)):
            if text.startswith(word, position):
                position += len(word)
                return value
        for word in ("NaN", "Infinity", "-Infinity"):
            if text.startswith(word, position):
                raise TermbaseError("termbase_non_finite", word)
        match = _NUMBER.match(text, position)
        if match is None:
            raise _not_json(text, position, "unexpected character")
        position += match.end() - match.start()
        literal = match.group(0)
        number = float(literal)
        if number in (float("inf"), float("-inf")):
            raise TermbaseError("termbase_non_finite", "number out of range")
        return int(literal) if len(literal) < 30 and not any(c in literal for c in ".eE") else number

    value = read_value(0)
    skip()
    if position < length:
        raise _not_json(text, position, "content after the value")
    return value


# -- validation ---------------------------------------------------------------

_TOP_KEYS = ("format", "schema_version", "updated_at", "entries")
_ENTRY_KEYS = ("id", "source", "target", "direction", "kind", "note", "updated_at")


def _invalid(path: str, why: str) -> TermbaseError:
    return TermbaseError("termbase_invalid_content", f"{path}: {why}")


def _exact_keys(value: dict, keys: Sequence[str], path: str) -> None:
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
        raise TermbaseError("termbase_nul", path)
    if _has_lone_surrogate(value):
        raise TermbaseError("termbase_surrogate", path)
    # Python counts Unicode scalars directly, as the contract does.
    count = len(value)
    low, high = bounds
    if count < low or count > high:
        raise _invalid(path, f"expected {low}..{high} characters, got {count}")
    return value


def validate_entry(value: object, index: int = 0) -> TermbaseEntry:
    path = f"entries[{index}]"
    if not isinstance(value, dict):
        raise _invalid(path, "expected an object")
    _exact_keys(value, _ENTRY_KEYS, path)
    direction, kind = value["direction"], value["kind"]
    if not isinstance(direction, str) or direction not in TERMBASE_DIRECTION_VALUES:
        raise _invalid(f"{path}.direction", 'expected "en" or "zh"')
    if not isinstance(kind, str) or kind not in TERMBASE_KIND_VALUES:
        raise _invalid(f"{path}.kind", "unknown kind")
    if not is_termbase_timestamp(value["updated_at"]):
        raise _invalid(f"{path}.updated_at", "expected an RFC 3339 UTC timestamp")
    return {
        "id": _bounded_string(value["id"], f"{path}.id", ID_SCALARS),
        "source": _bounded_string(value["source"], f"{path}.source", SOURCE_SCALARS),
        "target": _bounded_string(value["target"], f"{path}.target", TARGET_SCALARS),
        "direction": direction,  # type: ignore[typeddict-item]
        "kind": kind,  # type: ignore[typeddict-item]
        "note": _bounded_string(value["note"], f"{path}.note", NOTE_SCALARS),
        "updated_at": value["updated_at"],
    }


def validate_termbase(value: object) -> Termbase:
    """Checks a termbase value against the contract and returns a clean copy;
    anything else raises ``TermbaseError``."""
    if not isinstance(value, dict):
        raise _invalid("$", "expected an object")
    _exact_keys(value, _TOP_KEYS, "$")
    if value["format"] != TERMBASE_FORMAT or not isinstance(value["format"], str):
        raise _invalid("$.format", f'expected "{TERMBASE_FORMAT}"')
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != TERMBASE_SCHEMA_VERSION:
        raise _invalid("$.schema_version", f"expected {TERMBASE_SCHEMA_VERSION}")
    if not is_termbase_timestamp(value["updated_at"]):
        raise _invalid("$.updated_at", "expected an RFC 3339 UTC timestamp")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise _invalid("$.entries", "expected an array")
    if len(entries) > MAX_TERMBASE_ENTRIES:
        raise TermbaseError("termbase_too_many_entries", f"{len(entries)} entries")
    seen: set[str] = set()
    clean: list[TermbaseEntry] = []
    for index, item in enumerate(entries):
        entry = validate_entry(item, index)
        if entry["id"] in seen:
            raise _invalid(f"entries[{index}].id", "duplicate id")
        seen.add(entry["id"])
        clean.append(entry)
    return {
        "format": TERMBASE_FORMAT,
        "schema_version": TERMBASE_SCHEMA_VERSION,
        "updated_at": value["updated_at"],
        "entries": clean,
    }


# -- reading and writing ------------------------------------------------------


def parse_termbase_bytes(data: bytes) -> Termbase:
    """Reads a termbase from the file's exact bytes. The 1 MiB limit applies to
    these bytes, before anything is decoded."""
    if len(data) > MAX_TERMBASE_BYTES:
        raise TermbaseError("termbase_too_large", f"{len(data)} bytes")
    try:
        # A byte order mark stays as U+FEFF so the reader refuses it: the format is plain UTF-8.
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TermbaseError("termbase_not_json", "not valid UTF-8") from None
    if "\u0000" in text:
        raise TermbaseError("termbase_nul", "NUL byte")
    return validate_termbase(_read_json(text))


def serialize_termbase(termbase: Termbase) -> bytes:
    """The canonical file: key order as in the contract, two-space indent, LF,
    one trailing newline, UTF-8 without a byte order mark."""
    clean = validate_termbase(termbase)
    text = (
        json.dumps(
            {
                "format": clean["format"],
                "schema_version": clean["schema_version"],
                "updated_at": clean["updated_at"],
                "entries": [
                    {key: entry[key] for key in _ENTRY_KEYS}  # type: ignore[literal-required]
                    for entry in clean["entries"]
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    data = text.encode("utf-8")
    if len(data) > MAX_TERMBASE_BYTES:
        raise TermbaseError("termbase_too_large", f"{len(data)} bytes")
    return data


def empty_termbase(now: str | None = None) -> Termbase:
    return {
        "format": TERMBASE_FORMAT,
        "schema_version": TERMBASE_SCHEMA_VERSION,
        "updated_at": now or termbase_now(),
        "entries": [],
    }


def next_entry_id(entries: Iterable[TermbaseEntry]) -> str:
    """The next free ``tb-NNN`` id for a list of entries."""
    taken = {entry["id"] for entry in entries}
    number = len(taken) + 1
    while True:
        candidate = f"tb-{number:03d}"
        if candidate not in taken:
            return candidate
        number += 1


# -- the file the desktop keeps -----------------------------------------------


def termbase_path(base_dir: Path | None = None) -> Path:
    """``%APPDATA%/WuwaTerm/termbase.json`` beside ``config.json``; never a place the credential lives."""
    if base_dir is None:
        # Imported here so the pure reader, writer and matcher load without the
        # HTTP client the configuration module pulls in.
        from .config import app_data_dir

        base_dir = app_data_dir()
    return base_dir / TERMBASE_FILE_NAME


def write_file_atomically(path: Path, data: bytes, *, make_parents: bool = False) -> None:
    """Writes ``data`` to ``path`` through a temporary file in the same directory.

    The bytes are flushed and fsynced to the device and only then replace the
    target in one ``os.replace``, so a crash or a full disk part-way through
    can never leave a truncated termbase; the previous file stays as it was
    unless the replace itself succeeds. There is no parent-directory fsync (a
    directory cannot be opened for one on Windows).
    """
    if make_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
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


class TermbaseUnavailable(RuntimeError):
    """The stored file is refused or unreadable, so it is never overwritten by a single edit."""


def _read_file_bytes(path: Path) -> bytes | None:
    """The file's bytes, at most one byte past the limit, or None when there is no file."""
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_TERMBASE_BYTES + 1)
    except FileNotFoundError:
        return None


class TermbaseStore:
    """The person's termbase: loaded from one local file, saved atomically on every change.

    A file that is refused or cannot be read is kept as it is: ``error`` says
    why, ``entries`` is empty, and an edit raises ``TermbaseUnavailable``
    until the person replaces the termbase (import) or resets it on purpose.
    """

    def __init__(self, path: Path | None = None, *, clock: Callable[[], str] = termbase_now) -> None:
        self.path = path if path is not None else termbase_path()
        self._clock = clock
        self._termbase: Termbase = empty_termbase(self._clock())
        self.error: TermbaseError | OSError | None = None
        self.reload()

    # -- reading ---------------------------------------------------------

    def reload(self) -> None:
        try:
            data = _read_file_bytes(self.path)
            if data is None:
                self._termbase, self.error = empty_termbase(self._clock()), None
                return
            self._termbase, self.error = parse_termbase_bytes(data), None
        except TermbaseError as exc:
            self._termbase, self.error = empty_termbase(self._clock()), exc
        except OSError as exc:
            self._termbase, self.error = empty_termbase(self._clock()), exc

    @property
    def entries(self) -> list[TermbaseEntry]:
        return [dict(entry) for entry in self._termbase["entries"]]  # type: ignore[misc]

    @property
    def termbase(self) -> Termbase:
        return validate_termbase(self._termbase)

    @property
    def usable(self) -> bool:
        return self.error is None

    def __len__(self) -> int:
        return len(self._termbase["entries"])

    # -- writing ---------------------------------------------------------

    def _commit(self, candidate: Termbase) -> None:
        data = serialize_termbase(candidate)
        write_file_atomically(self.path, data, make_parents=True)
        self._termbase = validate_termbase(candidate)
        self.error = None

    def _require_usable(self) -> None:
        if self.error is not None:
            raise TermbaseUnavailable(str(self.error))

    def _next(self, entries: list[TermbaseEntry]) -> Termbase:
        return {
            "format": TERMBASE_FORMAT,
            "schema_version": TERMBASE_SCHEMA_VERSION,
            "updated_at": self._clock(),
            "entries": entries,
        }

    def add(self, *, source: str, target: str, direction: str, kind: str, note: str = "") -> TermbaseEntry:
        self._require_usable()
        entries = self.entries
        entry: TermbaseEntry = {
            "id": next_entry_id(entries),
            "source": source,
            "target": target,
            "direction": direction,  # type: ignore[typeddict-item]
            "kind": kind,  # type: ignore[typeddict-item]
            "note": note,
            "updated_at": self._clock(),
        }
        entries.append(entry)
        self._commit(self._next(entries))
        return dict(entry)  # type: ignore[return-value]

    def update(self, entry_id: str, *, source: str, target: str, direction: str, kind: str, note: str = "") -> TermbaseEntry:
        self._require_usable()
        entries = self.entries
        for index, existing in enumerate(entries):
            if existing["id"] == entry_id:
                entries[index] = {
                    "id": entry_id,
                    "source": source,
                    "target": target,
                    "direction": direction,  # type: ignore[typeddict-item]
                    "kind": kind,  # type: ignore[typeddict-item]
                    "note": note,
                    "updated_at": self._clock(),
                }
                self._commit(self._next(entries))
                return dict(entries[index])  # type: ignore[return-value]
        raise KeyError(entry_id)

    def remove(self, entry_id: str) -> None:
        self._require_usable()
        entries = self.entries
        kept = [entry for entry in entries if entry["id"] != entry_id]
        if len(kept) == len(entries):
            raise KeyError(entry_id)
        self._commit(self._next(kept))

    def replace(self, termbase: Termbase) -> None:
        """Replaces the whole termbase with an already validated one (an import), or resets it."""
        self._commit(validate_termbase(termbase))

    def reset(self) -> None:
        self._commit(empty_termbase(self._clock()))


def import_termbase_file(path: Path) -> Termbase:
    """Reads a termbase file the person chose; refused whole or accepted whole."""
    data = _read_file_bytes(Path(path))
    if data is None:
        raise FileNotFoundError(str(path))
    return parse_termbase_bytes(data)


def export_termbase_file(termbase: Termbase, path: Path) -> None:
    """Writes the canonical file to a path the person chose, atomically."""
    write_file_atomically(Path(path), serialize_termbase(termbase))


# -- reference matching -------------------------------------------------------

# Longest text the matcher reads, in UTF-16 code units, as in Web; the rest of
# a very long manuscript is not scanned.
MATCH_TEXT_LIMIT: Final = 100_000
# Spans kept per entry; ``count`` still counts every occurrence.
MATCH_SPANS_PER_ENTRY: Final = 20

# Script blocks whose characters have no spaces between words, so a neighbour
# in one of them never makes a Latin surface part of a longer word. Block
# ranges stand in for the Han, Hiragana, Katakana and Hangul scripts.
_CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x1100, 0x11FF), (0x2E80, 0x2FDF), (0x3005, 0x3007), (0x3021, 0x3029), (0x3038, 0x303B),
    (0x3041, 0x3096), (0x309D, 0x309F), (0x30A1, 0x30FA), (0x30FD, 0x30FF), (0x3105, 0x312F),
    (0x3131, 0x318E), (0x31A0, 0x31BF), (0x31F0, 0x31FF), (0x3200, 0x321E), (0x3220, 0x3247),
    (0x3260, 0x327E), (0x32D0, 0x32FE), (0x3300, 0x3357), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
    (0xA960, 0xA97C), (0xAC00, 0xD7A3), (0xD7B0, 0xD7C6), (0xD7CB, 0xD7FB), (0xF900, 0xFAFF),
    (0xFF66, 0xFF9D), (0xFFA0, 0xFFDC), (0x1B000, 0x1B16F), (0x1F200, 0x1F200),
    (0x20000, 0x2FA1F), (0x30000, 0x323AF),
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _is_word(ch: str) -> bool:
    """A letter, digit or underscore that is not CJK."""
    return (ch.isalnum() or ch == "_") and not _is_cjk(ch)


@dataclass(frozen=True)
class TermbaseSpan:
    start: int
    end: int


@dataclass(frozen=True)
class TermbaseMatch:
    entry: TermbaseEntry
    count: int
    spans: tuple[TermbaseSpan, ...]


@dataclass(frozen=True)
class TermbaseMatches:
    matches: tuple[TermbaseMatch, ...]
    truncated: bool


def _utf16_cut(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to at most ``limit`` UTF-16 code units, never inside an astral character."""
    if len(text) <= limit // 2:
        return text, False
    units = 0
    for index, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:index], True
    return text, False


def match_termbase(entries: Iterable[TermbaseEntry], text: str, direction: str) -> TermbaseMatches:
    """The entries of one direction whose source surface occurs literally in ``text``.

    Case-insensitive and, where the surface begins or ends with a letter or
    digit that is not CJK, only as a whole word. Offsets are scalar offsets
    into the scanned text. This is a reading aid and nothing else: a match
    changes no translation, review verdict or dictionary lookup.
    """
    entries = list(entries)
    if not entries or not text:
        return TermbaseMatches((), False)
    scanned, truncated = _utf16_cut(text, MATCH_TEXT_LIMIT)
    # A plain substring test in C rejects the entries that do not occur at all
    # before the regular expression runs; it only applies while lower-casing
    # keeps every character one character, so it can never reject a real hit.
    lowered = scanned.lower()
    prefilter = len(lowered) == len(scanned)
    found: list[TermbaseMatch] = []
    for entry in entries:
        if entry["direction"] != direction:
            continue
        surface = entry["source"]
        if prefilter and surface.lower() not in lowered:
            continue
        check_before = _is_word(surface[0])
        check_after = _is_word(surface[-1])
        pattern = re.compile(re.escape(surface), re.IGNORECASE)
        spans: list[TermbaseSpan] = []
        count = 0
        at = 0
        while True:
            hit = pattern.search(scanned, at)
            if hit is None:
                break
            start, end = hit.start(), hit.end()
            if (check_before and start > 0 and _is_word(scanned[start - 1])) or (
                check_after and end < len(scanned) and _is_word(scanned[end])
            ):
                at = start + 1
                continue
            count += 1
            if len(spans) < MATCH_SPANS_PER_ENTRY:
                spans.append(TermbaseSpan(start, end))
            at = end
        if count:
            found.append(TermbaseMatch(entry, count, tuple(spans)))
    found.sort(key=lambda match: match.spans[0].start)
    return TermbaseMatches(tuple(found), truncated)
