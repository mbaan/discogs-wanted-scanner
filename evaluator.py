"""
Per-release, per-condition deal evaluation.

For each release, listings are bucketed by media_condition. Each bucket is
evaluated on the **SOLD path** when sufficient real sales exist, or falls back to
the **ASKING path** when they don't. Import VAT is estimated per-listing by
shipping origin (see `vat_applies`).

**SOLD path** — used when the bucket's condition has at least `sold_min_points`
recent same-condition sales in a matching currency with raw prices available.
The **gate** (candidacy) operates on the listing's **item price** vs the lower tail
of the sold-price distribution (sold prices are item prices, so that comparison is
apples-to-apples); the **deal decision, ranking, and displayed discount** then operate
on **effective landed cost** against an **all-in benchmark** `B = median +
shipping_allowance` — the going rate to actually receive the record, since sold prices
carry no shipping:

- *Deal*: item price ≤ the `sold_deal_percentile`-th percentile of sold prices AND
  below the sold median by at least `sold_deal_min_discount` (a materiality floor that
  keeps trivially-tight markets quiet) AND effective cost ≤ `B`.
  `deal_source="below_sold_median"`. Discount and rank use effective cost
  (`effective_discount = 1 − effective_cost / B`), so the copy cheapest to actually
  receive leads, and the figure is always ≥ 0. The sold benchmark needs no peer pool,
  so a solo listing can qualify.
- A candidate whose effective cost lands **above `B`** (shipping/VAT erase the deal) is
  **dropped**, not shown.

**ASKING fallback** — low-confidence; used when sold data is insufficient or the
available sold prices are in a non-matching currency. Three sub-cases:

- *1–4 real sales*: item price below the lowest observed sold price AND effective
  cost ≤ that sparse median + `shipping_allowance` (shipping/VAT above it → dropped);
  discount on the effective all-in basis. `deal_source="below_sold_low"`; still
  carries `low_confidence`.
- *No sold data, pool large enough*: item price below the same-condition asking median
  by a steeper threshold than the SOLD path (asking prices are aspirational, so we
  demand more) AND effective cost ≤ asking median + `shipping_allowance`; gated by a
  minimum pool size (`asking_min_points`). Discount uses the same effective all-in
  basis. (`deal_source="below_asking_median"`).
- *Lone Discogs-flagged listing*: a solo listing (n=1 in bucket) with no sold data
  emits only when Discogs' own `isDeal` flag fires (`deal_source="remote_only"`),
  marked `ranked=False` so it sorts below all discount-ranked deals.
- A cheapest copy that is detached far below the next-cheapest is flagged
  `detached_low` — verify the pressing or grade before buying.

All asking-fallback verdicts carry `low_confidence=True`.

**Better-grade caveat** (all paths) — driven by `sold_tiers` / `tier_caveat_gap`:
a lower grade costing less than better grades is the normal condition discount, but
a copy priced within `tier_caveat_gap` of — or above — what a *better* grade
actually sold for is a suspect deal (the apparent discount is measured against a
worse-condition pool). Such listings keep their place but lose the loud red highlight
and carry `sold_tier_caveat`/grade/value so the digest can warn instead of crow.

**VAT** — import VAT (`vat_rate`) is applied **per-listing by shipping origin**:
EU/domestic prices already include VAT and are left untouched; only non-EU imports
(e.g. UK post-Brexit, US) receive the uplift. This means effective landed cost
reflects what each individual listing would actually cost delivered.
"""

import logging

from models import Deal, Listing

logger = logging.getLogger(__name__)

EU_COUNTRIES = frozenset({
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus",
    "Czech Republic", "Denmark", "Estonia", "Finland", "France",
    "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia",
    "Lithuania", "Luxembourg", "Malta", "Netherlands", "Poland",
    "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden",
})

_CONDITION_RANK = {
    "Mint (M)": 6,
    "Near Mint (NM or M-)": 5,
    "Very Good Plus (VG+)": 4,
    "Very Good (VG)": 3,
    "Good Plus (G+)": 2,
    "Good (G)": 1,
    "Fair (F)": 0,
    "Poor (P)": -1,
}

_CONDITION_SHORT = {
    "Mint (M)": "M",
    "Near Mint (NM or M-)": "NM",
    "Very Good Plus (VG+)": "VG+",
    "Very Good (VG)": "VG",
    "Good Plus (G+)": "G+",
    "Good (G)": "G",
    "Fair (F)": "F",
    "Poor (P)": "P",
}


# Inverse of _CONDITION_SHORT, so config can name a floor by its short grade.
_CONDITION_BY_SHORT = {short: full for full, short in _CONDITION_SHORT.items()}

# Fallback gap caveat: a cheapest copy sitting more than this far below the
# next-cheapest is flagged "verify pressing/grade" (possible mis-listing).
_DETACHED_GAP = 0.30


def condition_short(c: str | None) -> str:
    return _CONDITION_SHORT.get(c or "", c or "?")


def currency_symbol(code: str | None) -> str:
    return {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥"}.get(code or "", (code or "") + " ")


def parse_condition(value: str) -> str:
    """Normalize a condition given as a short grade ('NM', 'VG+') or the full
    Discogs string ('Near Mint (NM or M-)') to its canonical full string.

    Raises ValueError on anything unrecognized, so a typo'd .env floor fails loud."""
    v = (value or "").strip()
    if v in _CONDITION_RANK:
        return v
    for short, full in _CONDITION_BY_SHORT.items():
        if short.upper() == v.upper():
            return full
    raise ValueError(
        f"Unknown condition {value!r}; expected one of: "
        + ", ".join(_CONDITION_SHORT.values())
    )


def acceptable_conditions(min_condition: str) -> frozenset[str]:
    """The set of Discogs condition strings at or above `min_condition`."""
    floor = _CONDITION_RANK[parse_condition(min_condition)]
    return frozenset(c for c, rank in _CONDITION_RANK.items() if rank >= floor)


def passes_condition(
    media: str | None, sleeve: str | None,
    media_ok: frozenset[str], sleeve_ok: frozenset[str],
) -> bool:
    """Vinyl must be in `media_ok` and sleeve in `sleeve_ok` (the per-floor sets
    from `acceptable_conditions`)."""
    return (media in media_ok) and (sleeve in sleeve_ok)


def landed_price(listing: Listing) -> tuple[float, str]:
    """Total cost to the buyer = item + shipping, in the buyer's currency."""
    item = listing.buyer_price or listing.price or 0.0
    ship = listing.shipping_buyer_price or listing.shipping_price or 0.0
    ccy = listing.buyer_currency or listing.currency or "EUR"
    return float(item) + float(ship), ccy


def vat_applies(ships_from: str | None, my_country: str) -> bool:
    """True for non-EU imports, where the buyer owes import VAT on arrival.

    EU/domestic prices already include VAT; an unknown origin is treated as no
    VAT (don't penalise a deal on a guess).
    """
    sf = (ships_from or "").strip()
    if not sf:
        return False
    return sf != my_country.strip() and sf not in EU_COUNTRIES


def effective_cost(
    landed: float, ships_from: str | None, my_country: str, vat_rate: float
) -> float:
    """Landed price uplifted by estimated import VAT when shipped from outside the EU."""
    if vat_rate and vat_applies(ships_from, my_country):
        return landed * (1.0 + vat_rate)
    return landed


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated q-th percentile (q in [0, 100]). Empty → 0.0."""
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def sold_tiers(
    by_condition: dict | None,
    target_condition: str | None,
    sold_ccy: str | None = None,
    caveat_min_points: int = 0,
) -> dict | None:
    """Tiered SOLD view of a release relative to the grade we actually found.

    `by_condition` is the per-condition sold map from
    `sold_prices.parse_sell_history` (each entry carrying raw `prices`);
    `target_condition` is the found listing's media grade. Returns how the record
    trades at that grade *and better*:

        {"currency": sold_ccy,
         "exact":       {short, median, count, low, high} | None,   # target grade alone
         "higher":      [{short, median, count, low, high}, ...],    # each better grade, high→low
         "at_or_above": {short, median, count, low, high, grades} | None,  # pooled target+better
         "nearest_higher": {short, median, count} | None}           # cheapest *trusted* better grade

    `at_or_above` pools the raw `prices` across the target grade and every better
    grade, so its median is exact — `None` when an entry lacks raw prices (a legacy
    cache, pre-`prices`). `nearest_higher` is the lowest-rank grade strictly above
    target whose count ≥ `caveat_min_points` and median > 0 — the strictest single
    comparator for the "a better copy costs about the same" caveat (a better grade
    is cheapest just above the found one, so clearing it clears the rest). Returns
    `None` when the target grade is unknown or there's no data at target-or-above.
    """
    if not by_condition or not target_condition:
        return None
    target_rank = _CONDITION_RANK.get(target_condition)
    if target_rank is None:
        return None

    def _stat(cond: str, entry: dict) -> dict:
        return {
            "short": condition_short(cond),
            "median": entry.get("median"),
            "count": entry.get("count") or 0,
            "low": entry.get("low"),
            "high": entry.get("high"),
        }

    exact_entry = by_condition.get(target_condition)
    exact = (
        _stat(target_condition, exact_entry)
        if isinstance(exact_entry, dict) and exact_entry.get("median") is not None
        else None
    )

    # Each grade strictly above target that has a median, richest grade first.
    higher = [
        (rank, cond, entry)
        for cond, entry in by_condition.items()
        if isinstance(entry, dict)
        and entry.get("median") is not None
        and (rank := _CONDITION_RANK.get(cond)) is not None
        and rank > target_rank
    ]
    higher.sort(key=lambda t: t[0], reverse=True)
    higher_stats = [_stat(cond, entry) for _, cond, entry in higher]

    # Pooled "this grade and up" from raw prices (absent if any tier predates them).
    pooled: list[float] = []
    pooled_grades: list[tuple[int, str]] = []
    have_raw = True
    for cond, entry in by_condition.items():
        rank = _CONDITION_RANK.get(cond)
        if rank is None or rank < target_rank or not isinstance(entry, dict):
            continue
        prices = entry.get("prices")
        if prices is None:
            have_raw = False
            break
        pooled.extend(prices)
        pooled_grades.append((rank, condition_short(cond)))
    at_or_above = None
    if have_raw and pooled:
        pooled_grades.sort(key=lambda t: t[0], reverse=True)
        at_or_above = {
            "short": f"{condition_short(target_condition)}↑",
            "median": round(_median(pooled), 2),
            "count": len(pooled),
            "low": round(min(pooled), 2),
            "high": round(max(pooled), 2),
            "grades": [s for _, s in pooled_grades],
        }

    # Strictest caveat comparator: the cheapest *trusted* grade above target.
    nearest_higher = None
    for rank, cond, entry in sorted(higher, key=lambda t: t[0]):  # low rank (cheapest) first
        median = entry.get("median")
        if median and median > 0 and (entry.get("count") or 0) >= caveat_min_points:
            nearest_higher = {
                "short": condition_short(cond),
                "median": float(median),
                "count": entry.get("count") or 0,
            }
            break

    if exact is None and not higher_stats and at_or_above is None:
        return None
    return {
        "currency": sold_ccy,
        "exact": exact,
        "higher": higher_stats,
        "at_or_above": at_or_above,
        "nearest_higher": nearest_higher,
    }


def evaluate_release_group(
    listings: list[Listing],
    my_country: str,
    vat_rate: float = 0.21,
    sold_stats: dict | None = None,
    sold_min_points: int | None = None,
    sold_deal_percentile: float = 20.0,
    sold_deal_min_discount: float = 0.05,
    shipping_allowance: float = 0.0,
    asking_deal_threshold: float = 0.35,
    asking_min_points: int = 5,
    tier_caveat_gap: float = 0.0,
    tier_caveat_min_points: int = 3,
) -> list[Deal]:
    """Bucket by media_condition, then evaluate each bucket independently.

    `sold_stats` is the release's per-condition sold benchmark
    (`{currency, by_condition: {cond: {median, count, ...}}}` from
    `sold_prices.get_sell_history`); when a bucket's condition has enough sold
    data it leads the verdict, else the asking median does. `None` ⇒ asking only."""
    if not listings:
        return []

    # Partition by media condition.
    buckets: dict[str, list[Listing]] = {}
    for l in listings:
        cond = l.media_condition or ""
        buckets.setdefault(cond, []).append(l)

    sold_by_cond = (sold_stats or {}).get("by_condition") or {}
    sold_ccy = (sold_stats or {}).get("currency")

    deals: list[Deal] = []
    for cond, bucket in buckets.items():
        deals.extend(_evaluate_condition_bucket(
            cond, bucket, my_country, vat_rate,
            sold_cond=sold_by_cond.get(cond), sold_ccy=sold_ccy, sold_min_points=sold_min_points,
            sold_deal_percentile=sold_deal_percentile,
            sold_deal_min_discount=sold_deal_min_discount,
            shipping_allowance=shipping_allowance,
            asking_deal_threshold=asking_deal_threshold,
            asking_min_points=asking_min_points,
            sold_by_cond=sold_by_cond,
            tier_caveat_gap=tier_caveat_gap,
            tier_caveat_min_points=tier_caveat_min_points,
        ))
    return deals


def _sold_leads(
    sold_cond: dict | None, sold_ccy: str | None,
    sold_min_points: int | None, bucket_ccy: str | None,
) -> list[float] | None:
    """The raw sold price list when the per-condition SOLD distribution should
    lead this bucket's verdict, else None.

    Requires raw `prices` (a legacy cache without them → None → asking fallback),
    a positive median, at least `sold_min_points` same-condition sales, and a sold
    currency matching the listings'. Anything short returns None."""
    if not sold_cond or sold_min_points is None:
        return None
    prices = sold_cond.get("prices")
    median = sold_cond.get("median")
    count = sold_cond.get("count") or 0
    if not prices or not median or median <= 0:
        return None
    if count < sold_min_points:
        return None
    if not sold_ccy or sold_ccy != bucket_ccy:
        return None
    return [float(p) for p in prices]


def _evaluate_condition_bucket(
    condition: str,
    bucket: list[Listing],
    my_country: str,
    vat_rate: float,
    sold_cond: dict | None = None,
    sold_ccy: str | None = None,
    sold_min_points: int | None = None,
    sold_deal_percentile: float = 20.0,
    sold_deal_min_discount: float = 0.05,
    shipping_allowance: float = 0.0,
    asking_deal_threshold: float = 0.35,
    asking_min_points: int = 5,
    sold_by_cond: dict | None = None,
    tier_caveat_gap: float = 0.0,
    tier_caveat_min_points: int = 3,
) -> list[Deal]:
    # (landed, ccy, effective_cost, listing), cheapest effective cost first
    # (which is also the deepest discount first).
    enriched = []
    for l in bucket:
        landed, ccy = landed_price(l)
        eff = effective_cost(landed, l.ships_from, my_country, vat_rate)
        enriched.append((landed, ccy, eff, l))
    enriched.sort(key=lambda t: t[2])
    n = len(enriched)
    cond_short = condition_short(condition)
    bucket_ccy = enriched[0][1]

    # ── Better-grade caveat comparator ───────────────────────────────────────
    # The cheapest *trusted* grade above this one. A copy priced near/above what a
    # better grade actually sold for is a suspect deal (the apparent discount is
    # against a worse-condition pool); when a listing trips it we mute its red
    # highlight and tag it so the digest can warn. Same-currency only (sold figure is in the
    # account currency; the gap is meaningless across currencies).
    nearest_higher = None
    if tier_caveat_gap > 0 and sold_ccy and sold_ccy == bucket_ccy:
        tiers = sold_tiers(sold_by_cond, condition, sold_ccy, caveat_min_points=tier_caveat_min_points)
        nearest_higher = (tiers or {}).get("nearest_higher")

    def _caveat(eff: float) -> bool:
        return bool(nearest_higher) and eff >= nearest_higher["median"] * (1.0 - tier_caveat_gap)

    # ── Sold-leading branch ──────────────────────────────────────────────────
    # With enough same-condition sold data the SOLD distribution leads: the gate
    # is the listing's ITEM price vs the lower tail of real sales (percentile),
    # not the asking pool. Needs no peer pool, so a solo listing can qualify.
    sold_list = _sold_leads(sold_cond, sold_ccy, sold_min_points, bucket_ccy)
    if sold_list is not None:
        sym = currency_symbol(sold_ccy)
        M = float(sold_cond["median"])
        n_sold = sold_cond["count"]
        p_deal = _percentile(sold_list, sold_deal_percentile)
        deal_ceiling = min(p_deal, M * (1.0 - sold_deal_min_discount))
        # All-in benchmark: the sold median (item only) plus a typical shipping
        # allowance. Sold prices carry no shipping, so comparing the buyer's
        # effective cost against M alone would unfairly penalise the shipping every
        # past buyer also paid; M + S is the going rate to actually receive it.
        benchmark = M + shipping_allowance
        out: list[Deal] = []
        for landed, ccy, eff, listing in enriched:
            item = float(listing.buyer_price or listing.price or 0.0)
            if item <= 0 or item > deal_ceiling:
                continue  # item not in the lower tail of real sales
            if eff > benchmark:
                continue  # shipping/VAT push the all-in cost above the going rate
            caveat = _caveat(eff)
            # Discount + rank on effective cost vs the all-in benchmark — always
            # ≥ 0 for an emitted deal.
            eff_discount = 1.0 - (eff / benchmark)
            pct = int(eff_discount * 100)
            ship_note = f" + ~{sym}{shipping_allowance:.2f} ship" if shipping_allowance else ""
            reason = (f"{pct}% below {cond_short} all-in (SOLD median {sym}{M:.2f}{ship_note}) — "
                      f"item under the cheapest {int(sold_deal_percentile)}% of {n_sold} "
                      f"sales ({sym}{p_deal:.2f})")
            out.append(_verdict(
                listing, landed, ccy, eff,
                discount_pct=pct, effective_discount=eff_discount, ranked=True,
                reason=reason,
                source="below_sold_median",
                median_value=M, median_currency=sold_ccy,
                my_country=my_country, vat_rate=vat_rate,
                caveat=caveat, nearest_higher=nearest_higher,
            ))
        return out

    # ── Fallback (sold did not lead) ──────────────────────────────────────────
    # Every verdict here is low-confidence: asking prices are aspirational, so we
    # lean on any real sales first, then a min-pool-size-guarded asking median.
    sym = currency_symbol(bucket_ccy)
    items = sorted(float(l.buyer_price or l.price or 0.0) for *_, l in enriched)
    second_cheapest = items[1] if len(items) >= 2 else None

    # Sparse real sales (1..min-1) in the matching currency → "below sold low".
    sold_low = None
    sold_low_median = None
    sold_low_count = 0
    if sold_cond and sold_ccy and sold_ccy == bucket_ccy:
        cnt = sold_cond.get("count") or 0
        lo = sold_cond.get("low")
        med = sold_cond.get("median")
        if 1 <= cnt < (sold_min_points or 5) and lo and lo > 0:
            sold_low, sold_low_median, sold_low_count = float(lo), float(med or lo), cnt

    asking_median = _median(items)
    n = len(enriched)

    out: list[Deal] = []
    for landed, ccy, eff, listing in enriched:
        item = float(listing.buyer_price or listing.price or 0.0)
        if item <= 0:
            continue
        caveat = _caveat(eff)
        detached = bool(second_cheapest) and item == items[0] and item < second_cheapest * (1.0 - _DETACHED_GAP)
        verdict = None
        if sold_low is not None and item < sold_low:
            # Cheap on item basis (below the cheapest real sale), but still require the
            # all-in cost to land within the sparse going rate + allowance — the same
            # shipping/VAT guard the sold/asking paths apply. Above it → dropped.
            if eff > sold_low_median + shipping_allowance:
                continue
            benchmark = sold_low_median + shipping_allowance
            discount = 1.0 - (eff / benchmark)
            pct = int(discount * 100)
            verdict = dict(
                discount_pct=pct, effective_discount=discount,
                reason=f"below the lowest of {sold_low_count} recent {cond_short} sale(s) ({sym}{sold_low:.2f})",
                source="below_sold_low", median_value=None,
            )
        elif (n >= asking_min_points
              and item <= asking_median * (1.0 - asking_deal_threshold)
              and eff <= asking_median + shipping_allowance):
            benchmark = asking_median + shipping_allowance
            discount = 1.0 - (eff / benchmark)
            pct = int(discount * 100)
            verdict = dict(
                discount_pct=pct, effective_discount=discount,
                reason=f"{pct}% below {cond_short} asking all-in {sym}{benchmark:.2f} of {n}",
                source="below_asking_median",
                median_value=asking_median,
            )
        elif n == 1 and listing.is_deal_remote:
            out.append(_verdict(
                listing, landed, ccy, eff,
                discount_pct=None, effective_discount=None, ranked=False,
                reason=f"Discogs flagged · only {cond_short} listing on the marketplace",
                source="remote_only", median_value=None, median_currency=None,
                my_country=my_country, vat_rate=vat_rate,
                caveat=caveat, nearest_higher=nearest_higher, low_confidence=True,
            ))
            continue
        if verdict is None:
            continue
        out.append(_verdict(
            listing, landed, ccy, eff,
            discount_pct=verdict["discount_pct"], effective_discount=verdict["effective_discount"],
            ranked=True, reason=verdict["reason"], source=verdict["source"],
            median_value=verdict["median_value"], median_currency=bucket_ccy,
            my_country=my_country, vat_rate=vat_rate,
            caveat=caveat, nearest_higher=nearest_higher,
            low_confidence=True, detached_low=detached,
        ))
    return out


def _verdict(
    listing: Listing, landed: float, ccy: str, eff: float,
    *, discount_pct: int | None, effective_discount: float | None,
    ranked: bool, reason: str, source: str,
    median_value: float | None, median_currency: str | None,
    my_country: str, vat_rate: float,
    caveat: bool = False, nearest_higher: dict | None = None,
    low_confidence: bool = False,
    detached_low: bool = False,
) -> Deal:
    vat_estimated = bool(vat_rate) and vat_applies(listing.ships_from, my_country)
    return Deal.from_listing(
        listing,
        deal_reason=reason,
        deal_source=source,
        discount_pct=discount_pct,
        effective_discount=effective_discount,
        ranked=ranked,
        median_value=median_value,
        median_currency=median_currency,
        landed_price=landed,
        landed_currency=ccy,
        effective_cost=eff,
        vat_amount=round(eff - landed, 2),
        vat_estimated=vat_estimated,
        shipping_region=get_shipping_region(listing.ships_from, my_country),
        sold_tier_caveat=caveat,
        sold_tier_caveat_grade=(nearest_higher or {}).get("short") if caveat else None,
        sold_tier_caveat_value=(nearest_higher or {}).get("median") if caveat else None,
        low_confidence=low_confidence,
        detached_low=detached_low,
    )


def get_shipping_region(ships_from: str | None, my_country: str) -> str:
    if not ships_from:
        return "Unknown shipping origin"
    sf = ships_from.strip()
    if sf == my_country.strip():
        return f"Domestic ({sf})"
    if sf in EU_COUNTRIES:
        return f"EU ({sf})"
    return f"International ({sf})"


def group_by_seller(
    listings: list[Listing],
    media_ok: frozenset[str],
    sleeve_ok: frozenset[str],
    passing_only: bool = True,
) -> dict[int, list[Listing]]:
    """Group fetched wantlist listings by seller uid.

    Every /sell_item listing is already a wantlist match, so this yields, per
    seller, the other records of yours they have for sale — the basis for the
    "combine to save shipping" picks. When `passing_only`, keep only listings
    meeting the configured min condition (same gate as the rest of the digest).
    """
    out: dict[int, list[Listing]] = {}
    for l in listings:
        uid = l.seller_uid
        if uid is None:
            continue
        if passing_only and not passes_condition(l.media_condition, l.sleeve_condition, media_ok, sleeve_ok):
            continue
        out.setdefault(int(uid), []).append(l)
    return out


def seller_picks(seller_listings: list[Listing], exclude_id: int, limit: int) -> tuple[list[dict], int]:
    """Cheapest-first compact picks for the email, excluding the deal itself.

    Returns (picks_capped_at_limit, total_other_items).
    """
    others = [l for l in seller_listings if l.id != exclude_id]
    others.sort(key=lambda l: float(l.buyer_price or l.price or 0.0))
    picks = []
    for l in others[:limit]:
        picks.append({
            "release_artist": l.release_artist,
            "release_title": l.release_title,
            "media_condition": l.media_condition,
            "buyer_price": l.buyer_price or l.price,
            "buyer_currency": l.buyer_currency or l.currency,
            "listing_url": l.listing_url,
        })
    return picks, len(others)
