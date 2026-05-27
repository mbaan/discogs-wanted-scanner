"""
Per-release, per-condition deal evaluation.

For each release, listings are bucketed by media_condition and a median
landed price is computed within each bucket. A listing is a deal when its
landed price is at least DEAL_THRESHOLD below its bucket's median. Solo
listings (n=1 in a bucket) emit only if Discogs' own `isDeal` flag fires.

Certainty:
  HIGH   — discount ≥ 40%, or Discogs `isDeal` flag also fires
  MEDIUM — discount in [threshold, 40%) with ≥ 5 comparable listings
  LOW    — above threshold but thin comps, or solo-listing fallback
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

_CERTAINTY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def currency_symbol(code: str | None) -> str:
    return {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥"}.get(code or "", (code or "") + " ")


def passes_condition(media: str | None, sleeve: str | None) -> bool:
    """Both vinyl AND sleeve must be VG+ or better."""
    return (media in PASSING_CONDITIONS) and (sleeve in PASSING_CONDITIONS)


def certainty_passes_min(label: str, min_label: str) -> bool:
    return _CERTAINTY_RANK.get(label, -1) >= _CERTAINTY_RANK.get(min_label, 0)


def landed_price(listing: dict) -> tuple[float, str]:
    """Total cost to the buyer = item + shipping, in the buyer's currency."""
    item = listing.get("buyer_price") or listing.get("price") or 0.0
    ship = listing.get("shipping_buyer_price") or listing.get("shipping_price") or 0.0
    ccy = listing.get("buyer_currency") or listing.get("currency") or "EUR"
    return float(item) + float(ship), ccy


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
        deals.extend(
            _evaluate_condition_bucket(cond, bucket, deal_threshold, my_country)
        )
    return deals


def _evaluate_condition_bucket(
    condition: str,
    bucket: list[dict],
    deal_threshold: float,
    my_country: str,
) -> list[dict]:
    enriched = [(landed_price(l), l) for l in bucket]
    enriched.sort(key=lambda t: t[0][0])
    n = len(enriched)
    cond_short = condition_short(condition)

    if n == 1:
        only = enriched[0][1]
        landed, ccy = enriched[0][0]
        if not only.get("is_deal_remote"):
            return []
        return [_verdict(
            only, landed, ccy,
            is_deal=True, discount_pct=0,
            reason=f"Discogs flagged · only {cond_short} listing on the marketplace",
            source="remote_only",
            certainty="LOW",
            detail=f"No peer {cond_short} listings to compare against",
            median_value=None, median_currency=None,
            my_country=my_country,
        )]

    landed_values = [e[0][0] for e in enriched]
    bucket_median = _median(landed_values)
    sym = currency_symbol(enriched[0][0][1])
    out: list[dict] = []
    for (landed, ccy), listing in enriched:
        if landed >= bucket_median * (1.0 - deal_threshold):
            continue
        discount = 1.0 - (landed / bucket_median)
        pct = int(discount * 100)
        is_deal_remote = bool(listing.get("is_deal_remote"))
        certainty = _certainty(discount, n, is_deal_remote)
        # n=2: median equals one of the two prices, so degrade unless Discogs concurs
        if n == 2 and not is_deal_remote:
            certainty = "LOW"
        out.append(_verdict(
            listing, landed, ccy,
            is_deal=True, discount_pct=pct,
            reason=f"{pct}% below {cond_short} median {sym}{bucket_median:.2f} of {n}",
            source="below_condition_median",
            certainty=certainty,
            detail=_certainty_detail(discount, n, is_deal_remote),
            median_value=bucket_median, median_currency=ccy,
            my_country=my_country,
        ))
    return out


def _certainty(discount: float, comps: int, is_deal_remote: bool) -> str:
    if is_deal_remote or discount >= 0.40:
        return "HIGH"
    if comps >= 5:
        return "MEDIUM"
    return "LOW"


def _certainty_detail(discount: float, comps: int, is_deal_remote: bool) -> str:
    pct = int(discount * 100)
    parts = [f"{pct}% below median"]
    if comps:
        parts.append(f"{comps} comps for sale")
    if is_deal_remote:
        parts.append("Discogs flagged as deal")
    return ", ".join(parts)


def _verdict(
    listing: dict, landed: float, ccy: str,
    *, is_deal: bool, discount_pct: int, reason: str, source: str,
    certainty: str, detail: str,
    median_value: float | None, median_currency: str | None,
    my_country: str,
) -> dict:
    return {
        **listing,
        "deal_reason": reason,
        "deal_source": source,
        "discount_pct": discount_pct,
        "is_deal": is_deal,
        "certainty_label": certainty,
        "certainty_detail": detail,
        "median_value": median_value,
        "median_currency": median_currency,
        "landed_price": landed,
        "landed_currency": ccy,
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
