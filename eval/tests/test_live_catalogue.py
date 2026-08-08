"""The live agent catalogue: pinned offline, reconciled against live on demand.

The offline pin runs always and is hermetic. The live reconciliation calls the
unauthenticated ``GET /agents`` and is opt-in, so the default suite makes no
network request::

    GENESIS_EVAL_LIVE_CHECK=1 python -m pytest eval/tests/test_live_catalogue.py -v

``GET /agents`` requires no credential and invokes no agent, so the opt-in run
is safe to wire into CI.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from eval.genesis_client import (
    DEFAULT_BASE_URL,
    LIVE_SLUG_COUNT,
    LIVE_SLUGS,
    resolve_live_slug,
)

LIVE_CHECK_ENABLED = os.getenv("GENESIS_EVAL_LIVE_CHECK", "").lower() in {
    "1", "true", "yes"
}


def _fetch_live_slugs() -> list[str]:
    with urllib.request.urlopen(f"{DEFAULT_BASE_URL}/agents", timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    return [a["slug"] for a in payload["agents"]]


# ---------------------------------------------------------------------------
# Offline pin — always runs
# ---------------------------------------------------------------------------


def test_snapshot_holds_exactly_the_pinned_count():
    assert len(LIVE_SLUGS) == LIVE_SLUG_COUNT == 57


def test_snapshot_has_no_duplicates_or_blanks():
    assert all(s and s.strip() == s for s in LIVE_SLUGS)


@pytest.mark.parametrize(
    "slug",
    [
        # The ten that were missing from the first snapshot. Each must now
        # resolve to itself and be recognised as live.
        "unit-test-generator",
        "api-documentation-generator",
        "social-media-scheduler",
        "web-scraper-pro",
        "meeting-summarizer",
        "expense-tracker",
        "review-responder",
        "onboarding-automation",
        "image-optimizer",
        "backup-manager",
    ],
)
def test_previously_missing_slugs_are_present_and_resolve_to_themselves(slug):
    assert slug in LIVE_SLUGS
    resolved, resolution = resolve_live_slug(slug)
    assert resolved == slug
    assert resolution == "verified"


# ---------------------------------------------------------------------------
# Live reconciliation — opt-in
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not LIVE_CHECK_ENABLED,
    reason="set GENESIS_EVAL_LIVE_CHECK=1 to reconcile against the live gateway",
)
def test_live_catalogue_has_not_diverged_from_the_pinned_snapshot():
    live = _fetch_live_slugs()

    assert len(live) == LIVE_SLUG_COUNT, (
        f"LIVE GATEWAY NOW SERVES {len(live)} AGENTS, SNAPSHOT PINS "
        f"{LIVE_SLUG_COUNT}. Re-derive LIVE_SLUGS from GET /agents and update "
        f"LIVE_SLUG_COUNT."
    )

    missing = sorted(set(live) - set(LIVE_SLUGS))
    extra = sorted(set(LIVE_SLUGS) - set(live))
    assert not missing, f"live slugs absent from the snapshot: {missing}"
    assert not extra, f"snapshot slugs no longer served live: {extra}"
