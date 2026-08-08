# Genesis LangSmith evaluation harness

LangSmith instrumentation for the Genesis agent gateway. Additive package —
nothing in `eval/` is imported by the Genesis service, and no existing Genesis
source file was modified to add it.

## Why `@traceable` and not an SDK integration

LangSmith's quickstart offers integrations for the Claude Agent SDK, LangChain,
the OpenAI SDK and so on. **Genesis uses none of them.** Genesis is a FastAPI
service that makes raw HTTP calls to the SwarmSync router at `$LLM_API_URL`, so
`wrap_openai()` / `configure_claude_agent_sdk()` have nothing to hook.

The correct integration is the plain `langsmith` SDK's `@traceable` decorator,
which wraps any Python callable regardless of what is underneath. Do **not**
install the `langsmith[claude-agent-sdk]` extra — it adds a dependency that
instruments a code path Genesis does not have.

## Layout

| file | role |
| --- | --- |
| `genesis_client.py` | async HTTP client for `POST /agents/{slug}/run`; outcome classification, bounded retries, one-shot warmup, slug resolution, money-domain block |
| `traceable.py` | `@traceable` wrapper (`run_type="chain"`, name `genesis.agent.run`), trace metadata, graceful degradation |
| `target.py` | the evaluation target `evaluate()` calls per dataset example |
| `redaction.py` | recursive secret redactor (behaviour copied from Cato `approval_policy.redact()`) |
| `tests/` | full suite; every test runs against a fake transport and a fake LangSmith |

## Environment variables

No values appear in this repo. Set these in your shell or in `.env`.

### LangSmith

| name | purpose |
| --- | --- |
| `LANGSMITH_TRACING` | `true` to send traces. Set to `false`/`0`/`no`/`off` to disable tracing while leaving the key in place. If unset and a key is present, tracing is on. |
| `LANGSMITH_ENDPOINT` | LangSmith API base URL. Only needed for EU or self-hosted deployments; omit for the default US endpoint. Read by the `langsmith` SDK, not by this package. |
| `LANGSMITH_API_KEY` | LangSmith credential. **If absent, tracing is silently skipped and the agent call still runs.** |
| `LANGSMITH_PROJECT` | Project traces are written to. Read by the `langsmith` SDK. |

### Genesis gateway

| name | purpose |
| --- | --- |
| `GATEWAY_API_KEY` | Sent as `X-Agent-Api-Key`. This is the credential `POST /agents/{slug}/run` requires; without it every run returns 401. Verified against `main.py::verify_gateway_key`. |
| `AGENT_GATEWAY_SECRET` | Alternate credential, sent as `X-Agent-Gateway-Secret`. Either header is sufficient. Optional. |

### Judge model

| name | purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Used by the LLM-as-judge evaluators (owned by the rubric side of this harness, not by this package). Nothing in `eval/` calls the Anthropic API. |

Every one of these names is treated as a secret by `redaction.py` — their values
are stripped from any string before it can reach a trace.

## Install

Use the repo-local venv so the shared base interpreter stays untouched:

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r eval/requirements.txt
```

## Run the tests

```bash
./.venv/Scripts/python.exe -m pytest eval/tests -q
```

The suite is **hermetic by construction**. `tests/conftest.py` clears
`LANGSMITH_API_KEY`, `LANGSMITH_ENDPOINT` and `LANGSMITH_PROJECT` and pins
`LANGSMITH_TRACING=false` — both at conftest import time and again in a
session-scoped autouse fixture — so no test can ship a run to the real
LangSmith project no matter what `.env` or the shell holds. A second autouse
fixture fails any test that enables tracing against a non-loopback endpoint.
`tests/test_isolation.py` asserts the guard is in force.

No test performs a network call by default. `POST /agents/{slug}/run` is never
called by the suite. The one opt-in network test (`test_live_catalogue.py`,
gated on `GENESIS_EVAL_LIVE_CHECK=1`) calls only the unauthenticated
`GET /agents`.

## Use

```python
from eval import GenesisClient, make_target

target = make_target(GenesisClient())        # live gateway, env credentials
outputs = target({"slug": "genesis-research", "task": "Summarise ..."})
```

With LangSmith:

```python
from langsmith import evaluate
from eval import genesis_target

evaluate(genesis_target, data="my-dataset", evaluators=[...])
```

For unit tests and for the rubric/dataset agents, inject a transport so no live
gateway is needed:

```python
from eval import GenesisClient, make_target
from eval.tests.fakes import FakeTransport, ok_response

target = make_target(GenesisClient(transport=FakeTransport([ok_response("hi")])))
```

## Input schema (dataset example `inputs`)

| key | required | meaning |
| --- | --- | --- |
| `slug` | yes | Any known spelling; normalised to the live gateway form. |
| `task` | yes | Prompt/instruction. Aliases: `prompt`, `input`, `question`, `query`. |
| `mode` | no | `live_test` (default) or `full`. |
| `require_artifact` | no | bool, forwarded to the gateway. |
| `metadata` | no | dict merged into trace metadata (redacted). |

Unknown keys are ignored, so rubric-only fields (`expected`, `criteria`,
`category`, ...) can live in the same dict.

## Output schema (what evaluators receive)

`response`, `outcome`, `ok`, `determinate`, `slug`, `requested_slug`,
`slug_resolution`, `mode`, `http_status`, `elapsed_ms`, `attempts`,
`error_kind`, `error_message`, `agent_name`.

Full definitions are in the `target.py` module docstring, which is the
authoritative copy.

## Outcome classes

Five, mutually exclusive, on `outputs["outcome"]`:

| value | meaning | retried? |
| --- | --- | --- |
| `success` | 2xx | n/a |
| `auth_error` | 401 / 403 | never |
| `not_found` | 404 — usually the wrong slug form | never |
| `upstream_error` | definitively failed: 5xx after the attempt cap, connect failure, **or any other 4xx** | 5xx and connect failures only |
| `indeterminate` | the request reached the wire and the outcome is **unknown** (read/write timeout after send) | never — a retry could double-invoke |

**Rubrics must skip, not fail, an example where `determinate` is `False`.** An
unknown outcome is not evidence the agent is bad, and treating it as a failure
was a real defect on the Cato side.

### Deliberate deviation: non-auth, non-404 4xx map to `upstream_error`

There are five outcome classes, not six. A `400`, `422` or `429` lands in
`upstream_error` rather than getting its own class, with `error_kind` set to
`client_error_<status>`.

**Rubric authors: do not treat every `upstream_error` as a retryable upstream
failure.** Read `error_kind` to tell them apart:

| `error_kind` | meaning | whose fault |
| --- | --- | --- |
| `server_error_5xx` | gateway or router failed after 3 attempts | the service |
| `transport_connect` | never reached the server after 3 attempts | network / cold start |
| `client_error_422` | the request body was rejected — **the dataset row is malformed** | the dataset |
| `client_error_429` | rate limited — **not retried**, back off at the experiment level | the caller |

A `client_error_*` will never succeed on retry. If one appears, fix the dataset
row or the pacing; do not re-run the example expecting a different result.

## Operational notes

- **Timeouts.** Client default is 60s, deliberately above Render's 30s free-tier
  proxy timeout, so a proxy giving up (a 5xx around 30s) is distinguishable from
  our own client giving up.
- **Cold start.** The first call per client issues one `GET /health` to absorb
  Render cold start. It runs at most once and a failed warmup never blocks the
  run — the first real call just pays the cold start instead.
- **Retries.** Max 3 attempts total (1 initial + 2 retries), exponential backoff
  from 0.5s, capped at 8s, with jitter. Only 5xx and pre-send transport failures
  are retried. **No 4xx is ever retried.**
- **Slug forms.** `skill_bundles/*.json` uses hyphens (`genesis-research`); the
  live gateway serves underscores with an `_x402` suffix
  (`genesis_research_x402`). The client resolves either form to the live one and
  reports which via `slug_resolution` (`verified` / `aliased` / `unverified`).
- **An unknown slug does NOT 404 — it silently succeeds.** `main.py` falls back
  to a generic persona built from `slug.title()` and returns HTTP 200 with a
  bland answer; only `GET /capabilities` 404s. Scoring that would be scoring a
  stub. So `GenesisClient` ships with `strict_slugs=True`: a slug outside the
  57-agent live catalogue is refused before a request is built
  (`UnknownSlug` → `error_kind: "unknown_slug"` at the target level). Pass
  `strict_slugs=False` to send it anyway. `LIVE_SLUGS` is snapshotted from
  `GET /agents`; `LIVE_SLUG_COUNT` pins it at 57 and is asserted at import.
  Reconcile against live with:

  ```bash
  GENESIS_EVAL_LIVE_CHECK=1 ./.venv/Scripts/python.exe -m pytest eval/tests/test_live_catalogue.py -v
  ```
- **`live_test` vs `full` are different measurements.** `live_test` sets
  `mode: "live_test"` + `testContext: true`, which skips AgentRuntime (no
  ConduitBridge startup) and uses the fast persona LLM path — documented as
  required on the free tier. `full` exercises the real runtime and may exceed
  the proxy timeout or return an async job envelope. The mode used is always
  recorded in the outputs and the trace metadata, because it changes what is
  being measured.
- **Money-domain agents are refused.** Any slug matching finance / billing /
  commerce / pricing / escrow / payment / payout / invoice raises
  `MoneyDomainBlocked` before a request is built. Override with
  `GenesisClient(allow_money_domain=True)` only with a deliberate reason.

## Secret safety

`redaction.py` is a recursive redactor whose behaviour is copied from Cato's
`cato/core/approval_policy.py:redact()`. It redacts on the **key**
(`api_key`/`apikey`/`api-key`/`_key`/`private_key`/`session_key`/`authorization`
and friends, at any nesting depth) and on the **value shape** (`sk-…`,
`Bearer …`, JWTs, `AKIA…`, `postgres://user:pass@…`), and it registers the live
values of every credential env var as literals to strip. A bare `key` is left
intact, matching the reference implementation.

Redaction is applied to trace inputs, trace outputs, trace metadata, the result
object, and **exception messages inside the traced scope** — the last one
matters because `@traceable` records a raised exception on the run, so redacting
only in an outer handler leaks. `tests/test_secret_redaction.py` plants one
marker secret in all four positions and asserts it never appears in the
serialised run.
