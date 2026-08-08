"""test_meta_orchestration_trace.py — Local trace-proof test for Meta Agent orchestration.

Mocks the LLM network calls but does NOT mock genesis_call itself. The real
genesis_call tool is dispatched by AgentRuntime, which calls execute_agent
for the child agents. This proves the full tool-call chain and trace recording.

Run:
    pytest test_meta_orchestration_trace.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mock_psycopg():
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = MagicMock(return_value=MagicMock())
    psycopg.rows = types.ModuleType("psycopg.rows")
    psycopg.rows.dict_row = MagicMock()
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", psycopg.rows)


def _tool_call_msg(tc_id: str, fn_name: str, arguments: dict) -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": fn_name, "arguments": json.dumps(arguments)},
    }


# ---------------------------------------------------------------------------
# Mock LLM responses — ordered by call sequence across meta + child agents
# ---------------------------------------------------------------------------

def _llm_responses():
    """Return LLM mock responses in execution order:
    1. Meta turn 1  → genesis_call(agent="genesis-research", ...)
    2. Research run → final text
    3. Meta turn 2  → genesis_call(agent="genesis-finance", ...)
    4. Finance run  → final text
    5. Meta turn 3  → synthesis
    """
    return [
        # 1. Meta — decides to call genesis_call for research
        {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [_tool_call_msg(
                        "tc_research_001",
                        "genesis_call",
                        {"agent": "genesis-research", "task": "What year was Python created?"},
                    )],
                }
            }],
            "usage": {"total_tokens": 150},
        },
        # 2. Research agent — final text response
        {
            "choices": [{"message": {
                "content": "Python was created in 1991.",
                "tool_calls": None,
            }}],
            "usage": {"total_tokens": 60},
        },
        # 3. Meta — decides to call genesis_call for finance
        {
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [_tool_call_msg(
                        "tc_finance_001",
                        "genesis_call",
                        {"agent": "genesis-finance", "task": "What is 10 divided by 2?"},
                    )],
                }
            }],
            "usage": {"total_tokens": 120},
        },
        # 4. Finance agent — final text response
        {
            "choices": [{"message": {
                "content": "10 divided by 2 is 5.",
                "tool_calls": None,
            }}],
            "usage": {"total_tokens": 50},
        },
        # 5. Meta — synthesis
        {
            "choices": [{"message": {
                "content": (
                    "Delegation Summary:\n"
                    "- genesis-research: Python was created in 1991.\n"
                    "- genesis-finance: 10 divided by 2 is 5."
                ),
                "tool_calls": None,
            }}],
            "usage": {"total_tokens": 90},
        },
    ]


# ---------------------------------------------------------------------------
# Bundle factory
# ---------------------------------------------------------------------------

def _bundle(slug: str, tools: list[str] | None = None) -> dict:
    return {
        "slug": slug,
        "system_prompt": f"You are the {slug} agent.",
        "tools_advertised": tools or [],
        "token_budget": 8000,
        "model_hint": "auto",
        "success_criteria": None,
        "conduit_budget_cents": 0,
        "timeout_s": 120,
    }


def _make_bundle_loader(bundles: dict[str, dict]):
    """Return a load_bundle side_effect fn that returns the right bundle per slug."""
    def _load(slug: str):
        return bundles.get(slug)
    return _load


# ===========================================================================
# The trace-proof test
# ===========================================================================

class TestMetaOrchestrationTrace:
    """Proves that AgentRuntime records structured genesis_call entries in trace.tool_calls."""

    # [P0] [CRITICAL] — trace.tool_calls is a non-empty list after Meta delegates
    def test_meta_trace_tool_calls_is_a_list(self):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()

        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
            "genesis-finance": _bundle("genesis-finance"),
        }
        mock_llm = AsyncMock(side_effect=_llm_responses())

        with patch("agent_runtime.load_bundle", side_effect=_make_bundle_loader(bundles)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1/chat/completions",
                llm_key="test-key",
            )
            result = _run(runtime.execute_agent(
                "genesis-meta",
                "Delegate to research and finance agents.",
                {},
                job_id="parent-job-001",
            ))

        assert result.get("ok") is True, f"Expected ok=True, got: {result.get('error')}"
        trace = result.get("trace", {})
        tool_calls = trace.get("tool_calls")
        assert isinstance(tool_calls, list), (
            f"Expected trace.tool_calls to be a list, got {type(tool_calls).__name__}. "
            "agent_runtime.py must record structured tool call dicts, not just a turn count."
        )
        assert len(tool_calls) > 0, (
            "Expected at least one entry in trace.tool_calls. "
            "The genesis_call dispatch was not recorded."
        )

    # [P0] [CRITICAL] — at least two genesis_call entries exist with correct fields
    def test_meta_trace_has_two_genesis_call_entries(self):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()

        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
            "genesis-finance": _bundle("genesis-finance"),
        }
        mock_llm = AsyncMock(side_effect=_llm_responses())

        with patch("agent_runtime.load_bundle", side_effect=_make_bundle_loader(bundles)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1/chat/completions",
                llm_key="test-key",
            )
            result = _run(runtime.execute_agent(
                "genesis-meta",
                "Delegate to research and finance agents.",
                {},
                job_id="parent-job-002",
            ))

        trace = result.get("trace", {})
        tool_calls = trace.get("tool_calls", [])
        genesis_calls = [tc for tc in tool_calls if tc.get("tool_name") == "genesis_call"]

        assert len(genesis_calls) >= 2, (
            f"Expected >= 2 genesis_call entries in trace, got {len(genesis_calls)}. "
            f"Full tool_calls: {json.dumps(tool_calls, default=str)}"
        )

    # [P0] [CRITICAL] — first genesis_call targets genesis-research with child_job_id + child_ok
    def test_genesis_call_research_entry_has_required_fields(self):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()

        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
            "genesis-finance": _bundle("genesis-finance"),
        }
        mock_llm = AsyncMock(side_effect=_llm_responses())

        with patch("agent_runtime.load_bundle", side_effect=_make_bundle_loader(bundles)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1/chat/completions",
                llm_key="test-key",
            )
            result = _run(runtime.execute_agent(
                "genesis-meta",
                "Delegate to research and finance agents.",
                {},
                job_id="parent-job-003",
            ))

        trace = result.get("trace", {})
        tool_calls = trace.get("tool_calls", [])
        genesis_calls = [tc for tc in tool_calls if tc.get("tool_name") == "genesis_call"]
        assert genesis_calls, "No genesis_call entries found in trace.tool_calls"

        research_call = next(
            (gc for gc in genesis_calls if gc.get("target_agent_slug") == "genesis-research"),
            None,
        )
        assert research_call is not None, (
            f"No genesis_call with target_agent_slug='genesis-research' in trace. "
            f"Found targets: {[gc.get('target_agent_slug') for gc in genesis_calls]}"
        )
        assert research_call.get("child_job_id"), (
            "genesis-research call missing child_job_id. "
            "genesis_call_tool.py must generate and return a child_job_id."
        )
        assert research_call.get("child_ok") is True, (
            f"Expected child_ok=True for genesis-research call, "
            f"got child_ok={research_call.get('child_ok')}."
        )
        assert research_call.get("child_response_summary"), (
            "genesis-research call missing child_response_summary."
        )

    # [P0] [CRITICAL] — second genesis_call targets genesis-finance
    def test_genesis_call_finance_entry_has_required_fields(self):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()

        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
            "genesis-finance": _bundle("genesis-finance"),
        }
        mock_llm = AsyncMock(side_effect=_llm_responses())

        with patch("agent_runtime.load_bundle", side_effect=_make_bundle_loader(bundles)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1/chat/completions",
                llm_key="test-key",
            )
            result = _run(runtime.execute_agent(
                "genesis-meta",
                "Delegate to research and finance agents.",
                {},
                job_id="parent-job-004",
            ))

        trace = result.get("trace", {})
        tool_calls = trace.get("tool_calls", [])
        genesis_calls = [tc for tc in tool_calls if tc.get("tool_name") == "genesis_call"]

        finance_call = next(
            (gc for gc in genesis_calls if gc.get("target_agent_slug") == "genesis-finance"),
            None,
        )
        assert finance_call is not None, (
            f"No genesis_call with target_agent_slug='genesis-finance' in trace. "
            f"Found targets: {[gc.get('target_agent_slug') for gc in genesis_calls]}"
        )
        assert finance_call.get("child_job_id"), "genesis-finance call missing child_job_id"
        assert finance_call.get("child_ok") is True, (
            f"Expected child_ok=True for genesis-finance call, "
            f"got {finance_call.get('child_ok')}."
        )

    # [P1] — parent_job_id is propagated into each trace record
    def test_trace_records_contain_parent_job_id(self):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()

        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
            "genesis-finance": _bundle("genesis-finance"),
        }
        mock_llm = AsyncMock(side_effect=_llm_responses())
        parent_id = "parent-job-trace-check"

        with patch("agent_runtime.load_bundle", side_effect=_make_bundle_loader(bundles)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1/chat/completions",
                llm_key="test-key",
            )
            result = _run(runtime.execute_agent(
                "genesis-meta", "Delegate.", {}, job_id=parent_id,
            ))

        trace = result.get("trace", {})
        tool_calls = trace.get("tool_calls", [])
        assert tool_calls, "No tool_calls in trace"
        for tc in tool_calls:
            assert tc.get("parent_job_id") == parent_id, (
                f"Expected parent_job_id='{parent_id}' in record, "
                f"got '{tc.get('parent_job_id')}'. "
                f"Tool: {tc.get('tool_name')}"
            )

    # [P1] — child_job_id is prefixed with 'child-'
    def test_child_job_id_has_expected_prefix(self):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()

        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
            "genesis-finance": _bundle("genesis-finance"),
        }
        mock_llm = AsyncMock(side_effect=_llm_responses())

        with patch("agent_runtime.load_bundle", side_effect=_make_bundle_loader(bundles)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1/chat/completions",
                llm_key="test-key",
            )
            result = _run(runtime.execute_agent(
                "genesis-meta", "Delegate.", {}, job_id="parent-job-prefix-check",
            ))

        trace = result.get("trace", {})
        genesis_calls = [
            tc for tc in trace.get("tool_calls", [])
            if tc.get("tool_name") == "genesis_call"
        ]
        for gc in genesis_calls:
            cid = gc.get("child_job_id", "")
            assert cid.startswith("child-"), (
                f"Expected child_job_id to start with 'child-', got '{cid}'."
            )

    # [P0] — genesis_call without _runtime returns a clean error, not an exception
    def test_genesis_call_without_runtime_returns_error_dict(self):
        from tools.genesis_call_tool import genesis_call

        result = _run(genesis_call(agent="genesis-research", task="test", _runtime=None))
        assert result.get("ok") is False, (
            f"Expected ok=False when _runtime is None, got: {result}"
        )
        assert result.get("error") == "no_runtime_in_context", (
            f"Expected error='no_runtime_in_context', got '{result.get('error')}'."
        )
        assert result.get("target_agent_slug") == "genesis-research", (
            "target_agent_slug should be returned even on failure."
        )
