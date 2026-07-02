"""core.annotate_live_market — turn a live /sell/release scrape into the
competitive annotations on each deal: the best *other* offer (win or lose),
the is-cheapest verdict, the field ladder, and the authoritative flag. Pure /
network-free (copies are passed in as already-parsed dicts)."""
import core
from models import Deal

NM = "Near Mint (NM or M-)"
M = "Mint (M)"
VGP = "Very Good Plus (VG+)"

CFG = {
    "my_country": "Netherlands",
    "vat_rate": 0.21,
    "min_media_condition": "NM",
    "min_sleeve_condition": "VG+",
    "shipping_allowance": 7.0,
    "live_market_ladder_rows": 4,
}

SOLD = {500: {"currency": "EUR", "by_condition": {NM: {"median": 95.0, "count": 8}}}}


def _copy(lid, media, sleeve, landed, ships="Netherlands", seller=None):
    return {
        "listing_id": lid, "media_condition": media, "sleeve_condition": sleeve,
        "price": landed if landed is not None else 0.0, "currency": "EUR",
        "landed": landed, "landed_currency": "EUR" if landed is not None else None,
        "shipping": 0.0, "seller_username": seller or f"seller{lid}",
        "seller_rating": 99.0, "ships_from": ships,
        "listing_url": f"https://www.discogs.com/sell/item/{lid}",
    }


def _primary(lid, release_id, media, sleeve, eff):
    return Deal(id=lid, release_id=release_id, media_condition=media, sleeve_condition=sleeve,
                release_title="Kind of Blue", release_artist="Miles Davis",
                buyer_currency="EUR", landed_currency="EUR",
                landed_price=eff, effective_cost=eff)


def test_cheaper_copy_becomes_best_alt_and_not_cheapest():
    deal = _primary(1, 500, NM, VGP, 52.0)
    live = {500: [
        _copy(1, NM, VGP, 52.0),                 # the primary itself
        _copy(2, NM, NM, 44.0, seller="crateking"),   # cheaper, floor-passing → best other, beats
        _copy(3, VGP, VGP, 40.0),                # cheapest but BELOW the NM floor → ladder only
        _copy(4, NM, VGP, 58.0, ships="Germany"),     # floor-passing, pricier → ladder
        _copy(5, NM, NM, None),                  # shipping unknown → ladder, not ranked
    ]}
    core.annotate_live_market([deal], live, CFG, SOLD)

    assert deal.market_authoritative is True
    assert deal.best_alt is not None and deal.best_alt.id == 2
    assert deal.best_alt.effective_cost == 44.0
    assert deal.is_cheapest is False              # 44 < 52
    # market_total counts floor-passing copies (ids 1,2,4,5) — not the VG+ one.
    assert deal.market_total == 4
    assert deal.market_low == 44.0
    # Ladder = everything except the primary and the best_alt: ids 3, 4, 5.
    ids = {r["listing_id"] for r in deal.market_copies}
    assert ids == {3, 4, 5}
    below = {r["listing_id"]: r["below_floor"] for r in deal.market_copies}
    assert below[3] is True and below[4] is False and below[5] is False


def test_primary_cheapest_runner_up_is_best_alt():
    deal = _primary(10, 600, NM, NM, 38.0)
    live = {600: [
        _copy(10, NM, NM, 38.0),
        _copy(11, NM, NM, 49.0),                 # runner-up (does not beat)
        _copy(12, NM, NM, 55.0),
    ]}
    core.annotate_live_market([deal], live, CFG, None)

    assert deal.is_cheapest is True
    assert deal.best_alt is not None and deal.best_alt.id == 11   # still shown, as runner-up
    assert deal.best_alt.effective_cost == 49.0
    assert deal.market_total == 3


def test_best_alt_carries_discount_vs_sold_median():
    deal = _primary(1, 500, NM, VGP, 52.0)
    live = {500: [_copy(1, NM, VGP, 52.0), _copy(2, NM, NM, 44.0)]}
    core.annotate_live_market([deal], live, CFG, SOLD)
    # benchmark = 95 + 7 allowance = 102; 1 - 44/102 ≈ 0.569 → 56%
    assert deal.best_alt.discount_pct == 56
    assert deal.best_alt.deal_source == "below_sold_median"


def test_single_copy_no_best_alt():
    deal = _primary(1, 500, NM, VGP, 52.0)
    core.annotate_live_market([deal], {500: [_copy(1, NM, VGP, 52.0)]}, CFG, SOLD)
    assert deal.best_alt is None
    assert deal.is_cheapest is True
    assert deal.market_total == 1
    assert deal.market_copies == []


def test_missing_live_data_leaves_deal_untouched():
    deal = _primary(1, 800, NM, VGP, 52.0)
    core.annotate_live_market([deal], {}, CFG, SOLD)      # no entry for 800
    assert deal.market_authoritative is False
    assert deal.best_alt is None
    assert deal.market_total is None
