"""CHUNK_3_DEDUP test suite — dedup, permission-filter, and canonical-precedence logic
(knowledge_backbone.py). Pure in-memory tests: no DB, no network, no live `rg-kb-prod`
connection, matching this module's own backend-agnostic design (Open Question 3 undecided).
"""
from __future__ import annotations

import knowledge_backbone as kb


def _row(
    *,
    drive_id="canonical-drive",
    file_id="file-1",
    content_hash="hash-a",
    source_classification="canonical",
    source_account=None,
    permissions_snapshot=None,
    score=0.5,
    **extra,
):
    row = {
        "drive_id": drive_id,
        "file_id": file_id,
        "content_hash": content_hash,
        "source_classification": source_classification,
        "source_account": source_account or source_classification,
        "permissions_snapshot": permissions_snapshot,
        "score": score,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Dedup: (drive_id, file_id) identity match first, content_hash fallback
# ---------------------------------------------------------------------------

def test_identity_dedup_collapses_exact_duplicate():
    a = _row(drive_id="d1", file_id="f1", content_hash="h1", score=0.4)
    b = _row(drive_id="d1", file_id="f1", content_hash="h1", score=0.9)
    result = kb.dedup_and_precede([a, b])
    assert len(result) == 1
    assert result[0]["score"] == 0.9


def test_content_hash_fallback_dedup_across_different_identity_pairs():
    """The exact CHUNK_3 acceptance-criteria scenario: one logical document seeded at two
    different (drive_id, file_id) pairs, same content_hash -> exactly one result."""
    a = _row(drive_id="canonical-drive", file_id="fA", content_hash="same-hash",
              source_classification="canonical")
    b = _row(drive_id="controller-mydrive", file_id="fB", content_hash="same-hash",
              source_classification="controller")
    result = kb.dedup_and_precede([a, b])
    assert len(result) == 1


def test_unique_per_source_documents_both_survive():
    a = _row(drive_id="canonical-drive", file_id="fA", content_hash="hash-a",
              source_classification="canonical")
    b = _row(drive_id="controller-mydrive", file_id="fB", content_hash="hash-b",
              source_classification="controller")
    result = kb.dedup_and_precede([a, b])
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Precedence: canonical wins on a content_hash collision, controller excluded (not deleted)
# ---------------------------------------------------------------------------

def test_canonical_wins_over_controller_on_hash_collision():
    canonical = _row(drive_id="canonical-drive", file_id="fA", content_hash="dup-hash",
                       source_classification="canonical", score=0.1)
    controller = _row(drive_id="controller-mydrive", file_id="fB", content_hash="dup-hash",
                        source_classification="controller", score=0.99)
    # Canonical wins even though it scores lower — precedence, not ranking.
    result = kb.dedup_and_precede([controller, canonical])
    assert len(result) == 1
    assert result[0]["source_classification"] == "canonical"


def test_precedence_does_not_mutate_or_delete_input_rows():
    """'excluded from results but not deleted from the index' — this pure function only ever
    filters a list; the original row objects are untouched."""
    canonical = _row(drive_id="canonical-drive", file_id="fA", content_hash="dup-hash",
                       source_classification="canonical")
    controller = _row(drive_id="controller-mydrive", file_id="fB", content_hash="dup-hash",
                        source_classification="controller")
    original_controller = dict(controller)
    kb.dedup_and_precede([canonical, controller])
    assert controller == original_controller


# ---------------------------------------------------------------------------
# Permission filter: fails closed
# ---------------------------------------------------------------------------

def test_permission_filter_excludes_row_without_matching_principal():
    restricted = _row(permissions_snapshot={"principals": ["user:other@e4l.com"], "public": False})
    result = kb.filter_by_permission([restricted], "user:ben@e4l.com")
    assert result == []


def test_permission_filter_includes_row_with_matching_principal():
    row = _row(permissions_snapshot={"principals": ["user:ben@e4l.com"], "public": False})
    result = kb.filter_by_permission([row], "user:ben@e4l.com")
    assert result == [row]


def test_permission_filter_includes_public_row_for_any_principal():
    row = _row(permissions_snapshot={"principals": [], "public": True})
    result = kb.filter_by_permission([row], "user:anyone@e4l.com")
    assert result == [row]


def test_permission_filter_excludes_row_with_null_snapshot():
    """spec §6: 'unfilterable — must not be indexed'. A null/missing permissions_snapshot must
    never be treated as unrestricted, even for a principal that would otherwise match anything."""
    row = _row(permissions_snapshot=None)
    result = kb.filter_by_permission([row], "user:ben@e4l.com")
    assert result == []


def test_permission_filter_excludes_row_with_malformed_snapshot():
    """A snapshot that exists but isn't the expected shape is still 'unfilterable' -> excluded,
    never treated as unrestricted just because it's technically non-null."""
    row = _row(permissions_snapshot="not-a-dict")
    result = kb.filter_by_permission([row], "user:ben@e4l.com")
    assert result == []


def test_permission_filter_excludes_everything_when_principal_missing():
    row = _row(permissions_snapshot={"principals": ["user:ben@e4l.com"], "public": False})
    assert kb.filter_by_permission([row], None) == []
    assert kb.filter_by_permission([row], "") == []


def test_permission_filter_fails_closed_on_stale_snapshot_scenario():
    """A stale snapshot (spec §7: 'Permission snapshot stale... bounded by the ingestion
    cadence') is still just whatever data the filter has — this test proves the filter never
    special-cases staleness into an open pass; it only ever evaluates the snapshot's actual
    listed principals, exactly as it would for a fresh one."""
    stale_but_present = _row(permissions_snapshot={"principals": ["user:former-employee@e4l.com"],
                                                      "public": False})
    result = kb.filter_by_permission([stale_but_present], "user:ben@e4l.com")
    assert result == []  # ben isn't in the (possibly stale) snapshot -> excluded, same as fresh


# ---------------------------------------------------------------------------
# resolve(): permission filter runs before dedup+precedence
# ---------------------------------------------------------------------------

def test_resolve_zero_results_for_restricted_file_queried_by_non_matching_principal():
    """CHUNK_3's headline permission-filter acceptance test: a query from a principal without
    matching access to a chunk's source file never returns that chunk."""
    restricted = _row(
        drive_id="canonical-drive", file_id="secret", content_hash="h-secret",
        source_classification="canonical",
        permissions_snapshot={"principals": ["user:controller@e4l.com"], "public": False},
    )
    result = kb.resolve([restricted], "user:random-agent@e4l.com")
    assert result == []


def test_resolve_returns_controller_copy_when_only_it_is_accessible():
    """Filtering before deduping: if the principal can't see the canonical copy but can see a
    controller-source duplicate of the same content, they get the controller copy, not zero
    results (precedence only applies to the accessible set, not the raw set)."""
    canonical = _row(
        drive_id="canonical-drive", file_id="fA", content_hash="dup-hash",
        source_classification="canonical",
        permissions_snapshot={"principals": ["user:controller@e4l.com"], "public": False},
    )
    controller = _row(
        drive_id="controller-mydrive", file_id="fB", content_hash="dup-hash",
        source_classification="controller",
        permissions_snapshot={"principals": ["user:ben@e4l.com"], "public": False},
    )
    result = kb.resolve([canonical, controller], "user:ben@e4l.com")
    assert len(result) == 1
    assert result[0]["source_classification"] == "controller"


def test_resolve_happy_path_two_sources_one_duplicate_one_unique():
    """CHUNK_3's Test Scenarios happy path: two sources, one real duplicate, one real
    unique-per-source document -> search returns exactly the deduplicated, precedence-correct
    set, respecting permissions throughout."""
    public_snapshot = {"principals": [], "public": True}
    dup_canonical = _row(drive_id="canonical-drive", file_id="d1", content_hash="dup",
                           source_classification="canonical", permissions_snapshot=public_snapshot)
    dup_controller = _row(drive_id="controller-mydrive", file_id="d2", content_hash="dup",
                            source_classification="controller", permissions_snapshot=public_snapshot)
    unique_canonical = _row(drive_id="canonical-drive", file_id="u1", content_hash="unique-c",
                              source_classification="canonical", permissions_snapshot=public_snapshot)
    unique_controller = _row(drive_id="controller-mydrive", file_id="u2", content_hash="unique-ctrl",
                               source_classification="controller", permissions_snapshot=public_snapshot)

    result = kb.resolve(
        [dup_canonical, dup_controller, unique_canonical, unique_controller],
        "user:anyone@e4l.com",
    )
    hashes = {r["content_hash"] for r in result}
    assert hashes == {"dup", "unique-c", "unique-ctrl"}
    dup_result = [r for r in result if r["content_hash"] == "dup"][0]
    assert dup_result["source_classification"] == "canonical"
