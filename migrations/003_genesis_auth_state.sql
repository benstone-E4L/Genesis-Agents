-- Genesis AP2 replay protection and single-use action grants.
--
-- These two tables replace the SQLite file at GENESIS_AUTH_DB_PATH. Both are
-- pure security state: a row's EXISTENCE is the refusal. Neither table may be
-- given ON CONFLICT DO NOTHING semantics anywhere in the application — the
-- unique violation IS the replay signal, and swallowing it re-opens the window.
--
-- Both are swept by expiry rather than retained: a consumed nonce past its
-- clock-skew window can never be replayed successfully anyway, because the
-- envelope timestamp check rejects it first.

BEGIN;

-- One row per (client, nonce) an AP2 envelope has ever spent.
-- runtime/request_auth.py::_consume_nonce inserts exactly once; the PK
-- violation on a second insert is what raises ap2_replay_detected.
CREATE TABLE IF NOT EXISTS genesis_ap2_nonces (
  client_id  text   NOT NULL,
  nonce      text   NOT NULL,
  expires_at bigint NOT NULL,
  consumed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (client_id, nonce)
);

CREATE INDEX IF NOT EXISTS genesis_ap2_nonces_expiry_idx
  ON genesis_ap2_nonces(expires_at);

-- One row per consumed single-use action grant (runtime/action_grants.py).
-- The grant token itself is a signed, payload-bound HMAC; this table is the
-- only thing that makes it SINGLE-use. Losing it turns every outstanding grant
-- into a replayable authorization for a deployment-class tool.
CREATE TABLE IF NOT EXISTS genesis_action_grants (
  jti          text   PRIMARY KEY,
  consumed_at  bigint NOT NULL,
  expires_at   bigint NOT NULL,
  principal_id text,
  tenant_id    text,
  tool         text
);

CREATE INDEX IF NOT EXISTS genesis_action_grants_expiry_idx
  ON genesis_action_grants(expires_at);

-- Ownership of a consumed grant is retained so an audit can answer "which
-- tenant spent this authorization", which the SQLite table could not.
CREATE INDEX IF NOT EXISTS genesis_action_grants_owner_idx
  ON genesis_action_grants(tenant_id, principal_id);

COMMIT;
