from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "chain", "depth", "expected"),
    [
        ("missing-agent", (), 0, "delegation_target_not_allowed"),
        ("genesis-pricing", (), 3, "delegation_depth_exceeded"),
        ("genesis-pricing", ("genesis-pricing",), 0, "delegation_cycle_detected"),
        ("genesis-builder", (), 0, "delegation_target_not_allowed"),
    ],
)
async def test_genesis_call_fails_closed(agent, chain, depth, expected):
    from tools.genesis_call_tool import genesis_call

    runtime = AsyncMock()
    result = await genesis_call(
        agent=agent,
        task="bounded",
        _runtime=runtime,
        _parent_agent_slug="genesis-meta",
        _delegation_chain=chain,
        _delegation_depth=depth,
    )
    assert result["ok"] is False
    assert result["error"] == expected
    runtime.execute_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_genesis_call_allowed_target_propagates_depth_and_chain():
    from tools.genesis_call_tool import genesis_call

    runtime = AsyncMock()
    runtime.execute_agent.return_value = {"ok": True, "response": "done", "trace": {}}
    result = await genesis_call(
        agent="genesis-pricing",
        task="read only",
        _runtime=runtime,
        _parent_agent_slug="genesis-meta",
        _delegation_chain=("root",),
        _delegation_depth=1,
    )
    assert result["ok"] is True
    kwargs = runtime.execute_agent.await_args.kwargs
    assert kwargs["delegation_depth"] == 2
    assert kwargs["delegation_chain"] == ("root", "genesis-meta")
    assert "network" not in kwargs["delegated_allowed_risks"]


@pytest.mark.asyncio
async def test_buyer_session_is_deleted_and_job_fails_closed(monkeypatch):
    from agent_runtime import AgentRuntime
    import conduit_sessions

    bundle = {
        "slug": "genesis-maintenance", "system_prompt": "x",
        "tools_advertised": ["conduit"], "token_budget": 100,
    }
    deleted = []
    monkeypatch.setattr(conduit_sessions, "load_session", lambda **_: {"ok": True, "session_data": {"cookies": [{"name": "x"}]}})
    monkeypatch.setattr(conduit_sessions, "delete_session", lambda **kw: deleted.append(kw["job_id"]))
    with patch("agent_runtime.load_bundle", return_value=bundle):
        result = await AgentRuntime("https://router.invalid", "x").execute_agent(
            "genesis-maintenance", "task", {}, job_id="buyer-session-job"
        )
    assert result["ok"] is False
    assert result["error"] == "buyer_session_injection_unsupported"
    assert deleted == ["buyer-session-job"]


def test_conduit_schema_matches_execution_and_excludes_eval():
    from conduit_browser import SUPPORTED_ACTIONS
    from tools.conduit_tool import CONDUIT_SCHEMA

    advertised = set(CONDUIT_SCHEMA["function"]["parameters"]["properties"]["action"]["enum"])
    assert advertised == set(SUPPORTED_ACTIONS)
    assert "eval" not in advertised


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/x"])
def test_conduit_denies_unsafe_urls(monkeypatch, url):
    from conduit_browser import validate_public_url

    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (url.split('/')[2].split(':')[0] or "127.0.0.1", 80))])
    with pytest.raises(ValueError):
        validate_public_url(url)


@pytest.mark.asyncio
async def test_conduit_request_interceptor_blocks_redirect_to_private_host(monkeypatch):
    from conduit_browser import ConduitBridge

    route = AsyncMock()
    route.request.url = "http://127.0.0.1/admin"
    bridge = ConduitBridge("route-test")
    await bridge._guard_route(route)
    route.abort.assert_awaited_once_with("blockedbyclient")
    route.continue_.assert_not_awaited()


def test_tool_discovery_is_unique_and_canonical():
    import tools

    tools._TOOLS.clear()
    tools._TOOL_SCHEMAS.clear()
    tools.register_default_tools()
    assert tools._TOOLS["github_tool"].__module__ == "tools.github_tool"
    # vercel_deploy/netlify_deploy were removed outright — E4L deploys only to
    # Azure, and these held live credentials for platforms it does not use.
    assert "vercel_deploy" not in tools._TOOLS
    assert "netlify_deploy" not in tools._TOOLS
    with pytest.raises(RuntimeError, match="duplicate tool registration"):
        tools.register_tool("github_tool", AsyncMock(), tools._TOOL_SCHEMAS["github_tool"])


def test_genesis_audit_append_tamper_and_replay(tmp_path):
    from runtime.genesis_audit import get_audit_log

    db = tmp_path / "genesis-audit.db"
    audit = get_audit_log(db)
    first = audit.log("s1", "tool_call", "web_fetch", {"url": "https://example.com"}, {"ok": True})
    second = audit.log("s1", "tool_call", "file_write", {"path": "x"}, {"ok": True})
    assert (first, second) == (1, 2)
    assert audit.verify_chain("s1") is True
    rows = audit.get_session_rows("s1")
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit_log SET outputs_digest='tampered' WHERE id=2")
        conn.commit()
    assert audit.verify_chain("s1") is False
    audit.close()


def test_audit_recursively_redacts_nested_inputs_and_outputs(tmp_path):
    from runtime.genesis_audit import get_audit_log

    planted = "sk-ant-never-store-this-value"
    audit = get_audit_log(tmp_path / "audit.db")
    audit.log(
        "secret-test", "tool_call", "web_fetch",
        {"nested": [{"authorization": f"Bearer {planted}"}], "plain": planted},
        {"result": {"credentials": [{"token": planted}]}, "text": f"Bearer {planted}"},
    )
    serialized = audit.export_session("secret-test")
    assert planted not in serialized
    assert "[REDACTED]" in serialized
    audit.close()


def test_audit_redacts_url_query_and_embedded_credentials_without_truncating(tmp_path):
    from runtime.genesis_audit import get_audit_log

    token = "query-secret-never-store"
    embedded = "sk-ant-embedded-secret-123456789"
    retained = "safe-" + ("x" * 5000)
    audit = get_audit_log(tmp_path / "audit-url.db")
    audit.log(
        "url-secret-test", "tool_call", "web_fetch",
        {"url": f"https://example.com/path?access_token={token}&page=1"},
        {"text": f"prefix {embedded} suffix", "retained": retained},
    )
    serialized = audit.export_session("url-secret-test")
    assert token not in serialized
    assert embedded not in serialized
    assert retained in serialized
    audit.close()


def test_browser_and_shell_never_degrade_security_boundaries(monkeypatch, tmp_path):
    from conduit_browser import _chromium_args
    from runtime import sandbox_manager

    assert "--no-sandbox" not in _chromium_args()
    assert "--single-process" not in _chromium_args()
    monkeypatch.setattr(sandbox_manager, "_bwrap_works", lambda: False)
    result = sandbox_manager.run_in_sandbox("j", "echo should-not-run", job_dir=tmp_path)
    assert result == {
        "ok": False,
        "error": "secure_sandbox_unavailable",
        "isolation": "unavailable",
        "message": "shell execution requires a functional bwrap boundary",
    }


@pytest.mark.asyncio
async def test_audit_append_failure_makes_tool_result_fail(monkeypatch, tmp_path):
    from agent_runtime import AgentRuntime
    import tools

    spy = AsyncMock(return_value={"ok": True, "value": "real"})
    tools.register_default_tools()
    monkeypatch.setitem(tools._TOOLS, "web_fetch", spy)
    bundle = {
        "slug": "genesis-research", "system_prompt": "research",
        "tools_advertised": ["web_fetch"], "token_budget": 100,
        "success_criteria": None, "timeout_s": 30,
    }
    responses = [
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "t", "function": {"name": "web_fetch", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "claimed done", "tool_calls": []}}]},
    ]
    runtime = AgentRuntime("https://invalid", "x")
    with patch.object(runtime, "_call_llm", AsyncMock(side_effect=responses)), patch(
        "agent_runtime.append_tool_intent", side_effect=RuntimeError("audit offline")
    ):
        result = await runtime._run_loop(bundle, "task", {}, "audit-fail-job", tmp_path, None)
    assert spy.await_count == 0
    assert result["ok"] is False
    assert result["error"] == "success_criteria_failed"
    assert "audit_preflight_failed" in result["trace"]["tool_calls"][0]["result_summary"]


@pytest.mark.asyncio
async def test_alias_cannot_evade_canonical_delegation_cycle():
    from tools.genesis_call_tool import genesis_call

    runtime = AsyncMock()
    result = await genesis_call(
        agent="genesis_pricing_x402", task="cycle", _runtime=runtime,
        _parent_agent_slug="genesis-meta", _delegation_chain=("genesis-pricing",),
    )
    assert result["error"] == "delegation_cycle_detected"
    runtime.execute_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegation_inherits_remaining_token_and_cost_budgets():
    from tools.genesis_call_tool import genesis_call

    runtime = AsyncMock()
    runtime.execute_agent.return_value = {"ok": True, "response": "ok", "trace": {}}
    await genesis_call(
        agent="genesis-pricing", task="bounded", _runtime=runtime,
        _parent_agent_slug="genesis-meta", _remaining_token_budget=321,
        _remaining_cost_budget_cents=7,
    )
    kwargs = runtime.execute_agent.await_args.kwargs
    assert kwargs["inherited_token_budget"] == 321
    assert kwargs["inherited_cost_budget_cents"] == 7


def test_audit_defaults_never_name_cato_database():
    import audit, anchor_logger

    assert "cato.db" not in audit.__doc__.lower()
    assert anchor_logger._DEFAULT_DB_PATH is None
    assert anchor_logger._DEFAULT_ANCHOR_STORE is None
    with pytest.raises(RuntimeError, match="GENESIS_AUDIT_DB_PATH"):
        audit.AuditLog()
    with pytest.raises(RuntimeError, match="GENESIS_AUDIT_DB_PATH"):
        anchor_logger.AnchorLogger()


def test_production_audit_path_must_be_explicit(monkeypatch):
    from runtime import genesis_audit

    monkeypatch.delenv("GENESIS_AUDIT_DB_PATH", raising=False)
    monkeypatch.setattr(genesis_audit, "_instance", None)
    with pytest.raises(RuntimeError, match="GENESIS_AUDIT_DB_PATH"):
        genesis_audit.get_audit_log()
