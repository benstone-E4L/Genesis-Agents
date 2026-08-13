"""Anthropic Messages API wire translation.

The bug this exists to prevent is specific and was already hit once in Cato (K-01): the agent
loop keeps OpenAI-shaped history, and posting a `{"role": "tool"}` message to Anthropic returns
`400 messages: Unexpected role "tool"` — which kills every tool-using conversation on turn 2,
i.e. exactly when an agent first tries to do real work.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.anthropic_wire import (  # noqa: E402
    ANTHROPIC_FALLBACK_MODELS,
    KNOWN_ANTHROPIC_MODEL_IDS,
    MODEL_ID_TRANSLATIONS,
    build_anthropic_request,
    from_anthropic_response,
    split_system,
    to_anthropic_messages,
    to_anthropic_tools,
    translate_model_id,
)


# ---------------------------------------------------------------------------
# Model id translation + drift
# ---------------------------------------------------------------------------

def test_bundle_model_hints_all_translate_to_a_real_anthropic_id():
    """The drift test. All 24 bundles carry `anthropic/claude-sonnet-4-5`, a gateway-style id
    Anthropic rejects. If a bundle ever gains a hint with no translation, this fails rather than
    404-ing at runtime inside a paid agent run."""
    from bundle_loader import list_bundles, load_bundle

    slugs = list_bundles()
    assert slugs, "precondition: bundles exist"

    unmapped = []
    for slug in slugs:
        hint = (load_bundle(slug) or {}).get("model_hint")
        if not hint or hint == "auto":
            continue
        if translate_model_id(hint) not in KNOWN_ANTHROPIC_MODEL_IDS:
            unmapped.append((slug, hint, translate_model_id(hint)))

    assert not unmapped, f"bundle model_hints with no real Anthropic id: {unmapped}"


def test_every_translation_target_is_a_known_real_model_id():
    """Translations and the verified id registry cannot silently diverge."""
    bad = {k: v for k, v in MODEL_ID_TRANSLATIONS.items() if v not in KNOWN_ANTHROPIC_MODEL_IDS}
    assert not bad, f"translations pointing at ids not in the verified registry: {bad}"


def test_fallback_chain_contains_only_real_anthropic_ids():
    """The OpenAI path falls back to 'openrouter/free' and 'minimax/...', which mean nothing to
    Anthropic — a fallback that cannot work hides the real error behind the last one."""
    assert ANTHROPIC_FALLBACK_MODELS
    for model in ANTHROPIC_FALLBACK_MODELS:
        assert model in KNOWN_ANTHROPIC_MODEL_IDS, model


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("anthropic/claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("anthropic/claude-opus-5", "claude-opus-5"),
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("auto", "claude-sonnet-4-5-20250929"),
        ("", "claude-sonnet-4-5-20250929"),
    ],
)
def test_translate_model_id(raw, expected):
    assert translate_model_id(raw) == expected


def test_unknown_model_id_passes_through_rather_than_being_silently_rewritten():
    """A wrong-but-explicit 404 naming the id beats a silent substitution that bills a model
    nobody chose."""
    assert translate_model_id("some-future-model") == "some-future-model"


# ---------------------------------------------------------------------------
# The role:"tool" bug
# ---------------------------------------------------------------------------

def test_tool_role_becomes_a_user_tool_result_block():
    """THE regression. `role:"tool"` must never reach the wire."""
    out = to_anthropic_messages([
        {"role": "user", "content": "list the repo"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "file_write", "arguments": '{"path":"a.txt"}'}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
    ])

    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert all(m["role"] != "tool" for m in out)
    assistant_blocks = out[1]["content"]
    assert assistant_blocks[0]["type"] == "tool_use"
    assert assistant_blocks[0]["id"] == "call_1"
    assert assistant_blocks[0]["input"] == {"path": "a.txt"}
    result_block = out[2]["content"][0]
    assert result_block == {
        "type": "tool_result", "tool_use_id": "call_1", "content": '{"ok": true}'
    }


def test_multiple_tool_results_are_grouped_into_one_user_turn():
    """Anthropic rejects a tool_use block not answered in the IMMEDIATELY following user turn,
    so a parallel multi-tool turn must not become several user messages."""
    out = to_anthropic_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ])

    assert len(out) == 2
    assert [b["tool_use_id"] for b in out[1]["content"]] == ["c1", "c2"]  # order preserved


def test_translation_is_idempotent():
    already = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    assert to_anthropic_messages(already) == already


def test_non_dict_tool_arguments_do_not_crash_the_translation():
    out = to_anthropic_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "a", "arguments": "not json at all"}}
        ]},
    ])
    assert out[0]["content"][0]["input"] == {}


def test_empty_assistant_turn_is_dropped():
    """An assistant message with neither content nor tool_calls is not a legal Anthropic turn."""
    out = to_anthropic_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None},
    ])
    assert [m["role"] for m in out] == ["user"]


# ---------------------------------------------------------------------------
# system, tools, request assembly
# ---------------------------------------------------------------------------

def test_system_is_lifted_to_a_top_level_parameter():
    system, rest = split_system([
        {"role": "system", "content": "You are Genesis Research."},
        {"role": "user", "content": "go"},
    ])
    assert system == "You are Genesis Research."
    assert [m["role"] for m in rest] == ["user"]


def test_tools_are_converted_to_input_schema_shape():
    converted = to_anthropic_tools([
        {"type": "function", "function": {
            "name": "web_search", "description": "search",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }}
    ])
    assert converted == [{
        "name": "web_search", "description": "search",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]


def test_request_body_has_required_max_tokens_and_no_system_role():
    body = build_anthropic_request(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
        max_tokens=512,
    )

    assert body["max_tokens"] == 512  # REQUIRED by this API
    assert body["system"] == "sys"
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["tool_choice"] == {"type": "auto"}
    assert "input_schema" in body["tools"][0]


def test_request_omits_tools_entirely_when_there_are_none():
    body = build_anthropic_request(
        model="m", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=10
    )
    assert "tools" not in body and "tool_choice" not in body


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------

def test_response_with_tool_use_normalises_to_openai_tool_calls():
    """The agent loop reads choices[0].message.tool_calls — translating back means the loop,
    trace, budget accounting and every existing test keep working on one canonical shape."""
    normalised = from_anthropic_response({
        "id": "msg_1", "model": "claude-sonnet-4-5-20250929", "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "I'll search."},
            {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"q": "e4l"}},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 25},
    })

    msg = normalised["choices"][0]["message"]
    assert msg["content"] == "I'll search."
    assert msg["tool_calls"][0]["id"] == "tu_1"
    assert msg["tool_calls"][0]["function"]["name"] == "web_search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"q": "e4l"}
    assert normalised["choices"][0]["finish_reason"] == "tool_calls"
    assert normalised["usage"]["total_tokens"] == 125  # budget accounting depends on this


def test_plain_text_response_has_no_tool_calls():
    normalised = from_anthropic_response({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "done"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    })
    msg = normalised["choices"][0]["message"]
    assert msg["content"] == "done"
    assert "tool_calls" not in msg
    assert normalised["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "env,url,expected",
    [
        ("", "https://api.anthropic.com/v1/messages", True),
        ("", "https://api.swarmsync.ai/v1/chat/completions", False),
        ("anthropic", "https://api.swarmsync.ai/v1/chat/completions", True),
        ("openai", "https://api.anthropic.com/v1/messages", False),
    ],
)
def test_provider_selection(monkeypatch, env, url, expected):
    """Explicit env signal wins; URL detection is the fallback."""
    from agent_runtime import AgentRuntime

    monkeypatch.setenv("GENESIS_LLM_PROVIDER", env)
    assert AgentRuntime(url, "k")._is_anthropic() is expected


def test_openai_path_is_untouched_by_the_anthropic_branch(monkeypatch):
    """Provider-aware, not a rip-out: the gateway path must still build an OpenAI body."""
    from agent_runtime import AgentRuntime

    monkeypatch.delenv("GENESIS_LLM_PROVIDER", raising=False)
    rt = AgentRuntime("https://api.swarmsync.ai/v1/chat/completions", "k")
    assert rt._is_anthropic() is False


# ---------------------------------------------------------------------------
# Config documentation + the tool-evidence gate the live proof relied on
# ---------------------------------------------------------------------------

def test_env_example_documents_the_provider_switch():
    """A provider switch nobody can find is a provider switch nobody sets."""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "GENESIS_LLM_PROVIDER" in text
    assert "api.anthropic.com" in text
    assert "anthropic-version" in text


def test_tool_evidence_gate_is_not_vacuous():
    """The live proof claims `min_successful_tool_calls` was satisfied by real tool evidence.
    That claim is only worth anything if a prose-only answer FAILS."""
    from agent_runtime import _check_success_criteria
    from bundle_loader import load_bundle

    advertises_tools = bool((load_bundle("genesis-builder") or {}).get("tools_advertised"))
    assert advertises_tools, "precondition: genesis-builder advertises tools"

    real = {
        "response": "Output: Fizz 4",
        "trace": {"tool_calls": [
            {"tool_name": "file_write", "ok": True},
            {"tool_name": "run_code", "ok": True},
        ]},
        "elapsed_s": 12,
    }
    prose_only = {
        "response": "I have created the file and it works.",
        "trace": {"tool_calls": []},
        "elapsed_s": 1,
    }

    assert _check_success_criteria(None, real, require_tool_evidence=True)["ok"] is True
    verdict = _check_success_criteria(None, prose_only, require_tool_evidence=True)
    assert verdict["ok"] is False
    assert verdict["failed"][0]["type"] == "min_successful_tool_calls"
