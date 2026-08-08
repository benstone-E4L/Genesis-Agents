"""Test isolation for the eval suite.

Make the repo root importable so ``import eval`` resolves to this package, and
— more importantly — guarantee that **no test can ever ship a run to the real
LangSmith backend**, regardless of what is in ``.env`` or the ambient shell.

Tests are deliberately plain sync functions driving coroutines through
``asyncio.run`` — no pytest-asyncio dependency, no event-loop config to drift.

Why this is belt-and-braces rather than paranoia:

* The installed ``langsmith`` SDK does **not** auto-load ``.env`` (verified), and
  nothing in ``eval/`` calls ``load_dotenv``. So today the ambient environment
  is the only way real credentials could reach a test.
* But ``eval/`` will be imported alongside sibling modules (a runner, a rubric
  set) that may well load ``.env`` at import time, and a developer shell may
  export the real key. Either would silently arm every test in this file.
* So the guard is applied twice: at conftest **import** time (before any test
  module or client object is constructed) and again as a session-scoped autouse
  fixture (after all collection-time imports have run, so it wins over anything
  a sibling conftest loaded).

The single test that must exercise the real SDK opts back in explicitly with
function-scoped ``monkeypatch``, and is required to point ``LANGSMITH_ENDPOINT``
at a closed loopback port — enforced by :func:`_forbid_real_langsmith_endpoint`.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Cleared outright — a real value here is what would let a run ship.
_TRACING_KEYS_TO_CLEAR = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_ENDPOINT",
    "LANGSMITH_PROJECT",
    "LANGCHAIN_PROJECT",
)

#: Forced off.
_TRACING_KEYS_TO_DISABLE = (
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING_V2",
)


def _pin_tracing_off() -> None:
    for name in _TRACING_KEYS_TO_CLEAR:
        os.environ.pop(name, None)
    for name in _TRACING_KEYS_TO_DISABLE:
        os.environ[name] = "false"


# Applied at import time, before any test module is imported.
_pin_tracing_off()

# The SDK logs shipment failures at WARNING straight to the root handler. The
# one test that deliberately points at a dead endpoint would otherwise spray
# the suite output with connection errors.
logging.getLogger("langsmith").setLevel(logging.CRITICAL)


@pytest.fixture(scope="session", autouse=True)
def hermetic_tracing():
    """Re-pin tracing off after all collection-time imports have run.

    Session-scoped and autouse, so it applies to every test and cannot be
    forgotten. A test that genuinely needs tracing overrides it with
    function-scoped ``monkeypatch``, which pytest unwinds afterwards.
    """
    saved = {
        name: os.environ.get(name)
        for name in _TRACING_KEYS_TO_CLEAR + _TRACING_KEYS_TO_DISABLE
    }
    _pin_tracing_off()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def _forbid_real_langsmith_endpoint():
    """After every test, assert it did not point at a reachable LangSmith host.

    A test may enable tracing, but only against loopback. This catches the
    accident of enabling tracing and forgetting to redirect the endpoint.
    """
    yield
    endpoint = os.environ.get("LANGSMITH_ENDPOINT") or os.environ.get(
        "LANGCHAIN_ENDPOINT"
    )
    if os.environ.get("LANGSMITH_API_KEY") and endpoint:
        assert "127.0.0.1" in endpoint or "localhost" in endpoint, (
            "a test enabled tracing against a non-loopback LangSmith endpoint: "
            f"{endpoint}"
        )
