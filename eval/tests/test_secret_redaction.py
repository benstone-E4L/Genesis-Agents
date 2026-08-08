"""PROOF: a marker secret placed in inputs, outputs, metadata AND an exception
message never reaches the serialised LangSmith run.

The marker is a single synthetic string. It is planted in every position a
secret could realistically enter a trace, then the entire serialised run payload
is searched for it. If it appears anywhere, the test fails.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from eval import traceable as traceable_mod
from eval.genesis_client import GenesisClient, MoneyDomainBlocked, RawResponse
from eval.redaction import REDACTED, is_sensitive_key, redact, refresh_env_secrets
from eval.tests.fakes import FakeLangSmith, FakeTransport, RecordingSleep

MARKER = "SUPERSECRET-MARKER-9f3c1a7e-DO-NOT-LEAK"

SECRET_ENV_NAMES = (
    "GATEWAY_API_KEY",
    "AGENT_GATEWAY_SECRET",
    "LLM_API_KEY",
    "DATABASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "LANGSMITH_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture
def langsmith(monkeypatch):
    """Install a fake LangSmith and enable tracing."""
    fake = FakeLangSmith()
    monkeypatch.setattr(traceable_mod, "TRACEABLE_FACTORY", fake.traceable)
    monkeypatch.setattr(traceable_mod, "RUN_TREE_GETTER", fake.get_current_run_tree)
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-fake-key-for-tests")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    return fake


@pytest.fixture
def planted_env(monkeypatch):
    """Every credential env var set to the marker."""
    for name in SECRET_ENV_NAMES:
        monkeypatch.setenv(name, MARKER)
    monkeypatch.setenv("DATABASE_URL", f"postgres://genesis:{MARKER}@db.internal:5432/genesis")
    refresh_env_secrets()
    yield
    refresh_env_secrets()


# ---------------------------------------------------------------------------
# The end-to-end proof
# ---------------------------------------------------------------------------


def test_marker_secret_never_reaches_a_serialised_run(langsmith, planted_env):
    """Marker planted in: inputs, outputs, metadata, exception message."""
    # OUTPUT position: the gateway echoes a secret back in its response body.
    leaky_body = json.dumps(
        {
            "response": f"I read the config and it says api_key={MARKER}",
            "agentName": "Genesis Research Agent",
            "debug": {
                "headers": {"Authorization": f"Bearer {MARKER}"},
                "env": {"GATEWAY_API_KEY": MARKER, "DATABASE_URL": f"postgres://u:{MARKER}@h/d"},
                "nested": [{"deep": {"session_key": MARKER}}],
            },
        }
    )
    transport = FakeTransport([RawResponse(200, leaky_body)])
    client = GenesisClient(
        transport=transport,
        api_key=MARKER,          # credential itself is the marker
        gateway_secret=MARKER,
        jitter=False,
        sleep=RecordingSleep(),
    )

    outputs = asyncio.run(
        traceable_mod.traced_agent_run(
            client,
            # INPUT position: the dataset example itself carries a secret.
            slug="genesis-research",
            task=f"Use api_key={MARKER} and Authorization: Bearer {MARKER} to fetch it",
            mode="live_test",
            # METADATA position.
            extra_metadata={
                "api_key": MARKER,
                "run_owner": "eval-harness",
                "context": {"authorization": f"Bearer {MARKER}", "aws_secret_access_key": MARKER},
                "free_text": f"connection string postgres://u:{MARKER}@h/db",
            },
        )
    )

    serialised = langsmith.serialised()
    assert langsmith.runs, "no run was recorded — the proof would be vacuous"
    assert MARKER not in serialised, "MARKER LEAKED into the serialised run"
    assert REDACTED in serialised, "nothing was redacted — check the fixture"

    # The request headers carrying the credential must not be traced either.
    assert MARKER not in json.dumps(langsmith.runs[0]["inputs"], default=str)
    assert MARKER not in json.dumps(langsmith.runs[0]["outputs"], default=str)
    assert MARKER not in json.dumps(langsmith.runs[0]["metadata"], default=str)
    assert MARKER not in json.dumps(outputs.__dict__, default=str)


def test_marker_in_an_exception_message_never_reaches_the_run(langsmith, planted_env):
    """A raised exception carrying the secret is redacted before propagating."""

    class LeakyTransport:
        async def request(self, *a, **kw):
            raise RuntimeError(f"connect failed using api_key={MARKER}")

        async def aclose(self):
            return None

    client = GenesisClient(transport=LeakyTransport(), api_key=MARKER, jitter=False)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            traceable_mod.traced_agent_run(
                client, slug="genesis-research", task="task", mode="live_test"
            )
        )

    assert MARKER not in str(excinfo.value), "MARKER LEAKED via the exception message"
    assert MARKER not in langsmith.serialised(), "MARKER LEAKED into the run error field"
    assert REDACTED in str(excinfo.value)


def test_money_domain_block_message_contains_no_secret(langsmith, planted_env):
    client = GenesisClient(transport=FakeTransport([]), api_key=MARKER)
    with pytest.raises(MoneyDomainBlocked) as excinfo:
        asyncio.run(client.run_agent("genesis-finance", "compute my payout"))
    assert MARKER not in str(excinfo.value)


def test_transport_failure_message_is_redacted_at_construction(planted_env):
    from eval.genesis_client import TransportFailure

    exc = TransportFailure("connect", f"failed with token {MARKER}")
    assert MARKER not in str(exc)
    assert MARKER not in exc.message


# ---------------------------------------------------------------------------
# Redactor unit behaviour (matches Cato approval_policy.redact)
# ---------------------------------------------------------------------------


def test_redacts_by_key_at_any_depth():
    payload = {"a": {"b": {"c": {"headers": {"authorization": "Bearer abc123"}}}}}
    out = redact(payload)
    assert out["a"]["b"]["c"]["headers"]["authorization"] == REDACTED


@pytest.mark.parametrize(
    "key",
    [
        "api_key", "apikey", "api-key", "API_KEY", "x_api_key",
        "private_key", "session_key", "authorization", "Authorization",
        "client_secret", "AGENT_GATEWAY_SECRET", "GATEWAY_API_KEY",
        "password", "access_token", "cookie", "DATABASE_URL",
        "aws_secret_access_key", "AWS_ACCESS_KEY_ID",
        # The gateway signing key. "privkey" has no underscore before "key",
        # so the generic "_key" part does NOT catch it — it needs its own part.
        "GENESIS_GATEWAY_PRIVKEY_B64", "privkey", "gateway_privkey",
        "R2_API_TOKEN", "GENESIS_SESSION_VAULT_KEY",
    ],
)
def test_sensitive_keys_are_recognised(key):
    assert is_sensitive_key(key), key
    assert redact({key: "value-here"})[key] == REDACTED


def test_bare_key_is_left_intact():
    """Matches the reference implementation: '_key' is not a substring of 'key'."""
    assert is_sensitive_key("key") is False
    assert redact({"key": "genesis-research"})["key"] == "genesis-research"
    assert redact({"keyword": "market research"})["keyword"] == "market research"
    assert redact({"monkey": "banana"})["monkey"] == "banana"


@pytest.mark.parametrize(
    "text",
    [
        "sk-abcdefghijklmnop1234",
        "Bearer eyJhbGciOi.payload.signature",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-1234567890-abcdefghij",
        "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM.SflKxwRJSM",
    ],
)
def test_credential_shaped_values_are_redacted_under_innocent_keys(text):
    out = redact({"notes": f"the value is {text} ok"})
    assert text not in out["notes"], out["notes"]


def test_lists_tuples_and_sets_are_walked():
    out = redact({"items": [{"token": "abc"}, ({"password": "p"},)]})
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1][0]["password"] == REDACTED


def test_depth_limit_does_not_explode():
    node: dict = {"authorization": "Bearer x"}
    for _ in range(60):
        node = {"n": node}
    assert redact(node) is not None


def test_non_string_values_under_sensitive_keys_are_preserved():
    out = redact({"has_api_key": True, "token_count": 12, "secret": None})
    assert out["has_api_key"] is True
    assert out["token_count"] == 12
    assert out["secret"] is None


def test_client_never_stores_the_credential_on_a_result(planted_env):
    transport = FakeTransport([RawResponse(200, '{"response":"ok"}')])
    client = GenesisClient(transport=transport, api_key=MARKER, jitter=False)
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert MARKER not in json.dumps(result.__dict__, default=str)
    # ...but it WAS sent on the wire, otherwise auth could not work.
    assert transport.calls[0]["headers"]["X-Agent-Api-Key"] == MARKER
    assert client.has_credential() is True
