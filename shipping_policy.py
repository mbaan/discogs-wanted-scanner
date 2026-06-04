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


# ── Combine-shipping basket optimizer ─────────────────────────────────────────

def _listing_native_price(l) -> float:
    """The threshold-relevant native price (policy currency). Falls back to
    buyer_price only when the native price is missing/zero (fail-open).

    The free-shipping threshold (`free_min`) and the tier prices are expressed in
    the policy's native currency, and `watcher._annotate_shipping` already feeds
    `estimate_room` a native-currency subtotal, so the basket math must compare
    candidates on the *same* scale — native `l.price`, never `buyer_price`."""
    price = getattr(l, "price", None)
    if price:
        return float(price)
    return float(getattr(l, "buyer_price", 0.0) or 0.0)


def _item_dict(l, currency: str | None) -> dict:
    """An 'add to the order' item, mirroring seller_picks' shape but carrying the
    NATIVE price (policy currency) the threshold math is done in (§6). Deliberately
    NOT buyer_price: the basket line's figures are the threshold-relevant native
    ones, labelled in the policy currency so symbols stay self-consistent."""
    return {
        "release_artist": getattr(l, "release_artist", None),
        "release_title": getattr(l, "release_title", None),
        "media_condition": getattr(l, "media_condition", None),
        "price": _listing_native_price(l),
        "currency": currency,
        "listing_url": getattr(l, "listing_url", "") or "",
    }


def _solo_fee(policy: dict, deal_price: float, est_grams: int) -> float | None:
    """The shipping fee for buying the deal ALONE (1-item basket) — the baseline
    the saving is framed against (the natural "do nothing" cost). Reuses
    estimate_room; None when the policy carries no tiers."""
    solo = estimate_room(policy, 1, deal_price, est_grams)
    return solo.get("fee_now")


def optimize_basket(
    policy: dict,
    seller_listings: list,
    deal,
    *,
    est_grams: int,
    max_add: int,
) -> dict | None:
    """Recommend other condition-passing wantlist items from THIS seller to add
    to the order, and the shipping it saves vs. buying `deal` alone.

    Pure: consumes only the already-fetched seller listings + the normalized
    policy (same inputs estimate_room gets) — no IO, no network. Returns None when
    there's nothing actionable (no policy, single-item seller, no free threshold
    and no usable tiers, free shipping already met, free threshold unreachable
    within max_add, or tier room == 0). Never raises for normal data (fail-open).

    Free-shipping crossing takes precedence over tier room — a free threshold is
    the bigger, cleaner win. `est_grams` ballparks per-record weight for weight
    tiers (flagged via basis="weight-est"); `max_add` caps the suggestion size.
    """
    if not policy:
        return None

    currency = policy.get("currency")
    # Candidates: every OTHER passing listing from this seller, cheapest-first by
    # native price (the currency the policy/tiers are in). Greedy over this small
    # set is sufficient — a seller carries only a handful of the user's wantlist.
    others = sorted(
        [l for l in seller_listings if getattr(l, "id", None) != getattr(deal, "id", None)],
        key=_listing_native_price,
    )
    if not others:
        return None

    deal_price = _listing_native_price(deal)

    # ── Free-shipping crossing (takes precedence) ────────────────────────────
    free_min = policy.get("free_min")
    if policy.get("free_shipping") and free_min is not None:
        free_min = float(free_min)
        if deal_price >= free_min:
            return None  # already ships free solo — nothing to add
        if max_add >= 1:
            add = _crossing_basket(others, deal_price, free_min, max_add)
            if add is None:
                # Not reachable within max_add / available candidates: emit
                # nothing, never imply a saving that won't materialize.
                return None
            fee_before = _solo_fee(policy, deal_price, est_grams) or 0.0
            return {
                "kind": "free_crossing",
                "currency": currency,
                "add": [_item_dict(x, currency) for x in add],
                "new_subtotal": deal_price + sum(_listing_native_price(x) for x in add),
                "free_min": free_min,
                "fee_before": fee_before,
                "fee_after": 0.0,
                "saving": fee_before,
                "reachable": True,
                "basis": estimate_room(policy, 1, deal_price, est_grams).get("basis"),
            }
        return None

    # ── Tier room (no free threshold to cross) ───────────────────────────────
    return _tier_room_basket(policy, others, deal_price, est_grams, max_add, currency)


def _crossing_basket(others, deal_price, free_min, max_add) -> list | None:
    """The basket of <= max_add cheapest-first candidates that crosses free_min,
    or None when nothing within the cap reaches it.

    Greedy cheapest-first accumulation is the primary strategy (the cheapest combo
    that crosses). When that can't cross within the cap — e.g. max_add=1 and the
    two cheapest items each fall short — fall back to the single cheapest item that
    alone crosses, so a lone qualifying item is still surfaced rather than dropped."""
    add: list = []
    subtotal = deal_price
    for l in others:
        if len(add) >= max_add:
            break
        add.append(l)
        subtotal += _listing_native_price(l)
        if subtotal >= free_min:
            return add
    # Greedy didn't cross within the cap: accept the cheapest single item that does
    # on its own (handles max_add=1 where a pricier-but-sufficient item exists).
    for l in others:
        if deal_price + _listing_native_price(l) >= free_min:
            return [l]
    return None


def _tier_room_basket(policy, others, deal_price, est_grams, max_add, currency) -> dict | None:
    """Reframe estimate_room's room_more/next_fee with concrete cheapest picks.

    Emit only when there's real room (positive int room_more) AND a next tier to
    stay below (next_fee present). At the top tier room_more is None; at the tier
    edge it's 0 — both non-actionable -> None."""
    solo = estimate_room(policy, 1, deal_price, est_grams)
    room_more = solo.get("room_more")
    next_fee = solo.get("next_fee")
    if not isinstance(room_more, int) or room_more <= 0 or next_fee is None:
        return None
    take = min(room_more, max_add, len(others))
    if take <= 0:
        return None
    add = others[:take]
    return {
        "kind": "tier_room",
        "currency": currency,
        "add": [_item_dict(x, currency) for x in add],
        "room_more": room_more,
        "fee_now": solo.get("fee_now"),
        "next_fee": next_fee,
        "basis": solo.get("basis"),
    }
