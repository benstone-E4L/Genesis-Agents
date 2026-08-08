"""Fake transport + fake LangSmith. No test in this package touches the network."""

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


class FakeRunTree:
    """Stand-in for a LangSmith RunTree. Captures whatever metadata is attached."""

    def __init__(self) -> None:
        self.metadata: dict[str, Any] = {}


class FakeLangSmith:
    """Fake ``langsmith.traceable``. Serialises inputs/outputs/metadata exactly
    as the real SDK would send them, so tests can grep the payload for secrets.
    """

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.run_tree = FakeRunTree()

    def traceable(self, **decorator_kwargs: Any) -> Callable:
        outer = self

        def decorator(fn: Callable) -> Callable:
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                process_inputs = decorator_kwargs.get("process_inputs") or (lambda x: x)
                process_outputs = decorator_kwargs.get("process_outputs") or (lambda x: x)
                outer.run_tree = FakeRunTree()
                run: dict[str, Any] = {
                    "name": decorator_kwargs.get("name"),
                    "run_type": decorator_kwargs.get("run_type"),
                    "inputs": process_inputs(
                        args[0] if args else dict(kwargs)
                    ),
                    "outputs": None,
                    "error": None,
                    "metadata": outer.run_tree.metadata,
                }
                outer.runs.append(run)
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as exc:
                    run["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                run["outputs"] = process_outputs(result)
                return result

            wrapper.__name__ = getattr(fn, "__name__", "wrapper")
            return wrapper

        return decorator

    def get_current_run_tree(self) -> FakeRunTree:
        return self.run_tree

    def serialised(self) -> str:
        """Everything that would leave the process, as one JSON string."""
        return json.dumps(self.runs, default=str)


class ExplodingLangSmith:
    """A LangSmith that fails at decoration time — proves graceful degradation."""

    def traceable(self, **kwargs: Any) -> Callable:
        raise RuntimeError("langsmith backend unreachable")


class ExplodingAtCallLangSmith:
    """A LangSmith that decorates fine but fails when the run is flushed."""

    def traceable(self, **kwargs: Any) -> Callable:
        def decorator(fn: Callable) -> Callable:
            async def wrapper(*args: Any, **kw: Any) -> Any:
                result = await fn(*args, **kw)
                raise RuntimeError("failed to POST run to LangSmith")

            return wrapper

        return decorator


def read_timeout(msg: str = "timed out waiting for response") -> TransportFailure:
    return TransportFailure("read", msg)


def connect_error(msg: str = "connection refused") -> TransportFailure:
    return TransportFailure("connect", msg)
