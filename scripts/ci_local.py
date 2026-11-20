"""Run locally the blocking gates of .github/workflows/ci.yml, in order, and stop at the first failure.

    python -m scripts.ci_local          # ruff, mypy, bandit, pip-audit, pytest (the jobs `quality` and `test`)
    python -m scripts.ci_local --full   # plus promtool and the Docker build, when Docker is available (before the final push)

The commands are the ones of the workflow (keep them in step with it: tests/test_ci_gates.py reads the workflow, not this file).
Every tool runs as `python -m <tool>` with the interpreter that runs this script, so it works the same on Windows, Linux and macOS.
A step that cannot run (no Docker, no network for pip-audit) is a FAILURE for the gates of `quality` and `test`, and is reported as SKIPPED,
never as passed, for the optional Docker steps of --full. Exit code 0 only if every step that ran passed.
"""

from __future__ import annotations

import argparse
import os
import shutil
# bandit: only fixed command lists of the project run here, and no user input reaches them
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# Accepted advisories: the same four ids as the `pip-audit` step of ci.yml (ChromaDB's SERVER mode, which this project does not use).
PIP_AUDIT_IGNORED = ("PYSEC-2026-311", "PYSEC-2026-3813", "PYSEC-2026-3814", "PYSEC-2026-3815")


@dataclass
class Step:
    name: str
    cmd: list[str]
    env: dict[str, str] | None = None


def core_steps() -> list[Step]:
    """The gates of the CI jobs `quality` and `test`, in the workflow's order."""
    audit = [PY, "-m", "pip_audit", "-r", "constraints.txt", "--no-deps", "--disable-pip", "--progress-spinner", "off"]
    for vuln in PIP_AUDIT_IGNORED:
        audit += ["--ignore-vuln", vuln]
    return [
        Step("ruff", [PY, "-m", "ruff", "check", "."]),
        Step("mypy", [PY, "-m", "mypy"]),
        Step("bandit", [PY, "-m", "bandit", "-r", "app", "scripts", "-ll", "-q"]),
        Step("pip-audit", audit),
        Step("pytest", [PY, "-m", "pytest", "-q", "-m", "not eval"]),
    ]


def _git_revision() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)  # nosec B603 B607
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def docker_steps() -> list[Step]:
    """The optional steps of --full: the job `alerts` and the job `docker` (without trivy, which needs the image digest pinned in the workflow)."""
    obs = str(ROOT / "infra" / "observability")
    prom = ["docker", "run", "--rm", "--entrypoint", "promtool", "-v", f"{obs}:/w", "prom/prometheus:v3.0.1"]
    rev = _git_revision()
    steps = [
        Step("promtool check rules", prom + ["check", "rules", "/w/alerts.yml"]),
        Step("promtool test rules", prom + ["test", "rules", "/w/alerts_test.yml"]),
        Step(
            "docker build",
            ["docker", "build", "--build-arg", f"REVISION={rev}", "--build-arg", "VERSION=local", "-t", "getnet-agents:local", "."],
        ),
        Step("container is not root (uid 10001)", ["docker", "run", "--rm", "--entrypoint", "id", "getnet-agents:local", "-u"]),
    ]
    # the compose services read `.env` (not in git): the check only runs where it exists, and it is never created or overwritten here
    if (ROOT / ".env").exists():
        env = {"KEYCLOAK_ADMIN_PASSWORD": "ci", "DEMO_USER_PASSWORD": "ci"}
        steps.append(Step("compose config", ["docker", "compose", "config", "-q"], env))
        steps.append(Step("compose config (observability)", ["docker", "compose", "--profile", "observability", "config", "-q"], env))
    return steps


def run_step(step: Step) -> tuple[int, float]:
    print(f"\n=== {step.name}: {' '.join(step.cmd)}", flush=True)
    env = dict(os.environ, PYTHONUTF8="1")
    if step.env:
        env.update(step.env)
    started = time.monotonic()
    try:
        code = subprocess.run(step.cmd, cwd=ROOT, env=env).returncode  # nosec B603
    except OSError as exc:
        print(f"could not start {step.cmd[0]}: {exc}", file=sys.stderr)
        code = 127
    return code, time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the CI gates locally and stop at the first failure.")
    parser.add_argument("--full", action="store_true", help="also run promtool and the Docker build when Docker is available")
    args = parser.parse_args(argv)

    results: list[tuple[str, str, float]] = []

    def summary() -> None:
        print("\n--- summary ---")
        for name, status, secs in results:
            print(f"{status:<8} {name} ({secs:.1f} s)")

    for step in core_steps():
        code, secs = run_step(step)
        if code != 0:
            results.append((step.name, "FAILED", secs))
            summary()
            print(f"\nci_local: STOPPED at '{step.name}' (exit code {code}). The CI would fail here too.", file=sys.stderr)
            return code or 1
        results.append((step.name, "ok", secs))

    if args.full:
        if shutil.which("docker") is None:
            for step in docker_steps():
                results.append((step.name, "SKIPPED", 0.0))
            summary()
            print("\nci_local: core gates passed; --full steps SKIPPED because `docker` is not on the PATH (not a pass).")
            return 0
        for step in docker_steps():
            code, secs = run_step(step)
            if code != 0:
                results.append((step.name, "FAILED", secs))
                summary()
                print(f"\nci_local: STOPPED at '{step.name}' (exit code {code}). The CI would fail here too.", file=sys.stderr)
                return code or 1
            results.append((step.name, "ok", secs))

    summary()
    print("\nci_local: every step that ran passed." + ("" if args.full else " (run with --full for promtool and Docker)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
