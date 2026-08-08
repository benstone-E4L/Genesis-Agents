"""Deterministic graders for Genesis agent eval harness.

Each grader takes a result dict (the structured output from AgentRuntime or the
mock call) plus grading parameters, and returns a (passed: bool, reason: str)
tuple so callers can record both the outcome and the explanation.
"""
from __future__ import annotations

from typing import Any


def grade_required_fields(
    result: dict[str, Any],
    fields: list[str],
) -> tuple[bool, str]:
    """Return True if every field in `fields` is present in `result` with a non-None value."""
    missing = [f for f in fields if result.get(f) is None]
    if missing:
        return False, f"missing required fields: {missing}"
    return True, "all required fields present"


def grade_no_fake_success(
    result: dict[str, Any],
    forbidden_phrases: list[str],
) -> tuple[bool, str]:
    """Return True if no forbidden phrase appears in the result's response text.

    Checks `result['response']` (string) case-insensitively. Empty response
    is not considered a fake success — use grade_correct_status for that.
    """
    response_text = str(result.get("response") or "").lower()
    matched = [p for p in forbidden_phrases if p.lower() in response_text]
    if matched:
        return False, f"response contains forbidden phrases: {matched}"
    return True, "no fake success phrases detected"


def grade_correct_status(
    result: dict[str, Any],
    expected_ok: bool,
) -> tuple[bool, str]:
    """Return True if result['ok'] matches `expected_ok`."""
    actual = bool(result.get("ok"))
    if actual == expected_ok:
        return True, f"status ok={actual} matches expected ok={expected_ok}"
    return False, f"status ok={actual} but expected ok={expected_ok}"


def grade_max_latency(
    result: dict[str, Any],
    max_s: float,
) -> tuple[bool, str]:
    """Return True if result['elapsed_s'] is within the allowed maximum.

    If elapsed_s is absent (e.g. mock result), the grader passes vacuously
    and records the absence in the reason string.
    """
    elapsed = result.get("elapsed_s")
    if elapsed is None:
        return True, "elapsed_s not recorded (mock result — skipped latency check)"
    elapsed = float(elapsed)
    if elapsed <= max_s:
        return True, f"elapsed {elapsed:.2f}s <= max {max_s}s"
    return False, f"elapsed {elapsed:.2f}s > max {max_s}s"


def grade_artifact_count(
    result: dict[str, Any],
    min_count: int,
) -> tuple[bool, str]:
    """Return True if result contains at least `min_count` artifacts.

    Checks `result['artifact_count']` (int) or falls back to
    `len(result.get('output_artifact_uris', []))`.
    """
    explicit = result.get("artifact_count")
    if explicit is not None:
        count = int(explicit)
    else:
        uris = result.get("output_artifact_uris") or []
        count = len(uris)

    if count >= min_count:
        return True, f"artifact_count {count} >= min {min_count}"
    return False, f"artifact_count {count} < min {min_count}"


def run_all_graders(
    result: dict[str, Any],
    expected: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run every applicable grader against `result` given the task `expected` config.

    Returns a list of grading records:
      [{"grader": str, "passed": bool, "reason": str}, ...]
    """
    records: list[dict[str, Any]] = []

    # correct_status — always run
    passed, reason = grade_correct_status(result, expected_ok=bool(expected.get("ok", True)))
    records.append({"grader": "correct_status", "passed": passed, "reason": reason})

    # required_fields
    req = expected.get("required_fields") or []
    if req:
        passed, reason = grade_required_fields(result, req)
        records.append({"grader": "required_fields_present", "passed": passed, "reason": reason})

    # no_fake_success
    forbidden = expected.get("no_fake_success_phrases") or []
    if forbidden:
        passed, reason = grade_no_fake_success(result, forbidden)
        records.append({"grader": "no_fake_success", "passed": passed, "reason": reason})

    # max_latency
    max_latency = expected.get("max_latency_s")
    if max_latency is not None:
        passed, reason = grade_max_latency(result, float(max_latency))
        records.append({"grader": "max_latency", "passed": passed, "reason": reason})

    # artifact_count
    min_response_length = expected.get("min_response_length")
    if min_response_length is not None:
        response_len = len(str(result.get("response") or ""))
        passed = response_len >= int(min_response_length)
        reason = (
            f"response length {response_len} >= min {min_response_length}"
            if passed
            else f"response length {response_len} < min {min_response_length}"
        )
        records.append({"grader": "min_response_length", "passed": passed, "reason": reason})

    return records
