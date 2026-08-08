"""test_worker_autostart.py — Phase 7 automatic worker tests.

Verifies that:
- _genesis_auto_worker_loop() exists in main.py and calls run_tick directly
- The worker does NOT use the internal tick HTTP endpoint
- GENESIS_WORKER_ENABLED=true starts the loop in lifespan
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestAutoWorkerModule:
    def test_auto_worker_loop_exists_in_main(self):
        """_genesis_auto_worker_loop must be defined in main.py."""
        # Import main carefully to avoid side effects
        import importlib
        import main as m
        assert hasattr(m, "_genesis_auto_worker_loop"), (
            "_genesis_auto_worker_loop not found in main.py. "
            "Phase 7 requires this coroutine for in-process worker."
        )

    def test_auto_worker_loop_is_coroutine(self):
        import main as m
        fn = getattr(m, "_genesis_auto_worker_loop", None)
        assert fn is not None
        assert asyncio.iscoroutinefunction(fn), (
            "_genesis_auto_worker_loop must be an async def coroutine"
        )

    def test_auto_worker_does_not_call_tick_endpoint(self):
        """The auto-worker loop must call run_tick() directly, not POST to /internal/genesis-worker/tick."""
        import main as m
        import inspect
        source = inspect.getsource(m._genesis_auto_worker_loop)
        # Must not contain HTTP call to the tick endpoint
        forbidden_patterns = [
            "/internal/genesis-worker/tick",
            "httpx",
            "requests.post",
            "aiohttp",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, (
                f"_genesis_auto_worker_loop must NOT use HTTP to call {pattern!r}. "
                "It must call run_tick() directly (no HTTP hop)."
            )
        # Must call run_tick
        assert "run_tick" in source, (
            "_genesis_auto_worker_loop must call worker.run_tick() directly"
        )

    def test_genesis_worker_enabled_env_triggers_task(self):
        """When GENESIS_WORKER_ENABLED=true, lifespan must start an auto-worker task."""
        import main as m
        # Check that lifespan references GENESIS_WORKER_ENABLED
        source = inspect.getsource(m.lifespan)
        assert "GENESIS_WORKER_ENABLED" in source, (
            "lifespan() must check GENESIS_WORKER_ENABLED env var"
        )
        assert "_genesis_auto_worker_loop" in source, (
            "lifespan() must start _genesis_auto_worker_loop when enabled"
        )


class TestAutoWorkerBehavior:
    def test_run_tick_is_called_by_loop(self):
        """The auto-worker loop calls run_tick() on each iteration."""
        tick_calls = []

        async def mock_run_tick(**kwargs):
            tick_calls.append(kwargs)
            return {"claimed": 0, "processed": 0, "job_ids": [], "expired": 0}

        async def run_one_iteration():
            import main as m
            # Patch run_tick in worker module and sleep to exit after one tick
            with patch("worker.run_tick", side_effect=mock_run_tick), \
                 patch("worker._worker_state", {"enabled": False, "last_tick_at": None}):
                # Patch sleep to cancel after first iteration
                sleep_count = [0]
                original_sleep = asyncio.sleep

                async def _limited_sleep(n):
                    sleep_count[0] += 1
                    if sleep_count[0] >= 1:
                        raise asyncio.CancelledError()
                    await original_sleep(0)

                with patch("asyncio.sleep", side_effect=_limited_sleep):
                    with patch.dict(os.environ, {
                        "GENESIS_WORKER_INTERVAL_SECONDS": "0.01",
                        "GENESIS_WORKER_TICK_LIMIT": "1",
                    }):
                        try:
                            await m._genesis_auto_worker_loop()
                        except asyncio.CancelledError:
                            pass

        asyncio.new_event_loop().run_until_complete(run_one_iteration())
        assert len(tick_calls) >= 1, (
            "run_tick must be called at least once by _genesis_auto_worker_loop. "
            f"Got {len(tick_calls)} calls."
        )

    def test_interval_env_var_respected(self):
        """GENESIS_WORKER_INTERVAL_SECONDS configures the sleep interval."""
        import main as m
        source = inspect.getsource(m._genesis_auto_worker_loop)
        assert "GENESIS_WORKER_INTERVAL_SECONDS" in source, (
            "_genesis_auto_worker_loop must read GENESIS_WORKER_INTERVAL_SECONDS env var"
        )

    def test_tick_limit_env_var_respected(self):
        """GENESIS_WORKER_TICK_LIMIT configures how many jobs per tick."""
        import main as m
        source = inspect.getsource(m._genesis_auto_worker_loop)
        assert "GENESIS_WORKER_TICK_LIMIT" in source, (
            "_genesis_auto_worker_loop must read GENESIS_WORKER_TICK_LIMIT env var"
        )
