"""Renderer for the live competitive picture: the two-skin second card (oxblood
'Best price' when a cheaper copy exists, neutral 'Next best' when you're already
cheapest), the green ✓ chip, and the 'rest of the field' ladder — in both the
HTML and plain-text parts."""
from datetime import datetime, timezone

import notifier
from models import Deal

NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
NM = "Near Mint (NM or M-)"
VGP = "Very Good Plus (VG+)"
VG = "Very Good (VG)"


def _primary(**kw):
    base = dict(
        id=1, release_id=100, release_artist="Miles Davis", release_title="Kind of Blue",
        media_condition=NM, sleeve_condition=VGP,
        buyer_price=45.0, buyer_currency="EUR", landed_price=52.0, landed_currency="EUR",
        effective_cost=52.0, discount_pct=45, effective_discount=0.45, ranked=True,
        deal_source="below_sold_median", sold_median_value=95.0, sold_median_currency="EUR",
        sold_data_points=8, sold_low_value=40.0, sold_high_value=160.0,
        seller_username="vinylvault", listing_url="https://x/1",
        market_authoritative=True,
    )
    base.update(kw)
    return Deal(**base)


def _alt(eff, media=NM, **kw):
    base = dict(
        id=2, release_id=100, release_artist="Miles Davis", release_title="Kind of Blue",
        media_condition=media, sleeve_condition=NM,
        buyer_price=eff, buyer_currency="EUR", landed_price=eff, landed_currency="EUR",
        effective_cost=eff, discount_pct=50, effective_discount=0.50, ranked=True,
        deal_source="below_sold_median", median_value=95.0, median_currency="EUR",
        seller_username="crateking", listing_url="https://x/2",
    )
    base.update(kw)
    return Deal(**base)


def _row(lid, eff, media=NM, sleeve=NM, below=False, seller="s"):
    return {"listing_id": lid, "effective_cost": eff, "currency": "EUR",
            "media_condition": media, "sleeve_condition": sleeve,
            "seller_username": seller, "listing_url": f"https://x/{lid}", "below_floor": below}


def test_beats_renders_best_price_card_and_connector():
    deal = _primary(is_cheapest=False, best_alt=_alt(44.0), market_total=6)
    html = notifier._build_html([deal], NOW, 0)
    assert "Best price for this record" in html
    assert "better copy is on sale" in html
    assert "Next best" not in html


def test_runner_up_renders_next_best_and_cheapest_chip():
    deal = _primary(is_cheapest=True, best_alt=_alt(60.0, discount_pct=30), market_total=4)
    html = notifier._build_html([deal], NOW, 0)
    assert "Next best copy" in html                 # runner-up eyebrow
    assert "Cheapest of 4" in html
    assert "runner-up" in html                      # runner-up connector
    assert "better copy is on sale" not in html     # not the oxblood "beats" skin


def test_ladder_lists_rest_of_field_with_below_floor_tag():
    deal = _primary(
        is_cheapest=False, best_alt=_alt(44.0), market_total=4,
        market_low=44.0, market_high=98.0,
        market_copies=[_row(4, 58.0, media=NM), _row(3, 40.0, media=VGP, below=True)],
    )
    html = notifier._build_html([deal], NOW, 0)
    assert "rest of the field" in html.lower()
    assert "below your floor" in html.lower()


def test_single_copy_shows_only_copy_chip():
    deal = _primary(is_cheapest=True, best_alt=None, market_total=1)
    html = notifier._build_html([deal], NOW, 0)
    assert "Only copy listed" in html
    assert "Next best" not in html


def test_chapter_divider_between_deals():
    d1 = _primary(id=1, is_cheapest=True, market_total=2)
    d2 = _primary(id=2, release_id=200, is_cheapest=True, market_total=2)
    html = notifier._build_html([d1, d2], NOW, 0)
    assert "Find 2 of 2" in html
    assert "Find 1 of 2" not in html          # no divider before the first deal


def test_no_divider_for_single_deal():
    html = notifier._build_html([_primary()], NOW, 0)
    assert " of 1" not in html


def test_text_has_numbered_divider():
    d1 = _primary(id=1)
    d2 = _primary(id=2, release_id=200)
    text = notifier._build_text([d1, d2], NOW, 0)
    assert "Find 2 of 2" in text


def test_text_part_mirrors_cheapest_and_next_best():
    deal = _primary(is_cheapest=True, best_alt=_alt(60.0), market_total=4,
                    market_copies=[_row(4, 58.0)])
    text = notifier._build_text([deal], NOW, 0)
    assert "Cheapest of 4" in text
    assert "Next best" in text
