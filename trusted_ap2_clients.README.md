# Trusted AP2 Clients Registry

## Purpose

`trusted_ap2_clients.json` is the allowlist of external agent frameworks
authorized to deliver signed AP2 envelopes to the Genesis agents gateway
(`POST /agents/{slug}/run`). Each entry binds a stable `client_id` to a
base64-encoded Ed25519 public key, an algorithm tag, a capability set, and
an `enabled` flag.

## Format

The file is a single JSON object:

- `version` — integer schema version (currently `1`).
- `description` — human-readable summary of the file's role.
- `clients` — array of client records. Required fields per record:
  - `client_id` — short, stable, lowercase identifier (e.g. `cato`).
  - `name` — display name.
  - `pubkey_b64` — base64 Ed25519 public key (32 raw bytes encoded).
  - `algorithm` — currently always `ed25519`.
  - `capabilities` — list of gateway scopes the client is allowed to call.
  - `enabled` — boolean kill-switch.
  - `added_at` — ISO-8601 UTC timestamp.
  - `notes` — free-form operator notes.

## Current Auth Model

Today the agents-gateway authenticates each request by comparing the
`X-Agent-Api-Key` header against the `GATEWAY_API_KEY` environment
variable (`apps/agents-gateway/main.py`, `verify_gateway_key()`). The
signed envelope (payload + nonce + RFC3339 timestamp + Ed25519
signature + `X-AP2-Pubkey` sidecar) is transmitted but not yet
verified server-side.

## Forward-Compat Plan

A signature-verification middleware will land alongside the VCAP-AP2
binding rollout. Once active, the middleware will:

1. Look up the `X-AP2-Pubkey` header value in this registry.
2. Reject the request if no entry matches or `enabled` is false.
3. Re-derive the canonical signed bytes and verify the Ed25519
   signature on the envelope using the registered key.
4. Optionally enforce per-client `capabilities` against the route.

See `Protocols/VCAP-AP2-Binding-v1.0-draft.md` for the binding spec and
canonical-bytes definition the middleware will use.

## Operational Notes

- Add new clients by appending a record and bumping nothing else.
- Rotate a key by adding a second record with a new `client_id` suffix
  (e.g. `cato-2`) and disabling the old one rather than mutating it.
- Never commit private keys here; this file is public-key only.
