"""CI gate for the PERMANENTLY_PROHIBITED tool list.

docs/FINANCE-TOOL-CONTRACTS.md Section 6.2 Layer 5.

A guard with no negative control is not a guard. The tests marked NEGATIVE
CONTROL deliberately break each prohibition and assert that the guard raises.
If those tests ever start passing without the assertion firing, the guard is
decorative and this file is the thing that will say so.

Runs on the standard library plus pytest. No Genesis dependencies required.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.tool_policy import (  # noqa: E402
    DEFAULT_ALLOWED_RISKS,
    PROHIBITED_TOOLS,
    PROHIBITED_TOOLS_BY_SLUG,
    PROHIBITION_GROUPS,
    PROHIBITION_MANIFEST_NAMES,
    RISK_PAYMENT,
    RISK_PROHIBITED,
    SLUG_ALLOWED_RISKS,
    assert_prohibitions_intact,
    check_tool_policy,
    prohibition_manifest_digest,
)

MANIFEST_PATH = PROJECT_ROOT / "runtime" / "prohibited_tools.sha256"

# The four escrow_client functions are module functions, never registered tools.
REGISTERED_NAME_PROHIBITIONS = sorted(
    n for n in PROHIBITED_TOOLS if not n.startswith("escrow_client.")
)

MONEY_MODULES = [
    PROJECT_ROOT / "tools" / "finance_tool.py",
    PROJECT_ROOT / "tools" / "billing_tool.py",
    PROJECT_ROOT / "tools" / "commerce_tool.py",
    PROJECT_ROOT / "tools" / "pricing_tool.py",
    PROJECT_ROOT / "tools" / "domain_tool.py",
]


@pytest.fixture(scope="module")
def registry() -> dict:
    """The live tool registry after normal auto-registration."""
    import tools

    tools.register_default_tools()
    return tools._TOOLS


# ---------------------------------------------------------------------------
# The list itself
# ---------------------------------------------------------------------------

class TestProhibitionList:
    def test_twenty_names_are_prohibited(self):
        assert len(PROHIBITION_MANIFEST_NAMES) == 20, (
            "The contract enumerates exactly 20 PERMANENTLY_PROHIBITED names "
            f"(Section 6.1). Found {len(PROHIBITION_MANIFEST_NAMES)}."
        )

    def test_every_name_has_a_group(self):
        for name in PROHIBITION_MANIFEST_NAMES:
            assert PROHIBITION_GROUPS[name] in {"A", "B", "C"}

    def test_group_c_is_the_fabricated_report(self):
        group_c = [n for n, g in PROHIBITION_GROUPS.items() if g == "C"]
        assert group_c == ["pricing_generate_pricing_report"], (
            "Group C records that fabricating financial figures and returning "
            "them as verified success is prohibited for the same reason moving "
            "money is. It is not 'only a reporting bug'."
        )

    def test_escrow_functions_are_prohibited(self):
        for name in (
            "escrow_client.initiate_escrow",
            "escrow_client.complete_escrow",
            "escrow_client.release_escrow",
            "escrow_client.calculate_split",
        ):
            assert name in PROHIBITED_TOOLS


# ---------------------------------------------------------------------------
# Layer 1 — deletion
# ---------------------------------------------------------------------------

class TestLayer1Deletion:
    def test_no_prohibited_tool_is_registered(self, registry):
        present = sorted(PROHIBITED_TOOLS & set(registry))
        assert present == [], f"prohibited tools are registered: {present}"

    def test_get_tool_returns_none_for_every_prohibited_name(self, registry):
        import tools

        for name in REGISTERED_NAME_PROHIBITIONS:
            assert tools.get_tool(name) is None, f"{name} is still dispatchable"

    def test_no_register_tool_line_exists_for_a_prohibited_name(self):
        """Source-tree scan. Catches a re-registration that has not been run yet."""
        offenders: list[str] = []
        for path in sorted((PROJECT_ROOT / "tools").glob("*_tool.py")):
            text = path.read_text(encoding="utf-8")
            for name in REGISTERED_NAME_PROHIBITIONS:
                if re.search(rf'register_tool\(\s*["\']{re.escape(name)}["\']', text):
                    offenders.append(f"{path.name}: register_tool({name!r})")
        assert offenders == [], f"prohibited register_tool lines found: {offenders}"

    def test_no_prohibited_name_appears_in_any_tool_schema(self, registry):
        import tools

        for name in REGISTERED_NAME_PROHIBITIONS:
            assert name not in tools._TOOL_SCHEMAS, f"{name} still has a schema"


# ---------------------------------------------------------------------------
# Layer 2 — boot-time registry assertion
# ---------------------------------------------------------------------------

class TestLayer2BootAssertion:
    def test_passes_on_a_clean_registry(self, registry):
        assert_prohibitions_intact()  # must not raise

    def test_negative_control_registered_prohibited_tool_makes_the_guard_raise(self, registry):
        """NEGATIVE CONTROL.

        Re-register a prohibited tool into the REAL registry and prove the boot
        assertion raises. Without this test, a guard that silently never fires
        would look identical to a guard that works.
        """
        import tools

        victim = "finance_run_payroll_batch"
        assert victim in PROHIBITED_TOOLS
        assert tools.get_tool(victim) is None

        async def _reintroduced(**kwargs):  # pragma: no cover - never called
            return {"ok": True}

        tools._TOOLS[victim] = _reintroduced
        try:
            with pytest.raises(RuntimeError, match="prohibited tools are registered"):
                assert_prohibitions_intact()
        finally:
            tools._TOOLS.pop(victim, None)

        # And the guard must go quiet again once the prohibition is restored.
        assert_prohibitions_intact()

    def test_negative_control_every_prohibited_name_trips_the_guard(self):
        """NEGATIVE CONTROL, exhaustive.

        Not just one name: each of the 19 globally prohibited names must trip
        the assertion on its own, so no single entry is decorative.
        """
        for name in sorted(PROHIBITED_TOOLS):
            with pytest.raises(RuntimeError, match="prohibited tools are registered"):
                assert_prohibitions_intact(registry={name: object()})

    def test_negative_control_slug_granting_prohibited_risk_raises(self, registry):
        """NEGATIVE CONTROL. No slug may hold RISK_PROHIBITED."""
        import runtime.tool_policy as tp

        original = dict(tp.SLUG_ALLOWED_RISKS)
        tp.SLUG_ALLOWED_RISKS["genesis-finance"] = frozenset({RISK_PROHIBITED})
        try:
            with pytest.raises(RuntimeError, match="grants RISK_PROHIBITED"):
                assert_prohibitions_intact()
        finally:
            tp.SLUG_ALLOWED_RISKS.clear()
            tp.SLUG_ALLOWED_RISKS.update(original)
        assert_prohibitions_intact()


# ---------------------------------------------------------------------------
# Layer 3 — frozen manifest
# ---------------------------------------------------------------------------

class TestLayer3FrozenManifest:
    def test_manifest_file_exists(self):
        assert MANIFEST_PATH.is_file(), f"missing frozen manifest: {MANIFEST_PATH}"

    def test_manifest_matches_the_list(self):
        on_disk = [
            line.strip()
            for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert on_disk == [prohibition_manifest_digest()]

    def test_negative_control_editing_the_list_without_the_hash_breaks_the_boot(self, registry):
        """NEGATIVE CONTROL.

        Remove a name from the prohibition list and prove the boot assertion
        raises on the manifest mismatch. This is what converts a one-line config
        edit into a two-file, self-documenting, reviewable change.
        """
        import runtime.tool_policy as tp

        original_groups = dict(tp.PROHIBITION_GROUPS)
        original_names = tp.PROHIBITION_MANIFEST_NAMES
        original_set = tp.PROHIBITED_TOOLS
        try:
            tp.PROHIBITION_GROUPS.pop("finance_run_payroll_batch")
            tp.PROHIBITION_MANIFEST_NAMES = tuple(sorted(tp.PROHIBITION_GROUPS))
            tp.PROHIBITED_TOOLS = frozenset(
                n for n in tp.PROHIBITION_GROUPS if n not in tp._SLUG_SCOPED_ONLY
            )
            with pytest.raises(RuntimeError, match="prohibition manifest mismatch"):
                assert_prohibitions_intact()
        finally:
            tp.PROHIBITION_GROUPS.clear()
            tp.PROHIBITION_GROUPS.update(original_groups)
            tp.PROHIBITION_MANIFEST_NAMES = original_names
            tp.PROHIBITED_TOOLS = original_set
        assert_prohibitions_intact()

    def test_regen_script_is_idempotent(self):
        """Regenerating must not change a manifest that is already correct."""
        before = MANIFEST_PATH.read_bytes()
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "regen_prohibited_manifest.py")],
            check=True,
            capture_output=True,
            cwd=str(PROJECT_ROOT),
        )
        assert MANIFEST_PATH.read_bytes() == before


# ---------------------------------------------------------------------------
# Layer 4 / policy — denial for every name x every slug
# ---------------------------------------------------------------------------

class TestPolicyDenial:
    def test_every_prohibited_name_is_denied_to_every_slug(self):
        slugs = sorted(SLUG_ALLOWED_RISKS) + ["genesis-unknown-slug-xyz", ""]
        for name in sorted(PROHIBITED_TOOLS):
            for slug in slugs:
                result = check_tool_policy(slug, name)
                assert result["ok"] is False, f"{name} allowed for {slug!r}: {result}"
                assert result["risk_class"] == RISK_PROHIBITED

    def test_slug_scoped_prohibition_applies_to_finance_domain_slugs(self):
        for slug in PROHIBITED_TOOLS_BY_SLUG:
            result = check_tool_policy(slug, "workflow_webhook_trigger")
            assert result["ok"] is False
            assert result["risk_class"] == RISK_PROHIBITED

    def test_slug_scoped_name_is_not_globally_absent(self, registry):
        """workflow_webhook_trigger stays registered: it is legitimate outside a
        finance context. Its prohibition is slug-scoped, not absence-based."""
        assert "workflow_webhook_trigger" not in PROHIBITED_TOOLS

    def test_risk_prohibited_appears_in_no_allowed_set(self):
        for slug, allowed in SLUG_ALLOWED_RISKS.items():
            assert RISK_PROHIBITED not in allowed, slug
        assert RISK_PROHIBITED not in DEFAULT_ALLOWED_RISKS

    def test_risk_payment_appears_in_no_allowed_set(self):
        """Section 7 Phase 3/5 landed: RISK_PAYMENT was removed from
        genesis-finance in the same commit that switched check_tool_policy to
        TOOL_RISK_BY_NAME, and is now permanently unreachable (enforced again,
        redundantly, by assert_prohibitions_intact()). Every payment-capable
        tool is additionally prohibited by name, which is the stronger control
        regardless of this one.

        If this test ever fails, a slug was granted RISK_PAYMENT again --
        re-read docs/FINANCE-TOOL-CONTRACTS.md Section 7 before touching it.
        """
        holders = sorted(s for s, a in SLUG_ALLOWED_RISKS.items() if RISK_PAYMENT in a)
        assert holders == [], f"RISK_PAYMENT must be granted to no slug, found: {holders}"
        assert RISK_PAYMENT not in DEFAULT_ALLOWED_RISKS


# ---------------------------------------------------------------------------
# Section 5 — the truthful-failure envelope in the money modules
# ---------------------------------------------------------------------------

class TestTruthfulFailureEnvelope:
    def test_banned_keys_appear_in_no_money_module(self):
        offenders: list[str] = []
        for path in MONEY_MODULES:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for banned in ('"stub"', "'stub'", '"scaffold"', "'scaffold'", '"note"', "'note'"):
                    if banned in line:
                        offenders.append(f"{path.name}:{lineno}: {banned}")
        assert offenders == [], f"banned response keys present: {offenders}"

    def test_no_fabricated_money_constants_remain_in_executable_code(self):
        """The invented figures must be gone from every executable path.

        Scanned via the AST, not raw text, so that a comment or docstring
        recording what the old constant WAS does not count as a live constant —
        and, more importantly, so that a constant hidden inside an expression
        cannot escape a text grep.

        Section 4 forbids float money outright, so the rule enforced here is the
        stricter one: no float literal may appear anywhere in a money module.
        That subsumes 482500.00, 8.4, -1.32, 49.00, 15000.00 and 1500.00.
        """
        import ast

        banned_ints = {482500, 15000, 1500, 4900}
        offenders: list[str] = []
        for path in MONEY_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if isinstance(node.value, bool):
                    continue
                if isinstance(node.value, float):
                    offenders.append(f"{path.name}:{node.lineno}: float literal {node.value}")
                elif isinstance(node.value, int) and node.value in banned_ints:
                    offenders.append(f"{path.name}:{node.lineno}: fabricated figure {node.value}")
        assert offenders == [], f"fabricated/float money constants remain: {offenders}"

    def test_fabricated_figures_appear_in_no_response(self, registry):
        """Belt and braces: sweep every money tool's actual response for the
        invented figures the contract names."""
        import tools

        prefixes = ("finance_", "billing_", "commerce_", "pricing_", "domain_")
        needles = ["482500", "15000", "1500.0", "49.0", "-1.32", "8.4"]
        for name in [n for n in sorted(registry) if n.startswith(prefixes)]:
            body = str(asyncio.run(tools.get_tool(name)()))
            for needle in needles:
                assert needle not in body, f"{name} response still contains {needle}"

    def test_every_money_tool_response_is_a_valid_envelope(self, registry):
        """No money-module tool may return ok=true without evidence, and every
        failure must carry a taxonomy code with the correct retryability."""
        import tools
        from tools._envelope import BANNED_RESPONSE_KEYS, RETRYABLE_BY_CODE

        prefixes = ("finance_", "billing_", "commerce_", "pricing_", "domain_")
        names = [n for n in sorted(registry) if n.startswith(prefixes)]
        assert names, "no money-domain tools registered — the sweep would be vacuous"

        for name in names:
            fn = tools.get_tool(name)
            resp = asyncio.run(fn())
            assert isinstance(resp, dict), name
            assert "ok" in resp, name
            assert not (BANNED_RESPONSE_KEYS & set(resp)), f"{name} leaked a banned key"
            if resp["ok"]:
                assert resp.get("evidence"), f"{name} returned ok=true with no evidence"
                assert "error" not in resp, name
            else:
                assert "result" not in resp, name
                err = resp["error"]
                assert err["code"] in RETRYABLE_BY_CODE, f"{name}: {err['code']}"
                assert err["retryable"] is RETRYABLE_BY_CODE[err["code"]], name

    def test_pricing_report_shim_refuses_and_ignores_its_input(self):
        """The fabricated report is dead: no input reaches any figure."""
        from tools.pricing_tool import pricing_generate_pricing_report

        a = asyncio.run(pricing_generate_pricing_report(period="2026-07", dashboards=3))
        b = asyncio.run(pricing_generate_pricing_report(period="1999-01", dashboards=9999))
        for resp in (a, b):
            assert resp["ok"] is False
            assert resp["error"]["code"] == "policy_denied"
            assert resp["error"]["retryable"] is False
            assert "482500" not in str(resp)

    def test_success_envelope_requires_evidence(self):
        """NEGATIVE CONTROL for the structural guarantee that closes the stub
        loophole: success() must refuse to build ok=true without evidence."""
        from tools._envelope import MODE_READ_ONLY, success

        with pytest.raises(ValueError, match="requires a non-empty evidence"):
            success(tool="t", mode=MODE_READ_ONLY, result={"x": 1}, evidence={})

    def test_failure_envelope_rejects_an_unknown_code(self):
        from tools._envelope import failure

        with pytest.raises(ValueError, match="unknown error code"):
            failure(tool="t", code="something_went_wrong", message="m")

    def test_non_retryable_codes_cannot_carry_a_retry_hint(self):
        from tools._envelope import failure

        resp = failure(tool="t", code="not_implemented", message="m", retry_after_ms=5000)
        assert resp["error"]["retryable"] is False
        assert resp["error"]["retry_after_ms"] is None
