"""Smoke test for artifact_store."""
import os
import tempfile
from pathlib import Path
import pytest


def test_local_fallback_upload(tmp_path, monkeypatch):
    # Force local fallback by clearing AWS creds
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("GENESIS_LOCAL_ARTIFACT_DIR", str(tmp_path))

    # Reimport with new env
    import importlib
    import artifact_store
    importlib.reload(artifact_store)

    test_file = tmp_path / "test_input.txt"
    test_file.write_text("hello world")

    result = artifact_store.upload_file(job_id="test-job-1", local_path=test_file)
    assert result["ok"]
    assert result["backend"] == "local"
    assert (tmp_path / "test-job-1" / "test_input.txt").exists()


def test_list_artifacts_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("GENESIS_LOCAL_ARTIFACT_DIR", str(tmp_path))

    import importlib
    import artifact_store
    importlib.reload(artifact_store)

    result = artifact_store.list_artifacts(job_id="nonexistent")
    assert result["ok"]
    assert result["items"] == []
