"""Pure-Python manuscript document state primitives (no desktop toolkit, no HTTP)."""

from __future__ import annotations

from .alignment_state import AlignmentState, PairingSnapshot
from .edit_store import DocumentEditStore

__all__ = [
    "AlignmentState",
    "DocumentEditStore",
    "PairingSnapshot",
]
