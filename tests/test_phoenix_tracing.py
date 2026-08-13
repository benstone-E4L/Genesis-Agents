"""Phoenix/OpenInference tracing contract for the Genesis agent runtime.

The single most important property asserted here is **fail-open**: an
observability outage must never block an agent run. That is tested against a
real OTLP exporter pointed at a dead port, not a mock, because the failure mode
that matters is a live TCP failure inside the exporter, not a stubbed one.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime  # noqa: E402
from runtime import phoenix_tracing as pt  # noqa: E402

pytestmark = pytest.mark.usefixtures("_reset_tracing")

BUNDLE = {
    "slug": "genesis-builder",
    "system_prompt": "Build things.",
    "tools_advertised": ["file_write"],
    "token_budget": 1000,
    "model_hint": "auto",
    "success_criteria": None,
    "timeout_s": 30,
}


@pytest.fixture
def _reset_tracing():
    pt.reset_for_tests()
    yield
    pt.reset_for_tests()


@pytest.fixture(autouse=True)
def _hermetic_audit_db(tmp_path, monkeypatch):
    """Point the audit chain at a throwaway DB.

    ``runtime.genesis_audit`` caches a process-wide AuditLog singleton, so these
    tests would otherwise pass or fail depending on whether some earlier module
    happened to build it first. The singleton is cleared on the way in *and* out
    so this module neither depends on nor leaks that global state.
    """
    from runtime import genesis_audit

    monkeypatch.setenv("GENESIS_AUDIT_DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(genesis_audit, "_instance", None, raising=False)
    yield
    genesis_audit._instance = None


@pytest.fixture
def memory_spans(monkeypatch):
    """Route spans to an in-memory exporter instead of a real collector."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setattr(pt, "_build_tracer", lambda: provider.get_tracer("test"))
    pt.reset_for_tests()
    return exporter


def _llm_responses():
    return [
        {
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "file_write",
                                 "arguments": '{"path": "out.txt", "content": "hi"}'},
                }],
            }}],
            "model": "claude-sonnet-4-5",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
        {
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "model": "claude-sonnet-4-5",
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        },
    ]


async def _run(runtime, tmp_path):
    responses = _llm_responses()

    async def fake_llm(model, messages, schemas, token_budget):
        return responses.pop(0)

    with patch.object(runtime, "_call_llm_inner", side_effect=fake_llm):
        return await runtime._run_loop(
            BUNDLE, "write a file", {}, "job-trace-test", tmp_path, None, "session-1"
        )


def test_agent_run_emits_agent_llm_and_tool_spans(memory_spans, tmp_path):
    """Without instrumentation no spans exist at all — this is the core assertion."""
    runtime = AgentRuntime("https://router.invalid", "router-key")
    asyncio.run(_run(runtime, tmp_path))

    spans = memory_spans.get_finished_spans()
    names = [s.name for s in spans]
    assert any(n == "llm.completion" for n in names), names
    assert any(n == "tool.file_write" for n in names), names

    llm = next(s for s in spans if s.name == "llm.completion")
    assert llm.attributes[pt.SPAN_KIND] == "LLM"
    assert llm.attributes[pt.LLM_TOKEN_TOTAL] == 10
    assert llm.attributes[pt.LLM_TOKEN_PROMPT] == 7
    assert llm.attributes["llm.model_name.routed"] == "claude-sonnet-4-5"

    tool = next(s for s in spans if s.name == "tool.file_write")
    assert tool.attributes[pt.SPAN_KIND] == "TOOL"
    assert tool.attributes[pt.TOOL_NAME] == "file_write"
    assert tool.attributes["genesis.agent.slug"] == "genesis-builder"
    assert tool.attributes["genesis.job.id"] == "job-trace-test"
    assert "genesis.tool.ok" in tool.attributes


def test_llm_span_covers_both_provider_paths(memory_spans, tmp_path, monkeypatch):
    """The span records which wire format ran, so one dashboard covers both paths."""
    seen = []
    for provider, flag in (("anthropic", "anthropic"), ("openai_gateway", "openai")):
        monkeypatch.setenv("GENESIS_LLM_PROVIDER", flag)
        memory_spans.clear()
        runtime = AgentRuntime("https://router.invalid", "router-key")
        asyncio.run(_run(runtime, tmp_path))
        llm = next(s for s in memory_spans.get_finished_spans()
                   if s.name == "llm.completion")
        seen.append(llm.attributes[pt.LLM_PROVIDER])
    assert seen == ["anthropic", "openai_gateway"]


def test_success_criteria_verdict_lands_on_agent_span(memory_spans):
    """The verdict an operator filters on must be a span attribute, not just a log."""
    captured = {}

    class _Span:
        def set_attribute(self, k, v):
            captured[k] = v

    AgentRuntime._annotate_agent_span(_Span(), {
        "ok": False,
        "error": "success_criteria_failed",
        "turns": 3,
        "resource_usage": {"llm_calls": 3, "total_tokens": 900, "files_written": 1},
        "trace": {"tool_calls": [{"tool_name": "file_write", "ok": True},
                                 {"tool_name": "run_code", "ok": False}]},
        "success_criteria_eval": {"ok": False, "failed": ["min_successful_tool_calls"]},
    })
    assert captured["genesis.success_criteria.ok"] is False
    assert captured["genesis.success_criteria.failed"] == ["min_successful_tool_calls"]
    assert captured["genesis.turn.count"] == 3
    assert captured["genesis.tool_calls.count"] == 2
    assert captured["genesis.tool_calls.failed"] == 1
    assert captured[pt.LLM_TOKEN_TOTAL] == 900


# ---------------------------------------------------------------------------
# Fail-open: the safety property that outranks every other requirement here.
# ---------------------------------------------------------------------------

def test_agent_run_completes_when_phoenix_is_unreachable(monkeypatch, tmp_path):
    """A dead collector must not fail, slow, or alter an agent run.

    Port 9 (discard) with nothing listening gives a real connection failure
    inside a real OTLPSpanExporter — the exact production outage shape.
    """
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("PHOENIX_API_KEY", "not-a-real-key")
    pt.reset_for_tests()

    runtime = AgentRuntime("https://router.invalid", "router-key")
    result = asyncio.run(_run(runtime, tmp_path))

    assert result["ok"] is True
    assert result["response"] == "done"
    assert result["trace"]["tool_calls"][0]["tool_name"] == "file_write"
    # Flushing to a dead endpoint returns False rather than raising or hanging.
    assert pt.flush(2000) in (True, False)


def test_run_completes_when_tracer_construction_explodes(monkeypatch, tmp_path):
    """A broken/incompatible OTel install must degrade to no-tracing, not crash."""
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")

    def boom():
        raise RuntimeError("otel exploded")

    monkeypatch.setattr(pt, "_build_tracer", boom)
    pt.reset_for_tests()

    assert pt.get_tracer() is None
    assert "otel exploded" in (pt.disabled_reason() or "")

    runtime = AgentRuntime("https://router.invalid", "router-key")
    result = asyncio.run(_run(runtime, tmp_path))
    assert result["ok"] is True


def test_span_yields_none_when_start_fails(monkeypatch):
    """Caller body still executes when the tracer raises at span start."""

    class _BadTracer:
        def start_as_current_span(self, *_a, **_k):
            raise RuntimeError("cannot start span")

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setattr(pt, "_build_tracer", lambda: _BadTracer())
    pt.reset_for_tests()

    ran = []
    with pt.span("x", kind="AGENT") as sp:
        ran.append(sp)
    assert ran == [None]


def test_tracing_disabled_without_endpoint(monkeypatch, tmp_path):
    """Unconfigured is a normal, silent state — not an error."""
    for var in ("PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_ENDPOINT",
                "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    pt.reset_for_tests()

    assert pt.tracing_enabled() is False
    assert pt.get_tracer() is None

    runtime = AgentRuntime("https://router.invalid", "router-key")
    assert asyncio.run(_run(runtime, tmp_path))["ok"] is True


# ---------------------------------------------------------------------------
# Confidentiality: what may leave the box.
# ---------------------------------------------------------------------------

def test_no_prompt_content_on_spans_by_default(memory_spans, tmp_path):
    """Default posture is ids/counts/verdicts only — no prompt or tool payloads."""
    runtime = AgentRuntime("https://router.invalid", "router-key")
    asyncio.run(_run(runtime, tmp_path))
    for span in memory_spans.get_finished_spans():
        assert pt.INPUT_VALUE not in span.attributes, span.name
        assert pt.OUTPUT_VALUE not in span.attributes, span.name


def test_offbox_collector_requires_explicit_content_optin(monkeypatch):
    """Phoenix Cloud is third-party: content needs a second, deliberate switch."""
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "https://app.phoenix.arize.com/s/e4l")
    monkeypatch.setenv("PHOENIX_TRACE_CONTENT", "1")
    monkeypatch.delenv("PHOENIX_ALLOW_CONTENT_OFFBOX", raising=False)
    pt.reset_for_tests()

    assert pt.endpoint_is_offbox() is True
    assert pt.content_tracing_enabled() is False
    assert pt.safe_content("secret vault text") is None

    monkeypatch.setenv("PHOENIX_ALLOW_CONTENT_OFFBOX", "1")
    assert pt.content_tracing_enabled() is True


def test_local_collector_allows_content_with_single_optin(monkeypatch):
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("PHOENIX_TRACE_CONTENT", "1")
    monkeypatch.delenv("PHOENIX_ALLOW_CONTENT_OFFBOX", raising=False)
    pt.reset_for_tests()

    assert pt.endpoint_is_offbox() is False
    assert pt.content_tracing_enabled() is True


# ---------------------------------------------------------------------------
# eval/ migration: Phoenix supersedes LangSmith, redaction survives intact.
# ---------------------------------------------------------------------------

def test_phoenix_replaced_langsmith_outright(monkeypatch):
    """Phoenix is the only backend. A LangSmith key must change nothing.

    The migration's failure mode would be a surviving dormant path: a
    ``LANGSMITH_API_KEY`` in the environment quietly re-arming a second exporter
    that ships the same prompts and responses to a second vendor. Setting one
    here must have no effect at all.
    """
    from eval import traceable

    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "1")
    for var in ("PHOENIX_COLLECTOR_ENDPOINT", "PHOENIX_ENDPOINT",
                "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    pt.reset_for_tests()
    assert traceable.tracing_enabled() is False   # no Phoenix -> no tracing, key ignored

    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    pt.reset_for_tests()
    assert traceable.phoenix_enabled() is True
    assert traceable.tracing_enabled() is True    # Phoenix is the backend


def test_no_langsmith_runtime_coupling_remains(monkeypatch):
    """No module imports langsmith or reads a LANGSMITH_* variable."""
    import pathlib
    import re

    coupling = re.compile(r"^\s*(import|from)\s+langsmith|LANGSMITH_[A-Z_]+", re.MULTILINE)
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list(root.glob("*.py")) + list(root.glob("eval/*.py")) + list(
        root.glob("runtime/*.py")
    ):
        if path.name.startswith("test_"):
            continue
        if coupling.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"still coupled to langsmith: {offenders}"


def test_eval_span_redacts_secrets_and_withholds_content(memory_spans, monkeypatch):
    """eval/redaction.py must still fire on the Phoenix path."""
    from eval import traceable
    from eval.genesis_client import AgentRunResult, Outcome

    result = AgentRunResult(
        outcome=Outcome.SUCCESS, slug="genesis-builder",
        requested_slug="genesis-builder", mode="full", http_status=200,
        elapsed_ms=1234, attempts=1, warmed=True, slug_resolution="exact",
        body={"response": "here is the key sk-abcdefghijklmnopqrstuvwxyz012345",
              "agentName": "Genesis Builder"},
    )
    traceable._emit_phoenix_span(result, None, {"task": "do the thing"})

    span = next(s for s in memory_spans.get_finished_spans()
                if s.name == traceable.RUN_NAME)
    assert span.attributes["genesis.eval.slug"] == "genesis-builder"
    assert span.attributes["genesis.eval.outcome"] == Outcome.SUCCESS.value
    assert span.attributes["genesis.eval.elapsed_ms"] == 1234
    # Content is withheld entirely by default...
    assert pt.OUTPUT_VALUE not in span.attributes
    assert pt.INPUT_VALUE not in span.attributes
    # ...and the secret is nowhere in the serialized span either way.
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in str(dict(span.attributes))


def test_eval_span_content_is_redacted_when_explicitly_enabled(memory_spans, monkeypatch):
    """With content on, the secret is still scrubbed by eval/redaction.py."""
    from eval import traceable
    from eval.genesis_client import AgentRunResult, Outcome

    monkeypatch.setenv("PHOENIX_TRACE_CONTENT", "1")
    result = AgentRunResult(
        outcome=Outcome.SUCCESS, slug="genesis-builder",
        requested_slug="genesis-builder", mode="full", http_status=200,
        elapsed_ms=10, attempts=1, warmed=True, slug_resolution="exact",
        body={"response": "token sk-abcdefghijklmnopqrstuvwxyz012345 done",
              "agentName": "Genesis Builder"},
    )
    traceable._emit_phoenix_span(result, None, {"task": "do the thing"})

    span = next(s for s in memory_spans.get_finished_spans()
                if s.name == traceable.RUN_NAME)
    assert pt.OUTPUT_VALUE in span.attributes
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in span.attributes[pt.OUTPUT_VALUE]
    assert "done" in span.attributes[pt.OUTPUT_VALUE]


def test_content_is_redacted_when_enabled(monkeypatch):
    """Even opted-in content passes through the shared secret redactor."""
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("PHOENIX_TRACE_CONTENT", "1")
    pt.reset_for_tests()

    out = pt.safe_content("token sk-abcdefghijklmnopqrstuvwxyz123456")
    assert out is not None
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
