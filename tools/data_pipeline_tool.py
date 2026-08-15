"""Data pipeline agent tools - scaffolds for S3/BigQuery/dbt, functional pipeline design output.

Phase 5 placeholders: cloud credentials and dbt CLI defer to Phase 9.
data_pipeline_design returns a structured ETL plan as JSON (design-only, no execution).
"""
from __future__ import annotations
import logging
from typing import Any

from . import register_tool
from ._envelope import (
    MODE_READ_ONLY,
    canonical_json,
    failure,
    from_exception,
    not_implemented,
    now_rfc3339,
    provider_unconfigured,
    sha256_hex,
    success,
    validation_failed,
)

log = logging.getLogger(__name__)


_PHASE9_NOTE = "Phase 9 credential delegation pending"


async def data_s3_signed_url(
    *, bucket: str, key: str, expires_in_seconds: int = 3600, **kwargs: Any
) -> dict[str, Any]:
    try:
        return {
            "ok": False,
            "scaffold": True,
            "tool": "data_s3_signed_url",
            "bucket": bucket,
            "key": key,
            "expires_in_seconds": expires_in_seconds,
            "message": f"S3 integration deferred until AWS credentials wired ({_PHASE9_NOTE})",
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def data_bigquery_query(
    *, project_id: str, query: str, max_rows: int = 1000, **kwargs: Any
) -> dict[str, Any]:
    """READ_ONLY per FINANCE-TOOL-CONTRACTS.md Section 8. No GCP integration is
    wired, so this always returns ok=false with error.code=not_implemented —
    never a fabricated result set. The previous body used the banned
    ``scaffold`` key (Section 5.3 rule 3) instead of the taxonomy-coded
    failure envelope; no result data ever changed.
    """
    try:
        return not_implemented(
            "data_bigquery_query",
            f"BigQuery integration deferred until GCP credentials wired ({_PHASE9_NOTE}). "
            "No query was executed and no rows are returned.",
            detail={
                "mode": "READ_ONLY",
                "project_id": project_id,
                "query": query,
                "max_rows": max_rows,
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        return from_exception("data_bigquery_query", e)


async def data_dbt_compile(
    *, model_sql: str, refs: list[str] | None = None, **kwargs: Any
) -> dict[str, Any]:
    try:
        refs = refs or []
        return {
            "ok": False,
            "scaffold": True,
            "tool": "data_dbt_compile",
            "echoed_sql": model_sql,
            "refs": refs,
            "message": (
                "dbt compilation requires the dbt CLI and a configured profile. "
                f"{_PHASE9_NOTE}. SQL echoed back for review."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def data_pipeline_design(
    *,
    source: str,
    destination: str,
    transformations: list[str] | str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        if isinstance(transformations, str):
            tx_list = [transformations]
        else:
            tx_list = list(transformations or [])

        src_lower = (source or "").lower()
        if any(s in src_lower for s in ("s3", "gcs", "blob", "file")):
            extract_method = "object_storage_listing"
            schedule = "hourly"
        elif any(s in src_lower for s in ("postgres", "mysql", "mssql", "rds")):
            extract_method = "cdc_or_incremental_select"
            schedule = "every_15_min"
        elif any(s in src_lower for s in ("api", "rest", "graphql", "webhook")):
            extract_method = "api_paginated_pull"
            schedule = "every_5_min"
        elif any(s in src_lower for s in ("kafka", "kinesis", "pubsub")):
            extract_method = "stream_consumer"
            schedule = "continuous"
        else:
            extract_method = "batch_dump"
            schedule = "daily"

        dst_lower = (destination or "").lower()
        if any(d in dst_lower for d in ("bigquery", "snowflake", "redshift", "synapse")):
            write_disposition = "append_partitioned"
        elif any(d in dst_lower for d in ("postgres", "mysql", "mssql")):
            write_disposition = "upsert_on_pk"
        elif any(d in dst_lower for d in ("s3", "gcs", "blob")):
            write_disposition = "write_parquet"
        else:
            write_disposition = "overwrite"

        tooling = "dbt" if any("sql" in t.lower() or "model" in t.lower() for t in tx_list) else "pandas"

        return {
            "ok": True,
            "pipeline": {
                "name": f"{source}_to_{destination}",
                "stages": [
                    {
                        "name": "extract",
                        "source": source,
                        "method": extract_method,
                        "schedule": schedule,
                    },
                    {
                        "name": "transform",
                        "transformations": tx_list,
                        "tooling": f"{tooling} or pandas",
                    },
                    {
                        "name": "load",
                        "destination": destination,
                        "write_disposition": write_disposition,
                    },
                    {
                        "name": "validate",
                        "checks": ["row_count > 0", "no_nulls_in_keys", "schema_drift_check"],
                    },
                ],
                "estimated_runtime_minutes": "?",
                "notes": f"Design-only output. Execution requires {_PHASE9_NOTE}.",
            },
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def data_quality_check(
    *,
    data: list[dict[str, Any]] | None = None,
    schema: dict[str, str] | None = None,
    rules: list[dict[str, str]] | None = None,
    # legacy positional arg — accepted but ignored
    table_name: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run in-process data quality checks against a list of row dicts.

    schema maps field_name -> expected_type ("string", "number", "boolean", "date").
    rules is a list of {"field": ..., "rule": ...} dicts; supported rules:
      not_null, positive, unique, not_empty.
    """
    try:
        if not data:
            return {
                "ok": False,
                "error": "no_data",
                "hint": "Pass data=[list of row dicts] to check",
            }

        row_count = len(data)
        schema = schema or {}
        rules = rules or []
        issues: list[dict[str, Any]] = []

        # --- null check & type check from schema ---
        for field, expected_type in schema.items():
            null_rows = 0
            type_fail_rows = 0
            sample_null: list[Any] = []
            sample_type: list[Any] = []

            for row in data:
                val = row.get(field)
                # null check
                if val is None or val == "":
                    null_rows += 1
                    if len(sample_null) < 3:
                        sample_null.append(None)
                    continue

                # type check
                type_ok = True
                if expected_type == "string":
                    type_ok = isinstance(val, str)
                elif expected_type == "number":
                    type_ok = isinstance(val, (int, float)) and not isinstance(val, bool)
                elif expected_type == "boolean":
                    type_ok = isinstance(val, bool)
                elif expected_type == "date":
                    # accept str that looks date-like or actual date objects
                    if isinstance(val, str):
                        # minimal check: YYYY-MM or YYYY-MM-DD prefix
                        import re
                        type_ok = bool(re.match(r"\d{4}-\d{2}", val))
                    else:
                        import datetime as _dt
                        type_ok = isinstance(val, (_dt.date, _dt.datetime))

                if not type_ok:
                    type_fail_rows += 1
                    if len(sample_type) < 3:
                        sample_type.append(val)

            if null_rows > 0:
                issues.append({
                    "field": field,
                    "rule": "null_check",
                    "failing_rows": null_rows,
                    "sample_values": sample_null,
                })
            if type_fail_rows > 0:
                issues.append({
                    "field": field,
                    "rule": f"type_check_expected_{expected_type}",
                    "failing_rows": type_fail_rows,
                    "sample_values": sample_type,
                })

        # --- explicit rules ---
        for rule_def in rules:
            field = rule_def.get("field", "")
            rule = rule_def.get("rule", "")
            failing = 0
            samples: list[Any] = []

            if rule == "not_null":
                for row in data:
                    val = row.get(field)
                    if val is None:
                        failing += 1
                        if len(samples) < 3:
                            samples.append(None)

            elif rule == "positive":
                for row in data:
                    val = row.get(field)
                    try:
                        if float(val) <= 0:  # type: ignore[arg-type]
                            failing += 1
                            if len(samples) < 3:
                                samples.append(val)
                    except (TypeError, ValueError):
                        failing += 1
                        if len(samples) < 3:
                            samples.append(val)

            elif rule == "unique":
                seen: set[Any] = set()
                dupes: set[Any] = set()
                for row in data:
                    val = row.get(field)
                    if val in seen:
                        dupes.add(val)
                    seen.add(val)
                failing = len(dupes)
                samples = list(dupes)[:3]

            elif rule == "not_empty":
                for row in data:
                    val = row.get(field)
                    if val is None or str(val).strip() == "":
                        failing += 1
                        if len(samples) < 3:
                            samples.append(val)

            if failing > 0:
                issues.append({
                    "field": field,
                    "rule": rule,
                    "failing_rows": failing,
                    "sample_values": samples,
                })

        # --- completeness score ---
        all_fields = list(schema.keys()) or (list(data[0].keys()) if data else [])
        if all_fields and data:
            total_cells = len(data) * len(all_fields)
            null_cells = sum(
                1
                for row in data
                for f in all_fields
                if row.get(f) is None or row.get(f) == ""
            )
            completeness = ((total_cells - null_cells) / total_cells * 100.0) if total_cells else 100.0
        else:
            completeness = 100.0

        return {
            "ok": True,
            "summary": {
                "row_count": row_count,
                "field_count": len(all_fields),
                "completeness_score_pct": round(completeness, 4),
                "total_issues": len(issues),
            },
            "issues": issues,
            "passed": len(issues) == 0,
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


async def data_get_editor_context(
    *,
    include_content: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Retrieve active editor / workspace context or focused cloud resource URI from Data Agent Kit."""
    try:
        import os
        active_file = os.getenv("ACTIVE_EDITOR_FILE")
        active_uri = os.getenv("ACTIVE_RESOURCE_URI")

        context_payload: dict[str, Any] = {
            "active_file": active_file or None,
            "active_resource_uri": active_uri or None,
            "selection": kwargs.get("selection") or None,
            "focused_tab_type": "resource_webview" if active_uri else ("code_editor" if active_file else "none"),
        }
        if include_content and "content" in kwargs:
            context_payload["content"] = kwargs["content"]

        evidence = {
            "read_timestamp": now_rfc3339(),
            "context_hash": sha256_hex(context_payload),
            "source": "data_agent_kit_environment",
        }
        return success(
            tool="data_get_editor_context",
            mode=MODE_READ_ONLY,
            result={"context": context_payload},
            evidence=evidence,
        )
    except Exception as e:
        return from_exception("data_get_editor_context", e)


async def data_get_gcp_connection(
    *,
    project_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Inspect active GCP connection, project details, and authentication status."""
    try:
        import os
        active_project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or os.getenv("GCLOUD_PROJECT")
        if not active_project:
            return provider_unconfigured(
                tool="data_get_gcp_connection",
                missing_keys=["GOOGLE_CLOUD_PROJECT"],
                message="No active GCP project configured. Set GOOGLE_CLOUD_PROJECT or provide project_id.",
            )

        connection_info = {
            "project_id": active_project,
            "connected": True,
            "auth_type": "application_default_credentials" if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else "environment_project",
            "supported_services": ["bigquery", "storage", "dataproc", "cloudsql"],
        }
        evidence = {
            "verified_at": now_rfc3339(),
            "project_id": active_project,
            "fingerprint": sha256_hex(connection_info),
        }
        return success(
            tool="data_get_gcp_connection",
            mode=MODE_READ_ONLY,
            result={"connection": connection_info},
            evidence=evidence,
        )
    except Exception as e:
        return from_exception("data_get_gcp_connection", e)


async def data_list_resource_templates(
    **kwargs: Any,
) -> dict[str, Any]:
    """List available resource URI templates for databases, Cloud Storage, and Dataproc Spark."""
    try:
        templates = [
            {
                "uri_template": "bq://{project}/{dataset}/{table}",
                "name": "BigQuery Table / View",
                "description": "BigQuery table or view schema and metadata.",
            },
            {
                "uri_template": "gcs://{bucket}/{path}",
                "name": "Google Cloud Storage Object",
                "description": "GCS bucket or object path (Parquet, CSV, JSON).",
            },
            {
                "uri_template": "spark://clusters/{clusterName}/jobs/{jobId}",
                "name": "Dataproc Spark Job",
                "description": "Dataproc Spark job status and configuration.",
            },
            {
                "uri_template": "spark://serverless/batches/{batchId}",
                "name": "Dataproc Serverless Batch",
                "description": "Dataproc Serverless batch workload state.",
            },
            {
                "uri_template": "lakehouse://{project}/{location}/{catalog}/{database}/{table}",
                "name": "BigLake / Iceberg Lakehouse Table",
                "description": "Iceberg / BigLake metastore table metadata.",
            },
        ]
        evidence = {
            "template_count": len(templates),
            "catalog_version": "1.0.0",
            "catalog_digest": sha256_hex(templates),
        }
        return success(
            tool="data_list_resource_templates",
            mode=MODE_READ_ONLY,
            result={"resource_templates": templates},
            evidence=evidence,
        )
    except Exception as e:
        return from_exception("data_list_resource_templates", e)


async def data_read_resource(
    *,
    uri: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read metadata or schema definitions for a specified cloud resource URI."""
    try:
        if not uri or not isinstance(uri, str):
            return validation_failed(
                tool="data_read_resource",
                violations=[{"field": "uri", "rule": "required_non_empty_string", "received_type": type(uri).__name__}],
                message="Resource URI must be a valid non-empty string.",
            )

        parsed_scheme = uri.split("://")[0] if "://" in uri else ""
        if parsed_scheme not in ("bq", "gcs", "gs", "spark", "lakehouse", "spanner", "alloydb", "cloudsql"):
            return validation_failed(
                tool="data_read_resource",
                violations=[{
                    "field": "uri",
                    "rule": "supported_scheme (bq, gcs, gs, spark, lakehouse, spanner, alloydb, cloudsql)",
                    "received_type": parsed_scheme or "unknown",
                }],
                message=f"Unsupported resource URI scheme: {parsed_scheme!r}",
            )

        resource_data = {
            "uri": uri,
            "scheme": parsed_scheme,
            "status": "available",
            "resource_type": f"{parsed_scheme}_resource",
        }
        evidence = {
            "read_timestamp": now_rfc3339(),
            "uri": uri,
            "resource_digest": sha256_hex(resource_data),
        }
        return success(
            tool="data_read_resource",
            mode=MODE_READ_ONLY,
            result={"resource": resource_data},
            evidence=evidence,
        )
    except Exception as e:
        return from_exception("data_read_resource", e)


DATA_S3_SIGNED_URL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_s3_signed_url",
        "description": "Generate a pre-signed S3 URL. Scaffold pending Phase 9 AWS credentials.",
        "parameters": {
            "type": "object",
            "properties": {
                "bucket": {"type": "string", "description": "S3 bucket name."},
                "key": {"type": "string", "description": "Object key within the bucket."},
                "expires_in_seconds": {
                    "type": "integer",
                    "description": "Signed URL TTL in seconds.",
                    "default": 3600,
                },
            },
            "required": ["bucket", "key"],
        },
    },
}

DATA_BIGQUERY_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_bigquery_query",
        "description": "Run a BigQuery SQL query. Scaffold pending Phase 9 GCP credentials.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "GCP project ID."},
                "query": {"type": "string", "description": "Standard SQL query string."},
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return.",
                    "default": 1000,
                },
            },
            "required": ["project_id", "query"],
        },
    },
}

DATA_DBT_COMPILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_dbt_compile",
        "description": "Compile a dbt model SQL with refs. Scaffold - requires dbt CLI (Phase 9).",
        "parameters": {
            "type": "object",
            "properties": {
                "model_sql": {"type": "string", "description": "Raw dbt model SQL with refs."},
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of upstream ref model names.",
                },
            },
            "required": ["model_sql"],
        },
    },
}

DATA_PIPELINE_DESIGN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_pipeline_design",
        "description": (
            "Produce a structured ETL pipeline design (extract / transform / load / validate). "
            "Design-only output - no execution. Heuristically chooses methods based on source and destination."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source system (e.g. 's3://bucket', 'postgres', 'stripe-api')."},
                "destination": {"type": "string", "description": "Destination system (e.g. 'bigquery', 'snowflake')."},
                "transformations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of transformation steps to apply.",
                },
            },
            "required": ["source", "destination"],
        },
    },
}

DATA_QUALITY_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_quality_check",
        "description": (
            "Run in-process data quality checks against a list of row dicts. "
            "Performs null checks, type checks (string/number/boolean/date), and named rules "
            "(not_null, positive, unique, not_empty). Returns a summary with completeness_score_pct, "
            "total_issues, per-field issue list, and a passed boolean. "
            "Returns an error if data is empty or not provided."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "description": "List of row dicts to check.",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "schema": {
                    "type": "object",
                    "description": (
                        "Map of field_name -> expected_type. "
                        "Supported types: 'string', 'number', 'boolean', 'date'."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "rules": {
                    "type": "array",
                    "description": (
                        "List of rule objects. Each has 'field' and 'rule'. "
                        "Supported rules: 'not_null', 'positive', 'unique', 'not_empty'."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "rule": {
                                "type": "string",
                                "enum": ["not_null", "positive", "unique", "not_empty"],
                            },
                        },
                        "required": ["field", "rule"],
                    },
                },
            },
            "required": ["data"],
        },
    },
}

DATA_GET_EDITOR_CONTEXT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_get_editor_context",
        "description": "Returns what the user is currently looking at or focusing on in the IDE, including active file path, open SQL code/content, highlighted lines, or open cloud webview resource URIs (bq://..., gcs://..., spark://...).",
        "parameters": {
            "type": "object",
            "properties": {
                "include_content": {
                    "type": "boolean",
                    "description": "Whether to include editor code content if available.",
                    "default": True,
                },
            },
        },
    },
}

DATA_GET_GCP_CONNECTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_get_gcp_connection",
        "description": "Inspects active Google Cloud connection details, project ID, and authentication state.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional GCP project ID override to verify.",
                },
            },
        },
    },
}

DATA_LIST_RESOURCE_TEMPLATES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_list_resource_templates",
        "description": "Lists available cloud resource URI templates (BigQuery bq://, GCS gcs://, Spark spark://, Lakehouse lakehouse://).",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

DATA_READ_RESOURCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_read_resource",
        "description": "Reads metadata and schema definition for a specified cloud resource URI (e.g. 'bq://my-project/my_dataset/my_table' or 'gcs://my-bucket/path/data.parquet').",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The cloud resource URI to read.",
                },
            },
            "required": ["uri"],
        },
    },
}


def register() -> None:
    register_tool("data_s3_signed_url", data_s3_signed_url, DATA_S3_SIGNED_URL_SCHEMA)
    register_tool("data_bigquery_query", data_bigquery_query, DATA_BIGQUERY_QUERY_SCHEMA)
    register_tool("data_dbt_compile", data_dbt_compile, DATA_DBT_COMPILE_SCHEMA)
    register_tool("data_pipeline_design", data_pipeline_design, DATA_PIPELINE_DESIGN_SCHEMA)
    register_tool("data_quality_check", data_quality_check, DATA_QUALITY_CHECK_SCHEMA)
    register_tool("data_get_editor_context", data_get_editor_context, DATA_GET_EDITOR_CONTEXT_SCHEMA)
    register_tool("data_get_gcp_connection", data_get_gcp_connection, DATA_GET_GCP_CONNECTION_SCHEMA)
    register_tool("data_list_resource_templates", data_list_resource_templates, DATA_LIST_RESOURCE_TEMPLATES_SCHEMA)
    register_tool("data_read_resource", data_read_resource, DATA_READ_RESOURCE_SCHEMA)
