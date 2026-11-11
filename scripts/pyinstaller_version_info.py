#!/usr/bin/env python3
"""Generate PyInstaller Windows version resource from package version.

Single version source: wuwaterm_client.__version__ / pyproject.toml.
Emits a Python literal suitable for PyInstaller's `version=` parameter in
EXE(...) specs, containing VSVersionInfo, FixedFileInfo, and StringFileInfo.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
DEFAULT_FILE_DESCRIPTION = "WuwaTerm desktop client"
DEFAULT_PRODUCT_NAME = "WuwaTerm"
DEFAULT_INTERNAL_NAME = "WuwaTerm"
DEFAULT_ORIGINAL_FILENAME = "WuwaTerm.exe"


def parse_version(version_str: str) -> tuple[int, int, int, int]:
    """Parse a strict 'X.Y.Z' semantic version into a 4-tuple (X, Y, Z, 0).

    Rejects anything other than exactly three non-negative integer components
    bounded by Windows 16-bit WORD limits (0..65535).
    """
    if not isinstance(version_str, str):
        raise ValueError(
            f"Version must be a string, got {type(version_str).__name__}: {version_str!r}"
        )

    stripped = version_str.strip()
    match = _VERSION_RE.fullmatch(stripped)
    if not match:
        raise ValueError(
            f"Invalid version string {version_str!r}: must follow strict 'X.Y.Z' format with non-negative integers"
        )

    parts = [int(p) for p in match.groups()]
    for p in parts:
        if p > 0xFFFF:
            raise ValueError(
                f"Version component {p} in {version_str!r} exceeds 16-bit maximum (65535)"
            )

    return (parts[0], parts[1], parts[2], 0)


def get_package_version(root: Path | None = None) -> str:
    """Resolve the package version from pyproject.toml and wuwaterm_client.

    Ensures the two definitions agree if both are present.
    """
    if root is None:
        root = Path(__file__).resolve().parents[1]

    pyproject_path = root / "pyproject.toml"
    init_path = root / "src" / "wuwaterm_client" / "__init__.py"

    pyproject_version: str | None = None
    if pyproject_path.is_file():
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        raw = data.get("project", {}).get("version")
        if raw is not None:
            pyproject_version = str(raw).strip()

    init_version: str | None = None
    if init_path.is_file():
        match = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            init_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            init_version = match.group(1).strip()

    if (
        pyproject_version is not None
        and init_version is not None
        and pyproject_version != init_version
    ):
        raise ValueError(
            f"Version mismatch: pyproject.toml has {pyproject_version!r} "
            f"but wuwaterm_client has {init_version!r}"
        )

    version = pyproject_version or init_version
    if not version:
        raise RuntimeError(f"Could not determine package version from {root}")
    return version


def render_version_info(
    version: str | None = None,
    *,
    file_description: str = DEFAULT_FILE_DESCRIPTION,
    product_name: str = DEFAULT_PRODUCT_NAME,
    internal_name: str = DEFAULT_INTERNAL_NAME,
    original_filename: str = DEFAULT_ORIGINAL_FILENAME,
    root: Path | None = None,
) -> str:
    """Render PyInstaller Windows version resource string.

    The format is canonical PyInstaller VSVersionInfo structure deserialized by
    PyInstaller via eval().
    """
    if version is None:
        version = get_package_version(root=root)

    filevers = parse_version(version)
    prodvers = filevers
    version_str = version.strip()

    # Note: CompanyName is intentionally omitted ("CompanyName-less minimal honest metadata").
    return f"""# UTF-8
# Windows version resource generated at build time.
# Single source of truth: wuwaterm_client package version ({version_str}).

VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={prodvers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904b0',
          [
            StringStruct('FileDescription', {file_description!r}),
            StringStruct('FileVersion', {version_str!r}),
            StringStruct('InternalName', {internal_name!r}),
            StringStruct('OriginalFilename', {original_filename!r}),
            StringStruct('ProductName', {product_name!r}),
            StringStruct('ProductVersion', {version_str!r}),
          ],
        ),
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def generate_version_info_file(
    output_path: str | Path,
    version: str | None = None,
    *,
    file_description: str = DEFAULT_FILE_DESCRIPTION,
    product_name: str = DEFAULT_PRODUCT_NAME,
    internal_name: str = DEFAULT_INTERNAL_NAME,
    original_filename: str = DEFAULT_ORIGINAL_FILENAME,
    root: Path | None = None,
) -> Path:
    """Render and write the version resource to output_path."""
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_version_info(
        version,
        file_description=file_description,
        product_name=product_name,
        internal_name=internal_name,
        original_filename=original_filename,
        root=root,
    )
    target.write_text(content, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a PyInstaller Windows version resource from package version."
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=None,
        help="Path to write version info file to (stdout if omitted)",
    )
    parser.add_argument(
        "--version",
        dest="version",
        default=None,
        help="Explicit version string (defaults to wuwaterm_client package version)",
    )
    args = parser.parse_args()

    try:
        if args.output:
            out = generate_version_info_file(args.output, version=args.version)
            print(f"Version info resource written to: {out}")
        else:
            sys.stdout.write(render_version_info(version=args.version))
    except Exception as exc:
        print(f"Error generating version info: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
