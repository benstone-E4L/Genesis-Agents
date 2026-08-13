"""Test isolation for the eval suite.

Make the repo root importable so ``import eval`` resolves to this package, and
— more importantly — guarantee that **no test can ever ship a span to the real
Phoenix backend**, regardless of what is in ``.env`` or the ambient shell.

Tests are deliberately plain sync functions driving coroutines through
``asyncio.run`` — no pytest-asyncio dependency, no event-loop config to drift.

Why this is belt-and-braces rather than paranoia:

* Nothing in ``eval/`` calls ``load_dotenv``, so today the ambient environment
  is the only way real credentials could reach a test.
* But Render sets ``PHOENIX_COLLECTOR_ENDPOINT`` and ``PHOENIX_API_KEY`` for
  real and a developer shell may export the same values, either of which would
  silently arm every test in this package against the live Phoenix space.

The pin is **function-scoped and monkeypatch-based**, deliberately. An earlier
revision mutated ``os.environ`` at conftest import time, which is safe for a
variable only this package reads but not for ``PHOENIX_*``: the Genesis runtime
suite in ``tests/`` reads the same variables, and a process-wide pin applied
during collection silently disabled tracing for every test in every other
package for the rest of the session. Function scope means the pin exists exactly
while an eval test runs and pytest unwinds it afterwards.

Import-time pinning is also unnecessary here. ``runtime.phoenix_tracing`` reads
the endpoint and key lazily inside ``get_tracer()``, never at import, and
``_reset_phoenix_tracer`` below drops the memoised tracer around every test — so
there is no window in which a collection-time import could capture a live
credential.

A test that needs tracing on points ``PHOENIX_COLLECTOR_ENDPOINT`` at loopback
and swaps in an in-memory exporter — enforced by
:func:`_forbid_real_phoenix_endpoint`.
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

#: Cleared outright — a real value here is what would let a span ship.
_TRACING_KEYS_TO_CLEAR = (
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "PHOENIX_API_KEY",
    "PHOENIX_PROJECT_NAME",
    # Content capture is off in tests unless a test opts in deliberately.
    "PHOENIX_TRACE_CONTENT",
    "PHOENIX_ALLOW_CONTENT_OFFBOX",
)

#: Forced off.
_TRACING_KEYS_TO_DISABLE = ("PHOENIX_TRACING",)


# The OTLP exporter logs shipment failures at ERROR straight to the root
# handler. A test that deliberately points at a dead endpoint would otherwise
# spray the suite output with connection errors.
logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)


@pytest.fixture(autouse=True)
def hermetic_tracing(monkeypatch):
    """Pin tracing off for every test in this package.

    Autouse, so it applies to every test and cannot be forgotten. A test that
    genuinely needs tracing overrides it with its own ``monkeypatch`` calls,
    which run after this fixture and are unwound the same way.
    """
    for name in _TRACING_KEYS_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)
    for name in _TRACING_KEYS_TO_DISABLE:
        monkeypatch.setenv(name, "false")
    yield


@pytest.fixture(autouse=True)
def _reset_phoenix_tracer():
    """Drop the cached tracer around every test.

    ``runtime.phoenix_tracing`` memoises the tracer and its "already tried"
    flag, so without this a test that configures an endpoint would leak a live
    tracer into the next test — and a test that ran first with tracing off would
    pin every later test to ``None`` regardless of its own environment.
    """
    try:
        from runtime import phoenix_tracing
    except Exception:
        yield
        return
    phoenix_tracing.reset_for_tests()
    yield
    phoenix_tracing.reset_for_tests()


@pytest.fixture(autouse=True)
def _forbid_real_phoenix_endpoint():
    """After every test, assert it did not point at a reachable Phoenix host.

    A test may enable tracing, but only against loopback. This catches the
    accident of enabling tracing and forgetting to redirect the endpoint — which
    against Phoenix Cloud would publish evaluation content to a third party.
    """
    yield
    endpoint = (
        os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
        or os.environ.get("PHOENIX_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    )
    if endpoint:
        assert "127.0.0.1" in endpoint or "localhost" in endpoint, (
            f"a test pointed tracing at a non-loopback Phoenix endpoint: {endpoint}"
        )
