"""Arize Phoenix evaluation harness for the Genesis agent gateway.

Additive package. Nothing here is imported by the Genesis service itself.
"""

from .genesis_client import (
    AgentRunResult,
    GenesisClient,
    LIVE_SLUG_COUNT,
    LIVE_SLUGS,
    MoneyDomainBlocked,
    Outcome,
    UnknownSlug,
    resolve_live_slug,
)
from .redaction import redact
from .target import (
    agenesis_target,
    arun_example,
    genesis_target,
    make_async_target,
    make_target,
    parse_example,
)
from .traceable import (
    RUN_NAME,
    VERDICT_SPAN_NAME,
    emit_verdict_spans,
    traced_agent_run,
    tracing_enabled,
)

__all__ = [
    "AgentRunResult",
    "GenesisClient",
    "LIVE_SLUGS",
    "LIVE_SLUG_COUNT",
    "MoneyDomainBlocked",
    "Outcome",
    "RUN_NAME",
    "VERDICT_SPAN_NAME",
    "UnknownSlug",
    "agenesis_target",
    "emit_verdict_spans",
    "arun_example",
    "genesis_target",
    "make_async_target",
    "make_target",
    "parse_example",
    "redact",
    "resolve_live_slug",
    "traced_agent_run",
    "tracing_enabled",
]
