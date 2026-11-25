"""Atomic local crash-recovery draft storage for the Windows desktop workbench.

Saves and reloads an in-progress manuscript workfile in the existing v1-v4
JSON representation wrapped in a ``wuwaterm-recovery-v1`` envelope under
``%APPDATA%/WuwaTerm/recovery/draft.json`` (or an injected path).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from wuwaterm_client import config, manuscript

RECOVERY_FORMAT = "wuwaterm-recovery-v1"
RECOVERY_RELATIVE_PATH = Path("recovery") / "draft.json"
_MAX_ENVELOPE_BYTES = manuscript.MAX_V3_BYTES * 2 + 65536


@dataclass(frozen=True, slots=True)
class RecoveryState:
    status: Literal["missing", "valid", "corrupt"]
    payload: dict[str, Any] | None = None
    saved_at: str | None = None
    source_path: str | None = None
    error: str | None = None


def default_recovery_path(base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else config.app_data_dir()
    return root / RECOVERY_RELATIVE_PATH


def save_recovery(
    path: Path,
    payload: Mapping[str, Any],
    *,
    source_path: Path | str | None = None,
) -> None:
    """Atomically write ``payload`` (serialized as v1-v4 workfile) to ``path``."""
    workfile_json = manuscript.serialize_workfile(payload)
    envelope = {
        "format": RECOVERY_FORMAT,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_path": None if source_path is None else str(source_path),
        "workfile_json": workfile_json,
    }
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
        return RecoveryState(
            status="valid",
            payload=payload,
            saved_at=saved_at if isinstance(saved_at, str) else None,
            source_path=source_path if isinstance(source_path, str) else None,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, manuscript.WorkfileError, ValueError, TypeError) as exc:
        return RecoveryState(status="corrupt", error=str(exc))


def clear_recovery(path: Path) -> None:
    """Remove ``path`` if it exists."""
    with contextlib.suppress(OSError):
        path.unlink()
