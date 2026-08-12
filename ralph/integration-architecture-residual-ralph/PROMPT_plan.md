# RALPH PLANNING MODE

You are ralph-wiggum-loop operating in PLANNING mode from this workspace:
`C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents\ralph\integration-architecture-residual-ralph`.

## Your Only Job This Iteration

Read the governing specification and all four chunk specs, then produce `IMPLEMENTATION_PLAN.md` in this workspace. Do not write application code or tests. Do not create any other new file.

## Read These Files First

1. `SPEC-integration-architecture-final-stack.md` — governing residual specification.
2. `AGENTS.md` — repository ownership and the single-line validation gate.
3. `specs/*.md` — read all four in order.
4. `.ralph/guardrails.md` — risks and excluded work.

## Required Chunk Order and Ownership

1. `CHUNK_1_FINANCEOS_BOUNDARIES` — FinanceOS repository only.
2. `CHUNK_2_GENESIS_BLOB` — Genesis repository only; independently buildable.
3. `CHUNK_3_GENESIS_COMPOSIO` — Genesis repository only; independently buildable.
4. `CHUNK_4_GENESIS_VERIFY` — Genesis repository only; depends on chunks 2 and 3.

Four chunks are intentional. Do not invent a fifth chunk or absorb operational/live gates into CODE work.

## Produce: IMPLEMENTATION_PLAN.md

For every chunk include:

- Exact owning repository and working directory.
- Ordered, file-specific tasks suitable for one bounded implementation pass.
- Explicit tests and structural checks derived from that chunk spec.
- The full validation gate from `AGENTS.md`, expected exit 0, and the exact completion promise.
- Preserve only the three exact Genesis escrow baseline deselections already named in `AGENTS.md`; do not deselect their file, class, or any additional test.
- A scoped staging list that excludes pre-existing user changes and Ralph workspace files.

## Rules

- Every chunk must appear exactly once, in order.
- A task may modify only its chunk's owning repository.
- Do not duplicate completed FinanceOS integrations or create Document Intelligence work.
- Do not turn operational credential provisioning or live smoke tests into code tasks.
- Do not add work outside the governing spec.
- Do not write code. When `IMPLEMENTATION_PLAN.md` is complete, append the planning result to `.ralph/progress.md`, emit `<promise>PLANNING COMPLETE</promise>`, and stop.
