"""test_subagent_trace_contract.py — Phase 5 native subagent trace contract tests.

Proves that trace.subagents is a separate list (not tool_calls) with
child_session_id and parent_session_id fields.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
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


def _tc_msg(tc_id, fn_name, arguments):
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": fn_name, "arguments": json.dumps(arguments)},
    }


def _final(text="done"):
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


def _llm_responses_two_calls():
    """Meta calls genesis-research then genesis-finance, then synthesises."""
    return [
        {"choices": [{"message": {"content": None, "tool_calls": [
            _tc_msg("tc1", "genesis_call", {"agent": "genesis-research", "task": "Research it"}),
        ]}}], "usage": {"total_tokens": 100}},
        _final("Research answer"),
        {"choices": [{"message": {"content": None, "tool_calls": [
            _tc_msg("tc2", "genesis_call", {"agent": "genesis-finance", "task": "Finance it"}),
        ]}}], "usage": {"total_tokens": 90}},
        _final("Finance answer"),
        _final("Synthesis complete"),
    ]


def _run_meta_two_subagents():
    from agent_runtime import AgentRuntime
    from tools import register_default_tools
    register_default_tools()
    _mock_psycopg()

    bundles = {
        "genesis-meta": _bundle("genesis-meta", tools=["genesis_call"]),
        "genesis-research": _bundle("genesis-research"),
        "genesis-finance": _bundle("genesis-finance"),
    }
    mock_llm = AsyncMock(side_effect=_llm_responses_two_calls())

    with patch("agent_runtime.load_bundle", side_effect=lambda s: bundles.get(s)), \
         patch.object(AgentRuntime, "_call_llm", mock_llm):
        runtime = AgentRuntime(llm_url="https://mock.internal/v1", llm_key="test")
        return _run(runtime.execute_agent(
            "genesis-meta", "Orchestrate.", {}, job_id="subagent-trace-001",
        ))


class TestSubagentTraceContract:
    def test_trace_has_subagents_key(self):
        result = _run_meta_two_subagents()
        trace = result.get("trace", {})
        assert "subagents" in trace, (
            f"trace must have 'subagents' key. Got keys: {list(trace.keys())}"
        )

    def test_subagents_is_a_list(self):
        result = _run_meta_two_subagents()
        subagents = result.get("trace", {}).get("subagents")
        assert isinstance(subagents, list), (
            f"trace.subagents must be a list, got {type(subagents).__name__}"
        )

    def test_subagents_has_two_entries(self):
        result = _run_meta_two_subagents()
        subagents = result.get("trace", {}).get("subagents", [])
        assert len(subagents) >= 2, (
            f"Expected >= 2 subagent entries for two genesis_call dispatches. "
            f"Got {len(subagents)}: {json.dumps(subagents, default=str)}"
        )

    def test_each_subagent_has_child_session_id(self):
        result = _run_meta_two_subagents()
        subagents = result.get("trace", {}).get("subagents", [])
        assert subagents, "No subagent entries found"
        for i, entry in enumerate(subagents):
            assert entry.get("child_session_id"), (
                f"subagents[{i}] missing child_session_id. Entry: {entry}"
            )

    def test_each_subagent_has_parent_session_id(self):
        result = _run_meta_two_subagents()
        trace = result.get("trace", {})
        parent_session = trace.get("session_id")
        subagents = trace.get("subagents", [])
        assert parent_session, "Parent session_id missing from trace"
        assert subagents
        for i, entry in enumerate(subagents):
            assert entry.get("parent_session_id") == parent_session, (
                f"subagents[{i}].parent_session_id must match trace.session_id. "
                f"Expected {parent_session!r}, got {entry.get('parent_session_id')!r}"
            )

    def test_each_subagent_has_child_agent_slug(self):
        result = _run_meta_two_subagents()
        subagents = result.get("trace", {}).get("subagents", [])
        slugs = {entry.get("child_agent_slug") for entry in subagents}
        assert "genesis-research" in slugs, f"genesis-research missing from subagent slugs: {slugs}"
        assert "genesis-finance" in slugs, f"genesis-finance missing from subagent slugs: {slugs}"

    def test_each_subagent_has_parent_agent_slug(self):
        result = _run_meta_two_subagents()
        subagents = result.get("trace", {}).get("subagents", [])
        for entry in subagents:
            assert entry.get("parent_agent_slug") == "genesis-meta", (
                f"parent_agent_slug should be 'genesis-meta', got {entry.get('parent_agent_slug')!r}"
            )

    def test_subagents_separate_from_tool_calls(self):
        """trace.subagents and trace.tool_calls are distinct lists."""
        result = _run_meta_two_subagents()
        trace = result.get("trace", {})
        subagents = trace.get("subagents", [])
        tool_calls = trace.get("tool_calls", [])
        # Both must exist and be separate lists
        assert isinstance(subagents, list)
        assert isinstance(tool_calls, list)
        # tool_calls will have genesis_call entries AND subagents will have them too
        # but they are different objects with different schemas
        if subagents and tool_calls:
            assert subagents is not tool_calls, "subagents and tool_calls must be separate lists"

    def test_subagent_child_session_ids_are_distinct(self):
        """Each child invocation gets its own unique session_id."""
        result = _run_meta_two_subagents()
        subagents = result.get("trace", {}).get("subagents", [])
        if len(subagents) < 2:
            pytest.skip("Need >= 2 subagents to check distinctness")
        child_sessions = [e.get("child_session_id") for e in subagents]
        assert len(set(child_sessions)) == len(child_sessions), (
            f"child_session_ids must be unique. Got duplicates: {child_sessions}"
        )
