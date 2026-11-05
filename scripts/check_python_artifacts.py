#!/usr/bin/env python3
"""Check the built wheel and source archive retain runtime resources."""
from __future__ import annotations

import email.parser
from pathlib import Path
import tarfile
import tomllib
import zipfile

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
version = project["version"]
resources = root / "src" / "wuwaterm_client" / "resources"
expected = {p.relative_to(resources).as_posix(): p.read_bytes() for p in resources.rglob("*") if p.is_file()}
with zipfile.ZipFile(root / "dist" / f"wuwaterm_client-{version}-py3-none-any.whl") as wheel:
    prefix = "wuwaterm_client/resources/"
    actual = {name[len(prefix):]: wheel.read(name) for name in wheel.namelist() if name.startswith(prefix) and not name.endswith("/")}
    assert actual == expected, "wheel resources or license notices differ from source"
    metadata = email.parser.BytesParser().parsebytes(wheel.read(f"wuwaterm_client-{version}.dist-info/METADATA"))
    assert metadata["Version"] == version and metadata["Requires-Python"] == project["requires-python"]
    dependencies = metadata.get_all("Requires-Dist", [])
    assert not any(d.lower().startswith(("wuwaterm ", "wuwaterm;", "python-telegram-bot")) for d in dependencies)
    assert wheel.read(f"wuwaterm_client-{version}.dist-info/licenses/LICENSE") == (root / "LICENSE").read_bytes()
with tarfile.open(root / "dist" / f"wuwaterm_client-{version}.tar.gz", "r:gz") as archive:
    prefix = f"wuwaterm_client-{version}/"
    for name in ("LICENSE", "main.py", "WuwaTerm.spec", "build.ps1", "uv.lock", "contracts/sources.json", "provenance/client-source.json"):
        member = archive.extractfile(prefix + name)
        assert member is not None and member.read() == (root / name).read_bytes(), f"source archive differs: {name}"
    for name, data in expected.items():
        member = archive.extractfile(prefix + "src/wuwaterm_client/resources/" + name)
        assert member is not None and member.read() == data, f"source archive resource differs: {name}"
print(f"wheel and sdist contain {len(expected)} exact runtime resources and their licenses")
