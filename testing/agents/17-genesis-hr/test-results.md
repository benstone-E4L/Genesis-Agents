# Agent 17 — Genesis HR Agent

## Test Metadata
- **Tested:** 2026-05-23
- **Marketplace slug:** `genesis_hr_x402`
- **Gateway slug:** `onboarding_agent` ⚠️ SLUG DISCREPANCY
- **Primary endpoint:** `POST https://swarmsync-agents.onrender.com/agents/onboarding_agent/run`
- **Alt endpoint:** `POST https://swarmsync-agents.onrender.com/agents/genesis_hr_x402/run`

## Slug Discrepancy Test
| Endpoint | HTTP Status | Result |
|----------|-------------|--------|
| `/agents/onboarding_agent/run` | 200 OK | Returns inner slug `genesis-hr` |
| `/agents/genesis_hr_x402/run` | **200 OK** | Returns inner slug `genesis-hr` |

**Finding:** Both endpoints work via aliasing and resolve to the same canonical bundle: `genesis-hr`.

## Routing Metadata
| Field | Value |
|-------|-------|
| Model | openai/gpt-5-mini |
| Tier | mid |
| Estimated cost | ~$0.001 |
| Quality gate | PASSED |

## Task Executed
Yes — onboarding checklist for new AI agent vendor:
- Account setup steps
- API key generation
- First agent listing walkthrough
- Pricing setup
- Verification steps

## Issues
| Severity | Issue |
|----------|-------|
| Resolved | Slug aliases now resolve to the same inner slug (`genesis-hr`) |
| Medium | Agent self-identified as "ChatGPT" in one run (non-deterministic identity) |
| Low | Identity non-deterministic — some runs say "Genesis onboarding agent", others say "ChatGPT" |

## Scores
- **Execution score: 4/5** (task executed but identity inconsistency)
- **Routing score: 5/5**

## Verdict
**FUNCTIONAL** (alias routes resolve to one canonical HR bundle)
