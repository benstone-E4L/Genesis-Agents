"""Tests for RunRequest prompt assembly."""
from main import RunRequest, _build_user_prompt, _is_x402_stub_response


def test_build_user_prompt_merges_input_and_task():
    body = RunRequest(
        input="You are being tested. Complete the task below.",
        task="Create a QA test plan for login flows.",
        testContext=True,
    )
    merged = _build_user_prompt(body)
    assert "being tested" in merged
    assert "QA test plan" in merged
    assert "Task:" in merged


def test_x402_stub_detection():
    assert _is_x402_stub_response("Genesis Builder Agent is an x402 HTTP service.")
    assert not _is_x402_stub_response("Here is your API specification.")
