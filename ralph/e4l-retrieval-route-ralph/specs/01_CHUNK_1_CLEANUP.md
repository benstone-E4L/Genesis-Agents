# CHUNK_1_CLEANUP: Remove the dead agent_loader.py registry and its import guard

## Summary

`agent_loader.py` is a 45-slug `AGENT_REGISTRY` that resolves modules under
`PROJECT_ROOT / "agents"`, where `PROJECT_ROOT = Path(__file__).resolve().parents[2]` —
two directories above this repo's own root. That directory does not exist on disk, so
every call to `load_agent()` hits the `except Exception` branch in `importlib.import_module`
and returns `None`. The only call site (`main.py:2007-2035`) already treats a `None` return
as "fall through to the persona router," so removing the module changes zero runtime
behavior — it only removes an always-false code path and its silent-failure log noise.
Confirmed independently by the master spec (§2, §16 item 4) and the architecture-cartographer
audit (`docs/audits/architecture-map-2026-08-07.md`, "Genesis `agent_loader.py` 45-slug
registry | Remove | Path resolves two directories above the repo root; every call returns
`None`"). This chunk comes first because every later chunk in this workstream reads
`main.py` and a smaller, dead-code-free file is less risky to patch around.

## Acceptance Criteria

- [ ] `agent_loader.py` no longer exists in the repo.
- [ ] `main.py:64-70`'s `try: from agent_loader import load_agent ... except Exception: load_agent = None` block is removed entirely (no import, no fallback variable).
- [ ] The call site at `main.py:2007-2035` (`if load_agent is not None and not skip_loaded_agent: ...`) is removed; the persona-router fallback path (`main.py:2036+`, `call_llm_router(...)`) becomes the only path — behavior for every existing `/agents/{slug}/run` call is unchanged (same response for the same request, since `load_agent` always returned `None` before removal too).
- [ ] `skip_loaded_agent` parameter/variable, if it exists solely to gate the removed block, is removed or left as a documented no-op if other code still references it — grep before deleting, do not break an unrelated caller.
- [ ] `python -m compileall -q main.py` passes (no syntax errors from the edit).
- [ ] Existing test suite (money-path guards + any test that hits `/agents/{slug}/run`) still passes unmodified — this is a subtraction, not a behavior change.
- [ ] All tests pass with zero failures.

## Endpoints / Interfaces

No HTTP endpoints — internal dead-code removal only. `POST /agents/{slug}/run` keeps its
existing signature and behavior; only its internal call graph shrinks.

## Database Changes

No schema changes in this chunk.

## Test Scenarios

- **Happy path**: `POST /agents/{slug}/run` for a slug backed by a real skill bundle (e.g. `genesis_meta_agent`) returns the same response shape as before the edit (route now goes straight to the bundle-backed `AgentRuntime` path or persona fallback — never touched `agent_loader` even pre-removal).
- **Edge case**: a slug with no bundle and no persona entry still returns the gateway's existing "unknown agent" error path, unchanged.
- **Failure case**: importing `main.py` with `agent_loader.py` deleted does not raise `ImportError` or any exception at module load time (grep confirms no other file imports `agent_loader`).
- **Integration**: CHUNK_3_DOCS reads the post-cleanup `main.py` to describe the real dispatch path (bundle registry + persona fallback, no third "Python agent loader" path) — this chunk's removal must land before CHUNK_3_DOCS is written so the docs don't describe a path that no longer exists.

## Dependencies

- **Requires**: None
- **Blocks**: CHUNK_3_DOCS (doc description of the dispatch path should reflect the post-cleanup reality)

## Completion Promise

<promise>CHUNK COMPLETE: CHUNK_1_CLEANUP</promise>
