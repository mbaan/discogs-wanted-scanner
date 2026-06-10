"""Tests for shipping_policy normalization + room estimation, the seller
grouping helpers, and the notifier shipping block."""
import datetime as dt

import pytest

import evaluator
import notifier
import shipping_policy as sp
from models import Deal, Listing


# Real-shaped v3 payload (mirrors api.discogs.com/v3/marketplace/shipping/policies)
RAW_WEIGHT = {
    "seller_uid": 100,
    "currency": "EUR",
    "policies": [{
        "id": 1,
        "countries": ["Netherlands", "Germany", "France"],
        "methods": [{
            "name": "Standard - DHL",
            "range_type": "weight",
            "ranges": [
                {"price": 9.8, "max": 999},
                {"price": 11.8, "max": 1999},
                {"price": 14.8, "max": 4999},
                {"price": 47.6, "max": 0},   # "and up"
            ],
        }],
        "free_shipping": False,
        "free_shipping_min_order_val": None,
    }],
}


def test_normalize_picks_country_policy_and_sorts_tiers():
    pol = sp.normalize(RAW_WEIGHT, "Netherlands")
    assert pol["currency"] == "EUR"
    assert pol["free_shipping"] is False
    assert pol["range_type"] == "weight"
    # sorted ascending, and-up (max==0) last
    assert pol["tiers"] == [(999, 9.8), (1999, 11.8), (4999, 14.8), (0, 47.6)]


def test_normalize_country_fallback_to_first_policy():
    pol = sp.normalize(RAW_WEIGHT, "Japan")  # not listed -> fall back
    assert pol is not None
    assert pol["tiers"][0] == (999, 9.8)


def test_normalize_cheapest_method_wins():
    raw = {
        "currency": "EUR",
        "policies": [{
            "countries": ["Netherlands"],
            "methods": [
                {"name": "Express", "range_type": "weight", "ranges": [{"price": 20.0, "max": 999}]},
                {"name": "Economy", "range_type": "weight", "ranges": [{"price": 5.0, "max": 999}]},
            ],
            "free_shipping": False, "free_shipping_min_order_val": None,
        }],
    }
    pol = sp.normalize(raw, "Netherlands")
    assert pol["method_name"] == "Economy"
    assert pol["tiers"][0] == (999, 5.0)


def test_estimate_room_weight():
    pol = sp.normalize(RAW_WEIGHT, "Netherlands")
    est = sp.estimate_room(pol, n_items=2, subtotal=40.0, est_grams=250)
    assert est["basis"] == "weight-est"
    assert est["fee_now"] == 9.8          # 2*250=500g <= 999
    assert est["tier_max"] == 999
    assert est["room_more"] == 1          # (999-500)//250
    assert est["next_fee"] == 11.8
    assert est["per_item"] == 250


def test_estimate_room_top_tier_has_no_room_cap():
    pol = sp.normalize(RAW_WEIGHT, "Netherlands")
    est = sp.estimate_room(pol, n_items=100, subtotal=500.0, est_grams=250)
    assert est["fee_now"] == 47.6
    assert est["tier_max"] == 0
    assert est["room_more"] is None
    assert "next_fee" not in est


def test_estimate_room_quantity_is_exact():
    raw = {
        "currency": "EUR",
        "policies": [{
            "countries": ["Netherlands"],
            "methods": [{"name": "Q", "range_type": "quantity",
                         "ranges": [{"price": 4.0, "max": 3}, {"price": 6.0, "max": 0}]}],
            "free_shipping": False, "free_shipping_min_order_val": None,
        }],
    }
    pol = sp.normalize(raw, "Netherlands")
    est = sp.estimate_room(pol, n_items=2, subtotal=30.0, est_grams=250)
    assert est["basis"] == "quantity-exact"
    assert est["fee_now"] == 4.0
    assert est["room_more"] == 1          # 3 - 2


def test_estimate_room_free_shipping_gap():
    raw = {
        "currency": "EUR",
        "policies": [{
            "countries": ["Netherlands"],
            "methods": [{"name": "S", "range_type": "weight", "ranges": [{"price": 5.0, "max": 999}]}],
            "free_shipping": True, "free_shipping_min_order_val": 50.0,
        }],
    }
    pol = sp.normalize(raw, "Netherlands")
    est = sp.estimate_room(pol, n_items=2, subtotal=38.0, est_grams=250)
    assert est["free_shipping"] is True
    assert est["free_gap"] == pytest.approx(12.0)


def test_estimate_room_tolerates_json_roundtrip_lists():
    # Persistent cache stores tiers as JSON -> tuples become lists.
    pol = sp.normalize(RAW_WEIGHT, "Netherlands")
    pol = {**pol, "tiers": [list(t) for t in pol["tiers"]]}
    est = sp.estimate_room(pol, n_items=2, subtotal=40.0, est_grams=250)
    assert est["fee_now"] == 9.8


# ── grouping helpers ─────────────────────────────────────────────────────────

def _listing(lid, uid, price, media="Very Good Plus (VG+)", sleeve="Very Good Plus (VG+)",
             release_id=None, buyer_currency="EUR"):
    return Listing(id=lid, seller_uid=uid, buyer_price=price, price=price,
                   buyer_currency=buyer_currency, media_condition=media, sleeve_condition=sleeve,
                   release_id=release_id,
                   release_artist=f"A{lid}", release_title=f"T{lid}",
                   listing_url=f"https://www.discogs.com/sell/item/{lid}")


def test_group_by_seller_filters_condition():
    listings = [
        _listing(1, 100, 10.0),
        _listing(2, 100, 20.0, media="Very Good (VG)"),   # fails condition
        _listing(3, 200, 5.0),
    ]
    vgplus = evaluator.acceptable_conditions("VG+")
    groups = evaluator.group_by_seller(listings, vgplus, vgplus, passing_only=True)
    assert sorted(groups.keys()) == [100, 200]
    assert [l.id for l in groups[100]] == [1]


def test_seller_picks_excludes_deal_sorts_and_caps():
    listings = [_listing(1, 100, 30.0), _listing(2, 100, 10.0), _listing(3, 100, 20.0)]
    picks, total = evaluator.seller_picks(listings, exclude_id=1, limit=1)
    assert total == 2
    assert len(picks) == 1
    assert picks[0]["buyer_price"] == 10.0   # cheapest first
    assert picks[0]["discount_pct"] is None  # no sold stats passed


# Per-condition SOLD benchmark, shaped like sold_prices.get_sell_history output.
_SOLD_VGP = {
    777: {"currency": "EUR", "by_condition": {
        "Very Good Plus (VG+)": {"median": 20.0, "count": 6},
    }},
}


def test_seller_picks_signed_discount_vs_sold_median():
    listings = [
        _listing(1, 100, 38.0),                      # the deal itself (excluded)
        _listing(2, 100, 16.4, release_id=777),      # 18% below median 20
        _listing(3, 100, 23.0, release_id=777),      # 15% above median 20
    ]
    picks, total = evaluator.seller_picks(listings, exclude_id=1, limit=5,
                                          sold_stats_by_release=_SOLD_VGP)
    assert total == 2
    assert picks[0]["discount_pct"] == 18    # below median (positive = discount)
    assert picks[1]["discount_pct"] == -15   # above median


def test_seller_picks_deepest_discount_first_cap_keeps_best_value():
    sold = {777: _SOLD_VGP[777], 888: {"currency": "EUR", "by_condition": {
        "Very Good Plus (VG+)": {"median": 50.0, "count": 4},
    }}}
    listings = [
        _listing(1, 100, 99.0),                      # the deal itself
        _listing(2, 100, 10.0),                      # cheapest but no sold data
        _listing(3, 100, 30.0, release_id=888),      # 40% below its median 50
        _listing(4, 100, 16.4, release_id=777),      # 18% below its median 20
    ]
    picks, total = evaluator.seller_picks(listings, exclude_id=1, limit=2,
                                          sold_stats_by_release=sold)
    assert total == 3
    # The cap keeps the deepest discounts; the cheap no-data item misses the cut.
    assert [p["discount_pct"] for p in picks] == [40, 18]


def test_seller_picks_no_data_sorted_after_badged_cheapest_first():
    listings = [
        _listing(1, 100, 99.0),
        _listing(2, 100, 25.0),                      # no data
        _listing(3, 100, 8.0),                       # no data, cheaper
        _listing(4, 100, 23.0, release_id=777),      # -15% (overpriced, but badged)
    ]
    picks, _ = evaluator.seller_picks(listings, exclude_id=1, limit=5,
                                      sold_stats_by_release=_SOLD_VGP)
    assert [p["discount_pct"] for p in picks] == [-15, None, None]
    assert [p["buyer_price"] for p in picks] == [23.0, 8.0, 25.0]


def test_pick_discount_guards():
    stats = _SOLD_VGP[777]
    # exactly at median -> 0, not None
    assert evaluator._pick_discount(_listing(1, 100, 20.0, release_id=777), stats) == 0
    # currency mismatch -> None
    assert evaluator._pick_discount(
        _listing(2, 100, 16.4, release_id=777, buyer_currency="USD"), stats) is None
    # condition without sold data -> None
    assert evaluator._pick_discount(
        _listing(3, 100, 16.4, media="Good (G)", release_id=777), stats) is None
    # median <= 0 -> None
    bad = {"currency": "EUR", "by_condition": {"Very Good Plus (VG+)": {"median": 0.0}}}
    assert evaluator._pick_discount(_listing(4, 100, 16.4, release_id=777), bad) is None
    # price <= 0 -> None
    assert evaluator._pick_discount(_listing(5, 100, 0.0, release_id=777), stats) is None
    # no stats at all -> None
    assert evaluator._pick_discount(_listing(6, 100, 16.4, release_id=777), None) is None


# ── notifier rendering ───────────────────────────────────────────────────────

def test_shipping_summary_weight():
    pol = sp.normalize(RAW_WEIGHT, "Netherlands")
    hint = sp.estimate_room(pol, n_items=2, subtotal=40.0, est_grams=250)
    hint["country"] = "Netherlands"
    s = notifier._shipping_summary(hint)
    assert "Ship Netherlands" in s
    assert "(est.)" in s
    assert "€9.80 (≤3)" in s  # 999//250 = 3 records at the base fee


def test_shipping_block_renders_in_digest_without_crashing():
    deal = Deal(
        id=1, release_title="X", media_condition="Mint (M)",
        seller_username="test-seller", listing_url="u",
        seller_total_others=3,
        seller_picks=[
            {"release_artist": "B", "release_title": "Y", "media_condition": "Mint (M)",
             "buyer_price": 9.0, "buyer_currency": "EUR", "listing_url": "u2"},
        ],
        shipping_hint={"currency": "EUR", "country": "Netherlands", "free_shipping": False,
                       "tiers": [(999, 9.8), (1999, 11.8), (0, 47.6)], "per_item": 250,
                       "basis": "weight-est", "fee_now": 9.8, "room_more": 1, "next_fee": 11.8,
                       "seller": "test-seller", "n_items": 2, "subtotal": 18.0},
    )
    now = dt.datetime(2026, 5, 30, 12, 0, tzinfo=dt.timezone.utc)
    html = notifier._build_html([deal], now, 0)
    text = notifier._build_text([deal], now, 0)
    assert "test-seller" in html and "more on your wantlist" in html
    assert "test-seller" in text
    assert "€9.00" in html  # the pick price


def test_pick_rows_render_discount_badges():
    deal = Deal(
        id=1, release_title="X", media_condition="Mint (M)",
        seller_username="test-seller", listing_url="u",
        seller_total_others=4,
        seller_picks=[
            {"release_artist": "B", "release_title": "Below", "media_condition": "Mint (M)",
             "buyer_price": 9.0, "buyer_currency": "EUR", "listing_url": "u2",
             "discount_pct": 18},
            {"release_artist": "C", "release_title": "Above", "media_condition": "Mint (M)",
             "buyer_price": 14.0, "buyer_currency": "EUR", "listing_url": "u3",
             "discount_pct": -15},
            {"release_artist": "D", "release_title": "NoData", "media_condition": "Mint (M)",
             "buyer_price": 7.5, "buyer_currency": "EUR", "listing_url": "u4",
             "discount_pct": None},
            # Legacy pick persisted before discount_pct existed: no key at all.
            {"release_artist": "E", "release_title": "Legacy", "media_condition": "Mint (M)",
             "buyer_price": 5.0, "buyer_currency": "EUR", "listing_url": "u5"},
        ],
    )
    html = notifier._shipping_html(deal)
    text = "\n".join(notifier._shipping_text(deal))
    assert "−18%" in html and "−18%" in text            # below median: shown as a discount
    assert "+15%" in html and "+15%" in text            # above median: shown as a markup
    assert html.count("%") == 2 and text.count("%") == 2  # no badge on no-data/legacy rows
    assert "#1b5e20" in html                            # discount badge is green


# ── optimize_basket ────────────────────────────────────────────────────────


def _free_policy(free_min=50.0, fee=7.0):
    """A weight policy with a free-shipping threshold and a single fee tier."""
    return {
        "currency": "EUR", "free_shipping": True, "free_min": free_min,
        "range_type": "weight", "method_name": "Standard",
        "tiers": [(999, fee), (0, fee + 5.0)],
    }


def test_optimize_basket_free_crossing_picks_cheapest():
    # deal €38 solo (fee €7); candidates €9, €15, €4 -> cheapest combo crossing
    # €50 is €4 + €9 = €51 (greedy adds €4 then €9).
    deal = _listing(1, 100, 38.0)
    others = [_listing(2, 100, 9.0), _listing(3, 100, 15.0), _listing(4, 100, 4.0)]
    b = sp.optimize_basket(_free_policy(), [deal, *others], deal,
                           est_grams=250, max_add=3)
    assert b["kind"] == "free_crossing"
    assert b["currency"] == "EUR"
    assert [a["price"] for a in b["add"]] == [4.0, 9.0]
    assert b["new_subtotal"] == pytest.approx(51.0)
    assert b["fee_before"] == 7.0
    assert b["fee_after"] == 0.0
    assert b["saving"] == 7.0


def test_optimize_basket_free_already_met():
    # solo subtotal (deal €55) already >= free_min -> None.
    deal = _listing(1, 100, 55.0)
    others = [_listing(2, 100, 9.0)]
    assert sp.optimize_basket(_free_policy(), [deal, *others], deal,
                              est_grams=250, max_add=3) is None


def test_optimize_basket_free_unreachable_returns_none():
    # deal €38 + cheap candidates can't reach €50 even all-in -> None (no false saving).
    deal = _listing(1, 100, 38.0)
    others = [_listing(2, 100, 2.0), _listing(3, 100, 3.0)]
    assert sp.optimize_basket(_free_policy(), [deal, *others], deal,
                              est_grams=250, max_add=3) is None


def test_optimize_basket_respects_max_add():
    # deal €38; one €9 item can't cross €50 alone, and max_add=1 forbids a second
    # -> None. A single €13 item DOES cross (€51) -> add length 1.
    deal = _listing(1, 100, 38.0)
    cheap = [_listing(2, 100, 9.0), _listing(3, 100, 4.0)]
    assert sp.optimize_basket(_free_policy(), [deal, *cheap], deal,
                              est_grams=250, max_add=1) is None
    one_crosser = [_listing(4, 100, 13.0), _listing(5, 100, 4.0)]
    b = sp.optimize_basket(_free_policy(), [deal, *one_crosser], deal,
                           est_grams=250, max_add=1)
    assert b["kind"] == "free_crossing"
    assert len(b["add"]) == 1
    assert b["add"][0]["price"] == 13.0


def test_optimize_basket_single_item_seller_returns_none():
    # only the deal listing in the group -> no candidates -> None.
    deal = _listing(1, 100, 38.0)
    assert sp.optimize_basket(_free_policy(), [deal], deal,
                              est_grams=250, max_add=3) is None


def test_optimize_basket_no_policy_fields_returns_none():
    # no free_min and empty tiers -> nothing to reframe -> None.
    pol = {"currency": "EUR", "free_shipping": False, "free_min": None,
           "range_type": None, "method_name": None, "tiers": []}
    deal = _listing(1, 100, 38.0)
    others = [_listing(2, 100, 9.0)]
    assert sp.optimize_basket(pol, [deal, *others], deal,
                              est_grams=250, max_add=3) is None


def _quantity_policy():
    return {
        "currency": "EUR", "free_shipping": False, "free_min": None,
        "range_type": "quantity", "method_name": "Q",
        "tiers": [(3, 4.0), (0, 6.0)],
    }


def _weight_policy():
    return {
        "currency": "EUR", "free_shipping": False, "free_min": None,
        "range_type": "weight", "method_name": "W",
        "tiers": [(999, 9.8), (1999, 11.8), (0, 47.6)],
    }


def test_optimize_basket_tier_room_quantity():
    # Quantity policy: solo (1 item) sits in the <=3 tier with room for 2 more,
    # next fee €6. Expect tier_room with cheapest-first picks.
    deal = _listing(1, 100, 20.0)
    others = [_listing(2, 100, 15.0), _listing(3, 100, 5.0)]
    b = sp.optimize_basket(_quantity_policy(), [deal, *others], deal,
                           est_grams=250, max_add=3)
    assert b["kind"] == "tier_room"
    solo = sp.estimate_room(_quantity_policy(), 1, 20.0, 250)
    assert b["room_more"] == solo["room_more"]      # 2
    assert b["fee_now"] == solo["fee_now"]          # 4.0
    assert b["next_fee"] == solo["next_fee"]        # 6.0
    assert [a["price"] for a in b["add"]] == [5.0, 15.0]  # cheapest-first, capped at room_more


def test_optimize_basket_tier_room_weight_flags_est():
    # Weight policy at 250g solo -> weight-est basis carried through.
    deal = _listing(1, 100, 20.0)
    others = [_listing(2, 100, 9.0)]
    b = sp.optimize_basket(_weight_policy(), [deal, *others], deal,
                           est_grams=250, max_add=3)
    assert b["kind"] == "tier_room"
    assert b["basis"] == "weight-est"


def test_optimize_basket_tier_room_zero_returns_none():
    # Quantity policy where the solo basket already sits AT the top "and up" tier
    # (no next step): room_more is None -> None.
    pol = {"currency": "EUR", "free_shipping": False, "free_min": None,
           "range_type": "quantity", "method_name": "Q",
           "tiers": [(0, 6.0)]}
    deal = _listing(1, 100, 20.0)
    others = [_listing(2, 100, 9.0)]
    assert sp.optimize_basket(pol, [deal, *others], deal,
                              est_grams=250, max_add=3) is None


def test_optimize_basket_uses_native_price_for_threshold():
    # Native price (EUR, policy currency) differs from buyer_price (a converted
    # figure). The crossing math MUST use native price; reported currency MUST be
    # the policy currency. deal native €38; candidate native €13 (buyer 999.0).
    # 38 + 13 = 51 >= 50 -> crosses on native; using buyer_price (999) would also
    # "cross" but at the wrong scale and wrong reported price.
    deal = Listing(id=1, seller_uid=100, price=38.0, currency="EUR",
                   buyer_price=38.0, buyer_currency="EUR",
                   release_artist="A1", release_title="T1",
                   listing_url="https://www.discogs.com/sell/item/1")
    cand = Listing(id=2, seller_uid=100, price=13.0, currency="EUR",
                   buyer_price=999.0, buyer_currency="USD",
                   release_artist="A2", release_title="T2",
                   listing_url="https://www.discogs.com/sell/item/2")
    b = sp.optimize_basket(_free_policy(), [deal, cand], deal,
                           est_grams=250, max_add=3)
    assert b["kind"] == "free_crossing"
    assert b["currency"] == "EUR"            # policy currency, not buyer currency
    assert b["add"][0]["price"] == 13.0      # native, not buyer_price 999.0
    assert b["add"][0]["currency"] == "EUR"
    assert b["new_subtotal"] == pytest.approx(51.0)


# ── basket rendering ─────────────────────────────────────────────────────────


def _basket_deal(basket):
    return Deal(
        id=1, release_title="X", release_artist="Hero", media_condition="Mint (M)",
        seller_username="test-seller", listing_url="u", seller_total_others=1,
        seller_picks=[
            {"release_artist": "B", "release_title": "Y", "media_condition": "Mint (M)",
             "buyer_price": 9.0, "buyer_currency": "EUR", "listing_url": "u2"},
        ],
        shipping_hint={"currency": "EUR", "country": "Netherlands", "free_shipping": True,
                       "free_min": 50.0, "tiers": [(999, 7.0), (0, 12.0)], "per_item": 250,
                       "basis": "weight-est", "fee_now": 7.0,
                       "seller": "test-seller", "n_items": 1, "subtotal": 38.0},
        basket=basket,
    )


def test_basket_renders_in_shipping_block():
    now = dt.datetime(2026, 5, 30, 12, 0, tzinfo=dt.timezone.utc)
    basket = {
        "kind": "free_crossing", "currency": "EUR",
        "add": [{"release_artist": "Bill Evans", "release_title": "Waltz for Debby",
                 "media_condition": "Very Good Plus (VG+)", "price": 9.0,
                 "currency": "EUR", "listing_url": "u3"}],
        "new_subtotal": 51.0, "free_min": 50.0,
        "fee_before": 7.0, "fee_after": 0.0, "saving": 7.0,
        "reachable": True, "basis": None,
    }
    with_basket = _basket_deal(basket)
    html = notifier._build_html([with_basket], now, 0)
    text = notifier._build_text([with_basket], now, 0)
    assert "€7.00" in html and "€7.00" in text          # the saving figure
    assert "Waltz for Debby" in html and "Waltz for Debby" in text  # the added item
    assert "free shipping" in html and "free" in text   # the crossing wording

    # No-regression: a deal WITHOUT a basket renders byte-identically to today.
    without = _basket_deal(None)
    assert notifier._build_html([without], now, 0) == notifier._build_html([without], now, 0)
    assert "Waltz for Debby" not in notifier._build_html([without], now, 0)
    assert "🛒" not in notifier._build_text([without], now, 0)


def test_basket_weight_est_shows_qualifier():
    now = dt.datetime(2026, 5, 30, 12, 0, tzinfo=dt.timezone.utc)
    basket = {
        "kind": "tier_room", "currency": "EUR",
        "add": [{"release_artist": "Bill Evans", "release_title": "Waltz for Debby",
                 "media_condition": "Very Good Plus (VG+)", "price": 9.0,
                 "currency": "EUR", "listing_url": "u3"}],
        "room_more": 1, "fee_now": 9.8, "next_fee": 11.8, "basis": "weight-est",
    }
    deal = _basket_deal(basket)
    html = notifier._build_html([deal], now, 0)
    text = notifier._build_text([deal], now, 0)
    assert "(est.)" in html
    assert "(est.)" in text
    assert "Room for 1 more" in html and "Room for 1 more" in text
