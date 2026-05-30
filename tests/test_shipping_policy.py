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
    "seller_uid": 908656,
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

def _listing(lid, uid, price, media="Very Good Plus (VG+)", sleeve="Very Good Plus (VG+)"):
    return Listing(id=lid, seller_uid=uid, buyer_price=price, price=price,
                   buyer_currency="EUR", media_condition=media, sleeve_condition=sleeve,
                   release_artist=f"A{lid}", release_title=f"T{lid}",
                   listing_url=f"https://www.discogs.com/sell/item/{lid}")


def test_group_by_seller_filters_condition():
    listings = [
        _listing(1, 100, 10.0),
        _listing(2, 100, 20.0, media="Very Good (VG)"),   # fails condition
        _listing(3, 200, 5.0),
    ]
    groups = evaluator.group_by_seller(listings, passing_only=True)
    assert sorted(groups.keys()) == [100, 200]
    assert [l.id for l in groups[100]] == [1]


def test_seller_picks_excludes_deal_sorts_and_caps():
    listings = [_listing(1, 100, 30.0), _listing(2, 100, 10.0), _listing(3, 100, 20.0)]
    picks, total = evaluator.seller_picks(listings, exclude_id=1, limit=1)
    assert total == 2
    assert len(picks) == 1
    assert picks[0]["buyer_price"] == 10.0   # cheapest first


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
        seller_username="vinyldigital.de", listing_url="u",
        seller_total_others=3,
        seller_picks=[
            {"release_artist": "B", "release_title": "Y", "media_condition": "Mint (M)",
             "buyer_price": 9.0, "buyer_currency": "EUR", "listing_url": "u2"},
        ],
        shipping_hint={"currency": "EUR", "country": "Netherlands", "free_shipping": False,
                       "tiers": [(999, 9.8), (1999, 11.8), (0, 47.6)], "per_item": 250,
                       "basis": "weight-est", "fee_now": 9.8, "room_more": 1, "next_fee": 11.8,
                       "seller": "vinyldigital.de", "n_items": 2, "subtotal": 18.0},
    )
    now = dt.datetime(2026, 5, 30, 12, 0, tzinfo=dt.timezone.utc)
    html = notifier._build_html([deal], now, 0)
    text = notifier._build_text([deal], now, 0)
    assert "vinyldigital.de" in html and "more on your wantlist" in html
    assert "vinyldigital.de" in text
    assert "€9.00" in html  # the pick price
