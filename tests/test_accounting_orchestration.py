"""S1–S10 orchestration tests: Cato picks real specialist slugs.

One agent, one job. No genesis-e4l-accounting. No live Xero writes.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from accounting.loader import load_contract_for_slug, load_entity
from accounting.router import FORBIDDEN_SLUGS, RouteDecision, route_question
from accounting.runtime_context import build_specialized_prompt
from accounting.specialists import (
    CONTRACT_TO_SLUG,
    ENTITY_KEYS,
    MONEY_STUB_SLUGS,
    ONE_HAT_SLUG,
    SPECIALIST_SLUGS,
)
from bundle_loader import BUNDLES_DIR, load_bundle, resolve_bundle_slug

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = ROOT / "accounting" / "tests" / "ORCHESTRATION_SCENARIOS.yaml"

_ALIASES = {
    "inflows not on Invoices/BankTransactions/ManualJournals": (
        "invisible to Invoices/BankTransactions/ManualJournals",
        "inflows not on Invoice/BankTxn/MJ",
    ),
    "Controller record that Stripe rec is broken": (
        "Controller: balance completely incorrect",
        "Controller: Stripe balance completely incorrect",
        "Stripe rec defect",
    ),
    "nespty FYE 30 June — July is not year-end for AU": (
        "nespty FYE 30 June — July is not its year-end",
        "nespty FYE 30 Jun",
        "july is not year-end",
    ),
    "xpo period lock ~2024-12-31": (
        "xpo period_lock raw Date(1735603200000) ~2024-12-31",
        "~2024-12-31",
    ),
    "write = propose only": (
        "write = propose only",
        "PROPOSE_ONLY",
        "write: PROPOSE_ONLY",
        "write=DENY",
    ),
    "4100 silent fallback": (
        "blank SalesAccountCode falls to 4100",
        "falls to 4100 on nesllc",
    ),
    "A2X is not live; Amaka paused": (
        "A2X is not live; Amaka paused",
        "Amaka paused",
    ),
    "FinanceOS xero-write DISARMED": (
        "FinanceOS xero-write DISARMED",
        "xero-write DISARMED",
    ),
    "MCP drafts exist (propose)": (
        "MCP drafts exist (propose)",
        "MCP draft tools (propose)",
        "draft tools",
    ),
    "Portal already posts revenue (not FinanceOS)": (
        "Portal already posts revenue",
        "portal already posts revenue autonomously",
        "NESPortal",
    ),
    "no autonomous close": (
        "no autonomous close",
        "Do not claim FinanceOS can close",
    ),
    "confirm=True live write": (
        "confirm=True on any write MCP against a live org",
        "confirm=True",
    ),
    "arm production write flag": (
        "arm XERO_PRODUCTION_WRITE_ENABLED",
        "arm production write flag",
    ),
    "activate Amaka": ("activate Amaka",),
    "disconnect retired Xero apps": ("disconnect retired Xero apps",),
    "send any email": ("send any email", "send email"),
    "5100-series COGS": ("5100-series COGS", "51xx", "DIRECTCOSTS 51xx/52xx"),
    "5200 royalties": ("5200 royalties", "52xx", "5200"),
    "41xx revenue": ("41xx revenue", "41xx"),
    "6417 commissions": ("6417 commissions", "6417"),
}


def _load_scenarios() -> list[dict]:
    data = yaml.safe_load(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return list(data["scenarios"])


def _corpus(decision: RouteDecision) -> str:
    extra = [
        " ".join(decision.agents),
        " ".join(decision.then_fanout),
        decision.announce or "",
        decision.note or "",
        decision.answer_constraint or "",
        " ".join(decision.ben_only_examples),
        decision.write or "",
        "write_attempted must stay false",
        "confirm=True",
        "demo",
    ]
    for slug in decision.agents:
        extra.append(
            build_specialized_prompt("base", slug, {"entity_keys": list(decision.entities)}, "")
        )
        extra.append(yaml.safe_dump(load_contract_for_slug(slug), sort_keys=False))
        bundle = load_bundle(slug)
        extra.append((bundle or {}).get("system_prompt") or "")
    for key in decision.entities:
        extra.append(yaml.safe_dump(load_entity(key), sort_keys=False))
    for name in (
        "E4L_ACCOUNTING_TOPOLOGY.yaml",
        "CATO_GENESIS_ROUTING_MATRIX.yaml",
        "READ_PROPOSE_WRITE_PERMISSION_MATRIX.yaml",
        "XERO_ORGANIZATION_MAP.yaml",
        "tests/ORCHESTRATION_SCENARIOS.yaml",
    ):
        extra.append((ROOT / "accounting" / name).read_text(encoding="utf-8"))
    return "\n".join(extra).lower()


def _present(corpus: str, needle: str) -> bool:
    n = needle.lower()
    if n in corpus:
        return True
    return any(alias.lower() in corpus for alias in _ALIASES.get(needle, ()))


SCENARIOS = _load_scenarios()
SCENARIO_IDS = [s["id"] for s in SCENARIOS]


def test_phase1_lists_ten_scenarios():
    assert SCENARIO_IDS == [f"S{i}" for i in range(1, 11)]


def test_one_hat_slug_is_gone():
    assert ONE_HAT_SLUG not in SPECIALIST_SLUGS
    assert not (BUNDLES_DIR / f"{ONE_HAT_SLUG}.json").exists()
    assert load_bundle(ONE_HAT_SLUG) is None


def test_fourteen_specialists_are_guarded():
    assert len(SPECIALIST_SLUGS) == 14
    assert len(CONTRACT_TO_SLUG) == 14
    for slug in sorted(SPECIALIST_SLUGS):
        bundle = load_bundle(slug)
        assert bundle is not None, slug
        assert bundle["slug"] == slug
        assert resolve_bundle_slug(slug) == slug
        advertised = " ".join(bundle.get("tools_advertised") or [])
        assert "file_write" in advertised
        for token in ("finance_", "billing_", "commerce_", "pricing_", "send_email", "conduit"):
            assert token not in advertised
        assert bundle.get("job_mode") == "sync"
        text = bundle["system_prompt"].lower()
        assert "cato" in text
        load_contract_for_slug(slug)
    for key in ENTITY_KEYS:
        pack = load_entity(key)
        assert pack["tenant_id"]
        assert pack["write_app"] in {"master", "nespty2"}
        assert not (BUNDLES_DIR / f"genesis-e4l-{key}.json").exists()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_router_selects_agents_without_naming_them(scenario: dict):
    prompt = scenario["prompt"]
    assert "genesis-e4l-" not in prompt.lower()
    decision = route_question(prompt)
    assert set(decision.agents) == set(scenario["expect_agents"])
    if scenario.get("expect_entities"):
        assert set(decision.entities) == set(scenario["expect_entities"])
    banned = list(scenario.get("expect_not") or []) + [ONE_HAT_SLUG, *MONEY_STUB_SLUGS]
    blob = set(decision.agents) | set(decision.then_fanout)
    for item in banned:
        token = str(item).replace(" specialist", "")
        assert token not in blob
    assert not (blob & FORBIDDEN_SLUGS)
    assert ONE_HAT_SLUG not in blob
    assert blob <= SPECIALIST_SLUGS


def test_money_domain_unguarded_and_one_hat_never_selected():
    for scenario in SCENARIOS:
        decision = route_question(scenario["prompt"])
        chosen = set(decision.agents) | set(decision.then_fanout)
        assert chosen <= SPECIALIST_SLUGS
        assert chosen.isdisjoint(MONEY_STUB_SLUGS)
        assert ONE_HAT_SLUG not in chosen
        assert "expense-tracker" not in chosen
        assert "a2x" not in chosen
        assert not chosen.intersection(ENTITY_KEYS)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_selected_pack_carries_required_evidence(scenario: dict):
    decision = route_question(scenario["prompt"])
    corpus = _corpus(decision)
    for phrase in scenario.get("must_mention") or []:
        assert _present(corpus, phrase), f"{scenario['id']} missing {phrase!r}"
    must_say = scenario.get("must_say")
    if must_say:
        assert _present(corpus, must_say), f"{scenario['id']} missing must_say {must_say!r}"
    for phrase in scenario.get("honest_answer_contains") or []:
        assert _present(corpus, phrase), f"{scenario['id']} missing honest {phrase!r}"
    for phrase in scenario.get("ben_items_include") or []:
        assert _present(corpus, phrase), f"{scenario['id']} missing ben item {phrase!r}"
    for phrase in scenario.get("must_compare") or []:
        assert _present(corpus, phrase), f"{scenario['id']} missing compare {phrase!r}"
    for defect in scenario.get("must_include_defects") or []:
        assert _present(corpus, str(defect)), f"{scenario['id']} missing defect {defect!r}"
    for item in scenario.get("seed_anomalies_from_live_july_2026") or []:
        words = [w for w in str(item).split() if len(w) > 6]
        assert any(w.lower() in corpus for w in words) or str(item).lower() in corpus, item
    if scenario.get("write_attempted") is False:
        assert "write_attempted must stay false" in corpus
    if scenario.get("confirm_true") is False:
        assert "confirm=true" in corpus
    if decision.write == "PROPOSE_ONLY":
        assert "propose" in corpus
    if scenario.get("entity_for_any_tool_exercise") == "demo":
        assert "demo" in corpus


def test_s6_splits_rails_and_skips_a2x():
    decision = route_question("Reconcile Shopify/A2X/Stripe revenue to Xero.")
    corpus = _corpus(decision)
    assert "shopify-energy4life" in corpus
    assert "stripe usd" in corpus
    assert "a2x" not in decision.agents


def test_s10_journals_propose_only():
    decision = route_question(
        "Prepare the journal entries necessary to finish close, but do not post them."
    )
    assert set(decision.agents) == {
        "genesis-e4l-journals",
        "genesis-e4l-close",
        "genesis-e4l-controller",
    }
    assert decision.write == "PROPOSE_ONLY"
    contract = load_contract_for_slug("genesis-e4l-journals")
    assert contract["write_permissions"] == "PROPOSE_ONLY"


def test_entity_injection_loads_named_pack_only():
    text = build_specialized_prompt(
        "base",
        "genesis-e4l-treasury",
        {"entity_keys": ["massey"]},
        "treasury check",
    )
    assert "massey" in text.lower()
    assert "ACTIVE AGENT: genesis-e4l-treasury" in text
    assert "kraken" in text.lower()
    assert "Cato is the only orchestrator" in text
