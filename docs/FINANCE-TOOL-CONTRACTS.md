# Genesis Finance-Adjacent Tool Contracts and Risk Classification

**Document ID:** t11-genesis-finance-tool-contracts
**Status:** Authoritative. This document is the contract. Implementation that disagrees with this document is wrong.
**Repo:** `C:\Users\Work\Desktop\GitHub\Genesis Agents` (baseline commit `8db9a09`)
**Scope:** every registered Genesis tool that can move money, alter a price, issue an invoice, or change a financial record, plus `escrow_client.py`.
**Consumer:** Cato orchestration runtime, running accounting operations for Energy 4 Life (UK/US, three legal entities, Xero as system of record).

---

## 0. The governing rule

> **Automation may PREPARE. A human PAYS. Automation RECORDS.**

No Genesis component may construct or transmit a payment, transfer, ACH, wire, payroll disbursement, escrow settlement, or card charge; and none may alter bank or wire instructions. This document makes that rule enforceable at the type level (a risk class that no agent can hold, plus a boot-time assertion and a frozen manifest) rather than by intention.

Where this document is uncertain, it prohibits. An over-restrictive contract is a correctable mistake. An over-permissive one is not.

---

## 1. Verified ground truth

### 1.1 Registered names (verified, not assumed)

Names were obtained by importing `tools` and calling `register_default_tools()`, then dumping `tools._TOOLS`. They were **not** inferred from function definitions. Verification command and full output are reproduced in Appendix A.

**60 tools registered** in the local environment. Six modules (`deploy_tool`, `github_tool`, `netlify_deploy_tool`, `vercel_deploy_tool`, `vision_tool`, `web_tool`) failed to import because `httpx` is absent locally; in production those register and the total is higher. None of the six is finance-adjacent, so the finance-domain name set below is complete and production-accurate.

Registered counts per money-domain module — **9 / 8 / 8 / 8**, not 12 / 11 / 11 / 11:

| Module | Registered tools | Registration site |
|---|---|---|
| `tools/finance_tool.py` | 9 | lines 486–494 |
| `tools/billing_tool.py` | 8 | lines 460–467 |
| `tools/commerce_tool.py` | 8 | lines 361–368 |
| `tools/pricing_tool.py` | 8 | lines 383–390 |
| `tools/domain_tool.py` | 7 (5 finance-adjacent) | lines 459–465 |
| `tools/workflow_tool.py` | 4 | lines 318–321 |
| `tools/data_pipeline_tool.py` | 5 (3 finance-adjacent) | lines 453–457 |
| `escrow_client.py` | 0 registered — module-level functions called directly by `main.py` and `worker.py` | n/a |

### 1.2 The policy table is not merely mismatched — it is globally inert

`runtime/tool_policy.py:29-53` keys `TOOL_RISK` on 23 category words. `agent_runtime.py:516` passes the registered function name. Verified result:

* **55 of 60 registered names have no `TOOL_RISK` entry** and fail closed to `RISK_ADMIN`.
* **18 of 23 `TOOL_RISK` keys are orphans** — they match no registered tool name.
* Only `file_write`, `code_format`, `genesis_call`, `conduit`, `workspace_shell` resolve correctly locally (plus `web_search`, `web_fetch`, `vercel_deploy`, `netlify_deploy` in production).
* `check_tool_policy('genesis-finance', 'finance_run_payroll_batch')` → `{"ok": false, "risk_class": "admin", "error": "tool_policy_denied"}`.
* `check_tool_policy('genesis-finance', 'finance_generate_finance_report')` → **also denied**.

**Consequence for this contract:** it is not only the payment path that has never executed. *No* finance-domain tool has ever executed through the dispatcher, including the read-only report tools. Any correction to the keying is a first-ever activation of ~55 tools simultaneously. Section 7 exists to prevent that.

### 1.3 The `pricing` category key is mapped to the wrong risk

`runtime/tool_policy.py:51` maps `"pricing" → RISK_READ_ONLY`, yet `pricing_purchase_dataset` spends money. **Any "fix" that maps registered names by category-word prefix would resolve `pricing_purchase_dataset` to `read_only` and make it callable by every agent, including those on `DEFAULT_ALLOWED_RISKS`.** This is the single most dangerous naive remediation and is explicitly forbidden by Section 7.

### 1.4 The core defect: indistinguishable fake success

Every write action in `finance_tool.py`, `billing_tool.py`, `commerce_tool.py`, `pricing_tool.py`, and the write half of `domain_tool.py` returns:

```json
{"ok": true, "stub": true, "action": "...", "args": {...},
 "note": "Phase 3 scaffold - Phase 9 integrates real provider"}
```

A caller that branches on `ok` cannot distinguish this from a completed payroll run. The `stub` and `note` keys require string parsing to notice, and an LLM caller reading the JSON will report success.

`tools/data_pipeline_tool.py` already does the right thing (`{"ok": false, "scaffold": true, "message": "..."}`, lines 18–69). **That is the in-repo precedent. Section 5 generalises it.**

### 1.5 Money is represented as `float` throughout

Every amount parameter in the four money modules is typed `float` (`cost_per_employee`, `amount`, `fee_amount`, `price`, `spend`, `new_price`, `setup_fee`, `monthly_cost`, `per_order_cost`, `registration_cost`, `max_price_per_domain`, `max_cost`). Currency is either absent or a defaulted string (`currency: str = "USD"` at `finance_tool.py:125`). No tool in the repo currently satisfies the money representation rule in Section 4.

---

## 2. Definitions used by every row

### 2.1 The five modes

| Mode | Meaning |
|---|---|
| `READ_ONLY` | Reads or computes. Makes no change to any external system. May not write to a system of record. |
| `PROPOSE_ONLY` | Produces an artefact (draft, file, plan, export) for human review. Transmits nothing to a system that acts on it. |
| `APPROVAL_REQUIRED` | Writes to a non-payment financial record, and only when a valid, single-use, tool-exact Cato authorization is presented, an idempotency slot is reserved, and readback evidence is obtained. |
| `WRITE_CAPABLE` | Writes autonomously with no per-call human authorization. **Zero tools in this repository hold this mode and none may be granted it.** |
| `PERMANENTLY_PROHIBITED` | Must not exist as a callable tool. Enforced by Section 6. |

### 2.2 The Cato authorization context

Any tool marked "Cato auth: REQUIRED" must refuse to execute unless the dispatcher supplies:

```json
{
  "authorization_id": "<uuid v4>",
  "issued_by": "cato",
  "issued_at": "<RFC3339 UTC>",
  "expires_at": "<RFC3339 UTC, issued_at + <= 900s>",
  "human_approver_id": "<non-empty>",
  "human_approved_at": "<RFC3339 UTC>",
  "entity_id": "<one of the three E4L legal entities>",
  "tool": "<exact registered tool name, no wildcard, no prefix>",
  "scope": {
    "amount_minor_max": 12345,
    "currency": "GBP",
    "period": "2026-07",
    "object_ids": ["..."]
  },
  "nonce": "<128-bit hex, recorded on use>",
  "signature": "<Ed25519 over the canonical JSON of all fields above>"
}
```

Validation rules, all mandatory, all fail-closed:

1. Signature verifies against Cato's published Ed25519 public key (key material by **key name only** in config; never a literal in source).
2. `expires_at > now` and `expires_at - issued_at <= 900s`.
3. `tool` equals the registered name being dispatched, byte for byte. Wildcards and prefixes are rejected.
4. `nonce` has not been seen before. Nonce consumption is atomic with idempotency-slot reservation.
5. `entity_id` matches the entity the tool arguments reference.
6. Any money argument satisfies `amount_minor <= scope.amount_minor_max` and `currency == scope.currency`.
7. Absent or invalid context → `authorization_missing` (Section 5), never a silent default.

Cato **must refuse to mint** an authorization naming any tool on the Section 6 list. The gateway must additionally reject such a request with HTTP 403 before any LLM call.

### 2.3 Idempotency

Key composition, for every non-`READ_ONLY` tool:

```
idempotency_key = sha256(canonical_json({
  "tool":            "<registered name>",
  "entity_id":       "<E4L legal entity>",
  "period":          "<YYYY-MM or null>",
  "business_keys":   { ...tool-specific natural keys, listed per row... },
  "amount_minor":    <int or null>,
  "currency":        "<ISO-4217 or null>",
  "authorization_id":"<Cato authorization_id>"
}))
```

`canonical_json` = UTF-8, keys sorted lexicographically, no insignificant whitespace, integers only for money.

A durable store keyed `(tool, idempotency_key)` holds one of three states — `IN_FLIGHT`, `SUCCEEDED`, `FAILED_TERMINAL` — plus the stored response envelope. Reservation is atomic (INSERT with a unique constraint; a constraint violation means someone else holds the slot).

Duplicate-call behaviour is **not** optional:

| Stored state | A duplicate call must return |
|---|---|
| `SUCCEEDED` | The **stored success envelope verbatim**, with `duplicate: true`. It must **not** re-execute and must **not** create a second provider object. |
| `FAILED_TERMINAL` | The stored failure envelope verbatim, with `duplicate: true`. |
| `IN_FLIGHT` | `ok: false`, `error.code = "duplicate_request"`, `error.retryable = true`, `error.retry_after_ms` set. |

Retention: 400 days minimum (covers a full financial year plus audit).

### 2.4 Risk classes

Existing constants in `runtime/tool_policy.py:17-25` are used unchanged. This contract adds exactly one:

```python
RISK_PROHIBITED = "prohibited"   # appears in no slug's allowed set; boot-time assertion enforces this
```

`RISK_PAYMENT` is retained as a constant so the mismatch fix in Section 7 can be complete, but Section 7 Phase 5 makes it permanently unreachable in this deployment.

---

## 3. Per-tool contracts

Field letters map to the task packet: (a) registered name, (b) purpose, (c) input contract, (d) output contract, (e) risk class, (f) mode, (g) Cato auth, (h) idempotency, (i) verdict.

---

### 3.1 `tools/finance_tool.py`

---

#### `finance_run_payroll_batch`

* **(a) Registered name:** `finance_run_payroll_batch` — `tools/finance_tool.py:486`, defined line 45.
* **(b) Purpose:** Executes a payroll batch, disbursing wages to a list of employees.
* **(c) Input contract:** No valid input contract exists. Current parameters — `employees: list[dict]`, `employee_count: int`, `cost_per_employee: float`, `period: str`, plus `**kwargs` — are all **rejected**. A payroll disbursement payload must never be constructible inside Genesis.
* **(d) Output contract:** The only permitted output is the fixed refusal envelope of Section 6.4. There is no success shape.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — payroll disbursement is the canonical prohibited operation; there is no argument set under which automation may construct it.
* **(g) Cato auth:** Not applicable. Cato must refuse to mint an authorization naming this tool.
* **(h) Idempotency:** Not applicable — the call never executes.
* **(i) Verdict:** **PROHIBIT.** Delete the function, its schema, and its `register_tool` line. The legitimate replacement is a `PROPOSE_ONLY` tool that writes a payroll *journal entry draft* to Xero after the payroll bureau has already paid — a different tool, a different name, and outside this row.

---

#### `finance_process_vendor_invoice`

* **(a) Registered name:** `finance_process_vendor_invoice` — `tools/finance_tool.py:487`, defined line 74. Schema description (line 359): *"Schedule a vendor invoice payment"*.
* **(b) Purpose:** Schedules payment of a vendor invoice.
* **(c) Input contract:** No valid input contract. Current parameters `invoice: dict`, `vendor: str`, `amount: float`, `category: str` are rejected. "Schedule a payment" is payment construction with a delay.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — scheduling a disbursement is constructing a disbursement.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT under this name.** The name is poisoned by its schema description and must not be reused. The legitimate successor is a separate, new tool `finance_prepare_vendor_bill_draft` — `PROPOSE_ONLY`, creates a Xero *draft* `ACCPAY` bill, never approves it, never attaches a payment, provider = Xero Accounting API `PUT /Invoices` with `Status: DRAFT`. That successor must be specified and reviewed on its own before implementation.

---

#### `finance_sync_bank_fees`

* **(a) Registered name:** `finance_sync_bank_fees` — `tools/finance_tool.py:488`, defined line 101.
* **(b) Purpose:** Records bank fees against a bank account in the general ledger and reconciles them to the bank feed.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | One of the three E4L legal entity identifiers; must equal `authorization.entity_id`. |
| `bank_account_id` | `str` | yes | Xero `BankAccountID` (UUID). Must exist and be of type `BANK`. **Read-only reference — this tool may never alter account or routing details.** |
| `fee_amount_minor` | `int` | yes | Integer, `> 0`, minor units. Floats, strings, and negatives rejected with `validation_failed`. |
| `currency` | `str` | yes | ISO-4217, uppercase, exactly 3 chars, explicit. No default. Must equal the bank account's currency. |
| `fee_date` | `str` | yes | RFC3339 date, within the open accounting period; a date in a closed period is `validation_failed`. |
| `bank_statement_line_id` | `str` | yes | The specific statement line this fee reconciles to. Absent → `validation_failed`; a fee with no statement line is an unsourced ledger entry. |
| `description` | `str` | yes | 1–255 chars. |
| `authorization` | `object` | yes | Section 2.2 context. |

Current parameters `account: str | None` and `fee_amount: float | None` are both rejected: optional money and float money are contract violations.

* **(d) Output contract:**
  * Success: Section 5.1 envelope, `result = {"journal_entry_id": "<Xero ManualJournalID>", "reconciled_statement_line_id": "<id>", "amount_minor": <int>, "currency": "<ISO>"}`, `evidence` per Section 5.4 with `readback_matches: true`.
  * Failure: Section 5.2 envelope. Codes reachable: `validation_failed`, `authorization_missing`, `policy_denied`, `provider_unconfigured`, `upstream_timeout`, `duplicate_request`, `not_implemented`.
* **(e) Risk class:** `RISK_NETWORK` plus mandatory authorization gate. Not `RISK_PAYMENT` — no money moves; the bank already took the fee.
* **(f) Mode:** `APPROVAL_REQUIRED` — it writes to the system of record and affects the P&L, but it records a movement that has already happened rather than causing one.
* **(g) Cato auth:** **REQUIRED.** Scope must pin `entity_id`, `currency`, and `amount_minor_max`.
* **(h) Idempotency:** `business_keys = {bank_account_id, bank_statement_line_id, fee_date}`, plus `amount_minor`, `currency`, `authorization_id`. Duplicate → stored envelope with `duplicate: true`; must not create a second journal entry.
* **(i) Verdict:** **QUARANTINE.** Not implementable today. Preconditions: Xero provider integration named and credentialed (key names only), readback evidence implemented, idempotency store live. Provider when implemented: **Xero Accounting API** (`ManualJournals` + `BankTransactions`), never a bank API directly.

---

#### `finance_generate_finance_report`

* **(a) Registered name:** `finance_generate_finance_report` — `tools/finance_tool.py:489`, defined line 121.
* **(b) Purpose:** Computes a P&L summary from a transaction list supplied by the caller.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `transactions` | `list[object]` | yes | Empty list permitted but must be explicit. Each element requires `date` (RFC3339), `description` (str 1–255), `amount_minor` (**int**, `>= 0`), `type` (enum `income` \| `expense` \| `transfer`), `currency` (ISO-4217). Any element failing validation fails the whole call — partial acceptance is forbidden. |
| `currency` | `str` | yes | ISO-4217, explicit, **no default**. Must equal every element's `currency`; mixed-currency input is `validation_failed`, never silently summed. |
| `period` | `str` | yes | `YYYY-MM` or `YYYY-MM-DD/YYYY-MM-DD`. The free-text label at line 125 is rejected. |
| `source` | `str` | yes | Enum `caller_supplied` \| `xero_export`. Governs the `unverified` flag in evidence. |

Rejected from the current signature: `amount: float`, `currency: str = "USD"` (defaulted), `month`, `format`, `tooling_cost`, and the `**kwargs` catch-all.

* **(d) Output contract:**
  * Success: `result.report` with `total_income_minor`, `total_expenses_minor`, `net_profit_minor` as **integers**; `profit_margin_bp` as an integer in basis points (never a rounded float percentage); `expense_breakdown` keyed by an explicit `category` field, **not** by `description.split()[0]` as at line 189 — first-word grouping is not an accounting category and must be removed.
  * `evidence` per Section 5.5 with `source: "caller_supplied"` and `unverified: true` when applicable. A report over caller-supplied data must never be presented as reconciled.
  * Failure: Section 5.2 envelope; `validation_failed` and `not_implemented` reachable.
* **(e) Risk class:** `RISK_READ_ONLY`.
* **(f) Mode:** `READ_ONLY` — pure arithmetic over inputs, no external system touched.
* **(g) Cato auth:** Not required. Recommended for audit attribution but must not be a precondition of execution.
* **(h) Idempotency:** Not applicable (no side effect). The response must nonetheless carry a `result_checksum` over the canonical report so a caller can detect drift.
* **(i) Verdict:** **IMPLEMENT**, conditional on three fixes: (1) integer minor units throughout — the current `float()` accumulation at lines 168–195 is unacceptable for financial figures; (2) explicit `category` field replacing the first-word heuristic; (3) mandatory explicit currency with mixed-currency rejection. No external provider needed; source is caller-supplied data or a Xero export handed in by Cato.

---

#### `finance_run_finance_close`

* **(a) Registered name:** `finance_run_finance_close` — `tools/finance_tool.py:490`, defined line 231.
* **(b) Purpose:** Runs a full monthly close: payroll, then vendor invoices, then bank fees, then report (stages listed at lines 255–260).
* **(c) Input contract:** No valid input contract. Current parameters `employee_count`, `cost_per_employee: float`, `vendor_amount: float`, `category`, `bank_fee: float`, `period` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — a composite inherits the strictest classification of any stage, and stage one is payroll disbursement. Composites also defeat per-operation authorization by design: one approval would cover four distinct money events.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.** Month-end close must be orchestrated by Cato as a sequence of individually authorized, individually evidenced steps — never as one Genesis tool call.

---

#### `finance_import_x402_transactions`

* **(a) Registered name:** `finance_import_x402_transactions` — `tools/finance_tool.py:491`, defined line 268.
* **(b) Purpose:** Bulk-inserts x402 micropayment ledger entries into the finance audit trail.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity; equals `authorization.entity_id`. |
| `transactions` | `list[object]` | yes | 1–1000 elements. Each requires `external_txn_id` (str, unique within the batch), `amount_minor` (int, `> 0`), `currency` (ISO-4217), `occurred_at` (RFC3339), `counterparty_id` (str), `direction` (enum `debit` \| `credit`). |
| `batch_checksum` | `str` | yes | Caller-computed sha256 over the canonical batch. The tool recomputes and rejects on mismatch — this is the caller's assertion that the batch is the one it intended. |
| `authorization` | `object` | yes | Section 2.2 context. |

The current signature accepts `transactions: list[dict] | None = None` and reports `"imported": len(txs)` (line 282) without inserting anything — a fabricated row count. That is prohibited by Section 5.4.

* **(d) Output contract:**
  * Success: `result = {"import_id", "rows_inserted": <int>, "rows_skipped_duplicate": <int>, "external_txn_ids_inserted": [...]}`. `rows_inserted` must come from the database's own reported affected-row count, never from `len(input)`.
  * `evidence` must include a post-insert readback query returning the inserted `external_txn_id` set and its checksum.
  * Failure: Section 5.2 envelope.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate.
* **(f) Mode:** `APPROVAL_REQUIRED` — it changes a financial record (the audit trail) but moves no money.
* **(g) Cato auth:** **REQUIRED.**
* **(h) Idempotency:** `business_keys = {batch_checksum}`; per-row secondary idempotency on `(entity_id, external_txn_id)` with a unique index so a partially-applied batch converges rather than duplicating. Duplicate batch → stored envelope, `duplicate: true`, `rows_inserted: 0`.
* **(i) Verdict:** **QUARANTINE.** Preconditions: a real ledger table with a unique index on `(entity_id, external_txn_id)`, and a decision on whether x402 belongs in E4L's books at all — for a health-devices group with Xero as system of record, it probably does not. Provider if implemented: the Genesis durable store (`durable_store.py`), with onward posting to Xero as a separate, separately authorized step.

---

#### The twelve `*_get_budget_metrics` / `*_get_audit_log` / `*_get_alerts` tools

These twelve share one contract. Each name below inherits every field verbatim except (b).

| (a) Registered name | Site | (b) Purpose | (i) Verdict |
|---|---|---|---|
| `finance_get_budget_metrics` | `finance_tool.py:492` | Return the finance agent's spend limit, spend to date, and remaining budget. | QUARANTINE |
| `finance_get_audit_log` | `finance_tool.py:493` | Return signed finance audit-log entries. | QUARANTINE |
| `finance_get_alerts` | `finance_tool.py:494` | Return budget/threshold alerts raised this session. | QUARANTINE |
| `billing_get_budget_metrics` | `billing_tool.py:465` | As above, billing agent. | QUARANTINE |
| `billing_get_audit_log` | `billing_tool.py:466` | Return signed AP2 audit receipts for billing operations. | QUARANTINE |
| `billing_get_alerts` | `billing_tool.py:467` | As above, billing agent. | QUARANTINE |
| `commerce_get_budget_metrics` | `commerce_tool.py:366` | As above, commerce agent. | QUARANTINE |
| `commerce_get_audit_log` | `commerce_tool.py:367` | Return signed AP2 audit receipts for commerce operations. | QUARANTINE |
| `commerce_get_alerts` | `commerce_tool.py:368` | As above, commerce agent. | QUARANTINE |
| `pricing_get_budget_metrics` | `pricing_tool.py:388` | As above, pricing agent. | QUARANTINE |
| `pricing_get_audit_log` | `pricing_tool.py:389` | Return signed AP2 audit receipts for pricing operations. | QUARANTINE |
| `pricing_get_alerts` | `pricing_tool.py:390` | As above, pricing agent. | QUARANTINE |

* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity. The current `**kwargs`-only signatures accept anything and scope to nothing. |
| `window_start` | `str` | yes | RFC3339 UTC. No implicit "current month". |
| `window_end` | `str` | yes | RFC3339 UTC, `> window_start`. |
| `limit` | `int` | no (default 100) | 1–1000. Log/alert readers only. |
| `cursor` | `str` | no | Opaque pagination cursor. Log/alert readers only. |

* **(d) Output contract:**
  * Success (`*_get_budget_metrics`): `result = {"monthly_limit_minor": <int>, "monthly_spend_minor": <int>, "remaining_budget_minor": <int>, "currency": "<ISO>", "window": {"start", "end"}}`.
  * Success (`*_get_audit_log` / `*_get_alerts`): `result = {"entries": [...], "count": <int>, "next_cursor": <str|null>}`.
  * `evidence` per Section 5.5 — mandatory `source` naming the actual store, `row_count`, `checksum`, `as_of`.
  * Failure: Section 5.2 envelope. **`provider_unconfigured` is the correct response whenever no budget/audit store is wired.**
* **(e) Risk class:** `RISK_READ_ONLY`.
* **(f) Mode:** `READ_ONLY` — reads only, no side effects.
* **(g) Cato auth:** Not required.
* **(h) Idempotency:** Not applicable. Responses must carry `result_checksum` and `as_of`.
* **(i) Verdict (all twelve):** **QUARANTINE.** These are currently the most quietly dangerous functions in the repo: they return `ok: true` with **invented constants** — `monthly_limit: 15000.00` (`finance_tool.py:296`), `1500.00` (`billing_tool.py:276`, `commerce_tool.py:202`, `pricing_tool.py:219`) — and empty `entries: []` / `alerts: []` arrays that read as "clean audit log, no alerts". An accounting runtime consuming these will conclude budget headroom exists and nothing has been flagged. Until a real store backs them, they must return `provider_unconfigured`. Provider when implemented: the Genesis durable store (`durable_store.py` / `audit.py`), not AP2.

---

### 3.2 `tools/billing_tool.py`

---

#### `billing_import_ar_ledger`

* **(a) Registered name:** `billing_import_ar_ledger` — `tools/billing_tool.py:460`, defined line 45.
* **(b) Purpose:** Imports an accounts-receivable dataset (subscriptions, open invoices) from an external provider into Genesis.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity; equals `authorization.entity_id`. |
| `provider` | `str` | yes | Enum, closed set: `xero` \| `stripe`. Free-text rejected. The current `provider or source` aliasing at line 57 is removed — one parameter, one name. |
| `period_start` / `period_end` | `str` | yes | RFC3339 dates, `end > start`, span ≤ 400 days. |
| `expected_record_count` | `int` | yes | Caller's assertion. A mismatch against the actual fetched count is `validation_failed`, not a silent success. |
| `authorization` | `object` | yes | Section 2.2 context. |

The current `price: float` parameter (line 49) is rejected outright — an import operation has no price, and its presence signals this tool was also intended to *purchase* the dataset.

* **(d) Output contract:** Success `result = {"import_id", "rows_inserted": <int from DB>, "rows_updated": <int>, "rows_skipped": <int>, "provider_cursor": <str|null>}`; `evidence` with readback row count and checksum. Failure: Section 5.2.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate.
* **(f) Mode:** `APPROVAL_REQUIRED` — writes a financial record set; no money moves.
* **(g) Cato auth:** **REQUIRED.**
* **(h) Idempotency:** `business_keys = {provider, period_start, period_end}`; per-row on `(entity_id, provider, provider_record_id)`. Duplicate → stored envelope, `duplicate: true`, no re-fetch.
* **(i) Verdict:** **QUARANTINE.** Precondition: named provider credentials by key name, a real destination table, and confirmation that Xero — not Stripe — is the authoritative AR source for E4L. Provider when implemented: **Xero Accounting API** (`Invoices`, `Contacts`), with Stripe as a reconciliation input only.

---

#### `billing_run_dunning_batch`

* **(a) Registered name:** `billing_run_dunning_batch` — `tools/billing_tool.py:461`, defined line 75. Schema description (line 341): *"Run a dunning/retry cycle on overdue invoices"*.
* **(b) Purpose:** Runs a collections cycle over overdue invoices, including retrying failed charges.
* **(c) Input contract:** No valid input contract. Current parameters `experiment_id`, `overdue_invoices`, `sequence_name`, `cloud_hours: float`, `hourly_rate: float`, `expected_recovery_pct: float` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — "retry" against an overdue invoice means re-presenting a stored payment instrument. That is constructing and transmitting a card charge. Even if a given implementation were email-only, the contract cannot distinguish the two from the outside, and Section 0 requires prohibiting the ambiguous case.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable. Note that the absence of idempotency here is itself a live hazard: a retried dunning batch would re-charge every customer in it.
* **(i) Verdict:** **PROHIBIT.** A separate, email-only tool that *sends a reminder and records that it was sent* may be specified later under a different name, with an explicit contractual guarantee that it never touches a payment instrument.

---

#### `billing_deploy_plan_change`

* **(a) Registered name:** `billing_deploy_plan_change` — `tools/billing_tool.py:462`, defined line 108.
* **(b) Purpose:** Changes a customer's billing plan, altering what they are charged going forward.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity. |
| `customer_id` | `str` | yes | Provider customer identifier; must resolve to an existing customer before the write. |
| `subscription_id` | `str` | yes | The exact subscription being changed. The current signature omits this entirely, making the target ambiguous. |
| `current_plan_id` | `str` | yes | Optimistic-concurrency guard. If the live subscription is not on this plan, fail `validation_failed` — never overwrite an unexpected state. |
| `new_plan_id` | `str` | yes | Must exist in the provider's catalogue. |
| `new_amount_minor` | `int` | yes | Integer minor units; must equal the catalogue price of `new_plan_id` (the tool must not invent a price). |
| `currency` | `str` | yes | ISO-4217, explicit; must equal the subscription's currency. |
| `effective_at` | `str` | yes | RFC3339; must be `>= now`. Retroactive plan changes are `validation_failed`. |
| `proration_behavior` | `str` | yes | Enum `none` \| `create_prorations` \| `always_invoice`. **No default** — proration silently defaulting is how surprise invoices get issued. |
| `authorization` | `object` | yes | Section 2.2 context; `scope.amount_minor_max >= new_amount_minor`. |

Rejected: `channel`, `spend: float`, `risk_level` — none of these belong on a plan-change operation, and `spend` implies the tool also purchases something.

* **(d) Output contract:** Success `result = {"subscription_id", "previous_plan_id", "new_plan_id", "new_amount_minor", "currency", "effective_at", "proration_invoice_id": <str|null>, "proration_amount_minor": <int|null>}`; `evidence` with a post-change readback of the subscription confirming plan, amount, and currency. If `proration_behavior` caused an invoice to be issued, its id and amount are mandatory in both `result` and `evidence`. Failure: Section 5.2.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate. Not `RISK_PAYMENT` — it changes a future obligation rather than transmitting a payment. But note the boundary: `always_invoice` **issues an invoice**, which is why this is `APPROVAL_REQUIRED` and not `PROPOSE_ONLY`.
* **(f) Mode:** `APPROVAL_REQUIRED` — alters a price and can issue an invoice.
* **(g) Cato auth:** **REQUIRED**, per subscription, amount-capped.
* **(h) Idempotency:** `business_keys = {customer_id, subscription_id, new_plan_id, effective_at}`, plus `new_amount_minor`, `currency`, `authorization_id`. Duplicate → stored envelope, `duplicate: true`, **and no second proration invoice**. Verified by a provider-side invoice count in the evidence readback.
* **(i) Verdict:** **QUARANTINE.** Preconditions: named provider, plan catalogue read implemented, proration semantics tested against the provider's documented behaviour (do not assume — verify in the provider's API docs), evidence readback implemented. Provider when implemented: **Stripe Billing** (`Subscriptions` update with explicit `proration_behavior`) or **Chargebee**; the choice must be made before implementation, not at call time.

---

#### `billing_generate_revops_report`

* **(a) Registered name:** `billing_generate_revops_report` — `tools/billing_tool.py:463`, defined line 138.
* **(b) Purpose:** Computes MRR, ARR, churn, and ARPA from a subscription list supplied by the caller.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `subscriptions` | `list[object]` | yes | Each element requires `plan_id` (str), `amount_minor` (**int**, `>= 0`), `currency` (ISO-4217), `interval` (enum `month` \| `year`), `status` (enum `active` \| `trial` \| `cancelled`), `start_date` (RFC3339); `end_date` optional RFC3339. |
| `currency` | `str` | yes | ISO-4217, explicit. Mixed-currency input is `validation_failed` — MRR across currencies without an FX rate and an FX date is a fabricated number. |
| `period` | `str` | yes | `YYYY-MM`. The current free-text `period: str = "current"` default is rejected. |
| `source` | `str` | yes | Enum `caller_supplied` \| `xero_export` \| `stripe_export`. |

* **(d) Output contract:** Success `result.report` with `mrr_minor`, `arr_minor`, `arpa_minor` as **integers**; `churn_rate_bp` as an integer in basis points; counts as integers; `plan_breakdown` keyed by `plan_id`. `evidence` per Section 5.5 with `unverified: true` when `source == "caller_supplied"`. Failure: Section 5.2.
* **(e) Risk class:** `RISK_READ_ONLY`.
* **(f) Mode:** `READ_ONLY`.
* **(g) Cato auth:** Not required.
* **(h) Idempotency:** Not applicable; `result_checksum` mandatory.
* **(i) Verdict:** **IMPLEMENT**, conditional on two fixes. (1) Integer minor units — the current float accumulation (lines 182–208) is unacceptable. (2) **Remove the annual-detection heuristic at lines 189–191**, which decides a subscription is annual by string-matching `"annual"`, `"yearly"`, or `"year"` inside the *plan name*. A plan called `"Enterprise Yearly Support (billed monthly)"` would be silently divided by twelve, and a plan called `"Pro-12"` would not be divided at all. Annualisation must come from an explicit `interval` field. Amortisation of annual to monthly must use `Decimal` with `ROUND_HALF_EVEN` and largest-remainder allocation so twelve months sum exactly to the annual amount. No external provider needed.

---

#### `billing_run_billing_cycle`

* **(a) Registered name:** `billing_run_billing_cycle` — `tools/billing_tool.py:464`, defined line 235. Stages listed at lines 257–262: AR import, dunning, plan change, report.
* **(b) Purpose:** Runs a full billing cycle end to end.
* **(c) Input contract:** No valid input contract. Current parameters `provider`, `dataset_price: float`, `records`, `cloud_hours: float`, `deployment_spend: float` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — it contains `billing_run_dunning_batch` (charge retries) and `billing_deploy_plan_change` (invoice issuance) behind a single call with a single approval.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.** Cato orchestrates the sequence; Genesis never bundles it.

---

### 3.3 `tools/commerce_tool.py`

---

#### `commerce_register_domain`

* **(a) Registered name:** `commerce_register_domain` — `tools/commerce_tool.py:361`, defined line 47.
* **(b) Purpose:** Purchases a domain registration from a registrar.
* **(c) Input contract:** No valid input contract. Current parameters `domain`, `owner_info: dict`, `registrar: str = "namecheap"`, `registration_cost: float` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — registering a domain charges a stored payment instrument at a registrar. That is constructing and transmitting a payment. It also transmits `owner_info` (registrant PII) to a third party without a data-processing decision.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.** Duplicated by `domain_register`; both go.

---

#### `commerce_activate_payment_gateway`

* **(a) Registered name:** `commerce_activate_payment_gateway` — `tools/commerce_tool.py:362`, defined line 76.
* **(b) Purpose:** Activates a payment gateway (Stripe or similar) using supplied account details, paying a setup fee.
* **(c) Input contract:** No valid input contract. Current parameters `provider`, `gateway`, `account_info: dict`, `setup_fee: float` are rejected. `account_info` is a bank/payout-instruction payload.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — this is the highest-severity function in the four money modules. It both pays a setup fee and **alters where money lands**, which is precisely the "alters wire/bank instructions" prohibition. A compromised or merely confused agent calling this redirects settlement.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint. No approval level, human or otherwise, makes this acceptable through an LLM tool call.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.** Payout and bank instruction changes are performed by a named human in the provider's own console, under that provider's own MFA and change-notification controls.

---

#### `commerce_configure_tax_engine`

* **(a) Registered name:** `commerce_configure_tax_engine` — `tools/commerce_tool.py:363`, defined line 103.
* **(b) Purpose:** Configures tax calculation rules for a region, determining VAT and sales tax on future invoices.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity; tax config is per-entity and must never be applied across entities. |
| `jurisdiction` | `str` | yes | ISO-3166-1 alpha-2, plus ISO-3166-2 subdivision where the jurisdiction requires it. Free-text `region` (current line 105) is rejected. |
| `tax_rules` | `list[object]` | yes | Each requires `tax_code` (str), `rate_bp` (**int**, basis points, `0`–`10000`), `effective_from` (RFC3339 date), `applies_to` (enum). Float rates are rejected — a VAT rate of 20% is `2000`, not `0.2`. |
| `provider` | `str` | yes | Closed enum. The current silent default to `"avalara"` (line 115) is rejected; a defaulted tax provider is a defaulted tax liability. |
| `authorization` | `object` | yes | Section 2.2 context, with `human_approver_id` naming the person accountable for the tax position. |

Rejected: `monthly_cost: float` — a configuration operation must not carry a price.

* **(d) Output contract:** Success `result = {"config_id", "jurisdiction", "provider", "rules_applied": <int>, "effective_from"}`; `evidence` with a readback of the live configuration and a field-by-field comparison. Failure: Section 5.2.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate.
* **(f) Mode:** `APPROVAL_REQUIRED` — it moves no money but determines the tax on every future invoice, which is a regulatory position (UK VAT, US state sales tax) with penalty exposure.
* **(g) Cato auth:** **REQUIRED**, and the approver must be the person who signs the VAT return. This is a control requirement, not a technical one.
* **(h) Idempotency:** `business_keys = {entity_id, jurisdiction, provider, effective_from, sha256(canonical(tax_rules))}`. Duplicate → stored envelope, `duplicate: true`, no re-application.
* **(i) Verdict:** **QUARANTINE.** Preconditions: named provider, an accountant-approved rule set, evidence readback. Provider when implemented: **Avalara AvaTax** or the tax module of the invoicing system already in use. Flagged: tax configuration is a professional judgement; a qualified accountant must sign off the rule set before any implementation, and this document is not tax advice.

---

#### `commerce_ship_fulfillment_batch`

* **(a) Registered name:** `commerce_ship_fulfillment_batch` — `tools/commerce_tool.py:364`, defined line 130.
* **(b) Purpose:** Dispatches a fulfilment batch through a carrier at a per-order cost.
* **(c) Input contract:** No valid input contract. Current parameters `shipments`, `carrier`, `orders`, `per_order_cost: float` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — buying shipping labels charges a carrier account. It is a payment, and it is unbounded in volume: `order_count` is whatever the caller passes.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable. Note the live hazard: the current signature accepts both `shipments` and `orders` and silently coalesces them (line 139), so the same batch passed under two keys would double-purchase.
* **(i) Verdict:** **PROHIBIT.** A `PROPOSE_ONLY` successor that generates a *manifest* for a human to purchase may be specified separately.

---

#### `commerce_launch_commerce_stack`

* **(a) Registered name:** `commerce_launch_commerce_stack` — `tools/commerce_tool.py:365`, defined line 159. Stages at lines 183–188.
* **(b) Purpose:** Runs domain registration, gateway activation, tax configuration, and fulfilment setup in one call.
* **(c) Input contract:** No valid input contract. All current parameters rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — it composes two prohibited operations, one of which alters bank instructions.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.**

---

### 3.4 `tools/pricing_tool.py`

---

#### `pricing_purchase_dataset`

* **(a) Registered name:** `pricing_purchase_dataset` — `tools/pricing_tool.py:383`, defined line 45. Schema description (line 264): *"Buy a market/competitor pricing dataset (AP2-gated + x402 micropayment)"*.
* **(b) Purpose:** Purchases a pricing dataset using an x402 micropayment.
* **(c) Input contract:** No valid input contract. Current parameters `provider`, `price: float`, `records`, `dataset_id`, `max_price_usd: float` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`. **Note:** `TOOL_RISK["pricing"]` is `RISK_READ_ONLY` (`runtime/tool_policy.py:51`). Any prefix- or category-based remediation would classify this money-spending tool as read-only and expose it to every agent slug including the `DEFAULT_ALLOWED_RISKS` fallback. Section 7 forbids prefix-based mapping for exactly this reason.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — it constructs and transmits a payment. The `max_price_usd` cap is not a mitigation: a cap on an unauthorized payment is still an unauthorized payment.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.**

---

#### `pricing_run_elasticity_experiment`

* **(a) Registered name:** `pricing_run_elasticity_experiment` — `tools/pricing_tool.py:384`, defined line 75.
* **(b) Purpose:** Runs a live price-elasticity experiment across a price range on a product, on paid cloud capacity.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity. |
| `product_id` | `str` | yes | Must resolve to an existing product. |
| `price_points_minor` | `list[int]` | yes | 2–8 elements, integer minor units, strictly increasing, each `> 0`. The current `price_range: list[float]` is rejected. |
| `currency` | `str` | yes | ISO-4217, explicit. |
| `floor_price_minor` / `ceiling_price_minor` | `int` | yes | Hard bounds; every element of `price_points_minor` must lie within them. Absent bounds is `validation_failed`. |
| `duration_days` | `int` | yes | 1–90. |
| `auto_revert_at` | `str` | yes | RFC3339, `<= start + duration_days`. An experiment with no scheduled revert is `validation_failed`. |
| `authorization` | `object` | yes | Section 2.2 context. |

Rejected: `cloud_hours: float`, `hourly_rate: float`, `expected_uplift_pct: float` — a pricing tool must not also provision or purchase compute, and `expected_uplift_pct` is an input that only ever appears in the output as an unverified echo (line 103).

* **(d) Output contract:** Success `result = {"experiment_id", "product_id", "price_points_minor", "currency", "starts_at", "auto_revert_at", "variants_created": <int>}`; `evidence` with a readback of each created price variant confirming the exact minor-unit amounts live in the commerce system. Failure: Section 5.2.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate.
* **(f) Mode:** `APPROVAL_REQUIRED` — it changes what real customers are charged. It moves no money itself.
* **(g) Cato auth:** **REQUIRED**, scoped to `product_id` and bounded by `ceiling_price_minor`.
* **(h) Idempotency:** `business_keys = {entity_id, product_id, sha256(canonical(price_points_minor)), starts_at}`. Duplicate → stored envelope, `duplicate: true`, no second experiment.
* **(i) Verdict:** **QUARANTINE.** For the E4L accounting mandate specifically there is no legitimate use — an accounting runtime does not run live pricing experiments — so this should be denied to every Cato-facing slug even after implementation. Provider if ever implemented outside the E4L context: the commerce platform's own price/variant API.

---

#### `pricing_deploy_pricing_update`

* **(a) Registered name:** `pricing_deploy_pricing_update` — `tools/pricing_tool.py:385`, defined line 110.
* **(b) Purpose:** Publishes a new price for a product to a sales channel.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity. |
| `product_id` | `str` | yes | Must resolve to an existing product. |
| `channel` | `str` | yes | Closed enum of known channels. Free-text rejected. |
| `current_price_minor` | `int` | yes | Optimistic-concurrency guard; mismatch against live price → `validation_failed`. |
| `new_price_minor` | `int` | yes | Integer minor units, `> 0`. The current `new_price: float` is rejected. |
| `currency` | `str` | yes | ISO-4217, explicit. |
| `max_change_bp` | `int` | yes | Maximum permitted change from `current_price_minor`, in basis points. A change exceeding it is `validation_failed`. This is the guard against a fat-fingered or hallucinated order-of-magnitude error. |
| `effective_at` | `str` | yes | RFC3339, `>= now`. |
| `authorization` | `object` | yes | Section 2.2 context. |

Rejected: `spend: float`, `risk_level`, `scope` — a price update neither spends nor self-assesses its own risk.

* **(d) Output contract:** Success `result = {"deploy_id", "product_id", "channel", "previous_price_minor", "new_price_minor", "currency", "effective_at"}`; `evidence` with a channel readback confirming the live price equals `new_price_minor`. Failure: Section 5.2.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate.
* **(f) Mode:** `APPROVAL_REQUIRED` — alters a price.
* **(g) Cato auth:** **REQUIRED.**
* **(h) Idempotency:** `business_keys = {entity_id, product_id, channel, new_price_minor, currency, effective_at}`. Duplicate → stored envelope, `duplicate: true`.
* **(i) Verdict:** **QUARANTINE.** Preconditions: named channel provider, price readback implemented, `max_change_bp` enforced. Provider when implemented: the commerce platform's price API — named explicitly before build, never resolved at call time from a free-text `channel` string.

---

#### `pricing_generate_pricing_report`

* **(a) Registered name:** `pricing_generate_pricing_report` — `tools/pricing_tool.py:386`, defined line 141.
* **(b) Purpose:** Nominally, produces a pricing BI report. Actually, returns hardcoded fabricated financial figures.
* **(c) Input contract:** No valid input contract under this name. Current parameters `period`, `dashboards`, `seat_cost: float`, `product_ids` are accepted but **ignored** — the output does not depend on them.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — **prohibited for fabricated financial data, not for money movement.** Lines 157–174 return, wrapped in `ok: true`:

  ```python
  "revenue_total_usd": 482500.00,
  "revenue_delta_pct": 8.4,
  "elasticity_mean": -1.32,
  "best_price_point_usd": 49.00,
  ```

  These are constants. They are not computed from any input. An accounting runtime that ingests this receives a precise, plausible, entirely invented revenue figure marked `ok: true`. In an accounting context that is worse than a payment stub, because a fake payment eventually fails to reconcile whereas a fake revenue number may be believed and reported.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT under this name.** The name and its hardcoded body must be deleted rather than repaired, so that no future reader mistakes a partially-fixed version for the original. A genuine successor may be specified separately as `READ_ONLY`, computing from caller-supplied or Xero-exported data under the Section 5.5 evidence rules. **This finding contradicts the prior audit — see Section 9, Disagreement 1.**

---

#### `pricing_run_pricing_cycle`

* **(a) Registered name:** `pricing_run_pricing_cycle` — `tools/pricing_tool.py:387`, defined line 179. Stages at lines 201–206.
* **(b) Purpose:** Runs dataset purchase, elasticity experiment, price deployment, and report generation in one call.
* **(c) Input contract:** No valid input contract. All current parameters rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — composes `pricing_purchase_dataset` (a payment) and `pricing_generate_pricing_report` (fabricated figures).
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.**

---

### 3.5 `tools/domain_tool.py` — finance-adjacent subset

**Excluded from this contract with justification:** `domain_generate_candidates` (pure string heuristics, no money, no financial record, no price) and `domain_configure_dns` (DNS records only). Neither can move money, alter a price, issue an invoice, or change a financial record. They remain subject to the general policy fix in Section 7 but need no financial contract.

---

#### `domain_check_availability`

* **(a) Registered name:** `domain_check_availability` — `tools/domain_tool.py:460`, defined near line 120.
* **(b) Purpose:** Batch-checks domain availability against Name.com.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `domains` | `list[str]` | yes | 1–50 elements, each a syntactically valid FQDN, lowercase, punycode-encoded if non-ASCII. |

Credentials come from `NAMECOM_USERNAME` / `NAMECOM_TOKEN` **by key name from the environment** and never appear in arguments or output.

* **(d) Output contract:** Success `result = {"results": [{"domain", "available": bool, "price_minor": <int|null>, "currency": "<ISO|null>"}]}`. The current code passes the provider's price through untyped; where a price is returned it must be converted to integer minor units with an explicit currency at the boundary. Failure: Section 5.2 — replacing the current `"ok": resp.status_code < 400` construction, which conflates a 3xx and a malformed body with success.
* **(e) Risk class:** `RISK_NETWORK`.
* **(f) Mode:** `READ_ONLY` — a lookup; buys nothing.
* **(g) Cato auth:** Not required.
* **(h) Idempotency:** Not applicable; `result_checksum` and `as_of` mandatory since availability is time-varying.
* **(i) Verdict:** **IMPLEMENT** (largely already real), conditional on adopting the Section 5 envelope and returning `provider_unconfigured` — not a fake success — when `NAMECOM_USERNAME` / `NAMECOM_TOKEN` are absent. Provider: **Name.com v4 `domains:checkAvailability`**.

---

#### `domain_create_intent_mandate`

* **(a) Registered name:** `domain_create_intent_mandate` — `tools/domain_tool.py:461`, defined line 184.
* **(b) Purpose:** Creates an AP2 intent mandate authorising future domain purchases up to a price and count.
* **(c) Input contract:** No valid input contract. Current parameters `user_id`, `business_name`, `business_type`, `max_domains`, `max_price_per_domain: float`, `valid_for_hours` are rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — this tool **manufactures spending authority**. It is more dangerous than a single payment: one call creates a standing mandate for `max_domains × max_price_per_domain` of future spend, valid for `valid_for_hours`. Authorization to pay must originate from a human via Cato (Section 2.2) and must never be generated by an agent.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint. Any tool whose output is itself an authorization is categorically prohibited.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.**

---

#### `domain_register`

* **(a) Registered name:** `domain_register` — `tools/domain_tool.py:462`, defined line 214.
* **(b) Purpose:** Registers a domain at a registrar, charging a payment instrument.
* **(c) Input contract:** No valid input contract. Current parameters `domain`, `buyer_consent_token`, `years`, `privacy`, `auto_renew` are rejected. Note `auto_renew: bool = True` defaults to creating a **recurring** charge.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — constructs and transmits a payment, and by default establishes a recurring one.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint. `buyer_consent_token` is not a substitute — it is an opaque string the tool never validates.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.**

---

#### `domain_select_and_register`

* **(a) Registered name:** `domain_select_and_register` — `tools/domain_tool.py:464`, defined line 265. Stages at lines 288–294.
* **(b) Purpose:** Generates candidates, checks availability, creates an intent mandate, registers a domain, and configures DNS in one call.
* **(c) Input contract:** No valid input contract. `auto_register: bool = False` is not a mitigation — it is a config flag one argument away from a purchase.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — composes mandate creation and registration, both individually prohibited, and lets an LLM choose the domain, the price, and whether to buy.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** Not applicable.
* **(i) Verdict:** **PROHIBIT.**

---

#### `domain_get_cost_summary`

* **(a) Registered name:** `domain_get_cost_summary` — `tools/domain_tool.py:465`, defined line 305.
* **(b) Purpose:** Returns total recurring domain cost, domain count, and whether a cost threshold is exceeded.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `entity_id` | `str` | yes | E4L legal entity. The current `**kwargs`-only signature scopes to nothing. |
| `as_of` | `str` | no (default: now) | RFC3339 UTC. |

* **(d) Output contract:** Success `result = {"total_monthly_cost_minor": <int>, "currency": "<ISO>", "total_domains": <int>, "threshold_exceeded": bool, "registered_domains": [...]}`; `evidence` per Section 5.5 naming the store. Failure: Section 5.2.
* **(e) Risk class:** `RISK_READ_ONLY`.
* **(f) Mode:** `READ_ONLY`.
* **(g) Cato auth:** Not required.
* **(h) Idempotency:** Not applicable; `result_checksum` and `as_of` mandatory.
* **(i) Verdict:** **QUARANTINE.** It currently returns `ok: true` with `total_monthly_cost: 0.00`, `total_domains: 0`, `threshold_exceeded: false` — a fabricated all-clear identical in shape to a genuine one. Must return `provider_unconfigured` until a real registry store exists. Provider when implemented: the Genesis durable store.

---

### 3.6 `tools/workflow_tool.py`

---

#### `workflow_webhook_trigger`

* **(a) Registered name:** `workflow_webhook_trigger` — `tools/workflow_tool.py:321`, defined line 178.
* **(b) Purpose:** POSTs an arbitrary JSON body to an arbitrary HTTPS URL.
* **(c) Input contract:** No valid input contract under any finance authorization. Current parameters `webhook_url: str` and `payload: dict` are rejected in that context.
* **(d) Output contract:** Section 6.4 refusal envelope when dispatched under a finance authorization or to a finance-domain slug.
* **(e) Risk class:** `RISK_PROHIBITED` for any slug that can hold a Cato finance authorization; `RISK_NETWORK` elsewhere.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` (finance context) — it is a general-purpose arbitrary-payload transmitter. `{"action":"pay","amount":50000,"to":"..."}` is a valid `payload`, and no static analysis can prove a given payload is not a payment instruction. The existing controls are real but insufficient here: `_is_safe_url` (lines 20–33) blocks SSRF to private ranges and the `https://` check (line 182) blocks plaintext, but **neither constrains the destination host or the body**, which is what matters for money movement.
* **(g) Cato auth:** Not applicable in finance context; Cato must refuse to mint. Outside finance context it remains `RISK_NETWORK` under normal policy.
* **(h) Idempotency:** Not applicable — and its absence is itself disqualifying: the tool has no idempotency key, so a retried call re-transmits the payload in full.
* **(i) Verdict:** **PROHIBIT for every finance-domain slug and for any dispatch carrying a Cato authorization.** Enforcement is a slug-scoped entry in `PROHIBITED_TOOLS_BY_SLUG` (Section 6.2), not a runtime string inspection of the payload. If a legitimate finance webhook need appears later, it must be a distinct tool with a hardcoded destination allowlist and a fixed body schema.

---

#### `workflow_zapier_export`, `workflow_n8n_export`, `workflow_make_export`

* **(a) Registered names:** `workflow_zapier_export` (`workflow_tool.py:318`), `workflow_n8n_export` (line 319), `workflow_make_export` (line 320).
* **(b) Purpose:** Generate importable automation-platform JSON from a natural-language description.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `description` | `str` | yes | 1–4000 chars. |
| `trigger_app` / `action_app` | `str` | no | If present, must be in a closed app allowlist. |
| `forbid_payment_steps` | `bool` | yes | Must be `true` when the caller holds any finance authorization. The generated JSON is then scanned against a payment-node denylist (payment, transfer, ACH, wire, payroll, and the node types of known payment connectors) and the call fails `validation_failed` on a hit. |

* **(d) Output contract:** Success `result = {"workflow_json": {...}, "payment_nodes_detected": [], "requires_human_import": true}`; the artefact is returned to the caller and **never transmitted to the automation platform**. Failure: Section 5.2 envelope; `validation_failed` when a payment node is generated.
* **(e) Risk class:** `RISK_READ_ONLY` — it generates text and calls nothing.
* **(f) Mode:** `PROPOSE_ONLY` — produces an artefact for a human to review and import; transmits nothing that acts.
* **(g) Cato auth:** Not required to generate. **Required to be absent-or-valid** in the sense that if a finance authorization is present, `forbid_payment_steps` must be `true`.
* **(h) Idempotency:** Not applicable (no side effect). `result_checksum` over the generated JSON is mandatory so a human reviewer can confirm the artefact they approved is the one imported.
* **(i) Verdict:** **QUARANTINE** for all three. Precondition: the payment-node denylist scan must exist before these are exposed to any finance-domain slug; today an agent can author a Zapier workflow containing a payment step and hand it to a human as a reviewed-looking artefact. No external provider needed.

---

### 3.7 `tools/data_pipeline_tool.py` — finance-adjacent subset

**Excluded with justification:** `data_pipeline_design` (produces a design document only) and `data_quality_check` (validates records in-process, writes nothing). Neither moves money, alters a price, issues an invoice, or changes a financial record.

---

#### `data_bigquery_query`

* **(a) Registered name:** `data_bigquery_query` — `tools/data_pipeline_tool.py:454`, defined line 35.
* **(b) Purpose:** Runs a SQL query against BigQuery, potentially over financial data.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `project_id` | `str` | yes | Must be in a closed allowlist of permitted GCP projects. |
| `query` | `str` | yes | Must parse as a single `SELECT` statement. `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `TRUNCATE`, and multi-statement scripts are rejected with `validation_failed`. Parameters are bound, never interpolated. |
| `query_parameters` | `list[object]` | no | Named, typed bind parameters. Any user-derived value must arrive here, never inside `query`. |
| `max_rows` | `int` | no (default 1000) | 1–100000. |

* **(d) Output contract:** Success `result = {"rows": [...], "row_count": <int>, "truncated": bool, "schema": [...]}`; `evidence` per Section 5.5 with `source: "bigquery:<project_id>"`, `query_fingerprint`, `row_count`, `checksum`, `as_of`. Failure: Section 5.2 — the current shape (`ok: false, scaffold: true, message`) is already honest and only needs the code taxonomy added.
* **(e) Risk class:** `RISK_NETWORK`.
* **(f) Mode:** `READ_ONLY` — enforced by the `SELECT`-only parse, not by convention.
* **(g) Cato auth:** Not required for reads. Required if the allowlist is ever widened to a project holding customer PII, in which case the authorization records who read what.
* **(h) Idempotency:** Not applicable; `query_fingerprint` and `as_of` mandatory.
* **(i) Verdict:** **QUARANTINE.** Not because it is unsafe in principle but because for E4L the system of record is **Xero**, not BigQuery. Exposing a second, unreconciled financial data source to an accounting runtime invites two answers to the same question. Precondition for any change: a written decision that BigQuery holds a financial dataset Cato is permitted to read, plus the `SELECT`-only parser. Provider if implemented: **Google BigQuery** via the official client with a read-only service account.

---

#### `data_dbt_compile`

* **(a) Registered name:** `data_dbt_compile` — `tools/data_pipeline_tool.py:455`, defined line 52.
* **(b) Purpose:** Compiles a dbt model, i.e. the SQL that transforms raw data into reported financial figures.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `model_sql` | `str` | yes | 1–100000 chars. Must contain no DML or DDL outside the dbt model body. |
| `refs` | `list[str]` | no | Each must match a known model name; unknown refs fail `validation_failed` rather than compiling to a dangling reference. |

* **(d) Output contract:** Success `result = {"compiled_sql": "<str>", "refs_resolved": [...], "applied": false}`; `applied: false` is a constant — this tool never runs the model. Failure: Section 5.2.
* **(e) Risk class:** `RISK_READ_ONLY` (compile only; no warehouse connection).
* **(f) Mode:** `PROPOSE_ONLY` — it produces SQL for human review. Compiling is safe; *running* the model would change reported financial figures and is out of scope for this tool permanently.
* **(g) Cato auth:** Not required to compile. Any future "run" capability would be a different tool at `APPROVAL_REQUIRED` or higher.
* **(h) Idempotency:** Not applicable; `result_checksum` over `compiled_sql` mandatory.
* **(i) Verdict:** **QUARANTINE.** Precondition: an explicit contractual guarantee, tested, that no execution path from this tool reaches a warehouse. Provider if implemented: **dbt CLI** in a sandbox with `--no-write-json` and no warehouse profile configured.

---

#### `data_s3_signed_url`

* **(a) Registered name:** `data_s3_signed_url` — `tools/data_pipeline_tool.py:453`, defined line 18.
* **(b) Purpose:** Mints a pre-signed URL granting time-limited access to an S3 object, which may be a financial export.
* **(c) Input contract:**

| Parameter | Type | Required | Validation |
|---|---|---|---|
| `bucket` | `str` | yes | Must be in a closed allowlist. |
| `key` | `str` | yes | Must match an allowlisted prefix pattern; `..` and absolute paths rejected. |
| `method` | `str` | yes | Enum, **`GET` only**. `PUT` and `DELETE` are rejected — this tool must not mint write access to a financial data store. |
| `expires_in_seconds` | `int` | no (default 900) | 60–3600. The current default of 3600 is halved; a URL granting access to financial data is a bearer credential. |
| `authorization` | `object` | yes | Section 2.2 context, so the audit trail records who was granted access to what. |

* **(d) Output contract:** Success `result = {"url": "<presigned>", "expires_at": "<RFC3339>", "bucket", "key", "method": "GET"}`. **The URL must be redacted in all logs and in the audit record — the audit stores `bucket`, `key`, `expires_at`, and `authorization_id`, never the signed URL.** Failure: Section 5.2.
* **(e) Risk class:** `RISK_NETWORK` plus authorization gate.
* **(f) Mode:** `APPROVAL_REQUIRED` — minting a bearer credential to financial data is a disclosure event even though nothing is written.
* **(g) Cato auth:** **REQUIRED.**
* **(h) Idempotency:** `business_keys = {bucket, key, method, expires_in_seconds}`. A duplicate within the original URL's validity window returns the **stored envelope** rather than minting a second credential.
* **(i) Verdict:** **QUARANTINE.** Preconditions: bucket/prefix allowlist, `GET`-only enforcement, log redaction. Provider when implemented: **AWS S3** via a scoped IAM role permitting `s3:GetObject` on the allowlisted prefixes only.

---

### 3.8 `escrow_client.py` — live payment-capable code

These four are **not** registered tools. They are module functions called directly from `main.py` (lines 1753, 1834, 1867, 1874, 1878, 2439) and `worker.py` (lines 187, 211, 238). They bypass `tools/__init__.py`, bypass `check_tool_policy`, and bypass every control in this document. They must be classified because a later agent will otherwise assume "not a registered tool" means "out of scope".

---

#### `escrow_client.initiate_escrow`

* **(a) Registered name:** Not registered as a tool. Callable path: `escrow_client.initiate_escrow`, called at `main.py:1753` and `main.py:1834`. Defined `escrow_client.py:26`.
* **(b) Purpose:** Holds buyer funds in escrow by POSTing to `{SWARMSYNC_API_INTERNAL_URL}/payments/ap2/initiate`.
* **(c) Input contract:** No valid input contract. Current parameters `source_wallet_id`, `destination_wallet_id`, `amount_cents: int`, `memo`, `metadata` are rejected. **Defect for the record:** the signature takes integer minor units and then transmits `"amount": amount_cents / 100.0` (line 41) — a float in a payment payload. `1` cent becomes `0.01`, and any amount whose cent value is not exactly representable in binary floating point is transmitted with rounding error.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — it constructs and transmits a funds movement between two wallets. It is gated only on `INTERNAL_SECRET` being non-empty (line 35); there is no per-call human authorization anywhere in the path.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** None exists. Two identical calls create two escrows.
* **(i) Verdict:** **PROHIBIT** within any deployment that Cato can reach. Enforcement: the E4L/Cato-facing deployment must not ship `escrow_client.py`, and `main.py` / `worker.py` must not import it (Section 6.3).

---

#### `escrow_client.complete_escrow`

* **(a) Registered name:** Not registered. Called at `main.py:1867` and `worker.py:187` with `status="SETTLED"`. Defined `escrow_client.py:77`.
* **(b) Purpose:** Settles or fails an escrow, releasing held funds to the destination.
* **(c) Input contract:** No valid input contract. `escrow_id`, `status`, `failure_reason` rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — settling escrow *is* the disbursement. `worker.py:187` calls it with `status="SETTLED"` on job success, with **no human in the loop at any point**. This is automation paying, which is the exact inversion of Section 0.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** None exists.
* **(i) Verdict:** **PROHIBIT.**

---

#### `escrow_client.release_escrow`

* **(a) Registered name:** Not registered. Called at `main.py:1785`, `main.py:1854`, `main.py:1878`, `main.py:2439`, `worker.py:211`, `worker.py:238`. Defined `escrow_client.py:115`.
* **(b) Purpose:** Refunds the buyer by releasing the escrow.
* **(c) Input contract:** No valid input contract. `escrow_id`, `reason` rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — a refund is a transfer. That its direction favours the customer does not exempt it; it is still automation moving money, and it is still a financial event that must be recorded rather than caused.
* **(g) Cato auth:** Not applicable; Cato must refuse to mint.
* **(h) Idempotency:** None exists. Six distinct call sites in two files, several inside exception handlers, with no idempotency key between them.
* **(i) Verdict:** **PROHIBIT.**

---

#### `escrow_client.calculate_split`

* **(a) Registered name:** Not registered. Called at `main.py:1874`. Defined `escrow_client.py:149`.
* **(b) Purpose:** Computes the platform-fee split that determines how much each party is disbursed.
* **(c) Input contract:** No valid input contract. `total_cents: int`, `fee_pct_override: float` rejected.
* **(d) Output contract:** Section 6.4 refusal envelope only.
* **(e) Risk class:** `RISK_PROHIBITED`.
* **(f) Mode:** `PERMANENTLY_PROHIBITED` — it is the amount-construction step of a transfer. A function whose only consumer is a disbursement is part of the disbursement.
* **(g) Cato auth:** Not applicable.
* **(h) Idempotency:** Pure function; not applicable.
* **(i) Verdict:** **PROHIBIT.** **Defects for the record**, so a future implementer does not copy them: line 155 computes `int(total_cents * pct)` — an integer multiplied by a float, then truncated toward zero. At `total_cents=999`, `pct=0.10` the true fee is 99.9 and the result is 99, so one minor unit is silently unallocated on every such split. Correct practice is `Decimal` with an explicit rounding mode and largest-remainder allocation so the parts sum exactly to the total. `PLATFORM_FEE_PCT` is additionally parsed from an environment variable via `float()` at import (line 23), so a malformed value crashes at import and a subtly wrong one silently changes every split.

---

## 4. Money representation rules (binding on every contract above)

1. **Integer minor units only.** Every monetary value is an integer in the currency's minor unit (`amount_minor`). `float` is forbidden in parameters, return values, JSON payloads, and intermediate arithmetic.
2. **Field naming.** Monetary fields end in `_minor`. This makes a float or a major-unit value visible in code review.
3. **Explicit ISO-4217 currency.** Every monetary value is accompanied by an explicit uppercase 3-letter code. No defaults — `currency: str = "USD"` (`finance_tool.py:125`) is a contract violation. Zero-decimal currencies (JPY) and three-decimal currencies (BHD, KWD, TND) are handled by an exponent table, not by assuming 100.
4. **No cross-currency arithmetic** without an explicit FX rate, an FX rate date, and an FX rate source recorded in the evidence. Summing mixed currencies is `validation_failed`.
5. **Division and allocation.** Any division uses `decimal.Decimal` with `ROUND_HALF_EVEN`. Split amounts use largest-remainder allocation and must sum exactly to the original. Truncation is forbidden.
6. **Percentages and rates** are integers in basis points (`_bp`), never floats. 20% VAT is `2000`.
7. **Signs.** Amount fields are unsigned unless the field name says otherwise; direction is carried by an explicit `direction` or `type` enum, never by a negative number.
8. **Boundary conversion.** Where a provider insists on decimal strings, conversion happens at the HTTP boundary only, from the integer, using `Decimal`, and the integer remains authoritative in logs and evidence.

---

## 5. The truthful-failure contract

This replaces `{"ok": true, "stub": true, "note": "..."}` everywhere. `tools/data_pipeline_tool.py` is the closest existing precedent; this generalises and tightens it.

### 5.1 Success envelope

```json
{
  "ok": true,
  "tool": "<exact registered name>",
  "contract_version": "1.0.0",
  "request_id": "<uuid v4>",
  "mode": "READ_ONLY|PROPOSE_ONLY|APPROVAL_REQUIRED",
  "idempotency_key": "<sha256 hex, or null for READ_ONLY>",
  "duplicate": false,
  "result": { "...tool-specific..." },
  "evidence": { "...Section 5.4 or 5.5..." }
}
```

### 5.2 Failure envelope

```json
{
  "ok": false,
  "tool": "<exact registered name>",
  "contract_version": "1.0.0",
  "request_id": "<uuid v4>",
  "error": {
    "code": "<one of the seven codes>",
    "retryable": true,
    "message": "<human readable, no secret values, no key material>",
    "detail": { "...structured, machine-readable..." },
    "retry_after_ms": 2000
  }
}
```

### 5.3 Structural rules — these are what make failure distinguishable without string parsing

1. `ok` is the **only** discriminator. A caller branches on `ok` and nothing else.
2. `result` is present **if and only if** `ok` is `true`. `error` is present **if and only if** `ok` is `false`. Never both, never neither.
3. **The keys `stub`, `scaffold`, and `note` are forbidden in every envelope.** A tool that has not done the work returns `ok: false`.
4. `ok: true` requires a non-empty `evidence` object that satisfies Section 5.4 or 5.5. **A tool that cannot produce evidence must return `ok: false` with `not_implemented`.** This is the structural guarantee that no stub can report success: success requires proof, and a stub has none.
5. `error.retryable` is a boolean the caller may act on directly. It is derived from `error.code` by the fixed table in 5.6 — never chosen per call site.
6. Exception leakage is forbidden. The current `_err()` helpers (`finance_tool.py:31`, and the identical copies in the other three modules) return `{"ok": false, "error": "<ExceptionClassName>", "message": str(exc)}` — the `error` field holds a Python class name, not a taxonomy code, so callers cannot branch on it and `str(exc)` may carry connection strings or credential fragments. Replace with the envelope above: exceptions map to a taxonomy code, the original type goes in `detail.exception_type`, and `message` is a sanitised, tool-authored string.
7. `contract_version` is semver. A caller that does not recognise the major version must treat the response as `ok: false`.

### 5.4 Error taxonomy and retryability

| Code | Retryable | Meaning | Caller action |
|---|---|---|---|
| `not_implemented` | **No** | The tool exists but the operation is not built. Terminal and permanent. | Do not retry. Escalate to a human. This is the code every current stub must return. |
| `provider_unconfigured` | **No** | A required credential or endpoint is absent. `detail.missing_keys` lists **environment variable names only** — never values. | Do not retry. Configure and re-dispatch as a new request. |
| `upstream_timeout` | **Yes** | The provider did not respond in time, returned 5xx, or the write completed but could not be verified by readback (`detail.state = "indeterminate"`). | Retry **with the same idempotency key**, bounded exponential backoff, honouring `retry_after_ms`, max 5 attempts. Never retry without the key. |
| `validation_failed` | **No** | Input violated the contract. `detail.violations` is a list of `{field, rule, received_type}`. `received` values are omitted for money and identifiers to avoid echoing sensitive data. | Do not retry unchanged. Fix the input. |
| `duplicate_request` | **Yes** | The idempotency slot is `IN_FLIGHT` under another request. | Retry after `retry_after_ms` with the same key. A `SUCCEEDED` or `FAILED_TERMINAL` slot returns the stored envelope instead (Section 2.3), not this code. |
| `authorization_missing` | **No** | No Cato authorization, or it failed one of the seven checks in Section 2.2. `detail.failed_check` names which. | Do not retry. Obtain a fresh human authorization. |
| `policy_denied` | **No** | `check_tool_policy` denied, or the tool is on the prohibited list. `detail.risk_class` and `detail.agent_slug` are included. | Do not retry. A denial is a decision, not a transient fault. |

Retryability is a property of the code alone. No tool may mark `not_implemented`, `validation_failed`, `authorization_missing`, or `policy_denied` retryable.

### 5.5 Evidence contract — write operations

> **A 200 response is not evidence.** A 200 proves a request was accepted. It does not prove state changed, changed to the intended value, or changed exactly once.

Every `ok: true` from an `APPROVAL_REQUIRED` tool must carry:

```json
"evidence": {
  "provider": "xero",
  "provider_object_type": "ManualJournal",
  "provider_object_id": "<id assigned by the provider>",
  "provider_request_id": "<provider correlation id, or null>",
  "readback": { "...the object as re-fetched by a separate GET after the write..." },
  "readback_method": "GET /ManualJournals/<id>",
  "readback_at": "<RFC3339 UTC>",
  "readback_matches": true,
  "compared_fields": ["amount_minor", "currency", "date", "account_id"],
  "rows_affected": 1,
  "checksum": "<sha256 over the canonical serialization of compared_fields in readback>",
  "authorization_id": "<Cato authorization consumed>",
  "idempotency_key": "<sha256 hex>",
  "observed_at": "<RFC3339 UTC>"
}
```

Binding rules:

1. **Readback is a separate request.** The write response body is not readback. The object must be re-fetched by id.
2. **`readback_matches` is computed**, field by field, comparing the caller's intent against the re-fetched object. It is never asserted.
3. **`ok: true` requires `readback_matches: true`.** If readback fails or mismatches, the tool returns `ok: false` with `upstream_timeout` and `detail.state = "indeterminate"`, plus the idempotency key so a safe retry converges. An unverifiable write is never reported as success.
4. **`rows_affected` comes from the system that performed the write** — the database's own count or the provider's own response — never from `len(input)`. `finance_import_x402_transactions` currently returns `"imported": len(txs)` (`finance_tool.py:282`) having inserted nothing; that pattern is forbidden.
5. **`checksum`** is sha256 over the canonical JSON of the compared fields as they appear in the readback, so a downstream reconciler can detect later drift.
6. **Zero-row writes are failures.** `rows_affected: 0` on an operation that intended a write is `ok: false`, never a quiet success.

### 5.6 Evidence contract — read operations

Every `ok: true` from a `READ_ONLY` tool must carry:

```json
"evidence": {
  "source": "xero|bigquery:<project>|genesis_durable_store|caller_supplied",
  "unverified": false,
  "query_fingerprint": "<sha256 of the canonical query or input>",
  "row_count": 128,
  "checksum": "<sha256 over the canonical result>",
  "as_of": "<RFC3339 UTC>",
  "observed_at": "<RFC3339 UTC>"
}
```

`unverified` is `true` whenever `source == "caller_supplied"`. A report computed from data the caller handed in is arithmetic, not reconciliation, and must say so in a machine-readable field — not in a prose `note`.

### 5.7 The single rule that closes the stub loophole

> A tool may return `ok: true` **only** when it can name the system it changed or read, the identifier of the object it changed or the fingerprint of the query it ran, and a readback or checksum proving it. In every other circumstance it returns `ok: false`.

---

## 6. PERMANENTLY_PROHIBITED list and its enforcement

### 6.1 The list

**Group A — constructs or transmits a payment, transfer, ACH, wire, payroll disbursement, or escrow settlement, or alters bank/wire instructions (18):**

1. `finance_run_payroll_batch`
2. `finance_process_vendor_invoice`
3. `finance_run_finance_close`
4. `billing_run_dunning_batch`
5. `billing_run_billing_cycle`
6. `commerce_register_domain`
7. `commerce_activate_payment_gateway`
8. `commerce_ship_fulfillment_batch`
9. `commerce_launch_commerce_stack`
10. `pricing_purchase_dataset`
11. `pricing_run_pricing_cycle`
12. `domain_create_intent_mandate`
13. `domain_register`
14. `domain_select_and_register`
15. `workflow_webhook_trigger` *(slug-scoped: prohibited for every finance-domain slug and any dispatch carrying a Cato authorization)*
16. `escrow_client.initiate_escrow`
17. `escrow_client.complete_escrow`
18. `escrow_client.release_escrow`

**Group B — constructs the amount of a disbursement (1):**

19. `escrow_client.calculate_split`

**Group C — fabricates financial figures and returns them as verified success (1):**

20. `pricing_generate_pricing_report`

**Total: 20.** Groups A, B, and C are enforced identically. The grouping records *why*, so a future reviewer cannot argue Group C is "only a reporting bug".

### 6.2 Enforcement — six layers, at least four of which are not config edits

**Layer 1 — Deletion (primary).** The function bodies, their `_SCHEMAS` entries, and their `register_tool(...)` lines are deleted from the source tree. Absence is stronger than denial. Deleted code cannot be re-enabled by a config change; it requires reintroducing code in a reviewable diff.

**Layer 2 — Boot-time registry assertion.** In `runtime/tool_policy.py`:

```python
RISK_PROHIBITED = "prohibited"

PROHIBITED_TOOLS: frozenset[str] = frozenset({ ...the 20 names... })

PROHIBITED_TOOLS_BY_SLUG: dict[str, frozenset[str]] = {
    "genesis-finance":  frozenset({"workflow_webhook_trigger"}),
    "genesis-billing":  frozenset({"workflow_webhook_trigger"}),
    "genesis-commerce": frozenset({"workflow_webhook_trigger"}),
    "genesis-pricing":  frozenset({"workflow_webhook_trigger"}),
}

def assert_prohibitions_intact() -> None:
    """Raise at import time if any prohibition has been weakened."""
    import tools
    present = PROHIBITED_TOOLS & set(tools._TOOLS)
    if present:
        raise RuntimeError(f"prohibited tools are registered: {sorted(present)}")
    for slug, allowed in SLUG_ALLOWED_RISKS.items():
        if RISK_PROHIBITED in allowed:
            raise RuntimeError(f"slug {slug} grants RISK_PROHIBITED")
    if RISK_PROHIBITED in DEFAULT_ALLOWED_RISKS:
        raise RuntimeError("DEFAULT_ALLOWED_RISKS grants RISK_PROHIBITED")
```

Called at gateway startup **before** the FastAPI app accepts traffic. A re-registered prohibited tool is a **process that refuses to start**, not a request that gets denied. Fail-to-boot beats fail-to-deny: a denial can be missed in logs; a service that will not start cannot be.

**Layer 3 — Frozen manifest.** `runtime/prohibited_tools.sha256` contains a sha256 over the canonical sorted list. `assert_prohibitions_intact()` recomputes it and raises on mismatch. Editing the list alone therefore breaks the boot; the editor must also regenerate a hash in a file whose name announces what they are weakening. This converts a one-line config edit into a two-file, self-documenting, reviewable change.

**Layer 4 — Dispatcher pre-check.** In `agent_runtime.py`, before `check_tool_policy` and before any tool lookup:

```python
if fn_name in PROHIBITED_TOOLS or fn_name in PROHIBITED_TOOLS_BY_SLUG.get(slug, frozenset()):
    emit_event(job_id, "tool.prohibited", {"tool_name": fn_name, "agent_slug": slug})
    tool_result = _prohibited_refusal(fn_name)   # Section 6.4
```

This is independent of `TOOL_RISK` and of `SLUG_ALLOWED_RISKS`, so a mistake in the risk table cannot reach a prohibited tool. It is also the only place slug-scoped prohibition is evaluated.

**Layer 5 — CI gate.** `test_prohibited_tools.py`, required to pass before merge:

* For every name in `PROHIBITED_TOOLS`: `tools.get_tool(name) is None`.
* For every name × **every** slug in `SLUG_ALLOWED_RISKS` plus an unknown slug: `check_tool_policy(...)["ok"] is False`.
* `RISK_PROHIBITED` appears in no value of `SLUG_ALLOWED_RISKS` and not in `DEFAULT_ALLOWED_RISKS`.
* `RISK_PAYMENT` appears in no value of `SLUG_ALLOWED_RISKS` (Section 7 Phase 5).
* A source-tree scan asserts no `register_tool("<prohibited name>"` line exists.
* The manifest hash matches.
* A negative-control test constructs a registry containing a prohibited name and asserts `assert_prohibitions_intact()` raises — proving the guard works rather than assuming it.
* `escrow_client` is not importable from the Cato-facing deployment (Section 6.3).

**Layer 6 — Cato-side and gateway-side refusal.** Cato must refuse to mint an authorization naming a prohibited tool. The Genesis gateway must reject any request whose declared tool list intersects `PROHIBITED_TOOLS` with HTTP 403 **before** any LLM call, so a prohibited name never enters a prompt. Prohibition is thereby enforced on both sides of the boundary, and neither side trusts the other to do it.

### 6.3 `escrow_client.py` specifically

Layers 1–5 address registered tools. `escrow_client.py` is not one, so it needs its own treatment:

1. The Cato-facing / E4L deployment **does not ship `escrow_client.py`**. It is removed from the deployment artefact.
2. `main.py` and `worker.py` must not import it. The existing `try/except ImportError` fallback at `main.py:123-136` already tolerates its absence — that path becomes the only path, and the `logger.warning` becomes a hard startup assertion that the module is absent.
3. A CI test asserts `importlib.util.find_spec("escrow_client") is None` in the Cato-facing build.
4. The six `release_escrow` call sites and the two `complete_escrow` call sites are deleted rather than guarded, so no dormant settlement path remains behind a flag.
5. `INTERNAL_SECRET` must not be present in the Cato-facing environment. This is stated last deliberately: **it is the weakest control and must never be the only one.** Prohibition that depends on an unset environment variable is exactly the config-flag enforcement this section exists to replace.

### 6.4 The prohibited-call refusal envelope

If a prohibited name reaches the dispatcher despite Layers 1–3, the response is fixed and carries no execution:

```json
{
  "ok": false,
  "tool": "<name>",
  "contract_version": "1.0.0",
  "request_id": "<uuid v4>",
  "error": {
    "code": "policy_denied",
    "retryable": false,
    "message": "This operation is permanently prohibited. Automation may prepare; a human pays; automation records.",
    "detail": {
      "risk_class": "prohibited",
      "agent_slug": "<slug>",
      "prohibition_group": "A|B|C",
      "remediation": "Escalate to a named human approver. This decision is not overridable by configuration."
    },
    "retry_after_ms": null
  }
}
```

The event `tool.prohibited` is emitted at severity `critical` on every occurrence and must page, because a prohibited name reaching the dispatcher means Layers 1–3 have failed.

---

## 7. Safe re-enablement plan for `runtime/tool_policy.py`

**The forbidden fix:** do not map registered names by category-word prefix, and do not simply add the registered names to `TOOL_RISK` in one commit. Either would flip ~55 tools from denied to allowed simultaneously, and prefix-mapping would additionally resolve `pricing_purchase_dataset` — a money-spending tool — to `read_only`, exposing it to every slug (Section 1.3).

### Phase 0 — current state, deliberately preserved

Everything specialised resolves to `admin` and is denied. This is accidentally the safest configuration the system has ever had. It is the baseline; nothing is relaxed until Phases 1–3 are complete.

### Phase 1 — make the mismatch observable without changing behaviour

* Add `TOOL_RISK_BY_NAME: dict[str, str]`, keyed on **registered names**, explicitly populated for every registered tool. No prefixes, no defaults, no `.startswith()`. Each entry is written by hand and reviewed.
* Add `get_tool_risk_by_name(name)`.
* **Do not wire it into `check_tool_policy`.** In the dispatcher, compute both and emit `tool.policy.shadow` with `{tool, agent_slug, legacy_risk, new_risk, would_allow_legacy, would_allow_new}`. Enforcement continues to use the legacy path.
* **Preconditions to exit Phase 1:** a soak of at least 7 days or 500 dispatch attempts, whichever is later; the shadow log shows `would_allow_new == false` for every name in `PROHIBITED_TOOLS`; and every registered name appears in `TOOL_RISK_BY_NAME` with no name falling through to a default.
* **Gating test:** `test_tool_risk_coverage.py` — imports the live registry and asserts `set(tools._TOOLS) - set(TOOL_RISK_BY_NAME) == set()`. Any newly registered tool with no explicit entry fails the build, so a future tool cannot inherit a permission silently.

### Phase 2 — install the prohibition floor before relaxing anything

* Land `RISK_PROHIBITED`, `PROHIBITED_TOOLS`, `PROHIBITED_TOOLS_BY_SLUG`, `assert_prohibitions_intact()`, the manifest, the dispatcher pre-check, and `test_prohibited_tools.py`. Deploy.
* Runtime behaviour is unchanged — everything is still denied — but the floor now exists beneath every later phase.
* **Precondition to exit Phase 2:** `test_prohibited_tools.py` green, **including the negative-control test** that proves the boot assertion actually raises when a prohibited name is registered.

### Phase 3 — first behaviour change: read-only tools only, with payment simultaneously disabled

This is the only phase that widens access, and it widens it only to `READ_ONLY`.

* Switch `check_tool_policy` to `TOOL_RISK_BY_NAME`.
* **In the same commit,** remove `RISK_PAYMENT` from `genesis-finance` (`runtime/tool_policy.py:72`), leaving it `frozenset({RISK_READ_ONLY, RISK_NETWORK})`. `genesis-billing`, `genesis-commerce`, and `genesis-pricing` remain absent from `SLUG_ALLOWED_RISKS` and stay on `DEFAULT_ALLOWED_RISKS = {RISK_READ_ONLY}`.
* Net effect: read-only report tools become callable for the first time; **`RISK_PAYMENT` becomes unreachable at the moment the name mapping starts working.** The mismatch is corrected and payment enforcement is switched *off*, not on. It is never exercised untested because it is never exercised at all.
* **Preconditions to exit Phase 3:** Phase 1 and 2 complete; every `READ_ONLY` tool being enabled has already adopted the Section 5 envelope and returns `provider_unconfigured` rather than a fabricated success (this specifically blocks the twelve `*_get_*` tools and `domain_get_cost_summary`, which currently return invented constants).
* **Gating test — this is the test that must pass first:** `test_tool_policy_matrix.py`.
  * A checked-in fixture `expected_policy_matrix.json` enumerates every `(registered_name, slug)` pair with its expected `ok`.
  * The test asserts `check_tool_policy` matches the fixture for **every** pair, exhaustively.
  * The test fails if any registered name is absent from the fixture — so a new tool cannot be added without a reviewer explicitly writing down who may call it.
  * The test asserts `RISK_PAYMENT not in set().union(*SLUG_ALLOWED_RISKS.values()) | DEFAULT_ALLOWED_RISKS`.
  * The test asserts every `PROHIBITED_TOOLS` name is `ok: false` for every slug.
  * Reviewing the fixture diff is how a human sees, in one screen, exactly which permissions a commit changes. A policy change that does not show up in that diff has not happened.

### Phase 4 — `APPROVAL_REQUIRED` tools, one at a time, never by risk class alone

Risk class is necessary but not sufficient. Each such tool requires **three independent gates, all of which must pass**:

1. `check_tool_policy` allows;
2. `require_authorization(tool, args, auth_ctx)` validates a Cato authorization against all seven checks in Section 2.2;
3. the idempotency slot is reserved atomically with nonce consumption.

Per-tool preconditions, all mandatory before that one tool is enabled:

* A named provider with credentials present **by key name**;
* Section 5.5 readback evidence implemented, with `readback_matches` computed rather than asserted;
* The idempotency store live with the unique constraint in place;
* The Section 5 envelope adopted, with no `stub`, `scaffold`, or `note` keys anywhere;
* A staging run demonstrating a correct `ok: false` for **each of the seven error codes**, fault-injected;
* A staging run demonstrating a genuine write whose readback matches;
* A staging run demonstrating that a duplicate call returns the stored envelope and that the **provider-side object count is unchanged**.

* **Gating test per tool:** `test_<tool>_evidence_contract.py` asserting (a) `provider_unconfigured` when credentials are absent, (b) `ok: false` with `upstream_timeout` and `detail.state == "indeterminate"` when readback is fault-injected to mismatch, (c) a duplicate call creates no second provider object, asserted by counting provider-side objects rather than by trusting the tool's own report.
* Each tool is enabled in its own commit, with its own fixture diff, and soaked before the next.

### Phase 5 — `RISK_PAYMENT` remains permanently unreachable

`RISK_PAYMENT` stays defined so the mismatch correction is complete and no code path silently falls back to it. A boot-time assertion plus the Phase 3 matrix test forbid it from appearing in any slug's allowed set. Turning it on would require editing the assertion, editing the fixture, and regenerating the manifest — three visible changes across three files, each of which fails CI on its own. That is the intended cost.

### Related precondition outside `tool_policy.py`

`main.py:271-274` returns early with only a `logger.warning` when `GATEWAY_API_KEY` is unset, leaving the gateway open to all callers. Since Cato's authorization model assumes the gateway is authenticated, this must become a hard startup failure — refuse to boot without `GATEWAY_API_KEY` — before Phase 3. A fail-open front door makes every downstream control advisory.

---

## 8. Classification summary

| Mode | Count | Tools |
|---|---|---|
| `PERMANENTLY_PROHIBITED` | 20 | Section 6.1 |
| `APPROVAL_REQUIRED` | 8 | `finance_sync_bank_fees`, `finance_import_x402_transactions`, `billing_import_ar_ledger`, `billing_deploy_plan_change`, `commerce_configure_tax_engine`, `pricing_run_elasticity_experiment`, `pricing_deploy_pricing_update`, `data_s3_signed_url` |
| `PROPOSE_ONLY` | 4 | `workflow_zapier_export`, `workflow_n8n_export`, `workflow_make_export`, `data_dbt_compile` |
| `READ_ONLY` | 17 | `finance_generate_finance_report`, `billing_generate_revops_report`, `domain_check_availability`, `domain_get_cost_summary`, `data_bigquery_query`, and the twelve `*_get_budget_metrics` / `*_get_audit_log` / `*_get_alerts` |
| `WRITE_CAPABLE` | **0** | None. No tool in this repository may write autonomously to a financial system of record. |
| **Total classified** | **49** | |

| Verdict | Count |
|---|---|
| PROHIBIT | 20 |
| QUARANTINE | 26 |
| IMPLEMENT | 3 (`finance_generate_finance_report`, `billing_generate_revops_report`, `domain_check_availability` — each conditional on the fixes named in its row) |

Excluded from classification with justification: `domain_generate_candidates`, `domain_configure_dns`, `data_pipeline_design`, `data_quality_check`.

---

## 9. Disagreements with the prior audit

**1. `pricing_generate_pricing_report` does NOT do real arithmetic — it fabricates financial figures.** The prior audit grouped it with `finance_generate_finance_report` and `billing_generate_revops_report` as tools that "do real arithmetic on caller-supplied data". Evidence: `tools/pricing_tool.py:157-174` returns hardcoded constants — `revenue_total_usd: 482500.00`, `revenue_delta_pct: 8.4`, `elasticity_mean: -1.32`, `best_price_point_usd: 49.00` — wrapped by `_scaffold(...)`, hence `ok: true`. The output does not depend on any input. The other two genuinely compute from caller data. This is the highest-severity item in the four money modules for an accounting runtime, and it is why the prohibition list has a Group C.

**2. Function counts are 9 / 8 / 8 / 8, not 12 / 11 / 11 / 11.** The higher figures count the module-level helpers `_scaffold` and `_err` and the `register()` function, which are not tools. Verified against `register()` bodies and against a live dump of `tools._TOOLS` (Appendix A).

**3. The defect is far broader than the finance domain.** **55 of 60** registered names have no `TOOL_RISK` entry, including `run_code` (sandboxed code execution), `send_email`, all four `hr_*`, all four `workflow_*`, and all five `data_*`. Conversely **18 of 23** `TOOL_RISK` keys are orphans matching nothing. So it is not only the payment path that has never executed — **no finance-domain tool has ever executed at all, including the read-only report tools.** The prior finding understates the blast radius of a naive fix by roughly an order of magnitude.

**4. `escrow_client` is not "one environment variable away from functioning" — it is already fully wired into the live job lifecycle.** Call sites: `main.py:1753`, `1785`, `1834`, `1854`, `1867`, `1874`, `1878`, `2439`; `worker.py:187`, `211`, `238`. `worker.py:187` calls `complete_escrow(escrow_id=..., status="SETTLED")` on job success with no human in the loop. Setting `INTERNAL_SECRET` does not merely make a module callable; it activates an already-complete autonomous settlement path. The correct characterisation is "live payment code with its credential currently unset", and the remediation must therefore be removal of the call sites, not merely withholding the secret.

**5. `escrow_client` transmits floats in a payment payload and truncates the fee split.** `escrow_client.py:41` sends `"amount": amount_cents / 100.0` despite the parameter being `amount_cents: int`. `escrow_client.py:155` computes `int(total_cents * pct)`, an integer-times-float truncated toward zero — at `total_cents=999, pct=0.10` the fee is 99 rather than 99.9, silently leaving one minor unit unallocated on every such split. `PLATFORM_FEE_PCT` is additionally `float()`-parsed from the environment at import (line 23). Not mentioned in the prior audit; material to any future implementer.

**6. `TOOL_RISK["pricing"] = RISK_READ_ONLY` is itself wrong, independent of the keying bug.** `runtime/tool_policy.py:51`. Even the *intended* category mapping misclassified the pricing domain, which contains `pricing_purchase_dataset` — a tool that spends money. Any remediation that maps registered names by category prefix would therefore make a payment tool callable by every agent slug including the `DEFAULT_ALLOWED_RISKS` fallback. This is the most dangerous plausible fix and is explicitly forbidden by Section 7.

**7. `tools/data_pipeline_tool.py` already implements the honest-failure pattern.** Lines 18–69 return `{"ok": false, "scaffold": true, "message": "..."}`. The prior audit framed truthful failure as entirely absent. It exists, in one module, and is the in-repo precedent Section 5 generalises — worth citing to an implementer as "do what `data_pipeline_tool.py` does, plus the taxonomy and the evidence block".

---

## Appendix A — registered-name verification (proof artifact)

Method: import `tools`, call `register_default_tools()`, dump `tools._TOOLS` and resolve each name through `runtime.tool_policy.get_tool_risk`. **No tool function was executed. `escrow_client` was not imported, not called, and `INTERNAL_SECRET` was not set.** Script: `scratchpad/verify_names.py`.

Interpreter: Python 3.12.13. Six non-finance modules (`deploy_tool`, `github_tool`, `netlify_deploy_tool`, `vercel_deploy_tool`, `vision_tool`, `web_tool`) failed to import locally because `httpx` is absent; all finance-domain modules imported cleanly, so the finance name set is complete.

**Registered finance-adjacent names (49 classified from this set):**

```
billing_deploy_plan_change            commerce_activate_payment_gateway
billing_generate_revops_report        commerce_configure_tax_engine
billing_get_alerts                    commerce_get_alerts
billing_get_audit_log                 commerce_get_audit_log
billing_get_budget_metrics            commerce_get_budget_metrics
billing_import_ar_ledger              commerce_launch_commerce_stack
billing_run_billing_cycle             commerce_register_domain
billing_run_dunning_batch             commerce_ship_fulfillment_batch

data_bigquery_query                   domain_check_availability
data_dbt_compile                      domain_configure_dns      (excluded)
data_pipeline_design   (excluded)     domain_create_intent_mandate
data_quality_check     (excluded)     domain_generate_candidates (excluded)
data_s3_signed_url                    domain_get_cost_summary
                                      domain_register
                                      domain_select_and_register

finance_generate_finance_report       pricing_deploy_pricing_update
finance_get_alerts                    pricing_generate_pricing_report
finance_get_audit_log                 pricing_get_alerts
finance_get_budget_metrics            pricing_get_audit_log
finance_import_x402_transactions      pricing_get_budget_metrics
finance_process_vendor_invoice        pricing_purchase_dataset
finance_run_finance_close             pricing_run_elasticity_experiment
finance_run_payroll_batch             pricing_run_pricing_cycle
finance_sync_bank_fees

workflow_make_export   workflow_n8n_export   workflow_webhook_trigger   workflow_zapier_export
```

**Risk resolution — every finance-adjacent registered name resolves to `admin`:**

```
finance_run_payroll_batch                -> admin
finance_generate_finance_report          -> admin
billing_run_dunning_batch                -> admin
commerce_activate_payment_gateway        -> admin
pricing_purchase_dataset                 -> admin
domain_register                          -> admin
workflow_webhook_trigger                 -> admin
   ... (49 of 49, without exception)
```

**Category-word lookups — what the table was actually keyed on:**

```
finance    -> payment
billing    -> payment
commerce   -> payment
pricing    -> read_only      <-- wrong even as intended (Disagreement 6)
```

**Policy decisions:**

```json
{"agent_slug":"genesis-finance","allowed_risks":["network","payment","read_only"],"error":"tool_policy_denied","ok":false,"risk_class":"admin","tool_name":"finance_run_payroll_batch"}
{"agent_slug":"genesis-finance","allowed_risks":["network","payment","read_only"],"error":"tool_policy_denied","ok":false,"risk_class":"admin","tool_name":"finance_generate_finance_report"}
{"agent_slug":"genesis-billing","allowed_risks":["read_only"],"error":"tool_policy_denied","ok":false,"risk_class":"admin","tool_name":"billing_run_dunning_batch"}
{"agent_slug":"genesis-commerce","allowed_risks":["read_only"],"error":"tool_policy_denied","ok":false,"risk_class":"admin","tool_name":"commerce_activate_payment_gateway"}
{"agent_slug":"genesis-pricing","allowed_risks":["read_only"],"error":"tool_policy_denied","ok":false,"risk_class":"admin","tool_name":"pricing_deploy_pricing_update"}
```

**Coverage:**

```
Registered names with no TOOL_RISK entry (fail closed to admin): 55 of 60
TOOL_RISK keys matching no registered tool (orphans):            18 of 23
```

---

## Appendix B — implementer checklist

Before any tool in this document is marked done:

- [ ] No hardcoded secrets, API keys, wallet addresses, or account identifiers in any code path. Environment variable **names** only.
- [ ] All money is integer minor units with explicit ISO-4217 currency. No `float` in any monetary path.
- [ ] The Section 5 envelope is returned in every case; the keys `stub`, `scaffold`, and `note` appear nowhere.
- [ ] `ok: true` is impossible without an evidence block containing a readback or a checksum.
- [ ] Every error maps to exactly one of the seven taxonomy codes with the fixed retryability from Section 5.4.
- [ ] Idempotency key composition matches the tool's row; a duplicate call is proven — by provider-side object count — to create no second object.
- [ ] Cato authorization is validated against all seven checks in Section 2.2 for every `APPROVAL_REQUIRED` tool.
- [ ] `assert_prohibitions_intact()` runs at startup and the negative-control test proves it raises.
- [ ] `test_prohibited_tools.py`, `test_tool_risk_coverage.py`, and `test_tool_policy_matrix.py` are green.
- [ ] `escrow_client.py` is absent from the Cato-facing build and `importlib.util.find_spec("escrow_client") is None` is asserted in CI.
- [ ] The gateway refuses to boot without `GATEWAY_API_KEY`.
- [ ] Compliance obligations are surfaced, not assumed away: UK VAT and US sales-tax positions require a qualified accountant's sign-off; UK/EU personal data in payroll and registrant records is in GDPR scope; payment-instrument data must never enter Genesis at all, which is the strongest available PCI DSS scope reduction. This document is not legal, tax, or accounting advice.
