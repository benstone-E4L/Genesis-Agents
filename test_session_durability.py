"""test_session_durability.py — Phase 3 durable session tests.

Verifies that:
- execute_agent() attaches a session_id to every result
- session_id is included in the trace
- parent-child session linkage works via genesis_call
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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mock_psycopg():
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = MagicMock(return_value=MagicMock())
    psycopg.rows = types.ModuleType("psycopg.rows")
    psycopg.rows.dict_row = MagicMock()
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", psycopg.rows)


def _tool_call_msg(tc_id, fn_name, arguments):
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": fn_name, "arguments": json.dumps(arguments)},
    }


def _simple_final_response(text="done"):
    return {
        "choices": [{"message": {"content": text, "tool_calls": None}}],
        "usage": {"total_tokens": 50},
    }


def _bundle(slug, tools=None):
    return {
        "slug": slug,
        "system_prompt": f"You are {slug}.",
        "tools_advertised": tools or [],
        "token_budget": 4000,
        "model_hint": "auto",
        "success_criteria": None,
        "conduit_budget_cents": 0,
        "timeout_s": 120,
    }


class TestSessionDurability:
    def _run_meta_with_genesis_call(self):
        """Run genesis-meta that delegates to genesis-research and returns result."""
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()
        _mock_psycopg()

        bundles = {
            "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
            "genesis-research": _bundle("genesis-research"),
        }
        responses = [
            {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [_tool_call_msg(
                            "tc1", "genesis_call",
                            {"agent": "genesis-research", "task": "Find something"},
                        )],
                    }
                }],
                "usage": {"total_tokens": 100},
            },
            _simple_final_response("Research result"),
            _simple_final_response("Meta synthesis done"),
        ]
        mock_llm = AsyncMock(side_effect=responses)

        with patch("agent_runtime.load_bundle", side_effect=lambda s: bundles.get(s)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(
                llm_url="https://mock.internal/v1",
                llm_key="test",
            )
            return _run(runtime.execute_agent(
                "genesis-meta", "Delegate.", {}, job_id="session-test-001",
            ))

    def test_result_has_session_id(self):
        result = self._run_meta_with_genesis_call()
        trace = result.get("trace", {})
        session_id = trace.get("session_id")
        assert session_id, (
            "trace.session_id must be set on every execute_agent result. "
            f"Got trace keys: {list(trace.keys())}"
        )
        assert len(session_id) > 8, f"session_id looks too short: {session_id!r}"

    def test_child_session_id_in_subagents(self):
        result = self._run_meta_with_genesis_call()
        trace = result.get("trace", {})
        subagents = trace.get("subagents", [])
        assert subagents, (
            "trace.subagents must be populated when genesis_call is used. "
            f"Got trace keys: {list(trace.keys())}"
        )
        first = subagents[0]
        assert first.get("child_session_id"), (
            f"subagent entry must have child_session_id. Got: {first}"
        )

    def test_parent_session_id_in_subagents(self):
        result = self._run_meta_with_genesis_call()
        trace = result.get("trace", {})
        parent_session_id = trace.get("session_id")
        subagents = trace.get("subagents", [])
        assert subagents
        for entry in subagents:
            assert entry.get("parent_session_id") == parent_session_id, (
                f"subagent.parent_session_id must match parent trace.session_id. "
                f"Expected {parent_session_id!r}, got {entry.get('parent_session_id')!r}"
            )

    def test_session_id_is_stable_within_run(self):
        """The same session_id must appear in trace and all subagent entries."""
        result = self._run_meta_with_genesis_call()
        trace = result.get("trace", {})
        session_id = trace.get("session_id")
        subagents = trace.get("subagents", [])
        for entry in subagents:
            assert entry["parent_session_id"] == session_id

    def test_explicit_session_id_preserved(self):
        """When caller provides session_id, it must survive into the trace."""
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        register_default_tools()
        _mock_psycopg()

        bundles = {"genesis-research": _bundle("genesis-research")}
        mock_llm = AsyncMock(return_value=_simple_final_response("answer"))

        with patch("agent_runtime.load_bundle", side_effect=lambda s: bundles.get(s)), \
             patch.object(AgentRuntime, "_call_llm", mock_llm):
            runtime = AgentRuntime(llm_url="https://mock.internal/v1", llm_key="test")
            result = _run(runtime.execute_agent(
                "genesis-research", "Find things", {},
                job_id="session-explicit-001",
                session_id="explicit-session-abc123",
            ))

        trace = result.get("trace", {})
        assert trace.get("session_id") == "explicit-session-abc123", (
            f"Explicit session_id must be preserved in trace. Got: {trace.get('session_id')!r}"
        )
