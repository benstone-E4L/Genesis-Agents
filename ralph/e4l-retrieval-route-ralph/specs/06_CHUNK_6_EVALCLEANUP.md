# CHUNK_6_EVALCLEANUP: Delete the stale evals/ directory, leaving eval/ as the one canonical eval harness

## Summary

Resolves the `eval/` vs `evals/` open question this workspace originally deferred. Decision
(`.ralph/guardrails.md`, "RESOLVED (2026-08-08)"): `eval/` (singular) is canonical — actively
maintained, fully tested, matches this repo's current path/layout. `evals/` (plural) is a stale
artifact carried over from a different machine: `evals/reports/latest.json` was generated
`2026-06-13T13:16:06Z` and its own fixture paths are hardcoded to
`C:\Users\Ben\Desktop\Github\Genesis-Agents\...`, not this repo's actual location. This chunk
deletes it outright rather than migrating or merging it — there is nothing in `evals/` not
already superseded by `eval/`'s more complete, currently-maintained harness.

## Acceptance Criteria

- [ ] The `evals/` directory (`run_evals.py`, `graders/`, `reports/`, `tasks/`) is deleted
      entirely from the repo.
- [ ] `git grep -i "evals/"` (excluding `eval/` matches) returns zero hits in any active script,
      CI config, or doc under active maintenance — confirms nothing referenced it.
- [ ] `eval/` (singular) is untouched — no file inside it is modified, moved, or renamed by this
      chunk.
- [ ] `python -m compileall -q .` still passes after the deletion (confirms nothing in the live
      codebase imports anything from `evals/`).
- [ ] All tests pass with zero failures.

## Endpoints / Interfaces

No HTTP endpoints — repo cleanup only.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: `evals/` is gone, `eval/`'s existing test suite (13 files) still passes
  unmodified, `python -m compileall -q .` is clean.
- **Edge case**: if any script outside `evals/` imports something from it (unexpected — grep
  found none during scaffolding, but verify before deleting, not after), stop and report it as a
  guardrails.md finding rather than silently breaking that script.
- **Failure case**: none expected — this is a pure deletion of a directory with no live
  references.
- **Integration**: none — this chunk is independent of CHUNK_1-5's cleanup/registry/docs/
  retrieval work; it can run in any order relative to them.

## Dependencies

- **Requires**: None.
- **Blocks**: None.

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_6_EVALCLEANUP</promise>
