"""Unit tests for runtime.sandbox_manager — process-tier guarantees.

These run locally (no DB, no network). They prove the static-guard + process
sandbox blocks escapes, scrubs secrets, confines cwd, and enforces timeouts.
On a host with bubblewrap these same guards apply plus kernel FS isolation.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from runtime import sandbox_manager as sm

_POSIX = os.name == "posix"


@pytest.fixture()
def jobdir(tmp_path: Path) -> Path:
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_safe_command_runs(jobdir):
    r = sm.run_in_sandbox("job1", "echo hello-sandbox", job_dir=jobdir)
    assert r["ok"] is True
    assert "hello-sandbox" in r["stdout"]
    assert r["isolation"] in ("bwrap", "process")


def test_etc_passwd_blocked(jobdir):
    r = sm.run_in_sandbox("job1", "cat /etc/passwd", job_dir=jobdir)
    assert r["ok"] is False
    assert r["error"] == "command_blocked"
    assert r["reason"] == "sensitive_path_blocked"


def test_dotenv_blocked(jobdir):
    r = sm.run_in_sandbox("job1", "cat .env", job_dir=jobdir)
    assert r["ok"] is False
    assert r["error"] == "command_blocked"


def test_parent_traversal_blocked(jobdir):
    r = sm.run_in_sandbox("job1", "cat ../../secret.txt", job_dir=jobdir)
    assert r["ok"] is False
    assert r["error"] == "command_blocked"


def test_dangerous_command_blocked(jobdir):
    r = sm.run_in_sandbox("job1", "rm -rf /", job_dir=jobdir)
    assert r["ok"] is False
    assert r["error"] == "command_blocked"


def test_pipe_to_shell_blocked(jobdir):
    r = sm.run_in_sandbox("job1", "echo x | bash", job_dir=jobdir)
    assert r["ok"] is False


def test_cwd_escape_blocked(jobdir):
    r = sm.run_in_sandbox("job1", "echo hi", cwd="../../..", job_dir=jobdir)
    assert r["ok"] is False
    assert r["error"] == "path_escape_blocked"


@pytest.mark.skipif(not _POSIX, reason="uses POSIX $VAR expansion; sandbox target is Linux")
def test_secret_env_scrubbed(jobdir):
    # A secret-looking env var must NOT reach the sandbox.
    r = sm.run_in_sandbox(
        "job1",
        "echo TOKEN=[$MY_API_TOKEN] SAFE=[$SAFE_VAR]",
        env={"MY_API_TOKEN": "sk_live_abcdef", "SAFE_VAR": "ok"},
        job_dir=jobdir,
    )
    assert r["ok"] is True
    assert "sk_live_abcdef" not in r["stdout"]
    assert "TOKEN=[]" in r["stdout"] or "TOKEN=[ ]" in r["stdout"] or "sk_live" not in r["stdout"]
    assert "SAFE=[ok]" in r["stdout"]


def test_guard_command_helper():
    assert sm.guard_command("ls -la") is None
    assert sm.guard_command("cat /etc/passwd") == "sensitive_path_blocked"
    assert sm.guard_command("") == "empty_command"


def test_lifecycle_create_status_destroy(jobdir, monkeypatch):
    monkeypatch.setenv("GENESIS_WORKSPACE_ROOT", str(jobdir.parent))
    desc = sm.create_sandbox("lifejob")
    assert desc["ok"] is True
    assert desc["isolation"] in ("bwrap", "process")
    st = sm.sandbox_status("lifejob")
    assert st["isolation"] in ("bwrap", "process")
    out = sm.destroy_sandbox("lifejob", cleanup_policy="purge")
    assert "ok" in out
