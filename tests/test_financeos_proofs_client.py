"""Tests for FinanceOS proofs client and tools."""

from __future__ import annotations

import pytest

from accounting.financeos_proofs_client import financeos_proofs_configured, post_verify_api
from accounting.xero_scope import augment_tools_advertised
from tools.financeos_proofs_tool import financeos_verify_api


def test_augment_tools_adds_financeos_proof_tools() -> None:
    tools = augment_tools_advertised("genesis-e4l-ap", [])
    assert "financeos_invoice_proof" in tools
    assert "financeos_verify_api" in tools


@pytest.mark.asyncio
async def test_verify_api_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCEOS_API_BASE_URL", raising=False)
    monkeypatch.delenv("INTERNAL_PROOFS_SERVICE_TOKEN", raising=False)
    assert financeos_proofs_configured() is False
    result = await financeos_verify_api(
        entity_id="22222222-3333-4444-5555-666666666666",
        operation="create_draft_bill",
        _parent_agent_slug="genesis-e4l-ap",
    )
    assert result["ok"] is False
    assert result["error"] == "financeos_proofs_not_configured"


@pytest.mark.asyncio
async def test_post_verify_api_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FINANCEOS_API_BASE_URL",
        "https://e4l-financeos-api.orangedune-dad71fcc.westus2.azurecontainerapps.io",
    )
    monkeypatch.setenv("INTERNAL_PROOFS_SERVICE_TOKEN", "x" * 32)

    class FakeResp:
        status = 201

        async def text(self) -> str:
            return (
                '{"run_id":"55555555-6666-7777-8888-999999999999",'
                '"verdict":"PASS","blocked":false,"allowed_tools":["xero_scoped_invoke"]}'
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResp()

        async def close(self):
            return None

    def fake_client_session(*args, **kwargs):
        return FakeSession()

    monkeypatch.setattr("accounting.financeos_proofs_client.aiohttp.ClientSession", fake_client_session)
    result = await post_verify_api(
        entity_id="22222222-3333-4444-5555-666666666666",
        agent_slug="genesis-e4l-ap",
        operation="create_draft_bill",
    )
    assert result["ok"] is True
    assert result["run_id"] == "55555555-6666-7777-8888-999999999999"
