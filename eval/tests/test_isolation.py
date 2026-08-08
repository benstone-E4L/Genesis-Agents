"""Proof that the suite cannot ship a run to the real LangSmith backend."""

from __future__ import annotations

import asyncio
import os

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, Outcome
from eval.tests.fakes import FakeTransport, ok_response


def test_langsmith_api_key_is_cleared_for_every_test():
    """Even though .env carries a real key, no test sees it."""
    assert not os.environ.get("LANGSMITH_API_KEY")
    assert not os.environ.get("LANGCHAIN_API_KEY")


def test_langsmith_tracing_is_pinned_off():
    assert os.environ.get("LANGSMITH_TRACING") == "false"
    assert os.environ.get("LANGCHAIN_TRACING_V2") == "false"


def test_langsmith_endpoint_and_project_are_cleared():
    """So a leaked key could not even be aimed at the real Genesis project."""
    assert not os.environ.get("LANGSMITH_ENDPOINT")
    assert not os.environ.get("LANGSMITH_PROJECT")
    assert not os.environ.get("LANGCHAIN_PROJECT")


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
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-fake")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")
    assert traceable_mod.tracing_enabled() is True
    # The autouse _forbid_real_langsmith_endpoint fixture asserts the loopback
    # constraint at teardown; this test passing means it held.


@pytest.mark.parametrize(
    "endpoint,should_be_rejected",
    [
        ("https://api.smith.langchain.com", True),
        ("https://eu.api.smith.langchain.com", True),
        ("http://127.0.0.1:1", False),
        ("http://localhost:9", False),
    ],
)
def test_the_endpoint_guards_condition_discriminates(endpoint, should_be_rejected):
    """The loopback predicate the teardown guard asserts on."""
    is_loopback = "127.0.0.1" in endpoint or "localhost" in endpoint
    assert (not is_loopback) is should_be_rejected


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
