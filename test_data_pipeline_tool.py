"""test_data_pipeline_tool.py — regression coverage for the data_bigquery_query
envelope fix (FINANCE-TOOL-CONTRACTS.md Section 7 Phase 3 precondition).

The function previously returned a bare ``{"ok": False, "scaffold": True, ...}``
dict — the banned ``scaffold`` key (Section 5.3 rule 3) instead of a
taxonomy-coded failure envelope. It never fabricated a result, but it wasn't
contract-compliant either.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.data_pipeline_tool import data_bigquery_query  # noqa: E402


class TestDataBigqueryQueryEnvelope:
    def test_returns_ok_false_not_implemented(self):
        result = asyncio.run(
            data_bigquery_query(project_id="proj-1", query="SELECT 1", max_rows=10)
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "not_implemented"
        assert result["error"]["retryable"] is False

    def test_no_banned_keys_in_response(self):
        result = asyncio.run(
            data_bigquery_query(project_id="proj-1", query="SELECT 1")
        )
        assert "scaffold" not in result
        assert "stub" not in result
        assert "note" not in result

    def test_envelope_has_contract_fields(self):
        result = asyncio.run(
            data_bigquery_query(project_id="proj-1", query="SELECT 1")
        )
        assert result["tool"] == "data_bigquery_query"
        assert "contract_version" in result
        assert "request_id" in result

    def test_no_query_data_leaks_as_a_result(self):
        """ok=false must carry no `result` key at all (Section 5.3 rule 2)."""
        result = asyncio.run(
            data_bigquery_query(project_id="proj-1", query="SELECT * FROM secrets")
        )
        assert "result" not in result
