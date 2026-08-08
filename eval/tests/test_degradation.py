"""Observability must never become a dependency of execution."""

from __future__ import annotations

import asyncio
import sys

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, Outcome, RawResponse
from eval.tests.fakes import (
    ExplodingAtCallLangSmith,
    ExplodingLangSmith,
    FakeLangSmith,
    FakeTransport,
    RecordingSleep,
    ok_response,
)


def _client(script=None):
    transport = FakeTransport(script or [ok_response("agent answered")])
    client = GenesisClient(
        transport=transport, api_key="k", jitter=False, sleep=RecordingSleep()
    )
    return client, transport


def _call(client):
    return asyncio.run(
        traceable_mod.traced_agent_run(
            client, slug="genesis-research", task="summarise the market", mode="live_test"
        )
    )


def test_no_langsmith_api_key_means_no_tracing_but_the_call_still_runs(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", None)

    assert traceable_mod.tracing_enabled() is False

    client, transport = _client()
    result = _call(client)

    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "agent answered"
    assert transport.run_call_count == 1


def test_empty_langsmith_api_key_is_treated_as_absent(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "   ")
    assert traceable_mod.tracing_enabled() is False
    client, _ = _client()
    assert _call(client).outcome is Outcome.SUCCESS


def test_langsmith_tracing_false_disables_tracing_even_with_a_key(monkeypatch):
    fake = FakeLangSmith()
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", fake.traceable)

    assert traceable_mod.tracing_enabled() is False
    client, _ = _client()
    assert _call(client).outcome is Outcome.SUCCESS
    assert fake.runs == [], "tracing ran despite LANGSMITH_TRACING=false"


def test_langsmith_package_missing_does_not_break_the_call(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", None)
    # Simulate `import langsmith` failing.
    monkeypatch.setitem(sys.modules, "langsmith", None)

    client, transport = _client()
    result = _call(client)
    assert result.outcome is Outcome.SUCCESS
    assert transport.run_call_count == 1


def test_langsmith_unreachable_at_decoration_time_degrades(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", ExplodingLangSmith().traceable)

    client, transport = _client()
    result = _call(client)
    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "agent answered"
    assert transport.run_call_count == 1


def test_langsmith_failing_to_ship_the_run_degrades(monkeypatch):
    """The run executed; the tracing POST blew up afterwards. Return the result."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(
        traceable_mod, "TRACEABLE_FACTORY", ExplodingAtCallLangSmith().traceable
    )

    client, transport = _client()
    result = _call(client)
    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "agent answered"
    assert transport.run_call_count == 1


def test_metadata_attachment_failure_does_not_break_the_call(monkeypatch):
    def exploding_getter():
        raise RuntimeError("no run tree")

    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", FakeLangSmith().traceable)
    monkeypatch.setattr(traceable_mod, "RUN_TREE_GETTER", exploding_getter)

    client, _ = _client()
    assert _call(client).outcome is Outcome.SUCCESS


def test_a_real_agent_failure_is_still_reported_when_tracing_is_off(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    client, _ = _client([RawResponse(401, "nope")])
    result = _call(client)
    assert result.outcome is Outcome.AUTH_ERROR


def test_tracing_enabled_when_key_present_and_flag_unset(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert traceable_mod.tracing_enabled() is True


# ---------------------------------------------------------------------------
# Trace metadata contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["live_test", "full"])
def test_metadata_carries_the_required_fields(monkeypatch, mode):
    fake = FakeLangSmith()
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", fake.traceable)
    monkeypatch.setattr(traceable_mod, "RUN_TREE_GETTER", fake.get_current_run_tree)

    client, _ = _client([RawResponse(503, "x"), ok_response("ok")])
    asyncio.run(
        traceable_mod.traced_agent_run(
            client, slug="genesis-research", task="t", mode=mode
        )
    )

    assert len(fake.runs) == 1
    run = fake.runs[0]
    assert run["name"] == "genesis.agent.run"
    assert run["run_type"] == "chain"

    meta = run["metadata"]
    for field in ("slug", "mode", "elapsed_ms", "http_status", "attempts", "outcome"):
        assert field in meta, f"missing required metadata field {field}"
    assert meta["slug"] == "genesis_research_x402"
    assert meta["mode"] == mode
    assert meta["attempts"] == 2
    assert meta["http_status"] == 200
    assert meta["outcome"] == "success"
    assert isinstance(meta["elapsed_ms"], int)


def test_warmup_happens_once_and_a_failed_warmup_does_not_block(monkeypatch):
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    transport = FakeTransport(
        [ok_response("a"), ok_response("b")],
        health=RuntimeError("cold start, health check failed"),
    )
    client = GenesisClient(transport=transport, api_key="k", jitter=False)

    r1 = asyncio.run(client.run_agent("genesis-research", "one"))
    r2 = asyncio.run(client.run_agent("genesis-research", "two"))

    assert r1.outcome is Outcome.SUCCESS
    assert r2.outcome is Outcome.SUCCESS
    assert r1.warmed is False
    assert transport.health_calls == 1, "warmup must be one-shot"


def test_successful_warmup_is_recorded_and_not_repeated():
    transport = FakeTransport([ok_response("a"), ok_response("b")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    r1 = asyncio.run(client.run_agent("genesis-research", "one"))
    asyncio.run(client.run_agent("genesis-research", "two"))
    assert r1.warmed is True
    assert transport.health_calls == 1
