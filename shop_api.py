"""
Client for Discogs' internal shop-page-api used by /shop/mywants/.
Authenticated via the browser session cookies (sid + session).

HTTP uses curl_cffi with Chrome TLS impersonation because plain `requests`
gets `cf-mitigated: challenge` from Cloudflare on hosts whose Python ssl
stack produces an unrecognised JA3 — cookies and IP can be identical and
CF will still reject based on TLS fingerprint alone.
"""

import base64
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from curl_cffi import requests as cf_requests

logger = logging.getLogger(__name__)

# Bump this when Chrome ships a major version and curl_cffi adds support.
_IMPERSONATE = "chrome131"

_SELL_ITEM_URL = "https://www.discogs.com/api/shop-page-api/sell_item"


class FetchResult(NamedTuple):
    listings: list[dict]
    complete: bool          # False if pagination terminated early (don't advance last_run)
    cookie_invalid: bool    # True on 401/403 — session cookies need re-export


def _load_cookies(cookies_path: Path) -> dict:
    if not cookies_path.exists():
        raise FileNotFoundError(
            f"{cookies_path} not found. "
            "Copy cookies.json.example → cookies.json and paste sid + session from your browser."
        )
    with open(cookies_path) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def session_expires_at(cookies: dict) -> datetime | None:
    """
    Parse `_expires` from the session cookie value, e.g.
        <token>=?_expires=<base64 unix ts>&created_at=...
    Returns None on a malformed cookie.
    """
    raw = cookies.get("session")
    if not raw or "_expires=" not in raw:
        return None
    try:
        marker = "_expires="
        idx = raw.index(marker) + len(marker)
        end = raw.find("&", idx)
        b64 = raw[idx:end] if end != -1 else raw[idx:]
        ts = int(base64.b64decode(b64))
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, KeyError):
        return None


def _make_session(cookies: dict):
    session = cf_requests.Session(impersonate=_IMPERSONATE)
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.discogs.com/shop/mywants/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    session.cookies.update(cookies)
    return session


def _parse_date(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_listing(item: dict) -> dict | None:
    """Normalize one /sell_item entry. Returns None to exclude (no id, or unavailable)."""
    item_id = item.get("itemId")
    if item_id is None:
        return None

    availability = item.get("availability") or {}
    if availability.get("isAvailable") is False:
        return None

    price = item.get("price") or {}
    prev_price = item.get("previousPrice") or {}
    shipping = item.get("shipping") or {}
    release = item.get("release") or {}
    seller = item.get("seller") or {}

    artists = release.get("artists") or []
    artist_name = artists[0].get("name") if artists and isinstance(artists[0], dict) else None

    genres = release.get("genres") or []
    genre_names = [g.get("name") for g in genres if isinstance(g, dict) and g.get("name")]

    return {
        "id": int(item_id),
        "listed_at": _parse_date(item.get("listedDate")),
        "media_condition": item.get("mediaCondition"),
        "sleeve_condition": item.get("sleeveCondition"),

        # Native (seller) currency
        "price": float(price.get("amount") or 0.0),
        "currency": price.get("currencyCode"),

        # Pre-converted to buyer's account currency (what we compare against medians)
        "buyer_price": float(price.get("buyerItemPrice") or price.get("amount") or 0.0),
        "buyer_currency": price.get("buyerCurrencyCode") or price.get("currencyCode"),
        "price_usd": float(price.get("amountUsd") or 0.0),

        # Previous price (for drop detection) — Discogs sets prev == current when no change
        "previous_price": float(prev_price.get("amount") or 0.0) or None,
        "previous_buyer_price": float(prev_price.get("buyerItemPrice") or 0.0) or None,
        "previous_currency": prev_price.get("currencyCode"),

        "shipping_price": shipping.get("shippingPrice"),
        "shipping_buyer_price": shipping.get("buyerShippingPrice"),
        "shipping_free_min": shipping.get("freeShippingMin"),

        "image_url": item.get("imageUrl"),
        "comments": item.get("comments") or "",
        "accepts_offers": bool(item.get("allowsOffers")),
        "is_deal_remote": bool(item.get("isDeal")),

        "release_id": release.get("releaseId"),
        "release_title": release.get("title"),
        "release_artist": artist_name,
        "release_year": release.get("year"),
        "release_country": release.get("country"),
        "release_format": release.get("majorFormat"),
        "release_genres": genre_names,

        "seller_uid": seller.get("uid"),
        "seller_username": seller.get("name"),
        "seller_rating": seller.get("rating"),
        "seller_rating_count": seller.get("ratingCount"),
        "ships_from": seller.get("shipsFrom"),

        "listing_url": f"https://www.discogs.com/sell/item/{item_id}",
    }


# ── Pagination ──────────────────────────────────────────────────────────────

def fetch_listings(
    cookies_path: Path,
    seller_rating_min: int | None = None,
    listed_after: datetime | None = None,
    page_size: int = 100,
    max_pages: int | None = None,
    debug: bool = False,
) -> FetchResult:
    """
    Paginate /sell_item, sorted newest-first.

    `listed_after`: if set, stop at the first listing at or before this
    timestamp. If None, paginate to completion.
    """
    cookies = _load_cookies(cookies_path)
    session = _make_session(cookies)

    listings: list[dict] = []
    offset = 0
    page_index = 0

    while True:
        if max_pages is not None and page_index >= max_pages:
            logger.warning("Hit max_pages=%d at offset %d — pagination capped", max_pages, offset)
            return FetchResult(listings, complete=False, cookie_invalid=False)

        params: dict = {
            "sort": "listedDate",
            "sortOrder": "descending",
            "count": page_size,
            "offset": offset,
            "facets": "false",
        }
        if seller_rating_min is not None:
            params["sellerRatingMin"] = seller_rating_min

        try:
            resp = session.get(_SELL_ITEM_URL, params=params, timeout=30)
        except Exception as exc:
            logger.error("Network error fetching /sell_item at offset %d: %s", offset, exc)
            return FetchResult(listings, complete=False, cookie_invalid=False)

        if resp.status_code in (401, 403):
            logger.error(
                "HTTP %d at /sell_item — session cookies expired or invalid. "
                "Re-export sid + session from your browser to cookies.json.",
                resp.status_code,
            )
            return FetchResult([], complete=False, cookie_invalid=True)

        if resp.status_code != 200:
            logger.error("Unexpected HTTP %d from /sell_item at offset %d: %.300s",
                         resp.status_code, offset, resp.text)
            return FetchResult(listings, complete=False, cookie_invalid=False)

        try:
            data = resp.json()
        except ValueError:
            logger.error("Non-JSON response from /sell_item at offset %d", offset)
            return FetchResult(listings, complete=False, cookie_invalid=False)

        if debug and offset == 0:
            sample = (data.get("items") or [{}])[0]
            logger.debug("sell_item totalCount=%s, page item keys=%s",
                         data.get("totalCount"), sorted(sample.keys()))

        items = data.get("items") or []
        if not items:
            return FetchResult(listings, complete=True, cookie_invalid=False)

        hit_cutoff = False
        for item in items:
            parsed = parse_listing(item)
            if parsed is None:
                continue
            if listed_after is not None and parsed["listed_at"] is not None:
                if parsed["listed_at"] <= listed_after:
                    hit_cutoff = True
                    break
            listings.append(parsed)

        if hit_cutoff or len(items) < page_size:
            return FetchResult(listings, complete=True, cookie_invalid=False)

        offset += page_size
        page_index += 1
        time.sleep(1)
