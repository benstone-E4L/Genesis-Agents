"""
Genesis append-only, hash-chained audit log.

Every agent action is written here — never updated, never deleted.
The SHA-256 chain allows tamper detection: verify_chain() walks
every row and recomputes each row_hash from its fields + prev_hash.

Storage: SQLite at GENESIS_DATA_DIR/genesis-audit.db, table audit_log.

Schema v2 (current)
-------------------
Two digest columns were added: inputs_digest and outputs_digest.
Each is sha256(column_bytes).hexdigest() of the corresponding JSON
column at insert time.  _row_hash() now binds those digests — not the
raw JSON — into the chain.  This means inputs_json / outputs_json can
be redacted post-hoc without breaking chain verification, because the
digest columns are left untouched.

v1 rows (inputs_digest IS NULL) are detected in verify_chain() and
verified with the original v1 formula (raw JSON in the payload) so
existing databases continue to verify correctly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sqlite3
import threading
import time
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path
from typing import Any, Optional

from runtime import pg_store

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    action_type     TEXT    NOT NULL,
    tool_name       TEXT    NOT NULL,
    inputs_json     TEXT    NOT NULL,
    outputs_json    TEXT    NOT NULL,
    cost_cents      INTEGER NOT NULL DEFAULT 0,
    error           TEXT    NOT NULL DEFAULT '',
    timestamp       REAL    NOT NULL,
    prev_hash       TEXT    NOT NULL DEFAULT '',
    row_hash        TEXT    NOT NULL DEFAULT '',
    inputs_digest   TEXT,
    outputs_digest  TEXT,
    schema_version  INTEGER DEFAULT 2
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);

-- Terminal state of each session's chain. See migrations/005 for the full
-- rationale: recomputing row_hash and checking linkage both PASS on a chain
-- that has had rows deleted from the end, and a deleted session verifies
-- vacuously, so without this a tamper-evident log cannot detect removal.
CREATE TABLE IF NOT EXISTS audit_chain_heads (
    session_id    TEXT    PRIMARY KEY,
    last_row_id   INTEGER NOT NULL,
    last_row_hash TEXT    NOT NULL,
    row_count     INTEGER NOT NULL,
    updated_at    REAL    NOT NULL DEFAULT 0
);
"""

# Migration statements applied after CREATE TABLE so existing v1 databases
# gain the new columns without an error.  SQLite has no ADD COLUMN IF NOT EXISTS,
# so we catch the OperationalError that fires when the column already exists.
_MIGRATIONS = [
    "ALTER TABLE audit_log ADD COLUMN inputs_digest  TEXT",
    "ALTER TABLE audit_log ADD COLUMN outputs_digest TEXT",
    "ALTER TABLE audit_log ADD COLUMN schema_version INTEGER DEFAULT 2",
]

#: Serialises the SQLite append sequence (read prev → INSERT → UPDATE row_hash →
#: upsert head) across threads in this process. Postgres has
#: ``pg_advisory_xact_lock`` per session; SQLite had nothing, so six threads
#: appending to one session read the same prev_hash and forked the chain. A
#: single process-wide lock is correct and cheap: SQLite is the local-dev and
#: test backend, where cross-session append throughput is not a constraint.
#: ``BEGIN IMMEDIATE`` in the same path covers a second *process* on the file.
_sqlite_append_lock = threading.RLock()

_SENSITIVE_KEYS = frozenset({
    "api_key", "token", "password", "secret", "key", "authorization",
    "bearer", "credential", "passwd", "passphrase",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(value: Any, *, parent_key: str = "") -> Any:
    """Recursively redact nested mappings/lists and credential-like strings."""
    if any(s in parent_key.lower() for s in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, parent_key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, parent_key=parent_key) for v in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            clean_query = []
            for key, item in parse_qsl(parsed.query, keep_blank_values=True):
                clean_query.append((key, "[REDACTED]" if any(s in key.lower() for s in _SENSITIVE_KEYS) else item))
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(clean_query), parsed.fragment))
        lower = value.lower().strip()
        if lower.startswith(("bearer ", "sk-", "sk-ant-")):
            return "[REDACTED]"
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
        return value
    return value


def _sanitize_inputs(inputs: dict) -> dict:
    """Backward-compatible alias for recursive redaction."""
    return _sanitize(inputs) if isinstance(inputs, dict) else {}


def _digest(text: str) -> str:
    """Return sha256 hex digest of *text* encoded as UTF-8. Empty string in → empty string out."""
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_hash(
    row_id: int,
    session_id: str,
    action_type: str,
    tool_name: str,
    cost_cents: int,
    timestamp: float,
    prev_hash: str,
    inputs_digest: str,
    outputs_digest: str,
) -> str:
    """Compute SHA-256 hash for a v2 row.

    The payload binds the pre-computed digests of inputs/outputs rather than
    their raw text.  This allows the raw JSON columns to be redacted without
    invalidating the chain, provided inputs_digest / outputs_digest are kept.
    """
    payload = (
        f"{row_id}:{session_id}:{action_type}:{tool_name}:"
        f"{cost_cents}:{timestamp}:{prev_hash}:"
        f"{inputs_digest}:{outputs_digest}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_hash_v1(
    row_id: int,
    session_id: str,
    action_type: str,
    tool_name: str,
    cost_cents: int,
    timestamp: float,
    prev_hash: str,
    inputs_json: str,
    outputs_json: str,
) -> str:
    """Original v1 hash formula — raw JSON in the payload.  Used only by
    verify_chain() to validate rows written before the schema v2 migration."""
    payload = f"{row_id}:{session_id}:{action_type}:{tool_name}:{cost_cents}:{timestamp}:{prev_hash}:{inputs_json}:{outputs_json}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------

class AuditLog:
    """
    Append-only SQLite audit log with SHA-256 hash chain.

    Usage::

        log = AuditLog(Path("/persistent/genesis-audit.db"))
        log.connect()
        row_id = log.log(
            session_id="sess-001",
            action_type="tool_call",
            tool_name="browser.navigate",
            inputs={"url": "https://example.com"},
            outputs={"title": "Example", "text": "..."},
            cost_cents=1,
        )
        summary = log.session_summary("sess-001")
        ok = log.verify_chain("sess-001")
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        # An explicit db_path is always SQLite — it is how tests and local tools
        # ask for a throwaway file. Otherwise the backend follows pg_store, so a
        # Render deploy with GENESIS_JOB_DATABASE_URL set needs no persistent
        # disk and GENESIS_AUDIT_DB_PATH stops being a production requirement.
        self._pg = db_path is None and pg_store.postgres_selected()
        configured = (os.getenv("GENESIS_AUDIT_DB_PATH") or "").strip()
        if db_path is None and not configured and not self._pg:
            raise RuntimeError(
                "GENESIS_AUDIT_DB_PATH or db_path is required (or configure "
                "GENESIS_JOB_DATABASE_URL to use the Postgres audit chain)"
            )
        self._db_path = db_path or (Path(configured) if configured else None)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def backend(self) -> str:
        return "postgres" if self._pg else "sqlite"

    def connect(self) -> None:
        """Open the store and prove it is usable.

        Under Postgres there is no long-lived connection: every append takes its
        own transaction so an advisory lock can serialise chain writes and be
        released deterministically at commit. ``connect()`` therefore degrades to
        a reachability probe — which is still worth doing at boot, because a
        reachable-but-unmigrated database is the failure this whole change
        exists to stop.
        """
        if self._pg:
            with pg_store.transaction() as cur:
                cur.execute("SELECT 1 FROM genesis_audit_log LIMIT 1")
                cur.fetchall()
            logger.debug("AuditLog connected to Postgres (genesis_audit_log)")
            return
        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # Apply v2 migrations idempotently — SQLite lacks ADD COLUMN IF NOT EXISTS,
        # so we swallow the OperationalError that fires when the column already exists.
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        self._conn.commit()
        logger.debug("AuditLog connected to %s", self._db_path)

    def _ensure_connected(self) -> None:
        if self._pg:
            return
        if self._conn is None:
            self.connect()

    # ------------------------------------------------------------------
    # Postgres backend
    # ------------------------------------------------------------------

    def _chain_lock_key(self, session_id: str) -> int:
        return pg_store.advisory_key(f"genesis:audit:{session_id}")

    def _log_postgres(
        self, session_id: str, action_type: str, tool_name: str, inputs_json: str,
        outputs_json: str, cost_cents: int, error: str, ts: float,
        inputs_digest: str, outputs_digest: str,
    ) -> int:
        """Append one row with the chain serialised per session.

        THE LOCK IS THE POINT. Read-prev / insert / hash / update is a
        read-modify-write on a linked list. Two concurrent writers on the same
        session would both read the same prev_hash and both link to it, forking
        the chain into two branches with no way to prove which is real — a
        tamper-evidence failure that cannot be repaired after the fact.

        ``pg_advisory_xact_lock`` serialises appends per session id and is
        released by COMMIT or ROLLBACK, so a crashed writer cannot wedge the
        chain. Different sessions never contend, so throughput is unaffected.
        SELECT ... FOR UPDATE was rejected as the primary mechanism: the FIRST
        row of a session has nothing to lock, so two concurrent first-appends
        would not conflict at all.
        """
        with pg_store.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (self._chain_lock_key(session_id),))
            cur.execute(
                "SELECT row_hash FROM genesis_audit_log WHERE session_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            previous = cur.fetchone()
            prev_hash = previous["row_hash"] if previous else ""
            cur.execute(
                """
                INSERT INTO genesis_audit_log
                  (session_id, action_type, tool_name, inputs_json, outputs_json,
                   cost_cents, error, timestamp, prev_hash, row_hash,
                   inputs_digest, outputs_digest, schema_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '', %s, %s, 2)
                RETURNING id
                """,
                (session_id, action_type, tool_name, inputs_json, outputs_json,
                 cost_cents, error, ts, prev_hash, inputs_digest, outputs_digest),
            )
            row_id = int(cur.fetchone()["id"])
            rh = _row_hash(
                row_id, session_id, action_type, tool_name, cost_cents, ts, prev_hash,
                inputs_digest, outputs_digest,
            )
            cur.execute("UPDATE genesis_audit_log SET row_hash = %s WHERE id = %s", (rh, row_id))
            # Same transaction, same advisory lock: the head can never disagree
            # with the chain because of a crash between the two writes, and two
            # concurrent appends cannot both increment from the same count.
            cur.execute(
                "INSERT INTO genesis_audit_chain_heads"
                " (session_id, last_row_id, last_row_hash, row_count, updated_at)"
                " VALUES (%s, %s, %s, 1, now())"
                " ON CONFLICT (session_id) DO UPDATE SET"
                "   last_row_id = EXCLUDED.last_row_id,"
                "   last_row_hash = EXCLUDED.last_row_hash,"
                "   row_count = genesis_audit_chain_heads.row_count + 1,"
                "   updated_at = now()",
                (session_id, row_id, rh),
            )
        return row_id

    def _chain_head(self, session_id: str) -> Optional[dict]:
        """Recorded terminal state of this session's chain, or None for a legacy chain."""
        if self._pg:
            with pg_store.transaction() as cur:
                cur.execute(
                    "SELECT last_row_id, last_row_hash, row_count FROM "
                    "genesis_audit_chain_heads WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
            return dict(row) if row else None
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT last_row_id, last_row_hash, row_count FROM audit_chain_heads "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def _rows_postgres(self, session_id: str, columns: str) -> list[dict]:
        with pg_store.transaction() as cur:
            cur.execute(
                f"SELECT {columns} FROM genesis_audit_log WHERE session_id = %s ORDER BY id",
                (session_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def _last_row_hash(self, session_id: str) -> str:
        """Return the row_hash of the most recent row for this session, or ''."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT row_hash FROM audit_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row["row_hash"] if row else ""

    def log(
        self,
        session_id: str,
        action_type: str,
        tool_name: str,
        inputs: Any,
        outputs: Any,
        cost_cents: int = 0,
        error: str = "",
    ) -> int:
        """
        Append one audit row and return its auto-increment id.

        action_type: "tool_call" | "llm_response" | "skill_load" | "error" | "spec_commitment"
        inputs: sanitized — vault keys are redacted automatically.
        outputs: recursively redacted and retained in full.
        """
        self._ensure_connected()

        ts = time.time()
        safe_inputs = _sanitize_inputs(inputs if isinstance(inputs, dict) else {})
        inputs_json = json.dumps(safe_inputs, ensure_ascii=True)

        safe_outputs = _sanitize(outputs)
        raw_output = safe_outputs if isinstance(safe_outputs, str) else json.dumps(safe_outputs, ensure_ascii=True)
        outputs_json = raw_output

        # v2: compute digests before inserting so they can be stored and later
        # used for chain verification without touching the raw JSON columns.
        inputs_digest = _digest(inputs_json)
        outputs_digest = _digest(outputs_json)

        if self._pg:
            return self._log_postgres(
                session_id, action_type, tool_name, inputs_json, outputs_json,
                cost_cents, error, ts, inputs_digest, outputs_digest,
            )

        assert self._conn is not None
        # read-prev / INSERT / UPDATE row_hash / upsert head is a read-modify-
        # write on a linked list and must be atomic, or two writers link to the
        # same prev_hash and fork the chain. The lock serialises threads in this
        # process; BEGIN IMMEDIATE takes SQLite's RESERVED lock up front so a
        # second process blocks instead of interleaving.
        with _sqlite_append_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                prev_hash = self._last_row_hash(session_id)

                # We need the id first — insert a placeholder then update the hash.
                cur = self._conn.execute(
                    """
                    INSERT INTO audit_log
                      (session_id, action_type, tool_name, inputs_json, outputs_json,
                       cost_cents, error, timestamp, prev_hash, row_hash,
                       inputs_digest, outputs_digest, schema_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, action_type, tool_name, inputs_json, outputs_json,
                     cost_cents, error, ts, prev_hash, "",
                     inputs_digest, outputs_digest, 2),
                )
                row_id = cur.lastrowid
                assert row_id is not None

                rh = _row_hash(row_id, session_id, action_type, tool_name, cost_cents, ts, prev_hash, inputs_digest, outputs_digest)
                self._conn.execute(
                    "UPDATE audit_log SET row_hash = ? WHERE id = ?",
                    (rh, row_id),
                )
                self._conn.execute(
                    "INSERT INTO audit_chain_heads"
                    " (session_id, last_row_id, last_row_hash, row_count, updated_at)"
                    " VALUES (?, ?, ?, 1, ?)"
                    " ON CONFLICT(session_id) DO UPDATE SET"
                    "   last_row_id = excluded.last_row_id,"
                    "   last_row_hash = excluded.last_row_hash,"
                    "   row_count = audit_chain_heads.row_count + 1,"
                    "   updated_at = excluded.updated_at",
                    (session_id, row_id, rh, ts),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return row_id

    def log_spec_commitment(
        self,
        session_id: str,
        spec_hash: str,
        request_id: str = "",
    ) -> int:
        """
        Write a spec_commitment chain entry as the first row of a session.

        This anchors the audit chain to a specific task specification before any
        work begins. spec_hash should be sha256(task_spec_json). The chain entry
        proves what was requested before the agent started.

        Returns the row id of the committed entry.
        """
        return self.log(
            session_id=session_id,
            action_type="spec_commitment",
            tool_name="conduit.spec_commitment",
            inputs={"spec_hash": spec_hash, "request_id": request_id},
            outputs={"committed": True},
            cost_cents=0,
        )

    def session_summary(self, session_id: str) -> dict:
        """
        Return aggregate stats for a session.

        Keys: action_count (alias: count), total_cost_cents, errors,
              start_ts, end_ts, tools_used.
        """
        self._ensure_connected()

        columns = "action_type, tool_name, cost_cents, error, timestamp"
        if self._pg:
            rows = self._rows_postgres(session_id, columns)
        else:
            assert self._conn is not None
            rows = self._conn.execute(
                f"SELECT {columns} FROM audit_log WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

        if not rows:
            return {
                "action_count": 0, "count": 0, "total_cost_cents": 0, "errors": 0,
                "start_ts": None, "end_ts": None, "tools_used": [],
            }

        tools_used = sorted({r["tool_name"] for r in rows if r["tool_name"]})
        error_count = sum(1 for r in rows if r["error"])
        total_cost = sum(r["cost_cents"] for r in rows)
        timestamps = [r["timestamp"] for r in rows]
        n = len(rows)

        return {
            "action_count": n,    # canonical name used by audit/receipt/CLI
            "count": n,           # backward-compat alias
            "total_cost_cents": total_cost,
            "errors": error_count,
            "start_ts": min(timestamps),
            "end_ts": max(timestamps),
            "tools_used": tools_used,
        }

    def export_session(self, session_id: str, fmt: str = "jsonl") -> str:
        """
        Export all rows for *session_id* as JSONL or CSV string.

        fmt: "jsonl" | "csv"
        """
        self._ensure_connected()

        columns = (
            "id, session_id, action_type, tool_name, inputs_json, "
            "outputs_json, cost_cents, error, timestamp, prev_hash, row_hash"
        )
        column_names = [name.strip() for name in columns.split(",")]
        if self._pg:
            rows = self._rows_postgres(session_id, columns)
        else:
            assert self._conn is not None
            rows = self._conn.execute(
                f"SELECT {columns} FROM audit_log WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "id", "session_id", "action_type", "tool_name",
                "inputs_json", "outputs_json", "cost_cents", "error",
                "timestamp", "prev_hash", "row_hash",
            ])
            for r in rows:
                # Index by name, not position: a psycopg dict_row and a
                # sqlite3.Row disagree about what list(r) means.
                writer.writerow([r[name] for name in column_names])
            return buf.getvalue()

        # Default: JSONL
        lines: list[str] = []
        for r in rows:
            lines.append(json.dumps(dict(r), ensure_ascii=True))
        return "\n".join(lines)

    def verify_chain(self, session_id: str) -> bool:
        """
        Verify the SHA-256 chain for all rows in *session_id*.

        Two independent checks, both required:

        1. **Row integrity** — every row_hash recomputes from its own fields.
        2. **Linkage** — every row's prev_hash equals the previous row's
           row_hash, and the first row's prev_hash is empty.

        Check 2 is not redundant. Recomputing row_hash alone cannot detect a
        FORKED chain: if two concurrent writers both read the same prev_hash,
        both rows hash correctly against it and check 1 passes on every row
        while the history has silently branched. Under Postgres the per-session
        advisory lock in :meth:`_log_postgres` prevents the fork; this check is
        what proves it, and what would catch a fork written by any other client.

        Returns True if every row passes both. Logs a warning for each failure.
        """
        self._ensure_connected()

        columns = (
            "id, session_id, action_type, tool_name, cost_cents, "
            "timestamp, prev_hash, row_hash, inputs_json, outputs_json, "
            "inputs_digest, outputs_digest"
        )
        if self._pg:
            rows = self._rows_postgres(session_id, columns)
        else:
            assert self._conn is not None
            rows = self._conn.execute(
                f"SELECT {columns} FROM audit_log WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

        ok = True

        # Check 3 — COMPLETENESS. Checks 1 and 2 both pass on a chain whose last
        # rows were deleted (the survivors still hash and still link) and on a
        # session deleted outright (zero rows, nothing to contradict). The head
        # row records how many rows there should be and which row_hash should
        # terminate the chain, and is written in the same transaction as the
        # append. A missing head means a chain written before head tracking
        # existed, which is verified exactly as before rather than condemned.
        head = self._chain_head(session_id)
        if head is not None:
            expected_count = int(head["row_count"])
            if len(rows) != expected_count:
                logger.warning(
                    "AuditLog chain TRUNCATED for session=%s: %d rows present, %d recorded "
                    "by the chain head — rows have been deleted or inserted out of band",
                    session_id, len(rows), expected_count,
                )
                ok = False
            elif rows and (
                str(rows[-1]["row_hash"]) != str(head["last_row_hash"])
                or int(rows[-1]["id"]) != int(head["last_row_id"])
            ):
                logger.warning(
                    "AuditLog chain HEAD MISMATCH for session=%s: terminal row is id=%s "
                    "hash=%s, head records id=%s hash=%s",
                    session_id, rows[-1]["id"], str(rows[-1]["row_hash"])[:16],
                    head["last_row_id"], str(head["last_row_hash"])[:16],
                )
                ok = False

        expected_prev = ""
        for r in rows:
            if r["prev_hash"] != expected_prev:
                logger.warning(
                    "AuditLog chain LINKAGE broken at row id=%s (session=%s): prev_hash does "
                    "not match the preceding row_hash — the chain has been forked or reordered",
                    r["id"], session_id,
                )
                ok = False
            expected_prev = r["row_hash"]
            if r["inputs_digest"] is None:
                # v1 row — use the original formula (raw JSON in payload)
                expected = _row_hash_v1(
                    r["id"], r["session_id"], r["action_type"], r["tool_name"],
                    r["cost_cents"], r["timestamp"], r["prev_hash"],
                    r["inputs_json"], r["outputs_json"],
                )
            else:
                # v2 row — verify using stored digests (not raw JSON)
                expected = _row_hash(
                    r["id"], r["session_id"], r["action_type"], r["tool_name"],
                    r["cost_cents"], r["timestamp"], r["prev_hash"],
                    r["inputs_digest"], r["outputs_digest"],
                )
            if expected != r["row_hash"]:
                logger.warning(
                    "AuditLog chain broken at row id=%s (session=%s)",
                    r["id"], session_id,
                )
                ok = False

        return ok

    def get_session_rows(self, session_id: str) -> list[dict]:
        """
        Return all audit rows for *session_id* as a list of plain dicts.
        Used by ConduitProof to build the exportable bundle.
        """
        self._ensure_connected()
        columns = (
            "id, session_id, action_type, tool_name, inputs_json, "
            "outputs_json, cost_cents, error, timestamp, prev_hash, row_hash"
        )
        if self._pg:
            return self._rows_postgres(session_id, columns)
        assert self._conn is not None
        rows = self._conn.execute(
            f"SELECT {columns} FROM audit_log WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "AuditLog":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()
