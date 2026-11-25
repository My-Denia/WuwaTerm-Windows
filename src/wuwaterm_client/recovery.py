"""Atomic local crash-recovery draft storage for the Windows desktop workbench.

Saves and reloads an in-progress manuscript workfile in the existing v1-v5
JSON representation wrapped in a ``wuwaterm-recovery-v1`` envelope under
``%APPDATA%/WuwaTerm/recovery/draft.json`` (or an injected path).

The envelope may also carry the session list of alignments edits invalidated
(``alignment_invalidations``). It is session state, never part of the
manuscript: the workfile inside the envelope holds only the kept alignments.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from wuwaterm_client import config, manuscript
from wuwaterm_client.document_edits import MAX_SESSION_INVALIDATIONS, valid_invalidation

RECOVERY_FORMAT = "wuwaterm-recovery-v1"
RECOVERY_RELATIVE_PATH = Path("recovery") / "draft.json"
_MAX_ENVELOPE_BYTES = manuscript.MAX_V3_BYTES * 2 + 65536


@dataclass(frozen=True, slots=True)
class RecoveryState:
    status: Literal["missing", "valid", "corrupt"]
    payload: dict[str, Any] | None = None
    saved_at: str | None = None
    source_path: str | None = None
    dropped_conflicting_choices: int = 0
    error: str | None = None
    alignment_invalidations: tuple[dict[str, Any], ...] = ()


def default_recovery_path(base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else config.app_data_dir()
    return root / RECOVERY_RELATIVE_PATH


def save_recovery(
    path: Path,
    payload: Mapping[str, Any],
    *,
    source_path: Path | str | None = None,
    alignment_invalidations: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Atomically write ``payload`` (serialized as v1-v5 workfile) to ``path``,
    with the session list of invalidated alignments beside it."""
    dropped_conflicts = 0
    try:
        workfile_json = manuscript.serialize_workfile(payload)
    except manuscript.WorkfileError as exc:
        if exc.code != "workfile_duplicate_choices":
            raise
        sanitized, dropped_conflicts = manuscript.sanitize_duplicate_choices(payload)
        workfile_json = manuscript.serialize_workfile(sanitized)
    envelope = {
        "format": RECOVERY_FORMAT,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_path": None if source_path is None else str(source_path),
        "dropped_conflicting_choices": dropped_conflicts,
        "workfile_json": workfile_json,
    }
    invalidations = [dict(item) for item in alignment_invalidations if valid_invalidation(item)]
    if invalidations:
        envelope["alignment_invalidations"] = invalidations[-MAX_SESSION_INVALIDATIONS:]
    encoded = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def load_recovery(path: Path) -> RecoveryState:
    """Read ``path`` without raising on missing, truncated, or invalid files."""
    if not path.exists():
        return RecoveryState(status="missing")
    try:
        size = path.stat().st_size
        if size > _MAX_ENVELOPE_BYTES:
            return RecoveryState(status="corrupt", error="recovery file exceeds size cap")
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("format") != RECOVERY_FORMAT:
            return RecoveryState(status="corrupt", error="unrecognized recovery format")
        workfile_json = data.get("workfile_json")
        if not isinstance(workfile_json, str):
            return RecoveryState(status="corrupt", error="missing workfile payload")
        payload = manuscript.parse_workfile(workfile_json)
        saved_at = data.get("saved_at")
        source_path = data.get("source_path")
        dropped_raw = data.get("dropped_conflicting_choices", 0)
        dropped = (
            dropped_raw
            if isinstance(dropped_raw, int) and not isinstance(dropped_raw, bool) and dropped_raw >= 0
            else 0
        )
        listed = data.get("alignment_invalidations", [])
        invalidations = tuple(
            item for item in (listed if isinstance(listed, list) else []) if valid_invalidation(item)
        )[-MAX_SESSION_INVALIDATIONS:]
        return RecoveryState(
            status="valid",
            payload=payload,
            saved_at=saved_at if isinstance(saved_at, str) else None,
            source_path=source_path if isinstance(source_path, str) else None,
            dropped_conflicting_choices=dropped,
            alignment_invalidations=invalidations,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, manuscript.WorkfileError, ValueError, TypeError) as exc:
        return RecoveryState(status="corrupt", error=str(exc))


def clear_recovery(path: Path) -> None:
    """Remove ``path`` if it exists."""
    with contextlib.suppress(OSError):
        path.unlink()
