"""
Pure deal-building pipeline: fetched listings → ranked, annotated deals.

No network, SMTP, or disk I/O — those live in `watcher` (the app/orchestration
layer). `build_deals` is the entry point the UI rework can call directly after
fetching listings, and it's where the testable business logic lives.

The *network* annotation (per-seller shipping hint) is applied by the caller
after `build_deals` returns, since it makes HTTP calls; everything here is
deterministic given its inputs.
"""

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import NamedTuple

import evaluator
from models import Deal, Listing

logger = logging.getLogger(__name__)


class PipelineResult(NamedTuple):
    deals: list[Deal]                          # grouped + sorted, ready for annotation
    just_alerted: list[tuple[int, float]]      # (listing_id, buyer_price) to record
    seller_groups: dict[int, list[Listing]]    # qualifying listings per seller uid
    scanned_releases: int                      # distinct wantlist releases for sale


def build_deals(
    listings: list[Listing],
    prev_alerted: dict[int, float],
    price_history: dict,
    cfg: dict,
    now: datetime,
    sold_stats_by_release: dict[int, dict] | None = None,
) -> PipelineResult:
    """Turn fetched listings into a ranked list of new deals.

    `prev_alerted` maps listing id → last-alerted buyer price; a deal is skipped
    unless it's new or its price has dropped at least `price_drop_threshold`
    since. `price_history` is mutated in place: today's lowest landed prices are
    folded in (after deals are annotated against prior observations).
    `sold_stats_by_release` (release_id → per-condition sold benchmark from
    `sold_prices.get_sell_history`) lets the sold median lead the verdict where
    there's enough data; `None`/missing falls back to the asking median."""
    # ── Group by release, filter to qualifying conditions ────────────────────
    media_ok = evaluator.acceptable_conditions(cfg["min_media_condition"])
    sleeve_ok = evaluator.acceptable_conditions(cfg["min_sleeve_condition"])
    by_release: dict[int, list[Listing]] = {}
    skipped_condition = 0
    skipped_no_release = 0
    for l in listings:
        if not evaluator.passes_condition(l.media_condition, l.sleeve_condition, media_ok, sleeve_ok):
            skipped_condition += 1
            continue
        if l.release_id is None:
            skipped_no_release += 1
            continue
        by_release.setdefault(int(l.release_id), []).append(l)
    logger.info(
        "Grouped into %d release(s); skipped %d for condition, %d missing release_id",
        len(by_release), skipped_condition, skipped_no_release,
    )

    # Per-seller view of qualifying wantlist listings — basis for shipping hints.
    seller_groups = evaluator.group_by_seller(listings, media_ok, sleeve_ok)

    # ── Evaluate each release group + re-alert gate ──────────────────────────
    new_deals: list[Deal] = []
    all_evaluated: dict[int, list[Deal]] = {}
    just_alerted: list[tuple[int, float]] = []

    sold_by_release = sold_stats_by_release or {}
    for rid, group in by_release.items():
        deals = evaluator.evaluate_release_group(
            group,
            my_country=cfg["my_country"],
            vat_rate=cfg["vat_rate"],
            sold_stats=sold_by_release.get(rid),
            sold_min_points=cfg.get("sold_price_min_points"),
            sold_deal_percentile=cfg["sold_deal_percentile"],
            sold_deal_min_discount=cfg["sold_deal_min_discount"],
            shipping_allowance=cfg.get("shipping_allowance") or 0.0,
            asking_deal_threshold=cfg["asking_data_deal_threshold"],
            asking_min_points=cfg["asking_min_points"],
            tier_caveat_gap=cfg.get("sold_tier_caveat_gap", 0.0) or 0.0,
            tier_caveat_min_points=cfg.get("sold_tier_caveat_min_points", 3) or 3,
        )
        all_evaluated[rid] = deals
        for d in deals:
            cur_price = float(d.buyer_price or d.price or 0.0)
            prev = prev_alerted.get(d.id)
            if prev is not None and prev > 0 and cur_price >= prev * (1 - cfg["price_drop_threshold"]):
                continue  # already alerted at near this price
            new_deals.append(d)
            logger.info(
                "Deal[%s]: %s — %s | %s%.2f landed | %s",
                f"{d.discount_pct}%" if d.discount_pct is not None else d.deal_source,
                d.release_artist or "?", d.release_title or "?",
                evaluator.currency_symbol(d.landed_currency), d.landed_price,
                d.deal_reason,
            )

        # Record price for every listing (deals included — same id, same price)
        # so re-alerts only fire on drops.
        for l in group:
            just_alerted.append((l.id, float(l.buyer_price or l.price or 0.0)))

    # ── Group + sort ─────────────────────────────────────────────────────────
    if cfg["group_by_release"]:
        new_deals = group_by_release(new_deals, max_siblings=cfg["max_siblings_per_release"])
    new_deals.sort(key=deal_sort_key)

    # Annotate against prior observations, THEN fold today's prices into history.
    annotate_historical_floor(new_deals, price_history, cfg["price_history_min_points"])
    # Surface the best still-listed copy for each deal (dedup-immune: chosen from
    # every evaluated copy, not just the emitted ones), then badge its floor too.
    annotate_best_alt(new_deals, all_evaluated)
    annotate_historical_floor(
        [d.best_alt for d in new_deals if d.best_alt],
        price_history, cfg["price_history_min_points"],
    )
    qualifying = [l for group in by_release.values() for l in group]
    record_price_history(price_history, qualifying, now)

    return PipelineResult(new_deals, just_alerted, seller_groups, len(by_release))


def annotate_sold_price(deals: list[Deal], sold_stats_by_release: dict[int, dict] | None) -> None:
    """Mutate each deal's display fields with the SOLD benchmark for its *media
    condition*, from the already-fetched per-condition sell/history map (no extra
    network). Fail-open — a release or condition without sold data just omits it.

    This drives the "SOLD median …" digest snippet. When the sold median actually
    *led* the verdict the evaluator already set `deal_source='below_sold_median'`;
    this only layers on the display figures (which may also appear, as context, on
    asking-led deals whose condition had some but < min-points sales)."""
    for d in deals:
        stats = (sold_stats_by_release or {}).get(d.release_id)
        if not stats:
            continue
        by_cond = (stats.get("by_condition") or {}).get(d.media_condition or "")
        if not isinstance(by_cond, dict) or by_cond.get("median") is None:
            continue
        d.sold_median_value = by_cond["median"]
        d.sold_data_points = by_cond.get("count")
        d.sold_median_currency = stats.get("currency")
        # Gauge range is display-only: trim to the 10th–90th percentile so one freak
        # sale can't stretch the bar and mislocate the marker. Needs the raw prices
        # and enough of them; otherwise fall back to the true min/max.
        prices = by_cond.get("prices")
        if prices and len(prices) >= 5:
            d.sold_low_value = round(evaluator._percentile(prices, 10), 2)
            d.sold_high_value = round(evaluator._percentile(prices, 90), 2)
        else:
            d.sold_low_value = by_cond.get("low")
            d.sold_high_value = by_cond.get("high")
        d.sold_last_date = stats.get("last_sold")
        # Higher-tier context for display: pooled "this grade and up" + each better
        # grade alone (the caveat fields themselves were set by the evaluator).
        tiers = evaluator.sold_tiers(
            stats.get("by_condition"), d.media_condition or "", stats.get("currency")
        )
        if tiers:
            d.sold_tier_at_or_above = tiers.get("at_or_above")
            d.sold_tier_higher = tiers.get("higher") or []


def build_digest(
    listings: list[Listing],
    prev_alerted: dict[int, float],
    price_history: dict,
    cfg: dict,
    now: datetime,
    sold_stats_by_release: dict[int, dict] | None = None,
) -> PipelineResult:
    """The deterministic, network-free deal stretch: `build_deals` followed by
    `annotate_sold_price`. This is the single seam both `watcher.main` and the
    control-dataset golden test call, so the build + sold-annotation sequence can't
    drift between production and the test. (Shipping annotation is network-bound and
    stays in the caller.)"""
    result = build_deals(listings, prev_alerted, price_history, cfg, now, sold_stats_by_release)
    annotate_sold_price(result.deals, sold_stats_by_release)
    annotate_sold_price(
        [d.best_alt for d in result.deals if d.best_alt], sold_stats_by_release
    )
    return result


def should_flush(pending_count: int, digest_mode: str, now: datetime, digest_hour_utc: int) -> bool:
    """Whether to send a digest now (before applying the daily email cap).

    Hourly mode flushes whenever something is pending; daily mode waits until
    `digest_hour_utc`."""
    if not pending_count:
        return False
    if digest_mode == "daily":
        return now.hour == digest_hour_utc
    return True


# ── Grouping + sort ──────────────────────────────────────────────────────────

def deal_sort_key(d: Deal) -> tuple:
    """Sold-validated deals first, then low-confidence (asking) deals, then unranked
    (solo/flagged). Within a tier: deepest effective discount first, with a stable
    artist/title tie-break."""
    return (
        0 if d.ranked else 1,
        1 if d.low_confidence else 0,
        -(d.effective_discount or 0.0),
        (d.release_artist or "").lower(),
        (d.release_title or "").lower(),
    )


def group_by_release(deals: list[Deal], max_siblings: int = 1) -> list[Deal]:
    """
    Collapse multiple deals for the same release into one primary entry plus
    up to `max_siblings` runner-ups. Primary = deepest effective discount (the
    best deal, which also drives the digest sort order). With max_siblings=1,
    each release contributes at most 2 visible listings.
    """
    by_release: dict[int, list[Deal]] = {}
    no_release: list[Deal] = []
    for d in deals:
        rid = d.release_id
        if rid:
            by_release.setdefault(int(rid), []).append(d)
        else:
            no_release.append(d)

    grouped: list[Deal] = []
    for rid, group in by_release.items():
        group.sort(key=deal_sort_key)
        siblings = [
            {
                "landed_price": s.landed_price,
                "landed_currency": s.landed_currency,
                "listing_url": s.listing_url,
                "seller_username": s.seller_username,
                "media_condition": s.media_condition,
                "discount_pct": s.discount_pct,
            }
            for s in group[1:1 + max_siblings]
        ]
        grouped.append(dataclasses.replace(group[0], siblings=siblings))
    grouped.extend(no_release)
    return grouped


# ── Historical price floor ───────────────────────────────────────────────────
# We persist the lowest landed price we've *observed* per (release, condition)
# in state.json, one entry per day, pruned to a rolling window. When a new deal
# beats every prior observed low — and we have enough observations to mean it —
# it earns an "all-time low" badge in the digest.

def record_price_history(price_history: dict, qualifying_listings: list[Listing], now: datetime) -> None:
    """Record the lowest landed price seen today per (release_id, media_condition)."""
    today = now.date().isoformat()
    for listing in qualifying_listings:
        rid = listing.release_id
        cond = listing.media_condition
        if rid is None or not cond:
            continue
        key = f"{rid}:{cond}"
        landed, ccy = evaluator.landed_price(listing)
        landed = round(landed, 2)
        entries = price_history.setdefault(key, [])
        for entry in entries:
            if entry["d"] == today:
                if landed < entry["p"]:
                    entry["p"] = landed
                    entry["c"] = ccy
                break
        else:
            entries.append({"d": today, "p": landed, "c": ccy})


def prune_price_history(price_history: dict, now: datetime, days: int) -> None:
    """Drop entries older than `days` and remove keys left empty."""
    cutoff = (now - timedelta(days=days)).date().isoformat()
    for key in list(price_history):
        kept = [e for e in price_history[key] if e["d"] >= cutoff]
        if kept:
            price_history[key] = kept
        else:
            del price_history[key]


def annotate_historical_floor(deals: list[Deal], price_history: dict, min_points: int) -> None:
    """Mutate deals in-place: badge any whose landed price beats every prior
    observed low for its (release, condition), once we have >= min_points
    observations. Call this BEFORE recording today's prices, so a deal isn't
    compared against its own freshly-recorded entry."""
    for deal in deals:
        rid = deal.release_id
        cond = deal.media_condition
        if rid is None or not cond:
            continue
        history = price_history.get(f"{rid}:{cond}") or []
        # Same-currency observations only — a floor recorded in another currency
        # is not comparable. Entries without 'c' (legacy rows) count as matching
        # rather than vanishing from the history.
        ccy = deal.landed_currency
        if ccy:
            history = [e for e in history if e.get("c") in (None, ccy)]
        if len(history) < min_points:
            continue
        floor = min(e["p"] for e in history)
        landed = deal.landed_price
        if landed is not None and landed < floor:
            deal.historical_floor_value = floor
            deal.historical_floor_pct = int((1.0 - landed / floor) * 100)
            deal.historical_data_points = len(history)


# ── Best price for this record ─────────────────────────────────────────────────
# For each emitted deal, the cheapest still-listed copy of the same release at the
# deal's grade or better that lands cheaper — the "best price for this record" card.
# Drawn from EVERY evaluated copy (built before the re-alert gate), so a cheaper copy
# the gate suppressed still surfaces. This is the fix for "deals worse than one shown
# before": the buyer always sees the genuine current floor for the record.

def annotate_best_alt(deals: list[Deal], evaluated_by_release: dict[int, list[Deal]]) -> None:
    """Mutate deals in-place: set `best_alt` to the cheapest other evaluated copy of
    the same release at this grade or better with a lower effective landed cost.

    `evaluated_by_release` is every deal the evaluator produced per release, BEFORE
    the re-alert gate — so a previously-alerted, still-listed cheaper copy (absent
    from the emitted set) still qualifies. `best_alt` stays None when the deal is
    already the cheapest at its grade-or-better."""
    for deal in deals:
        rid = deal.release_id
        if rid is None:
            continue
        deal_rank = evaluator._CONDITION_RANK.get(deal.media_condition or "", -99)
        deal_eff = deal.effective_cost if deal.effective_cost is not None else deal.landed_price
        if deal_eff is None:
            continue
        best = None
        best_eff = deal_eff
        for c in evaluated_by_release.get(int(rid)) or []:
            if c.id == deal.id:
                continue
            if evaluator._CONDITION_RANK.get(c.media_condition or "", -99) < deal_rank:
                continue  # worse grade — not "this-or-better"
            c_eff = c.effective_cost if c.effective_cost is not None else c.landed_price
            if c_eff is None or c_eff >= best_eff:
                continue
            best, best_eff = c, c_eff
        if best is not None:
            deal.best_alt = best


# ── Live competitive picture ────────────────────────────────────────────────
# When LIVE_MARKET is on, a /sell/release scrape gives the authoritative set of
# every copy currently for sale. From it we set, per deal: the single best *other*
# offer (win or lose, rendered as best_alt), whether this copy is the cheapest at
# the reader's grade-or-better, the rest-of-field ladder, and the summary counts.
# This supersedes annotate_best_alt (evaluated-deals-only) when live data exists;
# a fetch miss leaves the deal untouched so the caller keeps the fallback best_alt.

def annotate_live_market(
    deals: list[Deal],
    live_by_release: dict[int, list[dict]] | None,
    cfg: dict,
    sold_stats_by_release: dict[int, dict] | None = None,
) -> None:
    """Mutate deals in-place with the live competitive picture. `live_by_release`
    maps release_id → parsed copies (`marketplace.parse_release_listings`); a
    missing/empty entry leaves that deal's live fields at their defaults."""
    media_ok = evaluator.acceptable_conditions(cfg["min_media_condition"])
    sleeve_ok = evaluator.acceptable_conditions(cfg["min_sleeve_condition"])
    for deal in deals:
        copies = (live_by_release or {}).get(deal.release_id)
        if not copies:
            continue
        deal.market_authoritative = True

        enriched = []
        for c in copies:
            landed = c.get("landed")
            eff = (evaluator.effective_cost(landed, c.get("ships_from"),
                                            cfg["my_country"], cfg["vat_rate"])
                   if landed is not None else None)
            passes = evaluator.passes_condition(
                c.get("media_condition"), c.get("sleeve_condition"), media_ok, sleeve_ok)
            enriched.append({"copy": c, "eff": eff, "floor": passes})

        floor_copies = [e for e in enriched if e["floor"]]
        priced_floor = sorted((e for e in floor_copies if e["eff"] is not None),
                              key=lambda e: e["eff"])
        deal.market_total = len(floor_copies)
        if priced_floor:
            deal.market_low = round(priced_floor[0]["eff"], 2)
            deal.market_high = round(priced_floor[-1]["eff"], 2)

        best = next((e for e in priced_floor if e["copy"]["listing_id"] != deal.id), None)
        primary_eff = deal.effective_cost if deal.effective_cost is not None else deal.landed_price
        if best is None:
            deal.is_cheapest = True
            deal.best_alt = None
        else:
            deal.is_cheapest = primary_eff is not None and primary_eff <= best["eff"]
            deal.best_alt = _copy_to_deal(best["copy"], best["eff"], deal, cfg, sold_stats_by_release)

        exclude = {deal.id} | ({deal.best_alt.id} if deal.best_alt else set())
        rows = [e for e in enriched if e["copy"]["listing_id"] not in exclude]
        # floor-passing priced first (cheapest-first), then below-floor / unpriced.
        rows.sort(key=lambda e: (not e["floor"], e["eff"] is None, e["eff"] or 0.0))
        deal.market_copies = [_ladder_row(e) for e in rows]


def _copy_to_deal(copy: dict, eff: float, primary: Deal, cfg: dict,
                  sold_stats_by_release: dict[int, dict] | None) -> Deal:
    """Build a display `Deal` for a live copy: effective landed cost + its signed
    discount vs the release's sold median (so the best-other card renders like any
    other). Release metadata is carried over from the primary (same release)."""
    landed = copy["landed"]
    ccy = copy["landed_currency"]
    median = discount_pct = eff_discount = None
    source = ""
    stats = (sold_stats_by_release or {}).get(primary.release_id)
    by_cond = (stats or {}).get("by_condition", {}).get(copy["media_condition"]) if stats else None
    if isinstance(by_cond, dict) and by_cond.get("median") and stats.get("currency") == ccy:
        median = float(by_cond["median"])
        benchmark = median + (cfg.get("shipping_allowance") or 0.0)
        if benchmark > 0:
            eff_discount = 1.0 - eff / benchmark
            discount_pct = int(eff_discount * 100)
            source = "below_sold_median"
    vat_est = bool(cfg["vat_rate"]) and evaluator.vat_applies(copy["ships_from"], cfg["my_country"])
    return Deal(
        id=copy["listing_id"],
        media_condition=copy["media_condition"], sleeve_condition=copy["sleeve_condition"],
        price=copy["price"], currency=copy["currency"],
        buyer_price=copy["price"], buyer_currency=copy["currency"],
        shipping_price=copy["shipping"], shipping_buyer_price=copy["shipping"],
        release_id=primary.release_id, release_title=primary.release_title,
        release_artist=primary.release_artist, release_year=primary.release_year,
        release_format=primary.release_format, release_country=primary.release_country,
        seller_username=copy["seller_username"], seller_rating=copy["seller_rating"],
        ships_from=copy["ships_from"], listing_url=copy["listing_url"],
        deal_source=source, discount_pct=discount_pct, effective_discount=eff_discount,
        ranked=discount_pct is not None,
        median_value=median, median_currency=ccy,
        landed_price=landed, landed_currency=ccy, effective_cost=round(eff, 2),
        vat_amount=round(eff - landed, 2), vat_estimated=vat_est,
        shipping_region=evaluator.get_shipping_region(copy["ships_from"], cfg["my_country"]),
    )


def _ladder_row(e: dict) -> dict:
    """One compact 'rest of the field' row for the digest ladder."""
    c = e["copy"]
    return {
        "listing_id": c["listing_id"],
        "effective_cost": round(e["eff"], 2) if e["eff"] is not None else None,
        "currency": c["landed_currency"] or c["currency"],
        "media_condition": c["media_condition"],
        "sleeve_condition": c["sleeve_condition"],
        "seller_username": c["seller_username"],
        "listing_url": c["listing_url"],
        "below_floor": not e["floor"],
    }
