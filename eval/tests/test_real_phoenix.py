"""Integration against the REAL OpenTelemetry SDK and a REAL OTLP exporter.

Replaces the retired ``test_real_langsmith.py``. Two things the in-memory
exporter cannot prove:

1. The installed OTel SDK actually accepts the spans and attribute types this
   package emits. ``InMemoryPhoenix`` runs real span construction, but the
   attribute values still have to survive a real *exporter*, which is stricter
   about types than the span API is.
2. When the Phoenix collector is unreachable, the agent call still returns.

The exporter is pointed at a closed loopback port, so this test makes no
internet request and never touches the live Phoenix space. It never reaches the
real Genesis gateway either — the client is driven by a fake transport as
everywhere else.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, Outcome
from eval.tests.fakes import FakeTransport, ok_response

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")


@pytest.fixture
def closed_port() -> int:
    """A loopback port with nothing listening on it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_real_tracer_construction_accepts_our_configuration(monkeypatch, closed_port):
    """``_build_tracer`` must produce a usable tracer from real OTel classes."""
    from runtime import phoenix_tracing

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", f"http://127.0.0.1:{closed_port}")
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_API_KEY", "not-a-real-key")
    phoenix_tracing.reset_for_tests()

    tracer = phoenix_tracing.get_tracer()
    assert tracer is not None, phoenix_tracing.disabled_reason()

    # Real span, real attribute coercion, real (doomed) batch exporter.
    with phoenix_tracing.span("probe", kind="AGENT", attributes={"genesis.eval.slug": "s"}):
        pass
    # Flushing to a dead endpoint returns a bool rather than raising or hanging.
    assert phoenix_tracing.flush(2000) in (True, False)


def test_unreachable_phoenix_backend_does_not_break_the_agent_call(
    monkeypatch, closed_port
):
    from runtime import phoenix_tracing

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", f"http://127.0.0.1:{closed_port}")
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    monkeypatch.setenv("PHOENIX_API_KEY", "not-a-real-key")
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "genesis-eval-unreachable-test")
    phoenix_tracing.reset_for_tests()

    assert traceable_mod.tracing_enabled() is True

    transport = FakeTransport([ok_response("answer despite no phoenix")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)

    result = asyncio.run(
        traceable_mod.traced_agent_run(
            client, slug="genesis-research", task="t", mode="live_test"
        )
    )

    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "answer despite no phoenix"
    assert transport.run_call_count == 1


def test_emitting_a_span_outside_any_trace_is_handled(monkeypatch):
    """No configured tracer must be a silent no-op, not an exception."""
    from runtime import phoenix_tracing

    for var in ("PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_ENDPOINT",
                "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    phoenix_tracing.reset_for_tests()

    assert phoenix_tracing.current_trace_id() is None
    with phoenix_tracing.span("probe") as span:
        assert span is None
    phoenix_tracing.set_attributes(None, {"a": 1})  # must not raise
