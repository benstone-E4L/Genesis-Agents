"""
anchor_logger.py — Daily Merkle-tree anchor log for the SwarmSync agents-gateway.

Each day's audit rows are hashed into a Merkle tree.  The root is stored in
an append-only JSONL file so any future tampering with the underlying SQLite
audit log can be detected by recomputing the tree and comparing roots.

Usage (CLI):
    python anchor_logger.py anchor [YYYY-MM-DD]   # compute + store anchor for date
    python anchor_logger.py verify YYYY-MM-DD     # verify stored anchor vs live DB
    python anchor_logger.py list                  # show last 10 anchors

Usage (library):
    from anchor_logger import AnchorLogger
    al = AnchorLogger()
    anchor = al.compute_daily_anchor("2026-04-23")
    al.record_anchor(anchor)
    result = al.verify_anchor("2026-04-23")
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime import pg_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths — Genesis-owned; never read an operator's Cato database.
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH: Path | None = None
_DEFAULT_ANCHOR_STORE: Path | None = None


class AnchorError(RuntimeError):
    """The audit chain could not be read, so no anchor can be computed.

    Deliberately NOT degraded to an empty anchor. ``_build_merkle_tree([])``
    returns ``sha256(b"empty")``, a perfectly valid-looking root, and
    ``record_anchor`` would then persist that as the day's authoritative Merkle
    root. Every later verification of that date would compare the real chain
    against a fabrication — reporting TAMPERED when nothing was tampered with,
    or INTACT over a day whose rows were in fact never read. An unreachable
    store must produce no anchor at all.
    """



# ---------------------------------------------------------------------------
# Inline Merkle tree (self-contained — no external imports)
# ---------------------------------------------------------------------------

def _build_merkle_tree(leaf_hashes: list[str]) -> dict:
    """
    Build a binary Merkle tree from a list of SHA-256 leaf hashes.

    Returns:
        {
            "root":   str,             # hex digest of the tree root
            "leaves": list[str],       # original leaf hashes
            "tree":   list[list[str]], # all levels, leaves first
        }

    Empty input returns root = sha256(b"empty") so callers always get a
    deterministic root even for days with no recorded actions.
    """
    if not leaf_hashes:
        return {
            "root": hashlib.sha256(b"empty").hexdigest(),
            "leaves": [],
            "tree": [],
        }

    levels: list[list[str]] = [list(leaf_hashes)]
    current = list(leaf_hashes)

    while len(current) > 1:
        next_level: list[str] = []
        for i in range(0, len(current), 2):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else left  # duplicate odd node
            parent = hashlib.sha256((left + right).encode()).hexdigest()
            next_level.append(parent)
        levels.append(next_level)
        current = next_level

    return {"root": current[0], "leaves": leaf_hashes, "tree": levels}


# ---------------------------------------------------------------------------
# AnchorLogger
# ---------------------------------------------------------------------------

class AnchorLogger:
    """
    Computes, stores, and verifies daily Merkle-tree anchors over the
    agents-gateway audit log.

    The underlying Genesis audit DB is opened read-only for all query
    operations — this class never writes to it.  Anchor records are written
    to a separate append-only JSONL file.

    Parameters
    ----------
    db_path:
        Path to the SQLite audit database. Defaults to ~/.genesis/genesis-audit.db.
    anchor_store_path:
        Path to the append-only JSONL anchor store.
        Defaults to ~/.conduit/anchors.jsonl.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        anchor_store_path: Optional[Path] = None,
    ) -> None:
        # An explicit path is always the file backend (tests, local forensics).
        # Otherwise Postgres, where neither the audit DB file nor the JSONL
        # anchor file is needed — which is what removes the persistent-disk
        # requirement for anchor state on Render.
        self._pg = db_path is None and anchor_store_path is None and pg_store.postgres_selected()
        configured_db = (os.getenv("GENESIS_AUDIT_DB_PATH") or "").strip()
        configured_anchor = (os.getenv("GENESIS_ANCHOR_STORE_PATH") or "").strip()
        if not self._pg:
            if db_path is None and not configured_db:
                raise RuntimeError("GENESIS_AUDIT_DB_PATH or db_path is required")
            if anchor_store_path is None and not configured_anchor:
                raise RuntimeError("GENESIS_ANCHOR_STORE_PATH or anchor_store_path is required")
        self._db_path = db_path or (Path(configured_db) if configured_db else None)
        self._anchor_store = anchor_store_path or (
            Path(configured_anchor) if configured_anchor else None
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query_date_rows_postgres(self, date: str) -> list[dict]:
        """Audit rows for *date* (UTC) from the Postgres chain, in chain order.

        Ordered by id, matching the SQLite path exactly: the Merkle leaves must
        be in append order or the same day's rows produce a different root and
        every stored anchor would look tampered.
        """
        try:
            dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"date must be YYYY-MM-DD, got {date!r}") from exc
        start_ts = dt.timestamp()
        with pg_store.transaction() as cur:
            cur.execute(
                "SELECT session_id, row_hash, timestamp FROM genesis_audit_log "
                "WHERE timestamp >= %s AND timestamp < %s ORDER BY id",
                (start_ts, start_ts + 86400.0),
            )
            return [dict(row) for row in cur.fetchall()]

    def _open_db(self) -> Optional[sqlite3.Connection]:
        """
        Open the audit DB in read-only URI mode.  Returns None (and logs a
        warning) if the file does not exist — callers must handle None.
        """
        if self._db_path is None or not self._db_path.exists():
            logger.warning(
                "Audit DB not found at %s — returning empty result", self._db_path
            )
            return None
        try:
            uri = self._db_path.as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as exc:
            logger.error("Failed to open audit DB at %s: %s", self._db_path, exc)
            return None

    def _query_date_rows(
        self, conn: sqlite3.Connection, date: str
    ) -> list[sqlite3.Row]:
        """
        Return all audit_log rows whose timestamp falls within *date* (UTC).

        date format: "YYYY-MM-DD"
        """
        try:
            dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"date must be YYYY-MM-DD, got {date!r}") from exc

        start_ts = dt.timestamp()
        # End of day = start of next day
        end_ts = start_ts + 86400.0

        rows = conn.execute(
            """
            SELECT session_id, row_hash, timestamp
            FROM   audit_log
            WHERE  timestamp >= ? AND timestamp < ?
            ORDER  BY id
            """,
            (start_ts, end_ts),
        ).fetchall()
        return rows

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_daily_anchor(self, date: Optional[str] = None) -> dict:
        """
        Compute the Merkle-tree anchor for *date*.

        Parameters
        ----------
        date:
            "YYYY-MM-DD" string.  Defaults to today (UTC).

        Returns
        -------
        dict with keys:
            date          — "YYYY-MM-DD"
            session_count — distinct sessions seen on that date
            action_count  — total audit rows on that date
            leaf_hashes   — list of row_hash values used as tree leaves
            merkle_root   — hex digest of the Merkle root
            computed_at   — ISO-8601 UTC timestamp of this computation
        """
        if date is None:
            date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        # Validate format early so callers get a clear error.
        datetime.strptime(date, "%Y-%m-%d")

        computed_at = datetime.now(tz=timezone.utc).isoformat()

        if self._pg:
            try:
                rows = self._query_date_rows_postgres(date)
            except ValueError:
                raise
            except Exception as exc:
                logger.error("Error querying audit chain for %s: %s", date, exc)
                raise AnchorError(
                    f"cannot compute the anchor for {date}: the audit chain is unreadable "
                    f"({type(exc).__name__}: {exc})"
                ) from exc
            leaf_hashes = [row["row_hash"] for row in rows]
            tree = _build_merkle_tree(leaf_hashes)
            return {
                "date": date,
                "session_count": len({row["session_id"] for row in rows}),
                "action_count": len(rows),
                "leaf_hashes": leaf_hashes,
                "merkle_root": tree["root"],
                "computed_at": computed_at,
            }

        conn = self._open_db()
        if conn is None:
            # "No audit database" is not "a day with no actions". Anchoring the
            # empty root here would record a fabrication as that date's truth.
            raise AnchorError(
                f"cannot compute the anchor for {date}: the audit database at "
                f"{self._db_path} is missing or unreadable"
            )

        try:
            rows = self._query_date_rows(conn, date)
        except ValueError:
            raise
        except Exception as exc:
            logger.error("Error querying audit DB for %s: %s", date, exc)
            raise AnchorError(
                f"cannot compute the anchor for {date}: the audit database is unreadable "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        finally:
            conn.close()

        leaf_hashes = [row["row_hash"] for row in rows]
        session_ids = {row["session_id"] for row in rows}
        tree = _build_merkle_tree(leaf_hashes)

        anchor = {
            "date": date,
            "session_count": len(session_ids),
            "action_count": len(rows),
            "leaf_hashes": leaf_hashes,
            "merkle_root": tree["root"],
            "computed_at": computed_at,
        }

        logger.info(
            "Computed anchor for %s: %d actions across %d sessions — root=%s",
            date,
            len(rows),
            len(session_ids),
            tree["root"][:16] + "...",
        )
        return anchor

    def record_anchor(self, anchor: dict) -> None:
        """
        Append *anchor* as a JSON line to the anchor store.

        The file is created (including parent directories) if it does not
        exist.  Existing content is never modified — only appended.

        Parameters
        ----------
        anchor:
            Any dict, but expected to be a value returned by
            ``compute_daily_anchor()``.
        """
        if self._pg:
            # Insert-only, never upserted. A second, differing anchor for a date
            # is itself tamper evidence and must be retainable — the JSONL store
            # this replaces allowed exactly that, and collapsing it to one row
            # per date would destroy the signal.
            with pg_store.transaction() as cur:
                cur.execute(
                    "INSERT INTO genesis_audit_anchors"
                    "(anchor_date, merkle_root, session_count, action_count, leaf_hashes, "
                    " computed_at) VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        str(anchor.get("date") or ""),
                        str(anchor.get("merkle_root") or ""),
                        int(anchor.get("session_count") or 0),
                        int(anchor.get("action_count") or 0),
                        json.dumps(list(anchor.get("leaf_hashes") or [])),
                        str(anchor.get("computed_at") or ""),
                    ),
                )
            logger.debug("Recorded anchor for %s to Postgres", anchor.get("date"))
            return
        assert self._anchor_store is not None
        self._anchor_store.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(anchor, ensure_ascii=True)
        with self._anchor_store.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.debug("Recorded anchor for %s to %s", anchor.get("date"), self._anchor_store)

    @staticmethod
    def _row_to_anchor(row: dict) -> dict:
        """Shape a genesis_audit_anchors row like the JSONL record it replaces."""
        return {
            "date": row["anchor_date"],
            "session_count": int(row["session_count"]),
            "action_count": int(row["action_count"]),
            "leaf_hashes": list(row["leaf_hashes"] or []),
            "merkle_root": row["merkle_root"],
            "computed_at": row["computed_at"],
        }

    def get_anchor(self, date: str) -> Optional[dict]:
        """
        Return the stored anchor for *date*, or None if not found.

        If the anchor store does not exist, returns None without raising.

        Parameters
        ----------
        date:
            "YYYY-MM-DD" string.
        """
        if self._pg:
            # ORDER BY id LIMIT 1 == "first line matching this date", preserving
            # the JSONL reader's semantics rather than silently returning the
            # newest record for a date that was anchored twice.
            try:
                with pg_store.transaction() as cur:
                    cur.execute(
                        "SELECT anchor_date, merkle_root, session_count, action_count, "
                        "leaf_hashes, computed_at FROM genesis_audit_anchors "
                        "WHERE anchor_date = %s ORDER BY id LIMIT 1",
                        (date,),
                    )
                    row = cur.fetchone()
            except Exception as exc:
                logger.error("Failed to read anchor store: %s", exc)
                return None
            return self._row_to_anchor(dict(row)) if row else None

        if self._anchor_store is None or not self._anchor_store.exists():
            logger.debug("Anchor store %s does not exist yet", self._anchor_store)
            return None

        try:
            with self._anchor_store.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed anchor line: %r", line[:80])
                        continue
                    if record.get("date") == date:
                        return record
        except OSError as exc:
            logger.error("Failed to read anchor store %s: %s", self._anchor_store, exc)

        return None

    def _sessions_on(self, date: str) -> list[str]:
        """Distinct session ids whose audit rows fall on *date*, in chain order."""
        if self._pg:
            rows = self._query_date_rows_postgres(date)
        else:
            conn = self._open_db()
            if conn is None:
                return []
            try:
                rows = self._query_date_rows(conn, date)
            finally:
                conn.close()
        seen: list[str] = []
        for row in rows:
            sid = str(row["session_id"])
            if sid not in seen:
                seen.append(sid)
        return seen

    def _broken_sessions(self, date: str) -> list[str]:
        """Sessions on *date* whose rows no longer hash/link to their own chain.

        The Merkle leaves ARE the recorded ``row_hash`` values, so the root is
        blind to any edit that leaves those values alone — rewriting
        ``tool_name`` or a payload digest keeps every leaf, and therefore the
        root, identical. Re-deriving each row's hash from its own fields is the
        only check that sees that edit, so the anchor delegates it to the same
        verifier the audit chain uses rather than claiming an integrity result
        it never computed.
        """
        from audit import AuditLog

        try:
            log = AuditLog(self._db_path)
            log.connect()
        except Exception as exc:
            # Unverifiable is not verified. Naming every in-scope session broken
            # makes `verify_anchor` fail closed instead of reporting a clean bill
            # of health it could not establish.
            logger.error("Cannot re-verify audit rows for %s: %s", date, exc)
            return self._sessions_on(date)
        broken = []
        for session_id in self._sessions_on(date):
            try:
                if not log.verify_chain(session_id):
                    broken.append(session_id)
            except Exception as exc:
                logger.error("verify_chain raised for session %s: %s", session_id, exc)
                broken.append(session_id)
        return broken

    def verify_anchor(self, date: str) -> dict:
        """
        Verify integrity for *date* with two independent checks: the stored
        Merkle root against a freshly computed one, AND every in-scope audit
        row against its own recorded hash.

        Returns
        -------
        dict with keys:
            date             — "YYYY-MM-DD"
            stored_root      — root recorded in the anchor store (empty string if none)
            recomputed_root  — root computed right now from the live DB
            anchored         — True if a stored anchor exists for this date at all
            rows_intact      — True if every session's rows still hash and link
            broken_sessions  — sessions that failed that re-verification
            match            — True if an anchor exists, the roots are equal,
                               AND the rows themselves are intact
            tampered         — True if a stored anchor exists and either check failed

        `anchored` is separate from `tampered` on purpose: "no anchor was ever
        recorded" is not evidence of integrity, and reporting it as `tampered:
        False` invited exactly that reading.

        The root check alone is NOT sufficient and never was. Leaves are the
        recorded ``row_hash`` values, so an attacker who edits a row and leaves
        its hash alone changes nothing the root can see: the tree still
        reproduces bit for bit while the evidence underneath it has been
        rewritten. `rows_intact` is what covers that case.
        """
        stored = self.get_anchor(date)
        stored_root: str = stored["merkle_root"] if stored else ""

        live_anchor = self.compute_daily_anchor(date)
        recomputed_root: str = live_anchor["merkle_root"]

        anchored = bool(stored)
        broken_sessions = self._broken_sessions(date)
        rows_intact = not broken_sessions
        # `match` requires an actual stored root: "" == "" would otherwise make a
        # date with no anchor and no rows look verified.
        roots_match = anchored and stored_root == recomputed_root
        match = roots_match and rows_intact
        # "tampered" is only meaningful when we have a prior stored root to compare.
        tampered = anchored and not match

        if tampered:
            logger.warning(
                "Anchor MISMATCH for %s: stored=%s recomputed=%s rows_intact=%s broken=%s",
                date,
                stored_root[:16] + "...",
                recomputed_root[:16] + "...",
                rows_intact,
                ",".join(broken_sessions) or "-",
            )
        else:
            logger.debug("Anchor verified for %s: root=%s", date, recomputed_root[:16] + "...")

        return {
            "date": date,
            "anchored": anchored,
            "stored_root": stored_root,
            "recomputed_root": recomputed_root,
            "rows_intact": rows_intact,
            "broken_sessions": broken_sessions,
            "match": match,
            "tampered": tampered,
        }

    def list_anchors(self, limit: int = 10) -> list[dict]:
        """
        Return the last *limit* anchors from the store, most-recent first.

        Returns an empty list if the store does not exist or is unreadable.
        """
        if self._pg:
            try:
                with pg_store.transaction() as cur:
                    cur.execute(
                        "SELECT anchor_date, merkle_root, session_count, action_count, "
                        "leaf_hashes, computed_at FROM genesis_audit_anchors "
                        "ORDER BY id DESC LIMIT %s",
                        (int(limit),),
                    )
                    return [self._row_to_anchor(dict(row)) for row in cur.fetchall()]
            except Exception as exc:
                logger.error("Failed to read anchor store: %s", exc)
                return []

        if self._anchor_store is None or not self._anchor_store.exists():
            return []

        records: list[dict] = []
        try:
            with self._anchor_store.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed anchor line: %r", line[:80])
        except OSError as exc:
            logger.error("Failed to read anchor store %s: %s", self._anchor_store, exc)
            return []

        # Return tail of file (most recent entries), newest first.
        return list(reversed(records[-limit:]))


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    cmd = sys.argv[1] if len(sys.argv) > 1 else "anchor"
    al = AnchorLogger()

    if cmd == "anchor":
        date_arg: Optional[str] = sys.argv[2] if len(sys.argv) > 2 else None
        anchor = al.compute_daily_anchor(date_arg)
        al.record_anchor(anchor)
        print(
            f"Anchored {anchor['action_count']} actions "
            f"({anchor['session_count']} sessions) "
            f"for {anchor['date']}: {anchor['merkle_root']}"
        )

    elif cmd == "verify":
        if len(sys.argv) < 3:
            print("Usage: python anchor_logger.py verify YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        result = al.verify_anchor(sys.argv[2])
        if not result["anchored"]:
            status = "UNANCHORED"
        else:
            status = "INTACT" if result["match"] else "TAMPERED"
        stored_prefix = result["stored_root"][:16] + "..." if result["stored_root"] else "(none)"
        recomputed_prefix = result["recomputed_root"][:16] + "..."
        print(
            f"{status} — {result['date']}: "
            f"stored={stored_prefix} "
            f"recomputed={recomputed_prefix}"
        )
        if result["tampered"]:
            sys.exit(2)
        if not result["anchored"]:
            # Never exit 0 on "we have nothing to compare against" — that is the
            # answer a monitoring script would read as a clean bill of health.
            sys.exit(3)

    elif cmd == "list":
        anchors = al.list_anchors(limit=10)
        if not anchors:
            print("No anchors recorded yet.")
        else:
            print(f"{'Date':<12}  {'Actions':>7}  {'Sessions':>8}  {'Root (first 20)'}")
            print("-" * 60)
            for a in anchors:
                root_preview = a.get("merkle_root", "")[:20] + "..."
                print(
                    f"{a.get('date','?'):<12}  "
                    f"{a.get('action_count', 0):>7}  "
                    f"{a.get('session_count', 0):>8}  "
                    f"{root_preview}"
                )

    else:
        print(
            "Usage:\n"
            "  python anchor_logger.py anchor [YYYY-MM-DD]   # compute + store\n"
            "  python anchor_logger.py verify YYYY-MM-DD     # verify integrity\n"
            "  python anchor_logger.py list                  # last 10 anchors",
            file=sys.stderr,
        )
        sys.exit(1)
