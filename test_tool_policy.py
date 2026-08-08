"""test_tool_policy.py — Phase 8 tool security boundary tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestToolRiskClassification:
    def test_known_tools_have_risk_classes(self):
        from runtime.tool_policy import TOOL_RISK
        assert "file_write" in TOOL_RISK
        assert "genesis_call" in TOOL_RISK
        assert "workspace_shell" in TOOL_RISK
        assert "conduit" in TOOL_RISK

    def test_unknown_tool_is_admin_risk(self):
        from runtime.tool_policy import get_tool_risk, RISK_ADMIN
        assert get_tool_risk("totally_unknown_tool_xyz") == RISK_ADMIN, (
            "Unknown tools must default to RISK_ADMIN (fail-closed)"
        )

    def test_genesis_call_is_subagent_risk(self):
        from runtime.tool_policy import get_tool_risk, RISK_SUBAGENT
        assert get_tool_risk("genesis_call") == RISK_SUBAGENT

    def test_workspace_shell_is_shell_risk(self):
        from runtime.tool_policy import get_tool_risk, RISK_SHELL
        assert get_tool_risk("workspace_shell") == RISK_SHELL

    def test_file_write_is_filesystem_write_risk(self):
        from runtime.tool_policy import get_tool_risk, RISK_FILESYSTEM_WRITE
        assert get_tool_risk("file_write") == RISK_FILESYSTEM_WRITE

    def test_vercel_deploy_is_deployment_risk(self):
        from runtime.tool_policy import get_tool_risk, RISK_DEPLOYMENT
        assert get_tool_risk("vercel_deploy") == RISK_DEPLOYMENT

    def test_finance_is_payment_risk(self):
        from runtime.tool_policy import get_tool_risk, RISK_PAYMENT
        assert get_tool_risk("finance") == RISK_PAYMENT


class TestSlugPermissions:
    """Per-agent permission enforcement."""

    # genesis-meta CAN call genesis_call
    def test_genesis_meta_can_call_genesis_call(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-meta", "genesis_call")
        assert result["ok"] is True, (
            f"genesis-meta must be allowed to call genesis_call. Got: {result}"
        )

    # genesis-finance CANNOT call workspace_shell
    def test_genesis_finance_cannot_call_workspace_shell(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-finance", "workspace_shell")
        assert result["ok"] is False, (
            f"genesis-finance must NOT be allowed to call workspace_shell. Got: {result}"
        )
        assert result["error"] == "tool_policy_denied"

    # genesis-builder CAN call file_write
    def test_genesis_builder_can_call_file_write(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-builder", "file_write")
        assert result["ok"] is True, (
            f"genesis-builder must be allowed to call file_write. Got: {result}"
        )

    # Unknown tools fail closed for all agents
    def test_unknown_tool_fails_closed_for_genesis_meta(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-meta", "totally_unknown_tool_xyz")
        assert result["ok"] is False, (
            "Unknown tools must fail closed (ok=False) even for genesis-meta. "
            f"Got: {result}"
        )

    def test_unknown_tool_fails_closed_for_unknown_agent(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-unknown-agent-xyz", "some_tool")
        assert result["ok"] is False, (
            "Unknown agent + unknown tool must fail closed. Got: {result}"
        )

    def test_unknown_agent_defaults_to_read_only_only(self):
        from runtime.tool_policy import check_tool_policy, RISK_READ_ONLY
        # code_format is read_only — should be allowed even for unknown agents
        result = check_tool_policy("genesis-unknown-agent-xyz", "code_format")
        assert result["ok"] is True, (
            "code_format (read_only risk) must be allowed for unknown agents. Got: {result}"
        )
        # file_write is filesystem_write — must NOT be allowed for unknown agents
        result2 = check_tool_policy("genesis-unknown-agent-xyz", "file_write")
        assert result2["ok"] is False, (
            "file_write must NOT be allowed for unknown agents (default read_only only). Got: {result2}"
        )

    def test_result_contains_risk_class(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-meta", "genesis_call")
        assert "risk_class" in result
        assert result["risk_class"] == "subagent"

    def test_result_contains_allowed_risks(self):
        from runtime.tool_policy import check_tool_policy
        result = check_tool_policy("genesis-meta", "genesis_call")
        assert "allowed_risks" in result
        assert isinstance(result["allowed_risks"], list)


class TestIsToolAllowed:
    def test_is_tool_allowed_returns_bool(self):
        from runtime.tool_policy import is_tool_allowed
        assert isinstance(is_tool_allowed("genesis-meta", "genesis_call"), bool)

    def test_is_tool_allowed_consistent_with_check(self):
        from runtime.tool_policy import is_tool_allowed, check_tool_policy
        pairs = [
            ("genesis-meta", "genesis_call"),
            ("genesis-finance", "workspace_shell"),
            ("genesis-builder", "file_write"),
            ("genesis-research", "web_search"),
        ]
        for slug, tool in pairs:
            check_result = check_tool_policy(slug, tool)["ok"]
            bool_result = is_tool_allowed(slug, tool)
            assert bool_result == check_result, (
                f"is_tool_allowed({slug!r}, {tool!r})={bool_result} "
                f"disagrees with check_tool_policy={check_result}"
            )
