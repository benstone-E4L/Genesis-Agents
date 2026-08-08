"""
cato/audit.py — Append-only, hash-chained audit log for CATO.

Every agent action is written here — never updated, never deleted.
The SHA-256 chain allows tamper detection: verify_chain() walks
every row and recomputes each row_hash from its fields + prev_hash.

Storage: SQLite at {data_dir}/cato.db, table audit_log.

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
import time
from pathlib import Path
from typing import Any, Optional

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
"""

# Migration statements applied after CREATE TABLE so existing v1 databases
# gain the new columns without an error.  SQLite has no ADD COLUMN IF NOT EXISTS,
# so we catch the OperationalError that fires when the column already exists.
_MIGRATIONS = [
    "ALTER TABLE audit_log ADD COLUMN inputs_digest  TEXT",
    "ALTER TABLE audit_log ADD COLUMN outputs_digest TEXT",
    "ALTER TABLE audit_log ADD COLUMN schema_version INTEGER DEFAULT 2",
]

_SENSITIVE_KEYS = frozenset({
    "api_key", "token", "password", "secret", "key", "authorization",
    "bearer", "credential", "passwd", "passphrase",
})

_MAX_OUTPUT_CHARS = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_inputs(inputs: dict) -> dict:
    """Remove any vault keys or sensitive values from inputs before logging."""
    if not isinstance(inputs, dict):
        return {}
    clean: dict = {}
    for k, v in inputs.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            clean[k] = "[REDACTED]"
        else:
            clean[k] = v
    return clean


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


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

        log = AuditLog()
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
        from .platform import get_data_dir
        self._db_path = db_path or (get_data_dir() / "cato.db")
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        """Open (or create) the SQLite database and apply the schema."""
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
        if self._conn is None:
            self.connect()

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
        outputs: truncated to 2000 chars.
        """
        self._ensure_connected()
        assert self._conn is not None

        ts = time.time()
        safe_inputs = _sanitize_inputs(inputs if isinstance(inputs, dict) else {})
        inputs_json = json.dumps(safe_inputs, ensure_ascii=True)

        raw_output = outputs if isinstance(outputs, str) else json.dumps(outputs, ensure_ascii=True)
        outputs_json = _truncate(raw_output)

        # v2: compute digests before inserting so they can be stored and later
        # used for chain verification without touching the raw JSON columns.
        inputs_digest = _digest(inputs_json)
        outputs_digest = _digest(outputs_json)

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
        self._conn.commit()
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
        assert self._conn is not None

        rows = self._conn.execute(
            """
            SELECT action_type, tool_name, cost_cents, error, timestamp
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
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
        assert self._conn is not None

        rows = self._conn.execute(
            """
            SELECT id, session_id, action_type, tool_name, inputs_json,
                   outputs_json, cost_cents, error, timestamp, prev_hash, row_hash
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
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
                writer.writerow(list(r))
            return buf.getvalue()

        # Default: JSONL
        lines: list[str] = []
        for r in rows:
            lines.append(json.dumps(dict(r), ensure_ascii=True))
        return "\n".join(lines)

    def verify_chain(self, session_id: str) -> bool:
        """
        Verify the SHA-256 chain for all rows in *session_id*.

        Returns True if every row_hash matches recomputed value.
        Logs a warning for each mismatch.
        """
        self._ensure_connected()
        assert self._conn is not None

        rows = self._conn.execute(
            """
            SELECT id, session_id, action_type, tool_name, cost_cents,
                   timestamp, prev_hash, row_hash,
                   inputs_json, outputs_json,
                   inputs_digest, outputs_digest
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
            (session_id,),
        ).fetchall()

        ok = True
        for r in rows:
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
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT id, session_id, action_type, tool_name, inputs_json,
                   outputs_json, cost_cents, error, timestamp, prev_hash, row_hash
            FROM audit_log
            WHERE session_id = ?
            ORDER BY id
            """,
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
