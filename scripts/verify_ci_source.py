#!/usr/bin/env python3
"""Require successful main-push CI for the exact source of a release."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED_JOBS = {"Pure manuscript contract", "Windows build and contracts / build and test"}


def verify_record(run: dict, jobs: list[dict], repository: str, source: str, workflow_id: int) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(bool(SHA.fullmatch(source)), "source must be a full commit SHA")
    require(run.get("workflow_id") == workflow_id, "wrong workflow")
    require(run.get("path", "").split("@", 1)[0] == ".github/workflows/ci.yml", "wrong workflow path")
    require(run.get("event") == "push" and run.get("head_branch") == "main", "not a main push")
    require(run.get("head_sha") == source, "CI source does not match release source")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "CI did not succeed")
    for field in ("repository", "head_repository"):
        require((run.get(field) or {}).get("full_name", "").lower() == repository.lower(), "wrong repository")
    by_name = {job.get("name"): job for job in jobs}
    require(len(by_name) == len(jobs), "ambiguous job names")
    require(REQUIRED_JOBS <= by_name.keys(), "missing required job")
    for job in jobs:
        require(job.get("status") == "completed" and job.get("conclusion") == "success", "incomplete or failed CI job")


def gh_json(*args: str):
    return json.loads(subprocess.check_output(["gh", "api", *args], text=True))


def verify_source(source: str, run_id: str | None = None) -> int:
    if not SHA.fullmatch(source):
        raise ValueError("source must be a full commit SHA")
    repository = os.environ["GH_REPO"]
    workflow = gh_json(f"repos/{repository}/actions/workflows/ci.yml")
    if run_id:
        if not run_id.isdecimal() or int(run_id) <= 0:
            raise ValueError("run ID must be a positive integer")
        candidates = [int(run_id)]
    else:
        result = gh_json(f"repos/{repository}/actions/workflows/ci.yml/runs", "--method", "GET",
                         "-f", "branch=main", "-f", "event=push", "-f", f"head_sha={source}",
                         "-F", "per_page=100")
        candidates = [item["id"] for item in result["workflow_runs"]]
    errors = []
    for candidate in candidates:
        try:
            run = gh_json(f"repos/{repository}/actions/runs/{candidate}")
            if run.get("id") != candidate:
                raise ValueError("wrong run ID")
            pages = gh_json(f"repos/{repository}/actions/runs/{candidate}/jobs?per_page=100", "--paginate", "--slurp")
            jobs = [job for page in pages for job in page["jobs"]]
            verify_record(run, jobs, repository, source, workflow["id"])
            return candidate
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
            errors.append(f"{candidate}: {exc}")
    raise ValueError("No successful main-push CI for the exact source: " + ("; ".join(errors) or "no matching run"))


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: verify_ci_source.py SOURCE_SHA [RUN_ID]", file=sys.stderr)
        return 2
    try:
        run_id = verify_source(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"CI source verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"CI run {run_id} validated source {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
