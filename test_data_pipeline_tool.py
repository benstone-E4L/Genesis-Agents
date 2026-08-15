"""test_data_pipeline_tool.py — regression coverage for data pipeline tools
including Data Agent Kit tool integrations (data_get_editor_context, data_get_gcp_connection,
data_list_resource_templates, data_read_resource).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.data_pipeline_tool import (  # noqa: E402
    data_bigquery_query,
    data_get_editor_context,
    data_get_gcp_connection,
    data_list_resource_templates,
    data_read_resource,
)


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


class TestDataAgentKitTools:
    def test_get_editor_context_success(self):
        result = asyncio.run(
            data_get_editor_context(include_content=True, content="SELECT * FROM my_table")
        )
        assert result["ok"] is True
        assert result["tool"] == "data_get_editor_context"
        assert "result" in result
        assert "context" in result["result"]
        assert result["result"]["context"]["content"] == "SELECT * FROM my_table"
        assert "evidence" in result
        assert "context_hash" in result["evidence"]

    def test_get_gcp_connection_with_project_id(self):
        result = asyncio.run(data_get_gcp_connection(project_id="test-project-123"))
        assert result["ok"] is True
        assert result["tool"] == "data_get_gcp_connection"
        assert result["result"]["connection"]["project_id"] == "test-project-123"
        assert result["result"]["connection"]["connected"] is True
        assert "evidence" in result

    def test_get_gcp_connection_unconfigured(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
        result = asyncio.run(data_get_gcp_connection())
        assert result["ok"] is False
        assert result["error"]["code"] == "provider_unconfigured"
        assert "GOOGLE_CLOUD_PROJECT" in result["error"]["detail"]["missing_keys"]

    def test_list_resource_templates(self):
        result = asyncio.run(data_list_resource_templates())
        assert result["ok"] is True
        assert result["tool"] == "data_list_resource_templates"
        templates = result["result"]["resource_templates"]
        assert isinstance(templates, list)
        assert len(templates) >= 3
        schemes = [t["uri_template"] for t in templates]
        assert any(s.startswith("bq://") for s in schemes)
        assert any(s.startswith("gcs://") for s in schemes)
        assert any(s.startswith("spark://") for s in schemes)

    def test_read_resource_valid_uri(self):
        result = asyncio.run(data_read_resource(uri="bq://my-project/analytics_dataset/events"))
        assert result["ok"] is True
        assert result["tool"] == "data_read_resource"
        assert result["result"]["resource"]["uri"] == "bq://my-project/analytics_dataset/events"
        assert result["result"]["resource"]["scheme"] == "bq"
        assert "evidence" in result
        assert "resource_digest" in result["evidence"]

    def test_read_resource_invalid_scheme(self):
        result = asyncio.run(data_read_resource(uri="ftp://unsupported.server/data"))
        assert result["ok"] is False
        assert result["error"]["code"] == "validation_failed"
        assert "violations" in result["error"]["detail"]

    def test_read_resource_empty_uri(self):
        result = asyncio.run(data_read_resource(uri=""))
        assert result["ok"] is False
        assert result["error"]["code"] == "validation_failed"
