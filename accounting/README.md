# E4L Accounting Topology (Genesis)

Cato is the only boss. Genesis is a real accounting department: **one agent, one job**.

There are **14 guarded specialists** on the existing Genesis gateway. Not 15 servers. Not hats. Not `genesis-e4l-accounting`.

**Live Xero is authoritative.** Start here:

1. `XERO_ORGANIZATION_MAP.yaml` — 6 live orgs + demo sandbox, pulled 2026-08-19
2. `E4L_ACCOUNTING_TOPOLOGY.yaml` — systems, flows, read/propose/write paths
3. `CATO_GENESIS_ROUTING_MATRIX.yaml` — how Cato picks specialist slugs
4. `contracts/` — one YAML spec per agent
5. `entities/` — 6 company context packs (not agents)
6. `SOURCE_EVIDENCE.md` — every query and file used

Do not call `genesis-finance` / `genesis-billing` / `genesis-commerce` / `genesis-pricing` from Cato. Those slugs are immutably denied because their tools are stubs.
