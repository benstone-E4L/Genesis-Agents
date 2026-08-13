"""LLM transport must resolve hostnames through the OS resolver.

aiohttp's default async resolver fails to resolve any hostname on some Windows
hosts ("Could not contact DNS servers"), which takes down every agent run with
errorCode=llm_call_failed even though the network is fine. Cato hit the same
defect and fixed it the same way (cato/tools/genesis.py, _ensure_session).

These tests fail if the LLM sessions are built with a bare ClientSession.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import aiohttp
import pytest

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_llm_session_helper_exists():
    import agent_runtime

    assert hasattr(agent_runtime, "_llm_client_session"), (
        "agent_runtime must expose a single _llm_client_session() factory so every "
        "LLM call shares one transport policy"
    )


def test_llm_session_uses_os_resolver():
    """The session must use ThreadedResolver (OS resolver), not aiohttp's default."""
    import agent_runtime

    async def build():
        session = agent_runtime._llm_client_session(aiohttp.ClientTimeout(total=5))
        try:
            resolver = session.connector._resolver
            assert isinstance(resolver, aiohttp.ThreadedResolver), (
                f"LLM ClientSession resolver is {type(resolver).__name__}; "
                "must be ThreadedResolver so hostname resolution uses the OS stack"
            )
        finally:
            await session.close()

    asyncio.run(build())


@pytest.mark.parametrize("func_name", ["_call_anthropic", "_call_llm"])
def test_llm_call_sites_do_not_build_bare_sessions(func_name):
    """Neither provider branch may construct aiohttp.ClientSession directly."""
    import agent_runtime

    runtime_cls = agent_runtime.AgentRuntime
    src = inspect.getsource(getattr(runtime_cls, func_name))
    assert "aiohttp.ClientSession(" not in src, (
        f"{func_name} builds a bare aiohttp.ClientSession, bypassing the shared "
        "resolver policy; use _llm_client_session() instead"
    )
