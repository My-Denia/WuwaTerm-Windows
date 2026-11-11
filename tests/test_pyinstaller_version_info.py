"""Unit tests for the PyInstaller Windows version resource generator."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.pyinstaller_version_info import (
    DEFAULT_FILE_DESCRIPTION,
    DEFAULT_PRODUCT_NAME,
    generate_version_info_file,
    get_package_version,
    parse_version,
    render_version_info,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# == Version parsing =======================================================


@pytest.mark.parametrize(
    ("version_str", "expected"),
    [
        ("0.4.0", (0, 4, 0, 0)),
        ("1.0.0", (1, 0, 0, 0)),
        ("2.15.8", (2, 15, 8, 0)),
        ("0.0.0", (0, 0, 0, 0)),
        ("10.200.3000", (10, 200, 3000, 0)),
        ("65535.65535.65535", (65535, 65535, 65535, 0)),
        ("  0.4.0  \n", (0, 4, 0, 0)),
    ],
)
def test_parse_version_valid(version_str: str, expected: tuple[int, int, int, int]) -> None:
    assert parse_version(version_str) == expected


@pytest.mark.parametrize(
    "invalid_version",
    [
        "0.4",
        "0.4.0.0",
        "v0.4.0",
        "0.4.0-rc1",
        "0.4.0+build",
        "0.4.x",
        "0.4.0.",
        ".0.4.0",
        "",
        "   ",
        "-1.0.0",
        "1.-2.0",
        "65536.0.0",
        "0.70000.0",
        "0.0.100000",
    ],
)
def test_parse_version_invalid_rejected(invalid_version: str) -> None:
    with pytest.raises(ValueError, match="Invalid version string|exceeds 16-bit maximum"):
        parse_version(invalid_version)


@pytest.mark.parametrize("non_string", [None, 123, 4.5, ["0", "4", "0"]])
def test_parse_version_non_string_rejected(non_string: object) -> None:
    with pytest.raises(ValueError, match="Version must be a string"):
        parse_version(non_string)  # type: ignore[arg-type]


# == Package version resolution ============================================


def test_get_package_version_agrees_with_repo() -> None:
    version = get_package_version(REPO_ROOT)
    assert version == "0.4.0"


def test_get_package_version_mismatch_raises(tmp_path: Path) -> None:
    # Set up simulated pyproject.toml and wuwaterm_client with conflicting versions
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.5.0"\n', encoding="utf-8")
    init_file = tmp_path / "src" / "wuwaterm_client" / "__init__.py"
    init_file.parent.mkdir(parents=True)
    init_file.write_text('__version__ = "0.4.0"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Version mismatch"):
        get_package_version(tmp_path)


# == Rendering and AST structure ===========================================


class _MockFixedFileInfo:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _MockStringStruct:
    def __init__(self, key: str, val: str) -> None:
        self.key = key
        self.val = val


class _MockStringTable:
    def __init__(self, name: str, strings: list[_MockStringStruct]) -> None:
        self.name = name
        self.strings = strings


class _MockStringFileInfo:
    def __init__(self, tables: list[_MockStringTable]) -> None:
        self.tables = tables


class _MockVarStruct:
    def __init__(self, name: str, values: list[int]) -> None:
        self.name = name
        self.values = values


class _MockVarFileInfo:
    def __init__(self, vars_: list[_MockVarStruct]) -> None:
        self.vars = vars_


class _MockVSVersionInfo:
    def __init__(self, ffi: _MockFixedFileInfo, kids: list[object]) -> None:
        self.ffi = ffi
        self.kids = kids


def _eval_version_info(text: str) -> _MockVSVersionInfo:
    eval_env = {
        "VSVersionInfo": _MockVSVersionInfo,
        "FixedFileInfo": _MockFixedFileInfo,
        "StringFileInfo": _MockStringFileInfo,
        "StringTable": _MockStringTable,
        "StringStruct": _MockStringStruct,
        "VarFileInfo": _MockVarFileInfo,
        "VarStruct": _MockVarStruct,
    }
    return eval(text, eval_env)


def test_render_version_info_structure() -> None:
    rendered = render_version_info("0.4.0")
    info = _eval_version_info(rendered)

    # FixedFileInfo
    assert info.ffi.filevers == (0, 4, 0, 0)
    assert info.ffi.prodvers == (0, 4, 0, 0)
    assert info.ffi.mask == 0x3F
    assert info.ffi.flags == 0x0
    assert info.ffi.OS == 0x40004
    assert info.ffi.fileType == 0x1

    # Kids
    assert len(info.kids) == 2
    string_file_info = info.kids[0]
    assert isinstance(string_file_info, _MockStringFileInfo)
    assert len(string_file_info.tables) == 1
    table = string_file_info.tables[0]
    assert table.name == "040904b0"

    strings_dict = {s.key: s.val for s in table.strings}
    assert strings_dict["ProductName"] == DEFAULT_PRODUCT_NAME
    assert strings_dict["FileVersion"] == "0.4.0"
    assert strings_dict["ProductVersion"] == "0.4.0"
    assert strings_dict["FileDescription"] == DEFAULT_FILE_DESCRIPTION
    assert strings_dict["InternalName"] == "WuwaTerm"
    assert strings_dict["OriginalFilename"] == "WuwaTerm.exe"

    # CompanyName must be omitted
    assert "CompanyName" not in strings_dict

    var_file_info = info.kids[1]
    assert isinstance(var_file_info, _MockVarFileInfo)
    assert len(var_file_info.vars) == 1
    assert var_file_info.vars[0].name == "Translation"
    assert var_file_info.vars[0].values == [1033, 1200]


def test_render_version_info_default_resolves_package() -> None:
    rendered = render_version_info(root=REPO_ROOT)
    assert "'FileVersion', '0.4.0'" in rendered
    assert "'ProductVersion', '0.4.0'" in rendered
    assert "filevers=(0, 4, 0, 0)" in rendered


# == File generation =======================================================


def test_generate_version_info_file_writes_path(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "version_info.txt"
    written = generate_version_info_file(out, version="1.2.3")
    assert written == out
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "'FileVersion', '1.2.3'" in content
    assert "filevers=(1, 2, 3, 0)" in content


# == CLI integration =======================================================


def test_cli_stdout() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/pyinstaller_version_info.py", "--version", "2.0.1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "'FileVersion', '2.0.1'" in result.stdout
    assert "filevers=(2, 0, 1, 0)" in result.stdout


def test_cli_file_output(tmp_path: Path) -> None:
    out_file = tmp_path / "cli_ver.txt"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/pyinstaller_version_info.py",
            str(out_file),
            "--version",
            "3.1.4",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Version info resource written to:" in result.stdout
    assert out_file.is_file()
    assert "'FileVersion', '3.1.4'" in out_file.read_text(encoding="utf-8")


def test_cli_invalid_version_fails() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/pyinstaller_version_info.py", "--version", "bad-ver"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Error generating version info:" in result.stderr
