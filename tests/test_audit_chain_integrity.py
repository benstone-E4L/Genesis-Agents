"""Hash-chain integrity under failure — the findings that are P0.

``verify_chain`` proved two things before this file existed: every row_hash
recomputes, and every row links to its predecessor. Neither survives contact
with deletion. Removing rows from the END of a chain leaves every remaining row
hashing and linking perfectly, and deleting a whole session leaves nothing to
walk, so both reported INTACT. A tamper-evident log that cannot see deletion
proves only what was not edited.

The other half is the SQLite backend. Postgres serialises appends with a
per-session ``pg_advisory_xact_lock``; SQLite had nothing, so read-prev /
insert / update-hash raced across threads and forked the chain in ordinary
local use.

Each test here fails against the pre-fix implementation.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from audit import AuditLog


def _append(log: AuditLog, session: str, n: int) -> None:
    for i in range(n):
        log.log(
            session_id=session,
            action_type="tool_call",
            tool_name=f"tool.{i}",
            inputs={"i": i},
            outputs={"ok": True},
        )


@pytest.fixture()
def sqlite_log(tmp_path, monkeypatch) -> AuditLog:
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "sqlite")
    log = AuditLog(tmp_path / "audit.db")
    log.connect()
    yield log
    log.close()


# ---------------------------------------------------------------------------
# Truncation — SQLite
# ---------------------------------------------------------------------------


def test_intact_chain_still_verifies(sqlite_log):
    """The head record must not make an honest chain look broken."""
    _append(sqlite_log, "s-ok", 5)
    assert sqlite_log.verify_chain("s-ok") is True


def test_tail_truncation_is_detected(sqlite_log):
    """Delete the last row: every remaining row still hashes and links perfectly."""
    _append(sqlite_log, "s-trunc", 5)
    assert sqlite_log.verify_chain("s-trunc") is True

    conn = sqlite3.connect(str(sqlite_log._db_path))
    conn.execute(
        "DELETE FROM audit_log WHERE id = (SELECT max(id) FROM audit_log WHERE session_id = ?)",
        ("s-trunc",),
    )
    conn.commit()
    conn.close()

    assert sqlite_log.verify_chain("s-trunc") is False


def test_deleting_the_whole_session_is_detected(sqlite_log):
    """Zero rows walked used to mean 'nothing to disprove' — i.e. INTACT."""
    _append(sqlite_log, "s-gone", 3)
    conn = sqlite3.connect(str(sqlite_log._db_path))
    conn.execute("DELETE FROM audit_log WHERE session_id = ?", ("s-gone",))
    conn.commit()
    conn.close()

    assert sqlite_log.verify_chain("s-gone") is False


def test_deleting_a_row_from_the_middle_is_detected(sqlite_log):
    _append(sqlite_log, "s-mid", 5)
    conn = sqlite3.connect(str(sqlite_log._db_path))
    conn.execute(
        "DELETE FROM audit_log WHERE id = (SELECT min(id) + 2 FROM audit_log WHERE session_id = ?)",
        ("s-mid",),
    )
    conn.commit()
    conn.close()

    assert sqlite_log.verify_chain("s-mid") is False


def test_a_session_that_was_never_written_verifies_vacuously(sqlite_log):
    """No head and no rows is honest emptiness, not a truncated chain."""
    assert sqlite_log.verify_chain("never-existed") is True


def test_a_legacy_chain_without_a_head_row_still_verifies(sqlite_log):
    """Applying head tracking must not retroactively condemn existing chains."""
    _append(sqlite_log, "s-legacy", 3)
    conn = sqlite3.connect(str(sqlite_log._db_path))
    conn.execute("DELETE FROM audit_chain_heads WHERE session_id = ?", ("s-legacy",))
    conn.commit()
    conn.close()

    assert sqlite_log.verify_chain("s-legacy") is True


def test_head_row_is_written_in_the_same_transaction_as_the_append(sqlite_log):
    _append(sqlite_log, "s-head", 4)
    conn = sqlite3.connect(str(sqlite_log._db_path))
    conn.row_factory = sqlite3.Row
    head = conn.execute(
        "SELECT * FROM audit_chain_heads WHERE session_id = ?", ("s-head",)
    ).fetchone()
    last = conn.execute(
        "SELECT id, row_hash FROM audit_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        ("s-head",),
    ).fetchone()
    conn.close()

    assert head is not None
    assert head["row_count"] == 4
    assert head["last_row_id"] == last["id"]
    assert head["last_row_hash"] == last["row_hash"]


# ---------------------------------------------------------------------------
# Concurrency — SQLite fork
# ---------------------------------------------------------------------------


def test_concurrent_sqlite_appends_do_not_fork_the_chain(tmp_path, monkeypatch):
    """Six threads, one session. Pre-fix this forked the chain and lost rows.

    read-prev, INSERT, UPDATE row_hash was three statements with nothing
    serialising them, so two threads read the same prev_hash and both linked to
    it. Postgres was protected by pg_advisory_xact_lock; SQLite was not, and
    SQLite is what every local run and the whole test suite uses.
    """
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "sqlite")
    log = AuditLog(tmp_path / "race.db")
    log.connect()

    threads_count, per_thread = 6, 15
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(per_thread):
                log.log(
                    session_id="race",
                    action_type="tool_call",
                    tool_name=f"w{n}.{i}",
                    inputs={},
                    outputs={"ok": True},
                )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"appends failed: {errors[:3]}"
    rows = log.get_session_rows("race")
    assert len(rows) == threads_count * per_thread
    assert log.verify_chain("race") is True
    log.close()


# ---------------------------------------------------------------------------
# Truncation — Postgres (real server only)
# ---------------------------------------------------------------------------


def test_postgres_tail_truncation_is_detected(pg_env):
    from runtime import genesis_audit, pg_store

    log = genesis_audit.get_audit_log()
    _append(log, "pg-trunc", 5)
    assert log.verify_chain("pg-trunc") is True

    with pg_store.transaction() as cur:
        cur.execute(
            "DELETE FROM genesis_audit_log WHERE id = "
            "(SELECT max(id) FROM genesis_audit_log WHERE session_id = %s)",
            ("pg-trunc",),
        )

    assert log.verify_chain("pg-trunc") is False


def test_postgres_whole_session_deletion_is_detected(pg_env):
    from runtime import genesis_audit, pg_store

    log = genesis_audit.get_audit_log()
    _append(log, "pg-gone", 3)

    with pg_store.transaction() as cur:
        cur.execute("DELETE FROM genesis_audit_log WHERE session_id = %s", ("pg-gone",))

    assert log.verify_chain("pg-gone") is False


def test_postgres_head_counts_every_concurrent_append(pg_env):
    """The head counter must be exact under the per-session advisory lock."""
    from concurrent.futures import ThreadPoolExecutor

    from runtime import genesis_audit, pg_store

    log = genesis_audit.get_audit_log()

    def one(i: int) -> int:
        return log.log(
            session_id="pg-race",
            action_type="tool_call",
            tool_name=f"t{i}",
            inputs={},
            outputs={"ok": True},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(24)))

    with pg_store.transaction() as cur:
        cur.execute(
            "SELECT row_count, last_row_hash FROM genesis_audit_chain_heads "
            "WHERE session_id = %s",
            ("pg-race",),
        )
        head = cur.fetchone()

    assert head is not None
    assert head["row_count"] == 24
    assert log.verify_chain("pg-race") is True


def test_postgres_legacy_chain_without_a_head_still_verifies(pg_env):
    from runtime import genesis_audit, pg_store

    log = genesis_audit.get_audit_log()
    _append(log, "pg-legacy", 3)
    with pg_store.transaction() as cur:
        cur.execute(
            "DELETE FROM genesis_audit_chain_heads WHERE session_id = %s", ("pg-legacy",)
        )

    assert log.verify_chain("pg-legacy") is True
