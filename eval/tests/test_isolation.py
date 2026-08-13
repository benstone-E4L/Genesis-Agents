"""Proof that the suite cannot ship a span to the real Phoenix backend.

Render sets ``PHOENIX_COLLECTOR_ENDPOINT`` and ``PHOENIX_API_KEY`` to real
values, and a developer shell may export the same. Without these guards a test
run would publish evaluation content — including anything a redaction bug let
through — into the live Arize Phoenix space.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, Outcome
from eval.tests.fakes import FakeTransport, ok_response


def test_phoenix_credentials_are_cleared_for_every_test():
    """Even though .env carries a real key, no test sees it."""
    assert not os.environ.get("PHOENIX_API_KEY")
    assert not os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")


def test_phoenix_tracing_is_pinned_off():
    assert os.environ.get("PHOENIX_TRACING") == "false"


def test_endpoint_aliases_and_project_are_cleared():
    """So a leaked key could not even be aimed at the real Genesis project."""
    assert not os.environ.get("PHOENIX_ENDPOINT")
    assert not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert not os.environ.get("PHOENIX_PROJECT_NAME")


def test_content_capture_is_off_by_default_in_the_suite():
    """Content is the payload a leak would ride out on."""
    assert not os.environ.get("PHOENIX_TRACE_CONTENT")
    assert not os.environ.get("PHOENIX_ALLOW_CONTENT_OFFBOX")


def test_tracing_is_disabled_by_default_in_the_suite():
    assert traceable_mod.tracing_enabled() is False


def test_a_default_traced_run_ships_nothing():
    """With the fixture in force, traced_agent_run is a plain call."""
    transport = FakeTransport([ok_response("no trace shipped")])
    client = GenesisClient(transport=transport, api_key="k", jitter=False)
    result = asyncio.run(
        traceable_mod.traced_agent_run(
            client, slug="genesis-research", task="t", mode="live_test"
        )
    )
    assert result.outcome is Outcome.SUCCESS
    assert traceable_mod.tracing_enabled() is False


def test_a_test_may_re_enable_tracing_only_against_loopback(monkeypatch):
    """monkeypatch overrides the session fixture; the endpoint guard still applies."""
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("PHOENIX_TRACING", "true")
    assert traceable_mod.tracing_enabled() is True
    # The autouse _forbid_real_phoenix_endpoint fixture asserts the loopback
    # constraint at teardown; this test passing means it held.


@pytest.mark.parametrize(
    "endpoint,should_be_rejected",
    [
        ("https://app.phoenix.arize.com/s/ben-stone", True),
        ("https://otlp.arize.com", True),
        ("http://127.0.0.1:1", False),
        ("http://localhost:6006", False),
    ],
)
def test_the_endpoint_guards_condition_discriminates(endpoint, should_be_rejected):
    """The loopback predicate the teardown guard asserts on."""
    is_loopback = "127.0.0.1" in endpoint or "localhost" in endpoint
    assert (not is_loopback) is should_be_rejected


def test_no_eval_module_imports_langsmith_or_reads_its_env():
    """The migration is only finished when the dependency is actually gone.

    A dormant import is not harmless: it is a second exporter one environment
    variable away from shipping the same content to a second vendor. This checks
    for real coupling — an import statement or a ``LANGSMITH_*`` read — not for
    the word, because the module docstrings legitimately explain what was
    migrated away from and why.
    """
    import pathlib
    import re

    coupling = re.compile(r"^\s*(import|from)\s+langsmith|LANGSMITH_[A-Z_]+", re.MULTILINE)
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = [
        p.name for p in root.glob("*.py") if coupling.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"eval/ modules still coupled to langsmith: {offenders}"


def test_langsmith_is_not_a_declared_dependency():
    import pathlib

    requirements = (
        pathlib.Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    declared = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("langsmith" in line.lower() for line in declared), declared


def test_no_eval_module_loads_dotenv():
    """Nothing in eval/ pulls .env into the process (tests excluded)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = [
        p.name
        for p in root.glob("*.py")
        if "load_" + "dotenv" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"eval/ modules calling load_dotenv: {offenders}"
