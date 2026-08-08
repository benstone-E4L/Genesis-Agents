#!/usr/bin/env python3
"""Genesis Agents eval harness.

Loads task fixtures from evals/tasks/{agent_slug}/*.json, runs them against
mock results embedded in the fixture (offline unit-testing mode — no live LLM
or network calls), grades each result with deterministic graders, prints a
per-agent report, and saves the full report to evals/reports/latest.json.

Usage:
    python evals/run_evals.py --agent genesis-builder
    python evals/run_evals.py --all
    python evals/run_evals.py --all --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Allow running from the repo root or from inside evals/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_EVALS_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _EVALS_DIR / "tasks"
_REPORTS_DIR = _EVALS_DIR / "reports"

# Ensure graders package is importable regardless of cwd
sys.path.insert(0, str(_EVALS_DIR))

from graders import run_all_graders  # noqa: E402  (after sys.path manipulation)


# ---------------------------------------------------------------------------
# Task loading
# ---------------------------------------------------------------------------


def discover_agent_slugs() -> list[str]:
    """Return all agent slugs that have a tasks/ subdirectory."""
    if not _TASKS_DIR.exists():
        return []
    return sorted(
        p.name for p in _TASKS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def load_tasks(slug: str) -> list[dict[str, Any]]:
    """Load all JSON task fixtures for a given agent slug."""
    task_dir = _TASKS_DIR / slug
    if not task_dir.exists():
        return []
    tasks = []
    for fixture_path in sorted(task_dir.glob("*.json")):
        try:
            with open(fixture_path, encoding="utf-8") as f:
                task = json.load(f)
            task["_fixture_path"] = str(fixture_path)
            tasks.append(task)
        except Exception as exc:
            print(f"  [WARN] Failed to load {fixture_path}: {exc}")
    return tasks


# ---------------------------------------------------------------------------
# Mock execution
# ---------------------------------------------------------------------------


def run_task_mock(task: dict[str, Any]) -> tuple[dict[str, Any], float]:
    """Return the mock_result embedded in the fixture (offline mode).

    In offline eval mode we do not call the live agent. The fixture embeds a
    representative `mock_result` that the graders evaluate. This allows the
    eval suite to run in CI without any LLM API keys.

    Returns (result_dict, elapsed_s).
    """
    t0 = time.monotonic()
    mock = task.get("mock_result")
    if mock is None:
        # No mock result — synthesise a minimal failure so graders still run.
        result: dict[str, Any] = {
            "ok": False,
            "error": "no_mock_result_in_fixture",
            "slug": task.get("slug", "unknown"),
        }
    else:
        result = dict(mock)
    elapsed = time.monotonic() - t0
    return result, elapsed


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def grade_task(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Run all applicable graders and return a grading record."""
    expected = task.get("expected", {})
    grading_records = run_all_graders(result, expected)

    all_passed = all(r["passed"] for r in grading_records)
    failures = [r for r in grading_records if not r["passed"]]

    return {
        "task_id": task.get("id", "unknown"),
        "category": task.get("category", "unknown"),
        "fixture_path": task.get("_fixture_path", ""),
        "passed": all_passed,
        "graders": grading_records,
        "failure_reasons": [r["reason"] for r in failures],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def run_agent_eval(slug: str, verbose: bool = False) -> dict[str, Any]:
    """Load, run (mock), and grade all tasks for one agent. Return agent report."""
    tasks = load_tasks(slug)
    if not tasks:
        return {
            "slug": slug,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "pass_rate": None,
            "task_results": [],
            "error": "no_tasks_found",
        }

    task_results = []
    for task in tasks:
        result, _elapsed = run_task_mock(task)
        grade = grade_task(task, result)
        task_results.append(grade)
        if verbose:
            status = "PASS" if grade["passed"] else "FAIL"
            print(f"    [{status}] {grade['task_id']} ({grade['category']})")
            if not grade["passed"]:
                for reason in grade["failure_reasons"]:
                    print(f"           reason: {reason}")

    total = len(task_results)
    passed = sum(1 for r in task_results if r["passed"])
    failed = total - passed
    pass_rate = round(passed / total * 100, 1) if total > 0 else 0.0

    return {
        "slug": slug,
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": pass_rate,
        "task_results": task_results,
    }


def print_summary(agent_reports: list[dict[str, Any]]) -> None:
    """Print a human-readable summary table to stdout."""
    print("\n" + "=" * 60)
    print("Genesis Agents Eval Report")
    print("=" * 60)

    total_tasks = 0
    total_passed = 0

    for report in agent_reports:
        slug = report["slug"]
        t = report["total"]
        p = report["passed"]
        f = report["failed"]
        rate = report.get("pass_rate")
        rate_str = f"{rate}%" if rate is not None else "N/A"
        bar_filled = int((p / t * 20) if t > 0 else 0)
        bar = "#" * bar_filled + "." * (20 - bar_filled)
        print(f"  {slug:<30} [{bar}] {p}/{t} ({rate_str})")

        if report.get("error"):
            print(f"    error: {report['error']}")

        for tr in report.get("task_results", []):
            if not tr["passed"]:
                for reason in tr["failure_reasons"]:
                    print(f"    FAIL [{tr['task_id']}]: {reason}")

        total_tasks += t
        total_passed += p

    total_failed = total_tasks - total_passed
    overall_rate = round(total_passed / total_tasks * 100, 1) if total_tasks > 0 else 0.0

    print("-" * 60)
    print(f"  Total: {total_passed}/{total_tasks} passed ({overall_rate}%) — {total_failed} failures")
    print("=" * 60)


def save_report(agent_reports: list[dict[str, Any]]) -> Path:
    """Persist the full report to evals/reports/latest.json. Returns the path."""
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _REPORTS_DIR / "latest.json"

    total_tasks = sum(r["total"] for r in agent_reports)
    total_passed = sum(r["passed"] for r in agent_reports)
    overall_rate = round(total_passed / total_tasks * 100, 1) if total_tasks > 0 else 0.0

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "total_tasks": total_tasks,
            "total_passed": total_passed,
            "total_failed": total_tasks - total_passed,
            "overall_pass_rate": overall_rate,
        },
        "agents": agent_reports,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return report_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Genesis Agents offline eval harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--agent",
        metavar="SLUG",
        help="Run evals for a single agent (e.g. genesis-builder)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Run evals for all agents with tasks/ directories",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-task pass/fail details",
    )
    parser.add_argument(
        "--tasks-dir",
        default=str(_TASKS_DIR),
        metavar="PATH",
        help=f"Override tasks directory (default: {_TASKS_DIR})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    global _TASKS_DIR
    _TASKS_DIR = Path(args.tasks_dir)

    if args.all:
        slugs = discover_agent_slugs()
        if not slugs:
            print(f"No agent task directories found under {_TASKS_DIR}")
            return 1
    else:
        slugs = [args.agent]

    print(f"Running evals for: {', '.join(slugs)}")
    print()

    agent_reports: list[dict[str, Any]] = []
    for slug in slugs:
        print(f"  Agent: {slug}")
        report = run_agent_eval(slug, verbose=args.verbose)
        agent_reports.append(report)
        rate = report.get("pass_rate")
        rate_str = f"{rate}%" if rate is not None else "N/A"
        print(f"    pass_rate={rate_str}  passed={report['passed']}  failed={report['failed']}")

    print_summary(agent_reports)

    report_path = save_report(agent_reports)
    print(f"\nFull report saved to: {report_path}")

    # Exit non-zero if any task failed
    any_failures = any(r["failed"] > 0 for r in agent_reports)
    return 1 if any_failures else 0


if __name__ == "__main__":
    sys.exit(main())
