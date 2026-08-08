from pathlib import Path

from fastapi import HTTPException

import json

import pytest

from main import RunRequest, _llm_api_key, _llm_api_url, _raise_for_runtime_failure, run_agent

_REPO_ROOT = Path(__file__).resolve().parent
_MAIN_PY = _REPO_ROOT / "main.py"
_TOOLS_DIR = _REPO_ROOT / "tools"

# Direct LLM-provider endpoints. Any Genesis code path that hits these bypasses
# the SwarmSync router (no routing metadata, cost tracking, or tier selection).
_DIRECT_LLM_HOSTS = (
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
    "openrouter.ai/api",
    "api.x.ai",
    "api.groq.com",
)

# Known, intentional direct-provider exceptions (NOT on the SwarmSync router).
# Each entry is a file that is allowed to call a provider directly today.
# Adding a NEW bypass — or removing one of these without migrating it to the
# router — must fail this regression so the decision is explicit.
#   - vision_tool.py: GPT-4o vision API (no SwarmSync vision route yet). Tracked
#     for router migration; gated behind OPENAI_API_KEY and only reachable via
#     the vision agent's tool calls, never the persona/negotiate path.
_KNOWN_DIRECT_LLM_FILES = {"vision_tool.py"}


def test_runtime_failure_raises_non_200_status():
    try:
        _raise_for_runtime_failure(
            {
                "ok": False,
                "error": "llm_call_failed",
                "message": 'LLM HTTP 429: {"error":"TOO_MANY_REQUESTS"}',
            },
            "genesis-test",
        )
    except HTTPException as exc:
        assert exc.status_code == 429
        assert exc.detail["agentSlug"] == "genesis-test"
        assert exc.detail["error"] == "llm_call_failed"
    else:
        raise AssertionError("expected HTTPException")


def test_timeout_maps_to_gateway_timeout():
    try:
        _raise_for_runtime_failure({"ok": False, "error": "timeout"}, "genesis-test")
    except HTTPException as exc:
        assert exc.status_code == 504
    else:
        raise AssertionError("expected HTTPException")


def test_llm_router_defaults_to_swarmsync(monkeypatch):
    monkeypatch.delenv("LLM_API_URL", raising=False)

    assert _llm_api_url() == "https://api.swarmsync.ai/v1/chat/completions"


def test_openrouter_key_is_not_default_genesis_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    assert _llm_api_key() == ""


def test_openrouter_key_requires_explicit_fallback_flag(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("GENESIS_ALLOW_OPENROUTER_FALLBACK", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    assert _llm_api_key() == "sk-or-test"


def test_main_py_has_no_direct_google_generative_language_api():
    text = _MAIN_PY.read_text(encoding="utf-8")
    assert "generativelanguage.googleapis.com" not in text
    assert "call_gemini_fallback" not in text
    assert "GEMINI_API_KEY" not in text


def test_no_direct_llm_provider_calls_outside_known_allowlist():
    """Lock the LLM-call surface: persona/negotiate (main.py) and every tool
    must route through SwarmSync, except the explicitly allowlisted files."""
    scanned = [_MAIN_PY, _REPO_ROOT / "agent_runtime.py", *sorted(_TOOLS_DIR.glob("*.py"))]

    offenders: dict[str, list[str]] = {}
    for path in scanned:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        hits = [host for host in _DIRECT_LLM_HOSTS if host in text]
        if hits and path.name not in _KNOWN_DIRECT_LLM_FILES:
            offenders[path.name] = hits

    assert not offenders, (
        f"New direct LLM-provider bypass(es) detected (must route via SwarmSync "
        f"or be added to _KNOWN_DIRECT_LLM_FILES with justification): {offenders}"
    )


def test_known_direct_llm_exceptions_still_exist():
    """Guard the allowlist against silent drift: if a known direct-provider file
    is removed or migrated to the router, update _KNOWN_DIRECT_LLM_FILES so the
    'no bypass' claim stays accurate."""
    for name in _KNOWN_DIRECT_LLM_FILES:
        path = _TOOLS_DIR / name
        assert path.exists(), f"allowlisted bypass file missing: {name} — update _KNOWN_DIRECT_LLM_FILES"
        text = path.read_text(encoding="utf-8")
        assert any(host in text for host in _DIRECT_LLM_HOSTS), (
            f"{name} no longer calls a direct provider — remove it from "
            f"_KNOWN_DIRECT_LLM_FILES so the bypass allowlist stays honest"
        )


@pytest.mark.asyncio
async def test_call_llm_router_targets_swarmsync_only(monkeypatch):
    import main

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}}],
                "swarmsync": {"routed_model": "anthropic/claude-haiku-4-5", "tier": "economy"},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("LLM_API_KEY", "sk-ss-test-key")
    monkeypatch.setenv("AGENT_GATEWAY_SECRET", "gateway-secret-test")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)

    result = await main.call_llm_router("system", "user")

    assert "generativelanguage.googleapis.com" not in captured["url"]
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-ss-test-key"
    assert captured["headers"]["X-Title"] == "SwarmSync Agent Gateway"
    assert captured["headers"]["X-Agent-Gateway-Secret"] == "gateway-secret-test"
    assert result["swarmsync"]["routed_model"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_slug", "expected_agent_name", "expected_bundle_slug"),
    [
        ("genesis_builder_x402", "Genesis Builder Agent", "genesis-builder"),
        ("genesis_deploy_x402", "Genesis Deploy Agent", "genesis-deploy"),
        ("genesis_qa_x402", "Genesis QA Agent", "genesis-qa"),
        ("genesis_research_x402", "Genesis Research Agent", "genesis-research"),
    ],
)
async def test_conduit_heavy_run_requests_enqueue_async_jobs(
    monkeypatch, request_slug, expected_agent_name, expected_bundle_slug
):
    import main

    def fake_create_job(**kwargs):
        return {
            "id": "cqueued123",
            "status": "QUEUED",
            "created_at": "2026-05-26T00:00:00+00:00",
            "idempotent_hit": False,
        }

    def fail_get_runtime():
        raise AssertionError("async bundles must not execute AgentRuntime during /run")

    monkeypatch.setattr(main, "_JOB_STORE_OK", True)
    monkeypatch.setattr(main, "create_job", fake_create_job)
    monkeypatch.setattr(main, "_get_runtime", fail_get_runtime)

    result = await run_agent(
        request_slug,
        RunRequest(prompt="Perform a real browser-heavy task", task={"scope": "smoke"}),
    )

    payload = json.loads(result.response)
    assert result.agentName == expected_agent_name
    assert payload["slug"] == expected_bundle_slug
    assert payload["job_id"] == "cqueued123"
    assert payload["status"] == "QUEUED"
    assert payload["poll_url"] == "/agents/jobs/cqueued123"


@pytest.mark.asyncio
@pytest.mark.parametrize("request_slug", ["onboarding_agent", "genesis_hr_x402"])
async def test_hr_aliases_use_same_canonical_bundle_in_live_test(monkeypatch, request_slug):
    import main

    async def fake_call_llm_router(system_prompt, user_prompt):
        assert "Human Resources operations specialist" in system_prompt
        return {"text": "HR response"}

    def fail_load_agent(slug):
        raise AssertionError("bundle-backed live_test calls must not load legacy Python agents")

    monkeypatch.setattr(main, "call_llm_router", fake_call_llm_router)
    monkeypatch.setattr(main, "load_agent", fail_load_agent)

    result = await run_agent(
        request_slug,
        RunRequest(prompt="Create an onboarding checklist", mode="live_test"),
    )

    payload = json.loads(result.response)
    assert result.agentName == "Genesis HR Agent"
    assert payload["slug"] == "genesis-hr"
