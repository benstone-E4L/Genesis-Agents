"""Skill bundle loader. Reads JSON files from skill_bundles/ and caches in memory."""
from __future__ import annotations
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BUNDLES_DIR = Path(__file__).parent / "skill_bundles"

# Marketplace / gateway slugs → skill_bundles/*.json stem
BUNDLE_SLUG_ALIASES: dict[str, str] = {
    "genesis_meta_agent": "genesis-meta",
    "genesis_meta_x402": "genesis-meta",
    "legal_agent": "genesis-legal",
    "genesis_legal_x402": "genesis-legal",
    "onboarding_agent": "genesis-hr",
    "genesis_hr_x402": "genesis-hr",
    "genesis-data-pipeline-agent": "genesis-data-pipeline",
    "genesis-data-pipeline": "genesis-data-pipeline",
    "genesis-ai-vision-api": "genesis-ai-vision",
    "genesis-workflow-automator": "genesis-workflow-automator",
}


def resolve_bundle_slug(slug: str) -> str:
    """Map gateway/marketplace slugs to skill bundle file stems."""
    key = (slug or "").strip()
    if not key:
        return key
    if key in BUNDLE_SLUG_ALIASES:
        return BUNDLE_SLUG_ALIASES[key]
    if key.endswith("_x402"):
        return key[:-5].replace("_", "-")
    return key.replace("_", "-")


@lru_cache(maxsize=64)
def load_bundle(slug: str) -> dict[str, Any] | None:
    """Load a skill bundle by slug. Returns None if missing.

    Normalises the slug so that underscore-style URL slugs (e.g.
    ``genesis_meta``) match hyphen-style bundle filenames (e.g.
    ``genesis-meta.json``) and vice-versa.  Both forms are tried in order:
    exact match first, then the hyphen-for-underscore variant.
    """
    resolved = resolve_bundle_slug(slug)
    candidates: list[str] = []
    for c in (resolved, slug, slug.replace("_", "-"), resolved.replace("_", "-")):
        if c and c not in candidates:
            candidates.append(c)
    for candidate in candidates:
        path = BUNDLES_DIR / f"{candidate}.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                log.exception("failed to load bundle %s", candidate)
                return None
    log.warning("bundle not found: %s resolved=%s (tried: %s)", slug, resolved, candidates)
    return None


def list_bundles() -> list[str]:
    """List all available bundle slugs."""
    if not BUNDLES_DIR.exists():
        return []
    return sorted(p.stem for p in BUNDLES_DIR.glob("*.json"))
