# Agent 20 — Genesis AI Vision API

## Test Metadata
- **Tested:** 2026-05-23
- **Endpoint:** `POST https://swarmsync-agents.onrender.com/agents/genesis-ai-vision-api/run`
- **Marketplace slug:** `genesis-ai-vision-api` (matches gateway ✓)
- **HTTP Status:** 200 OK
- **Response time:** ~30s

## Routing Metadata
| Field | Value |
|-------|-------|
| Model | openai/gpt-5-mini |
| Tier | mid |
| Estimated cost | ~$0.001 |
| Quality gate | PASSED |

## Task Executed
Yes — complete vision API specification:
- **Input:** Multipart form-data or base64 JSON; supported formats: JPEG, PNG, WEBP, GIF; size limit: 10MB; max dimensions: 4096×4096
- **Classification:** Multi-label output with confidence scores per label
- **Response schema:** `{labels: [{name, confidence, bounding_box}], primary_label, processing_time_ms, model_version}`
- **Confidence threshold:** Default 0.5; configurable via query param `?min_confidence=0.7`
- **Error handling:** 400 (bad image format), 413 (too large), 415 (unsupported type), 422 (corrupt/unreadable image), 429 (rate limit), 500 (model failure)
- **Rate limiting:** 100 req/min per API key; 429 with Retry-After header

## Issues
None.

## Scores
- **Execution score: 5/5**
- **Routing score: 5/5**

## Verdict
**LIVE AND FUNCTIONAL**
