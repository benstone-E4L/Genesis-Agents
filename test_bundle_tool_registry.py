"""Test: every tool advertised in a skill bundle is registered in the tool registry.

Run with:
    pytest test_bundle_tool_registry.py
from C:\\Users\\Ben\\Desktop\\Github\\Genesis-Agents
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path so `tools` package resolves correctly.
PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import _TOOL_SCHEMAS, _TOOLS, get_tool, register_default_tools, tool_schemas_for

BUNDLES_DIR = PROJECT_ROOT / "skill_bundles"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_all_bundles() -> list[dict]:
    """Return parsed JSON for every *.json file in skill_bundles/."""
    bundles = []
    for path in sorted(BUNDLES_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            bundles.append(json.load(fh))
    return bundles


def _load_bundle(slug: str) -> dict:
    path = BUNDLES_DIR / f"{slug}.json"
    assert path.exists(), f"Bundle file not found: {path}"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def registered():
    """Register all tools once for the entire module."""
    register_default_tools()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_skill_bundle_tools_are_registered():
    """Every tool name in tools_advertised must have a live registry entry."""
    bundles = _load_all_bundles()
    assert bundles, "No skill bundles found — check BUNDLES_DIR path"

    failures: list[str] = []
    for bundle in bundles:
        slug = bundle.get("slug", "<unknown>")
        advertised = bundle.get("tools_advertised", [])
        for tool_name in advertised:
            if get_tool(tool_name) is None:
                failures.append(f"  bundle '{slug}' advertises '{tool_name}' — NOT in registry")

    if failures:
        detail = "\n".join(failures)
        pytest.fail(
            f"{len(failures)} advertised tool(s) are missing from the registry:\n{detail}"
        )


def test_registered_tools_have_schemas():
    """Every registered tool must have a schema with non-empty function.name and function.description."""
    missing_schema_tools = [name for name in _TOOLS if name not in _TOOL_SCHEMAS]
    assert not missing_schema_tools, (
        f"Tools registered without a schema: {missing_schema_tools}"
    )

    bad: list[str] = []
    for tool_name, schema in _TOOL_SCHEMAS.items():
        fn_block = schema.get("function", {})
        fn_name = fn_block.get("name", "")
        fn_desc = fn_block.get("description", "")
        if not fn_name:
            bad.append(f"  '{tool_name}': missing function.name")
        if not fn_desc:
            bad.append(f"  '{tool_name}': missing function.description")

    if bad:
        detail = "\n".join(bad)
        pytest.fail(f"Schema integrity failures:\n{detail}")


def test_no_duplicate_tool_names():
    """Tool names must be unique — double-registration silently overwrites callables.

    register_default_tools() is idempotent (re-registering the same name with
    the same callable is acceptable), but two *different* modules registering
    the same name would be a bug.  We detect this by calling register_default_tools
    a second time and verifying the registry size is stable and no new name appeared.
    """
    names_before = set(_TOOLS.keys())
    size_before = len(names_before)

    register_default_tools()  # second call — should be a no-op

    names_after = set(_TOOLS.keys())
    size_after = len(names_after)

    assert size_before == size_after, (
        f"Registry changed size after second register_default_tools() call: "
        f"{size_before} -> {size_after}. Newly added: {names_after - names_before}"
    )
    # Confirm names are the same set (no ghost names dropped either)
    assert names_before == names_after


def test_builder_bundle_tools():
    """genesis-builder must advertise exactly the tools its agents depend on, all registered."""
    bundle = _load_bundle("genesis-builder")
    slug = bundle["slug"]
    advertised = bundle.get("tools_advertised", [])

    required = {"file_write", "code_format", "run_code", "github_tool", "conduit"}
    missing_from_bundle = required - set(advertised)
    assert not missing_from_bundle, (
        f"genesis-builder bundle is missing expected tools: {missing_from_bundle}"
    )

    unregistered = [t for t in advertised if get_tool(t) is None]
    assert not unregistered, (
        f"genesis-builder advertises tools not in registry: {unregistered}"
    )

    schemas = tool_schemas_for(advertised)
    registered_names = {s["function"]["name"] for s in schemas}
    assert registered_names == set(advertised), (
        f"tool_schemas_for returned schemas for only {registered_names}, expected {set(advertised)}"
    )


def test_deploy_bundle_tools():
    """genesis-deploy must advertise only the delivery tools it actually has.

    vercel_deploy/netlify_deploy were removed (E4L deploys to Azure only, and
    those tools took live credentials for platforms it does not use). The bundle
    must not re-advertise them, and its public description must not promise a
    hosting deploy it cannot perform.
    """
    bundle = _load_bundle("genesis-deploy")
    advertised = bundle.get("tools_advertised", [])

    required = {"file_write", "github_tool", "run_code", "conduit"}
    missing_from_bundle = required - set(advertised)
    assert not missing_from_bundle, (
        f"genesis-deploy bundle is missing expected tools: {missing_from_bundle}"
    )

    removed = {"vercel_deploy", "netlify_deploy"} & set(advertised)
    assert not removed, (
        f"genesis-deploy re-advertises deleted hosting-deploy tools: {removed}"
    )

    unregistered = [t for t in advertised if get_tool(t) is None]
    assert not unregistered, (
        f"genesis-deploy advertises tools not in registry: {unregistered}"
    )

    for name in required:
        fn = get_tool(name)
        assert callable(fn), f"get_tool('{name}') returned non-callable: {fn!r}"

    # capability_cards.card_for() publishes system_prompt[:300] as the public
    # marketplace description — the disclaimer has to survive that truncation.
    card_description = bundle["system_prompt"][:300].lower()
    assert "no hosting-provider deploy tool" in card_description, (
        "genesis-deploy's marketplace description must state, inside the first "
        "300 characters, that it cannot deploy to a hosting provider"
    )


def test_qa_bundle_tools():
    """genesis-qa must advertise screenshot_url (and all advertised tools must be registered)."""
    bundle = _load_bundle("genesis-qa")
    advertised = bundle.get("tools_advertised", [])

    assert "screenshot_url" in advertised, (
        "genesis-qa bundle must include 'screenshot_url' in tools_advertised"
    )

    unregistered = [t for t in advertised if get_tool(t) is None]
    assert not unregistered, (
        f"genesis-qa advertises tools not in registry: {unregistered}"
    )

    fn = get_tool("screenshot_url")
    assert callable(fn), f"get_tool('screenshot_url') returned non-callable: {fn!r}"


def test_meta_bundle_has_genesis_call():
    """genesis-meta must include genesis_call tool and it must be registered."""
    bundle = _load_bundle("genesis-meta")
    advertised = bundle.get("tools_advertised", [])

    assert "genesis_call" in advertised, (
        "genesis-meta bundle must include 'genesis_call' in tools_advertised"
    )

    fn = get_tool("genesis_call")
    assert fn is not None, "genesis_call is not registered in the tool registry"
    assert callable(fn), f"get_tool('genesis_call') returned non-callable: {fn!r}"

    schemas = tool_schemas_for(["genesis_call"])
    assert len(schemas) == 1, "tool_schemas_for should return exactly one schema for genesis_call"
    fn_block = schemas[0].get("function", {})
    assert fn_block.get("name") == "genesis_call"
    assert fn_block.get("description"), "genesis_call schema has empty description"


# ---------------------------------------------------------------------------
# Bundle truthfulness — general rules, not per-agent special cases
#
# A skill bundle is an advertisement. capability_cards.card_for() publishes
# system_prompt[:300] and output_shape_hint verbatim on the public marketplace
# card, and tool_methods_from_source is read by humans and by agents reasoning
# about what a slug can do. docs/FINANCE-TOOL-CONTRACTS.md Section 6.2 Layer 6
# already filters PROHIBITED_TOOLS out of _tool_descriptions(); that filter
# never reached the bundle files themselves, which is how 13 permanently
# prohibited operations (run_payroll_batch, activate_payment_gateway,
# purchase_dataset, ...) stayed advertised in four money-domain bundles.
#
# These tests pin the general rule so the rot cannot return in any bundle.
# ---------------------------------------------------------------------------

# Domain prefixes used by the registered tool names. A legacy agent source
# method is the same operation as `<prefix><method>`, so both forms must be
# checked against the prohibition list.
_TOOL_NAME_PREFIXES = (
    "finance_", "billing_", "commerce_", "pricing_", "domain_", "escrow_client.",
)

# Names the legacy agent sources used for operations the prohibition list holds
# under a different spelling. These are recorded explicitly rather than matched
# by fuzzy suffix rules, which produced false positives ("report" matching
# pricing_generate_pricing_report, "domain" matching commerce_register_domain).
# Each entry is a reviewed judgement, not an inference.
_PROHIBITED_ALIASES: dict[str, str] = {
    "select_and_register_domain": "domain_select_and_register",
    "create_payment_intent_mandate": "domain_create_intent_mandate",
    "_request_payment_consent_ap2": "domain_create_intent_mandate",
    "registration_success": "domain_register",
}


def _bare(prohibited_name: str) -> str:
    for prefix in _TOOL_NAME_PREFIXES:
        if prohibited_name.startswith(prefix):
            return prohibited_name[len(prefix):]
    return prohibited_name


def _advertised_names(bundle: dict) -> list[tuple[str, str]]:
    """Every name a bundle publishes, tagged with the field it came from."""
    out: list[tuple[str, str]] = []
    for m in bundle.get("tool_methods_from_source") or []:
        out.append(("tool_methods_from_source", m.get("name", "")))
    for t in bundle.get("tools_advertised") or []:
        out.append(("tools_advertised", t))
    for k in bundle.get("output_shape_hint") or []:
        out.append(("output_shape_hint", k))
    return out


def test_no_bundle_advertises_a_permanently_prohibited_operation():
    """No bundle field may name an operation PROHIBITED_TOOLS forbids.

    This is Section 6.2 Layer 6 applied to the bundle files. A prohibited
    operation must not be advertised under its registered name, under its bare
    (prefix-stripped) name, or under a recorded legacy alias — the marketplace
    must not advertise an operation that cannot and must not run.
    """
    from runtime.tool_policy import PROHIBITED_TOOLS

    forbidden = set(PROHIBITED_TOOLS) | {_bare(p) for p in PROHIBITED_TOOLS}

    offenders: list[str] = []
    for bundle in _load_all_bundles():
        slug = bundle.get("slug", "<unknown>")
        for field, name in _advertised_names(bundle):
            if name in forbidden:
                offenders.append(f"  {slug}.{field}: '{name}' is a prohibited operation")
            elif name in _PROHIBITED_ALIASES:
                target = _PROHIBITED_ALIASES[name]
                offenders.append(
                    f"  {slug}.{field}: '{name}' is the legacy alias of prohibited '{target}'"
                )

    assert not offenders, (
        "skill bundles advertise permanently prohibited operations:\n"
        + "\n".join(offenders)
    )


# Hosting-provider delivery markers. vercel_deploy/netlify_deploy were deleted
# (E4L deploys to Azure only) and no replacement exists, so NO bundle may claim
# a hosting deploy, a deployment URL, or a rollback in any published field.
# Substring markers are deliberately specific: a bare "platform" would match
# genesis-content's legitimate subscribe_video_platform.
_HOSTING_DEPLOY_SUBSTRINGS = (
    "netlify", "vercel", "railway", "deploy_to_", "cloudflare_pages",
    "rollback_deployment", "verify_deployment", "configure_cdn",
)
_HOSTING_DEPLOY_EXACT = frozenset({
    "vercel_deploy", "netlify_deploy", "deployment_url", "deployment_id",
    "platform", "verification_status", "rollback_status",
})


def test_no_bundle_claims_a_hosting_provider_deployment():
    """No bundle may advertise a hosting deploy it cannot perform.

    There is no Vercel/Netlify/Railway/CDN deploy tool in the registry and none
    is planned in this repo, so any bundle naming one is advertising a
    capability the runtime cannot deliver. Deleting the tools without deleting
    the advertisement is the exact defect this guards.
    """
    offenders: list[str] = []
    for bundle in _load_all_bundles():
        slug = bundle.get("slug", "<unknown>")
        for field, name in _advertised_names(bundle):
            low = name.lower()
            if low in _HOSTING_DEPLOY_EXACT:
                offenders.append(f"  {slug}.{field}: '{name}' claims a hosting deployment")
                continue
            for marker in _HOSTING_DEPLOY_SUBSTRINGS:
                if marker in low:
                    offenders.append(
                        f"  {slug}.{field}: '{name}' claims a hosting deployment (marker '{marker}')"
                    )
                    break

    assert not offenders, (
        "skill bundles claim hosting-provider deployments that no registered "
        "tool can perform:\n" + "\n".join(offenders)
    )


def test_tool_methods_from_source_entries_are_wellformed():
    """Every provenance entry must carry a real name and a real docstring.

    The referenced source_file agents are not in this repo, so this field
    cannot be validated against its stated source. Structural validity is the
    floor: an unnamed or undocumented entry is unreviewable, and an
    unreviewable capability claim is how the prohibited operations above went
    unnoticed.
    """
    malformed: list[str] = []
    for bundle in _load_all_bundles():
        slug = bundle.get("slug", "<unknown>")
        for i, entry in enumerate(bundle.get("tool_methods_from_source") or []):
            if not isinstance(entry, dict):
                malformed.append(f"  {slug}[{i}]: not an object")
                continue
            if not (entry.get("name") or "").strip():
                malformed.append(f"  {slug}[{i}]: empty name")
            if not (entry.get("docstring") or "").strip():
                malformed.append(f"  {slug}[{i}]: '{entry.get('name')}' has no docstring")

    assert not malformed, "malformed tool_methods_from_source entries:\n" + "\n".join(malformed)
