"""Integration against the REAL `langsmith` SDK — not the fake.

Two things the fakes cannot prove:

1. ``@traceable`` from the installed langsmith actually accepts the decoration
   we hand it (name, run_type, process_inputs, process_outputs).
2. When the LangSmith backend is unreachable, the agent call still returns.

The endpoint is pointed at a closed loopback port, so this test makes no
internet request. It never reaches the live Genesis gateway either — the client
is driven by a fake transport as everywhere else.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, Outcome
from eval.tests.fakes import FakeTransport, ok_response

langsmith = pytest.importorskip("langsmith")


@pytest.fixture
def closed_port() -> int:
    """A loopback port with nothing listening on it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_real_traceable_accepts_our_decoration(monkeypatch):
    from langsmith.run_helpers import traceable as real_traceable

    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", None)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-not-a-real-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    async def fn(inputs):
        return {"ok": True}

    wrapped = traceable_mod._wrap_traceable(fn)
    assert wrapped is not fn, "the real @traceable was not applied"
    assert wrapped.__name__ == "fn"

    # And it applies with the redaction hooks, which is the path we prefer.
    assert real_traceable(
        run_type=traceable_mod.RUN_TYPE,
        name=traceable_mod.RUN_NAME,
        process_inputs=traceable_mod.redact,
        process_outputs=traceable_mod.redact,
    )(fn) is not fn


def test_unreachable_langsmith_backend_does_not_break_the_agent_call(
    monkeypatch, closed_port
):
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", None)
    monkeypatch.setattr(traceable_mod, "RUN_TREE_GETTER", None)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-not-a-real-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", f"http://127.0.0.1:{closed_port}")
    monkeypatch.setenv("LANGSMITH_PROJECT", "genesis-eval-unreachable-test")
    # Keep the SDK's retry/flush behaviour short so an unreachable backend
    # cannot stall the suite.
    monkeypatch.setenv("LANGCHAIN_CALLBACKS_BACKGROUND", "true")

    assert traceable_mod.tracing_enabled() is True

    transport = FakeTransport([ok_response("answer despite no langsmith")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)

    result = asyncio.run(
        traceable_mod.traced_agent_run(
            client, slug="genesis-research", task="t", mode="live_test"
        )
    )

    assert result.outcome is Outcome.SUCCESS
    assert result.response_text == "answer despite no langsmith"
    assert transport.run_call_count == 1


def test_get_current_run_tree_outside_a_run_is_handled(monkeypatch):
    """No active run must be a silent no-op, not an exception."""
    monkeypatch.setattr(traceable_mod, "RUN_TREE_GETTER", None)
    traceable_mod._attach_metadata({"slug": "genesis_research_x402"})  # must not raise
