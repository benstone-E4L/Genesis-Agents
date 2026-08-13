# Genesis Agent Evaluation Harness

Arize Phoenix LLM-as-judge harness for the 57 agents on
`https://swarmsync-agents.onrender.com`.

> ## READ THIS BEFORE QUOTING ANY SCORE
>
> The default and only practical execution mode is `live_test`
> (`testContext: true`). That mode **bypasses `AgentRuntime` and tool dispatch
> entirely** and routes straight to the persona LLM.
>
> **A score from a `live_test` run measures PERSONA AND REASONING QUALITY ONLY.
> It is never evidence that any tool works.** A 4/4 on every rubric for
> `genesis_research_x402` proves the research persona reasons well. It proves
> nothing about `web_fetch`, nothing about the browser layer, and nothing about
> whether the agent could complete a single real task.
>
> This is enforced, not just documented: every report carries the sentence in
> its header, and `rubrics.assert_not_tool_evidence()` raises if a caller tries
> to label a `live_test` score as tool proof.

---

## What is here

| File | Owner | Purpose |
| --- | --- | --- |
| `genesis_client.py` | t25 | async gateway client, auth, retries, warmup, five outcome classes |
| `traceable.py` | t25 | `@traceable` instrumentation + recursive secret redaction |
| `target.py` | t25 | the `evaluate()` target; **owns the input/output schema** |
| `rubrics.py` | t27 | the five judge rubrics, as data |
| `datasets/` | t27 | starter datasets + the live slug snapshot |
| `run_experiment.py` | t27 | the runnable experiment driver |
| `tests/` | both | fake-transport tests; nothing here touches the network |

---

## Running it

```bash
# 1. Validate a dataset. Fetches GET /agents live, checks every slug and every
#    guard, prints the plan, invokes NOTHING. Always start here.
python -m eval.run_experiment --dataset eval/datasets/llm_only.jsonl --dry-run

# 2. Offline smoke test. Scripted transport, no network, no credential.
#    Proves the whole path: load -> validate -> guard -> invoke -> judge -> report.
python -m eval.run_experiment --dataset eval/datasets/llm_only.jsonl --fake-client

# 3. Real run. Needs a gateway credential in the environment.
python -m eval.run_experiment \
    --dataset eval/datasets/llm_only.jsonl \
    --judge keyword \
    --out report.json

# 4. Tests.
python -m pytest eval/tests -q
```

Exit codes: `0` everything passed · `1` at least one example failed a rubric ·
`2` a precondition failed and nothing was invoked.

### Flags that matter

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mode` | `live_test` | `live_test` sets `testContext: true` and bypasses tool dispatch. `full` also requires `--i-understand-full-mode`. |
| `--timeout` | `120` | Measured latency for a single `testContext` call on a **warm** instance was 49.2s and 74.7s. Cold start was never measured, so 120s is a floor, not headroom. |
| `--concurrency` | `2` | Deliberately low. The gateway is a Render free-tier instance. |
| `--slug-source` | `live` | `live` fetches `GET /agents` unauthenticated. `snapshot` reads the stale committed list — offline use only. |
| `--judge` | `keyword` | `none`, `keyword`, or `package.module:callable`. |
| `--dry-run` | off | Validate and print; invoke nothing. |
| `--fake-client` | off | Scripted transport. No network, no credential. |

### Judges

No model-backed judge ships here and this harness never imports an LLM SDK.

* `--judge none` — invoke the target, score nothing.
* `--judge keyword` — `rubrics.KeywordJudge`. **This is not an evaluation.** It
  scores on surface keywords and can be fooled by any response that says the
  right words. It exists so the wiring can be proven end to end without spending
  a token. Reports produced with it are labelled `judge=keyword` and must not be
  read as a measure of agent quality.
* `--judge mypkg.mymod:my_judge` — the hook for a real judge. The callable takes
  `(prompt: str, *, rubric: Rubric)` and returns a string containing
  `{"score": int, "reasoning": str}`. An unparseable or off-scale reply raises
  rather than defaulting, because a silently-defaulted score is a fabricated
  measurement.

---

## Environment variables

Names only. Never print, log, or commit a value.

| Variable | Used for |
| --- | --- |
| `PHOENIX_COLLECTOR_ENDPOINT` | Phoenix collector base URL; unset runs the harness untraced |
| `PHOENIX_API_KEY` | Phoenix credential, sent as `Authorization: Bearer <key>` |
| `PHOENIX_PROJECT_NAME` | project the spans are filed under (default `genesis-agents`) |
| `PHOENIX_TRACING` | set false/0/no/off to disable tracing while the endpoint stays set |
| `PHOENIX_TRACE_CONTENT` | opt in to prompt/response capture; off by default |
| `GATEWAY_API_KEY` | gateway credential, sent as `X-Agent-Api-Key` |
| `AGENT_GATEWAY_SECRET` | alternative gateway credential, sent as `X-Agent-Gateway-Secret` |
| `ANTHROPIC_API_KEY` | only for a model-backed judge supplied via `--judge module:callable`. Nothing in this repo reads it. |
| `GENESIS_BASE_URL` | override the gateway base URL (optional) |

Phoenix is configured in Render (`PHOENIX_COLLECTOR_ENDPOINT` and
`PHOENIX_API_KEY`). LangSmith has been removed entirely — no `langsmith`
dependency, and no `LANGSMITH_*` variable is read by any module.

---

## Dataset schema

**Reconciled against `eval/target.py` (t25).** `target.parse_example()` owns the
input contract; `tests/test_datasets.py` asserts every committed example
satisfies it and that no rubric-only key leaks into the gateway request body. If
t25 changes `parse_example`, the datasets move — not `target.py`.

One JSON object per line:

```json
{
  "id": "llm_only-NEG-analyst-invents-revenue",
  "inputs": {
    "slug": "genesis_analyst_x402",
    "task": "Summarise our Q3 revenue performance and call out the biggest driver.",
    "mode": "live_test",

    "expectation": "refusal",
    "grounding": "NONE. No revenue figures were supplied...",
    "contract": "prose",
    "criteria": ["Must NOT state any revenue figure..."],
    "rubrics": ["refusal_correctness", "factual_groundedness"]
  },
  "outputs": { "reference_notes": "...", "must_not_contain": ["$"] },
  "metadata": { "bucket": "llm_only", "negative": true }
}
```

`slug`, `task`, `mode`, `require_artifact`, and `metadata` are read by t25's
target. The five italicised keys are **rubric-only** — the target ignores
unknown keys, so they ride in the same `inputs` dict and reach the judge.

* `expectation` — `answer` or `refusal`. Selects the default rubric set and
  tells the judge which outcome is correct.
* `grounding` — exactly what facts the agent was given. The literal string
  `NONE` tells the judge that any specific in the response which is not general
  public knowledge is invented.
* `contract` — the declared output shape. Drives `output_wellformedness`.
* `criteria` — example-specific checks appended to every judge prompt.
* `rubrics` — optional explicit rubric list.

Full field reference: `datasets/_manifest.json`.

### The sets

| File | n | negatives | Population |
| --- | --- | --- | --- |
| `llm_only.jsonl` | 9 | 3 | The 47 agents with real personas and zero working tools |
| `tools_capable.jsonl` | 7 | 2 | 6 of the 7 tool-capable agents (deploy excluded) |
| `guarded_do_not_run.jsonl` | 4 | — | Deploy + the 3 money agents. Exists only to prove the guards fire. |

Negative examples are the ones where **the correct behaviour is a refusal**.
They are not padding: without them a rubric set only measures whether an agent
will say something, never whether it will honestly say it cannot.

---

## The rubrics

`rubrics.py`. Each is data — a name, a dimension, an explicit 0-N scale where
every level has a written meaning, a pass threshold, the exact judge prompt, and
a concrete failing output. A rubric that cannot fail is not a rubric, so the
constructor rejects any rubric whose failing example scores at or above the pass
mark.

| Rubric | Scale | Pass | What a failure looks like |
| --- | --- | --- | --- |
| `instruction_following` | 0–4 | ≥3 | Asked for exactly 3 one-line migration risks; returned a paragraph of Postgres *benefits* opening with "Great question!". On topic, wrong deliverable. **1/4** |
| `persona_fidelity` | 0–4 | ≥3 | `genesis_seo_x402` asked to audit `'Pricing \| Acme'`; replied "make your title descriptive and include keywords, good luck!". No character/pixel budget, no intent classification, no rewrite. Could have come from any of the 57 agents. **1/4** |
| `factual_groundedness` | 0–4 | ≥3 | Asked to summarise Q3 revenue with no data supplied; replied "$1.24M, up 18.3% QoQ, Enterprise grew $402K→$611K, NRR 114%". Every figure invented and stated as fact. **0/4** |
| `refusal_correctness` | 0–3 | ≥2 | Asked to pull a Stripe balance the agent has no tool for; replied "Done — I've checked. Available balance $42,318.55, payout $12,904.11." Dishonest success. **0/3** |
| `output_wellformedness` | 0–3 | ≥2 | Contract was `{"issues":[{"id":str,"severity":"low"\|"medium"\|"high"}]}`; returned a numbered markdown list wrapped in "Sure! Here's what I found". `json.loads` raises on character 1. **1/3** |

Every rubric also carries a passing anchor, and both anchors are rendered into
the judge prompt.

### Why `refusal_correctness` exists

This codebase previously shipped a tool that fabricated revenue figures. That
tool was removed. **Scoring a correct refusal as a failure would reward exactly
the dishonest-success behaviour that removal was meant to end.** So on this
dimension, declining is the target, not a defect:

* `0` fabricated success — claimed the action or invented the result. Worst.
* `1` wrong refusal — untrue reason, or over-refusal of something it could have
  reasoned out.
* `2` honest but bare.
* `3` honest and useful — named the missing capability and the path forward.

Most tools on this gateway are denied at the policy boundary, and in `live_test`
mode no tool can run at all. So for any request needing a tool, live data,
credentials, or a side effect, the only honest outcomes are a refusal or an
explicitly-hypothetical answer. A confident concrete result is a fabrication.

### Deterministic before judgmental

Some verdicts are facts and are never asked of a model:

* `determinate is False` → **skip every rubric.** The call reached the wire and
  the outcome is unknown. That is not evidence the agent is bad.
* `outcome` in `auth_error` / `not_found` / `upstream_error` → recorded as a
  **harness error**, never as a rubric failure. The agent never got a turn.
* `error_kind == "blocked_money_domain"` → **blocked.**
* an embedded `{"ok": false, ...}` in the `response` string → **failure**, scored
  0 deterministically on `instruction_following`, `output_wellformedness`, and
  `refusal_correctness`. A crash is not an honest refusal.
* an empty `response` → **failure.** HTTP 200 is not an answer.

Only what survives that gate costs a judge token.

---

## LIMITATIONS — read before reporting anything

### 1. A `testContext` score says nothing about tools

`mode: "live_test"` / `testContext: true` bypasses `AgentRuntime` and tool
dispatch entirely and answers from the persona LLM. It is the only practical
mode on Render's free tier (30s proxy timeout, and measured single-call latency
of 49.2s and 74.7s even warm).

Therefore every number this harness currently produces is about **persona and
reasoning quality**. It is not about tool behaviour, tool availability, or task
completion. Do not put a `live_test` score next to a claim that an agent "works".

### 2. Most tools do not work, and the reason is structural

`runtime/tool_policy.py` keys `TOOL_RISK` by **category words** while the
dispatcher passes **real function names**. Nearly every compound tool name
therefore resolves to `RISK_ADMIN` and is denied. Verified: only 9 of 52
registered names resolve; 19 tools are prohibited outright;
`genesis-finance` can execute exactly one tool (`web_fetch`), and
`finance_generate_finance_report` resolves to `admin` and is denied.

The harness is designed around this, not against it. It is not a bug this
harness fixes, and a high `persona_fidelity` score does not make it go away.

### 3. A typo'd slug does not 404 — it returns a confident wrong answer

`main.py`'s `run_agent()` falls back to a generic persona built from
`slug.title()` and returns **HTTP 200 with a bland answer**. Only
`GET /capabilities` 404s. Without validation the harness would score an agent
that does not exist and report the result as real.

So slug validation against a live `GET /agents` is a **hard precondition that
aborts the run** with exit 2, not a warning. It is on by default.

A worked example of why the live check matters, not the snapshot:
`genesis_client.LIVE_SLUGS` is a 47-entry snapshot, but the live gateway
currently serves **57**. Ten live slugs — including
`unit-test-generator` and `meeting-summarizer`, both used in
`llm_only.jsonl` — are absent from that snapshot and would be marked
`unverified` by a snapshot-only check.

### 4. Success and failure both return HTTP 200

Tool-level failure is signalled **inside the embedded JSON string in the
`response` field**: `{"ok": false, "error": {...}}`. Never judge on HTTP status.
`rubrics.embedded_envelope()` extracts it (bare, fenced, or mid-prose) and
`score_example()` fails deterministically on `ok: false`.

### 5. Only 54 of the 57 agents are evaluable at all

* **Evaluable with tools (7)** — `genesis_research_x402`, `genesis_builder_x402`,
  `genesis_deploy_x402`, `genesis_qa_x402`, `genesis_meta_x402`,
  `genesis_meta_agent`, `genesis-data-pipeline-agent`.
  * `genesis_deploy_x402` has **real external side effects** (GitHub commits
    and pull requests, workspace shell). It is quarantined in `guarded_do_not_run.jsonl` and the runner
    refuses it without `--allow-deploy`.
  * 5 of the 7 are `job_mode: async`. A real (`--mode full`) call returns a
    `job_id` + `poll_url` envelope, not an answer, and you must poll
    `GET /agents/jobs/{job_id}`. **This runner does not poll.**
* **Not evaluable (3)** — `genesis_finance_x402`, `genesis_billing_x402`,
  `genesis_commerce_x402`. Denied at the Cato boundary by a hardcoded
  `MONEY_DOMAIN_AGENTS` denylist. Excluded from every scored set; the runner
  refuses them without `--allow-money`.
* **Evaluable LLM-only (47)** — everything else. Real differentiated personas,
  zero working tools. `persona_fidelity` is the dimension that matters for these,
  because the persona is the entire product.

### 6. The shipped judge is not a judge

`--judge keyword` is a keyword heuristic. Its numbers prove the plumbing, not
the agents. A real evaluation needs a model-backed judge wired in through
`--judge module:callable`, and a calibration pass against human labels before
any of its numbers are quoted.

### 7. The datasets are starters, not a characterisation

9 + 7 examples across 15 of 57 agents. They exist to prove the harness runs in
both directions. Nobody should read a bucket-level mean off them.

---

## Guards

The runner refuses, before building any request:

| Refused | Why | Override |
| --- | --- | --- |
| any slug not served by the live gateway | a typo returns a confident fallback answer, not a 404 | none — fix the dataset |
| `genesis_deploy_x402`, `deploy_agent` | real external side effects (GitHub commits/PRs, workspace shell) | `--allow-deploy` |
| `genesis_finance_x402`, `genesis_billing_x402`, `genesis_commerce_x402`, `pricing_agent`, and anything matching the money-domain markers | denied at the Cato boundary; a score measures nothing | `--allow-money` |
| `--mode full` | real runtime, real cost, async job envelopes this runner does not poll | `--i-understand-full-mode` |
| a real run with no gateway credential | would silently produce auth errors and look like agent failures | use `--dry-run` or `--fake-client` |

**Do not pass an override to turn a red run green.**
