"""Smoke tests for Phase 4 (async meta), Phase 6 (escrow), Phase 11 (quality)."""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Phase 6 - escrow split + secret-less defensive behavior
# ---------------------------------------------------------------------------

def test_calculate_split_default_fee():
    from escrow_client import calculate_split
    s = calculate_split(10000)  # $100
    assert s["total_cents"] == 10000
    assert s["platform_fee_cents"] == 1000  # 10%
    assert s["agent_net_cents"] == 9000


def test_calculate_split_custom_fee():
    from escrow_client import calculate_split
    s = calculate_split(20000, fee_pct_override=0.20)
    assert s["platform_fee_cents"] == 4000
    assert s["agent_net_cents"] == 16000


def test_escrow_client_imports_without_secret(monkeypatch):
    monkeypatch.delenv("INTERNAL_SECRET", raising=False)
    import importlib
    import escrow_client
    importlib.reload(escrow_client)
    # initiate without secret should return error, not crash
    import asyncio
    r = asyncio.run(escrow_client.initiate_escrow(
        source_wallet_id="w1", destination_wallet_id="w2", amount_cents=1000
    ))
    assert r["ok"] is False
    assert r["error"] == "internal_secret_not_configured"


# ---------------------------------------------------------------------------
# Phase 11 - success criteria
# ---------------------------------------------------------------------------

def test_success_criteria_non_empty():
    from agent_runtime import _check_success_criteria
    r = _check_success_criteria([{"type": "non_empty"}], {"response": "hello"})
    assert r["ok"]

    r = _check_success_criteria([{"type": "non_empty"}], {"response": ""})
    assert not r["ok"]


def test_success_criteria_contains_keys():
    from agent_runtime import _check_success_criteria
    response_json = json.dumps({"summary": "x", "sources": []})
    r = _check_success_criteria(
        [{"type": "contains_keys", "config": {"keys": ["summary", "sources"]}}],
        {"response": response_json},
    )
    assert r["ok"]
