"""End-to-end golden for the deal pipeline (TODO #5).

Runs the curated control dataset through the *real* production seam
(`core.build_digest`) and the *real* email renderers (`notifier._build_html` /
`_build_text`) — the same functions `watcher.main` / `EmailNotifier.send` call —
then pins both the structured verdicts and the rendered email. A cross-cutting
regression (threshold tweak, sort change, renderer edit) shows up here in one
diff.

Expected values were derived by running the fixture through the current code and
reconciling each verdict against its intended path (see the fixture's scenario
map), not transcribed by hand.
"""
from datetime import datetime, timezone

import core
import notifier
from tests.fixtures.control_dataset import (
    control_cfg,
    control_listings,
    control_price_history,
    control_sold_stats,
)

NOW = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def _run():
    """The deterministic seam exactly as main() drives it, then both renders."""
    res = core.build_digest(
        control_listings(), {}, control_price_history(),
        control_cfg(), NOW, control_sold_stats(),
    )
    html = notifier._build_html(res.deals, NOW, 0)
    text = notifier._build_text(res.deals, NOW, 0)
    return res.deals, html, text


# ── Structured verdicts ──────────────────────────────────────────────────────

def test_deal_order_and_sources():
    deals, _, _ = _run()
    # Sold-validated lead (deepest first), then low-confidence (deepest first),
    # then the unranked remote-only listing. Dropped scenarios (106, 107) absent.
    assert [d.id for d in deals] == [1, 10, 20, 30, 40]
    assert [d.deal_source for d in deals] == [
        "below_sold_median",   # 1  — sold-led big deal
        "below_sold_median",   # 10 — sold-led, caveated
        "below_asking_median", # 20 — asking fallback
        "below_sold_low",      # 30 — sparse sold low
        "remote_only",         # 40 — Discogs-flagged
    ]
    assert [d.ranked for d in deals] == [True, True, True, True, False]


def test_deal_discounts_and_flags():
    deals, _, _ = _run()
    by_id = {d.id: d for d in deals}

    # 1 — clean sold-led deal, all-time low, one sibling, no caveat/detached.
    d = by_id[1]
    assert d.discount_pct == 45
    assert d.low_confidence is False
    assert d.sold_tier_caveat is False
    assert d.detached_low is False
    assert d.historical_floor_pct == 8
    assert d.historical_data_points == 3
    assert d.sold_median_value == 115.0
    assert d.sold_data_points == 8
    assert len(d.siblings) == 1 and d.siblings[0]["seller_username"] == "discland"

    # 10 — sold-led but a better grade (M) sold for ~the same → caveat, no red rail.
    d = by_id[10]
    assert d.discount_pct == 41
    assert d.low_confidence is False
    assert d.sold_tier_caveat is True
    assert d.sold_tier_caveat_grade == "M"
    assert d.sold_tier_caveat_value == 70.0
    assert d.sold_median_value == 105.0

    # 20 — asking-only fallback: low-confidence, no sold display, not detached.
    d = by_id[20]
    assert d.discount_pct == 40
    assert d.low_confidence is True
    assert d.sold_tier_caveat is False
    assert d.detached_low is False
    assert d.sold_median_value is None

    # 30 — sparse sold-low, cheap US import: low-confidence, detached, VAT applied.
    d = by_id[30]
    assert d.discount_pct == 15
    assert d.low_confidence is True
    assert d.detached_low is True
    assert d.vat_estimated is True
    assert d.vat_amount > 0
    assert d.sold_median_value == 62.0
    assert d.sold_data_points == 2

    # 40 — remote-only: unranked, no computed discount.
    d = by_id[40]
    assert d.discount_pct is None
    assert d.is_deal_remote is True


def test_dropped_scenarios_absent():
    deals, html, text = _run()
    ids = {d.id for d in deals}
    assert 50 not in ids   # phantom: shipping/VAT pushed it over the benchmark
    assert 60 not in ids   # clustered: nothing 35% below the asking median
    for marker in ("Slint", "Spiderland", "Disintegration"):
        assert marker not in html
        assert marker not in text


# ── Rendered HTML ────────────────────────────────────────────────────────────

def test_html_renders_signals_in_order():
    _, html, _ = _run()

    # Identity + the new edge-state language across the five cards.
    assert "Miles Davis — Kind of Blue" in html
    assert "Strong buy" in html                      # 1 sold-validated big deal
    assert "All-time low" in html                    # 1
    assert "Check the grade" in html                 # 10 better-grade caveat
    assert "sells for about €70.00" in html          # 10
    assert "Worth a look" in html                    # 20 asking-only
    assert "asking prices" in html                   # 20
    assert "Verify before buying" in html            # 30 detached-low
    assert "Only copy" in html                       # 40 remote-only

    # Deepest-first ordering across the cards: sold-validated lead, remote last.
    order = ["Kind of Blue", "Selected Ambient", "Geogaddi", "White Label", "Late-Night"]
    positions = [html.index(s) for s in order]
    assert positions == sorted(positions)

    # The higher-tier ladder is cut from the redesign entirely.
    assert "Also sold:" not in html


def test_text_renders_signals_and_money_path():
    _, _, text = _run()
    assert "SOLD median €115.00" in text                 # proof line (deal 1)
    assert "Also sold:" not in text                      # the tier ladder is cut
    assert "all-time low" in text                        # deal 1, rides its lines
    assert "flagged by Discogs" in text                  # deal 40 remote-only
    assert "verify the pressing/grade" in text           # deal 30 detached-low
    # 30 — the US import's all-in cost line: item + ship + estimated VAT = landed.
    assert "€44.00 + €4.00 ship + ~€10.08 VAT = €58.08 landed" in text
    assert "−15% · €58.08 landed" in text


# ── Live competitive picture end-to-end ──────────────────────────────────────

_NM = "Near Mint (NM or M-)"
_VGP = "Very Good Plus (VG+)"


def _live_copy(lid, media, sleeve, landed, ships="Netherlands"):
    return {"listing_id": lid, "media_condition": media, "sleeve_condition": sleeve,
            "price": landed, "currency": "EUR", "landed": landed, "landed_currency": "EUR",
            "shipping": 0.0, "seller_username": f"s{lid}", "seller_rating": 99.0,
            "ships_from": ships, "listing_url": f"https://x/{lid}"}


def test_live_market_panel_renders_end_to_end():
    """build_digest → annotate_live_market → render, exactly as watcher.main drives
    it: a cheaper live copy of Kind of Blue overrides the fallback best_alt, and the
    field ladder tags a below-floor copy."""
    res = core.build_digest(control_listings(), {}, control_price_history(),
                            control_cfg(), NOW, control_sold_stats())
    live = {101: [
        _live_copy(1, _NM, _VGP, 66.0, ships="Belgium"),   # the primary itself
        _live_copy(7, _NM, _NM, 58.0),                      # cheaper, floor-passing → best other
        _live_copy(8, _VGP, _VGP, 50.0),                    # cheapest but below the NM floor
        _live_copy(9, _NM, _VGP, 72.0),                     # floor-passing, pricier → ladder
    ]}
    core.annotate_live_market(res.deals, live, control_cfg(), control_sold_stats())
    alts = [d.best_alt for d in res.deals if d.best_alt and d.market_authoritative]
    core.annotate_sold_price(alts, control_sold_stats())
    html = notifier._build_html(res.deals, NOW, 0)

    kob = next(d for d in res.deals if d.release_id == 101)
    assert kob.best_alt.id == 7 and kob.is_cheapest is False
    assert kob.market_total == 3                 # floor-passing copies (1, 7, 9)
    assert "Best price for this record" in html
    assert "rest of the field" in html.lower()
    assert "below your floor" in html.lower()
