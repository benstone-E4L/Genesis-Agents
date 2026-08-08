"""test_observability_events.py — Phase 4 observability event tests."""
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


class TestObservabilityModule:
    def test_emit_and_get_events(self, tmp_path):
        """emit_event writes to jsonl, get_events reads it back."""
        import runtime.observability as obs
        job_id = "obs-unit-001"
        # Point to tmp_path
        original_root = obs.WORKSPACE_ROOT
        obs.WORKSPACE_ROOT = str(tmp_path)
        try:
            obs.emit_event(job_id, "test.event", {"foo": "bar"})
            obs.emit_event(job_id, "test.event2", {"baz": 42})
            events = obs.get_events(job_id)
            assert len(events) == 2
            assert events[0]["event_type"] == "test.event"
            assert events[0]["foo"] == "bar"
            assert events[1]["event_type"] == "test.event2"
            assert events[1]["baz"] == 42
        finally:
            obs.WORKSPACE_ROOT = original_root

    def test_get_events_returns_empty_for_unknown_job(self, tmp_path):
        import runtime.observability as obs
        original_root = obs.WORKSPACE_ROOT
        obs.WORKSPACE_ROOT = str(tmp_path)
        try:
            events = obs.get_events("no-such-job-xyz")
            assert events == []
        finally:
            obs.WORKSPACE_ROOT = original_root

    def test_events_have_required_fields(self, tmp_path):
        import runtime.observability as obs
        job_id = "obs-fields-001"
        original_root = obs.WORKSPACE_ROOT
        obs.WORKSPACE_ROOT = str(tmp_path)
        try:
            obs.emit_event(job_id, "job.created", {"agent_slug": "genesis-meta"})
            events = obs.get_events(job_id)
            e = events[0]
            assert "ts" in e, "event must have timestamp"
            assert "job_id" in e
            assert "event_type" in e
            assert e["job_id"] == job_id
        finally:
            obs.WORKSPACE_ROOT = original_root

    def test_emit_does_not_raise_on_bad_path(self):
        """emit_event must never raise (fire-and-forget)."""
        import runtime.observability as obs
        original_root = obs.WORKSPACE_ROOT
        obs.WORKSPACE_ROOT = "/nonexistent_root_xyz_123"
        try:
            # Should not raise
            obs.emit_event("job-xyz", "test.event")
        finally:
            obs.WORKSPACE_ROOT = original_root


class TestObservabilityIntegration:
    """Verify that agent_runtime emits events during a real run."""

    def test_agent_run_emits_job_created(self, tmp_path):
        """After execute_agent, events.jsonl must contain job.created."""
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        import runtime.observability as obs
        import runtime.workspace_manager as wm

        register_default_tools()
        _mock_psycopg()

        # Override workspace roots so events go to tmp_path
        original_obs = obs.WORKSPACE_ROOT
        original_wm = wm.WORKSPACE_ROOT
        obs.WORKSPACE_ROOT = str(tmp_path)
        wm.WORKSPACE_ROOT = str(tmp_path)

        job_id = "obs-int-001"
        wm._registry.pop(job_id, None)

        bundles = {"genesis-research": _bundle("genesis-research")}
        mock_llm = AsyncMock(return_value={
            "choices": [{"message": {"content": "Result", "tool_calls": None}}],
            "usage": {"total_tokens": 50},
        })

        try:
            with patch("agent_runtime.load_bundle", side_effect=lambda s: bundles.get(s)), \
                 patch.object(AgentRuntime, "_call_llm", mock_llm):
                runtime = AgentRuntime(llm_url="https://mock.internal/v1", llm_key="test")
                _run(runtime.execute_agent("genesis-research", "Fetch data", {}, job_id=job_id))

            events = obs.get_events(job_id)
            event_types = [e["event_type"] for e in events]
            assert "job.created" in event_types, (
                f"job.created event missing from events.jsonl. Got: {event_types}"
            )
        finally:
            obs.WORKSPACE_ROOT = original_obs
            wm.WORKSPACE_ROOT = original_wm
            wm._registry.pop(job_id, None)

    def test_agent_run_emits_agent_started(self, tmp_path):
        from agent_runtime import AgentRuntime
        from tools import register_default_tools
        import runtime.observability as obs
        import runtime.workspace_manager as wm

        register_default_tools()
        _mock_psycopg()

        original_obs = obs.WORKSPACE_ROOT
        original_wm = wm.WORKSPACE_ROOT
        obs.WORKSPACE_ROOT = str(tmp_path)
        wm.WORKSPACE_ROOT = str(tmp_path)

        job_id = "obs-int-002"
        wm._registry.pop(job_id, None)

        bundles = {"genesis-research": _bundle("genesis-research")}
        mock_llm = AsyncMock(return_value={
            "choices": [{"message": {"content": "Done", "tool_calls": None}}],
            "usage": {"total_tokens": 40},
        })

        try:
            with patch("agent_runtime.load_bundle", side_effect=lambda s: bundles.get(s)), \
                 patch.object(AgentRuntime, "_call_llm", mock_llm):
                runtime = AgentRuntime(llm_url="https://mock.internal/v1", llm_key="test")
                _run(runtime.execute_agent("genesis-research", "Do something", {}, job_id=job_id))

            events = obs.get_events(job_id)
            event_types = [e["event_type"] for e in events]
            assert "agent.started" in event_types, (
                f"agent.started event missing. Got: {event_types}"
            )
        finally:
            obs.WORKSPACE_ROOT = original_obs
            wm.WORKSPACE_ROOT = original_wm
            wm._registry.pop(job_id, None)
