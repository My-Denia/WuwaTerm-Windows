"""Release source checks must reject mismatched or incomplete CI evidence."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("verify_ci_source", ROOT / "scripts" / "verify_ci_source.py")
assert spec and spec.loader
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

SOURCE = "a" * 40
REPOSITORY = "My-Denia/WuwaTerm-Windows"


def record():
    run = {"workflow_id": 12, "path": ".github/workflows/ci.yml", "event": "push", "head_branch": "main",
           "head_sha": SOURCE, "status": "completed", "conclusion": "success",
           "repository": {"full_name": REPOSITORY}, "head_repository": {"full_name": REPOSITORY}}
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in sorted(verify.REQUIRED_JOBS)]
    return run, jobs


def test_valid_main_push_accepts_exact_source():
    run, jobs = record()
    verify.verify_record(run, jobs, REPOSITORY, SOURCE, 12)


def test_main_dispatch_run_for_the_exact_source_is_accepted():
    # ci.yml is dispatched on main for a documentation commit whose push run
    # was skipped; its head SHA still has to be the release source.
    run, jobs = record()
    run["event"] = "workflow_dispatch"
    verify.verify_record(run, jobs, REPOSITORY, SOURCE, 12)


def test_dispatch_run_for_another_commit_is_rejected():
    run, jobs = record()
    run["event"] = "workflow_dispatch"
    run["head_sha"] = "b" * 40
    with pytest.raises(ValueError):
        verify.verify_record(run, jobs, REPOSITORY, SOURCE, 12)


@pytest.mark.parametrize("field,value", [
    ("head_sha", "b" * 40), ("head_branch", "feature"), ("event", "pull_request"),
    ("event", "schedule"),
    ("conclusion", "failure"), ("status", "in_progress"), ("workflow_id", 13),
    ("path", ".github/workflows/other.yml"),
    ("repository", {"full_name": "other/repo"}), ("head_repository", {"full_name": "other/repo"}),
])
def test_untrusted_or_incomplete_run_is_rejected(field, value):
    run, jobs = record()
    run[field] = value
    with pytest.raises(ValueError):
        verify.verify_record(run, jobs, REPOSITORY, SOURCE, 12)


def test_missing_failed_and_ambiguous_jobs_are_rejected():
    run, jobs = record()
    cases = [jobs[:-1], copy.deepcopy(jobs), jobs + [copy.deepcopy(jobs[0])]]
    cases[1][0]["conclusion"] = "failure"
    for bad in cases:
        with pytest.raises(ValueError):
            verify.verify_record(run, bad, REPOSITORY, SOURCE, 12)


def test_docs_only_commits_skip_the_windows_build_and_stay_dispatchable():
    """A documentation commit must not spend windows-latest minutes.

    A release bumps pyproject.toml and src/wuwaterm_client/__init__.py, which
    are not ignored, so this filter cannot hide a release source from CI.
    """
    workflow = yaml.load((ROOT / ".github" / "workflows" / "ci.yml").read_text(), Loader=yaml.BaseLoader)
    triggers = workflow["on"]
    ignored = {"README.md", "README.*.md", "CHANGELOG.md", "docs/**", "provenance/**"}

    assert triggers["push"]["branches"] == ["main"]
    assert set(triggers["push"]["paths-ignore"]) == ignored
    assert set(triggers["pull_request"]["paths-ignore"]) == ignored
    # Without this trigger a skipped documentation commit has no full run to
    # hand release.yml, and verify_ci_source.py could never accept one.
    assert "workflow_dispatch" in triggers
