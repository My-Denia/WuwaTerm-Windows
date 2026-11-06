"""Release source checks must reject mismatched or incomplete CI evidence."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("verify_ci_source", Path(__file__).resolve().parents[1] / "scripts" / "verify_ci_source.py")
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


@pytest.mark.parametrize("field,value", [
    ("head_sha", "b" * 40), ("head_branch", "feature"), ("event", "pull_request"),
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
