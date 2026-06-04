"""The control dataset: one curated, realistic wantlist snapshot engineered to
exercise every deal-verdict path at once, so `core.build_digest` + the email
renderers can be validated end-to-end in a single diff (TODO #5).

Each release below is a self-contained scenario. The builders return real model
objects / config so the dataset runs through the *actual* production pipeline
(`tests/test_control_dataset.py`), under config that mirrors `_load_config`'s
shipped defaults (NM media floor, shipping_allowance 7, P20 sold gate, 0.35
asking threshold, 0.21 VAT, caveat 0.10/3). Everything is EUR so the sold gate
(which needs a matching currency) engages.

Scenario map (release_id → intended verdict):
  101  sold-led big deal, all-time low, one sibling      below_sold_median, ATL
  102  sold-led but a better grade sold for ~the same     below_sold_median + caveat
  103  deep-below-asking, no sold data                    below_asking_median (low-conf)
  104  sparse sales, cheap US import, detached-low         below_sold_low (low-conf, VAT)
  105  lone Discogs-flagged listing                        remote_only (unranked)
  106  cheap item, huge shipping → over the benchmark      dropped (phantom)
  107  prices clustered around the median                  dropped (no deal)
"""

from models import Listing

NM = "Near Mint (NM or M-)"
M = "Mint (M)"
VGP = "Very Good Plus (VG+)"


def _l(id_, release_id, **kw) -> Listing:
    """A Listing with the common defaults filled in (EUR, NM/VG+, item==seller
    price, shipping mirrored across native/buyer currency)."""
    base = dict(
        id=id_, release_id=release_id,
        media_condition=NM, sleeve_condition=VGP,
        currency="EUR", buyer_currency="EUR",
        ships_from="Belgium",  # EU → no import VAT unless overridden
        seller_uid=id_, seller_username=f"seller{id_}", seller_rating=99.0,
        listing_url=f"https://www.discogs.com/sell/item/{id_}",
    )
    base.update(kw)
    # Mirror item price into the native-currency field and shipping across both.
    base.setdefault("price", base.get("buyer_price", 0.0))
    if "shipping_buyer_price" in base:
        base.setdefault("shipping_price", base["shipping_buyer_price"])
    return Listing(**base)


def control_listings() -> list[Listing]:
    return [
        # 101 — sold-led big deal: cheapest copy well under the NM sold lower tail,
        # ships EU (no VAT). A second qualifying copy becomes the sibling. The
        # primary's landed price undercuts the recorded history → all-time low.
        _l(1, 101, release_artist="Miles Davis", release_title="Kind of Blue",
           release_year=1959, release_format="LP", release_country="US",
           buyer_price=60.0, shipping_buyer_price=6.0,
           comments="Plays great, light surface marks."),
        _l(2, 101, release_artist="Miles Davis", release_title="Kind of Blue",
           release_year=1959, release_format="LP", release_country="US",
           buyer_price=88.0, shipping_buyer_price=6.0, seller_username="discland"),

        # 102 — sold-led, but a *better* grade (M) sold for about the same money,
        # so the apparent NM discount is suspect: caveat fires, red rail muted.
        # (M median deliberately below the NM median — the very inversion the
        # better-grade caveat exists to flag.)
        _l(10, 102, release_artist="Aphex Twin",
           release_title="Selected Ambient Works 85–92",
           release_year=1994, release_format="2×LP", release_country="UK",
           buyer_price=60.0, shipping_buyer_price=5.0),

        # 103 — no sold data: falls back to the asking median. Five-copy pool, one
        # clearly cheap copy (not detached — second-cheapest is close enough).
        _l(20, 103, release_artist="Boards of Canada", release_title="Geogaddi",
           release_year=2002, release_format="3×LP", release_country="UK",
           buyer_price=53.0, shipping_buyer_price=6.0,
           comments="Opened but unplayed."),
        _l(21, 103, release_artist="Boards of Canada", release_title="Geogaddi",
           buyer_price=75.0, shipping_buyer_price=5.0, seller_username="warpfan"),
        _l(22, 103, release_artist="Boards of Canada", release_title="Geogaddi",
           buyer_price=92.0, shipping_buyer_price=5.0, seller_username="idmhead"),
        _l(23, 103, release_artist="Boards of Canada", release_title="Geogaddi",
           buyer_price=95.0, shipping_buyer_price=5.0, seller_username="hexagon"),
        _l(24, 103, release_artist="Boards of Canada", release_title="Geogaddi",
           buyer_price=98.0, shipping_buyer_price=5.0, seller_username="musicbox"),

        # 104 — sparse sales (2 < min 5): below_sold_low path. Cheap US import, so
        # estimated import VAT applies; a second pricey copy makes the cheap one
        # detached (far below the next cheapest).
        _l(30, 104, release_artist=None, release_title="White Label (untitled)",
           release_year=1998, release_format='12"', release_country="US",
           buyer_price=44.0, shipping_buyer_price=4.0, ships_from="United States",
           seller_username="cratedigger", seller_rating=95.0),
        _l(31, 104, release_artist=None, release_title="White Label (untitled)",
           buyer_price=90.0, shipping_buyer_price=6.0, ships_from="United States",
           seller_username="vinylimports"),

        # 105 — lone listing, no sold data, but Discogs' own Deal flag is set →
        # remote_only, unranked (sorts last, no computed discount).
        _l(40, 105, release_artist="Various", release_title="Late-Night Compilation",
           release_year=2010, release_format="CD", release_country="Europe",
           buyer_price=5.0, shipping_buyer_price=3.0, ships_from="France",
           is_deal_remote=True, seller_username="bargainbin", seller_rating=97.0),

        # 106 — cheap on the item price (a sold candidate), but huge shipping + US
        # VAT push the effective cost above the all-in benchmark → dropped, never
        # shown.
        _l(50, 106, release_artist="Slint", release_title="Spiderland",
           buyer_price=60.0, shipping_buyer_price=40.0, ships_from="United States",
           seller_username="faraway"),

        # 107 — five copies clustered around the median; nothing is 35% below the
        # asking median → no deal.
        _l(60, 107, release_artist="The Cure", release_title="Disintegration",
           buyer_price=59.0, shipping_buyer_price=5.0),
        _l(61, 107, release_artist="The Cure", release_title="Disintegration",
           buyer_price=60.0, shipping_buyer_price=5.0, seller_username="goth1"),
        _l(62, 107, release_artist="The Cure", release_title="Disintegration",
           buyer_price=61.0, shipping_buyer_price=5.0, seller_username="goth2"),
        _l(63, 107, release_artist="The Cure", release_title="Disintegration",
           buyer_price=62.0, shipping_buyer_price=5.0, seller_username="goth3"),
        _l(64, 107, release_artist="The Cure", release_title="Disintegration",
           buyer_price=63.0, shipping_buyer_price=5.0, seller_username="goth4"),
    ]


def _cond(prices: list[float]) -> dict:
    s = sorted(float(p) for p in prices)
    mid = len(s) // 2
    median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0
    return {"median": median, "count": len(s), "low": s[0], "high": s[-1], "prices": s}


def control_sold_stats() -> dict[int, dict]:
    """Per-release SOLD benchmark map (release_id → parsed sell/history stats).
    Only releases that should consult sold data appear; the rest fall back to the
    asking path. All EUR so the gate's currency match engages."""
    return {
        101: {"currency": "EUR", "last_sold": "2026-05-20",
              "by_condition": {NM: _cond([80, 90, 100, 110, 120, 130, 140, 150])}},
        102: {"currency": "EUR", "last_sold": "2026-05-18",
              "by_condition": {NM: _cond([80, 90, 100, 110, 120, 130]),
                               M: _cond([60, 65, 70, 75, 80])}},
        104: {"currency": "EUR", "last_sold": "2025-11-02",
              "by_condition": {NM: _cond([60, 64])}},
        106: {"currency": "EUR", "last_sold": "2026-04-30",
              "by_condition": {NM: _cond([80, 90, 95, 100, 105, 110])}},
    }


def control_price_history() -> dict:
    """Prior observed landed lows (state.json shape). Release 101's NM history sits
    above the primary's landed price, so it earns the all-time-low badge."""
    return {
        f"101:{NM}": [
            {"d": "2026-03-01", "p": 72.0, "c": "EUR"},
            {"d": "2026-04-01", "p": 75.0, "c": "EUR"},
            {"d": "2026-05-01", "p": 78.0, "c": "EUR"},
        ],
    }


def control_cfg() -> dict:
    """Config mirroring `watcher._load_config`'s shipped defaults, so the golden
    validates production behaviour (not test-only thresholds)."""
    return {
        "my_country": "Netherlands",
        "min_media_condition": "NM",
        "min_sleeve_condition": "VG+",
        "vat_rate": 0.21,
        "price_drop_threshold": 0.05,
        "asking_data_deal_threshold": 0.35,
        "asking_min_points": 5,
        "sold_deal_percentile": 20.0,
        "sold_deal_min_discount": 0.05,
        "sold_price_min_points": 5,
        "shipping_allowance": 7.0,
        "sold_tier_caveat_gap": 0.10,
        "sold_tier_caveat_min_points": 3,
        "group_by_release": True,
        "max_siblings_per_release": 1,
        "price_history_min_points": 3,
    }
