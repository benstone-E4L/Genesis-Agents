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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths — mirror the ~/.conduit convention used by audit.py
# ---------------------------------------------------------------------------

_CONDUIT_DATA_DIR = Path.home() / ".conduit"
_DEFAULT_DB_PATH = _CONDUIT_DATA_DIR / "cato.db"
_DEFAULT_ANCHOR_STORE = _CONDUIT_DATA_DIR / "anchors.jsonl"


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

    The underlying audit DB (cato.db) is opened read-only for all query
    operations — this class never writes to it.  Anchor records are written
    to a separate append-only JSONL file.

    Parameters
    ----------
    db_path:
        Path to the SQLite audit database.  Defaults to ~/.conduit/cato.db.
    anchor_store_path:
        Path to the append-only JSONL anchor store.
        Defaults to ~/.conduit/anchors.jsonl.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        anchor_store_path: Optional[Path] = None,
    ) -> None:
        self._db_path: Path = db_path or _DEFAULT_DB_PATH
        self._anchor_store: Path = anchor_store_path or _DEFAULT_ANCHOR_STORE

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_db(self) -> Optional[sqlite3.Connection]:
        """
        Open the audit DB in read-only URI mode.  Returns None (and logs a
        warning) if the file does not exist — callers must handle None.
        """
        if not self._db_path.exists():
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

        conn = self._open_db()
        if conn is None:
            # DB missing — return a valid empty anchor rather than crashing.
            empty_tree = _build_merkle_tree([])
            return {
                "date": date,
                "session_count": 0,
                "action_count": 0,
                "leaf_hashes": [],
                "merkle_root": empty_tree["root"],
                "computed_at": computed_at,
            }

        try:
            rows = self._query_date_rows(conn, date)
        except Exception as exc:
            logger.error("Error querying audit DB for %s: %s", date, exc)
            conn.close()
            empty_tree = _build_merkle_tree([])
            return {
                "date": date,
                "session_count": 0,
                "action_count": 0,
                "leaf_hashes": [],
                "merkle_root": empty_tree["root"],
                "computed_at": computed_at,
            }
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
        self._anchor_store.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(anchor, ensure_ascii=True)
        with self._anchor_store.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.debug("Recorded anchor for %s to %s", anchor.get("date"), self._anchor_store)

    def get_anchor(self, date: str) -> Optional[dict]:
        """
        Return the stored anchor for *date*, or None if not found.

        If the anchor store does not exist, returns None without raising.

        Parameters
        ----------
        date:
            "YYYY-MM-DD" string.
        """
        if not self._anchor_store.exists():
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

    def verify_anchor(self, date: str) -> dict:
        """
        Verify integrity for *date* by recomputing the Merkle root from the
        live DB and comparing it against the stored anchor.

        Returns
        -------
        dict with keys:
            date             — "YYYY-MM-DD"
            stored_root      — root recorded in the anchor store (empty string if none)
            recomputed_root  — root computed right now from the live DB
            match            — True if both roots are equal
            tampered         — True if a stored anchor exists but roots differ
        """
        stored = self.get_anchor(date)
        stored_root: str = stored["merkle_root"] if stored else ""

        live_anchor = self.compute_daily_anchor(date)
        recomputed_root: str = live_anchor["merkle_root"]

        match = stored_root == recomputed_root
        # "tampered" is only meaningful when we have a prior stored root to compare.
        tampered = bool(stored) and not match

        if tampered:
            logger.warning(
                "Anchor MISMATCH for %s: stored=%s recomputed=%s",
                date,
                stored_root[:16] + "...",
                recomputed_root[:16] + "...",
            )
        else:
            logger.debug("Anchor verified for %s: root=%s", date, recomputed_root[:16] + "...")

        return {
            "date": date,
            "stored_root": stored_root,
            "recomputed_root": recomputed_root,
            "match": match,
            "tampered": tampered,
        }

    def list_anchors(self, limit: int = 10) -> list[dict]:
        """
        Return the last *limit* anchors from the store, most-recent first.

        Returns an empty list if the store does not exist or is unreadable.
        """
        if not self._anchor_store.exists():
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
