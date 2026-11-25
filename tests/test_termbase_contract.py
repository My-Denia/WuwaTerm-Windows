"""The shared local-termbase contract (contracts/termbase-v1 in the pinned Web checkout).

Every committed fixture and the four generated size fixtures run through the
Windows reader and writer; the reference matcher is checked against the rules
the contract states; the file the desktop keeps is written atomically; and the
three things a termbase never does (join a manuscript, leave the machine, mix
with the credential) are checked on the code and on a real run.

Pure Python: no Qt, no HTTP.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import socket
from pathlib import Path

import pytest

from wuwaterm_client import termbase as tb
from wuwaterm_client.manuscript import empty_workfile, serialize_workfile

SRC = Path(tb.__file__).parent
STAMP = "2026-11-25T00:00:00Z"


@pytest.fixture
def web(contract_source) -> Path:
    return contract_source("web")


@pytest.fixture
def contract(web) -> Path:
    return web / "contracts" / "termbase-v1"


@pytest.fixture
def cases(contract) -> dict:
    return json.loads((contract / "cases.json").read_text(encoding="utf-8"))


def _entry(entry_id: str, source: str, direction: str, **over) -> dict:
    entry = {"id": entry_id, "source": source, "target": f"t-{entry_id}", "direction": direction,
             "kind": "community_term", "note": "", "updated_at": STAMP}
    entry.update(over)
    return entry


def _file(entries: list[dict]) -> dict:
    return {"format": tb.TERMBASE_FORMAT, "schema_version": 1, "updated_at": STAMP, "entries": entries}


def _generated(spec: dict) -> bytes:
    """Builds a size fixture from its descriptor, as Web's buildGenerated does."""
    count = spec["count"] if spec["kind"] == "entries" else tb.MAX_TERMBASE_ENTRIES
    entries = [
        _entry(f"tb-{i + 1:04d}", f"term {i + 1}", "zh" if i % 2 else "en", target=f"译名{i + 1}", note="n" * 100)
        for i in range(count)
    ]
    text = json.dumps(_file(entries), indent=2, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    if spec["kind"] == "entries":
        return data
    assert len(data) <= spec["bytes"]
    return data + b" " * (spec["bytes"] - len(data))


# -- the contract's own description -------------------------------------------


def test_the_contract_names_the_codes_and_limits_this_reader_implements(cases, web) -> None:
    assert cases["format"] == tb.TERMBASE_FORMAT
    assert cases["codes"] == list(tb.TERMBASE_ERROR_CODES)
    assert cases["limits"] == {
        "file_bytes": tb.MAX_TERMBASE_BYTES, "entries": tb.MAX_TERMBASE_ENTRIES,
        "id_scalars": tb.ID_SCALARS[1], "source_scalars": tb.SOURCE_SCALARS[1],
        "target_scalars": tb.TARGET_SCALARS[1], "note_scalars": tb.NOTE_SCALARS[1],
    }
    # The generated Python types are the schema in code; the constants must agree.
    spec = importlib.util.spec_from_file_location("termbase_contract", web / "contracts" / "generated" / "termbase_contract.py")
    generated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generated)
    assert generated.TERMBASE_FORMAT == tb.TERMBASE_FORMAT
    assert generated.TERMBASE_SCHEMA_VERSION == tb.TERMBASE_SCHEMA_VERSION
    assert generated.TERMBASE_DIRECTION_VALUES == tb.TERMBASE_DIRECTION_VALUES
    assert generated.TERMBASE_KIND_VALUES == tb.TERMBASE_KIND_VALUES
    assert generated.TERMBASE_MAX_ENTRIES == tb.MAX_TERMBASE_ENTRIES
    assert generated.TERMBASE_ID_SCALARS == tb.ID_SCALARS
    assert generated.TERMBASE_SOURCE_SCALARS == tb.SOURCE_SCALARS
    assert generated.TERMBASE_TARGET_SCALARS == tb.TARGET_SCALARS
    assert generated.TERMBASE_NOTE_SCALARS == tb.NOTE_SCALARS
    assert tuple(generated.ContractTermbaseEntryV1.__annotations__) == tb._ENTRY_KEYS
    assert tuple(generated.ContractTermbaseV1.__annotations__) == tb._TOP_KEYS


def test_the_fixture_set_is_the_57_committed_files_plus_4_generated(cases, contract) -> None:
    names = {path.name for path in (contract / "fixtures").iterdir()}
    assert len(names) == 57 == len(cases["valid"]) + len(cases["invalid"])
    assert names == {item["file"] for item in cases["valid"] + cases["invalid"]}
    assert len(cases["generated"]) == 4


# -- every fixture --------------------------------------------------------------


def test_every_valid_fixture_is_read_and_written_back_to_the_contracts_bytes(cases, contract) -> None:
    for item in cases["valid"]:
        data = (contract / "fixtures" / item["file"]).read_bytes()
        parsed = tb.parse_termbase_bytes(data)
        written = tb.serialize_termbase(parsed)
        if item["canonical"]:
            assert written == data, item["file"]
        # Whatever the input spacing, what is written reads back as the same termbase.
        assert tb.parse_termbase_bytes(written) == parsed, item["file"]
        assert written.endswith(b"\n") and not written.endswith(b"\n\n") and b"\r" not in written


def test_every_invalid_fixture_is_refused_whole_with_the_contracts_code(cases, contract) -> None:
    for item in cases["invalid"]:
        data = (contract / "fixtures" / item["file"]).read_bytes()
        with pytest.raises(tb.TermbaseError) as refused:
            tb.parse_termbase_bytes(data)
        assert refused.value.code == item["code"], f"{item['file']}: {refused.value}"


def test_the_generated_size_fixtures_sit_exactly_on_the_limits(cases) -> None:
    by_file = {item["file"]: item for item in cases["generated"]}
    full = tb.parse_termbase_bytes(_generated(by_file["valid-2000-entries.json"]))
    assert len(full["entries"]) == 2000
    assert tb.serialize_termbase(full) == _generated(by_file["valid-2000-entries.json"])
    exact = _generated(by_file["valid-exactly-1-mib.json"])
    assert len(exact) == 1_048_576
    assert len(tb.parse_termbase_bytes(exact)["entries"]) == 2000
    for name, code in (("invalid-2001-entries.json", "termbase_too_many_entries"),
                       ("invalid-1-mib-plus-1.json", "termbase_too_large")):
        data = _generated(by_file[name])
        with pytest.raises(tb.TermbaseError) as refused:
            tb.parse_termbase_bytes(data)
        assert refused.value.code == code == by_file[name]["code"]
    assert len(_generated(by_file["invalid-1-mib-plus-1.json"])) == 1_048_577


def test_a_reader_error_names_a_location_and_never_echoes_file_content(contract) -> None:
    secret = "SECRET-NOTE-TEXT"
    text = (contract / "fixtures" / "valid-one-entry.json").read_text(encoding="utf-8")
    text = text.replace("Community team role shorthand in guides.", secret).replace('"direction": "en"', '"direction": "xx"')
    with pytest.raises(tb.TermbaseError) as refused:
        tb.parse_termbase_bytes(text.encode("utf-8"))
    assert refused.value.code == "termbase_invalid_content"
    assert refused.value.detail.startswith("entries[0].direction")
    assert secret not in str(refused.value)


def test_where_a_file_breaks_several_rules_the_first_met_while_reading_wins() -> None:
    # A duplicate key before a forbidden key, and the reverse: file order decides.
    dup_first = b'{"format":"x","format":"y","__proto__":1}'
    proto_first = b'{"__proto__":1,"format":"x","format":"y"}'
    assert _code(dup_first) == "termbase_duplicate_key"
    assert _code(proto_first) == "termbase_forbidden_key"
    # Size comes before everything, a raw NUL before the reader.
    assert _code(b" " * (tb.MAX_TERMBASE_BYTES + 1)) == "termbase_too_large"
    assert _code(b'{"a":\x00 NaN}') == "termbase_nul"


def _code(data: bytes) -> str:
    with pytest.raises(tb.TermbaseError) as refused:
        tb.parse_termbase_bytes(data)
    return refused.value.code


def test_timestamps_are_rfc_3339_utc_with_real_calendar_days() -> None:
    for ok in ("2026-11-25T00:00:00Z", "2024-02-29T23:59:59Z", "2026-11-25T08:30:15.123456789Z"):
        assert tb.is_termbase_timestamp(ok), ok
    for no in ("2026-02-29T00:00:00Z", "2026-04-31T00:00:00Z", "2026-11-25T24:00:00Z", "2026-11-25T00:00:60Z",
               "2026-11-25T00:00:00+00:00", "2026-11-25 00:00:00Z", "2026-11-25T00:00:00.Z", "", 5, None,
               "２０２６-11-25T00:00:00Z"):
        assert not tb.is_termbase_timestamp(no), repr(no)
    assert tb.is_termbase_timestamp(tb.termbase_now()) and "." not in tb.termbase_now()


def test_validation_returns_a_copy_and_next_entry_id_skips_taken_ids(contract) -> None:
    source = tb.parse_termbase_bytes((contract / "fixtures" / "valid-all-kinds.json").read_bytes())
    copy = tb.validate_termbase(source)
    assert copy == source and copy["entries"][0] is not source["entries"][0]
    assert tb.next_entry_id([]) == "tb-001"
    assert tb.next_entry_id([{"id": "tb-002"}, {"id": "tb-001"}]) == "tb-003"
    assert tb.next_entry_id([{"id": "tb-002"}]) == "tb-003"


# -- the reference matcher --------------------------------------------------------


def _ids(entries, text, direction):
    return [m.entry["id"] for m in tb.match_termbase(entries, text, direction).matches]


def test_matching_is_literal_per_direction_ordered_by_first_position_with_scalar_offsets() -> None:
    entries = [_entry("a", "主C", "en"), _entry("b", "今汐", "en"), _entry("c", "今汐", "zh"), _entry("d", "不存在", "en")]
    text = "今汐是主C，今汐强。"
    result = tb.match_termbase(entries, text, "en")
    assert not result.truncated
    assert [m.entry["id"] for m in result.matches] == ["b", "a"]
    assert result.matches[0].spans == (tb.TermbaseSpan(0, 2), tb.TermbaseSpan(6, 8))
    assert result.matches[0].count == 2
    assert result.matches[1].spans == (tb.TermbaseSpan(3, 5),)
    assert _ids(entries, "今汐", "zh") == ["c"]
    assert tb.match_termbase([], text, "en") == tb.TermbaseMatches((), False)
    assert tb.match_termbase(entries, "", "en") == tb.TermbaseMatches((), False)


def test_latin_surfaces_are_case_insensitive_whole_words_and_cjk_or_symbol_edges_are_substrings() -> None:
    entries = [_entry("dps", "DPS", "en"), _entry("sub", "sub carry", "zh"), _entry("cpp", "C++", "zh"),
               _entry("dot", "a.b", "zh")]
    assert _ids(entries, "这个dps很强", "en") == ["dps"]
    assert _ids(entries, "MyDPS and DPSX", "en") == []
    assert _ids(entries, "A Sub Carry, ok", "zh") == ["sub"]
    assert _ids(entries, "subcarry", "zh") == []
    assert tb.match_termbase(entries, "I like C++ a lot, and C++.", "zh").matches[0].count == 2
    # Regex metacharacters in a surface are literal.
    assert _ids(entries, "xa.b and aXb", "zh") == []
    assert _ids(entries, "a.b and aXb", "zh") == ["dot"]
    # A rejected occurrence does not hide a real one that overlaps it.
    assert tb.match_termbase([_entry("x", "aa", "zh")], "aaa aa", "zh").matches[0].spans == (tb.TermbaseSpan(4, 6),)


def test_matching_counts_astral_characters_as_one_scalar_and_caps_the_work_on_a_long_text() -> None:
    astral = tb.match_termbase([_entry("x", "\U00020BB7字", "en")], "\U00020BB7\U00020BB7字", "en")
    assert astral.matches[0].spans == (tb.TermbaseSpan(1, 3),)
    many = tb.match_termbase([_entry("x", "ab", "zh")], "ab " * 500, "zh")
    assert many.matches[0].count == 500 and len(many.matches[0].spans) == tb.MATCH_SPANS_PER_ENTRY
    long = tb.match_termbase([_entry("x", "尾", "en")], "甲" * tb.MATCH_TEXT_LIMIT + "尾", "en")
    assert long.truncated and long.matches == ()
    cut = tb.match_termbase([_entry("x", "ab", "zh")], "x" * (tb.MATCH_TEXT_LIMIT - 1) + "\U00020BB7ab", "zh")
    assert cut.truncated


def test_matching_never_changes_its_inputs() -> None:
    entries = [_entry("a", "今汐", "en")]
    before = json.dumps(entries)
    tb.match_termbase(entries, "今汐", "en")
    assert json.dumps(entries) == before


def test_a_full_termbase_matches_a_long_text_without_a_noticeable_wait() -> None:
    import time

    entries = [_entry(f"tb-{i:04d}", f"term {i}", "zh") for i in range(2000)]
    text = ("The Rover meets Jinhsi. " * 5000)[:100_000] + " term 1999"
    start = time.perf_counter()
    result = tb.match_termbase(entries, text, "zh")
    assert time.perf_counter() - start < 2.0
    assert result.matches == () and result.truncated


# -- the file the desktop keeps -----------------------------------------------------


def _clock():
    return lambda: STAMP


def test_the_store_saves_every_change_to_one_canonical_file(tmp_path) -> None:
    path = tmp_path / "WuwaTerm" / "termbase.json"
    store = tb.TermbaseStore(path, clock=_clock())
    assert store.usable and len(store) == 0 and not path.exists()
    added = store.add(source="主C", target="main DPS", direction="en", kind="community_term", note="role")
    assert added["id"] == "tb-001" and added["updated_at"] == STAMP
    store.add(source="burst", target="爆发", direction="zh", kind="preferred_term")
    store.update("tb-001", source="主C", target="primary DPS", direction="en", kind="preferred_term", note="")
    store.remove("tb-002")
    data = path.read_bytes()
    assert data == tb.serialize_termbase(store.termbase)
    reopened = tb.TermbaseStore(path, clock=_clock())
    assert reopened.entries == store.entries == [
        {"id": "tb-001", "source": "主C", "target": "primary DPS", "direction": "en", "kind": "preferred_term",
         "note": "", "updated_at": STAMP}]
    with pytest.raises(KeyError):
        store.remove("tb-404")
    assert sorted(p.name for p in path.parent.iterdir()) == ["termbase.json"]


def test_the_default_path_is_the_roaming_app_data_folder_beside_the_config_and_apart_from_the_credential(tmp_path) -> None:
    pytest.importorskip("httpx")
    from wuwaterm_client import config

    path = tb.termbase_path()
    assert path == config.app_data_dir() / "termbase.json"
    assert path.parent == config.config_path().parent and path != config.config_path()
    assert str(path).startswith(str(tmp_path))  # conftest points APPDATA at a temporary folder
    store = tb.TermbaseStore()
    store.add(source="a", target="b", direction="en", kind="community_term")
    assert path.is_file() and not config.config_path().exists()  # nothing of the termbase went into the settings file


def test_the_write_goes_through_a_same_directory_temporary_file_fsync_then_replace(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append(("fsync", ""))
        return real_fsync(fd)

    def replace(src, dst):
        calls.append(("replace", os.path.dirname(os.fspath(src))))
        assert os.path.getsize(src) > 0  # the bytes were already in the temporary file
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    path = tmp_path / "termbase.json"
    tb.TermbaseStore(path, clock=_clock()).add(source="a", target="b", direction="en", kind="community_term")
    assert [name for name, _ in calls] == ["fsync", "replace"]
    assert calls[1][1] == str(tmp_path)


def test_a_failed_write_leaves_the_previous_file_and_no_temporary_file_behind(tmp_path, monkeypatch) -> None:
    path = tmp_path / "termbase.json"
    store = tb.TermbaseStore(path, clock=_clock())
    store.add(source="a", target="b", direction="en", kind="community_term")
    before = path.read_bytes()

    def broken_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.add(source="c", target="d", direction="en", kind="community_term")
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["termbase.json"]
    # The in-memory termbase did not move either.
    assert [e["source"] for e in store.entries] == ["a"]

    monkeypatch.undo()

    def broken_fsync(fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError):
        store.remove("tb-001")
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["termbase.json"]


def test_a_refused_file_is_kept_as_it_is_until_the_person_replaces_or_resets_it(tmp_path) -> None:
    path = tmp_path / "termbase.json"
    path.write_bytes(b'{"format": "wuwaterm-termbase-v1", "schema_version": 1, "entries": []}')
    original = path.read_bytes()
    store = tb.TermbaseStore(path, clock=_clock())
    assert not store.usable and isinstance(store.error, tb.TermbaseError) and store.entries == []
    with pytest.raises(tb.TermbaseUnavailable):
        store.add(source="a", target="b", direction="en", kind="community_term")
    with pytest.raises(tb.TermbaseUnavailable):
        store.remove("tb-001")
    assert path.read_bytes() == original
    store.replace(tb.validate_termbase(_file([_entry("tb-001", "x", "en")])))
    assert store.usable and tb.TermbaseStore(path).entries[0]["source"] == "x"
    path.write_bytes(b"not json")
    broken = tb.TermbaseStore(path)
    assert not broken.usable
    broken.reset()
    assert broken.usable and tb.TermbaseStore(path).entries == []


def test_a_termbase_over_the_limits_is_refused_before_anything_is_written(tmp_path) -> None:
    path = tmp_path / "termbase.json"
    store = tb.TermbaseStore(path, clock=_clock())
    store.replace(tb.validate_termbase(_file([_entry(f"tb-{i:04d}", f"s{i}", "en") for i in range(2000)])))
    before = path.read_bytes()
    with pytest.raises(tb.TermbaseError) as refused:
        store.add(source="one more", target="t", direction="en", kind="community_term")
    assert refused.value.code == "termbase_too_many_entries"
    with pytest.raises(tb.TermbaseError) as too_long:
        store.update("tb-0001", source="x" * 121, target="t", direction="en", kind="community_term")
    assert too_long.value.code == "termbase_invalid_content"
    assert path.read_bytes() == before


def test_import_and_export_move_whole_canonical_files_only(tmp_path, cases, contract) -> None:
    good = tmp_path / "in.json"
    good.write_bytes((contract / "fixtures" / "valid-compact-spacing.json").read_bytes())
    imported = tb.import_termbase_file(good)
    out = tmp_path / "out.json"
    tb.export_termbase_file(imported, out)
    assert out.read_bytes() == tb.serialize_termbase(imported)
    bad = tmp_path / "bad.json"
    bad.write_bytes((contract / "fixtures" / "invalid-duplicate-id.json").read_bytes())
    with pytest.raises(tb.TermbaseError):
        tb.import_termbase_file(bad)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (tb.MAX_TERMBASE_BYTES + 5000))
    with pytest.raises(tb.TermbaseError) as refused:
        tb.import_termbase_file(big)
    assert refused.value.code == "termbase_too_large"
    with pytest.raises(FileNotFoundError):
        tb.import_termbase_file(tmp_path / "missing.json")


# -- what a termbase never does ---------------------------------------------------------

_NETWORK_AND_SECRET_MODULES = {
    "httpx", "requests", "urllib", "urllib3", "http", "socket", "ssl", "asyncio", "aiohttp", "subprocess",
    "keyring", "webbrowser", "smtplib", "ftplib", "xmlrpc",
}
_FORBIDDEN_LOCAL_MODULES = {"api", "credentials", "manuscript", "document_controller", "semantic", "semantic_session",
                            "segment_records", "markdown_report", "recovery", "errors"}


def _imported_modules(path: Path) -> set[str]:
    """Top-level modules a file imports; a package-relative import keeps a leading dot."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.update("." + (node.module or alias.name).split(".")[0] for alias in node.names)
            else:
                names.add((node.module or "").split(".")[0])
    return names


def test_the_termbase_module_imports_no_network_credential_or_manuscript_code() -> None:
    imported = _imported_modules(SRC / "termbase.py")
    assert not imported & _NETWORK_AND_SECRET_MODULES, imported & _NETWORK_AND_SECRET_MODULES
    local = {name.lstrip(".") for name in imported if name.startswith(".")}
    assert not local & _FORBIDDEN_LOCAL_MODULES, local
    # The one local import is the path of the per-user folder, loaded only to name the default file.
    assert local <= {"config"}


def test_only_the_termbase_screens_import_the_termbase() -> None:
    allowed = {"termbase.py", "ui/termbase_dialog.py", "ui/manuscript_view.py", "ui/main_window.py"}
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        mentions = "termbase" in text.lower()
        if relative not in allowed:
            assert not mentions or relative == "strings.py", f"{relative} mentions the termbase"


def test_a_termbase_never_enters_a_manuscript_file(tmp_path) -> None:
    store = tb.TermbaseStore(tmp_path / "termbase.json", clock=_clock())
    store.add(source="主C", target="SECRET-RENDERING", direction="en", kind="preferred_term", note="SECRET-NOTE")
    payload = empty_workfile()
    payload.update(source="今汐是主C", target="Jinhsi is the main DPS", direction="en")
    written = serialize_workfile(payload)
    assert "SECRET-RENDERING" not in written and "SECRET-NOTE" not in written and "termbase" not in written.lower()
    assert tb.match_termbase(store.entries, payload["source"], "en").matches  # the hit exists, the file still holds none
    assert "termbase" not in " ".join(empty_workfile()).lower()
    assert not list(tmp_path.glob("*.wuwaterm")) and store.path.suffix == ".json"


def test_reading_matching_and_writing_a_termbase_open_no_connection(tmp_path, monkeypatch) -> None:
    def refuse(*_args, **_kwargs):
        pytest.fail("the termbase tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    path = tmp_path / "termbase.json"
    store = tb.TermbaseStore(path, clock=_clock())
    store.add(source="主C", target="main DPS", direction="en", kind="community_term")
    tb.match_termbase(store.entries, "主C", "en")
    out = tmp_path / "out.json"
    tb.export_termbase_file(store.termbase, out)
    store.replace(tb.import_termbase_file(out))
    assert len(store) == 1


def test_the_file_holds_no_credential_and_the_store_has_no_place_to_put_one(tmp_path) -> None:
    store = tb.TermbaseStore(tmp_path / "termbase.json", clock=_clock())
    store.add(source="a", target="b", direction="en", kind="community_term")
    keys = set(json.loads(store.path.read_text("utf-8")))
    assert keys == set(tb._TOP_KEYS)
    assert not {"token", "password", "secret", "api_key", "base_url"} & keys
    assert not hasattr(store, "token") and not hasattr(tb, "load_token")
