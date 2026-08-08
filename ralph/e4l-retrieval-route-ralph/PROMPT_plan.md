# RALPH PLANNING MODE

You are ralph-wiggum-loop operating in PLANNING mode.

## Your Only Job This Iteration

Read the specs and produce IMPLEMENTATION_PLAN.md.
Do NOT write any application code. Do NOT write any tests.
Do NOT create any files other than IMPLEMENTATION_PLAN.md.

## Project Context

Project: e4l-retrieval-route
Stack: Python 3.12 + FastAPI + PostgreSQL (psycopg3, no ORM)
Output directory: current working directory (this is `ralph/e4l-retrieval-route-ralph/` inside
the Genesis Agents repo — but the code you plan lands in the **repo root**, one level up:
`main.py`, `agent_loader.py`, `bundle_loader.py`, `CLAUDE.md`, new `retrieval_route.py` /
`retrieval_store.py` / `test_retrieval_route.py` all live at the repo root, not inside this
ralph workspace)

## Read These Files First

1. AGENTS.md — build commands and validation gate
2. specs/*.md — one file per chunk (read all five)
3. .ralph/guardrails.md — known risks and scope exclusions, including the explicit
   do-not-build list (Azure/Phoenix migration, AP2 signature verification, Trigger.dev stub
   fix, eval/ vs evals/ resolution — none of those are this workstream's job)

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

- Every chunk from specs/ must appear in the plan (CHUNK_1_CLEANUP, CHUNK_2_REGISTRY,
  CHUNK_3_DOCS, CHUNK_4_RETRIEVAL, CHUNK_5_TESTS — in that exact order, each depends on the one before it).
- Tasks must be specific enough that a junior developer could execute them without clarification.
- Do not include tasks outside the specs. Scope creep is forbidden — especially anything from
  the do-not-build list in guardrails.md.
- Do not generate code. Generate task descriptions only.
- When done writing IMPLEMENTATION_PLAN.md, stop. Do not proceed to build.

## Completion Signal

When IMPLEMENTATION_PLAN.md is written, append to .ralph/progress.md:
```
[{ISO_TIMESTAMP}] Planning complete — IMPLEMENTATION_PLAN.md written (5 chunks, {M} tasks)
<promise>PLANNING_COMPLETE</promise>
```
Then also output the same promise tag and stop.
