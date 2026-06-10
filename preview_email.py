"""Render the digest email to a local HTML file so the card layout can be eyeballed
without sending live mail. Run:

    uv run python preview_email.py            # writes email_preview.html
    uv run python preview_email.py out.html   # custom path

Builds a representative spread of deal shapes — sold-led (with all-time-low,
siblings, shipping bundle, comments), better-grade caveat, asking-only
low-confidence, sparse below_sold_low with detached-low, and remote-only — so a
layout regression is visible at a glance. The asserted end-to-end golden lives in
tests/test_control_dataset.py (TODO #5, driven through core.build_digest); this
stays the visual eyeball and additionally exercises the network-annotation fields
(shipping bundle, seller picks) that the golden omits."""
import sys
from datetime import datetime, timezone

import notifier
from models import Deal


def _sample_deals() -> list[Deal]:
    return [
        Deal(
            id=1, release_artist="Miles Davis", release_title="Kind of Blue",
            release_year=1959, release_format="LP", release_country="US",
            media_condition="Near Mint (NM or M-)", sleeve_condition="Very Good Plus (VG+)",
            buyer_price=45.0, buyer_currency="EUR", shipping_buyer_price=7.0,
            landed_price=52.0, landed_currency="EUR", discount_pct=45,
            effective_discount=0.45, ranked=True, deal_source="below_sold_median",
            median_value=95.0, median_currency="EUR",
            sold_median_value=95.0, sold_median_currency="EUR", sold_data_points=8,
            sold_low_value=26.0, sold_high_value=160.0, sold_last_date="2026-01-23",
            historical_floor_pct=18, historical_data_points=24,
            seller_username="vinylvault", seller_rating=99.5, ships_from="Netherlands",
            shipping_region="Netherlands", comments="Plays great, light surface marks.",
            listing_url="https://www.discogs.com/sell/item/1",
            siblings=[{"landed_price": 60.0, "landed_currency": "EUR",
                       "seller_username": "discland",
                       "media_condition": "Very Good Plus (VG+)", "discount_pct": 30,
                       "listing_url": "https://www.discogs.com/sell/item/9"}],
            shipping_hint={"seller": "vinylvault", "currency": "EUR", "country": "NL",
                           "free_shipping": True, "free_min": 80.0, "subtotal": 52.0,
                           "free_gap": 28.0, "tiers": [[2, 4.0], [0, 7.0]], "per_item": 1},
            seller_picks=[{"buyer_price": 18.0, "buyer_currency": "EUR",
                           "media_condition": "Near Mint (NM or M-)",
                           "release_artist": "Bill Evans", "release_title": "Waltz for Debby",
                           "listing_url": "https://www.discogs.com/sell/item/8",
                           "discount_pct": 24},
                          {"buyer_price": 22.0, "buyer_currency": "EUR",
                           "media_condition": "Very Good Plus (VG+)",
                           "release_artist": "Chet Baker", "release_title": "Chet Baker Sings",
                           "listing_url": "https://www.discogs.com/sell/item/11",
                           "discount_pct": -12},
                          {"buyer_price": 9.0, "buyer_currency": "EUR",
                           "media_condition": "Very Good Plus (VG+)",
                           "release_artist": "Art Pepper", "release_title": "Meets the Rhythm Section",
                           "listing_url": "https://www.discogs.com/sell/item/12",
                           "discount_pct": None}],
            seller_total_others=4,
        ),
        Deal(
            id=2, release_artist="Aphex Twin", release_title="Selected Ambient Works 85–92",
            release_year=1994, release_format="2×LP", release_country="UK",
            media_condition="Very Good Plus (VG+)", sleeve_condition="Very Good Plus (VG+)",
            buyer_price=30.0, buyer_currency="EUR", shipping_buyer_price=9.0,
            landed_price=39.0, landed_currency="EUR", discount_pct=22,
            effective_discount=0.22, ranked=True, deal_source="below_sold_median",
            median_value=50.0, median_currency="EUR",
            sold_median_value=50.0, sold_median_currency="EUR", sold_data_points=14,
            sold_low_value=15.0, sold_high_value=120.0, sold_last_date="2026-02-10",
            sold_tier_at_or_above={"short": "VG+↑", "median": 50.0, "count": 14},
            sold_tier_higher=[{"short": "NM", "median": 52.0, "count": 6},
                              {"short": "M", "median": 70.0, "count": 3}],
            sold_tier_caveat=True, sold_tier_caveat_grade="NM", sold_tier_caveat_value=52.0,
            seller_username="bleepstore", seller_rating=98.1, ships_from="United Kingdom",
            shipping_region="United Kingdom",
            listing_url="https://www.discogs.com/sell/item/2",
        ),
        Deal(
            id=3, release_artist="Boards of Canada", release_title="Geogaddi",
            release_year=2002, release_format="3×LP", release_country="UK",
            media_condition="Near Mint (NM or M-)", sleeve_condition="Near Mint (NM or M-)",
            buyer_price=70.0, buyer_currency="EUR", shipping_buyer_price=6.0,
            landed_price=76.0, landed_currency="EUR", discount_pct=33,
            effective_discount=0.33, ranked=True, deal_source="below_asking_median",
            low_confidence=True, median_value=110.0, median_currency="EUR",
            seller_username="warpfan", seller_rating=100.0, ships_from="Germany",
            shipping_region="Germany", comments="Opened but unplayed.",
            listing_url="https://www.discogs.com/sell/item/3",
        ),
        Deal(
            id=4, release_artist=None, release_title="White Label (untitled)",
            release_year=1998, release_format='12"', release_country="US",
            media_condition="Very Good Plus (VG+)", sleeve_condition="Generic",
            buyer_price=8.0, buyer_currency="EUR", shipping_buyer_price=12.0,
            landed_price=24.0, landed_currency="EUR", vat_estimated=True, vat_amount=4.0,
            discount_pct=60, effective_discount=0.60, ranked=True,
            deal_source="below_sold_low", low_confidence=True, detached_low=True,
            median_value=30.0, median_currency="EUR",
            sold_median_value=30.0, sold_median_currency="EUR", sold_data_points=3,
            sold_low_value=25.0, sold_high_value=40.0, sold_last_date="2025-11-02",
            seller_username="cratedigger", seller_rating=95.0, ships_from="United States",
            shipping_region="United States",
            listing_url="https://www.discogs.com/sell/item/4",
        ),
        Deal(
            id=5, release_artist="Various", release_title="Late-Night Compilation",
            release_year=2010, release_format="CD", release_country="Europe",
            media_condition="Near Mint (NM or M-)", sleeve_condition="Near Mint (NM or M-)",
            buyer_price=5.0, buyer_currency="EUR", shipping_buyer_price=3.0,
            landed_price=8.0, landed_currency="EUR", discount_pct=None,
            is_deal_remote=True, ranked=False, deal_source="remote_only",
            seller_username="bargainbin", seller_rating=97.0, ships_from="France",
            shipping_region="France",
            listing_url="https://www.discogs.com/sell/item/5",
        ),
    ]


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "email_preview.html"
    html = notifier._build_html(
        _sample_deals(), datetime.now(timezone.utc), extra_count=2,
        session_days_left=120,
        scan_counts={"scanned_releases": 120, "wantlist_total": 500},
    )
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
