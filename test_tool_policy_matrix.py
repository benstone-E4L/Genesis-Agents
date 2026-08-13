"""test_tool_policy_matrix.py — FINANCE-TOOL-CONTRACTS.md Section 7 Phase 3
gating test.

expected_policy_matrix.json is a checked-in fixture, generated once from a
reviewed check_tool_policy() run and committed alongside this test. Reviewing
its diff is how a human sees, in one screen, exactly which permissions a
commit changes (Section 7 Phase 3). This test does not regenerate the
fixture — it asserts live behavior still matches what was reviewed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_PATH = PROJECT_ROOT / "expected_policy_matrix.json"

# The slug set the fixture was generated over: every SLUG_ALLOWED_RISKS key
# plus one sentinel representing DEFAULT_ALLOWED_RISKS for every other slug.
_UNKNOWN_SLUG = "unknown-slug-xyz"


def _live_registered_names() -> list[str]:
    import tools

    tools.register_default_tools()
    return sorted(tools._TOOLS.keys())


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestExpectedPolicyMatrixFixture:
    def test_fixture_file_exists(self):
        assert FIXTURE_PATH.exists(), (
            "expected_policy_matrix.json must be checked in — Section 7 Phase 3 "
            "requires a reviewable fixture, not a value computed at test time"
        )

    def test_fixture_covers_every_registered_name(self):
        from runtime.tool_policy import SLUG_ALLOWED_RISKS

        matrix = _fixture()
        names = _live_registered_names()
        slugs = sorted(SLUG_ALLOWED_RISKS) + [_UNKNOWN_SLUG]
        missing = [
            f"{slug}::{name}"
            for slug in slugs
            for name in names
            if f"{slug}::{name}" not in matrix
        ]
        assert missing == [], (
            f"registered (slug, name) pairs absent from the fixture — a new "
            f"tool cannot be added without a reviewer writing down who may "
            f"call it: {missing}"
        )

    def test_live_check_tool_policy_matches_the_fixture_exhaustively(self):
        from runtime.tool_policy import check_tool_policy

        matrix = _fixture()
        mismatches = []
        for key, expected in matrix.items():
            slug, name = key.split("::", 1)
            actual = check_tool_policy(slug, name)
            if actual["ok"] != expected["ok"] or actual["risk_class"] != expected["risk_class"]:
                mismatches.append(
                    {
                        "pair": key,
                        "expected": expected,
                        "actual": {"ok": actual["ok"], "risk_class": actual["risk_class"]},
                    }
                )
        assert mismatches == [], (
            f"live check_tool_policy diverged from the reviewed fixture — if "
            f"this is an intended permission change, regenerate and re-review "
            f"expected_policy_matrix.json: {mismatches}"
        )

    def test_risk_payment_appears_in_no_allowed_set(self):
        from runtime.tool_policy import (
            DEFAULT_ALLOWED_RISKS,
            RISK_PAYMENT,
            SLUG_ALLOWED_RISKS,
        )

        all_allowed: set[str] = set(DEFAULT_ALLOWED_RISKS)
        for allowed in SLUG_ALLOWED_RISKS.values():
            all_allowed |= set(allowed)
        assert RISK_PAYMENT not in all_allowed, (
            "Section 7 Phase 5: RISK_PAYMENT must be permanently unreachable"
        )

    def test_default_policy_remains_read_only(self):
        from runtime.tool_policy import DEFAULT_ALLOWED_RISKS, RISK_READ_ONLY

        assert DEFAULT_ALLOWED_RISKS == frozenset({RISK_READ_ONLY})

    def test_every_bundle_has_a_slug_specific_policy(self):
        from bundle_loader import list_bundles
        from runtime.tool_policy import SLUG_ALLOWED_RISKS

        assert set(list_bundles()) == set(SLUG_ALLOWED_RISKS)

    def test_email_transmission_is_human_only_for_every_slug(self):
        from runtime.tool_policy import SLUG_ALLOWED_RISKS, check_tool_policy

        for slug in SLUG_ALLOWED_RISKS:
            decision = check_tool_policy(slug, "send_email")
            assert decision["ok"] is False
            assert decision["error"] == "human_action_required"

    def test_every_prohibited_name_is_denied_to_every_slug_in_the_fixture(self):
        from runtime.tool_policy import PROHIBITED_TOOLS, SLUG_ALLOWED_RISKS

        matrix = _fixture()
        slugs = sorted(SLUG_ALLOWED_RISKS) + [_UNKNOWN_SLUG]
        live_names = set(_live_registered_names())
        offenders = []
        for slug in slugs:
            for name in PROHIBITED_TOOLS & live_names:
                key = f"{slug}::{name}"
                if key in matrix and matrix[key]["ok"] is not False:
                    offenders.append(key)
        assert offenders == [], f"prohibited names not denied: {offenders}"

    def test_fixture_has_no_stale_entries(self):
        """Not a hard Section 7 requirement, but an entry for a tool no longer
        registered is exactly the kind of drift the mismatch bug was made
        of — flag it rather than let the fixture silently outlive reality."""
        from runtime.tool_policy import SLUG_ALLOWED_RISKS

        matrix = _fixture()
        names = set(_live_registered_names())
        slugs = set(SLUG_ALLOWED_RISKS) | {_UNKNOWN_SLUG}
        stale = []
        for key in matrix:
            slug, name = key.split("::", 1)
            if slug not in slugs or name not in names:
                stale.append(key)
        assert stale == [], f"fixture entries for slugs/tools no longer live: {stale}"
