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
    """genesis-deploy must advertise github_tool, vercel_deploy, netlify_deploy, all registered."""
    bundle = _load_bundle("genesis-deploy")
    advertised = bundle.get("tools_advertised", [])

    required = {"github_tool", "vercel_deploy", "netlify_deploy"}
    missing_from_bundle = required - set(advertised)
    assert not missing_from_bundle, (
        f"genesis-deploy bundle is missing expected tools: {missing_from_bundle}"
    )

    unregistered = [t for t in advertised if get_tool(t) is None]
    assert not unregistered, (
        f"genesis-deploy advertises tools not in registry: {unregistered}"
    )

    # Confirm each of the three critical deploy tools resolves to a callable
    for name in required:
        fn = get_tool(name)
        assert callable(fn), f"get_tool('{name}') returned non-callable: {fn!r}"


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
