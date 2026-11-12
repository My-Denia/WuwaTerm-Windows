"""Prove the advisory gate fails on high and critical, and only on those.

Also proves the gate can tell a clean report from a report that only read part
of the locked set, which is the whole reason `--export` exists.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-audit.yml"

spec = importlib.util.spec_from_file_location(
    "check_dependency_advisories", ROOT / "scripts" / "check_dependency_advisories.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

CRITICAL = {"GHSA-6q48-6c7w-9gw2": {"database_specific": {"severity": "CRITICAL"}}}
HIGH = {"GHSA-h5c8-rqwp-cp95": {"database_specific": {"severity": "HIGH"}}}
MODERATE = {"GHSA-9wx4-h78v-vm56": {"database_specific": {"severity": "MODERATE"}}}
LOW = {"GHSA-aaaa-bbbb-cccc": {"database_specific": {"severity": "LOW"}}}

# A platform marker that selects nothing on any host running this suite. A pin
# narrowed to win32 or darwin cannot be used here: the same file runs on Linux
# and on the Windows build job, and on Windows it would apply.
OTHER_PLATFORM = "sys_platform == 'sunos5'"


class Osv:
    """Stand-in for the OSV lookup that records what was asked."""

    def __init__(self, records=None, error=None):
        self.records = records or {}
        self.error = error
        self.asked = []

    def __call__(self, vuln_id):
        self.asked.append(vuln_id)
        if self.error is not None:
            raise gate.AuditError(self.error)
        return self.records.get(vuln_id)


def _vuln(advisory_id, fix_versions=(), aliases=()):
    return {
        "id": advisory_id,
        "fix_versions": list(fix_versions),
        "aliases": list(aliases),
        "description": "fixture advisory",
    }


def _dependency(name, version="1.0", vulns=(), skip_reason=None):
    entry = {"name": name, "version": version, "vulns": list(vulns)}
    if skip_reason is not None:
        entry["skip_reason"] = skip_reason
    return entry


def _report(tmp_path, dependencies, name="pip-audit.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"dependencies": dependencies, "fixes": []}))
    return path


def _export(tmp_path, pins, name="requirements-locked.txt"):
    path = tmp_path / name
    path.write_text("".join(f"{line}\n" for line in pins))
    return path


def test_clean_report_passes_without_asking_osv(tmp_path, capsys):
    osv = Osv()
    report = _report(tmp_path, [_dependency("httpx", "0.28.4")])

    assert gate.main([str(report)], fetch=osv) == 0
    assert osv.asked == []
    output = capsys.readouterr().out
    assert "1 package audited" in output
    assert "no advisories" in output
    assert "PASSED" in output


def test_critical_advisory_blocks_and_names_the_fix(tmp_path, capsys):
    osv = Osv(CRITICAL)
    report = _report(
        tmp_path,
        [
            _dependency(
                "certifi",
                "2026.6.17",
                [_vuln("GHSA-6q48-6c7w-9gw2", ["2026.10.19"])],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 1
    captured = capsys.readouterr()
    assert "critical" in captured.out
    assert "certifi" in captured.out
    assert "2026.6.17" in captured.out
    assert "GHSA-6q48-6c7w-9gw2" in captured.out
    assert "fix: 2026.10.19" in captured.out
    assert "::error title=certifi GHSA-6q48-6c7w-9gw2::" in captured.out
    assert "FAILED: 1 blocking advisory" in captured.err


def test_high_advisory_blocks(tmp_path):
    osv = Osv(HIGH)
    report = _report(
        tmp_path,
        [_dependency("jinja2", "3.1.4", [_vuln("GHSA-h5c8-rqwp-cp95", ["3.1.5"])])],
    )

    assert gate.main([str(report)], fetch=osv) == 1


def test_moderate_and_low_are_reported_but_do_not_block(tmp_path, capsys):
    osv = Osv({**MODERATE, **LOW})
    report = _report(
        tmp_path,
        [
            _dependency(
                "requests",
                "2.31.0",
                [
                    _vuln("GHSA-9wx4-h78v-vm56", ["2.32.0"]),
                    _vuln("GHSA-aaaa-bbbb-cccc"),
                ],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 0
    captured = capsys.readouterr()
    assert "moderate" in captured.out
    assert "low" in captured.out
    assert "GHSA-9wx4-h78v-vm56" in captured.out
    assert "no fixed version published" in captured.out
    assert "::error" not in captured.out
    assert "PASSED" in captured.out


def test_an_advisory_reported_twice_is_one_finding(tmp_path, capsys):
    osv = Osv(HIGH)
    duplicate = _vuln("GHSA-h5c8-rqwp-cp95", ["3.1.5"])
    report = _report(
        tmp_path, [_dependency("jinja2", "3.1.4", [duplicate, dict(duplicate)])]
    )

    assert gate.main([str(report)], fetch=osv) == 1
    assert osv.asked == ["GHSA-h5c8-rqwp-cp95"]
    output = capsys.readouterr().out
    assert "totals: 1 advisory (1 high)" in output


def test_severity_comes_from_the_ghsa_alias_when_the_primary_has_none(tmp_path):
    """A PYSEC record carries a CVSS vector, not GitHub's label."""
    osv = Osv(
        {
            "PYSEC-2026-4320": {"severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/x"}]},
            **CRITICAL,
        }
    )
    report = _report(
        tmp_path,
        [
            _dependency(
                "certifi",
                "2026.6.17",
                [_vuln("PYSEC-2026-4320", ["2026.10.18"], ["GHSA-6q48-6c7w-9gw2"])],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 1
    # The GHSA is asked for first precisely so the PYSEC record - which has no
    # label to read - is not needed.
    assert osv.asked == ["GHSA-6q48-6c7w-9gw2"]


def test_records_sharing_a_cve_are_one_vulnerability(tmp_path, capsys):
    osv = Osv({"PYSEC-2024-38": {"database_specific": {"severity": "HIGH"}}})
    report = _report(
        tmp_path,
        [
            _dependency(
                "python-multipart",
                "0.0.6",
                [
                    _vuln("PYSEC-2024-38", ["0.0.7"], ["CVE-2024-24762", "PYSEC-2026-1850"]),
                    _vuln("PYSEC-2026-1850", ["0.0.7"], ["CVE-2024-24762"]),
                ],
            )
        ],
    )

    assert gate.main([str(report)], fetch=osv) == 1
    output = capsys.readouterr().out
    assert "totals: 1 advisory (1 high)" in output
    # The second record survives as an identifier on the first row, not as a
    # row of its own.
    assert output.count("python-multipart  0.0.6") == 1
    assert "PYSEC-2026-1850" in output


def test_grouping_keeps_other_packages_apart():
    shared = ("PYSEC-1", ("CVE-9",), ("2.0",))

    grouped = gate.group_advisories(
        [gate.Advisory("a", "1.0", *shared), gate.Advisory("b", "1.0", *shared)]
    )

    assert [advisory.package for advisory in grouped] == ["a", "b"]


def test_unreadable_severity_blocks(tmp_path, capsys):
    osv = Osv({})
    report = _report(tmp_path, [_dependency("certifi", "2026.6.17", [_vuln("PYSEC-1")])])

    assert gate.main([str(report)], fetch=osv) == 1
    captured = capsys.readouterr()
    assert "severity could not be read from OSV" in captured.out
    assert "unknown" in captured.out


def test_lookup_order_prefers_the_ghsa_identifier():
    advisory = gate.Advisory(
        "pkg", "1.0", "PYSEC-1", ("CVE-2", "GHSA-abc-def-ghi"), ()
    )

    assert gate.lookup_order(advisory) == ("GHSA-abc-def-ghi", "PYSEC-1", "CVE-2")


@pytest.mark.parametrize(
    "audit_exit, dependencies, fragment",
    [
        (0, [], "lists no audited dependencies"),
        (2, [_dependency("httpx")], "the audit did not complete"),
        (1, [_dependency("httpx")], "report lists no advisories"),
        (0, [_dependency("httpx", skip_reason="unresolvable")], "could not audit"),
    ],
)
def test_untrustworthy_audits_are_red_not_green(tmp_path, capsys, audit_exit, dependencies, fragment):
    report = _report(tmp_path, dependencies)

    assert gate.main([str(report), "--audit-exit", str(audit_exit)], fetch=Osv()) == 2
    assert fragment in capsys.readouterr().err


def test_missing_report_is_red(tmp_path, capsys):
    assert gate.main([str(tmp_path / "absent.json")], fetch=Osv()) == 2
    assert "could not be read" in capsys.readouterr().err


def test_malformed_report_is_red(tmp_path, capsys):
    path = tmp_path / "pip-audit.json"
    path.write_text(json.dumps({"dependencies": [{"name": "httpx"}]}))

    assert gate.main([str(path)], fetch=Osv()) == 2
    assert "without name or version" in capsys.readouterr().err


def test_osv_lookup_failure_is_red(tmp_path, capsys):
    osv = Osv(error="OSV lookup for PYSEC-1 failed with HTTP 503")
    report = _report(tmp_path, [_dependency("certifi", "1.0", [_vuln("PYSEC-1")])])

    assert gate.main([str(report)], fetch=osv) == 2
    assert "HTTP 503" in capsys.readouterr().err


def test_a_pin_the_report_does_not_cover_is_red(tmp_path, capsys):
    """A report that read part of the locked set is not a clean result."""
    export = _export(tmp_path, ["httpx==0.28.1", "qasync==0.28.0"])
    report = _report(tmp_path, [_dependency("httpx", "0.28.1")])

    assert gate.main([str(report), "--export", str(export)], fetch=Osv()) == 2
    error = capsys.readouterr().err
    assert "does not cover 1 pinned requirement" in error
    assert "qasync 0.28.0" in error


def test_the_bootstrap_blind_spot_is_printed_and_does_not_block(tmp_path, capsys):
    """pip-audit never reports a distribution its own venv already satisfies.

    Measured on the real lock: `setuptools` and `packaging` are pinned and
    absent from the report. Failing on that would leave the gate permanently
    red with no fix path, so it is printed as a warning on every run instead.
    """
    export = _export(
        tmp_path, ["httpx==0.28.1", "packaging==26.3", "setuptools==84.2.1"]
    )
    report = _report(tmp_path, [_dependency("httpx", "0.28.1")])

    assert gate.main([str(report), "--export", str(export)], fetch=Osv()) == 0
    output = capsys.readouterr().out
    assert "PASSED" in output
    assert "3 locked requirements apply to this platform" in output
    assert "packaging 26.3, setuptools 84.2.1" in output
    assert "::warning title=Dependency audit::" in output


def test_a_pin_for_another_platform_is_not_a_gap(tmp_path, capsys):
    export = _export(
        tmp_path, [f"colorama==0.4.6 ; {OTHER_PLATFORM}", "httpx==0.28.1"]
    )
    report = _report(tmp_path, [_dependency("httpx", "0.28.1")])

    assert gate.main([str(report), "--export", str(export)], fetch=Osv()) == 0
    output = capsys.readouterr().out
    assert "1 locked requirements apply to this platform; 1 select another one" in output


def test_an_unreadable_export_is_red(tmp_path, capsys):
    export = _export(tmp_path, ["httpx==0.28.1", "this is not a pin"])
    report = _report(tmp_path, [_dependency("httpx", "0.28.1")])

    assert gate.main([str(report), "--export", str(export)], fetch=Osv()) == 2
    assert "unreadable line" in capsys.readouterr().err


def test_an_export_with_no_applicable_pin_is_red(tmp_path, capsys):
    export = _export(tmp_path, [f"colorama==0.4.6 ; {OTHER_PLATFORM}"])
    report = _report(tmp_path, [_dependency("httpx", "0.28.1")])

    assert gate.main([str(report), "--export", str(export)], fetch=Osv()) == 2
    assert "selects no requirements for this platform" in capsys.readouterr().err


def _workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def _job():
    return _workflow()["jobs"]["dependency-audit"]


def _step(name):
    matches = [step for step in _job()["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}"
    return matches[0]


def test_triggers_are_restrained():
    triggers = _workflow()["on"]

    assert set(triggers) == {"push", "pull_request", "schedule", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    paths = triggers["pull_request"]["paths"]
    assert "pyproject.toml" in paths
    assert "uv.lock" in paths
    assert "scripts/check_dependency_advisories.py" in paths
    assert ".github/workflows/dependency-audit.yml" in paths
    # A dependency-wide scan on every pull request is the noise this avoids.
    assert "src/**" not in paths
    weekly = triggers["schedule"]
    assert len(weekly) == 1
    minute, hour, day, month, weekday = weekly[0]["cron"].split()
    assert (day, month) == ("*", "*")
    assert weekday != "*"


def test_the_audit_reads_the_lock_and_pins_the_scanner():
    export = _step("Export the locked dependency set")["run"]
    assert "uv export" in export
    for flag in (
        "--frozen",
        "--format requirements-txt",
        "--no-hashes",
        "--all-extras",
        "--no-emit-project",
    ):
        assert flag in export
    assert "uv lock" not in export
    # The export is checked before it is audited, not trusted afterwards.
    assert "pyside6" in export
    assert "exit 1" in export

    audit = _step("Audit the locked set")["run"]
    assert "pip-audit==2.10.1" in audit
    assert "--no-project" in audit, "uv run must not sync the project into the audit env"
    assert "-r requirements-locked.txt" in audit
    assert "--format json" in audit
    assert "pip_audit" in audit


def test_the_gate_decides_and_the_report_is_kept():
    gate_step = _step("Fail on critical, high or unreadable advisories")
    assert "scripts/check_dependency_advisories.py" in gate_step["run"]
    assert "--audit-exit" in gate_step["run"]
    # Without the export the gate cannot tell a partial report from a clean one.
    assert "--export requirements-locked.txt" in gate_step["run"]

    keep = _step("Keep the audit report")
    assert keep["if"] == "always()"
    assert "pip-audit.json" in keep["with"]["path"]
    # Artifact names must be unique inside one run, and a failed job gets
    # re-run once the advisory is fixed; without the attempt the re-run fails
    # on the upload instead of reporting the new audit.
    assert keep["with"]["name"].endswith("-${{ github.run_attempt }}")

    assert _workflow()["permissions"] == {"contents": "read"}
    assert int(_job()["timeout-minutes"]) <= 15
