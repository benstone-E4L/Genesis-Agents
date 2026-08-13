"""Real-PostgreSQL fixtures for the Genesis store tests.

Why this exists
---------------
829 tests passed while every async AP2 dispatch returned HTTP 500 in production.
The reason was that ``test_job_store.py`` drives an in-memory mock connection:
it proves the Python branches, and proves nothing at all about whether the SQL
is valid, whether the columns exist, whether a unique constraint fires, or
whether an advisory lock actually serialises anything. A mock cannot have schema
drift, so a mock can never detect it.

Every test that depends on Postgres semantics — unique violations, advisory
locks, transaction rollback, ``bigserial`` ordering — uses :func:`postgres_url`
and therefore runs against a real server or does not run at all.

Resolution order
----------------
1. ``GENESIS_TEST_DATABASE_URL`` — an already-running database. This is the
   normal way to run these tests, locally and in CI.
2. ``GENESIS_TEST_PROVISION_POSTGRES=1`` — provision a throwaway cluster from
   the local ``initdb``/``pg_ctl`` binaries on a free loopback port and destroy
   it at session end. Opt-in rather than automatic because ``initdb`` costs
   roughly a minute and would triple the default suite's runtime.
3. Otherwise every dependent test **skips with an explicit reason** naming both
   options. It never silently falls back to SQLite or to a mock, because a green
   run that quietly proved nothing is the exact failure being corrected here.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Set when no real server could be reached, and reported verbatim in the skip.
_SKIP_REASON = ""


def _binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in (
        r"C:\Program Files\PostgreSQL\15\bin",
        r"C:\Program Files\PostgreSQL\16\bin",
        r"C:\Program Files\PostgreSQL\17\bin",
    ):
        candidate = Path(base) / f"{name}.exe"
        if candidate.exists():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _reachable(url: str) -> tuple[bool, str]:
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=10) as conn:
            conn.execute("SELECT 1")
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory) -> str:
    """A URL for a REAL PostgreSQL database, or skip with a stated reason."""
    global _SKIP_REASON

    explicit = (os.getenv("GENESIS_TEST_DATABASE_URL") or "").strip()
    if explicit:
        ok, reason = _reachable(explicit)
        if not ok:
            pytest.skip(f"GENESIS_TEST_DATABASE_URL is set but unreachable ({reason})")
        # `yield`, not `return`: this is a generator fixture, and a bare return
        # in one branch makes pytest report "did not yield a value".
        yield explicit
        return

    if _SKIP_REASON:
        pytest.skip(_SKIP_REASON)

    if (os.getenv("GENESIS_TEST_PROVISION_POSTGRES") or "").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        _SKIP_REASON = (
            "no real PostgreSQL configured. These tests assert Postgres semantics "
            "(unique violations, advisory locks, transactional DDL) and are deliberately "
            "NOT run against SQLite or a mock. Set GENESIS_TEST_DATABASE_URL to a real "
            "database, or GENESIS_TEST_PROVISION_POSTGRES=1 to have this fixture create a "
            "throwaway cluster from the local initdb/pg_ctl binaries (~1 minute)."
        )
        pytest.skip(_SKIP_REASON)

    required = {name: _binary(name) for name in ("initdb", "pg_ctl", "createdb")}
    missing = sorted(name for name, path in required.items() if not path)
    if missing:
        _SKIP_REASON = (
            "GENESIS_TEST_PROVISION_POSTGRES is set but these binaries were not found on "
            f"PATH or in a standard install location: {', '.join(missing)}."
        )
        pytest.skip(_SKIP_REASON)

    data_dir = tmp_path_factory.mktemp("pgdata") / "cluster"
    log_path = data_dir.parent / "postgres.log"
    port = _free_port()

    try:
        subprocess.run(
            [required["initdb"], "-D", str(data_dir), "-A", "trust", "--no-locale",
             "--encoding=UTF8"],
            check=True, capture_output=True, text=True, timeout=180,
        )
        # NOT `-w`: on Windows `pg_ctl -w start` routinely fails to return even
        # though the server came up, which hangs the whole session. Start
        # detached and poll the port instead — the port accepting a connection
        # is the condition `-w` claims to wait for anyway.
        subprocess.Popen(
            [required["pg_ctl"], "-D", str(data_dir), "-l", str(log_path),
             "-o", f"-h 127.0.0.1 -p {port}", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                probe.settimeout(1)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.5)
        else:
            raise TimeoutError(f"postgres did not accept connections on port {port}")
    except Exception as exc:
        _SKIP_REASON = f"could not start a local PostgreSQL cluster: {type(exc).__name__}: {exc}"
        pytest.skip(_SKIP_REASON)

    try:
        subprocess.run(
            [required["createdb"], "-h", "127.0.0.1", "-p", str(port), "genesis_test"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        url = f"postgresql://{quote(os.getenv('USER') or os.getenv('USERNAME') or 'postgres', safe='')}@127.0.0.1:{port}/genesis_test"
        ok, reason = _reachable(url)
        if not ok:
            _SKIP_REASON = f"provisioned cluster is not reachable ({reason})"
            pytest.skip(_SKIP_REASON)
        yield url
    finally:
        subprocess.run(
            [required["pg_ctl"], "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=False, capture_output=True, text=True, timeout=60,
        )


@pytest.fixture(scope="session")
def migrated_postgres_url(postgres_url: str) -> str:
    """The same database with every migration applied, via the real runner.

    Applying the schema through ``migrations/runner.py`` rather than a
    hand-written CREATE TABLE is deliberate: it means these tests fail if the
    runner breaks, so the thing production depends on is the thing under test.
    """
    from migrations import runner

    previous = os.environ.get("GENESIS_JOB_DATABASE_URL")
    os.environ["GENESIS_JOB_DATABASE_URL"] = postgres_url
    try:
        runner.migrate()
    finally:
        if previous is None:
            os.environ.pop("GENESIS_JOB_DATABASE_URL", None)
        else:
            os.environ["GENESIS_JOB_DATABASE_URL"] = previous
    return postgres_url


@pytest.fixture
def pg_env(migrated_postgres_url: str, monkeypatch) -> str:
    """Select the Postgres backend for one test and start from empty tables."""
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", migrated_postgres_url)
    monkeypatch.setenv("GENESIS_STORE_BACKEND", "postgres")
    monkeypatch.delenv("GENESIS_AUTH_DB_PATH", raising=False)
    monkeypatch.delenv("GENESIS_AUDIT_DB_PATH", raising=False)
    monkeypatch.delenv("GENESIS_ANCHOR_STORE_PATH", raising=False)

    from runtime import genesis_audit, pg_store

    genesis_audit.reset_for_tests()
    with pg_store.transaction() as cur:
        cur.execute(
            "TRUNCATE genesis_ap2_nonces, genesis_action_grants, genesis_audit_log, "
            "genesis_audit_anchors, genesis_audit_chain_heads RESTART IDENTITY"
        )
    yield migrated_postgres_url
    genesis_audit.reset_for_tests()
