"""CI gate for the gateway auth boot guard.

docs/FINANCE-TOOL-CONTRACTS.md Section 7 "Related precondition": this
gateway previously logged only a warning and stayed open to anonymous
callers when GATEWAY_API_KEY was unset. This is a hard startup failure now,
called from main.lifespan() before the app accepts traffic, matching the
prohibition guard and escrow containment guards next to it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


class TestGatewayKeyBootGuard:
    def test_negative_control_missing_key_raises(self, monkeypatch):
        """NEGATIVE CONTROL. If this ever passes silently, the guard is decorative."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GATEWAY_API_KEY is not set"):
            main.assert_gateway_key_configured()

    def test_empty_string_key_also_raises(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "")
        with pytest.raises(RuntimeError, match="GATEWAY_API_KEY is not set"):
            main.assert_gateway_key_configured()

    def test_configured_key_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "some-real-key-value")
        main.assert_gateway_key_configured()  # must not raise

    def test_lifespan_calls_the_guard_before_accepting_traffic(self):
        """Source-level check, matching test_worker_autostart.py's own pattern
        for this same lifespan() function — a full lifespan invocation touches
        browser-cache/worker-thread startup that is out of scope here."""
        import inspect

        source = inspect.getsource(main.lifespan)
        guard_at = source.index("assert_gateway_key_configured()")
        # Must run after the prohibition guard, matching the documented
        # Layer-2-then-escrow-then-gateway-key startup ordering.
        prohibition_at = source.index("assert_prohibitions_intact()")
        assert prohibition_at < guard_at, (
            "assert_gateway_key_configured() must run after assert_prohibitions_intact() "
            "in lifespan(), matching the documented startup guard ordering"
        )


class TestVerifyGatewayKeyDependency:
    """verify_gateway_key() itself is unchanged behavior (per-request check) --
    these pin it stays correct now that the docstring changed, not the logic."""

    @pytest.mark.asyncio
    async def test_no_key_configured_accepts_any_caller(self, monkeypatch):
        """This path is unreachable in a running instance (lifespan refuses to
        boot first) -- pinned anyway since it is still real, callable code."""
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
        await main.verify_gateway_key(x_agent_api_key=None, x_agent_gateway_secret=None)

    @pytest.mark.asyncio
    async def test_correct_key_is_accepted(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_API_KEY", "correct-key")
        await main.verify_gateway_key(x_agent_api_key="correct-key", x_agent_gateway_secret=None)

    @pytest.mark.asyncio
    async def test_wrong_key_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setenv("GATEWAY_API_KEY", "correct-key")
        monkeypatch.delenv("AGENT_GATEWAY_SECRET", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            await main.verify_gateway_key(x_agent_api_key="wrong-key", x_agent_gateway_secret=None)
        assert exc_info.value.status_code == 401
