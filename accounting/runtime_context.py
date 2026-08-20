"""Inject entity packs into an E4L specialist run. Cato already picked the agent."""
from __future__ import annotations

from typing import Any

from accounting.loader import ACCOUNTING_DIR, dump_yaml, load_contract_for_slug, load_entity
from accounting.specialists import ENTITY_KEYS, is_e4l_specialist

_HARD_RULES = """
HARD RULES (non-negotiable):
- Cato is the only orchestrator and the only final-answer authority.
- You are one specialist with one job. You are not a hat on a shared slug.
- You are not genesis-finance / billing / commerce / pricing. Those stubs lie.
- Host Xero MCP is the Xero path. Genesis has no Xero client. No third write path.
- READ allowed via host MCP. PROPOSE draft payloads as artefacts. WRITE denied.
- Never confirm=True except entity=demo (and only a human does that). Never send email. Never pay.
- Never manufacture balancing journals. Never silently resolve disagreement.
- Entity YAML is identity/CoA map, not live TB. Re-query Xero before RECONCILED/VERIFIED.
- Confidence: VERIFIED | STRONGLY_INFERRED | POSSIBLE | INSUFFICIENT_EVIDENCE.
- Return CROSS_AGENT_HANDOFF_CONTRACT JSON. write_attempted must stay false.
""".strip()


def _entity_keys_from_params(params: dict[str, Any]) -> tuple[str, ...]:
    raw = params.get("entity_keys") or params.get("entities") or []
    if isinstance(raw, str):
        raw = [raw]
    keys = tuple(str(k).strip() for k in raw if str(k).strip())
    unknown = [k for k in keys if k not in ENTITY_KEYS and k != "demo"]
    if unknown:
        raise ValueError(f"unknown entity_keys (company packs only): {unknown}")
    return tuple(k for k in keys if k in ENTITY_KEYS)


def build_specialized_prompt(base_prompt: str, slug: str, params: dict[str, Any], task: str) -> str:
    if not is_e4l_specialist(slug):
        return base_prompt
    contract = load_contract_for_slug(slug)
    keys = _entity_keys_from_params(params)
    entities = [load_entity(k) for k in keys]
    period_from = params.get("period_from")
    period_to = params.get("period_to")
    question = params.get("question") or task
    parts = [
        base_prompt.rstrip(),
        "",
        _HARD_RULES,
        "",
        f"ACTIVE AGENT: {slug}",
        f"WRITE PERMISSIONS: {contract.get('write_permissions')}",
        f"ENTITIES NAMED BY CATO: {list(keys) if keys else '(none — do not guess live balances)'}",
        f"PERIOD: {period_from} .. {period_to}",
        f"QUESTION: {question}",
        "",
        "THIS AGENT'S CONTRACT (source of scope/non-scope):",
        dump_yaml(contract),
        "",
        "ENTITY PACKS (context, not agents, not live TB):",
        dump_yaml(entities) if entities else "(Cato named no entity_keys)",
        "",
        "EVIDENCE CONTRACT:",
        (ACCOUNTING_DIR / "ACCOUNTING_EVIDENCE_CONTRACT.yaml").read_text(encoding="utf-8"),
        "",
        "HANDOFF OUTPUT SHAPE:",
        (ACCOUNTING_DIR / "CROSS_AGENT_HANDOFF_CONTRACT.yaml").read_text(encoding="utf-8"),
        "",
        "DISAGREEMENT RULES:",
        (ACCOUNTING_DIR / "SPECIALIST_DISAGREEMENT_RESOLUTION.yaml").read_text(encoding="utf-8"),
    ]
    demo = "demo" in str(params.get("entity_keys") or params.get("entities") or "").lower()
    if slug == "genesis-e4l-journals":
        parts += [
            "",
            "JOURNALS GATE: propose only. entity=demo is the only tool-exercise org. "
            f"demo_named={demo}. write_attempted=false.",
        ]
    return "\n".join(parts)


def enrich_bundle(bundle: dict[str, Any], params: dict[str, Any], task: str) -> dict[str, Any]:
    slug = str(bundle.get("slug") or "")
    out = dict(bundle)
    out["system_prompt"] = build_specialized_prompt(
        str(bundle.get("system_prompt") or ""), slug, params, task
    )
    return out


def enrich_system_prompt(system_prompt: str, slug: str, params: dict[str, Any], task: str) -> str:
    return build_specialized_prompt(system_prompt, slug, params, task)
