-- Genesis audit chain heads — truncation detection for the tamper-evident log.
--
-- The problem this fixes
-- ----------------------
-- audit.py::verify_chain() recomputes every row_hash and checks that each row's
-- prev_hash equals the preceding row_hash. Both checks pass on a chain that has
-- had rows removed from the END: rows 1..N-1 still hash correctly and still link
-- correctly, so a truncated chain reports INTACT. Deleting an entire session is
-- worse — verify_chain() walks zero rows and returns True. A tamper-evident log
-- that cannot detect deletion only proves what was not edited, never what was
-- not removed, and that is a P0 for anything the chain is meant to underwrite.
--
-- The fix
-- -------
-- One row per session recording the chain's terminal state: how many rows there
-- should be, the id of the last one and its row_hash. It is written inside the
-- SAME transaction as the append, under the SAME per-session advisory lock, so
-- head and chain cannot disagree because of a crash or a concurrent writer.
--
-- What it does and does not prove
-- -------------------------------
-- It detects deletion, truncation and silent row loss (failed restore, partial
-- replication, a DELETE with a bad WHERE). It does not defeat an attacker with
-- write access to this table who also updates the head consistently — nothing
-- inside one database can. That attacker is what genesis_audit_anchors and the
-- daily Merkle root exist for. The realistic 3 AM failure is accidental loss,
-- and this is what turns that from silent into loud.
--
-- Pre-existing chains have no head row. verify_chain() treats a missing head as
-- "legacy chain, verify as before" rather than as tampering, so applying this
-- migration cannot retroactively invalidate a chain written before it.

BEGIN;

CREATE TABLE IF NOT EXISTS genesis_audit_chain_heads (
  session_id    text        PRIMARY KEY,
  last_row_id   bigint      NOT NULL,
  last_row_hash text        NOT NULL,
  row_count     bigint      NOT NULL,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMIT;
