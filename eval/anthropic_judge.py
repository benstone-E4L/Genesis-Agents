"""Anthropic-backed :class:`~eval.rubrics.Judge` callable for the Genesis harness.

Implements the narrow ``Judge`` protocol from ``eval.rubrics``: a callable of
``(prompt: str, *, rubric: Rubric) -> str`` returning a reply that
``rubrics.parse_judge_reply`` can parse into ``{"score": int, "reasoning": str}``.

This is the real judge the harness docs describe as a hook nothing here ships.
Model and token budget are fixed module constants, not caller-configurable —
the caller supplies only ``prompt`` and ``rubric``, so nothing invoking this
judge can smuggle a model override the way the Cato-side model router was
explicitly hardened against exactly that shape of argument (a caller picking
its own model/tier).

``ANTHROPIC_API_KEY`` is read once from the environment and never logged,
printed, or embedded in a returned value.
"""

from __future__ import annotations

import os

import anthropic

from .rubrics import Rubric

__all__ = ["judge"]

_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 1024

_SYSTEM_PROMPT = (
    "You are a strict, calibrated evaluation judge scoring one AI agent's "
    "response against one rubric dimension. Read the rubric scale, the "
    "concrete failing example, and the response carefully. Anchor your score "
    "to the scale's written meanings, not to how confident or articulate the "
    "response sounds — confident and wrong scores at the bottom of the scale.\n\n"
    "Reply with ONLY a single JSON object of the exact shape:\n"
    '{"score": <integer from the scale>, "reasoning": "<one or two sentences>"}\n'
    "No text outside the JSON object. No markdown fence unless it is a ```json fence."
)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. This judge will not silently "
                "fall back to an unauthenticated or mocked reply."
            )
        _client = anthropic.Anthropic(api_key=key)
    return _client


def judge(prompt: str, *, rubric: Rubric) -> str:
    """The ``Judge`` protocol callable — one Anthropic call, returns raw text.

    Thinking is explicitly disabled. Sonnet 5 runs adaptive thinking by
    default when ``thinking`` is omitted, and ``max_tokens`` caps thinking +
    response text combined — on a harder rubric prompt, adaptive thinking
    consumed the entire budget before any JSON was written, producing an
    empty reply. A rubric-scoring judgment doesn't need deep reasoning, and
    Sonnet 5 (unlike Opus 5) accepts ``disabled`` at any effort level.
    """
    client = _get_client()
    response = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
