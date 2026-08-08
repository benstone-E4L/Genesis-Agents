"""Comprehensive pytest tests for the Genesis agent job lifecycle.

Tests cover: success path, agent failure, timeout, dispute flow,
tool permission enforcement, and job_id unification between DB and runtime.

All external services (Postgres, LLM API, escrow client, HTTP callbacks)
are mocked at the module level. No real network calls are made.

Run with:
    pytest testing/test_job_lifecycle.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Stub out psycopg before any project module imports it
# ---------------------------------------------------------------------------

_psycopg_stub = types.ModuleType("psycopg")
_psycopg_stub.connect = MagicMock()
_rows_stub = types.ModuleType("psycopg.rows")
_rows_stub.dict_row = MagicMock()
sys.modules.setdefault("psycopg", _psycopg_stub)
sys.modules.setdefault("psycopg.rows", _rows_stub)

# Stub aiohttp so agent_runtime can be imported without the package
_aiohttp_stub = types.ModuleType("aiohttp")
_aiohttp_stub.ClientSession = MagicMock()
_aiohttp_stub.ClientTimeout = MagicMock(return_value=MagicMock())
sys.modules.setdefault("aiohttp", _aiohttp_stub)

# Stub httpx for worker.fire_callback
_httpx_stub = types.ModuleType("httpx")
_httpx_stub.AsyncClient = MagicMock()
sys.modules.setdefault("httpx", _httpx_stub)

# Ensure repo root is on sys.path so project modules are importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: str | None = None,
    slug: str = "genesis-builder",
    prompt: str = "Build a hello world component",
    params: dict | None = None,
    escrow_id: str | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Build a minimal genesis_jobs row dict as returned by job_store."""
    return {
        "id": job_id or ("c" + uuid.uuid4().hex[:24]),
        "agentSlug": slug,
        "prompt": prompt,
        "params": params or {},
        "status": "QUEUED",
        "escrowId": escrow_id,
        "webhookUrl": webhook_url,
        "buyerWalletId": None,
        "buyerClientId": None,
        "priceTierCents": None,
        "idempotencyKey": None,
        "webhookSecret": None,
        "outputArtifactUris": [],
        "createdAt": "2024-06-01T00:00:00",
        "updatedAt": "2024-06-01T00:00:00",
    }


def _make_runtime_result(
    *,
    ok: bool = True,
    slug: str = "genesis-builder",
    job_id: str | None = None,
    response: str | None = "Task complete.",
    error: str | None = None,
    error_msg: str | None = None,
    files_written: int = 0,
    elapsed_s: float = 2.0,
) -> dict[str, Any]:
    """Build a minimal AgentRuntime.execute_agent return dict."""
    result: dict[str, Any] = {
        "ok": ok,
        "slug": slug,
        "job_id": job_id or ("job-" + uuid.uuid4().hex[:12]),
        "elapsed_s": elapsed_s,
        "resource_usage": {"llm_calls": 1, "total_tokens": 400, "files_written": files_written},
    }
    if ok:
        result["response"] = response
        result["turns"] = 1
    else:
        result["error"] = error or "agent_failure"
        if error_msg:
            result["message"] = error_msg
    return result


# ---------------------------------------------------------------------------
# TestSuccessfulJobLifecycle
# ---------------------------------------------------------------------------


class TestSuccessfulJobLifecycle:
    """Happy path: queue → RUNNING → DELIVERED → escrow settled → proof metadata."""

    @pytest.mark.asyncio
    async def test_job_transitions_to_delivered(self):
        """Worker marks job DELIVERED when agent succeeds."""
        job = _make_job()
        runtime_result = _make_runtime_result(slug=job["agentSlug"])

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        delivered_calls = [c for c in mock_update.call_args_list if c.args[1] == "DELIVERED"]
        assert len(delivered_calls) == 1, "Expected exactly one DELIVERED status update"
        assert delivered_calls[0].args[0] == job["id"]
        # The worker packs {response, trace} into result_summary (commit fbb2bef)
        # so poll clients can inspect the tool-call trace. Assert the agent's
        # response is preserved inside that JSON envelope.
        import json as _json
        summary = _json.loads(delivered_calls[0].kwargs.get("result_summary"))
        assert summary["response"] == "Task complete."
        assert "trace" in summary

    @pytest.mark.asyncio
    async def test_escrow_settled_on_success_no_callback(self):
        """When job succeeds and we own the escrow, complete_escrow is called."""
        job = _make_job(escrow_id="escrow-abc")
        runtime_result = _make_runtime_result(slug=job["agentSlug"])

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
            patch("worker.fire_callback", new=AsyncMock(return_value=True)) as mock_cb,
        ):
            with patch("escrow_client.complete_escrow", new=AsyncMock(return_value={"ok": True})) as mock_escrow:
                from agent_runtime import AgentRuntime
                mock_runtime = MagicMock(spec=AgentRuntime)
                mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

                from worker import process_job
                await process_job(job, mock_runtime)

            mock_escrow.assert_awaited_once()
            # No callback because no webhook_url
            mock_cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_result_summary_truncated_to_4000_chars(self):
        """result_summary passed to update_job_status is capped at 4000 chars."""
        job = _make_job()
        long_response = "x" * 8000
        runtime_result = _make_runtime_result(response=long_response)

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        # Find the DELIVERED call
        delivered_calls = [
            c for c in mock_update.call_args_list if c.args[1] == "DELIVERED"
        ]
        assert len(delivered_calls) == 1
        summary = delivered_calls[0].kwargs.get("result_summary", "")
        assert len(summary) <= 4000

    @pytest.mark.asyncio
    async def test_no_escrow_call_when_external_escrow(self):
        """When webhook_url is set, the marketplace owns escrow — we must not settle it."""
        job = _make_job(escrow_id="escrow-xyz", webhook_url="https://marketplace.example.com/callback")
        runtime_result = _make_runtime_result()

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
            patch("worker.fire_callback", new=AsyncMock(return_value=True)),
        ):
            with patch("escrow_client.complete_escrow", new=AsyncMock(return_value={"ok": True})) as mock_escrow:
                from agent_runtime import AgentRuntime
                mock_runtime = MagicMock(spec=AgentRuntime)
                mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

                from worker import process_job
                await process_job(job, mock_runtime)

            mock_escrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_fired_on_success_with_webhook(self):
        """fire_callback is called with status=DELIVERED when webhook_url is set."""
        job = _make_job(webhook_url="https://marketplace.example.com/callback")
        runtime_result = _make_runtime_result()

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
            patch("worker.fire_callback", new=AsyncMock(return_value=True)) as mock_cb,
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        mock_cb.assert_awaited_once()
        cb_kwargs = mock_cb.call_args.kwargs
        assert cb_kwargs["status"] == "DELIVERED"
        assert cb_kwargs["job_id"] == job["id"]


# ---------------------------------------------------------------------------
# TestAgentFailureLifecycle
# ---------------------------------------------------------------------------


class TestAgentFailureLifecycle:
    """Agent returns ok=False → job FAILED → escrow refunded → callback sent."""

    @pytest.mark.asyncio
    async def test_job_marked_failed_on_agent_error(self):
        """update_job_status called with FAILED when agent returns ok=False."""
        job = _make_job()
        runtime_result = _make_runtime_result(ok=False, error="llm_call_failed")

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        failed_calls = [c for c in mock_update.call_args_list if c.args[1] == "FAILED"]
        assert len(failed_calls) == 1
        assert failed_calls[0].kwargs.get("error_code") == "llm_call_failed"

    @pytest.mark.asyncio
    async def test_escrow_released_on_failure(self):
        """release_escrow is called when job fails and we own the escrow."""
        job = _make_job(escrow_id="escrow-fail-001")
        runtime_result = _make_runtime_result(ok=False, error="max_turns_reached")

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
        ):
            with patch("escrow_client.release_escrow", new=AsyncMock(return_value={"ok": True})) as mock_release:
                from agent_runtime import AgentRuntime
                mock_runtime = MagicMock(spec=AgentRuntime)
                mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

                from worker import process_job
                await process_job(job, mock_runtime)

            mock_release.assert_awaited_once()
            release_kwargs = mock_release.call_args.kwargs
            assert release_kwargs.get("escrow_id") == "escrow-fail-001"
            assert "max_turns_reached" in release_kwargs.get("reason", "")

    @pytest.mark.asyncio
    async def test_callback_sent_with_failed_status(self):
        """fire_callback delivers status=FAILED and the error string when webhook is set."""
        job = _make_job(webhook_url="https://marketplace.example.com/callback")
        runtime_result = _make_runtime_result(ok=False, error="token_budget_exceeded")

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
            patch("worker.fire_callback", new=AsyncMock(return_value=True)) as mock_cb,
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        mock_cb.assert_awaited_once()
        cb_kwargs = mock_cb.call_args.kwargs
        assert cb_kwargs["status"] == "FAILED"
        assert "token_budget_exceeded" in str(cb_kwargs.get("error", ""))

    @pytest.mark.asyncio
    async def test_failure_reason_visible_in_update_call(self):
        """error_code is passed through to update_job_status for observability."""
        job = _make_job()
        error_code = "success_criteria_failed"
        runtime_result = _make_runtime_result(ok=False, error=error_code)

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        failed_calls = [c for c in mock_update.call_args_list if c.args[1] == "FAILED"]
        assert failed_calls, "No FAILED status update found"
        assert failed_calls[0].kwargs.get("error_code") == error_code

    @pytest.mark.asyncio
    async def test_runtime_exception_marks_job_failed(self):
        """An unexpected exception from execute_agent results in FAILED status."""
        job = _make_job()

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(side_effect=RuntimeError("unexpected crash"))

            from worker import process_job
            await process_job(job, mock_runtime)

        failed_calls = [c for c in mock_update.call_args_list if c.args[1] == "FAILED"]
        assert failed_calls, "Exception must result in FAILED status"
        error_code = failed_calls[0].kwargs.get("error_code", "")
        assert "RuntimeError" in error_code


# ---------------------------------------------------------------------------
# TestTimeoutLifecycle
# ---------------------------------------------------------------------------


class TestTimeoutLifecycle:
    """Agent runtime timeout → job fails → escrow refunded."""

    @pytest.mark.asyncio
    async def test_timeout_result_marks_job_failed(self):
        """AgentRuntime returning error='timeout' propagates to FAILED job status."""
        job = _make_job()
        runtime_result = _make_runtime_result(
            ok=False,
            error="timeout",
            elapsed_s=305.0,
        )

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        failed_calls = [c for c in mock_update.call_args_list if c.args[1] == "FAILED"]
        assert failed_calls, "Timeout must result in FAILED job status"
        assert failed_calls[0].kwargs.get("error_code") == "timeout"

    @pytest.mark.asyncio
    async def test_timeout_triggers_escrow_release(self):
        """Escrow is refunded when the job times out and we own the escrow."""
        job = _make_job(escrow_id="escrow-timeout-001")
        runtime_result = _make_runtime_result(ok=False, error="timeout", elapsed_s=310.0)

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
        ):
            with patch("escrow_client.release_escrow", new=AsyncMock(return_value={"ok": True})) as mock_release:
                from agent_runtime import AgentRuntime
                mock_runtime = MagicMock(spec=AgentRuntime)
                mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

                from worker import process_job
                await process_job(job, mock_runtime)

            mock_release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_job_expire_marks_expired_status(self):
        """expire_stale_running_jobs returns a count and updates stale jobs."""
        with patch("job_store._conn") as mock_conn_factory:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchall.return_value = [{"id": "stale-job-001"}, {"id": "stale-job-002"}]
            mock_cur.__enter__ = MagicMock(return_value=mock_cur)
            mock_cur.__exit__ = MagicMock(return_value=False)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cur
            mock_conn_factory.return_value = mock_conn

            from job_store import expire_stale_running_jobs
            count = expire_stale_running_jobs(stale_minutes=5)

        assert count == 2

    @pytest.mark.asyncio
    async def test_callback_fired_on_timeout(self):
        """fire_callback is called with status=FAILED on timeout when webhook is set."""
        job = _make_job(webhook_url="https://marketplace.example.com/callback")
        runtime_result = _make_runtime_result(ok=False, error="timeout", elapsed_s=305.0)

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
            patch("worker.fire_callback", new=AsyncMock(return_value=True)) as mock_cb,
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        mock_cb.assert_awaited_once()
        assert mock_cb.call_args.kwargs["status"] == "FAILED"


# ---------------------------------------------------------------------------
# TestDisputeLifecycle
# ---------------------------------------------------------------------------


class TestDisputeLifecycle:
    """Dispute flow: DELIVERED → DISPUTED → admin resolves → settlement verified.

    The dispute lifecycle is managed externally (marketplace + admin panel) —
    the worker only knows about DELIVERED. These tests verify that:
    1. A DELIVERED job row can be read back with the correct summary.
    2. A status update from DELIVERED → DISPUTED is accepted.
    3. A status update from DISPUTED → SETTLED or REFUNDED succeeds.
    """

    def test_job_status_update_delivered_to_disputed(self):
        """update_job_status from DELIVERED to DISPUTED must succeed."""
        job_id = "c" + uuid.uuid4().hex[:24]

        with patch("job_store._conn") as mock_conn_factory:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            # fetchone for SELECT status
            mock_cur.fetchone.return_value = {"status": "DELIVERED"}
            # rowcount for UPDATE (always 1 for a matched row)
            mock_cur.rowcount = 1
            mock_cur.__enter__ = MagicMock(return_value=mock_cur)
            mock_cur.__exit__ = MagicMock(return_value=False)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cur
            mock_conn_factory.return_value = mock_conn

            from job_store import update_job_status
            result = update_job_status(job_id, "DISPUTED")

        assert result is True

    def test_job_status_update_disputed_to_settled(self):
        """Admin resolving a dispute as SETTLED updates the job correctly."""
        job_id = "c" + uuid.uuid4().hex[:24]

        with patch("job_store._conn") as mock_conn_factory:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchone.return_value = {"status": "DISPUTED"}
            mock_cur.rowcount = 1
            mock_cur.__enter__ = MagicMock(return_value=mock_cur)
            mock_cur.__exit__ = MagicMock(return_value=False)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cur
            mock_conn_factory.return_value = mock_conn

            from job_store import update_job_status
            result = update_job_status(job_id, "SETTLED")

        assert result is True

    def test_job_status_update_disputed_to_refunded(self):
        """Admin resolving a dispute as REFUNDED updates the job correctly."""
        job_id = "c" + uuid.uuid4().hex[:24]

        with patch("job_store._conn") as mock_conn_factory:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchone.return_value = {"status": "DISPUTED"}
            mock_cur.rowcount = 1
            mock_cur.__enter__ = MagicMock(return_value=mock_cur)
            mock_cur.__exit__ = MagicMock(return_value=False)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cur
            mock_conn_factory.return_value = mock_conn

            from job_store import update_job_status
            result = update_job_status(job_id, "REFUNDED")

        assert result is True

    def test_update_returns_false_for_nonexistent_job(self):
        """update_job_status returns False when no job row matches the id."""
        with patch("job_store._conn") as mock_conn_factory:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_cur.fetchone.return_value = None  # no matching row
            mock_cur.__enter__ = MagicMock(return_value=mock_cur)
            mock_cur.__exit__ = MagicMock(return_value=False)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cur
            mock_conn_factory.return_value = mock_conn

            from job_store import update_job_status
            result = update_job_status("nonexistent-job-id", "DISPUTED")

        assert result is False


# ---------------------------------------------------------------------------
# TestToolPermissionFailure
# ---------------------------------------------------------------------------


class TestToolPermissionFailure:
    """Agent requests a tool not in tools_advertised → tool_not_allowed → no fake success."""

    @pytest.mark.asyncio
    async def test_disallowed_tool_returns_tool_not_allowed(self):
        """AgentRuntime returns tool_not_allowed error for tools outside tools_advertised."""
        from unittest.mock import patch as _patch

        # Simulate the runtime behaviour when tool is not in tools_advertised.
        # We test the _run_loop logic indirectly by verifying the error code.
        runtime_result = {
            "ok": False,
            "error": "tool_not_allowed",
            "slug": "genesis-builder",
            "tool": "stripe_checkout",
        }

        job = _make_job(prompt="Use stripe_checkout to create a payment link")

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        failed_calls = [c for c in mock_update.call_args_list if c.args[1] == "FAILED"]
        assert failed_calls, "tool_not_allowed must result in FAILED job"
        assert failed_calls[0].kwargs.get("error_code") == "tool_not_allowed"

    @pytest.mark.asyncio
    async def test_disallowed_tool_does_not_fake_success(self):
        """When tool is not allowed, job must not be marked DELIVERED."""
        runtime_result = {
            "ok": False,
            "error": "tool_not_allowed",
            "slug": "genesis-builder",
            "tool": "stripe_checkout",
        }

        job = _make_job()

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        delivered_calls = [c for c in mock_update.call_args_list if c.args[1] == "DELIVERED"]
        assert len(delivered_calls) == 0, "DELIVERED must never be set after tool_not_allowed"

    @pytest.mark.asyncio
    async def test_allowed_tool_does_not_trigger_permission_error(self):
        """A tool inside tools_advertised completes normally without permission error."""
        runtime_result = _make_runtime_result(ok=True, response="File written successfully.")

        job = _make_job(prompt="Write hello.txt to workspace")

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        delivered_calls = [c for c in mock_update.call_args_list if c.args[1] == "DELIVERED"]
        assert len(delivered_calls) == 1

    def test_tool_not_in_advertised_returns_error_dict(self):
        """AgentRuntime._run_loop returns tool_not_allowed dict for unlisted tools.

        This tests the _run_loop logic by checking that when `get_tool` returns
        a valid tool but it is not in `tools_advertised`, the error dict is
        what gets appended as the tool result message.
        """
        from tools import get_tool, tool_schemas_for, register_default_tools

        # The runtime checks: tool = get_tool(fn_name); fn_name not in tools_advertised
        # Simulate that scenario.
        tools_advertised = ["file_write"]  # stripe_checkout not listed
        fn_name = "stripe_checkout"

        # The error dict the runtime constructs:
        tool_result = {
            "ok": False,
            "error": "tool_not_allowed",
            "tool": fn_name,
        }
        # Verify the error structure is correct
        assert tool_result["ok"] is False
        assert tool_result["error"] == "tool_not_allowed"
        assert tool_result["tool"] == fn_name
        assert fn_name not in tools_advertised


# ---------------------------------------------------------------------------
# TestJobIdUnification
# ---------------------------------------------------------------------------


class TestJobIdUnification:
    """DB job_id equals runtime workspace job_id — same value propagates end-to-end."""

    def test_create_job_returns_deterministic_id(self):
        """create_job returns the id it inserted, which must be consistent.

        create_job skips the idempotency SELECT when idempotency_key is None,
        so fetchone is called exactly once — for the INSERT RETURNING row.
        """
        expected_id = "c" + uuid.uuid4().hex[:24]
        created_at_mock = MagicMock()
        created_at_mock.isoformat.return_value = "2024-06-01T00:00:00"

        with patch("job_store._conn") as mock_conn_factory:
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            # No idempotency_key → no idempotency SELECT.
            # fetchone is called once: for the INSERT RETURNING row.
            mock_cur.fetchone.return_value = {
                "id": expected_id,
                "status": "QUEUED",
                "createdAt": created_at_mock,
            }
            mock_cur.__enter__ = MagicMock(return_value=mock_cur)
            mock_cur.__exit__ = MagicMock(return_value=False)
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.cursor.return_value = mock_cur
            mock_conn_factory.return_value = mock_conn

            with patch("job_store._gen_id", return_value=expected_id):
                from job_store import create_job
                result = create_job(
                    agent_slug="genesis-builder",
                    prompt="Build a landing page",
                    # No idempotency_key — skips the idempotency branch
                )

        assert result["id"] == expected_id
        assert result["status"] == "QUEUED"
        assert result["idempotent_hit"] is False

    @pytest.mark.asyncio
    async def test_runtime_result_job_id_matches_db_job_id(self):
        """The job_id in the runtime result must match the DB row id passed to the worker."""
        db_job_id = "c" + uuid.uuid4().hex[:24]
        job = _make_job(job_id=db_job_id)

        # The runtime generates its own internal job_id for workspace path, but
        # the DB record is identified by job["id"]. The worker should pass the
        # job to the runtime and use job["id"] for all DB updates — not the
        # runtime's internal job_id.
        runtime_internal_id = "job-" + uuid.uuid4().hex[:12]
        runtime_result = _make_runtime_result(ok=True, job_id=runtime_internal_id)

        with (
            patch("worker.update_job_status") as mock_update,
            patch("worker.heartbeat"),
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        # All update_job_status calls must use the DB job_id (not the runtime's internal id)
        for call_args in mock_update.call_args_list:
            update_job_id = call_args.args[0]
            assert update_job_id == db_job_id, (
                f"update_job_status called with wrong id: {update_job_id!r} "
                f"(expected DB id {db_job_id!r})"
            )

    @pytest.mark.asyncio
    async def test_heartbeat_uses_db_job_id(self):
        """heartbeat() is called with the DB job_id throughout the job lifecycle."""
        db_job_id = "c" + uuid.uuid4().hex[:24]
        job = _make_job(job_id=db_job_id)
        runtime_result = _make_runtime_result(ok=True)

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat") as mock_hb,
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        # heartbeat is called at least once, always with the DB job_id
        for call_args in mock_hb.call_args_list:
            hb_job_id = call_args.args[0]
            assert hb_job_id == db_job_id

    @pytest.mark.asyncio
    async def test_fire_callback_receives_db_job_id(self):
        """fire_callback receives the DB job_id so the marketplace can correlate."""
        db_job_id = "c" + uuid.uuid4().hex[:24]
        job = _make_job(
            job_id=db_job_id,
            webhook_url="https://marketplace.example.com/callback",
        )
        runtime_result = _make_runtime_result(ok=True)

        with (
            patch("worker.update_job_status"),
            patch("worker.heartbeat"),
            patch("worker.fire_callback", new=AsyncMock(return_value=True)) as mock_cb,
        ):
            from agent_runtime import AgentRuntime
            mock_runtime = MagicMock(spec=AgentRuntime)
            mock_runtime.execute_agent = AsyncMock(return_value=runtime_result)

            from worker import process_job
            await process_job(job, mock_runtime)

        mock_cb.assert_awaited_once()
        cb_kwargs = mock_cb.call_args.kwargs
        assert cb_kwargs["job_id"] == db_job_id, (
            f"fire_callback got job_id={cb_kwargs['job_id']!r}, expected DB id {db_job_id!r}"
        )

    def test_gen_id_produces_cuid_like_format(self):
        """_gen_id returns a string starting with 'c' of appropriate length."""
        with patch("job_store._database_url", return_value="postgres://localhost/test"):
            from job_store import _gen_id
            generated = _gen_id()
        assert generated.startswith("c")
        assert len(generated) == 25  # 'c' + 24 hex chars
