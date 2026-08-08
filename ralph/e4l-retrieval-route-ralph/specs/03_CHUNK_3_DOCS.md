# CHUNK_3_DOCS: Fix the CLAUDE.md agent-count claim and document the count split explicitly

## Summary

`CLAUDE.md:3` currently reads "Standalone FastAPI gateway serving 20 specialised Genesis AI
agents." This is wrong on the repo's own evidence: there are 24 real bundle-backed agents
(`skill_bundles/*.json`), and — independently — only some subset of the 57 slugs catalogued
in `main.py`'s `AGENT_PERSONAS` actually resolve to a bundle via `bundle_loader.py`; the rest
fall through to the unguarded single-turn persona/router path with no tool-loop, no budget
cap, and no escrow check. The master spec (§2, §16 item 4) and the architecture-cartographer
audit both call this out as doc-drift that "feeds directly into build-planning miscounts if
not corrected first." This chunk fixes the number **and** adds a short, explicitly-derived
table so the next person (human or agent) can re-verify the split in under a minute instead
of re-auditing the whole repo — that reproducibility is the actual fix; a new hardcoded
number would just be next year's drift.

## Acceptance Criteria

- [ ] `CLAUDE.md:3`'s "20 specialised Genesis AI agents" is replaced with accurate, non-stale language (e.g. "24 bundle-backed agents; see the Agent Count table below for the catalogue split").
- [ ] `CLAUDE.md` gains a new short section, "Agent Count (verify, don't trust)", containing: (a) the count of files in `skill_bundles/*.json` (bundle-backed agents that exist), (b) the count of `AGENT_PERSONAS` keys in `main.py` (catalogued `/agents` slugs), (c) the count of catalogued slugs whose `bundle_loader.resolve_bundle_slug()` output matches a real file in `skill_bundles/` (guarded, tool-loop-backed agents), (d) the remainder (unguarded, persona-only, no tool-loop/budget/escrow protection) — all four numbers must be the actual counts as of this chunk's completion, taken **after** CHUNK_2_REGISTRY's reconciliation, not the pre-reconciliation snapshot from the audit.
- [ ] The section states, in one sentence, the concrete risk of dispatching finance-adjacent work to an unguarded persona-only slug (matches the master spec's Genesis 21-slug-allowlist risk finding) and points to `.ralph/guardrails.md` in this workstream for the enforced list, so the warning has a durable home even after this ralph workspace is archived — copy the guardrail list into `CLAUDE.md` itself, don't just link to a disposable ralph directory.
- [ ] The counting method is written as a literal, copy-pasteable one-liner (Python or shell) in the doc, e.g. a `python -c "..."` snippet that imports `main.AGENT_PERSONAS` and `bundle_loader`, so re-verifying next month is `python -c "..."`, not "re-read the whole codebase."
- [ ] All four numbers in the new table are cross-checked against a live run of the one-liner before the chunk is marked complete — paste the actual command output into the PR/commit description, do not eyeball the file tree.
- [ ] All tests pass with zero failures (this chunk touches no code, so "tests" = `python -m compileall -q main.py` still passing, confirming no accidental code edits crept in while editing the doc).

## Endpoints / Interfaces

No HTTP endpoints — documentation-only chunk.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: running the documented one-liner reproduces the exact numbers written in `CLAUDE.md`'s new table.
- **Edge case**: if CHUNK_2_REGISTRY was skipped or only partially landed for some reason, this chunk's numbers still reflect actual repo state (never copy CHUNK_2_REGISTRY's *planned* outcome — always re-derive from the live tree).
- **Failure case**: if the one-liner import fails (e.g. `main.py` has a syntax error from an earlier chunk), this chunk is blocked and must not paper over it with a guessed number — write `BLOCKED` per the build prompt's blocked-signal rule instead.
- **Integration**: this chunk's guardrail-list copy into `CLAUDE.md` is read by CHUNK_4_RETRIEVAL and CHUNK_5_TESTS as the canonical "don't dispatch finance-adjacent retrieval-augmented work through an unguarded slug" reference, though the retrieval route itself does not dispatch to Genesis agent slugs at all (it is a standalone read-only route) — the connection is documentation consistency, not a runtime dependency.

## Dependencies

- **Requires**: CHUNK_1_CLEANUP, CHUNK_2_REGISTRY (numbers must reflect both)
- **Blocks**: None (informational; does not block the retrieval-route chunks)

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_3_DOCS</promise>
