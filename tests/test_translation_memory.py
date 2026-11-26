"""The local translation memory: ``wuwaterm-tm-v1``.

The store is a real SQLite file, so these tests reopen it, read it with the
``sqlite3`` module directly, and check the guarantees the module states: only
confirmed origins enter, distinct candidates coexist, ranking names the right
kind, a changed number or revision is never certified, and an exported file is
either accepted whole or refused whole.

Pure Python: no Qt, no HTTP, no network. The manager dialog is compiled in the
same change but not imported here.
"""
from __future__ import annotations

import ast
import json
import socket
import sqlite3
from pathlib import Path

import pytest

from wuwaterm_client import translation_memory as tm

SRC = Path(tm.__file__).parent
STAMP = "2026-11-27T00:00:00Z"
LATER = "2026-11-28T00:00:00Z"
THIRD = "2026-11-29T00:00:00Z"


def _entry(index: int = 0, **over: object) -> dict:
    entry = {
        "direction": "en",
        "source_text": f"source {index}",
        "target_text": f"target {index}",
        "prev_source_hash": None,
        "next_source_hash": None,
        "origin": "human_confirmed",
        "terms_db_revision": None,
        "game_version": None,
        "note": "",
        "use_count": 1,
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    entry.update(over)
    return entry


def _file(entries: list[dict] | None = None, **over: object) -> dict:
    payload = {
        "format": "wuwaterm-tm-v1",
        "schema_version": 1,
        "exported_at": STAMP,
        "entries": entries or [],
    }
    payload.update(over)
    return payload


def _bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def store(tmp_path):
    return tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)


# -- the file ----------------------------------------------------------------


def test_the_default_path_is_the_roaming_app_data_folder_and_a_sqlite_file(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = tm.default_tm_path()
    assert path == tmp_path / "WuwaTerm" / "tm.sqlite3"
    assert path.name == tm.TM_FILE_NAME and path.suffix == ".sqlite3"


def test_entries_persist_across_reopen_and_the_file_is_a_wal_sqlite_database(tmp_path):
    path = tmp_path / "tm.sqlite3"
    store = tm.TranslationMemory(path, clock=lambda: STAMP)
    store.add("攻击力提高 20%，持续 10 秒", "ATK +20% for 10s", "en",
              terms_db_revision="r14", game_version="3.0", note="确认过")
    reopened = tm.TranslationMemory(path, clock=lambda: STAMP)
    assert reopened.usable and len(reopened) == 1
    (match,) = reopened.lookup_segment("攻击力提高 20%，持续 10 秒", "en")
    assert match.entry.target_text == "ATK +20% for 10s"
    assert match.entry.note == "确认过"

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"tm_schema", "tm_entries"} <= tables
        meta = dict(connection.execute("SELECT key, value FROM tm_schema"))
        assert meta == {"format": "wuwaterm-tm-v1", "schema_version": "1"}
        assert connection.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0] == 1
    finally:
        connection.close()


def test_only_confirmed_origins_are_stored_at_the_module_and_the_database(store):
    imported = store.add("主C", "main DPS", "en", origin="imported_confirmed")
    assert imported.origin == "imported_confirmed"
    for origin in ("machine_translated", "draft", "model", "", "HUMAN_CONFIRMED"):
        with pytest.raises(tm.TmError) as caught:
            store.add("主C", "another", "en", origin=origin)
        assert caught.value.code == "tm_unconfirmed_origin"
    assert len(store) == 1

    # The schema's own CHECK is the second half of the same rule: a hand-edited
    # row cannot smuggle a machine origin past the module.
    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tm_entries (direction, source_text, source_norm, source_hash, target_text,"
                " origin, note, use_count, created_at, updated_at)"
                " VALUES ('en', 'x', 'x', ?, 'y', 'machine_translated', '', 1, ?, ?)",
                (tm.source_hash("x"), STAMP, STAMP),
            )
    finally:
        connection.close()


def test_the_same_source_keeps_distinct_targets_and_distinct_contexts(store):
    store.add("攻击", "ATK", "en")
    store.add("攻击", "Attack", "en")
    store.add("攻击", "ATK", "en", prev_source="前置段落")
    store.add("攻击", "ATK", "zh")
    assert len(store) == 4

    matches = store.lookup_segment("攻击", "en", prev_source="前置段落")
    assert [(m.match_kind, m.entry.target_text) for m in matches] == [
        ("context_exact", "ATK"),
        ("exact", "ATK"),
        ("exact", "Attack"),
    ]
    plain = store.lookup_segment("攻击", "en")
    assert {m.match_kind for m in plain} == {"exact"}
    assert {m.entry.target_text for m in plain} == {"ATK", "Attack"}


def test_repeating_the_same_candidate_counts_a_use_and_updates_provenance(tmp_path):
    moments = [STAMP, LATER, THIRD]
    store = tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: moments.pop(0))
    first = store.add("主C", "main DPS", "en")
    assert (first.use_count, first.created_at, first.updated_at) == (1, STAMP, STAMP)
    assert first.terms_db_revision is None
    again = store.add("主C", "main DPS", "en", terms_db_revision="r15", game_version="3.1", note="复核")
    assert len(store) == 1
    assert again.id == first.id
    assert (again.use_count, again.created_at, again.updated_at) == (2, STAMP, LATER)
    assert (again.terms_db_revision, again.game_version, again.note) == ("r15", "3.1", "复核")
    # A pass that does not know the revision or the note keeps what was recorded.
    third = store.add("主C", "main DPS", "en", origin="imported_confirmed")
    assert (third.use_count, third.updated_at) == (3, THIRD)
    assert (third.origin, third.terms_db_revision, third.game_version, third.note) == (
        "human_confirmed", "r15", "3.1", "复核"
    )


def test_a_different_context_is_a_different_candidate_even_with_the_same_target(store):
    first = store.add("开", "open", "en")
    second = store.add("开", "open", "en", next_source="宝箱")
    assert first.id != second.id
    assert second.next_source_hash == tm.source_hash("宝箱")
    # A neighbour made only of whitespace is no context at all.
    blank = store.add("开", "open2", "en", prev_source=" \n ")
    assert blank.prev_source_hash is None


def test_update_keeps_identity_and_refuses_a_collision(store):
    first = store.add("A", "一", "en", note="旧")
    second = store.add("A", "二", "en")
    with pytest.raises(tm.TmError) as caught:
        store.update(first.id, "A", "二", "en")
    assert caught.value.code == "tm_duplicate_entry"
    updated = store.update(first.id, "A", "壹", "en", terms_db_revision="r15", note="新")
    assert updated.id == first.id and updated.use_count == first.use_count
    assert updated.target_text == "壹" and updated.note == "新"
    assert store.entry(second.id).target_text == "二"
    with pytest.raises(KeyError):
        store.update(9999, "x", "y", "en")


def test_removing_and_clearing(store):
    first = store.add("A", "一", "en")
    store.add("B", "二", "en")
    with pytest.raises(KeyError):
        store.remove(9999)
    store.remove(first.id)
    assert len(store) == 1 and store.entry(first.id) is None
    assert store.clear() == 1
    assert len(store) == 0 and store.lookup_segment("A", "en") == []
    with pytest.raises(KeyError):
        store.remove(first.id)


def test_entries_filters_by_text_and_direction_and_caps_the_list(store):
    store.add("Alpha 攻击", "Alpha attack", "en")
    store.add("Beta 防御", "Beta defense", "en")
    store.add("Gamma 治疗", "Gamma heal", "zh")
    assert len(store.entries()) == 3
    assert [e.source_text for e in store.entries(query="alpha")] == ["Alpha 攻击"]
    assert [e.source_text for e in store.entries(query="defense")] == ["Beta 防御"]
    assert {e.direction for e in store.entries(direction="zh")} == {"zh"}
    assert store.entries(query="a", direction="en", limit=1).__len__() == 1
    assert store.entries(query="   ") != []
    with pytest.raises(tm.TmError):
        store.entries(direction="fr")
    with pytest.raises(tm.TmError):
        store.entries(limit=0)


# -- ranking -----------------------------------------------------------------


def test_context_exact_ranks_above_exact(store):
    store.add("挑战成功", "Challenge complete", "en", prev_source="准备", next_source="奖励")
    store.add("挑战成功", "Challenge succeeded", "en")
    matches = store.lookup_segment("挑战成功", "en", prev_source="准备", next_source="奖励")
    assert [m.match_kind for m in matches] == ["context_exact", "exact"]
    assert matches[0].entry.target_text == "Challenge complete"
    # A context that does not match the stored one is not a context match.
    assert [m.match_kind for m in store.lookup_segment("挑战成功", "en", prev_source="其他")] == [
        "exact", "exact"
    ]
    # One provided neighbour matching is enough for rank 1.
    assert matches[0].match_kind == "context_exact"
    assert store.lookup_segment("挑战成功", "en", next_source="奖励")[0].match_kind == "context_exact"


def test_normalized_exact_matches_only_whitespace_and_line_endings(store):
    exact = store.add("攻击力提高 20% 持续 10 秒", "ATK +20% for 10s", "en")
    normalized = store.add("攻击力提高 20%\r\n持续 10 秒", "ATK +20% for 10s (line)", "en")
    matches = store.lookup_segment("攻击力提高 20% 持续 10 秒", "en")
    assert [(m.match_kind, m.entry.id) for m in matches] == [
        ("exact", exact.id), ("normalized_exact", normalized.id)
    ]
    assert matches[1].numeric_changed is False
    assert store.lookup_segment("攻击力提高 20% 持续 10 秒！", "en") == []


def test_numeric_variants_are_references_never_certified(store):
    entry = store.add("攻击力提高 20%，持续 10 秒", "ATK +20% for 10s", "en",
                      terms_db_revision="r14", game_version="3.0")
    (match,) = store.lookup_segment("攻击力提高 25%，持续 12 秒", "en")
    assert match.match_kind == "numeric_variant"
    assert match.entry.id == entry.id
    assert match.entry.target_text == "ATK +20% for 10s"
    assert match.numeric_changed is True and match.needs_recheck is True
    assert match.stale_reasons == ("numeric_changed",)
    # Text that differs outside a number is not a numeric variant.
    assert store.lookup_segment("攻击力提高 25%，持续 12 秒！", "en") == []
    # Full-width digits are not the ASCII numeric tokens this rank compares.
    assert store.lookup_segment("攻击力提高 ２５％，持续 １０ 秒", "en") == []


def test_stale_flags_name_the_reason_and_only_when_a_current_value_is_known(store):
    store.add("挑战成功", "Challenge complete", "en", terms_db_revision="r14", game_version="3.0")
    (fresh,) = store.lookup_segment("挑战成功", "en", current_terms_db_revision="r14",
                                    current_game_version="3.0")
    assert fresh.stale_reasons == () and fresh.needs_recheck is False
    (terms,) = store.lookup_segment("挑战成功", "en", current_terms_db_revision="r15")
    assert terms.stale_reasons == ("terms_revision_changed",) and terms.needs_recheck is True
    (game,) = store.lookup_segment("挑战成功", "en", current_game_version="3.1")
    assert game.stale_reasons == ("game_version_changed",) and game.needs_recheck is True
    # No current value means "not known", not "everything is stale".
    (unknown,) = store.lookup_segment("挑战成功", "en")
    assert unknown.stale_reasons == ()
    # A stored entry that recorded no revision cannot be fresh against a known one.
    store.add("没有记录", "no record", "en")
    (no_record,) = store.lookup_segment("没有记录", "en", current_terms_db_revision="r14")
    assert no_record.stale_reasons == ("terms_revision_changed",)


def test_a_stale_entry_is_ordered_by_kind_then_use_count_then_recency(store):
    older = store.add("同一句", "old", "en")
    newer = store.add("同一句", "new", "en")
    store.add("同一句", "new", "en")  # counts a use of the newer candidate
    matches = store.lookup_segment("同一句", "en")
    assert [m.entry.id for m in matches] == [newer.id, older.id]
    assert store.lookup_segment("同一句", "en", limit=1)[0].entry.id == newer.id
    with pytest.raises(tm.TmError):
        store.lookup_segment("同一句", "en", limit=0)
    with pytest.raises(tm.TmError):
        store.lookup_segment("同一句", "en", limit=tm.MAX_LOOKUP_LIMIT + 1)


# -- incremental re-translation ----------------------------------------------


def test_incremental_classification_reuses_fresh_context_matches(store):
    segments = ["攻击力提高 20%，持续 10 秒", "另一段"]
    store.add(segments[0], "ATK +20% for 10s", "en", next_source=segments[1],
              terms_db_revision="r14", game_version="3.0")
    store.add(segments[1], "another segment", "en", prev_source=segments[0],
              terms_db_revision="r14", game_version="3.0")
    classified = store.classify_segments_for_tm(
        segments, "en", current_terms_db_revision="r14", current_game_version="3.0")
    assert [c.status for c in classified] == ["context_exact_fresh", "context_exact_fresh"]
    assert all(c.index == index and c.source == source
               for index, (c, source) in enumerate(zip(classified, segments)))


def test_incremental_classification_flags_changed_numbers_and_stale_revisions(store):
    segments = ["攻击力提高 20%，持续 10 秒", "另一段"]
    store.add(segments[0], "ATK +20% for 10s", "en", next_source=segments[1],
              terms_db_revision="r14", game_version="3.0")
    store.add(segments[1], "another segment", "en", prev_source=segments[0],
              terms_db_revision="r14", game_version="3.0")
    changed = store.classify_segments_for_tm(
        ["攻击力提高 25%，持续 12 秒", "另一段"], "en",
        current_terms_db_revision="r14", current_game_version="3.0")
    assert [c.status for c in changed] == ["stale_or_numeric_reference", "exact_fresh"]
    assert changed[0].match is not None and changed[0].match.numeric_changed is True
    assert changed[1].match is not None and changed[1].match.match_kind == "exact"

    stale = store.classify_segments_for_tm(
        segments, "en", current_terms_db_revision="r15", current_game_version="3.0")
    assert [c.status for c in stale] == ["stale_or_numeric_reference", "stale_or_numeric_reference"]
    assert stale[0].match is not None
    assert "terms_revision_changed" in stale[0].match.stale_reasons

    unmatched = store.classify_segments_for_tm(["没有见过的段落"], "en")
    assert unmatched[0].status == "unmatched" and unmatched[0].match is None


def test_classification_accepts_explicit_context_and_normalized_references(store):
    store.add("A B", "translated", "en", next_source="C")
    classified = store.classify_segments_for_tm(
        [{"source": "A\nB", "prev_source": None, "next_source": "C"}], "en")
    # A mapping names its own context; the whitespace difference is only a reference.
    assert classified[0].status == "stale_or_numeric_reference"
    assert classified[0].match is not None
    assert classified[0].match.match_kind == "normalized_exact"
    with pytest.raises(tm.TmError):
        store.classify_segments_for_tm([42], "en")
    with pytest.raises(tm.TmError):
        store.classify_segments_for_tm(["   "], "en")
    with pytest.raises(tm.TmError):
        store.classify_segments_for_tm(["ok"], "fr")


# -- input validation --------------------------------------------------------


def test_the_store_validates_every_value_before_writing(store):
    bad_calls = [
        (dict(source_text="", target_text="b", direction="en"), "tm_invalid_content"),
        (dict(source_text="   ", target_text="b", direction="en"), "tm_invalid_content"),
        (dict(source_text="a", target_text="   ", direction="en"), "tm_invalid_content"),
        (dict(source_text="a", target_text="b", direction="fr"), "tm_invalid_content"),
        (dict(source_text="a" * (tm.SOURCE_SCALARS[1] + 1), target_text="b", direction="en"),
         "tm_invalid_content"),
        (dict(source_text="a", target_text="b", direction="en", note="n" * (tm.NOTE_SCALARS[1] + 1)),
         "tm_invalid_content"),
        (dict(source_text="a", target_text="b", direction="en", terms_db_revision=""), "tm_invalid_content"),
        (dict(source_text="a", target_text="b", direction="en", game_version="g" * 129),
         "tm_invalid_content"),
        (dict(source_text="a", target_text="b", direction="en", prev_source="x" * 2001),
         "tm_invalid_content"),
        (dict(source_text="a\x00b", target_text="b", direction="en"), "tm_nul"),
        (dict(source_text="a\ud800b", target_text="b", direction="en"), "tm_surrogate"),
        (dict(source_text="a", target_text="b", direction="en", origin="draft"), "tm_unconfirmed_origin"),
    ]
    for kwargs, code in bad_calls:
        with pytest.raises(tm.TmError) as caught:
            store.add(**kwargs)
        assert caught.value.code == code
    assert len(store) == 0

    with pytest.raises(tm.TmError):
        store.lookup_segment("", "en")
    with pytest.raises(tm.TmError):
        store.lookup_segment("a", "en", current_terms_db_revision="")
    with pytest.raises(tm.TmError):
        store.lookup_segment("a", "en", current_game_version="g\x00")
    with pytest.raises(tm.TmError):
        store.lookup_segment("a", "en", limit=True)


# -- a foreign or newer file -------------------------------------------------


def test_a_file_that_is_not_a_translation_memory_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE other (value TEXT)")
    connection.execute("INSERT INTO other VALUES ('keep me')")
    connection.commit()
    connection.close()
    before = path.read_bytes()

    store = tm.TranslationMemory(path)
    assert not store.usable and isinstance(store.error, tm.TmError)
    assert store.error.code == "tm_schema_mismatch"
    assert len(store) == 0 and store.entries() == [] and store.lookup_segment("a", "en") == []
    with pytest.raises(tm.TmError):
        store.add("a", "b", "en")
    with pytest.raises(tm.TmError):
        store.clear()
    assert path.read_bytes() == before

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM other").fetchone()[0] == "keep me"
    finally:
        connection.close()


def test_a_newer_schema_version_is_refused_rather_than_written_into(tmp_path):
    path = tmp_path / "tm.sqlite3"
    store = tm.TranslationMemory(path, clock=lambda: STAMP)
    store.add("a", "b", "en")
    connection = sqlite3.connect(path)
    connection.execute("UPDATE tm_schema SET value = '2' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    newer = tm.TranslationMemory(path)
    assert not newer.usable and newer.error is not None
    assert newer.error.code == "tm_schema_mismatch"


# -- portable JSON -----------------------------------------------------------


def test_json_round_trip_is_canonical_and_reimportable(tmp_path):
    source = tm.TranslationMemory(tmp_path / "a.sqlite3", clock=lambda: STAMP)
    source.add("攻击力提高 20%，持续 10 秒", "ATK +20% for 10s", "en",
               next_source="另一段", terms_db_revision="r14", game_version="3.0", note="确认")
    imported = source.add("另一段", "another segment", "en", prev_source="攻击力提高 20%，持续 10 秒")
    assert imported.prev_source_hash == tm.source_hash("攻击力提高 20%，持续 10 秒")
    data = source.export_tm_json()
    parsed = tm.parse_tm_json_bytes(data)
    assert len(parsed) == 2
    assert all(entry.origin == "human_confirmed" for entry in parsed)

    target = tm.TranslationMemory(tmp_path / "b.sqlite3", clock=lambda: STAMP)
    assert target.import_tm_json_bytes(data) == 2
    assert target.export_tm_json() == data
    (match,) = target.lookup_segment("攻击力提高 20%，持续 10 秒", "en", next_source="另一段")
    assert match.match_kind == "context_exact" and match.entry.note == "确认"


def test_the_exported_keys_and_order_are_the_format(tmp_path):
    store = tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)
    store.add("a", "b", "en")
    payload = json.loads(store.export_tm_json())
    assert list(payload) == ["format", "schema_version", "exported_at", "entries"]
    assert list(payload["entries"][0]) == [
        "direction", "source_text", "target_text", "prev_source_hash", "next_source_hash",
        "origin", "terms_db_revision", "game_version", "note", "use_count", "created_at", "updated_at",
    ]
    assert payload["format"] == tm.TM_FORMAT and payload["schema_version"] == 1


def test_a_second_import_merges_without_doubling_or_losing_provenance(tmp_path):
    source = tm.TranslationMemory(tmp_path / "a.sqlite3", clock=lambda: STAMP)
    source.add("主C", "main DPS", "en", terms_db_revision="r14", note="人工")
    data = source.export_tm_json()

    target = tm.TranslationMemory(tmp_path / "b.sqlite3", clock=lambda: STAMP)
    target.add("主C", "main DPS", "en", origin="imported_confirmed", game_version="3.0")
    target.import_tm_json_bytes(data)
    target.import_tm_json_bytes(data)
    (entry,) = target.entries()
    assert len(target) == 1
    assert entry.origin == "human_confirmed"  # never downgraded
    assert entry.use_count == 1  # merging is not a use
    assert entry.terms_db_revision == "r14" and entry.game_version == "3.0"
    assert entry.note == "人工"

    exported = json.loads(target.export_tm_json())
    assert exported["entries"][0]["origin"] == "human_confirmed"
    assert exported["entries"][0]["use_count"] == 1


def test_importing_the_same_file_into_a_replaced_memory_is_stable(tmp_path):
    source = tm.TranslationMemory(tmp_path / "a.sqlite3", clock=lambda: STAMP)
    for index in range(3):
        source.add(f"source {index}", f"target {index}", "en")
    data = source.export_tm_json()
    target = tm.TranslationMemory(tmp_path / "b.sqlite3", clock=lambda: STAMP)
    assert target.import_tm_json_bytes(data, replace=True) == 3
    first = target.export_tm_json()
    target.import_tm_json_bytes(data, replace=True)
    assert target.export_tm_json() == first


# -- strict JSON refusals ----------------------------------------------------


def _refuses(data: bytes, code: str) -> None:
    assert code in tm.TM_ERROR_CODES
    with pytest.raises(tm.TmError) as caught:
        tm.parse_tm_json_bytes(data)
    assert caught.value.code == code


def test_the_strict_reader_refuses_what_the_format_does_not_allow():
    _refuses(b"", "tm_not_json")
    _refuses(b"{", "tm_not_json")
    _refuses(b"\xff\xfe\x00", "tm_not_json")
    _refuses(b"\xef\xbb\xbf" + _bytes(_file()), "tm_not_json")  # a BOM is not UTF-8 text
    _refuses(_bytes(_file()) + b" trailing", "tm_not_json")
    _refuses(_bytes(_file()) + b"\x00", "tm_nul")
    _refuses(b'{"format": "wuwaterm-tm-v1", "format": "wuwaterm-tm-v1"}', "tm_duplicate_key")
    _refuses(b'{"__proto__": 1, "format": "wuwaterm-tm-v1"}', "tm_forbidden_key")
    _refuses(b'{"constructor": 1}', "tm_forbidden_key")
    _refuses(b'{"format": NaN}', "tm_non_finite")
    _refuses(b'{"format": -Infinity}', "tm_non_finite")
    _refuses(b'{"entries": 1e400}', "tm_non_finite")
    _refuses(_bytes(_file([_entry(source_text="a\u0000b")])), "tm_nul")
    lone = json.dumps(_file([_entry(source_text="a\ud800b")]), ensure_ascii=True).encode("utf-8")
    _refuses(lone, "tm_surrogate")
    _refuses(_bytes(_file([_entry(origin="machine_translated")])), "tm_unconfirmed_origin")
    _refuses(_bytes(_file([_entry(origin="draft")])), "tm_unconfirmed_origin")
    _refuses(_bytes(_file([_entry(extra="unknown")])), "tm_invalid_content")
    missing = _entry()
    del missing["note"]
    _refuses(_bytes(_file([missing])), "tm_invalid_content")
    _refuses(_bytes(_file(schema_version=2)), "tm_invalid_content")
    _refuses(_bytes(_file(schema_version=True)), "tm_invalid_content")
    _refuses(_bytes(_file(format="wuwaterm-tm-v2")), "tm_invalid_content")
    _refuses(_bytes(_file(exported_at="2026-11-27 00:00:00")), "tm_invalid_content")
    not_a_list = _file()
    not_a_list["entries"] = {}
    _refuses(_bytes(not_a_list), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(direction="fr")])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(source_text="")])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(target_text="   ")])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(note="n" * 161)])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(prev_source_hash="A" * 64)])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(use_count=-1)])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(use_count=True)])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(created_at="yesterday")])), "tm_invalid_content")
    _refuses(_bytes(_file([_entry(), _entry()])), "tm_invalid_content")  # duplicate candidate


def test_the_size_and_entry_limits_are_refused_before_anything_is_read_or_written():
    _refuses(b"x" * (tm.MAX_TM_BYTES + 1), "tm_too_large")
    too_many = _bytes(_file([_entry(index) for index in range(tm.MAX_TM_ENTRIES + 1)]))
    assert len(too_many) < tm.MAX_TM_BYTES
    _refuses(too_many, "tm_too_many_entries")


def test_a_refused_import_changes_nothing(tmp_path):
    store = tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)
    store.add("保留", "kept", "en")
    before = store.export_tm_json()
    for data, code in (
        (_bytes(_file([_entry(origin="machine_translated")])), "tm_unconfirmed_origin"),
        (b"not json", "tm_not_json"),
        (_bytes(_file([_entry(), _entry()])), "tm_invalid_content"),
    ):
        with pytest.raises(tm.TmError) as caught:
            store.import_tm_json_bytes(data)
        assert caught.value.code == code
        assert store.export_tm_json() == before


def test_a_file_over_the_limit_from_disk_is_refused_without_being_loaded_whole(tmp_path):
    path = tmp_path / "huge.json"
    path.write_bytes(b"x" * (tm.MAX_TM_BYTES + 1))
    assert len(tm.read_tm_file_bytes(path)) == tm.MAX_TM_BYTES + 1
    store = tm.TranslationMemory(tmp_path / "tm.sqlite3")
    with pytest.raises(tm.TmError) as caught:
        store.import_tm_json_bytes(tm.read_tm_file_bytes(path))
    assert caught.value.code == "tm_too_large"


# -- the boundaries ----------------------------------------------------------


_NETWORK_AND_SECRET_MODULES = frozenset({
    "socket", "ssl", "http", "urllib", "urllib3", "httpx", "requests", "keyring", "PySide6",
})
_FORBIDDEN_LOCAL_MODULES = frozenset({"manuscript", "credentials", "api"})


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_the_engine_imports_no_network_credential_manuscript_or_qt_code():
    imported = _imported_modules(SRC / "translation_memory.py")
    assert not imported & _NETWORK_AND_SECRET_MODULES, imported & _NETWORK_AND_SECRET_MODULES
    local = {name.lstrip(".") for name in imported if name.startswith(".")}
    # The one local import is the path of the per-user folder, loaded only inside the helper.
    assert local <= {"config"}
    assert not local & _FORBIDDEN_LOCAL_MODULES


def test_storing_matching_and_moving_a_memory_open_no_connection(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        pytest.fail("the translation memory tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    store = tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)
    store.add("主C", "main DPS", "en")
    store.lookup_segment("主C", "en")
    store.classify_segments_for_tm(["主C"], "en")
    out = tmp_path / "out.json"
    tm.export_tm_file(store, out)
    other = tm.TranslationMemory(tmp_path / "other.sqlite3")
    other.import_tm_json_bytes(tm.read_tm_file_bytes(out))
    assert len(other) == 1


def test_a_numbered_template_cannot_make_rank_three_unbounded(tmp_path):
    store = tm.TranslationMemory(tmp_path / "tm.sqlite3", clock=lambda: STAMP)
    entries = [
        _entry(
            index,
            source_text=f"攻击力提高 {index}，持续 10 秒",
            target_text=f"ATK +{index} for 10s",
            use_count=index + 1,
        )
        for index in range(300)
    ]
    store.import_tm_json_bytes(_bytes(_file(entries)))
    matches = store.lookup_segment("攻击力提高 999，持续 10 秒", "en", limit=5)
    assert [m.match_kind for m in matches] == ["numeric_variant"] * 5
    # The cap keeps the most-used candidates, not the first row SQLite meets.
    assert [m.entry.use_count for m in matches] == sorted(
        (m.entry.use_count for m in matches), reverse=True)
    assert "攻击力提高 299，持续 10 秒" == matches[0].entry.source_text


def test_an_empty_file_is_initialised_rather_than_failing_every_query(tmp_path):
    path = tmp_path / "tm.sqlite3"
    path.touch()
    store = tm.TranslationMemory(path, clock=lambda: STAMP)
    assert store.usable and len(store) == 0 and store.entries() == []
    store.add("a", "b", "en")
    assert len(store) == 1
