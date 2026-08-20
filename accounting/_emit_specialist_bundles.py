"""Emit 14 guarded skill bundles from accounting/contracts. One agent, one job."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from accounting.specialists import CONTRACT_TO_SLUG, DISPLAY_NAMES, ONE_HAT_SLUG

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "accounting" / "contracts"
BUNDLES = ROOT / "skill_bundles"
WALLET = "2f885d8c-b8e3-43ee-8430-c7f575cf850b"

SHARED = """Cato is the only boss. You are one specialist with one job on the existing Genesis gateway. You are not a hat, not a persona-only slug, and not genesis-finance / genesis-billing / genesis-commerce / genesis-pricing (those tools stub success and are denied).

XERO PATH: host Xero MCP (user-e4l-xero-read / user-e4l-xero-write). Genesis has no Xero client. Do not invent a third write path.

READ vs PROPOSE vs WRITE:
- READ: allowed via host MCP. Entity YAML is identity/CoA, not live TB.
- PROPOSE: draft journal/bill/invoice payloads as artefacts. Not posted.
- WRITE: denied. Never confirm=True on live orgs. Never entity!=demo for tool exercise. Never send email. Never pay.

EVIDENCE: every material claim needs source_system, xero_org_key, account, amount, currency, period, evidence, confidence (VERIFIED|STRONGLY_INFERRED|POSSIBLE|INSUFFICIENT_EVIDENCE). TB balancing itself, HTTP 200, and stub {{ok:true}} are not proof.

DISAGREEMENT: never silently pick a side. Record both sides. Cato may call genesis-e4l-controller. GAAP is human Controller/CPA.

OUTPUT: JSON matching accounting/CROSS_AGENT_HANDOFF_CONTRACT.yaml. write_attempted=false. No final close statement.
"""


def _list(val) -> str:
    if not val:
        return "(none)"
    if isinstance(val, str):
        return val.strip()
    return "\n".join(f"- {item}" for item in val)


def build_prompt(slug: str, name: str, contract: dict) -> str:
    return (
        f"You are {slug} ({name}). {contract.get('mission', '').strip()}\n\n"
        f"{SHARED}\n"
        f"WRITE PERMISSIONS: {contract.get('write_permissions')}\n\n"
        f"SCOPE:\n{_list(contract.get('scope'))}\n\n"
        f"NON-SCOPE:\n{_list(contract.get('non_scope'))}\n\n"
        f"PRIMARY ENTITIES (still load packs only when Cato names them):\n{_list(contract.get('entities_primary'))}\n\n"
        f"ACCOUNTING KNOWLEDGE (Phase 1, not current cash):\n{_list(contract.get('accounting_knowledge'))}\n\n"
        f"PROCEDURES:\n{_list(contract.get('procedures'))}\n\n"
        f"RECONCILIATION TESTS:\n{_list(contract.get('reconciliation_tests'))}\n\n"
        f"ESCALATION:\n{_list(contract.get('escalation_rules'))}\n\n"
        f"FORBIDDEN TOOLS:\n{_list((contract.get('tools') or {}).get('forbidden'))}\n\n"
        "Host MCP read tools you may REQUEST Cato to run (you do not own a Genesis Xero client):\n"
        f"{_list((contract.get('tools') or {}).get('read'))}\n"
    )


def main() -> None:
    hat = BUNDLES / f"{ONE_HAT_SLUG}.json"
    if hat.exists():
        hat.unlink()
        print(f"deleted {hat.name}")

    for contract_id, slug in CONTRACT_TO_SLUG.items():
        path = CONTRACTS / f"{contract_id}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["bundle"] = slug
        # Keep Phase 1 contract id; point escalation at the real controller agent.
        rules = []
        for rule in data.get("escalation_rules") or []:
            text = str(rule).replace("e4l-controller-review", "genesis-e4l-controller")
            text = text.replace("e4l-stripe-merchant", "genesis-e4l-stripe")
            rules.append(text)
        data["escalation_rules"] = rules
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")

        name = DISPLAY_NAMES[slug]
        bundle = {
            "slug": slug,
            "wallet_id": WALLET,
            "name": name,
            "version": "1.0.0",
            "source_file": f"accounting/contracts/{contract_id}.yaml",
            "system_prompt": build_prompt(slug, name, data),
            "tools_advertised": ["file_write"],
            "tool_methods_from_source": [
                {
                    "name": "file_write",
                    "docstring": "Save a proposed artefact. Not a Xero post.",
                }
            ],
            "output_shape_hint": [
                "agent_slug",
                "entities_touched",
                "findings",
                "open_items",
                "disagreements",
                "proposed_journals",
                "write_attempted",
                "blocked",
                "next_agents_recommended",
            ],
            "model_hint": "anthropic/claude-sonnet-4-5",
            "token_budget": 8000 if slug != "genesis-e4l-journals" else 10000,
            "price_tier_default_cents": 7500,
            "absorbed_from": [],
            "is_orchestrator": False,
            "job_mode": "sync",
            "runtime_level": "skill_bundle",
            "tools_verified": True,
            "artifact_support": True,
            "browser_required": False,
            "success_criteria": [{"type": "non_empty"}],
            "e4l_contract_id": contract_id,
        }
        out = BUNDLES / f"{slug}.json"
        out.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
