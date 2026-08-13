"""test_tool_risk_coverage.py — FINANCE-TOOL-CONTRACTS.md Section 7 Phase 1
gating test.

Imports the live tool registry and asserts every registered name has an
explicit entry in TOOL_RISK_BY_NAME. A newly registered tool with no explicit
entry must fail this test rather than silently inherit RISK_ADMIN (which
would be safe but invisible) or any other default — Phase 1's whole point is
that every name is written by hand and reviewed, never inferred.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _live_registered_names() -> set[str]:
    import tools

    tools.register_default_tools()
    return set(tools._TOOLS.keys())


class TestToolRiskByNameCoverage:
    def test_every_registered_tool_has_an_explicit_entry(self):
        from runtime.tool_policy import TOOL_RISK_BY_NAME

        live = _live_registered_names()
        missing = live - set(TOOL_RISK_BY_NAME)
        assert missing == set(), (
            f"registered tools with no TOOL_RISK_BY_NAME entry (Section 7 "
            f"Phase 1 requires every one written by hand): {sorted(missing)}"
        )

    def test_no_orphan_entries_for_names_no_longer_registered(self):
        """Not a hard failure mode Section 7 mandates, but an orphan entry is
        exactly the kind of drift the mismatch bug itself was made of — flag
        it loudly rather than let TOOL_RISK_BY_NAME silently grow stale."""
        from runtime.tool_policy import TOOL_RISK_BY_NAME

        live = _live_registered_names()
        orphans = set(TOOL_RISK_BY_NAME) - live
        assert orphans == set(), (
            f"TOOL_RISK_BY_NAME entries for tools no longer registered — "
            f"remove or confirm the tool was intentionally deleted: {sorted(orphans)}"
        )

    def test_get_tool_risk_by_name_matches_the_table(self):
        from runtime.tool_policy import TOOL_RISK_BY_NAME, get_tool_risk_by_name

        for name, expected in TOOL_RISK_BY_NAME.items():
            assert get_tool_risk_by_name(name) == expected

    def test_unknown_tool_is_admin_risk_fail_closed(self):
        from runtime.tool_policy import RISK_ADMIN, get_tool_risk_by_name

        assert get_tool_risk_by_name("totally_unknown_tool_xyz") == RISK_ADMIN

    def test_prohibited_tool_resolves_to_prohibited_not_the_table_value(self):
        """Prohibition (Section 6) must win even for a name that also happens
        to have a TOOL_RISK_BY_NAME entry, mirroring get_tool_risk's own
        prohibition-first precedence."""
        from runtime.tool_policy import (
            PROHIBITED_TOOLS,
            RISK_PROHIBITED,
            get_tool_risk_by_name,
        )

        for name in PROHIBITED_TOOLS:
            assert get_tool_risk_by_name(name) == RISK_PROHIBITED

    def test_previously_unchanged_pairs_still_resolve_the_same_way(self):
        """These four pairs resolve identically under TOOL_RISK_BY_NAME and
        the legacy TOOL_RISK — they were never part of the mismatch bug, and
        Section 7 Phase 3 (which switched check_tool_policy over to this
        table, see runtime/tool_policy.py and test_tool_policy_matrix.py)
        must not have disturbed them."""
        from runtime.tool_policy import check_tool_policy

        assert check_tool_policy("genesis-meta", "genesis_call")["ok"] is True
        assert check_tool_policy("genesis-finance", "workspace_shell")["ok"] is False
        assert check_tool_policy("genesis-builder", "file_write")["ok"] is True

    def test_side_effect_risks_are_not_granted_to_new_slugs(self):
        from runtime.tool_policy import RISK_DEPLOYMENT, RISK_PAYMENT, SLUG_ALLOWED_RISKS

        deployment_holders = {
            slug for slug, risks in SLUG_ALLOWED_RISKS.items() if RISK_DEPLOYMENT in risks
        }
        payment_holders = {
            slug for slug, risks in SLUG_ALLOWED_RISKS.items() if RISK_PAYMENT in risks
        }
        assert deployment_holders == {"genesis-builder", "genesis-deploy"}
        assert payment_holders == set()
