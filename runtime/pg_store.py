"""Canonical Postgres connection + backend selection for every Genesis state store.

Why this module exists
----------------------
Genesis used to keep three classes of production state on a local disk: AP2
nonces and consumed action grants in ``GENESIS_AUTH_DB_PATH`` (SQLite), the
tamper-evident audit chain in ``GENESIS_AUDIT_DB_PATH`` (SQLite), and the daily
Merkle anchors in ``GENESIS_ANCHOR_STORE_PATH`` (JSONL). On Render that means a
persistent disk is load-bearing for *correctness*, not just convenience:

* a disk that fails to mount re-opens the AP2 replay window silently,
* an ephemeral filesystem makes single-use action grants replayable after a
  restart,
* the audit chain restarts from an empty prev_hash, so the chain no longer
  proves anything about what happened before the last deploy,
* and no second instance can ever be run, because two Render instances would
  each hold a *different* audit chain for the same tenant.

Every one of those stores now selects Postgres when a database URL is
configured. SQLite/JSONL remain for local development and the test suite.

Backend selection
-----------------
``GENESIS_STORE_BACKEND`` — ``auto`` (default), ``postgres`` or ``sqlite``.

* ``auto`` selects Postgres when :func:`database_url` resolves to a non-empty
  URL, otherwise SQLite. This is what production gets: Render already sets
  ``GENESIS_JOB_DATABASE_URL``.
* ``postgres`` forces Postgres and refuses to fall back. Setting this is how a
  deployment declares "a missing database URL is a boot failure, not a silent
  downgrade to a file nobody is backing up".
* ``sqlite`` forces the legacy file backend. For local dev only.

Fail-closed
-----------
Nothing in this module degrades to "allow" on a database error. Callers that
protect a security decision (nonce consumption, grant single-use, audit-before-
side-effect) surface :class:`StoreUnavailable` and refuse the operation. That is
the whole point: an unreachable store must never be indistinguishable from a
store that said yes.
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any, Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query params that psycopg rejects but Prisma/Supabase pooled URLs commonly
#: carry. Stripped rather than failing the connection.
UNSUPPORTED_PSYCOPG_QUERY_PARAMS = {"connection_limit", "pgbouncer"}

#: Resolution order. ``GENESIS_JOB_DATABASE_URL`` is the canonical variable for
#: the whole service — jobs, auth state and audit all share one database.
DATABASE_URL_ENV_ORDER = ("GENESIS_JOB_DATABASE_URL", "DIRECT_URL", "DATABASE_URL")

_FALSEY = {"0", "false", "no", "off", ""}


class StoreUnavailable(RuntimeError):
    """The backing store could not be reached or written.

    Never caught-and-ignored by a security-relevant caller. Callers translate it
    into their own refusal type (``AuthenticationError``, ``GrantError``) so the
    operation fails closed with a diagnosable name.
    """


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def database_url() -> str:
    """Return a psycopg-compatible Postgres URL, or "" when none is configured.

    Supabase/Prisma pooled URLs commonly include ``pgbouncer=true`` and
    ``connection_limit``; psycopg rejects unknown query params, so those two are
    stripped while SSL and every other connection setting is preserved.
    """
    raw = ""
    for name in DATABASE_URL_ENV_ORDER:
        raw = _env(name)
        if raw:
            break
    if not raw:
        return ""

    parts = urlsplit(raw)
    if not parts.query:
        return raw
    filtered = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in UNSUPPORTED_PSYCOPG_QUERY_PARAMS
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment)
    )


def backend() -> str:
    """``"postgres"`` or ``"sqlite"`` — the store family every caller must use."""
    declared = _env("GENESIS_STORE_BACKEND").lower()
    if declared == "postgres":
        return "postgres"
    if declared == "sqlite":
        return "sqlite"
    if declared and declared != "auto":
        raise StoreUnavailable(f"GENESIS_STORE_BACKEND_invalid:{declared}")
    return "postgres" if database_url() else "sqlite"


def postgres_selected() -> bool:
    return backend() == "postgres"


def require_database_url() -> str:
    """The Postgres URL, or :class:`StoreUnavailable` when it is missing.

    ``GENESIS_STORE_BACKEND=postgres`` with no URL is a configuration error that
    must surface as a refusal, never as a silent fall back to a local file.
    """
    url = database_url()
    if not url:
        raise StoreUnavailable("GENESIS_JOB_DATABASE_URL_not_configured")
    return url


#: Default ceiling on how long ANY single statement may run. Chosen because
#: every statement Genesis issues on a request path is a single-row insert,
#: delete-by-index or short select; anything taking longer than this is blocked,
#: not working. Set ``GENESIS_DB_STATEMENT_TIMEOUT_MS=0`` to disable, which the
#: migration runner does because DDL on a large table legitimately takes minutes.
DEFAULT_STATEMENT_TIMEOUT_MS = 15000

#: Ceiling on connections this process will hold at once. Every store operation
#: opens its own connection, so without a cap a burst of concurrent requests
#: races every other client of the same database to ``max_connections`` — and a
#: database that has run out of connection slots takes down the jobs, the audit
#: chain and the nonce store together.
DEFAULT_MAX_CONNECTIONS = 10
DEFAULT_ACQUIRE_TIMEOUT_S = 10.0

_cap_lock = threading.Lock()
_connection_slots: Any = None
_connection_slots_size = 0


def statement_timeout_ms() -> int:
    raw = _env("GENESIS_DB_STATEMENT_TIMEOUT_MS")
    if not raw:
        return DEFAULT_STATEMENT_TIMEOUT_MS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_STATEMENT_TIMEOUT_MS


def max_connections() -> int:
    raw = _env("GENESIS_DB_MAX_CONNECTIONS")
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_CONNECTIONS
    except ValueError:
        return DEFAULT_MAX_CONNECTIONS


def _slots() -> Any:
    """The process-wide connection semaphore, rebuilt when the cap changes."""
    global _connection_slots, _connection_slots_size
    size = max_connections()
    with _cap_lock:
        if _connection_slots is None or _connection_slots_size != size:
            _connection_slots = threading.BoundedSemaphore(size)
            _connection_slots_size = size
        return _connection_slots


def reset_connection_cap_for_tests() -> None:
    global _connection_slots, _connection_slots_size
    with _cap_lock:
        _connection_slots = None
        _connection_slots_size = 0


def connect(*, autocommit: bool = False, statement_timeout_override_ms: int | None = None) -> Any:
    """Open a psycopg connection. Raises :class:`StoreUnavailable` on failure.

    ``connect_timeout`` is always bounded: an unbounded blocking connect against
    a saturated pooler would stall a request thread indefinitely, which for the
    audit path means the side effect proceeds while its "durable" record is
    still waiting on a socket.

    ``statement_timeout`` is bounded for the same reason one level up. Connect
    timeouts do nothing once the socket is established: an append waiting on the
    per-session ``pg_advisory_xact_lock`` held by a stuck writer blocks forever,
    one wedged thread per attempt, with no error and no signal. A server-side
    timeout turns that into a fail-closed refusal.
    """
    url = require_database_url()
    timeout_ms = (
        statement_timeout_ms()
        if statement_timeout_override_ms is None
        else max(0, int(statement_timeout_override_ms))
    )
    try:
        import psycopg
        from psycopg.rows import dict_row

        kwargs: dict[str, Any] = {}
        if timeout_ms:
            kwargs["options"] = f"-c statement_timeout={timeout_ms}"
        return psycopg.connect(
            url,
            row_factory=dict_row,
            prepare_threshold=None,
            autocommit=autocommit,
            connect_timeout=int(_env("GENESIS_DB_CONNECT_TIMEOUT_S") or "10"),
            **kwargs,
        )
    except StoreUnavailable:
        raise
    except Exception as exc:  # psycopg.OperationalError, ImportError, ValueError
        raise StoreUnavailable(f"{type(exc).__name__}: {exc}") from exc


@contextmanager
def transaction(*, autocommit: bool = False) -> Iterator[Any]:
    """Connection + cursor, committed on success and rolled back on any error.

    psycopg's own ``with connection`` commits on exit, but it leaves the
    connection open for reuse from a pool Genesis does not have. This closes it,
    so a failed migration or a failed audit append cannot leave a half-open
    transaction holding an advisory lock.

    Concurrency is capped here rather than in :func:`connect` because this is the
    only path with a guaranteed close: every request-path caller (nonces, action
    grants, audit chain, anchors) goes through it, so the slot cannot leak. A
    slot that leaked would convert a transient error into a permanent outage,
    which is strictly worse than the exhaustion it protects against.
    """
    slots = _slots()
    timeout = float(_env("GENESIS_DB_ACQUIRE_TIMEOUT_S") or DEFAULT_ACQUIRE_TIMEOUT_S)
    if not slots.acquire(timeout=timeout):
        raise StoreUnavailable(
            f"db_connection_cap_reached: {max_connections()} concurrent connections already "
            f"open from this process and none freed within {timeout:g}s"
        )
    try:
        conn = connect(autocommit=autocommit)
    except BaseException:
        slots.release()
        raise
    try:
        with conn.cursor() as cur:
            yield cur
        if not autocommit:
            conn.commit()
    except Exception:
        try:
            if not autocommit:
                conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
        slots.release()


def advisory_key(name: str) -> int:
    """Deterministic signed 64-bit advisory-lock key for an arbitrary string.

    Derived in Python from SHA-256 rather than via Postgres ``hashtext`` so the
    key does not depend on an undocumented internal function whose algorithm has
    changed between major versions. A collision between two different session
    ids costs a little serialisation, never correctness.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()[:8]
    value = int.from_bytes(digest, "big", signed=False)
    return value - (1 << 64) if value >= (1 << 63) else value


_unique_violation_cache: Any = None
_unique_lock = threading.Lock()


def is_unique_violation(exc: BaseException) -> bool:
    """True when *exc* is a Postgres unique-constraint violation.

    Used to turn "this nonce/grant row already exists" into a replay refusal
    rather than a store-unavailable refusal. The two must not be conflated: one
    means the caller cheated, the other means Genesis cannot tell.
    """
    global _unique_violation_cache
    if _unique_violation_cache is None:
        with _unique_lock:
            if _unique_violation_cache is None:
                try:
                    from psycopg import errors

                    _unique_violation_cache = errors.UniqueViolation
                except Exception:
                    return False
    return isinstance(exc, _unique_violation_cache)


def store_is_usable() -> tuple[bool, str]:
    """Boot probe: can the selected Postgres store actually be read? (ok, reason).

    Checks the tables the security paths depend on actually exist. A reachable
    database with no schema is exactly the drift that let every async AP2
    dispatch 500 while the mocked test suite stayed green.
    """
    # Anchors and chain heads are in the list because a database missing 004/005
    # boots "usable" and then fails on the first anchor write or verifies a
    # truncated chain as intact — both silent, both at 3 AM.
    required = (
        "genesis_ap2_nonces",
        "genesis_action_grants",
        "genesis_audit_log",
        "genesis_audit_anchors",
        "genesis_audit_chain_heads",
    )
    try:
        with transaction() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = ANY(current_schemas(false))"
                " AND tablename = ANY(%s)",
                (list(required),),
            )
            present = {row["tablename"] for row in cur.fetchall()}
        missing = sorted(set(required) - present)
        if missing:
            return False, (
                "missing tables: " + ", ".join(missing) + " — run `python scripts/migrate.py`"
            )
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
