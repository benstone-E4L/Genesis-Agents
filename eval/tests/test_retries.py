"""Retry policy: bounded, exponential, 5xx/transport only. 4xx NEVER."""

from __future__ import annotations

import asyncio

import pytest

from eval.genesis_client import GenesisClient, Outcome, RawResponse
from eval.tests.fakes import FakeTransport, RecordingSleep, connect_error, ok_response, read_timeout

ALL_4XX = [400, 401, 402, 403, 404, 405, 409, 410, 418, 422, 429, 451, 499]


def _build(script, **kw):
    transport = FakeTransport(script)
    sleep = RecordingSleep()
    client = GenesisClient(transport=transport, api_key="k", jitter=False, sleep=sleep, **kw)
    return client, transport, sleep


@pytest.mark.parametrize("status", ALL_4XX)
def test_no_4xx_is_ever_retried(status):
    client, transport, sleep = _build([RawResponse(status, "client side problem")])
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert transport.run_call_count == 1, f"HTTP {status} was retried"
    assert result.attempts == 1
    assert sleep.delays == [], f"HTTP {status} triggered a backoff sleep"
    assert result.outcome in (
        Outcome.AUTH_ERROR,
        Outcome.NOT_FOUND,
        Outcome.UPSTREAM_ERROR,
    )


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_5xx_is_retried_to_the_cap(status):
    client, transport, sleep = _build([RawResponse(status, "server side problem")])
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert transport.run_call_count == 3
    assert result.attempts == 3
    assert len(sleep.delays) == 2


def test_attempt_cap_is_configurable_and_respected():
    client, transport, _ = _build([RawResponse(503, "x")], max_attempts=5)
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert transport.run_call_count == 5
    assert result.attempts == 5


def test_max_attempts_one_disables_retry():
    client, transport, sleep = _build([RawResponse(503, "x")], max_attempts=1)
    asyncio.run(client.run_agent("genesis-research", "task"))
    assert transport.run_call_count == 1
    assert sleep.delays == []


def test_backoff_is_exponential_and_capped():
    client, _, sleep = _build(
        [RawResponse(503, "x")], max_attempts=6, backoff_base_s=1.0, backoff_max_s=4.0
    )
    asyncio.run(client.run_agent("genesis-research", "task"))
    assert sleep.delays == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_indeterminate_is_never_retried_even_though_it_is_a_transport_error():
    client, transport, sleep = _build([read_timeout()])
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert result.outcome is Outcome.INDETERMINATE
    assert transport.run_call_count == 1
    assert sleep.delays == []


def test_connect_error_is_retried_because_nothing_reached_the_server():
    client, transport, sleep = _build([connect_error(), connect_error(), ok_response()])
    result = asyncio.run(client.run_agent("genesis-research", "task"))
    assert result.outcome is Outcome.SUCCESS
    assert transport.run_call_count == 3
    assert len(sleep.delays) == 2


def test_max_attempts_zero_is_rejected():
    with pytest.raises(ValueError):
        GenesisClient(max_attempts=0)
