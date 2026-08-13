from pathlib import Path
import getpass
import shutil
import socket
import subprocess
from urllib.parse import quote

import pytest


def test_owned_migration_covers_every_runtime_table_and_is_transactional():
    sql = Path("migrations/001_genesis_runtime.sql").read_text(encoding="utf-8")
    assert sql.strip().startswith("BEGIN;")
    assert sql.strip().endswith("COMMIT;")
    for table in (
        "genesis_jobs", "genesis_job_events", "genesis_agent_sessions",
        "genesis_agent_events", "genesis_job_relationships", "genesis_artifacts",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_runtime_no_longer_claims_external_schema_ownership():
    source = Path("durable_store.py").read_text(encoding="utf-8")
    assert "owned by the SwarmSync.AI" not in source


def test_ci_has_no_continue_on_error_escape_hatch():
    workflow = Path(".github/workflows/money-path-guards.yml").read_text(encoding="utf-8")
    assert "continue-on-error" not in workflow
    assert "GENESIS_DEPLOYMENT_PROFILE=cato" in workflow
    assert "forbidden escrow artifact was rejected" in workflow


def test_orphan_submodule_and_colliding_package_are_removed():
    assert not Path(".gitmodules").exists()
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "conduit-browser" not in requirements
    guide = Path("CLAUDE.md").read_text(encoding="utf-8")
    assert "git submodule update" not in guide


def _postgres_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(r"C:\Program Files\PostgreSQL\15\bin") / f"{name}.exe"
    return str(candidate) if candidate.exists() else None


def test_fresh_postgres_migration_supports_production_store_crud(tmp_path, monkeypatch):
    """Apply the owned migration, then execute every store family against it."""
    required = {name: _postgres_binary(name) for name in ("initdb", "pg_ctl", "createdb", "psql")}
    if not all(required.values()):
        pytest.skip("local PostgreSQL binaries are required for the migration integration proof")

    data_dir = (tmp_path / "postgres-data").resolve()
    assert data_dir.is_relative_to(tmp_path.resolve())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    print("PG_STAGE initdb", flush=True)
    subprocess.run(
        [required["initdb"], "-D", str(data_dir), "-A", "trust", "--no-locale", "--encoding=UTF8"],
        check=True, capture_output=True, text=True, timeout=90,
    )
    started = False
    try:
        log_path = (tmp_path / "postgres.log").resolve()
        print("PG_STAGE start", flush=True)
        subprocess.run(
            [required["pg_ctl"], "-D", str(data_dir), "-l", str(log_path),
             "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
        )
        started = True
        print("PG_STAGE create", flush=True)
        subprocess.run(
            [required["createdb"], "-h", "127.0.0.1", "-p", str(port), "genesis_test"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        print("PG_STAGE migrate", flush=True)
        subprocess.run(
            [required["psql"], "-h", "127.0.0.1", "-p", str(port), "-d", "genesis_test", "-v", "ON_ERROR_STOP=1", "-f", "migrations/001_genesis_runtime.sql"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        encoding = subprocess.run(
            [required["psql"], "-h", "127.0.0.1", "-p", str(port), "-d", "genesis_test",
             "-Atc", "SHOW server_encoding"],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        assert encoding == "UTF8"

        print("PG_STAGE crud", flush=True)
        user = quote(getpass.getuser(), safe="")
        db_url = f"postgresql://{user}@127.0.0.1:{port}/genesis_test"
        monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", db_url)
        import durable_store
        import job_store

        monkeypatch.setattr(durable_store, "_TABLES_MISSING", False)
        monkeypatch.setattr(durable_store, "_submit_write", lambda fn: fn())

        first = job_store.create_job(
            agent_slug="genesis-research", prompt="bounded", idempotency_key="idem-1"
        )
        repeat = job_store.create_job(
            agent_slug="genesis-research", prompt="ignored", idempotency_key="idem-1"
        )
        assert repeat["idempotent_hit"] is True and repeat["id"] == first["id"]
        assert job_store.get_job(first["id"])["status"] == "QUEUED"
        assert job_store.claim_job_by_id(first["id"])["status"] == "RUNNING"
        assert job_store.heartbeat(first["id"]) is True
        assert job_store.update_job_status(first["id"], "DELIVERED", result_summary="ok") is True

        queued = job_store.create_job(agent_slug="genesis-support", prompt="queue")
        assert {row["id"] for row in job_store.claim_queued_jobs()} == {queued["id"]}
        child_id = "child-crud-proof"
        job_store.create_child_job(
            child_job_id=child_id, agent_slug="genesis-pricing", prompt="child",
            parent_job_id=first["id"],
        )

        durable_store.session_create(
            session_id="session-proof", job_id=child_id, agent_slug="genesis-pricing"
        )
        assert durable_store.session_get("session-proof")["jobId"] == child_id
        assert len(durable_store.sessions_by_job(child_id)) == 1
        durable_store.event_insert(child_id, "tool_completed", {"ok": True}, session_id="session-proof")
        assert durable_store.events_get(child_id)[0]["event_type"] == "tool_completed"
        durable_store.relationship_create(
            parent_job_id=first["id"], child_job_id=child_id,
            parent_session_id=None, child_session_id="session-proof",
            parent_agent_slug="genesis-meta", child_agent_slug="genesis-pricing",
        )
        durable_store.relationship_update(child_id, status="COMPLETED")
        assert durable_store.relationships_by_parent(first["id"])[0]["delegationStatus"] == "COMPLETED"
        durable_store.artifact_record(
            job_id=child_id, session_id="session-proof", agent_slug="genesis-pricing",
            path="out/proof.json", filename="proof.json", mime_type="application/json",
            size_bytes=2, sha256="a" * 64, storage_backend="local", uri="artifact://proof",
        )
        artifact = durable_store.artifacts_by_job(child_id)[0]
        assert artifact["filename"] == "proof.json" and artifact["sizeBytes"] == 2
        durable_store.session_finish("session-proof", status="COMPLETED", trace={"ok": True})
        assert durable_store.session_get("session-proof")["status"] == "COMPLETED"
        assert job_store.expire_stale_running_jobs(stale_minutes=0) >= 1
    finally:
        if started:
            print("PG_STAGE stop", flush=True)
            subprocess.run(
                [required["pg_ctl"], "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
