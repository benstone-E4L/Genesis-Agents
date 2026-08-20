"""Cato-facing specialist router. User never names an agent.

Reads accounting/CATO_GENESIS_ROUTING_MATRIX.yaml. Returns real guarded
Genesis slugs (one agent, one job). Never selects stub money slugs,
unguarded personas, A2X, company-named agents, or the rejected one-hat slug.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from accounting.loader import ACCOUNTING_DIR, _read_yaml
from accounting.specialists import (
    ENTITY_KEYS,
    MONEY_STUB_SLUGS,
    ONE_HAT_SLUG,
    SPECIALIST_SLUGS,
)

FORBIDDEN_SLUGS = MONEY_STUB_SLUGS | {
    "a2x",
    "a2x-specialist",
    ONE_HAT_SLUG,
    *ENTITY_KEYS,  # company packs are not agents
}


@dataclass(frozen=True)
class RouteDecision:
    agents: tuple[str, ...]
    then_fanout: tuple[str, ...]
    entities: tuple[str, ...]
    scenario_id: str | None = None
    escalate_to: str | None = None
    announce: str | None = None
    note: str | None = None
    write: str | None = None
    forbidden: tuple[str, ...] = ()
    answer_constraint: str | None = None
    ben_only_examples: tuple[str, ...] = ()
    do_not_invoke: tuple[str, ...] = ()
    matrix_route: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


@lru_cache(maxsize=1)
def load_routing_matrix() -> dict[str, Any]:
    data = _read_yaml(ACCOUNTING_DIR / "CATO_GENESIS_ROUTING_MATRIX.yaml")
    if not isinstance(data, dict):
        raise ValueError("routing matrix is not a mapping")
    if data.get("dispatch_contract", {}).get("pattern") != "one_agent_one_job":
        raise ValueError("routing matrix must dispatch one_agent_one_job specialists")
    if ONE_HAT_SLUG in str(data.get("dispatch_contract") or {}):
        raise ValueError("rejected one-hat slug still in dispatch_contract")
    return data


def _hint_score(prompt_n: str, route: dict[str, Any]) -> int:
    score = 0
    rid = str(route.get("id") or "")
    for ex in (_norm(x) for x in (route.get("examples") or [])):
        if prompt_n == ex:
            return 10_000
        if ex and ex in prompt_n:
            score += 400
        score += len(set(ex.split()) & set(prompt_n.split())) * 3
    hints: dict[str, tuple[str, ...]] = {
        "S1": ("stripe cash", "cash not match", "not match xero", "stripe"),
        "S2": ("close july", "all e4l entities", "close for all"),
        "S3": ("trusting the p&l", "trust the p&l", "preventing us from trusting"),
        "S4": ("contribution margin",),
        "S5": ("intercompany", "don't agree", "do not agree"),
        "S6": ("shopify/a2x/stripe", "reconcile shopify", "a2x", "shopify"),
        "S7": ("financeos complete", "complete itself today", "can financeos"),
        "S8": ("requires ben", "what specifically requires"),
        "S9": ("material accounting anomaly", "anomalies across all", "all xero organization"),
        "S10": ("journal entries", "do not post", "don't post them", "finish close"),
    }
    for hint in hints.get(rid, ()):
        if hint in prompt_n:
            score += 80 if len(hint) > 12 else 35
    if rid == "S10" and "journal" in prompt_n:
        score += 60
    if rid == "S2" and "journal" in prompt_n:
        score -= 40
    if rid == "S6" and ("shopify" in prompt_n or "a2x" in prompt_n):
        score += 50
    if rid == "S1" and "shopify" in prompt_n:
        score -= 30
    if rid == "S1" and "stripe" in prompt_n and "cash" in prompt_n:
        score += 40
    return score


def _entities_for(route: dict[str, Any], prompt_n: str, matrix: dict[str, Any]) -> tuple[str, ...]:
    defaults = tuple(route.get("entities_default") or ())
    if defaults:
        return defaults
    found: list[str] = []
    mapping = {
        "nesllc": ("stripe", "shopify", "portal"),
        "massey": ("kraken", "interactive brokers", "interactive broker"),
        "xpo": ("hsbc", "uk vat", "vat"),
        "nespty": ("aud", "gst", "june year"),
        "ibe": ("donation", "ibe"),
        "energy4life": ("intangible", "gem ip"),
    }
    for entity, tokens in mapping.items():
        if any(tok in prompt_n for tok in tokens) and entity not in found:
            found.append(entity)
    return tuple(found) if found else defaults


def _assert_agents(agents: tuple[str, ...], label: str) -> None:
    if not agents:
        raise ValueError(f"{label} is empty")
    bad = [a for a in agents if a not in SPECIALIST_SLUGS]
    if bad:
        raise ValueError(f"{label} contains non-specialists: {bad}")
    banned = [a for a in agents if a in FORBIDDEN_SLUGS]
    if banned:
        raise ValueError(f"{label} contains forbidden slugs: {banned}")


def route_question(prompt: str) -> RouteDecision:
    """Select real specialist slugs for a user question. Never asks the user to name them."""
    matrix = load_routing_matrix()
    prompt_n = _norm(prompt)
    routes: list[dict[str, Any]] = list(matrix.get("intent_routes") or [])
    if not routes:
        raise ValueError("routing matrix has no intent_routes")

    best: dict[str, Any] | None = None
    best_score = -1
    for route in routes:
        score = _hint_score(prompt_n, route)
        if score > best_score:
            best = route
            best_score = score
    if best is None:
        raise ValueError("no intent route matched")

    agents = tuple(best.get("agents") or ())
    fanout = tuple(best.get("then_fanout") or ())
    _assert_agents(agents, f"route {best.get('id')} agents")
    if fanout:
        _assert_agents(fanout, f"route {best.get('id')} then_fanout")
    escalate = best.get("escalate_to")
    if escalate:
        _assert_agents((escalate,), "escalate_to")

    entities = _entities_for(best, prompt_n, matrix)
    if not entities and set(agents) & {
        "genesis-e4l-close",
        "genesis-e4l-intercompany",
        "genesis-e4l-fs-integrity",
    }:
        entities = ENTITY_KEYS

    return RouteDecision(
        agents=agents,
        then_fanout=fanout,
        entities=entities,
        scenario_id=str(best.get("id") or "") or None,
        escalate_to=escalate,
        announce=best.get("announce"),
        note=best.get("note"),
        write=best.get("write"),
        forbidden=tuple(best.get("forbidden") or ()),
        answer_constraint=best.get("answer_constraint"),
        ben_only_examples=tuple(best.get("ben_only_examples") or ()),
        do_not_invoke=tuple(best.get("do_not_invoke") or ()),
        matrix_route=dict(best),
    )
