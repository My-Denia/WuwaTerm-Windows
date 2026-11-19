"""Manuscript v3 (public Web contracts/manuscript-v3/SPEC.md) in the client's reader and writer.

Pure: needs only pytest and the pinned Web checkout, no PySide6 or keyring.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from wuwaterm_client import manuscript


def _folder(contract_source) -> Path:
    return contract_source("web") / "contracts" / "manuscript-v3"


def _cases(folder: Path) -> dict:
    return json.loads((folder / "cases.json").read_text(encoding="utf-8"))


def _content(folder: Path, case: dict) -> str:
    """The case's file; with pad_to_bytes, padded with ASCII spaces to exactly that many bytes."""
    text = (folder / case["file"]).read_text(encoding="utf-8")
    if "pad_to_bytes" in case:
        text += " " * (case["pad_to_bytes"] - len(text.encode("utf-8")))
    return text


def _reversed_keys(value):
    if isinstance(value, list):
        return [_reversed_keys(item) for item in value]
    if isinstance(value, dict):
        return {key: _reversed_keys(child) for key, child in reversed(list(value.items()))}
    return value


def _code(content: str) -> str:
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.parse_workfile(content)
    return error.value.code


def test_exported_limits_equal_the_contract(contract_source) -> None:
    limits = _cases(_folder(contract_source))["limits"]
    assert limits == {
        "file_bytes": manuscript.MAX_V3_BYTES, "legacy_file_bytes": manuscript.MAX_WORKFILE_BYTES,
        "text_scalars": manuscript.MAX_V3_TEXT_SCALARS, "array_items": manuscript.MAX_V3_ITEMS, "object_fields": 32,
        "depth": 16, "segments": manuscript.MAX_SEGMENTS, "findings_per_result": manuscript.MAX_RESULT_FINDINGS,
        "candidates_per_finding": manuscript.MAX_FINDING_CANDIDATES,
        "choices_per_record": manuscript.MAX_SEGMENT_CHOICES, "offered_per_choice": manuscript.MAX_OFFERED,
        "segment_choice_history": manuscript.MAX_SEGMENT_CHOICE_HISTORY,
        "progress_completed": manuscript.MAX_SEGMENTS, "core_scalars": manuscript.MAX_CORE_SCALARS,
        "choices": manuscript.MAX_CHOICES, "history": manuscript.MAX_HISTORY, "alignments": 64,
        "semantic_decisions": manuscript.MAX_SEMANTIC_DECISIONS, "semantic_texts": manuscript.MAX_SEMANTIC_TEXTS,
        "semantic_history": manuscript.MAX_SEMANTIC_HISTORY,
        "semantic_history_segments": manuscript.MAX_HISTORY_SEGMENTS,
    }


def test_every_valid_case_is_read_and_written_back_to_its_canonical_bytes(contract_source) -> None:
    folder = _folder(contract_source)
    for case in _cases(folder)["valid"]:
        parsed = manuscript.parse_workfile(_content(folder, case))
        canonical = (folder / case["canonical"]).read_text(encoding="utf-8")
        assert manuscript.serialize_workfile(parsed) == canonical, case["file"]
        if parsed["format"] != manuscript.WORKFILE_FORMAT_V3:
            continue
        # The key order of the input must not leak into a v3 file.
        shuffled = dict(reversed(list({
            **parsed, "segments": _reversed_keys(parsed["segments"]),
            "segment_choice_history": _reversed_keys(parsed["segment_choice_history"]),
            "progress": _reversed_keys(parsed["progress"])}.items())))
        assert manuscript.serialize_workfile(shuffled) == canonical, f"{case['file']} (reversed key order)"


def test_every_invalid_case_is_rejected_with_its_code(contract_source) -> None:
    folder = _folder(contract_source)
    for case in _cases(folder)["invalid"]:
        if case["file"] == "invalid-v3-format-v4.json":
            parsed = manuscript.parse_workfile(_content(folder, case))
            assert parsed["format"] == manuscript.WORKFILE_FORMAT_V4
            assert manuscript.serialize_workfile(parsed) == (folder / "valid-v3-all-parts.json").read_text(encoding="utf-8")
            continue
        assert _code(_content(folder, case)) == case["error"], case["file"]


def test_integral_numbers_read_as_int_in_every_v3_field(contract_source) -> None:
    folder = _folder(contract_source)
    parsed = manuscript.parse_workfile((folder / "valid-v3-integral-numbers.json").read_text(encoding="utf-8"))
    assert parsed == manuscript.parse_workfile((folder / "valid-v3-all-parts.json").read_text(encoding="utf-8"))
    record = parsed["segments"][1]
    assert type(record["index"]) is int and type(parsed["segments"][0]["source_start"]) is int
    assert type(record["result"]["evaluated"]) is int and type(record["result"]["findings"][0]["source"][0]) is int
    assert type(parsed["progress"]["completed"][0]) is int and type(parsed["progress"]["current"]) is int
    # A number that is not integral, or not safe, is not an integer.
    text = (folder / "valid-v3-all-parts.json").read_text(encoding="utf-8")
    for bad in ('"index":1.5', '"index":9007199254740993'):
        assert _code(text.replace('"index":1', bad, 1)) == "workfile_segment_invalid", bad


def test_v3_boundaries_that_the_fixtures_do_not_name(contract_source) -> None:
    folder = _folder(contract_source)
    base = json.loads((folder / "valid-v3-all-parts.json").read_text(encoding="utf-8"))

    def code(change) -> str:
        draft = copy.deepcopy(base)
        change(draft)
        return _code(json.dumps(draft, ensure_ascii=False))

    # A negative index must not count from the end of the records.
    assert code(lambda d: d["progress"].update(current=-1)) == "workfile_progress_invalid"
    assert code(lambda d: d["progress"].update(completed=[-1])) == "workfile_progress_invalid"
    # The v1/v2 part keeps its own array cap and counts in a v3 file.
    assert code(lambda d: d.update(semantic_decisions=[0] * 129)) == "workfile_too_many_entries"
    assert code(lambda d: d.update(semantic_decisions=[0] * 128)) == "workfile_invalid_content"
    assert code(lambda d: d.update(choices=[0] * 33)) == "workfile_invalid_content"


def test_load_workfile_reads_v3_files_up_to_44_mib(contract_source, tmp_path: Path) -> None:
    folder = _folder(contract_source)
    cases = _cases(folder)
    over_1_mib = tmp_path / "over-1mib.json"
    over_1_mib.write_bytes((folder / "valid-v3-over-1mib.json").read_bytes())
    assert over_1_mib.stat().st_size > manuscript.MAX_WORKFILE_BYTES
    loaded = manuscript.load_workfile(over_1_mib)
    assert loaded == manuscript.parse_workfile(over_1_mib.read_text(encoding="utf-8"))
    assert loaded["segments"]

    padded = next(c for c in cases["valid"] if c.get("pad_to_bytes") == manuscript.MAX_V3_BYTES)
    limit = tmp_path / "limit.json"
    limit.write_bytes(_content(folder, padded).encode("utf-8"))
    assert limit.stat().st_size == 46_137_344
    assert manuscript.load_workfile(limit) == manuscript.parse_workfile((folder / padded["canonical"]).read_text(encoding="utf-8"))

    limit.write_bytes(limit.read_bytes() + b" ")
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.load_workfile(limit)
    assert error.value.code == "workfile_too_large"

    broken = tmp_path / "broken.json"
    broken.write_bytes(b'{"format": "\xff"}')
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.load_workfile(broken)
    assert error.value.code == "workfile_not_json"


def test_v1_and_v2_payloads_keep_their_bytes(contract_source) -> None:
    v1 = {"source": "a", "target": "b", "direction": "en", "alignments": None, "choices": [], "history": []}
    expected = ('{\n  "format": "wuwaterm-manuscript-v1",\n  "source": "a",\n  "target": "b",\n'
                '  "direction": "en",\n  "alignments": null,\n  "choices": [],\n  "history": []\n}')
    assert manuscript.serialize_workfile(v1) == expected
    # Empty v3 state does not change the format; neither does a progress that nothing completed.
    assert manuscript.serialize_workfile({**v1, "segments": [], "segment_choice_history": [],
                                          "progress": {"current": 0, "completed": []}}) == expected

    v2_file = (contract_source("web") / "contracts" / "manuscript-v2" / "valid-all-parts.json").read_text(encoding="utf-8")
    v2 = manuscript.parse_workfile(v2_file)
    assert v2["format"] == manuscript.WORKFILE_FORMAT_V2
    assert manuscript.serialize_workfile(v2) == v2_file
    assert manuscript.serialize_workfile({**v2, "segments": [], "segment_choice_history": []}) == v2_file


def test_a_payload_over_1_mib_without_segment_records_is_written_as_v3() -> None:
    draft = manuscript.empty_workfile()
    draft["source"] = "字" * 1_100_000  # over the v1/v2 file size, and over their 1,048,576-scalar text limit
    content = manuscript.serialize_workfile(draft)
    assert len(content.encode("utf-8")) > manuscript.MAX_WORKFILE_BYTES
    assert content.startswith('{"format":"wuwaterm-manuscript-v3","source":"字')
    assert content.endswith('"segments":[],"segment_choice_history":[],"progress":{"current":null,"completed":[]}}')
    parsed = manuscript.parse_workfile(content)
    assert parsed["segments"] == [] and parsed["progress"] == {"current": None, "completed": []}
    assert parsed["source"] == draft["source"]


def test_a_payload_over_44_mib_fails_with_workfile_too_large() -> None:
    draft = manuscript.empty_workfile()
    draft["source"] = "x" * manuscript.MAX_V3_BYTES
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.serialize_workfile(draft)
    assert error.value.code == "workfile_too_large"
    assert "44 MiB" in str(error.value)


def test_only_the_oldest_semantic_history_is_dropped_and_only_over_44_mib(contract_source, monkeypatch) -> None:
    folder = _folder(contract_source)
    payload = manuscript.parse_workfile((folder / "valid-v3-with-semantic.json").read_text(encoding="utf-8"))
    snapshot = payload["semantic_history"][0]
    older = {**snapshot, "direction": "zh" if snapshot["direction"] == "en" else "en"}
    payload["semantic_history"] = [older, snapshot]
    both = manuscript.serialize_workfile(payload)
    assert [s["direction"] for s in json.loads(both)["semantic_history"]] == [older["direction"], snapshot["direction"]]

    only_newest = manuscript.serialize_workfile({**payload, "semantic_history": [snapshot]})
    monkeypatch.setattr(manuscript, "MAX_V3_BYTES", len(both.encode("utf-8")) - 1)
    assert manuscript.serialize_workfile(payload) == only_newest
    written = json.loads(only_newest)
    assert written["semantic_decisions"] == payload["semantic_decisions"] and written["segments"]

    without_history = manuscript.serialize_workfile({**payload, "semantic_history": []})
    monkeypatch.setattr(manuscript, "MAX_V3_BYTES", len(without_history.encode("utf-8")) - 1)
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.serialize_workfile(payload)
    assert error.value.code == "workfile_too_large"


def test_the_progress_defaults_and_malformed_records_are_refused(contract_source) -> None:
    folder = _folder(contract_source)
    parsed = manuscript.parse_workfile((folder / "valid-v3-all-parts.json").read_text(encoding="utf-8"))
    pending = {key: value for key, value in parsed.items() if key != "progress"}
    pending["segments"] = [{**r, "status": "pending", "direction": None, "alignment_input": None, "basis": None,
                            "result": None, "choices": [{**c, "confirmed": False} for c in r["choices"]]}
                           for r in parsed["segments"]]
    written = json.loads(manuscript.serialize_workfile(pending))
    assert written["progress"] == {"current": None, "completed": []}

    for change, code in ((lambda d: d["segments"][0].pop("status"), "workfile_segment_invalid"),
                         (lambda d: d["segments"][0].update(index=5), "workfile_segment_order"),
                         (lambda d: d.update(progress={"current": 9, "completed": []}), "workfile_progress_invalid")):
        draft = copy.deepcopy(parsed)
        change(draft)
        with pytest.raises(manuscript.WorkfileError) as error:
            manuscript.serialize_workfile(draft)
        assert error.value.code == code


def test_segment_records_survive_save_and_load_unchanged(contract_source, tmp_path: Path) -> None:
    folder = _folder(contract_source)
    names = ("valid-v3-all-parts.json", "valid-v3-explicit-alignments.json", "valid-v3-with-semantic.json",
             "valid-v3-32-choices.json", "valid-v3-512-history.json", "valid-v3-over-1mib.json")
    for name in names:
        original = manuscript.parse_workfile((folder / name).read_text(encoding="utf-8"))
        assert original["segments"] or original["segment_choice_history"], name
        path = tmp_path / name
        manuscript.save_workfile(path, original)
        assert path.read_bytes() == (folder / name).read_bytes(), name
        loaded = manuscript.load_workfile(path)
        assert loaded == original, name
        assert loaded["segments"] == original["segments"] and loaded["progress"] == original["progress"], name
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)
