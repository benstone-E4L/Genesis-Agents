"""Smoke test for capability cards."""
def test_all_cards_load():
    from capability_cards import all_cards
    cards = all_cards()
    assert len(cards) >= 20, f"too few cards: {len(cards)}"
    # Each card has required fields
    for c in cards:
        assert c["slug"]
        assert c["name"]
        assert "capabilities" in c
        assert "pricing" in c
        assert "reputation" in c
        assert c["pricing"]["platform_fee_pct"] == 0.10
        # Check fee math
        total = c["pricing"]["total_cents"]
        agent = c["pricing"]["agent_net_cents"]
        fee = c["pricing"]["platform_fee_cents"]
        assert agent + fee == total

def test_card_for_known_slug():
    from capability_cards import card_for
    c = card_for("genesis-research")
    assert c is not None
    assert c["slug"] == "genesis-research"
    assert c["pricing"]["total_cents"] > 0

def test_card_for_unknown_slug():
    from capability_cards import card_for
    assert card_for("does-not-exist") is None
