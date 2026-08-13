"""Smoke test for the agent runtime. Mocks LLM calls; uses real bundle_loader."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from agent_runtime import AgentRuntime, _check_success_criteria, _deployment_side_effect_authorized
from bundle_loader import load_bundle, list_bundles, resolve_bundle_slug


def test_bundles_present():
    """At least 20 bundles must be available."""
    slugs = list_bundles()
    assert len(slugs) >= 20, f"too few bundles: {slugs}"
    # Spot-check a few core slugs
    for slug in ["genesis-meta", "genesis-research", "genesis-builder"]:
        assert slug in slugs, f"missing slug: {slug}"


def test_bundle_load_research():
    b = load_bundle("genesis-research")
    assert b is not None
    assert b["slug"] == "genesis-research"
    assert b["system_prompt"]
    assert "conduit" in b.get("tools_advertised", [])


def test_resolve_bundle_slug_x402_and_aliases():
    assert resolve_bundle_slug("genesis_builder_x402") == "genesis-builder"
    assert resolve_bundle_slug("genesis_legal_x402") == "genesis-legal"
    assert resolve_bundle_slug("legal_agent") == "genesis-legal"
    # onboarding_agent has its own bundle; aliasing it onto genesis-hr silently
    # served an HR persona to onboarding callers and orphaned genesis-onboarding.
    assert resolve_bundle_slug("onboarding_agent") == "genesis-onboarding"
    assert resolve_bundle_slug("genesis_hr_x402") == "genesis-hr"
    assert load_bundle("onboarding_agent")["slug"] != load_bundle("genesis_hr_x402")["slug"]
    assert load_bundle("onboarding_agent")["slug"] == "genesis-onboarding"
    assert resolve_bundle_slug("genesis_meta_agent") == "genesis-meta"
    assert load_bundle("genesis_qa_x402") is not None


@pytest.mark.asyncio
async def test_runtime_unknown_slug():
    rt = AgentRuntime(llm_url="http://fake", llm_key="fake")
    result = await rt.execute_agent("does-not-exist", "test", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_slug"


def test_tool_capable_success_requires_real_successful_tool_evidence():
    prose_only = {
        "response": "I completed the work",
        "trace": {"tool_calls": []},
    }
    result = _check_success_criteria(None, prose_only, require_tool_evidence=True)
    assert result["ok"] is False
    assert result["failed"][0]["type"] == "min_successful_tool_calls"

    evidenced = {
        "response": "Completed",
        "trace": {"tool_calls": [{"tool_name": "web_fetch", "ok": True}]},
    }
    assert _check_success_criteria(None, evidenced, require_tool_evidence=True)["ok"] is True


def test_bundle_cannot_override_tool_evidence_with_prose_only_criterion():
    result = _check_success_criteria(
        [{"type": "non_empty"}],
        {"response": "claimed success", "trace": {"tool_calls": []}},
        require_tool_evidence=True,
    )
    assert result["ok"] is False
    assert any(f["type"] == "min_successful_tool_calls" for f in result["failed"])


def test_deployment_authorization_context_is_separated_from_model_params():
    """Authorization material is extracted for the dispatcher, never for the model."""
    context, clean = _deployment_side_effect_authorized(
        {
            "_action_grant": "grant-token",
            "_request_principal_id": "service:cato",
            "_request_tenant_id": "e4l",
            "target": "preview",
        }
    )
    assert context == {
        "grant": "grant-token",
        "principal_id": "service:cato",
        "tenant_id": "e4l",
    }
    assert clean == {"target": "preview"}


def test_obsolete_static_deployment_token_is_stripped_and_confers_nothing():
    """The old bearer-style token must not survive as a compatibility path.

    A static shared string is replayable and not bound to the tool or its args,
    so accepting it would reintroduce exactly the authority the action grant
    replaced.  It must be removed from params AND produce an empty grant.
    """
    context, clean = _deployment_side_effect_authorized(
        {"_deployment_approval_token": "server-only-approval", "target": "preview"}
    )
    assert clean == {"target": "preview"}
    assert "_deployment_approval_token" not in clean
    assert context["grant"] == ""


_GRANT_KEY = "unit-test-action-grant-key-0123456789ab"


def _deploy_grant(tmp_path, *, tool="github_tool", args=None, principal="service:cato"):
    from runtime.action_grants import issue_action_grant

    return issue_action_grant(
        principal_id=principal,
        tenant_id="e4l",
        tool=tool,
        args=args if args is not None else {},
        authorization_id="auth-1",
        key=_GRANT_KEY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("presented", "expected_calls"),
    [(None, 0), ("wrong", 0), ("valid-grant", 1)],
)
async def test_deployment_dispatch_requires_server_approval_and_never_leaks_token(
    monkeypatch, tmp_path, presented, expected_calls
):
    """Hostile dispatch-seam proof: auth denial happens before tool invocation."""
    import tools
    import runtime.genesis_audit as genesis_audit

    monkeypatch.setenv("GENESIS_ACTION_GRANT_KEY", _GRANT_KEY)
    monkeypatch.setenv("GENESIS_AUTH_DB_PATH", str(tmp_path / "grants.db"))
    monkeypatch.setenv("GENESIS_AUDIT_DB_PATH", str(tmp_path / "deploy-audit.db"))
    monkeypatch.setattr(genesis_audit, "_instance", None)
    spy = AsyncMock(return_value={"ok": True, "result": {"commit_sha": "deadbeef"}})
    tools.register_default_tools()
    monkeypatch.setitem(tools._TOOLS, "github_tool", spy)
    bundle = {
        "slug": "genesis-deploy",
        "system_prompt": "Deploy only with proof.",
        "tools_advertised": ["github_tool"],
        "token_budget": 1000,
        "model_hint": "auto",
        "success_criteria": None,
        "timeout_s": 30,
    }
    seen_messages = []
    responses = [
        {
            "choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "tc-1",
                    "function": {"name": "github_tool", "arguments": "{}"},
                }],
            }}],
            "usage": {"total_tokens": 10},
        },
        {
            "choices": [{"message": {"content": "complete", "tool_calls": []}}],
            "usage": {"total_tokens": 10},
        },
    ]

    async def fake_llm(model, messages, schemas, token_budget):
        seen_messages.extend(messages)
        return responses.pop(0)

    grant = _deploy_grant(tmp_path)
    params = {
        "target": "preview",
        "_request_principal_id": "service:cato",
        "_request_tenant_id": "e4l",
    }
    if presented == "valid-grant":
        params["_action_grant"] = grant
    elif presented is not None:
        params["_action_grant"] = presented

    runtime = AgentRuntime("https://router.invalid", "router-key")
    with patch.object(runtime, "_call_llm", side_effect=fake_llm):
        result = await runtime._run_loop(
            bundle, "deploy preview", params, "job-deploy-test", tmp_path, None, "session-1"
        )

    assert spy.await_count == expected_calls
    serialized_messages = str(seen_messages)
    assert grant not in serialized_messages
    assert "_action_grant" not in serialized_messages
    assert "wrong" not in serialized_messages
    if expected_calls == 0:
        calls = result["trace"]["tool_calls"]
        assert calls[0]["ok"] is False
        assert "deployment_approval_required" in calls[0]["result_summary"]
        assert result["ok"] is False
        assert result["error"] == "success_criteria_failed"
    else:
        assert result["ok"] is True
        assert result["trace"]["tool_calls"][0]["ok"] is True


def test_swarmsync_router_uses_auto_model_by_default(monkeypatch):
    rt = AgentRuntime(llm_url="https://api.swarmsync.ai/v1/chat/completions", llm_key="fake")

    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeSession:
        def __init__(self, timeout=None, connector=None, **kwargs):
            # Mirrors aiohttp.ClientSession's real signature: the LLM transport
            # passes a connector carrying the OS resolver policy.
            self.connector = connector

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    class FakeClientTimeout:
        def __init__(self, total):
            self.total = total

    import aiohttp

    monkeypatch.delenv("GENESIS_LLM_MODEL", raising=False)
    monkeypatch.delenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", raising=False)
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(aiohttp, "ClientTimeout", FakeClientTimeout)

    import asyncio

    # When the bundle's model_hint is "auto" (the default routing mode) and no
    # GENESIS_LLM_MODEL override is set, "auto" is passed through so SwarmSync's
    # complexity scorer chooses the tier.
    asyncio.run(rt._call_llm("auto", [{"role": "user", "content": "x"}], [], 100))

    assert captured["url"] == "https://api.swarmsync.ai/v1/chat/completions"
    assert captured["json"]["model"] == "auto"


def test_swarmsync_router_passes_auto_model_through(monkeypatch):
    rt = AgentRuntime(llm_url="https://api.swarmsync.ai/v1/chat/completions", llm_key="fake")

    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeSession:
        def __init__(self, timeout=None, connector=None, **kwargs):
            # Mirrors aiohttp.ClientSession's real signature: the LLM transport
            # passes a connector carrying the OS resolver policy.
            self.connector = connector

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers, json):
            captured["json"] = json
            return FakeResponse()

    class FakeClientTimeout:
        def __init__(self, total):
            self.total = total

    import aiohttp
    import asyncio

    monkeypatch.setenv("GENESIS_LLM_MODEL", "auto")
    monkeypatch.delenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", raising=False)
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(aiohttp, "ClientTimeout", FakeClientTimeout)

    # GENESIS_LLM_MODEL=auto means "let the bundle decide": a concrete bundle
    # model_hint is respected and passed through (commit 61d56e2), so
    # function-calling agents like genesis-meta get a capable model.
    asyncio.run(rt._call_llm("anthropic/claude-sonnet-4-5", [{"role": "user", "content": "x"}], [], 100))

    assert captured["json"]["model"] == "anthropic/claude-sonnet-4-5"


def test_swarmsync_router_passes_concrete_model_through(monkeypatch):
    rt = AgentRuntime(llm_url="https://api.swarmsync.ai/v1/chat/completions", llm_key="fake")

    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeSession:
        def __init__(self, timeout=None, connector=None, **kwargs):
            # Mirrors aiohttp.ClientSession's real signature: the LLM transport
            # passes a connector carrying the OS resolver policy.
            self.connector = connector

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers, json):
            captured["json"] = json
            return FakeResponse()

    class FakeClientTimeout:
        def __init__(self, total):
            self.total = total

    import aiohttp
    import asyncio

    monkeypatch.setenv("GENESIS_LLM_MODEL", "minimax/minimax-m2.5")
    monkeypatch.delenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", raising=False)
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(aiohttp, "ClientTimeout", FakeClientTimeout)

    asyncio.run(rt._call_llm("anthropic/claude-sonnet-4-5", [{"role": "user", "content": "x"}], [], 100))

    assert captured["json"]["model"] == "minimax/minimax-m2.5"


def test_openrouter_url_rejected_unless_explicit_fallback_enabled(monkeypatch):
    rt = AgentRuntime(llm_url="https://openrouter.ai/api/v1/chat/completions", llm_key="fake")

    import asyncio

    monkeypatch.delenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", raising=False)

    with pytest.raises(RuntimeError, match="OpenRouter is disabled"):
        asyncio.run(rt._call_llm("anthropic/claude-sonnet-4-5", [{"role": "user", "content": "x"}], [], 100))
