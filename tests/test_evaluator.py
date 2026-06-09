"""Per-release, per-condition deal evaluation."""
import pytest

import evaluator
from models import Listing


def _listing(id_, **kw):
    base = dict(
        id=id_, release_id=100, release_title="Test", release_artist="Artist",
        media_condition="Very Good Plus (VG+)", sleeve_condition="Very Good Plus (VG+)",
        price=20.0, currency="EUR",
        buyer_price=20.0, buyer_currency="EUR",
        shipping_price=5.0, shipping_buyer_price=5.0,
        is_deal_remote=False, ships_from="Belgium",
    )
    base.update(kw)
    return Listing(**base)


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_condition_filter_blocks_g_grade():
    vgplus = evaluator.acceptable_conditions("VG+")
    assert not evaluator.passes_condition("Good (G)", "Very Good Plus (VG+)", vgplus, vgplus)
    assert not evaluator.passes_condition("Very Good Plus (VG+)", "Good (G)", vgplus, vgplus)
    assert evaluator.passes_condition("Mint (M)", "Near Mint (NM or M-)", vgplus, vgplus)


def test_acceptable_conditions_floor():
    nm = evaluator.acceptable_conditions("NM")
    assert nm == {"Mint (M)", "Near Mint (NM or M-)"}  # NM floor excludes VG+
    # Short and full forms agree; matching is case-insensitive.
    assert evaluator.acceptable_conditions("vg+") == evaluator.acceptable_conditions("Very Good Plus (VG+)")


def test_parse_condition_rejects_unknown():
    with pytest.raises(ValueError):
        evaluator.parse_condition("Excellent")


def test_landed_price_includes_shipping():
    amt, ccy = evaluator.landed_price(_listing(1, buyer_price=18.0, shipping_buyer_price=7.50))
    assert amt == 25.50 and ccy == "EUR"


def test_landed_price_handles_missing_shipping():
    l = _listing(1, buyer_price=20.0, shipping_buyer_price=None, shipping_price=None)
    assert evaluator.landed_price(l)[0] == 20.0


def test_median_calculation():
    assert evaluator._median([10, 20, 30]) == 20
    assert evaluator._median([10, 20]) == 15
    assert evaluator._median([5]) == 5


def test_percentile_interpolates():
    # 0th = min, 100th = max, 50th = median; interior interpolates linearly.
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert evaluator._percentile(vals, 0) == 10.0
    assert evaluator._percentile(vals, 100) == 50.0
    assert evaluator._percentile(vals, 50) == 30.0
    # 20th pct of 5 points: index (5-1)*0.2 = 0.8 → 10 + 0.8*(20-10) = 18.0
    assert evaluator._percentile(vals, 20) == 18.0


def test_percentile_single_and_empty():
    assert evaluator._percentile([42.0], 20) == 42.0
    assert evaluator._percentile([], 20) == 0.0


def test_condition_short():
    assert evaluator.condition_short("Mint (M)") == "M"
    assert evaluator.condition_short("Near Mint (NM or M-)") == "NM"
    assert evaluator.condition_short("Very Good Plus (VG+)") == "VG+"


# ── Per-condition bucketing ─────────────────────────────────────────────────

def test_asking_min_pool_size_suppresses_small_pools():
    listings = [
        _listing(1, buyer_price=20.0), _listing(2, buyer_price=90.0), _listing(3, buyer_price=95.0),
    ]
    assert evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5,
    ) == []
    listings += [_listing(4, buyer_price=92.0), _listing(5, buyer_price=98.0)]
    deals = evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5, asking_deal_threshold=0.35,
    )
    assert [d.id for d in deals] == [1]
    assert deals[0].deal_source == "below_asking_median"
    assert deals[0].low_confidence is True


def test_zero_priced_listing_does_not_pad_asking_pool():
    # 4 real listings + 1 zero-priced (missing API field): the zero row must not
    # count toward asking_min_points or deflate the median, so no deal fires.
    listings = [
        _listing(1, buyer_price=20.0), _listing(2, buyer_price=90.0),
        _listing(3, buyer_price=95.0), _listing(4, buyer_price=92.0),
        _listing(5, buyer_price=0.0, price=0.0),
    ]
    assert evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5, asking_deal_threshold=0.35,
    ) == []


def test_asking_fallback_is_effective_basis():
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=40.0),
        _listing(2, buyer_price=90.0), _listing(3, buyer_price=95.0),
        _listing(4, buyer_price=92.0), _listing(5, buyer_price=98.0),
    ]
    deals = evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5,
        asking_deal_threshold=0.35, shipping_allowance=0.0,
    )
    assert [d.id for d in deals] == [1]
    assert deals[0].deal_source == "below_asking_median"
    # asking median of items [20,90,92,95,98] = 92; effective basis: eff 60
    # (item 20 + €40 ship) vs benchmark 92 → 1 - 60/92 = 34%.
    assert deals[0].discount_pct == 34


def test_asking_above_band_is_dropped():
    # item 20 clears the asking item gate, but €80 shipping lands eff 100 — above
    # the asking median + S (92 at S=0) → dropped.
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=80.0),
        _listing(2, buyer_price=90.0), _listing(3, buyer_price=95.0),
        _listing(4, buyer_price=92.0), _listing(5, buyer_price=98.0),
    ]
    deals = evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5,
        asking_deal_threshold=0.35, shipping_allowance=0.0,
    )
    assert deals == []


def test_sold_low_rule_fires_with_sparse_sales():
    sold = _sold(40.0, 3, prices=[30, 40, 50])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=25.0)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5,
    )
    assert [d.id for d in deals] == [1]
    assert deals[0].deal_source == "below_sold_low"
    assert deals[0].low_confidence is True
    assert evaluator.evaluate_release_group(
        [_listing(1, buyer_price=31.0)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5,
    ) == []


def test_sold_low_path_drops_high_landed_import():
    # Sparse sold data (2 NM sales, low 44). item 43 < low → candidate on item basis,
    # but €60 shipping lands eff 103 — above sold_low_median + S → dropped.
    sold = _sold(44.5, 2, cond="Near Mint (NM or M-)", prices=[44, 45])
    deals = evaluator.evaluate_release_group(
        [_listing(1, media_condition="Near Mint (NM or M-)",
                  buyer_price=43.0, shipping_buyer_price=60.0)],
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=7.0,
    )
    assert deals == []


def test_sold_low_path_keeps_low_landed_on_effective_basis():
    # item 43 < low 44, eff 47 (item 43 + €4 ship) ≤ sold_low_median 44.5 + 7 = 51.5
    # → kept, low-confidence, discount on the effective all-in basis.
    sold = _sold(44.5, 2, cond="Near Mint (NM or M-)", prices=[44, 45])
    deals = evaluator.evaluate_release_group(
        [_listing(1, media_condition="Near Mint (NM or M-)",
                  buyer_price=43.0, shipping_buyer_price=4.0)],
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=7.0,
    )
    assert [d.id for d in deals] == [1]
    d = deals[0]
    assert d.deal_source == "below_sold_low"
    assert d.low_confidence is True
    assert d.discount_pct == int((1 - 47 / 51.5) * 100)  # 8
    assert d.effective_discount > 0


def test_detached_cheapest_gets_verify_caveat():
    listings = [
        _listing(1, buyer_price=8.0), _listing(2, buyer_price=80.0), _listing(3, buyer_price=85.0),
        _listing(4, buyer_price=82.0), _listing(5, buyer_price=88.0),
    ]
    deals = evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5, asking_deal_threshold=0.35,
    )
    d = next(x for x in deals if x.id == 1)
    assert d.detached_low is True


def test_solo_listing_with_remote_flag_still_emits():
    deals = evaluator.evaluate_release_group(
        [_listing(1, is_deal_remote=True)], my_country="Netherlands",
    )
    assert len(deals) == 1 and deals[0].deal_source == "remote_only"
    assert deals[0].ranked is False


def test_no_deals_when_prices_clustered():
    listings = [_listing(i, buyer_price=p) for i, p in enumerate([20, 21, 19, 20, 22], 1)]
    assert evaluator.evaluate_release_group(
        listings, my_country="Netherlands", asking_min_points=5, asking_deal_threshold=0.35,
    ) == []


# ── Sold median leads the verdict ────────────────────────────────────────────

def _sold(median, count, cond="Very Good Plus (VG+)", ccy="EUR", prices=None):
    if prices is None:
        # Symmetric-ish synthetic distribution around `median` with `count` points.
        prices = [round(median * (0.6 + 0.8 * i / max(count - 1, 1)), 2) for i in range(count)]
    return {
        "currency": ccy, "last_sold": "2026-01-01",
        "by_condition": {cond: {"median": median, "count": count,
                                "low": min(prices), "high": max(prices),
                                "prices": sorted(prices)}},
    }


def test_sold_percentile_gate_fires_below_p20():
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    not_deal = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=20.0)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5,
    )
    assert not_deal == []
    deal = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=17.0)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5,
    )
    assert [d.id for d in deal] == [1]
    d = deal[0]
    assert d.deal_source == "below_sold_median"
    assert d.median_value == 30.0
    # Effective-cost basis: item 17 + €5 ship = €22 landed vs median €30 → 26%
    # (not the 43% the bare item price would have shown).
    assert d.discount_pct == 26
    assert "sales" in d.deal_reason


def test_sold_materiality_floor_blocks_trivial_in_tight_market():
    sold = _sold(40.0, 5, prices=[38, 39, 40, 41, 42])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=38.5)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5, sold_deal_min_discount=0.05,
    )
    assert deals == []
    # item 37 clears the materiality floor (deal_ceiling=38); shipping 1 → eff 38 ≤ M=40
    deals2 = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=37.0, shipping_buyer_price=1.0)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5, sold_deal_min_discount=0.05,
    )
    assert [d.id for d in deals2] == [1]


def test_sold_deal_within_band_just_over_median():
    # item 11 clears the item gate; +€22 shipping lands eff 33 — above the bare
    # median 30 but within M+S (37). It stays a deal, discount measured on the
    # all-in benchmark (always ≥ 0).
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=11.0, shipping_buyer_price=22.0)],
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=7.0,
    )
    assert len(deals) == 1
    d = deals[0]
    assert d.discount_pct == int((1 - 33 / 37) * 100)  # 10
    assert d.effective_discount > 0


def test_sold_discount_is_effective_cost_basis():
    # item 17 clears the item gate (P20=18), but carries +€8 shipping. The headline
    # discount must reflect the landed cost vs the all-in benchmark (S=0 → B = median
    # = 30): eff 25 vs B 30 → 16%, NOT the item-only 43% that ignored shipping.
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=17.0, shipping_buyer_price=8.0)],  # eff 25, Belgium → no VAT
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
    )
    assert deals[0].deal_source == "below_sold_median"
    assert deals[0].discount_pct == 16
    assert deals[0].effective_discount == pytest.approx(1 - 25 / 30)


def test_sold_phantom_above_band_is_dropped():
    # item 11 clears the item gate, but +€30 shipping lands eff 41 — above M+S
    # (37 at S=7). Shipping/VAT have erased the deal, so it is dropped entirely,
    # not paraded as "+N% above median".
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=11.0, shipping_buyer_price=30.0)],
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=7.0,
    )
    assert deals == []


def test_sold_phantom_dropped_genuine_kept():
    # Copy 1: cheap item 11 but €34 ship → eff 45, above M+S → dropped.
    # Copy 2: item 17 + €1 ship → eff 18, a genuine landed deal → kept.
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=11.0, shipping_buyer_price=34.0),
         _listing(2, buyer_price=17.0, shipping_buyer_price=1.0)],
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=7.0,
    )
    assert [d.id for d in deals] == [2]
    assert deals[0].effective_discount > 0


def test_sold_strict_when_allowance_zero():
    # S=0 → benchmark is the bare median; a copy landing over it is dropped.
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    kept = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=17.0, shipping_buyer_price=5.0)],  # eff 22 ≤ 30
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=0.0,
    )
    assert [d.id for d in kept] == [1]
    dropped = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=17.0, shipping_buyer_price=15.0)],  # eff 32 > 30
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=0.0,
    )
    assert dropped == []


def test_sold_solo_listing_qualifies():
    sold = _sold(50.0, 8, prices=[20, 30, 40, 50, 60, 70, 80, 90])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=20.0)], my_country="Netherlands",
        sold_stats=sold, sold_min_points=5,
    )
    assert len(deals) == 1 and deals[0].deal_source == "below_sold_median"


def test_sold_below_min_points_does_not_lead():
    # 4 sales < min_points=5 → sold-median branch does not lead; price above
    # the sold low (30) so the sparse-sold-low rule also doesn't fire.
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=35.0)], my_country="Netherlands",
        sold_stats=_sold(50.0, 4), sold_min_points=5,
    )
    assert deals == []


def test_sold_currency_mismatch_does_not_lead():
    deals = evaluator.evaluate_release_group(
        [_listing(1)], my_country="Netherlands",
        sold_stats=_sold(50.0, 8, ccy="USD"), sold_min_points=5,
    )
    assert deals == []


def test_sold_legacy_cache_without_prices_does_not_lead():
    stats = _sold(50.0, 8)
    del stats["by_condition"]["Very Good Plus (VG+)"]["prices"]
    deals = evaluator.evaluate_release_group(
        [_listing(1)], my_country="Netherlands", sold_stats=stats, sold_min_points=5,
    )
    assert deals == []


# ── Higher-tier sold breakdown ───────────────────────────────────────────────

def _cond(prices):
    s = sorted(float(p) for p in prices)
    return {"median": evaluator._median(s), "count": len(s),
            "low": min(s), "high": max(s), "prices": s}


# Mirrors tests/fixtures/sold_history.html: VG+ 15/20/25, NM 30/40/50/60/70, M 25/50/60/80/100/120.
_TIER_BY_COND = {
    "Very Good Plus (VG+)": _cond([15, 20, 25]),
    "Near Mint (NM or M-)": _cond([30, 40, 50, 60, 70]),
    "Mint (M)": _cond([25, 50, 60, 80, 100, 120]),
}


def test_sold_tiers_breakdown():
    t = evaluator.sold_tiers(_TIER_BY_COND, "Very Good Plus (VG+)", "EUR", caveat_min_points=3)
    assert t["currency"] == "EUR"
    assert t["exact"]["short"] == "VG+" and t["exact"]["median"] == 20.0 and t["exact"]["count"] == 3
    # Each better grade alone, richest first.
    assert [h["short"] for h in t["higher"]] == ["M", "NM"]
    assert t["higher"][1]["median"] == 50.0 and t["higher"][1]["count"] == 5
    # Pooled "VG+ and up": 14 sales, median of the combined set.
    aoa = t["at_or_above"]
    assert aoa["short"] == "VG+↑" and aoa["count"] == 14 and aoa["median"] == 50.0
    assert aoa["low"] == 15.0 and aoa["high"] == 120.0 and aoa["grades"] == ["M", "NM", "VG+"]
    # Cheapest trusted better grade is NM (lowest rank above VG+ with >= 3 sales).
    assert t["nearest_higher"] == {"short": "NM", "median": 50.0, "count": 5}


def test_sold_tiers_nearest_higher_respects_min_points():
    # Raise the bar above NM's 5 sales ⇒ the comparator climbs to M (6 sales).
    t = evaluator.sold_tiers(_TIER_BY_COND, "Very Good Plus (VG+)", "EUR", caveat_min_points=6)
    assert t["nearest_higher"]["short"] == "M"


def test_sold_tiers_without_raw_prices_skips_pool():
    # Legacy cache (no `prices`): per-grade tiers still work, pooled is unavailable.
    legacy = {c: {k: v for k, v in e.items() if k != "prices"} for c, e in _TIER_BY_COND.items()}
    t = evaluator.sold_tiers(legacy, "Very Good Plus (VG+)", "EUR", caveat_min_points=3)
    assert t["at_or_above"] is None
    assert [h["short"] for h in t["higher"]] == ["M", "NM"]
    assert t["nearest_higher"]["short"] == "NM"


def test_sold_tiers_top_grade_has_no_higher():
    t = evaluator.sold_tiers(_TIER_BY_COND, "Mint (M)", "EUR", caveat_min_points=3)
    assert t["higher"] == [] and t["nearest_higher"] is None


# ── Better-grade caveat (warn + drop big-deal) ───────────────────────────────

def _caveat_listings():
    # VG+ bucket: one cheap copy (item 22) against an aspirational asking pool (~95),
    # padded to >= asking_min_points so the asking fallback fires.
    return [
        _listing(1, buyer_price=22.0, shipping_buyer_price=5.0),
        _listing(2, buyer_price=95.0, shipping_buyer_price=5.0),
        _listing(3, buyer_price=95.0, shipping_buyer_price=5.0),
        _listing(4, buyer_price=96.0, shipping_buyer_price=5.0),
        _listing(5, buyer_price=98.0, shipping_buyer_price=5.0),
    ]


def _nm_sold(median, count):
    # Only NM sold data (no VG+ ⇒ the VG+ bucket stays asking-led; NM is the comparator).
    return {"currency": "EUR", "last_sold": "2026-01-01",
            "by_condition": {"Near Mint (NM or M-)": {"median": median, "count": count,
                                                       "low": median, "high": median}}}


def test_better_grade_caveat_fires():
    # item 22 reads as a steal vs the VG+ asking median 95 — but NM copies sold
    # for ~28, so paying 22 for VG+ is no bargain. Caveat fires (mutes the red rail).
    deals = evaluator.evaluate_release_group(
        _caveat_listings(), my_country="Netherlands",
        asking_min_points=5, asking_deal_threshold=0.35,
        sold_stats=_nm_sold(28.0, 5), tier_caveat_gap=0.10, tier_caveat_min_points=3,
    )
    d = next(x for x in deals if x.id == 1)
    assert d.discount_pct == 71           # effective-basis: int((1 - 27/95)*100) = 71
    assert d.sold_tier_caveat is True
    assert d.sold_tier_caveat_grade == "NM" and d.sold_tier_caveat_value == 28.0


def test_better_grade_caveat_not_triggered_when_gap_healthy():
    # NM sells for 100 — comfortably above item 22 — so the VG+ deal is genuine.
    deals = evaluator.evaluate_release_group(
        _caveat_listings(), my_country="Netherlands",
        asking_min_points=5, asking_deal_threshold=0.35,
        sold_stats=_nm_sold(100.0, 5), tier_caveat_gap=0.10, tier_caveat_min_points=3,
    )
    d = next(x for x in deals if x.id == 1)
    assert d.sold_tier_caveat is False


def test_better_grade_caveat_off_by_default():
    # Default tier_caveat_gap=0.0 ⇒ caveat never fires, even with a cheap better grade.
    deals = evaluator.evaluate_release_group(
        _caveat_listings(), my_country="Netherlands",
        asking_min_points=5, asking_deal_threshold=0.35,
        sold_stats=_nm_sold(28.0, 5),
    )
    d = next(x for x in deals if x.id == 1)
    assert d.sold_tier_caveat is False


def test_better_grade_caveat_ignores_thin_better_grade():
    # NM has only 2 sales (< min_points 3) ⇒ not trusted as a comparator; no caveat.
    deals = evaluator.evaluate_release_group(
        _caveat_listings(), my_country="Netherlands",
        asking_min_points=5, asking_deal_threshold=0.35,
        sold_stats=_nm_sold(28.0, 2), tier_caveat_gap=0.10, tier_caveat_min_points=3,
    )
    d = next(x for x in deals if x.id == 1)
    assert d.sold_tier_caveat is False


# ── VAT estimate ─────────────────────────────────────────────────────────────

def test_vat_applies_truth_table():
    assert evaluator.vat_applies("Netherlands", "Netherlands") is False  # domestic
    assert evaluator.vat_applies("Belgium", "Netherlands") is False      # EU
    assert evaluator.vat_applies("United States", "Netherlands") is True  # non-EU
    assert evaluator.vat_applies("United Kingdom", "Netherlands") is True  # non-EU post-Brexit
    assert evaluator.vat_applies(None, "Netherlands") is False           # unknown → no VAT


def test_effective_cost_uplifts_non_eu():
    assert evaluator.effective_cost(100.0, "United States", "Netherlands", 0.21) == pytest.approx(121.0)
    assert evaluator.effective_cost(100.0, "Belgium", "Netherlands", 0.21) == 100.0
    assert evaluator.effective_cost(100.0, "United States", "Netherlands", 0.0) == 100.0


# ── Shipping region ─────────────────────────────────────────────────────────

def test_shipping_region_classification():
    assert "Domestic" in evaluator.get_shipping_region("Netherlands", "Netherlands")
    assert "EU" in evaluator.get_shipping_region("Belgium", "Netherlands")
    assert "International" in evaluator.get_shipping_region("United States", "Netherlands")
    assert "Unknown" in evaluator.get_shipping_region(None, "Netherlands")


def test_no_emitted_sold_deal_exceeds_benchmark():
    # A mixed bucket: one genuine deal, one phantom (huge shipping). Only the
    # genuine one survives, and every survivor has eff ≤ M+S and discount ≥ 0.
    sold = _sold(30.0, 5, prices=[10, 20, 30, 40, 50])
    deals = evaluator.evaluate_release_group(
        [_listing(1, buyer_price=15.0, shipping_buyer_price=3.0),    # eff 18 → deal
         _listing(2, buyer_price=12.0, shipping_buyer_price=40.0)],  # eff 52 → drop
        my_country="Netherlands", sold_stats=sold, sold_min_points=5,
        shipping_allowance=7.0,
    )
    assert [d.id for d in deals] == [1]
    for d in deals:
        assert d.effective_cost <= 30.0 + 7.0
        assert d.effective_discount >= 0
        assert d.discount_pct >= 0
