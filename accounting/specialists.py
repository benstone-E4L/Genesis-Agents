"""Canonical map: Phase 1 contract id -> real guarded Genesis slug.

One agent, one job. Not hats. Not one slug with profiles.
Entity YAML files are context packs, not agents.
"""
from __future__ import annotations

CONTRACT_TO_SLUG: dict[str, str] = {
    "e4l-revenue": "genesis-e4l-revenue",
    "e4l-shopify-ecommerce": "genesis-e4l-shopify",
    "e4l-stripe-merchant": "genesis-e4l-stripe",
    "e4l-cash-bank": "genesis-e4l-cash",
    "e4l-ap": "genesis-e4l-ap",
    "e4l-ar": "genesis-e4l-ar",
    "e4l-cogs-cm": "genesis-e4l-cogs-cm",
    "e4l-affiliate-commission": "genesis-e4l-commissions",
    "e4l-intercompany": "genesis-e4l-intercompany",
    "e4l-month-end-close": "genesis-e4l-close",
    "e4l-journal-propose": "genesis-e4l-journals",
    "e4l-fs-integrity": "genesis-e4l-fs-integrity",
    "e4l-controller-review": "genesis-e4l-controller",
    "e4l-treasury": "genesis-e4l-treasury",
}

SLUG_TO_CONTRACT: dict[str, str] = {v: k for k, v in CONTRACT_TO_SLUG.items()}

SPECIALIST_SLUGS: frozenset[str] = frozenset(CONTRACT_TO_SLUG.values())

DISPLAY_NAMES: dict[str, str] = {
    "genesis-e4l-revenue": "Genesis E4L Revenue",
    "genesis-e4l-shopify": "Genesis E4L Shopify",
    "genesis-e4l-stripe": "Genesis E4L Stripe",
    "genesis-e4l-cash": "Genesis E4L Cash",
    "genesis-e4l-ap": "Genesis E4L AP",
    "genesis-e4l-ar": "Genesis E4L AR",
    "genesis-e4l-cogs-cm": "Genesis E4L COGS & CM",
    "genesis-e4l-commissions": "Genesis E4L Commissions",
    "genesis-e4l-intercompany": "Genesis E4L Intercompany",
    "genesis-e4l-close": "Genesis E4L Close",
    "genesis-e4l-journals": "Genesis E4L Journals",
    "genesis-e4l-fs-integrity": "Genesis E4L FS Integrity",
    "genesis-e4l-controller": "Genesis E4L Controller",
    "genesis-e4l-treasury": "Genesis E4L Treasury",
}

MONEY_STUB_SLUGS = frozenset({
    "genesis-finance",
    "genesis-billing",
    "genesis-commerce",
    "genesis-pricing",
})

ONE_HAT_SLUG = "genesis-e4l-accounting"

ENTITY_KEYS = (
    "energy4life",
    "ibe",
    "xpo",
    "massey",
    "nesllc",
    "nespty",
)


def is_e4l_specialist(slug: str) -> bool:
    return (slug or "").strip() in SPECIALIST_SLUGS
