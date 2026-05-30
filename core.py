"""
Pure deal-building pipeline: fetched listings → ranked, annotated deals.

No network, SMTP, or disk I/O — those live in `watcher` (the app/orchestration
layer). `build_deals` is the entry point the UI rework can call directly after
fetching listings, and it's where the testable business logic lives.

The two *network* annotations (Discogs-wide median, per-seller shipping hint)
are applied by the caller after `build_deals` returns, since they make HTTP
calls; everything here is deterministic given its inputs.
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
) -> PipelineResult:
    """Turn fetched listings into a ranked list of new deals.

    `prev_alerted` maps listing id → last-alerted buyer price; a deal is skipped
    unless it's new or its price has dropped at least `price_drop_threshold`
    since. `price_history` is mutated in place: today's lowest landed prices are
    folded in (after deals are annotated against prior observations)."""
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

    for group in by_release.values():
        deals = evaluator.evaluate_release_group(
            group,
            deal_threshold=cfg["deal_threshold"],
            my_country=cfg["my_country"],
            vat_rate=cfg["vat_rate"],
            big_deal_threshold=cfg["big_deal_threshold"],
        )
        for d in deals:
            cur_price = float(d.buyer_price or d.price or 0.0)
            prev = prev_alerted.get(d.id)
            if prev is not None and prev > 0 and cur_price >= prev * (1 - cfg["price_drop_threshold"]):
                continue  # already alerted at near this price
            new_deals.append(d)
            just_alerted.append((d.id, cur_price))
            logger.info(
                "Deal[%s%s]: %s — %s | %s%.2f landed | %s",
                f"{d.discount_pct}%" if d.discount_pct is not None else d.deal_source,
                " ★50%+" if d.big_deal else "",
                d.release_artist or "?", d.release_title or "?",
                evaluator.currency_symbol(d.landed_currency), d.landed_price,
                d.deal_reason,
            )

        # Record price for every listing so re-alerts only fire on drops.
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
    """Deepest effective discount first; unranked (solo/flagged) deals sink to
    the bottom. Artist/title is a stable tie-break so equal-discount deals read
    naturally."""
    return (
        0 if d.ranked else 1,
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
        if len(history) < min_points:
            continue
        floor = min(e["p"] for e in history)
        landed = deal.landed_price
        if landed is not None and landed < floor:
            deal.historical_floor_value = floor
            deal.historical_floor_pct = int((1.0 - landed / floor) * 100)
            deal.historical_data_points = len(history)
