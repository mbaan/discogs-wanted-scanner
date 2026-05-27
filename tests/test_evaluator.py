"""Per-release, per-condition deal evaluation."""
import evaluator


def _listing(id_, **kw):
    base = {
        "id": id_, "release_id": 100, "release_title": "Test", "release_artist": "Artist",
        "media_condition": "Very Good Plus (VG+)", "sleeve_condition": "Very Good Plus (VG+)",
        "price": 20.0, "currency": "EUR",
        "buyer_price": 20.0, "buyer_currency": "EUR",
        "shipping_price": 5.0, "shipping_buyer_price": 5.0,
        "is_deal_remote": False, "ships_from": "Belgium",
    }
    base.update(kw)
    return base


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_condition_filter_blocks_g_grade():
    assert not evaluator.passes_condition("Good (G)", "Very Good Plus (VG+)")
    assert not evaluator.passes_condition("Very Good Plus (VG+)", "Good (G)")
    assert evaluator.passes_condition("Mint (M)", "Near Mint (NM or M-)")


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


def test_condition_short():
    assert evaluator.condition_short("Mint (M)") == "M"
    assert evaluator.condition_short("Near Mint (NM or M-)") == "NM"
    assert evaluator.condition_short("Very Good Plus (VG+)") == "VG+"


# ── Per-condition bucketing ─────────────────────────────────────────────────

def test_solo_listing_no_baseline_no_deal():
    deals = evaluator.evaluate_release_group(
        [_listing(1)],
        deal_threshold=0.25, my_country="Netherlands",
    )
    assert deals == []


def test_solo_listing_with_remote_flag_emits():
    deals = evaluator.evaluate_release_group(
        [_listing(1, is_deal_remote=True)],
        deal_threshold=0.25, my_country="Netherlands",
    )
    assert len(deals) == 1
    assert deals[0]["deal_source"] == "remote_only"
    assert deals[0]["certainty_label"] == "LOW"


def test_deal_when_cheapest_is_outlier_within_condition():
    """All listings are VG+; one cheap, two pricey."""
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=5.0),  # 25 landed
        _listing(2, buyer_price=50.0, shipping_buyer_price=8.0),  # 58
        _listing(3, buyer_price=55.0, shipping_buyer_price=8.0),  # 63
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    assert len(deals) == 1 and deals[0]["id"] == 1
    assert deals[0]["deal_source"] == "below_condition_median"
    assert "VG+ median" in deals[0]["deal_reason"]
    assert "of 3" in deals[0]["deal_reason"]


def test_cross_condition_does_not_create_spurious_deals():
    """A VG+ listing at €30 should NOT look like a 70% deal vs Mint copies at €100+.

    Before per-condition bucketing this scenario produced false positives.
    """
    listings = [
        # One cheap VG+ — solo within its condition bucket (no peer baseline)
        _listing(1, buyer_price=30.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)",
                 sleeve_condition="Very Good Plus (VG+)"),
        # A cluster of Mint listings
        _listing(2, buyer_price=100.0, shipping_buyer_price=10.0,
                 media_condition="Mint (M)", sleeve_condition="Mint (M)"),
        _listing(3, buyer_price=110.0, shipping_buyer_price=10.0,
                 media_condition="Mint (M)", sleeve_condition="Mint (M)"),
        _listing(4, buyer_price=115.0, shipping_buyer_price=10.0,
                 media_condition="Mint (M)", sleeve_condition="Mint (M)"),
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    # VG+ listing is now solo within its bucket → no deal (no peer baseline, no remote flag)
    # Mint listings cluster tight, none is 25% below the others' median
    assert deals == []


def test_per_condition_separately_emits_deals():
    """Both conditions present, both have a clear outlier within their bucket."""
    listings = [
        # VG+ bucket: one cheap outlier vs two priced consistently
        _listing(1, buyer_price=20.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)",
                 sleeve_condition="Very Good Plus (VG+)"),
        _listing(2, buyer_price=40.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)",
                 sleeve_condition="Very Good Plus (VG+)"),
        _listing(3, buyer_price=45.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)",
                 sleeve_condition="Very Good Plus (VG+)"),
        # Mint bucket: one cheap outlier vs two
        _listing(4, buyer_price=60.0, shipping_buyer_price=10.0,
                 media_condition="Mint (M)", sleeve_condition="Mint (M)"),
        _listing(5, buyer_price=120.0, shipping_buyer_price=10.0,
                 media_condition="Mint (M)", sleeve_condition="Mint (M)"),
        _listing(6, buyer_price=130.0, shipping_buyer_price=10.0,
                 media_condition="Mint (M)", sleeve_condition="Mint (M)"),
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    by_id = {d["id"]: d for d in deals}
    assert 1 in by_id and "VG+ median" in by_id[1]["deal_reason"]
    assert 4 in by_id and "M median" in by_id[4]["deal_reason"]
    # And these are the ONLY deals — listings 2, 3, 5, 6 are not outliers in their buckets
    assert set(by_id) == {1, 4}


def test_two_listing_bucket_downgrades_certainty_to_low():
    """n=2 in a bucket means the median IS one of the two prices — thin signal."""
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)"),
        _listing(2, buyer_price=50.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)"),
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    # median = (25 + 55) / 2 = 40; landed 25 < 40*0.75 = 30 → deal, but n=2 → LOW
    assert len(deals) == 1 and deals[0]["certainty_label"] == "LOW"


def test_two_listing_bucket_with_remote_flag_keeps_certainty():
    """If Discogs flags the deal AND n=2 fires, don't degrade certainty."""
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)", is_deal_remote=True),
        _listing(2, buyer_price=50.0, shipping_buyer_price=5.0,
                 media_condition="Very Good Plus (VG+)"),
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    # is_deal_remote=True triggers HIGH in _certainty, not floored to LOW
    assert len(deals) == 1 and deals[0]["certainty_label"] == "HIGH"


def test_no_deals_when_prices_clustered_in_bucket():
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=5.0),
        _listing(2, buyer_price=21.0, shipping_buyer_price=5.0),
        _listing(3, buyer_price=19.0, shipping_buyer_price=5.0),
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    assert deals == []


# ── Shipping vs landed price ─────────────────────────────────────────────────

def test_high_shipping_does_not_get_filtered_but_loses_on_landed():
    """A high-shipping listing is INCLUDED in the bucket (no separate filter);
    if its landed price beats the median, it still qualifies as a deal."""
    listings = [
        _listing(1, buyer_price=15.0, shipping_buyer_price=5.0),   # 20 landed (was rejected at 33% before)
        _listing(2, buyer_price=30.0, shipping_buyer_price=8.0),   # 38
        _listing(3, buyer_price=35.0, shipping_buyer_price=8.0),   # 43
        _listing(4, buyer_price=20.0, shipping_buyer_price=5.0),   # 25
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    # Bucket landed: [20, 25, 38, 43]; median = (25+38)/2 = 31.5;
    # threshold ⇒ landed < 23.625. Only id 1 (20) qualifies.
    assert [d["id"] for d in deals] == [1]


# ── Certainty ────────────────────────────────────────────────────────────────

def test_certainty_high_for_deep_discount():
    listings = [
        _listing(1, buyer_price=10.0, shipping_buyer_price=2.0),  # 12 landed
        _listing(2, buyer_price=30.0, shipping_buyer_price=5.0),  # 35
        _listing(3, buyer_price=33.0, shipping_buyer_price=5.0),  # 38
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    # median = 35; landed 12 → 66% discount → HIGH
    assert deals[0]["certainty_label"] == "HIGH"


def test_certainty_medium_with_decent_comps():
    listings = [
        _listing(1, buyer_price=20.0, shipping_buyer_price=5.0),  # 25
        _listing(2, buyer_price=30.0, shipping_buyer_price=5.0),  # 35
        _listing(3, buyer_price=33.0, shipping_buyer_price=5.0),  # 38
        _listing(4, buyer_price=35.0, shipping_buyer_price=5.0),  # 40
        _listing(5, buyer_price=36.0, shipping_buyer_price=5.0),  # 41
    ]
    deals = evaluator.evaluate_release_group(
        listings, deal_threshold=0.25, my_country="Netherlands",
    )
    assert deals[0]["certainty_label"] == "MEDIUM"


# ── Shipping region ─────────────────────────────────────────────────────────

def test_shipping_region_classification():
    assert "Domestic" in evaluator.get_shipping_region("Netherlands", "Netherlands")
    assert "EU" in evaluator.get_shipping_region("Belgium", "Netherlands")
    assert "International" in evaluator.get_shipping_region("United States", "Netherlands")
    assert "Unknown" in evaluator.get_shipping_region(None, "Netherlands")


def test_certainty_min_filter():
    assert evaluator.certainty_passes_min("HIGH", "MEDIUM")
    assert evaluator.certainty_passes_min("MEDIUM", "MEDIUM")
    assert not evaluator.certainty_passes_min("LOW", "MEDIUM")
