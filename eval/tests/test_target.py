"""The evaluation target: schema contract, client injection, no live gateway."""

from __future__ import annotations

import asyncio
import json

import pytest

from eval.genesis_client import GenesisClient, Outcome, RawResponse
from eval.target import (
    InvalidExample,
    arun_example,
    make_async_target,
    make_target,
    parse_example,
)
from eval.tests.fakes import FakeTransport, RecordingSleep, ok_response, read_timeout

OUTPUT_KEYS = {
    "response", "outcome", "ok", "determinate", "slug", "requested_slug",
    "slug_resolution", "mode", "http_status", "elapsed_ms", "attempts",
    "error_kind", "error_message", "agent_name",
}


def _target(script=None):
    transport = FakeTransport(script or [ok_response("the answer")])
    client = GenesisClient(
        transport=transport, api_key="k", jitter=False, sleep=RecordingSleep()
    )
    return make_target(client), transport


def test_target_is_callable_without_a_live_gateway():
    target, transport = _target()
    outputs = target({"slug": "genesis-research", "task": "summarise the market"})
    assert outputs["outcome"] == "success"
    assert outputs["response"] == "the answer"
    assert transport.run_call_count == 1


def test_output_schema_is_exactly_as_documented():
    target, _ = _target()
    outputs = target({"slug": "genesis-research", "task": "t"})
    assert set(outputs) == OUTPUT_KEYS, set(outputs).symmetric_difference(OUTPUT_KEYS)


def test_outputs_are_json_serialisable():
    target, _ = _target()
    outputs = target({"slug": "genesis-research", "task": "t"})
    json.dumps(outputs)  # must not raise


@pytest.mark.parametrize("alias", ["task", "prompt", "input", "question", "query"])
def test_task_aliases_are_accepted(alias):
    target, transport = _target()
    outputs = target({"slug": "genesis-research", alias: "do the thing"})
    assert outputs["outcome"] == "success"
    body = transport.calls[0]["json"]
    assert body.get("prompt") == "do the thing"


def test_mode_defaults_to_live_test():
    target, transport = _target()
    outputs = target({"slug": "genesis-research", "task": "t"})
    assert outputs["mode"] == "live_test"
    assert transport.calls[0]["json"]["testContext"] is True


def test_mode_full_is_honoured_and_echoed():
    target, transport = _target()
    outputs = target({"slug": "genesis-research", "task": "t", "mode": "full"})
    assert outputs["mode"] == "full"
    assert "testContext" not in transport.calls[0]["json"]


def test_unknown_dataset_keys_are_ignored():
    """The dataset may carry rubric-only fields in the same inputs dict."""
    target, _ = _target()
    outputs = target(
        {
            "slug": "genesis-research",
            "task": "t",
            "expected": "a good answer",
            "criteria": ["cites sources"],
            "category": "research",
        }
    )
    assert outputs["outcome"] == "success"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"task": "no slug"},
        {"slug": "", "task": "t"},
        {"slug": "genesis-research"},
        {"slug": "genesis-research", "task": ""},
        {"slug": "genesis-research", "task": "t", "mode": "turbo"},
        {"slug": 42, "task": "t"},
    ],
)
def test_invalid_examples_are_rejected_by_the_parser(bad):
    with pytest.raises(InvalidExample):
        parse_example(bad)


def test_invalid_example_returns_a_schema_shaped_error_not_a_crash():
    """evaluate() must not die on one bad row."""
    target, transport = _target()
    outputs = target({"task": "no slug here"})
    assert set(outputs) == OUTPUT_KEYS
    assert outputs["error_kind"] == "invalid_example"
    assert outputs["ok"] is False
    assert transport.run_call_count == 0


def test_money_domain_example_is_blocked_and_reported_not_raised():
    target, transport = _target()
    outputs = target({"slug": "genesis-finance", "task": "compute my payout"})
    assert outputs["error_kind"] == "blocked_money_domain"
    assert outputs["ok"] is False
    assert transport.run_call_count == 0


@pytest.mark.parametrize(
    "script,expected,determinate",
    [
        ([ok_response()], "success", True),
        ([RawResponse(401, "no")], "auth_error", True),
        ([RawResponse(404, "no")], "not_found", True),
        ([RawResponse(503, "no")], "upstream_error", True),
        ([read_timeout()], "indeterminate", False),
    ],
)
def test_every_outcome_class_surfaces_through_the_target(script, expected, determinate):
    target, _ = _target(script)
    outputs = target({"slug": "genesis-research", "task": "t"})
    assert outputs["outcome"] == expected
    assert outputs["determinate"] is determinate
    assert outputs["ok"] is (expected == "success")


def test_async_target_form():
    transport = FakeTransport([ok_response("async answer")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    atarget = make_async_target(client)
    outputs = asyncio.run(atarget({"slug": "genesis-research", "task": "t"}))
    assert outputs["response"] == "async answer"


def test_sync_target_works_from_inside_a_running_event_loop():
    """langsmith.evaluate may call the target from a thread with a live loop."""
    target, _ = _target()

    async def driver():
        return target({"slug": "genesis-research", "task": "t"})

    outputs = asyncio.run(driver())
    assert outputs["outcome"] == "success"


def test_slug_is_normalised_in_the_outputs():
    target, _ = _target()
    outputs = target({"slug": "genesis-research", "task": "t"})
    assert outputs["slug"] == "genesis_research_x402"
    assert outputs["requested_slug"] == "genesis-research"
    assert outputs["slug_resolution"] == "verified"


def test_arun_example_accepts_an_injected_client_directly():
    transport = FakeTransport([ok_response("injected")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    outputs = asyncio.run(
        arun_example({"slug": "genesis-research", "task": "t"}, client=client)
    )
    assert outputs["response"] == "injected"
