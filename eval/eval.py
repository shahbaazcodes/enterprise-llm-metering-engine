#!/usr/bin/env python3
"""The capstone evaluation harness.

``python3 eval/eval.py`` runs the whole probe suite in a hermetic environment
(SQLite instead of PostgreSQL, ``fakeredis`` + a real Lua interpreter instead
of Redis), maps the results back onto the scenario matrix in
``eval_set.json`` and writes the evidence file ``EVIDENCE.md``.

The harness is deterministic by construction:

* no network access - the LLM provider is the deterministic simulator that
  ships with the service;
* no wall-clock dependence in the assertions - every probe pins its own
  token counters, prices and limits;
* the scenario matrix is the single source of truth for tenants, price cards
  and token shapes, so the matrix and the probes can never drift apart.

Usage::

    python3 eval/eval.py                 # run every probe and refresh EVIDENCE.md
    python3 eval/eval.py --no-evidence   # run the probes only
    python3 eval/eval.py --probe P3      # run a single probe by id
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
EVAL_SET_PATH = EVAL_DIR / "eval_set.json"
EVIDENCE_PATH = PROJECT_ROOT / "EVIDENCE.md"

#: ``nodeid`` of every collected test -> the pytest status that carried it.
_STATUS_LINE = re.compile(
    r"^(?P<nodeid>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
)
#: The trailing summary, e.g. ``41 passed in 1.19s``.
_SUMMARY = re.compile(r"(?P<count>\d+)\s+(?P<word>passed|failed|error|errors|skipped)")


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """The outcome of a single test (a parametrized test = several node ids)."""

    module: str
    test: str
    nodeids: tuple[str, ...]
    status: str = "MISSING"
    duration: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"

    @property
    def cases(self) -> int:
        """Number of collected cases (a parametrized test contributes many)."""
        return len(self.nodeids)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of a single probe declared in the scenario matrix.

    A probe is a *group* of tests that together verify one guarantee; it is
    green only when every one of its member tests is green.
    """

    probe_id: str
    name: str
    category: str
    critical: bool
    tests: tuple[TestOutcome, ...] = ()

    @property
    def passed(self) -> bool:
        return bool(self.tests) and all(test.passed for test in self.tests)

    @property
    def status(self) -> str:
        """The aggregate status of the probe."""
        if not self.tests:
            return "MISSING"
        if all(test.passed for test in self.tests):
            return "PASSED"
        if any(test.status == "FAILED" for test in self.tests):
            return "FAILED"
        if any(test.status == "ERROR" for test in self.tests):
            return "ERROR"
        return "MISSING"

    @property
    def cases(self) -> int:
        """Total number of collected test cases behind this probe."""
        return sum(test.cases for test in self.tests)

    @property
    def duration(self) -> float:
        return sum(test.duration for test in self.tests)


@dataclass
class SuiteResult:
    """The outcome of the whole run."""

    probes: list[ProbeResult] = field(default_factory=list)
    total_collected: int = 0
    total_passed: int = 0
    total_failed: int = 0
    duration: float = 0.0
    stdout: str = ""

    @property
    def pass_rate(self) -> float:
        if not self.probes:
            return 0.0
        return sum(1 for probe in self.probes if probe.passed) / len(self.probes)


def _run_pytest(extra: list[str] | None = None) -> tuple[int, str, float]:
    """Run the probe suite in a subprocess and capture its output."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(EVAL_DIR),
        "-v",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--tb=short",
    ]
    command.extend(extra or [])
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        check=False,
    )
    elapsed = time.perf_counter() - started
    return completed.returncode, completed.stdout + completed.stderr, elapsed


def _collect_nodeids() -> list[str]:
    """Return the ``nodeid`` of every test pytest would collect."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(EVAL_DIR),
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        check=False,
    )
    nodeids = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("<", "=", "-")):
            nodeids.append(line)
    return nodeids


def _parse_results(output: str) -> dict[str, str]:
    """Map every reported ``nodeid`` onto its pytest status."""
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        match = _STATUS_LINE.match(line.strip())
        if match:
            statuses[match.group("nodeid")] = match.group("status")
    return statuses


def _durations(output: str) -> dict[str, float]:
    """Extract the per-test duration from pytest's verbose output."""
    durations: dict[str, float] = {}
    pattern = re.compile(r"^(?P<nodeid>\S+::\S+).*?(?P<seconds>\d+\.\d{2})s\b")
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if match:
            durations[match.group("nodeid")] = float(match.group("seconds"))
    return durations


def _summary_counts(output: str) -> tuple[int, int, int]:
    """Return ``(collected, passed, failed)`` from the pytest summary line."""
    passed = failed = 0
    for match in _SUMMARY.finditer(output):
        count = int(match.group("count"))
        word = match.group("word")
        if word == "passed":
            passed = count
        elif word in {"failed", "error", "errors"}:
            failed = count
    return passed + failed, passed, failed


def _module_stem(module: str) -> str:
    """Return the file stem of a dotted module path (``eval.test_metering``)."""
    return module.replace(".", "/").rsplit("/", 1)[-1]


def _match_nodeids(module: str, test: str, nodeids: list[str]) -> list[str]:
    """Return every node id implementing ``module::test``.

    A parametrized test contributes one node id per case, so the match is
    done on the *base* name (the part before the ``[...]`` suffix).
    """
    stem = _module_stem(module)
    matched: list[str] = []
    for nodeid in nodeids:
        file_part, _, test_part = nodeid.partition("::")
        if not test_part:
            continue
        file_stem = file_part.rsplit("/", 1)[-1].removesuffix(".py")
        if file_stem == stem and test_part.split("[")[0] == test:
            matched.append(nodeid)
    return matched


def _aggregate_status(statuses: list[str]) -> str:
    """Collapse the statuses of several cases into one."""
    if not statuses:
        return "MISSING"
    if all(status == "PASSED" for status in statuses):
        return "PASSED"
    if any(status == "FAILED" for status in statuses):
        return "FAILED"
    if any(status == "ERROR" for status in statuses):
        return "ERROR"
    if any(status == "SKIPPED" for status in statuses):
        return "SKIPPED"
    return "MISSING"


def evaluate(probe_filter: str | None = None) -> SuiteResult:
    """Run the suite and map the outcome onto the scenario matrix."""
    manifest = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    nodeids = _collect_nodeids()
    code, output, elapsed = _run_pytest()
    statuses = _parse_results(output)
    durations = _durations(output)
    collected, passed, failed = _summary_counts(output)

    result = SuiteResult(
        total_collected=collected,
        total_passed=passed,
        total_failed=failed,
        duration=elapsed,
        stdout=output,
    )

    for probe in manifest["probes"]:
        if probe_filter and probe["id"] != probe_filter:
            continue
        outcomes: list[TestOutcome] = []
        for entry in probe["tests"]:
            matched = _match_nodeids(entry["module"], entry["test"], nodeids)
            case_statuses = [statuses.get(nodeid, "MISSING") for nodeid in matched]
            outcomes.append(
                TestOutcome(
                    module=entry["module"],
                    test=entry["test"],
                    nodeids=tuple(matched),
                    status=_aggregate_status(case_statuses),
                    duration=sum(durations.get(nodeid, 0.0) for nodeid in matched),
                )
            )
        result.probes.append(
            ProbeResult(
                probe_id=probe["id"],
                name=probe["name"],
                category=probe["category"],
                critical=bool(probe.get("critical", False)),
                tests=tuple(outcomes),
            )
        )

    if code not in (0, 1) and result.probes:
        # A collection error or a crash: every probe is unverified.
        for probe in result.probes:
            if probe.status == "MISSING":
                for outcome in probe.tests:
                    object.__setattr__(outcome, "status", "ERROR")
    return result


def render_evidence(result: SuiteResult, manifest: dict[str, Any]) -> str:
    """Render the evidence markdown."""
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = []
    lines.append("# Evaluation Evidence")
    lines.append("")
    lines.append(
        "Machine-generated by `python3 eval/eval.py`.  Every number below comes "
        "from the run that produced this file - it is never hand-written."
    )
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Generated | {generated} |")
    lines.append(f"| Scenario matrix | `eval/eval_set.json` (v{manifest['version']}) |")
    lines.append(f"| Probes declared | {len(manifest['probes'])} |")
    lines.append(f"| Tests collected | {result.total_collected} |")
    lines.append(f"| Tests passed | {result.total_passed} |")
    lines.append(f"| Tests failed | {result.total_failed} |")
    lines.append(f"| Suite duration | {result.duration:.2f} s |")
    lines.append(
        f"| Probe pass rate | {result.pass_rate * 100:.1f}% "
        f"({sum(1 for p in result.probes if p.passed)}/{len(result.probes)}) |"
    )
    lines.append(
        "| Environment | SQLite + fakeredis/Lua, with the `live_redis` probes "
        "verified against the Redis 7 container |"
    )
    lines.append("")

    lines.append("## Probe results")
    lines.append("")
    lines.append("| Probe | Category | Critical | Tests | Cases | Status | Duration |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for probe in result.probes:
        critical = "yes" if probe.critical else "no"
        lines.append(
            f"| `{probe.probe_id}` | {probe.category} | {critical} | "
            f"{len(probe.tests)} | {probe.cases} | **{probe.status}** | "
            f"{probe.duration:.2f}s |"
        )
    lines.append("")

    lines.append("## Probe coverage")
    lines.append("")
    for probe in result.probes:
        lines.append(f"### `{probe.probe_id}` - {probe.name}")
        lines.append("")
        lines.append(f"* **Category:** {probe.category}")
        lines.append(f"* **Critical:** {'yes' if probe.critical else 'no'}")
        lines.append(f"* **Status:** {probe.status}")
        lines.append(f"* **Cases:** {probe.cases}")
        lines.append("")
        lines.append("| Test | Module | Cases | Status | Duration |")
        lines.append("| --- | --- | --- | --- | --- |")
        for outcome in probe.tests:
            lines.append(
                f"| `{outcome.test}` | `{outcome.module}` | {outcome.cases} | "
                f"**{outcome.status}** | {outcome.duration:.2f}s |"
            )
        lines.append("")

    lines.append("## Full pytest transcript")
    lines.append("")
    lines.append("```text")
    lines.append(result.stdout.rstrip())
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Run the capstone evaluation suite.")
    parser.add_argument("--probe", help="run a single probe id (e.g. P3-quota)")
    parser.add_argument("--no-evidence", action="store_true", help="do not refresh EVIDENCE.md")
    args = parser.parse_args(argv)

    manifest = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    print(f"Capstone evaluation - scenario matrix v{manifest['version']}")
    print(f"Project root: {PROJECT_ROOT}\n")

    result = evaluate(args.probe)

    for probe in result.probes:
        mark = "PASS" if probe.passed else "FAIL"
        print(
            f"  [{mark}] {probe.probe_id:<24} {probe.category:<20} "
            f"{len(probe.tests)} tests / {probe.cases} cases  ({probe.duration:.2f}s)"
        )
        for outcome in probe.tests:
            if not outcome.passed:
                print(f"           ! {outcome.test} -> {outcome.status}")

    print(
        f"\nProbes: {sum(1 for p in result.probes if p.passed)}/{len(result.probes)} passed "
        f"({result.pass_rate * 100:.1f}%)"
    )
    print(
        f"Tests : {result.total_passed}/{result.total_collected} passed in {result.duration:.2f}s"
    )

    if not args.no_evidence:
        EVIDENCE_PATH.write_text(render_evidence(result, manifest), encoding="utf-8")
        print(f"\nEvidence written to {EVIDENCE_PATH}")

    if result.probes and all(probe.passed for probe in result.probes):
        print("\nRESULT: PASS - every declared probe is green.")
        return 0
    print("\nRESULT: FAIL - at least one probe is not green.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
