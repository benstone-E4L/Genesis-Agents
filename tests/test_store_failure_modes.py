"""Failure-mode regressions for the Postgres stores, the migration runner,
the anchor log and the Phoenix redaction path.

Every test here was written against the pre-fix implementation and failed.
Grouped by the failure it pins:

* an operation that blocks forever instead of refusing (no statement timeout);
* one process opening unbounded connections until the server refuses everyone;
* a line-ending difference between a Windows checkout and a Linux deploy being
  reported as schema tampering, which blocks every migration;
* the migration runner continuing after its advisory-lock connection died;
* the anchor log recording ``sha256("empty")`` as a day's authoritative Merkle
  root when the store was simply unreachable, and then reporting INTACT for a
  date that was never anchored at all;
* an exception raised *inside* redaction printing the unredacted original.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from runtime import pg_store


# ---------------------------------------------------------------------------
# Bounded blocking
# ---------------------------------------------------------------------------


def test_transactions_carry_a_bounded_statement_timeout(pg_env):
    """An unbounded statement can pin a request thread until the process dies."""
    with pg_store.transaction() as cur:
        cur.execute("SHOW statement_timeout")
        value = cur.fetchone()["statement_timeout"]
    assert value not in ("0", "0ms"), "statement_timeout is unlimited"


def test_an_append_starved_of_the_chain_lock_refuses_instead_of_hanging(
    pg_env, monkeypatch
):
    """Advisory-lock starvation must surface as a refusal, not an infinite wait.

    A stuck writer holding the per-session chain lock used to block every
    subsequent append on that session forever: no statement timeout, no
    cancellation, one wedged thread per attempt until the worker pool was gone.
    """
    monkeypatch.setenv("GENESIS_DB_STATEMENT_TIMEOUT_MS", "1500")

    from runtime import genesis_audit

    genesis_audit.reset_for_tests()
    log = genesis_audit.get_audit_log()
    key = log._chain_lock_key("starved-session")

    blocker = pg_store.connect(autocommit=True)
    try:
        with blocker.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))

        # Run in a thread with a join deadline: pre-fix this call never returns
        # at all, and a test that hangs the suite proves nothing readable.
        outcome: list[object] = []

        def attempt() -> None:
            try:
                outcome.append(
                    log.log(
                        session_id="starved-session",
                        action_type="tool_call",
                        tool_name="blocked",
                        inputs={},
                        outputs={"ok": True},
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        started = time.monotonic()
        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(timeout=20)
        elapsed = time.monotonic() - started

        assert not worker.is_alive(), (
            f"append still blocked on the chain lock after {elapsed:.1f}s — unbounded wait"
        )
        assert outcome and isinstance(outcome[0], BaseException), (
            "an append that cannot take the chain lock must refuse, not succeed"
        )
    finally:
        try:
            with blocker.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            blocker.close()
        genesis_audit.reset_for_tests()


def test_concurrent_transactions_are_capped_rather_than_exhausting_the_server(
    pg_env, monkeypatch
):
    """One process opening a connection per operation is how max_connections dies.

    The cap makes the (N+1)th caller refuse quickly and diagnosably instead of
    racing every other tenant of the same database to the server's limit.
    """
    monkeypatch.setenv("GENESIS_DB_MAX_CONNECTIONS", "2")
    monkeypatch.setenv("GENESIS_DB_ACQUIRE_TIMEOUT_S", "1")
    pg_store.reset_connection_cap_for_tests()

    release = threading.Event()
    holding = threading.Semaphore(0)
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with pg_store.transaction() as cur:
                cur.execute("SELECT 1")
                holding.release()
                release.wait(20)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            holding.release()

    holders = [threading.Thread(target=hold) for _ in range(2)]
    for t in holders:
        t.start()
    try:
        assert holding.acquire(timeout=20)
        assert holding.acquire(timeout=20)
        with pytest.raises(pg_store.StoreUnavailable, match="connection"):
            with pg_store.transaction() as cur:
                cur.execute("SELECT 1")
    finally:
        release.set()
        for t in holders:
            t.join(timeout=20)
        pg_store.reset_connection_cap_for_tests()
    assert errors == []


def test_the_connection_cap_is_released_even_when_the_transaction_fails(
    pg_env, monkeypatch
):
    """A leaked slot would turn a transient error into a permanent outage."""
    monkeypatch.setenv("GENESIS_DB_MAX_CONNECTIONS", "1")
    monkeypatch.setenv("GENESIS_DB_ACQUIRE_TIMEOUT_S", "1")
    pg_store.reset_connection_cap_for_tests()
    try:
        for _ in range(5):
            with pytest.raises(Exception):
                with pg_store.transaction() as cur:
                    cur.execute("SELECT * FROM a_table_that_does_not_exist")
        with pg_store.transaction() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() is not None
    finally:
        pg_store.reset_connection_cap_for_tests()


def test_boot_probe_covers_the_audit_anchor_and_chain_head_tables(pg_env):
    """A database missing 004/005 booted "usable" and failed on first anchor write."""
    with pg_store.transaction() as cur:
        cur.execute(
            "ALTER TABLE genesis_audit_chain_heads RENAME TO genesis_audit_chain_heads_hidden"
        )
    try:
        ok, reason = pg_store.store_is_usable()
        assert ok is False
        assert "genesis_audit_chain_heads" in reason
    finally:
        with pg_store.transaction() as cur:
            cur.execute(
                "ALTER TABLE genesis_audit_chain_heads_hidden RENAME TO genesis_audit_chain_heads"
            )


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def _migration(tmp_path: Path, name: str, body: str, *, crlf: bool = False) -> Path:
    path = tmp_path / name
    data = body.encode("utf-8")
    if crlf:
        data = data.replace(b"\n", b"\r\n")
    path.write_bytes(data)
    return path


def test_a_crlf_checkout_is_not_reported_as_schema_tampering(tmp_path):
    """`core.autocrlf=true` on Windows + LF on Render = two checksums, one file.

    The runner refuses to do ANYTHING on checksum drift, so this turned a line
    ending into a total migration outage with a message accusing the operator of
    editing an applied migration.
    """
    from migrations import runner

    body = "CREATE TABLE IF NOT EXISTS t (id int);\n"
    _migration(tmp_path, "001_a.sql", body, crlf=True)
    lf_checksum = runner.checksum_bytes(body.encode("utf-8"))

    migrations = runner.discover(tmp_path)
    applied = {"001_a.sql": {"checksum": lf_checksum, "applied_at": None, "duration_ms": 0}}

    assert runner.reconcile(migrations, applied) == []


def test_an_lf_checkout_of_a_migration_applied_from_windows_is_also_accepted(tmp_path):
    from migrations import runner

    body = "CREATE TABLE IF NOT EXISTS t (id int);\n"
    _migration(tmp_path, "001_a.sql", body)  # LF on disk
    crlf_checksum = runner.checksum_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    migrations = runner.discover(tmp_path)
    applied = {"001_a.sql": {"checksum": crlf_checksum, "applied_at": None, "duration_ms": 0}}

    assert runner.reconcile(migrations, applied) == []


def test_a_genuine_edit_is_still_fatal(tmp_path):
    """Line-ending tolerance must not become content tolerance."""
    from migrations import runner

    _migration(tmp_path, "001_a.sql", "CREATE TABLE IF NOT EXISTS t (id int);\n")
    original = runner.checksum_bytes(b"CREATE TABLE IF NOT EXISTS t (id bigint);\n")

    migrations = runner.discover(tmp_path)
    applied = {"001_a.sql": {"checksum": original, "applied_at": None, "duration_ms": 0}}

    with pytest.raises(runner.ChecksumMismatch):
        runner.reconcile(migrations, applied)


def test_a_dollar_quoted_function_body_is_not_mistaken_for_transaction_control():
    """`CREATE FUNCTION ... AS $$ BEGIN ... END $$` is normal PL/pgSQL, not a txn."""
    from migrations import runner

    sql = (
        "BEGIN;\n"
        "CREATE OR REPLACE FUNCTION bump() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "  NEW.updated_at = now();\n"
        "  RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "COMMIT;\n"
    )
    body = runner.strip_outer_transaction(sql)
    assert "CREATE OR REPLACE FUNCTION" in body
    assert not body.strip().upper().startswith("BEGIN;")


def test_transaction_control_outside_a_dollar_quote_is_still_rejected():
    from migrations import runner

    sql = (
        "CREATE FUNCTION f() RETURNS void AS $$ BEGIN RETURN; END; $$ LANGUAGE plpgsql;\n"
        "COMMIT;\n"
        "CREATE TABLE t (id int);\n"
    )
    with pytest.raises(runner.MigrationError, match="transaction control"):
        runner.strip_outer_transaction(sql)


def test_migration_aborts_when_the_advisory_lock_connection_dies(pg_env, monkeypatch):
    """Losing the lock releases it — a second deploy can then apply concurrently.

    Continuing as though the lock were still held is the dangerous choice; the
    runner must notice and stop.
    """
    from migrations import runner

    # A live connection that holds the lock is fine.
    conn = pg_store.connect(autocommit=True, statement_timeout_override_ms=0)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (runner.ADVISORY_LOCK_KEY,))
        runner._assert_lock_alive(conn)

        # Released without the runner noticing: it is no longer the sole applier.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (runner.ADVISORY_LOCK_KEY,))
        with pytest.raises(runner.MigrationError, match="advisory lock"):
            runner._assert_lock_alive(conn)
    finally:
        conn.close()

    # A dead connection released the lock the moment it died.
    with pytest.raises(runner.MigrationError, match="advisory lock"):
        runner._assert_lock_alive(conn)


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_anchor_refuses_to_invent_an_empty_root_when_the_store_is_unreachable(
    pg_env, monkeypatch
):
    """The old code swallowed the error and returned root=sha256("empty").

    ``record_anchor`` would then persist that as the day's authoritative root,
    and every later verification of that date compares against a fabrication.
    """
    from anchor_logger import AnchorError, AnchorLogger

    logger_obj = AnchorLogger()

    def boom(*_a, **_k):
        raise pg_store.StoreUnavailable("connection refused")

    monkeypatch.setattr(pg_store, "transaction", boom)
    with pytest.raises(AnchorError):
        logger_obj.compute_daily_anchor("2026-08-13")


def test_anchor_refuses_when_the_sqlite_audit_db_is_missing(tmp_path, monkeypatch):
    from anchor_logger import AnchorError, AnchorLogger

    monkeypatch.setenv("GENESIS_STORE_BACKEND", "sqlite")
    logger_obj = AnchorLogger(
        db_path=tmp_path / "not-there.db", anchor_store_path=tmp_path / "anchors.jsonl"
    )
    with pytest.raises(AnchorError):
        logger_obj.compute_daily_anchor("2026-08-13")


def test_verify_anchor_distinguishes_unanchored_from_intact(pg_env):
    """`tampered=False` for a date that was never anchored reads as "verified"."""
    from anchor_logger import AnchorLogger

    result = AnchorLogger().verify_anchor("1999-01-01")

    assert result["anchored"] is False
    assert result["match"] is False
    assert result["tampered"] is False


def test_verify_anchor_reports_anchored_when_a_root_exists(pg_env):
    from anchor_logger import AnchorLogger

    logger_obj = AnchorLogger()
    logger_obj.record_anchor(logger_obj.compute_daily_anchor("2026-08-13"))

    result = logger_obj.verify_anchor("2026-08-13")

    assert result["anchored"] is True
    assert result["match"] is True


# ---------------------------------------------------------------------------
# Redaction under exception (Phoenix eval path)
# ---------------------------------------------------------------------------


def test_a_redaction_failure_never_prints_the_unredacted_original():
    """`_redacted_copy` called `redact_text` outside a try.

    Raising there raises *from inside* the `except` block that was hiding the
    original, so Python chains them and the traceback prints the original
    exception's message verbatim — the secret the whole function exists to
    remove.
    """
    import traceback

    import eval.traceable as traceable

    def explode(_text: str) -> str:
        raise ValueError("redactor itself failed")

    original = traceable.redact_text
    traceable.redact_text = explode
    try:
        try:
            try:
                raise RuntimeError("api_key=sk-LIVE-SECRET-abcdefghijklmnop")
            except Exception as exc:
                raise traceable._redacted_copy(exc) from None
        except BaseException:
            rendered = traceback.format_exc()
    finally:
        traceable.redact_text = original

    assert "sk-LIVE-SECRET-abcdefghijklmnop" not in rendered
    assert "api_key=" not in rendered


def test_redaction_failure_still_preserves_the_exception_type_name():
    import eval.traceable as traceable

    def explode(_text: str) -> str:
        raise ValueError("redactor itself failed")

    original = traceable.redact_text
    traceable.redact_text = explode
    try:
        rebuilt = traceable._redacted_copy(KeyError("secret-bearing-detail"))
    finally:
        traceable.redact_text = original

    assert "KeyError" in str(rebuilt) or isinstance(rebuilt, KeyError)
    assert "secret-bearing-detail" not in str(rebuilt)


def test_the_redactor_module_is_loaded_once_not_per_span_attribute(monkeypatch):
    """`_load_redactor` compiled and exec'd eval/redaction.py on EVERY call.

    Every span attribute, every recorded error. At any real span volume that is
    a module compile per attribute, and it re-scans os.environ each time.
    """
    from runtime import phoenix_tracing

    phoenix_tracing.reset_redactor_for_tests()
    calls = {"n": 0}
    real = phoenix_tracing._load_redactor_uncached

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(phoenix_tracing, "_load_redactor_uncached", counting)
    for _ in range(10):
        phoenix_tracing._load_redactor()

    assert calls["n"] == 1
    phoenix_tracing.reset_redactor_for_tests()


def test_redact_text_survives_a_secret_being_registered_concurrently():
    """`for secret in _LITERAL_SECRETS` iterated a set another thread mutates."""
    from eval import redaction

    errors: list[BaseException] = []
    stop = threading.Event()
    # The registry is a process-global set and `redact_text` walks ALL of it on
    # every call, so an unbounded churn does not just make this test slow — it
    # makes every later redaction in the same interpreter slow, which is what
    # turned a 3000-line scan into one that never finished. Rotating a fixed
    # pool (add then discard) mutates the set exactly as hard while keeping it
    # small, so the "set changed size during iteration" race is still what is
    # under test.
    pool = [f"rotating-secret-value-{i}" for i in range(64)]
    before = set(redaction._LITERAL_SECRETS)

    def churn() -> None:
        while not stop.is_set():
            for value in pool:
                redaction.register_literal_secret(value)
            with redaction._secrets_lock:
                for value in pool:
                    redaction._LITERAL_SECRETS.discard(value)

    def scan() -> None:
        try:
            for i in range(3000):
                redaction.redact_text(f"ordinary log line {i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    churner = threading.Thread(target=churn, daemon=True)
    churner.start()
    try:
        scanner = threading.Thread(target=scan)
        scanner.start()
        scanner.join(timeout=60)
    finally:
        stop.set()
        churner.join(timeout=10)
        with redaction._secrets_lock:
            redaction._LITERAL_SECRETS.clear()
            redaction._LITERAL_SECRETS.update(before)

    # `errors == []` is trivially true for a scanner that never finished, so the
    # completion check has to come first or the whole test passes vacuously.
    assert not scanner.is_alive(), (
        "the scanner thread never completed — this test proves nothing about the "
        "race, and because the thread is non-daemon it also pins the interpreter "
        "at shutdown, which is why `pytest tests/` printed its summary and then "
        "never exited"
    )
    assert errors == [], f"redact_text raced its own secret registry: {errors[:1]}"
