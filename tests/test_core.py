"""The pure deal-building pipeline: filtering, the re-alert price-drop gate,
grouping, sorting, the all-time-low signal, and the flush decision."""
from datetime import datetime, timezone

import core
from models import Deal, Listing


def _cfg(**over):
    base = dict(
        asking_data_deal_threshold=0.35,
        sold_deal_percentile=20.0,
        sold_deal_min_discount=0.05, asking_min_points=3,
        my_country="Netherlands", vat_rate=0.21, price_drop_threshold=0.05,
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
        ranked=ranked,
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
        _listing(4, release_id=200, buyer_price=28.0), _listing(5, release_id=200, buyer_price=50.0),
        _listing(6, release_id=200, buyer_price=52.0),
    ]
    res = core.build_deals(listings, {}, {}, _cfg(), NOW)
    discounts = [d.discount_pct for d in res.deals]
    assert discounts == sorted(discounts, reverse=True)


def test_build_deals_sold_median_leads_when_asking_would_not():
    # Asking median of [25, 35] landed = 30; id 1 (25) is only 16% below ⇒ no asking
    # deal at threshold 0.35. But SOLD median 50 (8 sales) ⇒ id 1 is 50% below ⇒ deal.
    listings = [_listing(1, buyer_price=20.0), _listing(2, buyer_price=30.0)]
    sold = {100: {"currency": "EUR", "last_sold": "2026-01-01",
                  "by_condition": {"Very Good Plus (VG+)": {"median": 50.0, "count": 8,
                                                            "low": 20.0, "high": 80.0,
                                                            "prices": [20, 25, 35, 40, 50, 55, 65, 70]}}}}
    res = core.build_deals(listings, {}, {}, _cfg(sold_price_min_points=5), NOW, sold)
    assert [d.id for d in res.deals] == [1]
    assert res.deals[0].deal_source == "below_sold_median"
    # Without the sold map, the asking median finds nothing here.
    res_none = core.build_deals(listings, {}, {}, _cfg(sold_price_min_points=5), NOW, None)
    assert res_none.deals == []


def test_build_digest_runs_build_deals_then_sold_annotation():
    # build_digest == build_deals + annotate_sold_price: the pipeline result is
    # identical, but the SOLD *display* fields (used by the digest) are layered on.
    listings = [_listing(1, buyer_price=20.0), _listing(2, buyer_price=30.0)]
    sold = {100: {"currency": "EUR", "last_sold": "2026-01-01",
                  "by_condition": {"Very Good Plus (VG+)": {"median": 50.0, "count": 8,
                                                            "low": 20.0, "high": 80.0,
                                                            "prices": [20, 25, 35, 40, 50, 55, 65, 70]}}}}
    cfg = _cfg(sold_price_min_points=5)

    bare = core.build_deals(listings, {}, {}, cfg, NOW, sold)
    assert bare.deals[0].sold_median_value is None   # build_deals leaves display fields unset

    res = core.build_digest(listings, {}, {}, cfg, NOW, sold)
    assert [d.id for d in res.deals] == [d.id for d in bare.deals]
    assert res.deals[0].deal_source == "below_sold_median"
    assert res.deals[0].sold_median_value == 50.0
    assert res.deals[0].sold_data_points == 8


def _sold_one(prices, median, cond="Very Good Plus (VG+)"):
    return {"currency": "EUR", "last_sold": "2026-01-01",
            "by_condition": {cond: {"median": median, "count": len(prices),
                                    "low": min(prices), "high": max(prices),
                                    "prices": sorted(prices)}}}


def test_sold_deals_sort_by_effective_not_item_discount():
    # Release 100's only copy: item 11 + €34 ship (eff 45 > M+S=30 → dropped).
    # Release 200's only copy: item 17 + €1 ship (eff 18 → genuine 40% landed deal).
    # The phantom is dropped entirely; only the genuine landed deal appears.
    listings = [
        _listing(1, release_id=100, buyer_price=11.0, shipping_buyer_price=34.0),
        _listing(2, release_id=200, buyer_price=17.0, shipping_buyer_price=1.0),
    ]
    sold = {100: _sold_one([10, 20, 30, 40, 50], 30.0),
            200: _sold_one([10, 20, 30, 40, 50], 30.0)}
    res = core.build_deals(listings, {}, {}, _cfg(sold_price_min_points=5), NOW, sold)
    assert [d.id for d in res.deals] == [2]


def test_group_primary_is_cheapest_landed_not_cheapest_item():
    # Same release: the expensive-ship copy (eff 45 > M+S=30) is dropped entirely;
    # only the genuinely cheap-landed copy (eff 18) survives as the primary, with no
    # phantom sibling cluttering the output.
    listings = [
        _listing(1, release_id=100, buyer_price=11.0, shipping_buyer_price=34.0,  # eff 45 → dropped
                 seller_username="far"),
        _listing(2, release_id=100, buyer_price=17.0, shipping_buyer_price=1.0,   # eff 18
                 seller_username="near"),
    ]
    sold = {100: _sold_one([10, 20, 30, 40, 50], 30.0)}
    res = core.build_deals(listings, {}, {}, _cfg(sold_price_min_points=5), NOW, sold)
    assert len(res.deals) == 1
    primary = res.deals[0]
    assert primary.id == 2
    assert primary.siblings == []


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


def test_sort_puts_sold_validated_above_asking_fallback():
    import core
    from models import Deal
    sold = Deal(id=1, ranked=True, effective_discount=0.20, deal_source="below_sold_median")
    asking = Deal(id=2, ranked=True, effective_discount=0.50, deal_source="below_asking_median",
                  low_confidence=True)
    ordered = sorted([asking, sold], key=core.deal_sort_key)
    # Sold-validated leads even though the asking deal shows a deeper %.
    assert [d.id for d in ordered] == [1, 2]


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


def test_deal_new_flags_round_trip():
    from models import Deal
    d = Deal(id=1, low_confidence=True, detached_low=True)
    again = Deal.from_pending(d.to_pending())
    assert again.low_confidence is True
    assert again.detached_low is True


def test_build_deals_uses_percentile_sold_gate():
    from datetime import datetime, timezone
    from models import Listing
    import core
    listings = [Listing(id=1, release_id=7, media_condition="Near Mint (NM or M-)",
                        sleeve_condition="Near Mint (NM or M-)", buyer_price=17.0,
                        buyer_currency="EUR", price=17.0, currency="EUR",
                        shipping_buyer_price=3.0, ships_from="Belgium",
                        release_title="X", release_artist="Y")]
    sold = {7: {"currency": "EUR", "last_sold": "2026-01-01",
                "by_condition": {"Near Mint (NM or M-)": {
                    "median": 30.0, "count": 5, "low": 10.0, "high": 50.0,
                    "prices": [10, 20, 30, 40, 50]}}}}
    cfg = _cfg(min_media_condition="NM", min_sleeve_condition="NM",
               group_by_release=False, sold_price_min_points=5)
    res = core.build_deals(listings, {}, {}, cfg, datetime.now(timezone.utc), sold)
    assert [d.id for d in res.deals] == [1]
    assert res.deals[0].deal_source == "below_sold_median"


def test_all_time_low_sets_floor_badge():
    import core
    from models import Deal
    # 3 prior observations, all above this deal's landed price → ATL.
    history = {"7:Near Mint (NM or M-)": [
        {"d": "2026-01-01", "p": 40.0, "c": "EUR"},
        {"d": "2026-01-02", "p": 38.0, "c": "EUR"},
        {"d": "2026-01-03", "p": 42.0, "c": "EUR"},
    ]}
    d = Deal(id=1, release_id=7, media_condition="Near Mint (NM or M-)",
             landed_price=30.0, ranked=True)
    core.annotate_historical_floor([d], history, min_points=3)
    assert d.historical_floor_pct is not None     # ATL detected
    assert d.historical_floor_value == 38.0       # cheapest prior observation
    assert d.historical_data_points == 3
