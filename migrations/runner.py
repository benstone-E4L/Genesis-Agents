"""Genesis SQL migration runner.

CLI entrypoint: ``scripts/migrate.py`` (thin wrapper). This module holds the
logic and is directly importable for tests.

    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --check    # report pending, exit 2 if any
    python scripts/migrate.py --status   # print the applied ledger

Render pre-deploy command:

    python scripts/migrate.py

Why this exists
---------------
The repository shipped ``migrations/*.sql`` with no runner, so applying them was
a manual ``psql -f`` that nobody could prove had happened. Production schema
drift is what made every async AP2 dispatch return HTTP 500 while 829 tests
passed against an in-memory mock. A migration is not "applied" because a file
exists in git; it is applied when a row says so and the checksum still matches.

Guarantees
----------
1. **Ledger with checksums.** ``genesis_schema_migrations`` records filename,
   sha256 of the file bytes, applied_at and duration_ms.
2. **Checksum drift is fatal, and never auto-repaired.** If an already-applied
   file's bytes changed, the runner refuses to do anything at all — it does not
   re-apply, does not update the recorded checksum, and does not apply the
   *other* pending migrations either. Editing an applied migration means the
   database and the repository disagree about what the schema is; guessing which
   one is right is not the runner's job.
3. **One transaction per migration.** The ledger row is written inside the same
   transaction as the DDL, so a migration is recorded if and only if it applied.
   A failure rolls the whole thing back and nothing partial is recorded.
4. **Lexical order, no gaps.** A pending file that sorts *before* an already
   applied one is refused. Out-of-order application produces a schema that no
   fresh database will ever reproduce.
5. **Idempotent.** Re-running when current is a no-op that exits 0.
6. **Single applier.** A session-level advisory lock is held for the run, so two
   Render instances booting at once cannot both apply the same migration.

Transaction control in the .sql files
-------------------------------------
The existing files wrap themselves in ``BEGIN; ... COMMIT;``. If those were sent
verbatim inside the runner's transaction, the embedded ``COMMIT`` would end it
early and the ledger INSERT would land in a *separate* transaction — precisely
the partial-record failure this runner exists to prevent. The outer
``BEGIN``/``COMMIT`` pair is therefore stripped and the runner owns the
transaction. Any *other* transaction-control statement in the body is a hard
error rather than a silent surprise. The checksum is always taken over the raw
file bytes, not the stripped text, so it means "this file, as committed".
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__ in (None, ""):  # allow `python migrations/runner.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import pg_store
from runtime.pg_store import StoreUnavailable

MIGRATIONS_DIR = Path(__file__).resolve().parent
LEDGER_TABLE = "genesis_schema_migrations"

#: Held for the whole run so two instances cannot apply concurrently.
ADVISORY_LOCK_KEY = pg_store.advisory_key("genesis:schema_migrations")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PENDING = 2

#: A migration file opens with a licence/rationale comment block, so the outer
#: BEGIN is not necessarily the first token. Leading line comments are skipped.
_LEADING_BEGIN = re.compile(
    r"\A(?:\s*--[^\n]*\n)*\s*(BEGIN|START\s+TRANSACTION)\s*;", re.IGNORECASE
)
_TRAILING_COMMIT = re.compile(r"(COMMIT|END)\s*;\s*\Z", re.IGNORECASE)
#: Transaction control left in the body after stripping the outer pair. Any of
#: these would silently break the "one transaction per migration" guarantee.
_INNER_TXN_CONTROL = re.compile(
    r"(?<![\w.])(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|START\s+TRANSACTION)(?![\w.])", re.IGNORECASE
)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
#: PL/pgSQL bodies are dollar-quoted and legitimately contain BEGIN ... END.
#: Scanning them would reject `CREATE FUNCTION ... AS $$ BEGIN ... END $$`,
#: which is ordinary, correct SQL and not transaction control at all.
_DOLLAR_QUOTED = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$.*?\$\1?\$", re.DOTALL)


def _without_comments(sql: str) -> str:
    """Comment- and dollar-quote-stripped copy used only for scanning.

    Every migration in this repository opens with a long rationale comment, and
    those comments discuss transaction handling by name. Scanning the raw text
    for ``BEGIN`` would reject a perfectly correct file because its own
    documentation mentions the word. Dollar-quoted bodies are removed for the
    same reason: the BEGIN inside a PL/pgSQL function is a block, not a
    transaction.
    """
    return _LINE_COMMENT.sub("", _DOLLAR_QUOTED.sub("", _BLOCK_COMMENT.sub("", sql)))

_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
  filename    text PRIMARY KEY,
  checksum    text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now(),
  duration_ms integer NOT NULL DEFAULT 0
)
"""


class MigrationError(RuntimeError):
    """A migration cannot proceed. Always fatal — the runner never repairs."""


class ChecksumMismatch(MigrationError):
    """An already-applied migration's file bytes changed since it was applied."""


class OutOfOrderMigration(MigrationError):
    """A pending migration sorts before one that is already applied."""


class MissingMigrationFile(MigrationError):
    """The ledger records a migration whose file is no longer in the repository."""


@dataclass(frozen=True)
class Migration:
    filename: str
    path: Path
    checksum: str
    sql: str
    #: Checksums of this file's content under either line-ending convention.
    #: Defaults to just ``checksum`` so a Migration built by hand (tests, tools)
    #: behaves exactly as it did before line-ending tolerance existed.
    equivalents: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.equivalents:
            object.__setattr__(self, "equivalents", frozenset({self.checksum}))


@dataclass
class RunResult:
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    durations_ms: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.pending


def checksum_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def equivalent_checksums(raw: bytes) -> frozenset[str]:
    """Every checksum the SAME migration content can legitimately have.

    ``core.autocrlf=true`` is the Git default on Windows and this repository has
    no ``.gitattributes``, so the identical committed file is CRLF in a Windows
    checkout and LF on Render. Checksumming raw bytes therefore gives one
    migration two identities, and because checksum drift makes the runner refuse
    to apply ANYTHING, a line ending was enough to block every deploy while
    accusing the operator of having edited an applied migration.

    Content changes still change all three forms, so this tolerates line endings
    without tolerating edits.
    """
    lf = raw.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    return frozenset({checksum_bytes(raw), checksum_bytes(lf), checksum_bytes(crlf)})


def strip_outer_transaction(sql: str) -> str:
    """Remove a file's own outer ``BEGIN;``/``COMMIT;`` so the runner owns the txn.

    Raises :class:`MigrationError` if transaction control survives the strip —
    an inner COMMIT would break the atomic "DDL + ledger row" pairing, and
    failing loudly beats writing a ledger row for a migration that half ran.
    """
    body = sql.strip()
    match = _LEADING_BEGIN.search(body)
    if match:
        # Replace only the BEGIN token itself, keeping the leading comments so a
        # failing statement's context is still readable in a server-side error.
        body = (body[: match.start(1)] + body[match.end() :]).strip()
        if not _TRAILING_COMMIT.search(body):
            raise MigrationError("migration opens a transaction it never commits")
        body = _TRAILING_COMMIT.sub("", body, count=1).strip()
    leftover = _INNER_TXN_CONTROL.search(_without_comments(body))
    if leftover:
        raise MigrationError(
            f"migration contains transaction control ({leftover.group(0).upper()}) inside its "
            "body; the runner must own the transaction so the ledger row and the DDL commit "
            "together"
        )
    return body


def discover(directory: Path | None = None) -> list[Migration]:
    """Every ``NNN_*.sql`` in *directory*, in lexical order."""
    base = Path(directory) if directory else MIGRATIONS_DIR
    found: list[Migration] = []
    for path in sorted(base.glob("*.sql"), key=lambda p: p.name):
        raw = path.read_bytes()
        found.append(
            Migration(
                filename=path.name,
                path=path,
                checksum=checksum_bytes(raw),
                sql=raw.decode("utf-8"),
                equivalents=equivalent_checksums(raw),
            )
        )
    return found


def _ensure_ledger(cur: Any) -> None:
    cur.execute(_LEDGER_DDL)


def _read_ledger(cur: Any) -> dict[str, dict[str, Any]]:
    cur.execute(
        f"SELECT filename, checksum, applied_at, duration_ms FROM {LEDGER_TABLE} ORDER BY filename"
    )
    return {row["filename"]: dict(row) for row in cur.fetchall()}


def reconcile(
    migrations: Sequence[Migration], applied: dict[str, dict[str, Any]]
) -> list[Migration]:
    """Validate the ledger against the files and return what is pending.

    Every check here is fatal by design. This function does not mutate anything,
    so ``--check`` gets exactly the same verdict the apply path would.
    """
    on_disk = {m.filename: m for m in migrations}

    drifted = [
        (name, record["checksum"], on_disk[name].checksum)
        for name, record in applied.items()
        if name in on_disk and record["checksum"] not in on_disk[name].equivalents
    ]
    if drifted:
        detail = "; ".join(
            f"{name}: applied sha256={was[:12]}… file sha256={now[:12]}…"
            for name, was, now in sorted(drifted)
        )
        raise ChecksumMismatch(
            "refusing to run — an already-applied migration has been edited since it was "
            f"applied ({detail}). The database and this repository disagree about the schema. "
            "Do not edit an applied migration: add a new one. If the edit was intentional and "
            "the database really is correct, update the checksum in "
            f"{LEDGER_TABLE} by hand, deliberately."
        )

    vanished = sorted(set(applied) - set(on_disk))
    if vanished:
        raise MissingMigrationFile(
            "refusing to run — the ledger records migrations whose files are missing from the "
            f"repository: {', '.join(vanished)}. A fresh database can no longer be built to "
            "match production."
        )

    pending = [m for m in migrations if m.filename not in applied]
    if applied and pending:
        highest_applied = max(applied)
        late = [m.filename for m in pending if m.filename < highest_applied]
        if late:
            raise OutOfOrderMigration(
                "refusing to run — pending migrations sort before the highest applied one "
                f"({highest_applied}): {', '.join(late)}. Applying them now would produce a "
                "schema no fresh database can reproduce. Renumber them to sort last."
            )
    return pending


def _apply_one(migration: Migration) -> int:
    """Apply one migration and its ledger row in a single transaction.

    A new connection per migration keeps the blast radius of a failure to that
    migration: a rolled-back DDL never leaves the *next* one running inside a
    poisoned transaction.
    """
    body = strip_outer_transaction(migration.sql)
    started = time.monotonic()
    with pg_store.transaction() as cur:
        _ensure_ledger(cur)
        if body:
            cur.execute(body)
        duration_ms = int((time.monotonic() - started) * 1000)
        cur.execute(
            f"INSERT INTO {LEDGER_TABLE} (filename, checksum, applied_at, duration_ms) "
            "VALUES (%s, %s, now(), %s)",
            (migration.filename, migration.checksum, duration_ms),
        )
    return duration_ms


def _assert_lock_alive(conn: Any) -> None:
    """Raise unless *conn* is still up and still holding the migration lock.

    The lock is session-scoped, so if this connection dies — pooler recycle,
    network blip, server restart — the lock is released instantly and silently
    while the runner carries on believing it is the only applier. A second
    deploy can then start applying the same files. The ledger primary key stops
    an actual double-apply, but two concurrent DDL streams against one database
    is not a state to continue into on a guess.
    """
    try:
        if getattr(conn, "closed", False):
            raise MigrationError(
                "lost the migration advisory lock: the lock connection closed mid-run. "
                "Another deploy may now be applying concurrently. Re-run the migration."
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS held FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND ((classid::bigint << 32) | objid::bigint) = %s",
                (ADVISORY_LOCK_KEY,),
            )
            row = cur.fetchone()
            held = int((row or {}).get("held", 0))
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(
            f"lost the migration advisory lock: {type(exc).__name__}: {exc}"
        ) from exc
    if held < 1:
        raise MigrationError(
            "lost the migration advisory lock: this session no longer holds it, so another "
            "applier is no longer excluded. Re-run the migration."
        )


def _with_advisory_lock(fn):
    """Run *fn* while holding the global migration advisory lock.

    Session-scoped on a dedicated connection: a concurrent applier blocks rather
    than racing. The lock is released with the connection even if the process is
    killed, so a crashed deploy cannot wedge the next one.

    ``statement_timeout`` is disabled on this connection on purpose. Waiting for
    a concurrent deploy to finish and running DDL on a large table are both
    legitimately slow; the request-path ceiling would abort them.
    """
    conn = pg_store.connect(autocommit=True, statement_timeout_override_ms=0)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        return fn(conn)
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def status(directory: Path | None = None) -> RunResult:
    """Read-only: what is applied, what is pending. Raises on any drift."""
    migrations = discover(directory)
    with pg_store.transaction() as cur:
        _ensure_ledger(cur)
        applied = _read_ledger(cur)
    pending = reconcile(migrations, applied)
    return RunResult(
        applied=[],
        pending=[m.filename for m in pending],
        already_applied=sorted(applied),
        durations_ms={n: int(r["duration_ms"]) for n, r in applied.items()},
    )


def migrate(directory: Path | None = None) -> RunResult:
    """Apply every pending migration in lexical order. Idempotent."""

    def _run(lock_conn: Any) -> RunResult:
        migrations = discover(directory)
        with pg_store.transaction() as cur:
            _ensure_ledger(cur)
            applied = _read_ledger(cur)
        pending = reconcile(migrations, applied)
        result = RunResult(pending=[], already_applied=sorted(applied))
        for migration in pending:
            # Re-checked before every file, not just at the start: a run can
            # take minutes and the lock can be lost at any point in it.
            _assert_lock_alive(lock_conn)
            result.durations_ms[migration.filename] = _apply_one(migration)
            result.applied.append(migration.filename)
        return result

    return _with_advisory_lock(_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Apply Genesis SQL migrations to the configured Postgres database.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report pending migrations without applying; exit 2 when any are pending",
    )
    parser.add_argument(
        "--status", action="store_true", help="print the applied-migration ledger and exit 0"
    )
    parser.add_argument(
        "--dir", default=None, help="migrations directory (default: alongside this file)"
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    directory = Path(args.dir) if args.dir else None

    try:
        if args.check or args.status:
            result = status(directory)
            for name in result.already_applied:
                print(f"applied  {name}")
            for name in result.pending:
                print(f"PENDING  {name}")
            if args.status:
                return EXIT_OK
            if result.pending:
                print(f"{len(result.pending)} migration(s) pending", file=sys.stderr)
                return EXIT_PENDING
            print("schema is current")
            return EXIT_OK

        result = migrate(directory)
        for name in result.applied:
            print(f"applied  {name} ({result.durations_ms.get(name, 0)}ms)")
        if not result.applied:
            print(f"schema is current ({len(result.already_applied)} migration(s) applied)")
        return EXIT_OK
    except (MigrationError, StoreUnavailable) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # a broken .sql file, a permissions error, a dropped connection
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
