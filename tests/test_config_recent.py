"""The recent-files list in config.json: loaded defensively, saved whole."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from wuwaterm_client import recent
from wuwaterm_client.config import (
    MAX_RECENT,
    ClientConfig,
    RecentCursor,
    RecentEntry,
    RecentScroll,
    config_path,
)

URL = "https://example.invalid/api"


def _write(tmp_path: Path, **values) -> None:
    path = config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_url": URL, "request_timeout_seconds": 7.0, **values}), encoding="utf-8")


def _entry(path: str, **fields) -> dict:
    return {"path": path, "opened_at": "2026-11-10T08:00:00+00:00", **fields}


def test_an_old_four_key_file_loads_and_resaves_with_the_same_four_values(tmp_path: Path) -> None:
    _write(tmp_path, translate_timeout_seconds=30.0, appearance="dark")
    loaded = ClientConfig.load(tmp_path)
    assert loaded.recent == () and loaded.base_url == URL
    loaded.save(tmp_path)
    data = json.loads(config_path(tmp_path).read_text(encoding="utf-8"))
    assert data == {"base_url": URL, "request_timeout_seconds": 7.0, "translate_timeout_seconds": 30.0,
                    "appearance": "dark", "recent": []}


def test_recent_round_trips_and_the_config_stays_hashable(tmp_path: Path) -> None:
    entry = RecentEntry("/work/a.json", "2026-11-10T08:00:00+00:00", RecentCursor("source", 12),
                        RecentScroll(3, 4), "f" * 32, 300)
    config = ClientConfig(base_url=URL, recent=(entry,))
    config.save(tmp_path)
    data = json.loads(config_path(tmp_path).read_text(encoding="utf-8"))
    assert data["recent"] == [{"path": "/work/a.json", "opened_at": "2026-11-10T08:00:00+00:00",
                               "cursor": {"side": "source", "offset": 12}, "scroll": {"source": 3, "target": 4},
                               "selected_issue_id": "f" * 32, "panel_width": 300}]
    assert ClientConfig.load(tmp_path) == config
    hash(config)


@pytest.mark.parametrize("hostile", [
    "text", 5, {"path": "/a"}, None, [None, 3, "x", []],
    [_entry("")], [_entry("a\0b")], [_entry("x" * 5000)], [{"path": "/a"}], [{"path": "/a", "opened_at": "yesterday"}],
    [{"path": 7, "opened_at": "2026-11-10T08:00:00+00:00"}],
])
def test_a_hostile_recent_never_costs_the_other_settings(tmp_path: Path, hostile) -> None:
    _write(tmp_path, recent=hostile)
    loaded = ClientConfig.load(tmp_path)
    assert loaded.recent == () and loaded.base_url == URL and loaded.request_timeout_seconds == 7.0


def test_bad_position_fields_cost_the_position_not_the_entry(tmp_path: Path) -> None:
    _write(tmp_path, recent=[_entry("/a", cursor={"side": "middle", "offset": -1}, scroll={"source": "x", "target": 1},
                                    selected_issue_id=5, panel_width=3)])
    (entry,) = ClientConfig.load(tmp_path).recent
    assert (entry.cursor, entry.scroll, entry.selected_issue_id, entry.panel_width) == (None, RecentScroll(), None, None)


def test_non_finite_numbers_are_dropped(tmp_path: Path) -> None:
    config_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    config_path(tmp_path).write_text(
        '{"base_url": "URLX", "recent": [{"path": "/a", "opened_at": "2026-11-10T08:00:00+00:00", '
        '"cursor": {"side": "source", "offset": NaN}, "scroll": {"source": Infinity, "target": 1}, '
        '"panel_width": NaN}]}'.replace("URLX", URL), encoding="utf-8")
    loaded = ClientConfig.load(tmp_path)
    assert loaded.base_url == URL
    assert loaded.recent[0].cursor is None and loaded.recent[0].panel_width is None


def test_the_list_is_capped_and_deduplicated_keeping_the_first(tmp_path: Path) -> None:
    items = [_entry(f"/w/{n}.json") for n in range(15)]
    items.insert(1, _entry("/w/0.json", panel_width=400))
    _write(tmp_path, recent=items)
    loaded = ClientConfig.load(tmp_path)
    assert [e.path for e in loaded.recent] == [f"/w/{n}.json" for n in range(MAX_RECENT)]
    assert loaded.recent[0].panel_width is None


def test_upsert_moves_to_the_top_and_caps() -> None:
    entries = tuple(RecentEntry(f"/w/{n}.json", "2026-11-10T08:00:00+00:00") for n in range(MAX_RECENT))
    entries = recent.upsert(entries, entries[5])
    assert entries[0].path == "/w/5.json" and len(entries) == MAX_RECENT
    entries = recent.upsert(entries, RecentEntry("/w/new.json", "2026-11-10T08:00:00+00:00"))
    assert len(entries) == MAX_RECENT and "/w/9.json" not in [e.path for e in entries]


def test_asdict_matches_the_documented_schema() -> None:
    entry = RecentEntry("/a", "2026-11-10T08:00:00+00:00")
    assert dataclasses.asdict(entry) == {"path": "/a", "opened_at": "2026-11-10T08:00:00+00:00", "cursor": None,
                                         "scroll": {"source": 0, "target": 0}, "selected_issue_id": None,
                                         "panel_width": None}


def test_accepting_the_settings_dialog_preserves_the_recent_list() -> None:
    from PySide6.QtWidgets import QApplication

    from wuwaterm_client.ui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    entry = RecentEntry("/work/a.json", "2026-11-10T08:00:00+00:00", RecentCursor("target", 5))
    config = ClientConfig(base_url=URL, translate_timeout_seconds=42.0, recent=(entry,))
    result = SettingsDialog(config).result_config()
    assert result.recent == (entry,) and result.translate_timeout_seconds == 42.0
