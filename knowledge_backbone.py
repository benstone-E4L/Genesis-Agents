"""CHUNK_3_DEDUP — dedup, permission-filter, and canonical-precedence logic for the
`knowledge_backbone` second retrieval backend (SPEC-e4l-drive-knowledge-integration.md §6, §9).

Deliberately backend-agnostic: `SPEC-e4l-drive-knowledge-integration.md`'s Open Question 3
(whether this logic ultimately lives inside `kb-mcp-prod`'s own query layer, if Shreyas owns and
prefers that, or inside this repo's `retrieval_store.py`) is not yet resolved. Every function
here is a pure function over plain dicts shaped like spec §6's row contract — no network call,
no DB connection, no import of `knowledge_backbone_store.py` (CHUNK_4's backend-selection
module) or any `kb-mcp-prod`/Postgres client. Whichever side Open Question 3 lands on can import
and call these functions unchanged; nothing here assumes it is running inside Genesis.

Row shape this module expects (spec §6 — required columns, checked structurally, not by type):
    source_account         'canonical' | 'controller'
    source_classification  'canonical' | 'controller'  (duplicate-safe merge key)
    drive_id               str
    file_id                str
    content_hash           str   -- sha256, the dedup fallback key
    permissions_snapshot   dict | None  -- {"principals": [str, ...], "public": bool} or None
    score                  float (optional; used only as a tiebreaker within a dedup group)
Any other keys (chunk_id, text, original_path, modified_at, citation, ...) pass through
untouched — this module never drops or renames a field it doesn't inspect.
"""
from __future__ import annotations

from typing import Any, Optional


def _has_matching_access(snapshot: Any, principal: str) -> bool:
    """A principal has access iff the snapshot marks the file public, or the principal is
    explicitly listed. Any other shape (missing keys, wrong types) is treated as no access —
    this function never raises on a malformed snapshot, it just returns False."""
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("public") is True:
        return True
    principals = snapshot.get("principals")
    if isinstance(principals, (list, tuple, set)):
        return principal in principals
    return False


def filter_by_permission(rows: list[dict[str, Any]], principal: Optional[str]) -> list[dict[str, Any]]:
    """Query-time permission filter (spec §9's non-negotiable requirement; this workspace's own
    PERMISSION-FILTER-FAILS-CLOSED guardrail).

    Fails closed in every direction:
    - `principal` is None/empty -> every row excluded (no identity to check access against is
      never treated as "unrestricted").
    - a row's `permissions_snapshot` is missing/None/malformed -> that row is excluded,
      regardless of `principal` (spec §6's "unfilterable — must not be indexed" doctrine,
      enforced here at query time as a second layer, not just at ingestion time).
    - a row's snapshot exists but does not list `principal` and is not `public` -> excluded.
    """
    if not principal:
        return []
    return [row for row in rows if _has_matching_access(row.get("permissions_snapshot"), principal)]


def _dedup_by_identity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact (drive_id, file_id) duplicates first — the same row indexed twice under an
    identical identity key. Keeps the highest-scoring copy; ties keep the first-seen copy
    (stable, deterministic — never arbitrary re-ordering)."""
    best_by_identity: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []
    for row in rows:
        key = (row.get("drive_id"), row.get("file_id"))
        existing = best_by_identity.get(key)
        if existing is None:
            best_by_identity[key] = row
            order.append(key)
        elif float(row.get("score", 0.0)) > float(existing.get("score", 0.0)):
            best_by_identity[key] = row
    return [best_by_identity[key] for key in order]


def _dedup_by_content_hash_with_precedence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross-source dedup fallback (spec §6's dedup key: "(drive_id, file_id) primary identity
    match first; content_hash fallback when the same logical file exists at two different
    (drive_id, file_id) pairs"). Precedence rule (spec §6): on a content_hash match across
    source_classification values, the 'canonical' row wins; the 'controller' row is excluded
    from these results (never deleted from any index — this function only filters an in-memory
    row list, it never touches storage)."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for row in rows:
        key = row.get("content_hash")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    result: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            result.append(group[0])
            continue
        canonical_rows = [r for r in group if r.get("source_classification") == "canonical"]
        if canonical_rows:
            # Multiple canonical rows sharing one content_hash is an edge case the spec does not
            # define a tiebreak for beyond "the canonical row wins" — keep the highest-scoring
            # canonical row, first-seen on a tie, same deterministic rule as identity dedup.
            best = max(canonical_rows, key=lambda r: float(r.get("score", 0.0)))
            result.append(best)
        else:
            best = max(group, key=lambda r: float(r.get("score", 0.0)))
            result.append(best)
    return result


def dedup_and_precede(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Full dedup pipeline: (drive_id, file_id) identity dedup, then content_hash fallback dedup
    with canonical-over-controller precedence. Order matters — identity dedup must run first so
    the content_hash pass only ever compares already-identity-deduplicated rows."""
    return _dedup_by_content_hash_with_precedence(_dedup_by_identity(rows))


def resolve(rows: list[dict[str, Any]], principal: Optional[str]) -> list[dict[str, Any]]:
    """The single entry point CHUNK_4's retrieval route calls for every knowledge_backbone query
    — permission filter first, then dedup+precedence on the surviving, principal-accessible set.

    Filtering before deduping (not the reverse) is deliberate: if a principal cannot access the
    canonical copy of a document but *can* access a controller-source duplicate of the same
    content, the permission-filtered set correctly leaves only the controller copy, and the
    dedup pass then has nothing to suppress it against — the principal gets the one copy they
    are actually allowed to see, not zero results because the (inaccessible) canonical copy
    would otherwise have "won" precedence.
    """
    return dedup_and_precede(filter_by_permission(rows, principal))
