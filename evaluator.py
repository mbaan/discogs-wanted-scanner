"""
Per-release, per-condition deal evaluation.

For each release, listings are bucketed by media_condition. Within a bucket we
compute each listing's *effective cost* — the true cost to the buyer: landed
price (item + shipping) plus estimated import VAT for non-EU origins — and take
the median over those. A listing is a deal when its effective discount
(1 - effective_cost / median) is at least DEAL_THRESHOLD. Deals are ranked
deepest-first; anything at or above BIG_DEAL_THRESHOLD earns a `big_deal` flag.

Solo listings (n=1 in a bucket, no peer to compare against) have no computed
discount; they emit only if Discogs' own `isDeal` flag fires, and are marked
`ranked=False` so they sort below all discount-ranked deals.

VAT is estimated by region (see `vat_applies`): EU/domestic prices already
include VAT, so only non-EU imports are uplifted. The same uplift is applied to
every listing in the bucket, keeping the median comparison apples-to-apples.
"""

import logging

logger = logging.getLogger(__name__)

PASSING_CONDITIONS = frozenset({
    "Mint (M)",
    "Near Mint (NM or M-)",
    "Very Good Plus (VG+)",
})

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


def condition_short(c: str | None) -> str:
    return _CONDITION_SHORT.get(c or "", c or "?")


def currency_symbol(code: str | None) -> str:
    return {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥"}.get(code or "", (code or "") + " ")


def passes_condition(media: str | None, sleeve: str | None) -> bool:
    """Both vinyl AND sleeve must be VG+ or better."""
    return (media in PASSING_CONDITIONS) and (sleeve in PASSING_CONDITIONS)


def landed_price(listing: dict) -> tuple[float, str]:
    """Total cost to the buyer = item + shipping, in the buyer's currency."""
    item = listing.get("buyer_price") or listing.get("price") or 0.0
    ship = listing.get("shipping_buyer_price") or listing.get("shipping_price") or 0.0
    ccy = listing.get("buyer_currency") or listing.get("currency") or "EUR"
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


def price_drop_pct(listing: dict) -> float:
    """
    How much did the *buyer-currency* price drop vs the seller's previous price?
    Returns 0.0 when there's no drop or no previous-price data.
    """
    cur = listing.get("buyer_price") or listing.get("price")
    prev = listing.get("previous_buyer_price") or listing.get("previous_price")
    if not cur or not prev or prev <= cur:
        return 0.0
    return 1.0 - (cur / prev)


def _own_median(price_suggestions: dict | None, condition: str) -> tuple[float | None, str | None]:
    if not price_suggestions:
        return None, None
    obj = price_suggestions.get(condition)
    if not isinstance(obj, dict):
        return None, None
    return obj.get("value"), obj.get("currency")


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def evaluate_release_group(
    listings: list[dict],
    deal_threshold: float,
    my_country: str,
    vat_rate: float = 0.21,
    big_deal_threshold: float = 0.50,
) -> list[dict]:
    """Bucket by media_condition, then evaluate each bucket independently."""
    if not listings:
        return []

    # Partition by media condition.
    buckets: dict[str, list[dict]] = {}
    for l in listings:
        cond = l.get("media_condition") or ""
        buckets.setdefault(cond, []).append(l)

    deals: list[dict] = []
    for cond, bucket in buckets.items():
        deals.extend(_evaluate_condition_bucket(
            cond, bucket, deal_threshold, my_country, vat_rate, big_deal_threshold,
        ))
    return deals


def _evaluate_condition_bucket(
    condition: str,
    bucket: list[dict],
    deal_threshold: float,
    my_country: str,
    vat_rate: float,
    big_deal_threshold: float,
) -> list[dict]:
    # (landed, ccy, effective_cost, listing), cheapest effective cost first
    # (which is also the deepest discount first).
    enriched = []
    for l in bucket:
        landed, ccy = landed_price(l)
        eff = effective_cost(landed, l.get("ships_from"), my_country, vat_rate)
        enriched.append((landed, ccy, eff, l))
    enriched.sort(key=lambda t: t[2])
    n = len(enriched)
    cond_short = condition_short(condition)

    if n == 1:
        landed, ccy, eff, only = enriched[0]
        if not only.get("is_deal_remote"):
            return []
        return [_verdict(
            only, landed, ccy, eff,
            discount_pct=None, effective_discount=None, ranked=False, big_deal=False,
            reason=f"Discogs flagged · only {cond_short} listing on the marketplace",
            source="remote_only",
            median_value=None, median_currency=None,
            my_country=my_country, vat_rate=vat_rate,
        )]

    bucket_median = _median([e[2] for e in enriched])
    sym = currency_symbol(enriched[0][1])
    out: list[dict] = []
    for landed, ccy, eff, listing in enriched:
        if eff >= bucket_median * (1.0 - deal_threshold):
            continue
        discount = 1.0 - (eff / bucket_median)
        pct = int(discount * 100)
        out.append(_verdict(
            listing, landed, ccy, eff,
            discount_pct=pct, effective_discount=discount, ranked=True,
            big_deal=discount >= big_deal_threshold,
            reason=f"{pct}% below {cond_short} median {sym}{bucket_median:.2f} of {n}",
            source="below_condition_median",
            median_value=bucket_median, median_currency=ccy,
            my_country=my_country, vat_rate=vat_rate,
        ))
    return out


def _verdict(
    listing: dict, landed: float, ccy: str, eff: float,
    *, discount_pct: int | None, effective_discount: float | None,
    ranked: bool, big_deal: bool, reason: str, source: str,
    median_value: float | None, median_currency: str | None,
    my_country: str, vat_rate: float,
) -> dict:
    vat_estimated = bool(vat_rate) and vat_applies(listing.get("ships_from"), my_country)
    return {
        **listing,
        "deal_reason": reason,
        "deal_source": source,
        "discount_pct": discount_pct,
        "effective_discount": effective_discount,
        "ranked": ranked,
        "big_deal": big_deal,
        "is_deal": True,
        "median_value": median_value,
        "median_currency": median_currency,
        "landed_price": landed,
        "landed_currency": ccy,
        "effective_cost": eff,
        "vat_amount": round(eff - landed, 2),
        "vat_estimated": vat_estimated,
        "shipping_region": get_shipping_region(listing.get("ships_from"), my_country),
    }


def get_shipping_region(ships_from: str | None, my_country: str) -> str:
    if not ships_from:
        return "Unknown shipping origin"
    sf = ships_from.strip()
    if sf == my_country.strip():
        return f"Domestic ({sf})"
    if sf in EU_COUNTRIES:
        return f"EU ({sf})"
    return f"International ({sf})"


def group_by_seller(listings: list[dict], passing_only: bool = True) -> dict[int, list[dict]]:
    """Group fetched wantlist listings by seller uid.

    Every /sell_item listing is already a wantlist match, so this yields, per
    seller, the other records of yours they have for sale — the basis for the
    "combine to save shipping" picks. When `passing_only`, keep only listings
    meeting the configured min condition (same gate as the rest of the digest).
    """
    out: dict[int, list[dict]] = {}
    for l in listings:
        uid = l.get("seller_uid")
        if uid is None:
            continue
        if passing_only and not passes_condition(l.get("media_condition"), l.get("sleeve_condition")):
            continue
        out.setdefault(int(uid), []).append(l)
    return out


def seller_picks(seller_listings: list[dict], exclude_id: int, limit: int) -> tuple[list[dict], int]:
    """Cheapest-first compact picks for the email, excluding the deal itself.

    Returns (picks_capped_at_limit, total_other_items).
    """
    others = [l for l in seller_listings if l.get("id") != exclude_id]
    others.sort(key=lambda l: float(l.get("buyer_price") or l.get("price") or 0.0))
    picks = []
    for l in others[:limit]:
        picks.append({
            "release_artist": l.get("release_artist"),
            "release_title": l.get("release_title"),
            "media_condition": l.get("media_condition"),
            "buyer_price": l.get("buyer_price") or l.get("price"),
            "buyer_currency": l.get("buyer_currency") or l.get("currency"),
            "listing_url": l.get("listing_url"),
        })
    return picks, len(others)
