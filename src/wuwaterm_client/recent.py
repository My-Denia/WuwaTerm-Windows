"""The recently opened manuscripts list: pure bookkeeping, no Qt, no I/O.

Entries are stored by `config.ClientConfig.recent` and nowhere else; see the
note there on why a path never leaves that file.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable, Iterable

from .config import MAX_RECENT, RecentEntry, recent_key

# The folder name is context, not identity (the full path is the tooltip), so
# a long one is cut rather than allowed to widen the menu.
_PARENT_CHARS = 24


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def upsert(entries: Iterable[RecentEntry], entry: RecentEntry) -> tuple[RecentEntry, ...]:
    """`entry` first, any earlier entry for the same file dropped, capped."""
    key = recent_key(entry.path)
    rest = [item for item in entries if recent_key(item.path) != key]
    return tuple([entry, *rest][:MAX_RECENT])


def find(entries: Iterable[RecentEntry], path: str) -> RecentEntry | None:
    key = recent_key(path)
    return next((item for item in entries if recent_key(item.path) == key), None)


def without_missing(
    entries: Iterable[RecentEntry], exists: Callable[[str], bool]
) -> tuple[RecentEntry, ...]:
    return tuple(item for item in entries if exists(item.path))


def file_exists(path: str) -> bool:
    # Checked only when the menu is shown. A share that cannot be reached
    # counts as missing; the entry is kept and shows up again next time.
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def menu_label(path: str) -> str:
    """File name, then the parent folder (cut if long); '&' escaped for menus."""
    name = os.path.basename(path) or path
    parent = os.path.basename(os.path.dirname(path))
    if len(parent) > _PARENT_CHARS:
        parent = parent[: _PARENT_CHARS - 1] + "…"
    text = f"{name}  —  {parent}" if parent else name
    return text.replace("&", "&&")
