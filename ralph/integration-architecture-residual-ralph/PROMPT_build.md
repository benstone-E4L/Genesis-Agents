# RALPH BUILD MODE

You are ralph-wiggum-loop operating in BUILD mode. The Ralph workspace is:
`C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents\ralph\integration-architecture-residual-ralph`.

## State Recovery

Read before every action:

1. `.ralph/state.md`
2. `.ralph/progress.md`
3. `.ralph/guardrails.md`
4. `.ralph/errors.log`
5. `IMPLEMENTATION_PLAN.md`
6. `AGENTS.md`
7. The current file in `specs/`

## Repository Routing

- For `CHUNK_1_FINANCEOS_BOUNDARIES`, set the working directory to `C:\Users\Work\Desktop\vault\projects\E4L-FinanceOS\app` and edit only that Git repository.
- For `CHUNK_2_GENESIS_BLOB`, `CHUNK_3_GENESIS_COMPOSIO`, and `CHUNK_4_GENESIS_VERIFY`, set the working directory to `C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents` and edit only that Git repository.
- Before editing and before committing, confirm `git rev-parse --show-toplevel` matches the current chunk and inspect `git status --short` so pre-existing user changes are preserved.

## Your Job This Iteration

1. Find the current chunk and task in state and the implementation plan.
2. Check repository ownership and guardrails.
3. Implement exactly one task, with no adjacent refactor or future-chunk work.
4. Run the focused tests, then the one-line validation gate from `AGENTS.md`.
5. If validation passes, stage only the named current-task files and commit in the owning application repository.
6. Update this workspace's state and append real command/exit proof to progress.
7. If validation fails, record the failure, attempt one scoped fix, and re-run the gate.
8. If the second attempt fails, record a guardrail and emit a blocked promise; do not grind.

The Genesis gate's three exact escrow test deselections are a recorded pre-existing baseline, not permission to broaden exclusions. If current work touches escrow policy, run those three node IDs directly and remove any repaired exclusion before completion.

## Commit Format

Use `{chunk_id}: {task description}`. Never use `git add -A`, `--no-verify`, or stage Ralph/Vault/pre-existing user files with application changes.

## Evidence Rules

- A task or chunk is complete only after the required gate exits 0 at the changed repository's exact HEAD.
- Record command, exit code, test count, and HEAD in `.ralph/progress.md`; never record credentials, signed URLs, or authorization headers.
- Automated/mocked proof is CODE evidence only. It is not live-readiness evidence.
- Do not update Vault state/evidence from a promise alone. Structured Vault evidence may be updated only after real exact-HEAD proof exists and the governing operator explicitly performs that update.

## Signals

After a validated task append `<promise>TASK_COMPLETE</promise>` to `.ralph/progress.md`.

After all tasks in a chunk are validated, append and output the chunk's exact promise from its spec.

After all four chunks are validated with no remaining tasks, append and output `<promise>BUILD COMPLETE</promise>`.

If the same task fails twice, append and output `<promise>BLOCKED: {task} — {failure pattern}</promise>`.
