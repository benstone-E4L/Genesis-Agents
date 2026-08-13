"""Wire-format translation for the Anthropic Messages API.

Genesis's agent loop keeps its conversation history in the OpenAI shape: assistant turns carry
``tool_calls``, and every tool result is its own ``{"role": "tool"}`` message. The Anthropic
Messages API has no ``tool`` role — it takes ``tool_use`` content blocks on the assistant turn
and ``tool_result`` blocks inside the *next user* turn. Posting the OpenAI shape verbatim
returns ``400 invalid_request_error: messages: Unexpected role "tool"``, which kills every
tool-using conversation on its second turn.

DUPLICATION, DELIBERATE AND FLAGGED: Cato solves the identical problem in
``cato/model_policy.py::to_anthropic_messages`` (applied at its wire boundary in
``build_request_payload``). This module mirrors that approach rather than importing it — the two
repos deploy independently and share no package — so the same bug now has two implementations
that must be kept in step. If a third consumer appears, extract a shared package instead of
copying this a third time.

Design rule, same as Cato's: selection and history stay in ONE canonical shape (OpenAI), and
only the outbound provider payload is normalised, at the last point before the wire, so no
caller can bypass it.
"""
from __future__ import annotations

import json
from typing import Any

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_HOST_MARKERS = ("api.anthropic.com",)

# Gateway-style ids (what the 24 bundles carry in `model_hint`) -> real Anthropic API ids.
# Verified against GET https://api.anthropic.com/v1/models with the live key: Anthropic rejects
# the `anthropic/` vendor prefix and the undated `claude-sonnet-4-5` alias outright, so this
# translation is required, not cosmetic.
MODEL_ID_TRANSLATIONS: dict[str, str] = {
    "anthropic/claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "anthropic/claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "anthropic/claude-opus-4-5": "claude-opus-4-5-20251101",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "anthropic/claude-opus-5": "claude-opus-5",
    "anthropic/claude-fable-5": "claude-fable-5",
    # Undated aliases the gateway accepted but the API does not.
    "claude-sonnet-4-5": "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-opus-4-5": "claude-opus-4-5-20251101",
}

# Ids confirmed present on GET /v1/models for this account. Used to decide whether an untranslated
# id is safe to pass through.
KNOWN_ANTHROPIC_MODEL_IDS: frozenset[str] = frozenset({
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
})

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# Provider-specific fallback chain. The OpenAI-path fallbacks ("openrouter/free",
# "minimax/minimax-m2.5:free") are gateway ids that mean nothing to Anthropic and could only ever
# produce a second 404 — a fallback that cannot work is worse than none, because it hides the
# real error behind the last one. These are real ids from the list above.
ANTHROPIC_FALLBACK_MODELS: tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251001",
)


def is_anthropic_url(url: str) -> bool:
    return any(marker in (url or "") for marker in ANTHROPIC_HOST_MARKERS)


def translate_model_id(model: str | None) -> str:
    """Map a gateway-style model id onto a real Anthropic API id.

    Unknown ids are passed through unchanged rather than silently rewritten: a wrong-but-explicit
    404 from Anthropic naming the id is far easier to diagnose than a silent substitution that
    bills a model nobody chose.
    """
    raw = (model or "").strip()
    if not raw or raw == "auto":
        return DEFAULT_ANTHROPIC_MODEL
    if raw in MODEL_ID_TRANSLATIONS:
        return MODEL_ID_TRANSLATIONS[raw]
    if raw in KNOWN_ANTHROPIC_MODEL_IDS:
        return raw
    if raw.startswith("anthropic/"):
        stripped = raw.split("/", 1)[1]
        return MODEL_ID_TRANSLATIONS.get(stripped, stripped)
    return raw


def split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic takes `system` as a TOP-LEVEL parameter, not a message role."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue
        rest.append(msg)
    return "\n\n".join(system_parts), rest


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate OpenAI-shaped turns to the Anthropic Messages shape.

    Properties this must keep (same three as Cato's):
      * Idempotent — already-Anthropic messages (list content, no ``tool_calls``) pass through.
      * Grouped — every ``tool_result`` answering one assistant turn merges into a SINGLE user
        message. Anthropic rejects a ``tool_use`` block not answered in the immediately following
        user turn, so a multi-tool turn must not become several user messages.
      * Order preserving — ``tool_result`` blocks stay in call order.
    """
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def _flush() -> None:
        if pending:
            out.append({"role": "user", "content": list(pending)})
            pending.clear()

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "tool":
            content = msg.get("content")
            pending.append({
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or "unknown"),
                "content": content if isinstance(content, str) else json.dumps(content, default=str),
            })
            continue

        _flush()

        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            elif isinstance(text, list):
                blocks.extend(b for b in text if isinstance(b, dict))
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args or "{}")
                    except (json.JSONDecodeError, ValueError):
                        parsed = {}
                else:
                    parsed = raw_args
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or "unknown"),
                    "name": str(fn.get("name") or ""),
                    "input": parsed if isinstance(parsed, dict) else {},
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        # An assistant turn with no content and no tool_calls is not a legal Anthropic message.
        if role == "assistant" and not msg.get("content"):
            continue
        out.append(msg)

    _flush()
    return out


def to_anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """`{type:"function", function:{name, description, parameters}}` -> `{name, description,
    input_schema}`. Already-Anthropic tool dicts pass through."""
    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if "input_schema" in tool and "name" in tool:
            converted.append(tool)
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "")
        if not name:
            continue
        converted.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted


def build_anthropic_request(
    *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """Assemble the /v1/messages body. `max_tokens` is REQUIRED by this API."""
    system, conversation = split_system(messages)
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max(1, int(max_tokens)),
        "messages": to_anthropic_messages(conversation),
    }
    if system:
        body["system"] = system
    anthropic_tools = to_anthropic_tools(tools)
    if anthropic_tools:
        body["tools"] = anthropic_tools
        body["tool_choice"] = {"type": "auto"}
    return body


def from_anthropic_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise an Anthropic response back into the OpenAI shape the agent loop parses.

    The loop reads `choices[0].message.tool_calls` and `usage.total_tokens`; translating on the
    way back means the loop, the trace, the budget accounting and every existing test keep
    working against one canonical shape.
    """
    blocks = payload.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, default=str),
                },
            })

    usage = payload.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(p for p in text_parts if p) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": payload.get("id"),
        "model": payload.get("model"),
        "choices": [{
            "index": 0,
            "message": message,
            # Anthropic's "tool_use" stop_reason is the OpenAI "tool_calls" finish_reason.
            "finish_reason": "tool_calls" if payload.get("stop_reason") == "tool_use" else "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
