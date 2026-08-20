# SOURCE_EVIDENCE — Phase 1 accounting topology
# Nothing in the topology is “remembered” without a row here.

Session: 2026-08-19 (America/Los_Angeles afternoon; live pull UTC 23:09)
Agent: E4L-Financial-Systems-Architect
Xero mutations: none

## Standing / boot

| When (UTC) | What | Result |
|---|---|---|
| 23:05 | Read STANDING_ORDERS.md | Followed: no email, no unbuilt-ready claims |
| 23:05 | `python -m projectctl portfolio` from E4L-Project-Control-Plane | e4l-financeos EFFECTIVE STATUS CONFLICTED |
| 23:05 | `python -m projectctl boot e4l-financeos` | CONFLICTED; Xero req IN_PROGRESS; 0 tasks; 3 unpushed FinanceOS commits noted in portfolio notes |
| 23:05 | Read e4l-project-control-plane / e4l-controller / xero-accounting skills (headers) | Controller skill’s “local stdio DOWN” is **stale vs this Cursor session** — local stdio worked |

## Live Xero MCP (this Cursor session)

| When (UTC) | Tool | Args | Result |
|---|---|---|---|
| ~23:05 | GetMcpTools user-e4l-xero-read | — | 12 read tools + mcp_auth; server ready |
| ~23:05 | user-e4l-xero-read.list_entities | {} | 6 orgs; apps **readonlyv1/readonlyv2**; no demo; write none/partial |
| ~23:05 | user-e4l-xero-write.list_entities | {} | 7 orgs; apps **master/nespty2/demo**; write_access **full** granular scopes |
| 23:09:57 | Python import of server.py `_get_*` with XERO_MCP_MODE=write, XERO_ENV_PATH=PCP .env, GET only | energy4life, ibe, xpo, massey, nesllc, nespty | Wrote accounting/_live/{key}.json and xero_org_map_live.json |

Live pull used the **same** `xero_get` path as the MCP (Organisation, Accounts, TrackingCategories, Reports/BankSummary, Reports/TrialBalance, Invoices page1 AR/AP). No POST/PUT.

## Config / code files opened

- `C:\Users\Work\Desktop\E4L-Project-Control-Plane\.mcp.json` — e4l-xero-read / e4l-xero-write both launch FinanceOS `server.py`
- `C:\Users\Work\Desktop\E4L-Project-Control-Plane\config\tool-registry.yaml`
- `C:\Users\Work\Desktop\vault\projects\E4L-FinanceOS\app\mcp-servers\xero\server.py` APP_CLIENT_IDS, ROUTING, TENANTS, write_tool defs
- PCP `generated/E4L_FINANCE_ARCHITECTURE_REGISTRY.yaml`, `E4L_FINANCIAL_FLOW_REGISTRY.yaml`, `E4L_ENTITY_ACCOUNTING_MAP.yaml` (2026-08-19 prior pass; banks were UNKNOWN — now superseded by live map)
- Genesis `main.py` AGENT_PERSONAS (60 slugs), `bundle_loader.py`, `skill_bundles/` (24 json), `skill_bundles/genesis-finance.json`
- Genesis `docs/FINANCE-TOOL-CONTRACTS.md`
- `C:\Users\Work\Desktop\vault\projects\My Github\Cato\cato\tools\genesis.py` GENESIS_AGENTS + MONEY_DOMAIN_AGENTS
- FinanceOS Ralph README/progress: accounting-framework, entity-expansion, autonomous-operating-layer, metabase, azure-phoenix (progress empty)
- Genesis Ralph: e4l-retrieval-route progress (chunks complete); integration-architecture-residual specs

## Not obtained this session (INSUFFICIENT_EVIDENCE)

- NESPortal DB / sync code (not checked out)
- Amaka dashboard re-verification
- Stripe API (not authenticated)
- Azure Container Apps MCP live call (skill says they exist; Cursor used local stdio)
- Unified Journals endpoint (uncertified apps)
- Payments/CreditNotes for Stripe inflows
- Transaction-level Shopify-Energy4Life (empty account code)
- Cato live `cato_status` (no Cato MCP in this subagent session)

## Confidence rule used

Live Xero GET this session = VERIFIED. Prior registry + docs = cited, not current books.
