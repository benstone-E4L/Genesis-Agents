"""Smoke test for the agent runtime. Mocks LLM calls; uses real bundle_loader."""
from __future__ import annotations

import pytest

from agent_runtime import AgentRuntime
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
    assert resolve_bundle_slug("onboarding_agent") == "genesis-hr"
    assert resolve_bundle_slug("genesis_hr_x402") == "genesis-hr"
    assert load_bundle("onboarding_agent")["slug"] == load_bundle("genesis_hr_x402")["slug"] == "genesis-hr"
    assert resolve_bundle_slug("genesis_meta_agent") == "genesis-meta"
    assert load_bundle("genesis_qa_x402") is not None


@pytest.mark.asyncio
async def test_runtime_unknown_slug():
    rt = AgentRuntime(llm_url="http://fake", llm_key="fake")
    result = await rt.execute_agent("does-not-exist", "test", {})
    assert result["ok"] is False
    assert result["error"] == "unknown_slug"


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
        def __init__(self, timeout):
            pass

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
        def __init__(self, timeout):
            pass

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
        def __init__(self, timeout):
            pass

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
