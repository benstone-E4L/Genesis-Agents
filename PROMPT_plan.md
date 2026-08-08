# RALPH PLANNING MODE

You are ralph-wiggum-loop operating in PLANNING mode.

## Your Only Job This Iteration

Read the specs and produce IMPLEMENTATION_PLAN.md.
Do NOT write any application code. Do NOT write any tests.
Do NOT create any files other than IMPLEMENTATION_PLAN.md.

## Project Context

Project: genesis-agents-azure
Stack: Python 3.12 + FastAPI + uvicorn → Azure Container Apps + Phoenix
Repo: `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents`

## Read These Files First

1. AGENTS.md — build commands and validation gate
2. specs/01_CHUNK_1_DOCKER.md … specs/05_CHUNK_5_CONFIG.md
3. specs/SPEC-genesis-azure-phoenix-migration.md
4. `.ralph/guardrails.md`
5. `.ralph/HUMAN_GATES.md` — do not plan Azure portal / DNS / Key Vault tasks as code

## Produce: IMPLEMENTATION_PLAN.md

Format:
```
# IMPLEMENTATION_PLAN.md

## Chunk Order
{List chunks in order with one-sentence descriptions}

## Chunk {N}: {chunk_id}
### Tasks (in order)
1. {specific file/function to create or modify}
2. {next task}
...
### Validation
- Command: {validation gate from AGENTS.md}
- Expected: exit 0, all tests green
### Promise
<promise>CHUNK COMPLETE: {chunk_id}</promise>
```

## Rules

- Every chunk from specs/01–05 must appear in the plan.
- Tasks must be specific (file paths, functions).
- Do not plan changes to `runtime/tool_policy.py`, `agent_runtime.py`, or skill bundles.
- Do not plan FinanceOS or Cato repo edits.
- Do not plan Azure resource provisioning — HUMAN_GATES only.
- When done writing IMPLEMENTATION_PLAN.md, stop.

## Completion Signal

When IMPLEMENTATION_PLAN.md is written, append to .ralph/progress.md:
```
[{ISO_TIMESTAMP}] Planning complete — IMPLEMENTATION_PLAN.md written ({N} chunks, {M} tasks)
<promise>PLANNING_COMPLETE</promise>
```
Then also output the same promise tag and stop.
