"""Observability must never become a dependency of execution.

Every test here asserts the same property from a different failure angle: the
agent call runs, and its result is returned, no matter what the Phoenix tracing
layer does. Tracing is fail-OPEN. (Secret handling, by contrast, is fail-CLOSED
— see test_secret_redaction.py.)
"""

from __future__ import annotations

import asyncio

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, Outcome, RawResponse
from eval.tests.fakes import (
    ExplodingAtEndTracer,
    ExplodingTracer,
    FakeTransport,
    InMemoryPhoenix,
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


def _install_tracer(monkeypatch, tracer):
    from runtime import phoenix_tracing

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setattr(phoenix_tracing, "_build_tracer", lambda: tracer)
    phoenix_tracing.reset_for_tests()


# ---------------------------------------------------------------------------
# Tracing off
# ---------------------------------------------------------------------------


def test_no_phoenix_endpoint_means_no_tracing_but_the_call_still_runs(monkeypatch):
    for var in ("PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_ENDPOINT",
                "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)

    assert traceable_mod.tracing_enabled() is False

    client, transport = _client()
    result = _call(client)

    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "agent answered"
    assert transport.run_call_count == 1


def test_empty_phoenix_endpoint_is_treated_as_absent(monkeypatch):
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "   ")
    assert traceable_mod.tracing_enabled() is False
    client, _ = _client()
    assert _call(client).outcome is Outcome.SUCCESS


def test_phoenix_tracing_false_disables_tracing_even_with_an_endpoint(monkeypatch):
    phoenix = InMemoryPhoenix().install(monkeypatch)
    monkeypatch.setenv("PHOENIX_TRACING", "false")

    from runtime import phoenix_tracing

    phoenix_tracing.reset_for_tests()

    assert traceable_mod.tracing_enabled() is False
    client, _ = _client()
    assert _call(client).outcome is Outcome.SUCCESS
    assert phoenix.spans == [], "tracing ran despite PHOENIX_TRACING=false"


def test_tracing_enabled_when_endpoint_present_and_flag_unset(monkeypatch):
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.delenv("PHOENIX_TRACING", raising=False)
    assert traceable_mod.tracing_enabled() is True


# ---------------------------------------------------------------------------
# Tracing on, but broken
# ---------------------------------------------------------------------------


def test_a_missing_opentelemetry_install_degrades_to_no_tracing(monkeypatch):
    """An ImportError inside tracer construction must not reach the caller."""
    from runtime import phoenix_tracing

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("PHOENIX_TRACING", "true")

    def boom():
        raise ImportError("no module named opentelemetry")

    monkeypatch.setattr(phoenix_tracing, "_build_tracer", boom)
    phoenix_tracing.reset_for_tests()

    client, transport = _client()
    result = _call(client)
    assert result.outcome is Outcome.SUCCESS
    assert transport.run_call_count == 1


def test_a_tracer_that_explodes_at_span_start_does_not_break_the_call(monkeypatch):
    _install_tracer(monkeypatch, ExplodingTracer())

    client, transport = _client()
    result = _call(client)

    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "agent answered"
    assert transport.run_call_count == 1


def test_a_tracer_that_explodes_on_export_does_not_break_the_call(monkeypatch):
    """The span was started, the agent ran, and the export blew up afterwards.

    The result must still be returned — an observability error must never be
    surfaced to the caller as an agent error.
    """
    _install_tracer(monkeypatch, ExplodingAtEndTracer())

    client, transport = _client()
    result = _call(client)

    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "agent answered"
    assert transport.run_call_count == 1


def test_a_real_agent_failure_is_still_reported_when_tracing_is_off(monkeypatch):
    for var in ("PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_ENDPOINT",
                "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    client, _ = _client([RawResponse(401, "nope")])
    result = _call(client)
    assert result.outcome is Outcome.AUTH_ERROR


# ---------------------------------------------------------------------------
# Trace attribute contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["live_test", "full"])
def test_span_carries_the_required_attributes(monkeypatch, mode):
    phoenix = InMemoryPhoenix().install(monkeypatch)

    client, _ = _client([RawResponse(503, "x"), ok_response("ok")])
    asyncio.run(
        traceable_mod.traced_agent_run(
            client, slug="genesis-research", task="t", mode=mode
        )
    )

    spans = phoenix.named(traceable_mod.RUN_NAME)
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)

    assert attributes["openinference.span.kind"] == "AGENT"
    for field in ("slug", "mode", "elapsed_ms", "http_status", "attempts", "outcome"):
        assert f"genesis.eval.{field}" in attributes, f"missing required attribute {field}"
    assert attributes["genesis.eval.slug"] == "genesis_research_x402"
    assert attributes["genesis.eval.mode"] == mode
    assert attributes["genesis.eval.attempts"] == 2
    assert attributes["genesis.eval.http_status"] == 200
    assert attributes["genesis.eval.outcome"] == "success"
    assert isinstance(attributes["genesis.eval.elapsed_ms"], int)


def test_evaluation_verdicts_are_emitted_as_spans(monkeypatch):
    """Verdict spans replace LangSmith feedback — losing them would lose the scores."""
    from eval.rubrics import ExampleScore, Verdict

    phoenix = InMemoryPhoenix().install(monkeypatch)

    score = ExampleScore(
        example_id="ex-1",
        slug="genesis_research_x402",
        mode="full",
        status="fail",
        verdicts=(
            Verdict("refusal_correctness", "safety", 4, 5, "pass", "declined cleanly",
                    "deterministic"),
            Verdict("citation_quality", "grounding", 1, 5, "fail", "no sources", "judge"),
        ),
    )
    traceable_mod.emit_verdict_spans(score)

    spans = phoenix.named(traceable_mod.VERDICT_SPAN_NAME)
    assert len(spans) == 2
    by_rubric = {dict(s.attributes)["genesis.eval.rubric"]: dict(s.attributes) for s in spans}

    passing = by_rubric["refusal_correctness"]
    assert passing["openinference.span.kind"] == "EVALUATOR"
    assert passing["genesis.eval.status"] == "pass"
    assert passing["genesis.eval.score"] == 4
    assert passing["genesis.eval.score_normalised"] == pytest.approx(0.8)
    assert passing["genesis.eval.example_id"] == "ex-1"
    assert passing["genesis.eval.source"] == "deterministic"

    failing = by_rubric["citation_quality"]
    assert failing["genesis.eval.status"] == "fail"
    assert failing["genesis.eval.score_normalised"] == pytest.approx(0.2)


def test_verdict_emission_never_raises_when_tracing_explodes(monkeypatch):
    from eval.rubrics import ExampleScore, Verdict

    _install_tracer(monkeypatch, ExplodingTracer())
    score = ExampleScore(
        example_id="ex-1", slug="s", mode="full", status="pass",
        verdicts=(Verdict("r", "d", 5, 5, "pass", "c", "judge"),),
    )
    traceable_mod.emit_verdict_spans(score)  # must not raise


def test_verdict_comment_is_withheld_unless_content_tracing_is_on(monkeypatch):
    """A judge comment quotes the agent's answer, so it is content, not metadata."""
    from eval.rubrics import ExampleScore, Verdict

    phoenix = InMemoryPhoenix().install(monkeypatch)
    score = ExampleScore(
        example_id="ex-1", slug="s", mode="full", status="pass",
        verdicts=(Verdict("r", "d", 5, 5, "pass", "the answer quoted the estate ledger",
                          "judge"),),
    )
    traceable_mod.emit_verdict_spans(score)

    attributes = dict(phoenix.named(traceable_mod.VERDICT_SPAN_NAME)[0].attributes)
    assert "genesis.eval.comment" not in attributes
    assert "estate ledger" not in phoenix.serialised()


def test_warmup_happens_once_and_a_failed_warmup_does_not_block(monkeypatch):
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
