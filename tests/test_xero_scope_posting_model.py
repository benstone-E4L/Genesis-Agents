"""Genesis-side scope map and xero_scoped_invoke tests."""

from __future__ import annotations

import pytest

from accounting.xero_scope import augment_tools_advertised, load_scope_map, operation_allowed, writes_forbidden, validate_scope_map_structure
from tools.xero_scoped_tool import xero_scoped_invoke


def test_scope_map_yaml_structure_valid() -> None:
    validate_scope_map_structure(load_scope_map())


def test_ap_scope_allows_bill() -> None:
    ok, _ = operation_allowed("genesis-e4l-ap", "create_draft_bill")
    assert ok is True


def test_ar_scope_denies_bill() -> None:
    ok, reason = operation_allowed("genesis-e4l-ar", "create_draft_bill")
    assert ok is False
    assert reason == "operation_denied:create_draft_bill"


def test_fs_integrity_writes_forbidden() -> None:
    assert writes_forbidden("genesis-e4l-fs-integrity") is True


def test_augment_tools_adds_xero_invoke() -> None:
    tools = augment_tools_advertised("genesis-e4l-ap", ["file_write"])
    assert "xero_scoped_invoke" in tools


@pytest.mark.asyncio
async def test_xero_invoke_requires_verify_api() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ap",
        verify_api_run_id="",
    )
    assert result["ok"] is False
    assert result["error"] == "verify_api_required"


@pytest.mark.asyncio
async def test_xero_invoke_dry_run_with_verify() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ap",
        verify_api_run_id="verify-test-001",
        entity_key="demo",
        _parent_agent_slug="genesis-e4l-ap",
    )
    assert result["ok"] is True
    assert result.get("dry_run") is True
    assert "receipt" in result


@pytest.mark.asyncio
async def test_xero_invoke_scope_denied_for_ar_bill() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ar",
        verify_api_run_id="verify-test-002",
        _parent_agent_slug="genesis-e4l-ar",
    )
    assert result["ok"] is False
    assert result["error"] == "scope_forbidden"


@pytest.mark.asyncio
async def test_xero_invoke_rejects_agent_slug_escalation() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        agent_slug="genesis-e4l-ap",
        verify_api_run_id="verify-test-escalation",
        _parent_agent_slug="genesis-e4l-ar",
    )
    assert result["ok"] is False
    assert result["error"] == "agent_slug_mismatch"


@pytest.mark.asyncio
async def test_xero_invoke_enforces_dispatch_allowlist() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_invoice",
        verify_api_run_id="verify-test-allowlist",
        _parent_agent_slug="genesis-e4l-ar",
        _allowed_xero_operations=["get_trial_balance"],
    )
    assert result["ok"] is False
    assert result["error"] == "dispatch_scope_denied"


@pytest.mark.asyncio
async def test_xero_invoke_rejects_short_verify_id() -> None:
    result = await xero_scoped_invoke(
        operation="create_draft_bill",
        verify_api_run_id="short",
        _parent_agent_slug="genesis-e4l-ap",
    )
    assert result["ok"] is False
    assert result["error"] == "verify_api_required"
