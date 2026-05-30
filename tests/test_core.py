"""The pure deal-building pipeline: filtering, the re-alert price-drop gate,
grouping, sorting, the all-time-low signal, and the flush decision."""
from datetime import datetime, timezone

import core
from models import Deal, Listing


def _cfg(**over):
    base = dict(
        deal_threshold=0.35, my_country="Netherlands", vat_rate=0.21,
        big_deal_threshold=0.50, price_drop_threshold=0.05,
        min_media_condition="VG+", min_sleeve_condition="VG+",
        group_by_release=True, max_siblings_per_release=1,
        price_history_min_points=3,
    )
    base.update(over)
    return base


def _listing(id_, **kw):
    base = dict(
        id=id_, release_id=100, release_title="Test", release_artist="Artist",
        media_condition="Very Good Plus (VG+)", sleeve_condition="Very Good Plus (VG+)",
        price=20.0, currency="EUR", buyer_price=20.0, buyer_currency="EUR",
        shipping_price=5.0, shipping_buyer_price=5.0, ships_from="Belgium",
    )
    base.update(kw)
    return Listing(**base)


def _deal(id_, release_id, discount_pct=40, ranked=True, artist="X"):
    return Deal(
        id=id_, release_id=release_id, release_artist=artist, release_title=f"R{release_id}",
        buyer_price=10.0, buyer_currency="EUR", landed_price=15.0, landed_currency="EUR",
        discount_pct=discount_pct if ranked else None,
        effective_discount=(discount_pct / 100.0) if ranked else None,
        ranked=ranked, big_deal=ranked and discount_pct >= 50,
        seller_username=f"seller{id_}", listing_url=f"https://x/{id_}",
    )


NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


# ── build_deals ───────────────────────────────────────────────────────────────

def test_build_deals_filters_condition_and_flags_outlier():
    listings = [
        _listing(1, buyer_price=20.0),  # 25 landed — the outlier
        _listing(2, buyer_price=50.0),  # 55
        _listing(3, buyer_price=55.0),  # 60
        _listing(4, media_condition="Very Good (VG)"),  # fails condition → dropped
    ]
    res = core.build_deals(listings, {}, {}, _cfg(), NOW)
    assert [d.id for d in res.deals] == [1]
    assert res.scanned_releases == 1
    # Every condition-passing listing's price is recorded for re-alert tracking.
    assert {lid for lid, _ in res.just_alerted} == {1, 2, 3}


def test_build_deals_realert_gate_skips_until_price_drops():
    listings = [_listing(1, buyer_price=20.0), _listing(2, buyer_price=50.0), _listing(3, buyer_price=55.0)]
    # Already alerted at the current buyer price (20) → suppressed.
    res = core.build_deals(listings, {1: 20.0}, {}, _cfg(), NOW)
    assert res.deals == []
    # Previously alerted higher (30); current 20 is >5% lower → re-alerts.
    res2 = core.build_deals(listings, {1: 30.0}, {}, _cfg(), NOW)
    assert [d.id for d in res2.deals] == [1]


def test_build_deals_sorts_deepest_discount_first():
    # Two releases, each a clear outlier; the deeper discount sorts first.
    listings = [
        _listing(1, release_id=100, buyer_price=20.0), _listing(2, release_id=100, buyer_price=50.0),
        _listing(3, release_id=100, buyer_price=55.0),
        _listing(4, release_id=200, buyer_price=40.0), _listing(5, release_id=200, buyer_price=50.0),
        _listing(6, release_id=200, buyer_price=52.0),
    ]
    res = core.build_deals(listings, {}, {}, _cfg(), NOW)
    discounts = [d.discount_pct for d in res.deals]
    assert discounts == sorted(discounts, reverse=True)


# ── grouping + sort ───────────────────────────────────────────────────────────

def test_group_by_release_picks_deepest_with_sibling_cap():
    deals = [
        _deal(1, 100, discount_pct=30),
        _deal(2, 100, discount_pct=40),
        _deal(3, 100, discount_pct=55),   # deepest → primary
        _deal(4, 200, discount_pct=50),   # solo
    ]
    grouped = core.group_by_release(deals, max_siblings=1)
    by_rel = {g.release_id: g for g in grouped}
    assert by_rel[100].id == 3
    assert [s["seller_username"] for s in by_rel[100].siblings] == ["seller2"]
    assert by_rel[200].siblings == []


def test_deal_sort_key_unranked_last_and_alpha_tiebreak():
    a = _deal(1, 1, discount_pct=40, artist="Zoe")
    b = _deal(2, 2, discount_pct=55)            # deepest → first
    c = _deal(3, 3, ranked=False)               # unranked → last
    d = _deal(4, 4, discount_pct=40, artist="Aphex")
    ordered = sorted([a, b, c, d], key=core.deal_sort_key)
    assert [x.id for x in ordered] == [2, 4, 1, 3]


# ── all-time-low price history ─────────────────────────────────────────────────

def test_record_price_history_keeps_daily_low():
    history = {}
    core.record_price_history(history, [_listing(1, buyer_price=20.0), _listing(2, buyer_price=15.0)], NOW)
    # Both are release 100 / VG+: keeps the lower landed (15+5=20).
    assert history["100:Very Good Plus (VG+)"][0]["p"] == 20.0


def test_prune_price_history_drops_old_and_empty():
    history = {"100:VG+": [{"d": "2026-02-25", "p": 15.0, "c": "EUR"},
                           {"d": "2026-05-01", "p": 18.0, "c": "EUR"}]}
    core.prune_price_history(history, datetime(2026, 5, 28, tzinfo=timezone.utc), days=90)
    assert [e["d"] for e in history["100:VG+"]] == ["2026-05-01"]


def test_annotate_historical_floor_badges_and_skips():
    history = {"100:Very Good Plus (VG+)": [
        {"d": "2026-05-01", "p": 22.0, "c": "EUR"},
        {"d": "2026-05-05", "p": 24.0, "c": "EUR"},
        {"d": "2026-05-10", "p": 25.0, "c": "EUR"},
    ]}
    new_low = Deal(release_id=100, media_condition="Very Good Plus (VG+)", landed_price=18.0)
    above = Deal(release_id=100, media_condition="Very Good Plus (VG+)", landed_price=26.0)
    core.annotate_historical_floor([new_low, above], history, min_points=3)
    assert new_low.historical_floor_pct == 18 and new_low.historical_data_points == 3
    assert above.historical_floor_pct is None


def test_annotate_historical_floor_needs_min_points():
    history = {"100:Very Good Plus (VG+)": [{"d": "2026-05-01", "p": 22.0, "c": "EUR"}]}
    deal = Deal(release_id=100, media_condition="Very Good Plus (VG+)", landed_price=10.0)
    core.annotate_historical_floor([deal], history, min_points=3)
    assert deal.historical_floor_pct is None


# ── flush decision ─────────────────────────────────────────────────────────────

def test_should_flush_empty_pending_never_flushes():
    assert core.should_flush(0, "hourly", NOW, 7) is False
    assert core.should_flush(0, "daily", NOW.replace(hour=7), 7) is False


def test_should_flush_hourly_flushes_whenever_pending():
    assert core.should_flush(3, "hourly", NOW, 7) is True


def test_should_flush_daily_waits_for_the_hour():
    assert core.should_flush(3, "daily", NOW.replace(hour=7), 7) is True
    assert core.should_flush(3, "daily", NOW.replace(hour=8), 7) is False
