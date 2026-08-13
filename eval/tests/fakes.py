"""Fake transport + in-memory Phoenix. No test in this package touches the network."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from eval.genesis_client import RawResponse, TransportFailure


class FakeTransport:
    """Scripted transport.

    ``script`` is a list of either :class:`RawResponse`, :class:`TransportFailure`
    (raised), or a callable taking the recorded request dict. The last entry
    repeats once the script is exhausted, so a "always 503" case needs one entry.
    """

    def __init__(self, script: list[Any], health: Any = None) -> None:
        self.script = list(script)
        self.health = health if health is not None else RawResponse(200, '{"status":"ok"}')
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0
        self.closed = False
        self._idx = 0

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any | None,
        timeout_s: float,
    ) -> RawResponse:
        record = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "json": json_body,
            "timeout_s": timeout_s,
        }
        if url.endswith("/health"):
            self.health_calls += 1
            if isinstance(self.health, BaseException):
                raise self.health
            return self.health

        self.calls.append(record)
        if not self.script:
            return RawResponse(200, '{"response":"ok"}')
        item = self.script[min(self._idx, len(self.script) - 1)]
        self._idx += 1
        if callable(item) and not isinstance(item, (RawResponse, BaseException)):
            item = item(record)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True

    # -- assertions helpers ------------------------------------------------

    @property
    def run_call_count(self) -> int:
        return len(self.calls)

    def serialised_requests(self) -> str:
        return json.dumps(self.calls, default=str)


def ok_response(text: str = "hello", **extra: Any) -> RawResponse:
    body = {"response": text, "agentSlug": "genesis_research_x402",
            "agentName": "Genesis Research Agent"}
    body.update(extra)
    return RawResponse(200, json.dumps(body))


class RecordingSleep:
    """Replacement for asyncio.sleep that records delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class InMemoryPhoenix:
    """A real OpenTelemetry pipeline with an in-memory exporter.

    Deliberately NOT a hand-written mock of the tracing API. The thing under
    test is what actually lands in a span's exported attributes, so this runs
    genuine OTel span construction and attribute coercion and only replaces the
    network exporter. A hand-rolled fake would happily "record" a value that the
    real SDK would drop, coerce or serialise differently — which is exactly the
    class of bug a redaction proof must not be blind to.
    """

    def __init__(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def install(self, monkeypatch: Any, *, content: bool = False) -> "InMemoryPhoenix":
        """Point eval tracing at this exporter. Loopback endpoint only.

        ``content=True`` opts into prompt/response capture, which is what makes a
        redaction proof non-vacuous: with content withheld there is nothing on
        the span for a secret to leak *through*, and the test would pass for the
        wrong reason.
        """
        from runtime import phoenix_tracing

        monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
        monkeypatch.setenv("PHOENIX_TRACING", "true")
        if content:
            monkeypatch.setenv("PHOENIX_TRACE_CONTENT", "1")
        monkeypatch.setattr(
            phoenix_tracing, "_build_tracer", lambda: self.provider.get_tracer("test")
        )
        phoenix_tracing.reset_for_tests()
        return self

    @property
    def spans(self) -> list[Any]:
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list[Any]:
        return [s for s in self.spans if s.name == name]

    def clear(self) -> None:
        self.exporter.clear()

    def serialised(self) -> str:
        """Everything that would leave the process, as one JSON string."""
        return json.dumps(
            [
                {
                    "name": s.name,
                    "attributes": dict(s.attributes or {}),
                    "status": getattr(s.status, "description", None),
                    "status_code": str(getattr(s.status, "status_code", "")),
                    "events": [
                        {"name": e.name, "attributes": dict(e.attributes or {})}
                        for e in (s.events or ())
                    ],
                }
                for s in self.spans
            ],
            default=str,
        )


class ExplodingTracer:
    """A tracer that fails when a span is started — proves graceful degradation."""

    def start_as_current_span(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("phoenix collector unreachable")

    def start_span(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("phoenix collector unreachable")


class ExplodingAtEndTracer:
    """A tracer whose spans fail on exit — the "flush failed" shape."""

    class _Span:
        def set_attribute(self, *_a: Any, **_k: Any) -> None:
            return None

        def set_status(self, *_a: Any, **_k: Any) -> None:
            return None

        def get_span_context(self) -> Any:
            raise RuntimeError("no context")

        def end(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("failed to export span batch")

    class _Ctx:
        def __enter__(self) -> Any:
            return ExplodingAtEndTracer._Span()

        def __exit__(self, *_a: Any) -> None:
            raise RuntimeError("failed to export span batch")

    def start_as_current_span(self, *_a: Any, **_k: Any) -> Any:
        return self._Ctx()

    def start_span(self, *_a: Any, **_k: Any) -> Any:
        return self._Span()


def read_timeout(msg: str = "timed out waiting for response") -> TransportFailure:
    return TransportFailure("read", msg)


def connect_error(msg: str = "connection refused") -> TransportFailure:
    return TransportFailure("connect", msg)
