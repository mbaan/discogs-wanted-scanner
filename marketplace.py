"""
Live marketplace listings for a release — every copy currently for sale, so the
digest can show the true competitive field ("is this really the cheapest?").

Like sold prices, the current listings-by-release are not in the official API;
they live in the logged-in website HTML at

    https://www.discogs.com/sell/release/{id}

which renders the marketplace table — one `shortcut_navigable` row per copy with
media/sleeve condition, native price, the account-currency landed price
(`converted_price` = item + shipping, pre-VAT; empty when the seller hasn't set a
shipping cost), seller + rating, and ships-from. Parsed with `re` (no bs4).

Authenticated via the same cookie session `shop_api`/`sold_prices` use. Fail-open:
any miss (no cookies, 401/403, parse drift, no listings) returns None, and the
caller downgrades the competitive claim to "cheapest I can see" instead of a
guarantee.
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

# Cheapest-first so a truncated fetch still keeps the low end (the part that
# matters for "is this the cheapest?"). limit=250 covers all but the deepest pools.
_RELEASE_URL = "https://www.discogs.com/sell/release/{id}?limit=250&sort=price%2Casc"

_SYMBOL_TO_CODE = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}

_ROW_RE = re.compile(r'<tr class="shortcut_navigable[^"]*"[^>]*>(.*?)</tr>', re.DOTALL)
_ITEM_RE = re.compile(r'/sell/item/(\d+)')
_PRICE_RE = re.compile(r'class="price"\s+data-currency=([A-Z]+)\s+data-pricevalue=([\d.]+)')
_SHIP_RE = re.compile(r'class="item_shipping">\s*([^<]*?)\s*<', re.DOTALL)
_CONV_RE = re.compile(r'class="converted_price">\s*(?:about\s*)?([^<]*?)\s*(?:<|$)', re.DOTALL)
_MEDIA_RE = re.compile(r'condition-label-mobile">Media:</span>\s*<span>\s*([^<]+?)\s*<', re.DOTALL)
_SLEEVE_RE = re.compile(r'item_sleeve_condition">\s*([^<]+?)\s*<')
_SELLER_RE = re.compile(r'/seller/([^/]+)/profile')
_RATING_RE = re.compile(r'<strong>([\d.]+)%</strong>')
_SHIPS_FROM_RE = re.compile(r'Ships From:</span>\s*([^<]+)')


def _first(rx: re.Pattern, row: str) -> str | None:
    """First capture group of `rx` in `row`, or None when it doesn't match."""
    m = rx.search(row)
    return m.group(1) if m else None


def _parse_money(raw: str | None) -> tuple[float | None, str | None]:
    """'€20.84' / 'about $1,234.00' → (amount, ISO code). Blank/garbage → (None, code)."""
    if not raw:
        return None, None
    code = next((c for sym, c in _SYMBOL_TO_CODE.items() if sym in raw), None)
    cleaned = re.sub(r"[^\d.,-]", "", raw).replace(",", "")
    if not cleaned:
        return None, code
    try:
        return float(cleaned), code
    except ValueError:
        return None, code


def parse_release_listings(html: str | None) -> list[dict] | None:
    """Normalize the marketplace table into current copies, or None on any miss.

    Each copy:
        {listing_id, media_condition, sleeve_condition,
         price, currency,                 # native (seller) item price
         landed, landed_currency,         # account-ccy item+shipping (pre-VAT), None if no shipping set
         shipping,                        # native shipping amount, None if not shown
         seller_username, seller_rating, ships_from, listing_url}
    """
    if not html:
        return None
    copies: list[dict] = []
    for row in _ROW_RE.findall(html):
        im = _ITEM_RE.search(row)
        pm = _PRICE_RE.search(row)
        if not im or not pm:
            continue
        listing_id = int(im.group(1))
        currency, price = pm.group(1), float(pm.group(2))
        landed, landed_ccy = _parse_money(_first(_CONV_RE, row))
        ship_amt, _ = _parse_money(_first(_SHIP_RE, row))
        rating = _first(_RATING_RE, row)
        ships_from = _first(_SHIPS_FROM_RE, row)

        copies.append({
            "listing_id": listing_id,
            "media_condition": _first(_MEDIA_RE, row),
            "sleeve_condition": _first(_SLEEVE_RE, row),
            "price": price,
            "currency": currency,
            "landed": landed,
            "landed_currency": landed_ccy,
            "shipping": ship_amt,
            "seller_username": _first(_SELLER_RE, row),
            "seller_rating": float(rating) if rating else None,
            "ships_from": ships_from.strip() if ships_from else None,
            "listing_url": f"https://www.discogs.com/sell/item/{listing_id}",
        })
    return copies or None


# ── Fetch + per-run cache ─────────────────────────────────────────────────────

def _fetch_release_html(release_id, session) -> str | None:
    """The single HTTP locus. Returns page HTML on 200, else None. Paces 1s after
    an actual fetch (mirrors sold_prices/shop_api). Degrades silently."""
    url = _RELEASE_URL.format(id=release_id)
    try:
        resp = session.get(url, timeout=30)
    except Exception as exc:
        logger.debug("marketplace(%s) network error: %s", release_id, exc)
        return None
    time.sleep(1)
    if resp.status_code != 200:
        logger.debug("marketplace(%s) HTTP %d", release_id, resp.status_code)
        return None
    return resp.text


def get_release_listings(release_id, *, session, run_cache: dict) -> list[dict] | None:
    """Current marketplace copies for a release, or None. Cookie-gated (NOT the
    PAT). `run_cache` short-circuits within a run; there is deliberately NO
    persistent cache — live listings must be fresh every run. None is cached too
    (a miss shouldn't be retried within the same run). `session is None` → None."""
    if session is None or not release_id:
        return None
    run_key = f"_mp:{release_id}"
    if run_key in run_cache:
        return run_cache[run_key]
    copies = parse_release_listings(_fetch_release_html(release_id, session))
    run_cache[run_key] = copies
    return copies
