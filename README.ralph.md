# README.ralph.md — Quick Start for ralph-wiggum-loop

## Project

genesis-agents-azure — Genesis Agents Render → Azure ACA + Phoenix (CODE slice)
Generated: 2026-08-05 from `specs/SPEC-genesis-azure-phoenix-migration.md`

## Chunks (5)

- CHUNK_1_DOCKER — Dockerfile + container health
- CHUNK_2_HOSTING — Remove Render/onrender.com + /var/data coupling
- CHUNK_3_PHOENIX — LangSmith → Phoenix in eval/traceable
- CHUNK_4_TESTS — Money-path, sandbox, Phoenix-safe harness tests
- CHUNK_5_CONFIG — .env.example + operator docs

## Prerequisites

- Python 3.12
- pip, pytest, docker (for image build)
- Optional: live Postgres for full gateway tests (not required for validation gate)

## Setup

```bash
cd "C:\Users\Work\Desktop\vault\projects\My Github\Genesis Agents"
pip install -r requirements.txt
pip install -r eval/requirements.txt pytest
cp .env.example .env
```

Read `.ralph/HUMAN_GATES.md` before claiming production cutover done.

## Start ralph

```bash
# 1. Planning (once)
cat PROMPT_plan.md | claude

# 2. Verify plan
cat IMPLEMENTATION_PLAN.md

# 3. Build loop (external terminal)
# ralph-loop.ps1 -Auto -MaxIterations 25
```

## Validation gate

```bash
python -m compileall -q main.py eval runtime tools && pytest tests/test_prohibited_tools.py tests/test_escrow_containment.py test_sandbox_manager.py eval/tests/test_degradation.py eval/tests/test_secret_redaction.py -q
```

## Warnings

- Azure ACA provision / DNS / Key Vault = HUMAN_GATES (not ralph tasks).
- FinanceOS migration runs in separate repo ralph workspace.
- Phase 1 single replica — do not scale ACA until in-memory stores addressed.
- Parent SPEC: `Energy 4 Life\E4L Finance OS\specs\SPEC-azure-phoenix-infrastructure-migration.md`
