"""Job store tests — uses an in-memory mock connection."""
# Skip if no DATABASE_URL — these are integration-style; smoke-only in CI.
import os, pytest


def test_database_url_strips_unsupported_pgbouncer_param(monkeypatch):
    from job_store import _database_url

    monkeypatch.delenv("GENESIS_JOB_DATABASE_URL", raising=False)
    monkeypatch.delenv("DIRECT_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:pass@example.supabase.co:6543/postgres?pgbouncer=true&connection_limit=1&sslmode=require",
    )

    assert _database_url() == (
        "postgresql://user:pass@example.supabase.co:6543/postgres?sslmode=require"
    )


def test_database_url_prefers_genesis_job_database_url(monkeypatch):
    from job_store import _database_url

    monkeypatch.setenv("DATABASE_URL", "postgresql://pooled/db?pgbouncer=true")
    monkeypatch.setenv("DIRECT_URL", "postgresql://direct/db")
    monkeypatch.setenv("GENESIS_JOB_DATABASE_URL", "postgresql://jobs/db")

    assert _database_url() == "postgresql://jobs/db"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs Postgres")
def test_create_and_get_job():
    from job_store import create_job, get_job
    j = create_job(agent_slug="genesis-research", prompt="test prompt")
    assert j["id"]
    assert j["status"] == "QUEUED"
    got = get_job(j["id"])
    assert got is not None
    assert got["agentSlug"] == "genesis-research"

@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs Postgres")
def test_idempotency():
    from job_store import create_job
    j1 = create_job(agent_slug="genesis-research", prompt="x", idempotency_key="test-key-1")
    j2 = create_job(agent_slug="genesis-research", prompt="x", idempotency_key="test-key-1")
    assert j1["id"] == j2["id"]
    assert j2["idempotent_hit"] is True
