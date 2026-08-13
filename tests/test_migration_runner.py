"""The migration runner, against a REAL PostgreSQL server.

The runner is the control that stops production schema drift, so its failure
modes are the tests that matter: a drifted checksum, a vanished file, an
out-of-order insert, and a migration that raises halfway through. Each must
refuse loudly and record nothing.

The pure-text tests at the top need no database and always run.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from migrations import runner
from migrations.runner import (
    ChecksumMismatch,
    MigrationError,
    MissingMigrationFile,
    OutOfOrderMigration,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Transaction-control parsing — no database required
# ---------------------------------------------------------------------------


def test_outer_begin_commit_is_stripped_so_the_runner_owns_the_transaction():
    """If the file's own COMMIT survived, the ledger row would land outside it."""
    body = runner.strip_outer_transaction("BEGIN;\nCREATE TABLE t (id int);\nCOMMIT;\n")
    assert body == "CREATE TABLE t (id int);"


def test_leading_comments_do_not_hide_the_outer_begin():
    sql = "-- why this migration exists\n-- second line\nBEGIN;\nSELECT 1;\nCOMMIT;\n"
    assert "BEGIN" not in runner.strip_outer_transaction(sql).upper()


def test_a_migration_that_mentions_begin_in_a_comment_is_accepted():
    """Every migration in this repo documents its transaction handling by name.

    Leading comments are retained deliberately — they stay attached to the
    statement so a server-side error still has its rationale next to it — but no
    executable transaction control may survive.
    """
    sql = "-- the runner strips the outer BEGIN and owns the COMMIT\nBEGIN;\nSELECT 1;\nCOMMIT;\n"
    body = runner.strip_outer_transaction(sql)
    assert body.endswith("SELECT 1;")
    assert "-- the runner strips" in body
    assert "BEGIN;" not in body and "COMMIT;" not in body


def test_a_migration_with_no_transaction_control_is_left_alone():
    assert runner.strip_outer_transaction("CREATE TABLE t (id int);") == "CREATE TABLE t (id int);"


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nSELECT 1;\nCOMMIT;\nBEGIN;\nSELECT 2;\nCOMMIT;\n",
        "BEGIN;\nSELECT 1;\nROLLBACK;\nCOMMIT;\n",
        "BEGIN;\nSAVEPOINT sp;\nSELECT 1;\nCOMMIT;\n",
    ],
)
def test_inner_transaction_control_is_rejected(sql):
    """An inner COMMIT would break the atomic DDL+ledger pairing."""
    with pytest.raises(MigrationError, match="transaction control"):
        runner.strip_outer_transaction(sql)


def test_an_unclosed_transaction_is_rejected():
    with pytest.raises(MigrationError, match="never commits"):
        runner.strip_outer_transaction("BEGIN;\nCREATE TABLE t (id int);\n")


def test_every_shipped_migration_parses():
    """A migration that the runner cannot parse is one that cannot deploy."""
    for migration in runner.discover(REPO_ROOT / "migrations"):
        runner.strip_outer_transaction(migration.sql)


def test_shipped_migrations_are_discovered_in_lexical_order():
    names = [m.filename for m in runner.discover(REPO_ROOT / "migrations")]
    assert names == sorted(names)
    assert names[:4] == [
        "001_genesis_runtime.sql",
        "002_genesis_tenant_ownership.sql",
        "003_genesis_auth_state.sql",
        "004_genesis_audit_chain.sql",
    ]


def test_checksum_is_over_the_raw_file_bytes():
    """So "this file, as committed" is what the ledger pins."""
    path = REPO_ROOT / "migrations" / "001_genesis_runtime.sql"
    import hashlib

    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    migration = next(
        m for m in runner.discover(REPO_ROOT / "migrations") if m.filename == path.name
    )
    assert migration.checksum == expected


# ---------------------------------------------------------------------------
# Reconciliation — no database required
# ---------------------------------------------------------------------------


def _fake(name: str, checksum: str) -> runner.Migration:
    return runner.Migration(filename=name, path=Path(name), checksum=checksum, sql="SELECT 1;")


def test_reconcile_returns_only_unapplied_migrations():
    migrations = [_fake("001_a.sql", "aa"), _fake("002_b.sql", "bb")]
    pending = runner.reconcile(migrations, {"001_a.sql": {"checksum": "aa"}})
    assert [m.filename for m in pending] == ["002_b.sql"]


def test_reconcile_refuses_when_an_applied_migration_was_edited():
    migrations = [_fake("001_a.sql", "CHANGED")]
    with pytest.raises(ChecksumMismatch, match="edited since it was"):
        runner.reconcile(migrations, {"001_a.sql": {"checksum": "aa"}})


def test_reconcile_never_auto_repairs_a_drifted_checksum():
    """Explicitly: refusal, not silent re-application and not a ledger rewrite."""
    ledger = {"001_a.sql": {"checksum": "aa"}}
    with pytest.raises(ChecksumMismatch):
        runner.reconcile([_fake("001_a.sql", "CHANGED")], ledger)
    assert ledger == {"001_a.sql": {"checksum": "aa"}}, "the ledger was mutated"


def test_reconcile_refuses_when_an_applied_migration_file_has_vanished():
    with pytest.raises(MissingMigrationFile, match="003_gone.sql"):
        runner.reconcile([_fake("001_a.sql", "aa")],
                         {"001_a.sql": {"checksum": "aa"}, "003_gone.sql": {"checksum": "cc"}})


def test_reconcile_refuses_a_pending_migration_that_sorts_before_an_applied_one():
    """Out-of-order application yields a schema no fresh database reproduces."""
    migrations = [_fake("001_a.sql", "aa"), _fake("002_late.sql", "bb"), _fake("003_c.sql", "cc")]
    with pytest.raises(OutOfOrderMigration, match="002_late.sql"):
        runner.reconcile(migrations, {"001_a.sql": {"checksum": "aa"},
                                      "003_c.sql": {"checksum": "cc"}})


# ---------------------------------------------------------------------------
# Against a real server
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_db(postgres_url, monkeypatch):
    """A brand-new empty database per test, so migration state is never shared."""
    import psycopg

    name = f"genesis_mig_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = postgres_url.rsplit("/", 1)[0] + "/" + name
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", url)
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "postgres")
    try:
        yield url
    finally:
        monkeypatch.undo()
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _ledger(url: str) -> dict[str, dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        rows = conn.execute(
            f"SELECT filename, checksum, duration_ms FROM {runner.LEDGER_TABLE}"
        ).fetchall()
    return {r["filename"]: dict(r) for r in rows}


def test_migrate_applies_everything_and_records_the_ledger(scratch_db):
    result = runner.migrate()
    assert result.applied == [m.filename for m in runner.discover()]

    ledger = _ledger(scratch_db)
    for migration in runner.discover():
        assert ledger[migration.filename]["checksum"] == migration.checksum
        assert ledger[migration.filename]["duration_ms"] >= 0


def test_migrate_is_idempotent(scratch_db):
    runner.migrate()
    second = runner.migrate()
    assert second.applied == []
    assert len(_ledger(scratch_db)) == len(runner.discover())


def test_check_mode_exits_non_zero_while_pending_and_zero_once_current(scratch_db, capsys):
    assert runner.main(["--check"]) == runner.EXIT_PENDING
    assert "PENDING" in capsys.readouterr().out

    assert runner.main([]) == runner.EXIT_OK
    assert runner.main(["--check"]) == runner.EXIT_OK
    assert "schema is current" in capsys.readouterr().out


def test_a_drifted_checksum_refuses_the_whole_run(scratch_db, tmp_path, capsys):
    """Not just the drifted file: nothing at all is applied."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_a.sql").write_text("BEGIN;\nCREATE TABLE a (id int);\nCOMMIT;\n",
                                         encoding="utf-8")
    runner.migrate(directory)

    # Edit the applied file and add a legitimate new one.
    (directory / "001_a.sql").write_text(
        "BEGIN;\nCREATE TABLE a (id int, extra text);\nCOMMIT;\n", encoding="utf-8"
    )
    (directory / "002_b.sql").write_text("BEGIN;\nCREATE TABLE b (id int);\nCOMMIT;\n",
                                         encoding="utf-8")

    with pytest.raises(ChecksumMismatch):
        runner.migrate(directory)

    ledger = _ledger(scratch_db)
    assert "002_b.sql" not in ledger, "an unrelated migration was applied despite the drift"

    assert runner.main(["--dir", str(directory)]) == runner.EXIT_ERROR
    assert "migration failed" in capsys.readouterr().err


def test_a_failing_migration_records_nothing_and_leaves_no_partial_schema(
    scratch_db, tmp_path
):
    """The DDL and the ledger row commit together or not at all."""
    import psycopg

    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_ok.sql").write_text("BEGIN;\nCREATE TABLE ok_table (id int);\nCOMMIT;\n",
                                          encoding="utf-8")
    # Second statement is invalid, so the first must be rolled back with it.
    (directory / "002_broken.sql").write_text(
        "BEGIN;\nCREATE TABLE half_table (id int);\nSELECT this_function_does_not_exist();\n"
        "COMMIT;\n",
        encoding="utf-8",
    )

    with pytest.raises(psycopg.Error):
        runner.migrate(directory)

    ledger = _ledger(scratch_db)
    assert "001_ok.sql" in ledger, "the migration that succeeded should stay applied"
    assert "002_broken.sql" not in ledger, "a failed migration was recorded as applied"

    with psycopg.connect(scratch_db) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    assert "ok_table" in present
    assert "half_table" not in present, "a rolled-back migration left a table behind"


def test_concurrent_runners_apply_each_migration_exactly_once(scratch_db):
    """Two Render instances running the pre-deploy command at the same time."""
    barrier_results = []

    def run(_: int):
        try:
            return runner.migrate().applied
        except Exception as exc:  # recorded, then asserted against
            return exc

    with ThreadPoolExecutor(max_workers=4) as pool:
        barrier_results = list(pool.map(run, range(4)))

    failures = [r for r in barrier_results if isinstance(r, Exception)]
    assert not failures, failures

    applied_counts: dict[str, int] = {}
    for applied in barrier_results:
        for name in applied:
            applied_counts[name] = applied_counts.get(name, 0) + 1
    assert all(count == 1 for count in applied_counts.values()), applied_counts
    assert len(_ledger(scratch_db)) == len(runner.discover())


def test_status_mode_lists_the_ledger_and_exits_zero(scratch_db, capsys):
    runner.migrate()
    assert runner.main(["--status"]) == runner.EXIT_OK
    out = capsys.readouterr().out
    for migration in runner.discover():
        assert f"applied  {migration.filename}" in out


def test_runner_reports_a_missing_database_url_instead_of_raising(monkeypatch, capsys):
    for name in ("GENESIS_JOB_DATABASE_URL", "DIRECT_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert runner.main(["--check"]) == runner.EXIT_ERROR
    assert "GENESIS_JOB_DATABASE_URL_not_configured" in capsys.readouterr().err


def test_the_documented_cli_entrypoint_exists_and_wires_to_the_runner():
    """`python scripts/migrate.py` is the Render pre-deploy command."""
    source = (REPO_ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert "from migrations.runner import main" in source
    assert (REPO_ROOT / "scripts" / "migrate.py").exists()
