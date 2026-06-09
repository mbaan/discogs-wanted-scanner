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
