-- Genesis tamper-evident audit chain and daily Merkle anchors.
--
-- Replaces the SQLite file at GENESIS_AUDIT_DB_PATH and the JSONL file at
-- GENESIS_ANCHOR_STORE_PATH.
--
-- Chain shape is preserved EXACTLY from audit.py so a chain written under
-- either backend verifies with the same formula:
--
--   row_hash = sha256("{id}:{session_id}:{action_type}:{tool_name}:{cost_cents}:
--                      {timestamp}:{prev_hash}:{inputs_digest}:{outputs_digest}")
--
-- prev_hash is the row_hash of the previous row FOR THE SAME SESSION. Appends
-- are serialised per session by a pg_advisory_xact_lock keyed on the session id
-- (see audit.py::_PostgresAuditBackend.log) — without it two concurrent writers
-- both read the same prev_hash and the chain forks, which is unrecoverable
-- because neither branch can be shown to be the real one.
--
-- `timestamp` is double precision, matching Python's float exactly (both are
-- IEEE-754 binary64), so the value that goes into the hash round-trips
-- bit-for-bit. Storing it as timestamptz would round and silently break every
-- row_hash on read-back.

BEGIN;

CREATE TABLE IF NOT EXISTS genesis_audit_log (
  id             bigserial PRIMARY KEY,
  session_id     text             NOT NULL,
  action_type    text             NOT NULL,
  tool_name      text             NOT NULL,
  inputs_json    text             NOT NULL,
  outputs_json   text             NOT NULL,
  cost_cents     integer          NOT NULL DEFAULT 0,
  error          text             NOT NULL DEFAULT '',
  timestamp      double precision NOT NULL,
  prev_hash      text             NOT NULL DEFAULT '',
  row_hash       text             NOT NULL DEFAULT '',
  inputs_digest  text,
  outputs_digest text,
  schema_version integer          NOT NULL DEFAULT 2
);

CREATE INDEX IF NOT EXISTS genesis_audit_log_session_idx
  ON genesis_audit_log(session_id, id);
CREATE INDEX IF NOT EXISTS genesis_audit_log_ts_idx
  ON genesis_audit_log(timestamp);

-- Append-only daily Merkle anchors (anchor_logger.py). Deliberately NOT keyed
-- unique on date: the JSONL store it replaces allowed a date to be anchored
-- more than once, and get_anchor() returns the FIRST record written for a date.
-- Enforcing uniqueness here would change tamper-evidence semantics — a second,
-- differing anchor for the same date is itself evidence and must be retainable.
CREATE TABLE IF NOT EXISTS genesis_audit_anchors (
  id            bigserial PRIMARY KEY,
  anchor_date   text        NOT NULL,
  merkle_root   text        NOT NULL,
  session_count integer     NOT NULL DEFAULT 0,
  action_count  integer     NOT NULL DEFAULT 0,
  leaf_hashes   jsonb       NOT NULL DEFAULT '[]'::jsonb,
  computed_at   text        NOT NULL DEFAULT '',
  recorded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS genesis_audit_anchors_date_idx
  ON genesis_audit_anchors(anchor_date, id);

COMMIT;
