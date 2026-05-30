"""
Seller shipping-policy lookup + combined-shipping ("room") estimation.

Source: Discogs' internal v3 endpoint (PAT-authenticated, same token as
discogs_api):

    GET https://api.discogs.com/v3/marketplace/shipping/policies
        ?seller_id={uid}&country={MY_COUNTRY}

It returns the seller's full policy: per-region method(s) with either
weight-gram or item-quantity tiers, plus an optional free-shipping threshold.
This is the only place the sub-threshold free-shipping amount is exposed — the
shop-page-api only reveals freeShippingMin once an item already qualifies, and
the per-listing `weight` is null on the official listings endpoint.

Cost control: one GET per seller, throttled through discogs_api's shared 1/s +
429-backoff machinery, and cached persistently (caller passes a dict that lives
in state.json) with a TTL since policies change rarely.
"""

import logging
from datetime import datetime, timezone

import requests

import discogs_api

logger = logging.getLogger(__name__)

_V3_URL = "https://api.discogs.com/v3/marketplace/shipping/policies"
_USER_AGENT = "DiscogsWantlistWatcher/1.0"


# ── Fetch + persistent cache ─────────────────────────────────────────────────

def _fresh(ts_iso: str | None, ttl_days: int) -> bool:
    if not ts_iso:
        return False
    try:
        dt = datetime.fromisoformat(ts_iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() < ttl_days * 86400


def get_policy(
    seller_uid: int,
    country: str,
    *,
    token: str,
    run_cache: dict,
    persistent: dict,
    ttl_days: int = 30,
) -> dict | None:
    """Return a normalized policy for (seller, country), or None.

    `run_cache` is the shared per-run discogs cache (carries the throttle
    sentinel). `persistent` survives across runs (stored in state.json) keyed
    by "uid:country"; entries older than `ttl_days` are refetched.
    """
    if not seller_uid or not token:
        return None
    key = f"{seller_uid}:{country}"

    run_key = f"_shipping_policy:{key}"
    if run_key in run_cache:
        return run_cache[run_key]

    ent = (persistent or {}).get(key)
    if ent and _fresh(ent.get("fetched_at"), ttl_days):
        run_cache[run_key] = ent.get("policy")
        return ent.get("policy")

    policy = _fetch_and_normalize(seller_uid, country, token=token, run_cache=run_cache)
    # Cache even None (a failed/absent lookup) so we don't hammer it, but with a
    # fresh timestamp so it retries after the TTL.
    persistent[key] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "policy": policy}
    run_cache[run_key] = policy
    return policy


def _fetch_and_normalize(seller_uid, country, *, token, run_cache) -> dict | None:
    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    resp = discogs_api._get_with_429_retry(
        _V3_URL, headers=headers,
        params={"seller_id": seller_uid, "country": country},
        cache=run_cache, label=f"shipping_policy({seller_uid})",
    )
    if resp is None:
        return None
    if resp.status_code != 200:
        logger.debug("shipping_policy(%s) HTTP %d", seller_uid, resp.status_code)
        return None
    try:
        data = resp.json()
    except (ValueError, requests.JSONDecodeError):
        logger.debug("shipping_policy(%s) non-JSON", seller_uid)
        return None
    return normalize(data, country)


# ── Normalization ────────────────────────────────────────────────────────────

def normalize(raw: dict, country: str) -> dict | None:
    """Collapse the v3 payload to the one policy/method relevant to `country`.

    Returns:
        {currency, free_shipping, free_min, range_type, method_name,
         tiers: [(max, price), ...]}   # sorted ascending; max==0 means "and up"
    or None when nothing applies.
    """
    policies = raw.get("policies") or []
    chosen = next((p for p in policies if country in (p.get("countries") or [])), None)
    if chosen is None:
        chosen = policies[0] if policies else None
    if chosen is None:
        return None

    method = _cheapest_method(chosen.get("methods") or [])
    return {
        "currency": raw.get("currency"),
        "free_shipping": bool(chosen.get("free_shipping")),
        "free_min": chosen.get("free_shipping_min_order_val"),
        "range_type": method.get("range_type") if method else None,
        "method_name": method.get("name") if method else None,
        "tiers": method.get("tiers") if method else [],
    }


def _tier_sort_key(t: tuple) -> tuple:
    mx = t[0]
    return (mx == 0, mx)  # "and up" (max==0) sorts last


def _cheapest_method(methods: list[dict]) -> dict | None:
    """Pick the method with the lowest entry-tier price (the one a buyer hits)."""
    best = None
    for m in methods:
        tiers = sorted(
            [(r.get("max") or 0, r.get("price") or 0.0) for r in (m.get("ranges") or [])],
            key=_tier_sort_key,
        )
        if not tiers:
            continue
        cand = {"range_type": m.get("range_type"), "name": m.get("name"), "tiers": tiers}
        if best is None or tiers[0][1] < best["tiers"][0][1]:
            best = cand
    return best


# ── Estimation ───────────────────────────────────────────────────────────────

def estimate_room(policy: dict, n_items: int, subtotal: float, est_grams: int) -> dict:
    """Given a normalized policy and the basket (count + native subtotal),
    work out the current fee, how many more items fit, and free-shipping gap.

    For weight policies, item weight is unknown (Discogs hides it) so we ballpark
    with `est_grams` per item — flagged via basis="weight-est".
    """
    out: dict = {
        "currency": policy.get("currency"),
        "method_name": policy.get("method_name"),
        "free_shipping": policy.get("free_shipping"),
        "free_min": policy.get("free_min"),
        "n_items": n_items,
        "subtotal": subtotal,
    }

    if policy.get("free_shipping") and policy.get("free_min") is not None:
        out["free_gap"] = max(0.0, float(policy["free_min"]) - subtotal)

    tiers = policy.get("tiers") or []
    if tiers:
        rtype = policy.get("range_type")
        if rtype == "quantity":
            measure, per = n_items, 1
            out["basis"] = "quantity-exact"
        else:
            measure, per = n_items * est_grams, est_grams
            out["basis"] = "weight-est"
        out["per_item"] = per
        out["range_type"] = rtype

        cur_idx = next((i for i, (mx, _) in enumerate(tiers) if mx == 0 or measure <= mx), len(tiers) - 1)
        mx, price = tiers[cur_idx]
        out["fee_now"] = price
        out["tier_max"] = mx
        out["room_more"] = None if mx == 0 else max(0, int((mx - measure) // per))
        if cur_idx + 1 < len(tiers):
            out["next_fee"] = tiers[cur_idx + 1][1]
        out["tiers"] = tiers

    return out
