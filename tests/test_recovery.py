"""Tests for local atomic crash-recovery draft persistence."""

from __future__ import annotations

import json
from pathlib import Path

from wuwaterm_client import manuscript, recovery


def _sample_v4_payload() -> dict:
    payload = manuscript.empty_workfile()
    source = "甲。\n\n乙。"
    target = "Alpha.\n\nBeta."
    payload.update(
        source=source,
        target=target,
        direction="en",
        alignments=[
            {"source": {"start": 0, "end": 2, "text": "甲。"}, "target": {"start": 0, "end": 6, "text": "Alpha."}},
            {"source": {"start": 4, "end": 6, "text": "乙。"}, "target": None},
        ],
    )
    return payload


def test_default_recovery_path_uses_appdata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert recovery.default_recovery_path() == tmp_path / "Roaming" / "WuwaTerm" / "recovery" / "draft.json"


def test_save_load_and_clear_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "recovery" / "draft.json"
    assert recovery.load_recovery(path).status == "missing"

    payload = _sample_v4_payload()
    recovery.save_recovery(path, payload, source_path="/tmp/example.json")
    assert path.is_file()
    assert not list(path.parent.glob("*.tmp-*"))

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format"] == recovery.RECOVERY_FORMAT
    assert raw["source_path"] == "/tmp/example.json"
    assert "wuwaterm-manuscript-v5" not in raw["workfile_json"]

    loaded = recovery.load_recovery(path)
    assert loaded.status == "valid"
    assert loaded.source_path == "/tmp/example.json"
    assert loaded.saved_at is not None
    assert loaded.payload == manuscript.parse_workfile(manuscript.serialize_workfile(payload))

    recovery.clear_recovery(path)
    recovery.clear_recovery(path)  # idempotent
    assert not path.exists()
    assert recovery.load_recovery(path).status == "missing"


def test_load_recovery_handles_corrupt_and_truncated_files(tmp_path: Path) -> None:
    path = tmp_path / "recovery" / "draft.json"
    path.parent.mkdir(parents=True)

    for bad in (
        "{not valid json",
        json.dumps({"format": "wrong-format", "workfile_json": "{}"}),
        json.dumps({"format": recovery.RECOVERY_FORMAT, "workfile_json": "{bad-workfile"}),
        b"\xff\xfe\xfd",
    ):
        if isinstance(bad, bytes):
            path.write_bytes(bad)
        else:
            path.write_text(bad, encoding="utf-8")
        state = recovery.load_recovery(path)
        assert state.status == "corrupt"
        assert state.payload is None


def test_large_v3_v4_recovery_round_trip_beyond_v1_v2_cap(tmp_path: Path) -> None:
    path = tmp_path / "recovery" / "draft.json"
    payload = _sample_v4_payload()
    large_source = "甲" * 1_200_000
    large_target = "A" * 1_200_000
    payload.update(source=large_source, target=large_target, alignments=None)
    recovery.save_recovery(path, payload)
    assert path.stat().st_size > manuscript.MAX_WORKFILE_BYTES * 2 + 65536
    loaded = recovery.load_recovery(path)
    assert loaded.status == "valid"
    assert loaded.payload is not None
    assert loaded.payload["source"] == large_source
    assert loaded.payload["target"] == large_target
