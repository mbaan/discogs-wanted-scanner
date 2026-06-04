"""
Release sold-price benchmark — what a record *actually* trades for, per condition.

Sold prices are not in the official API and never will be; they live only in the
logged-in website HTML. The sell-history page

    https://www.discogs.com/sell/history/{id}

renders a table of recent individual sales — one `sales-history-row` per sale with
the order date, media condition, sleeve condition, and price. The "Price in your
currency" column (`<td class="price">`) is *pre-converted to the account currency*,
so we read it directly; the adjacent `converted_price` cell holds the original sale
currency, which we ignore. Rows are aggregated by **media** condition (sleeve
ignored) into a per-condition median / count / low / high, plus the most recent sale
date. Parsed with `re` (no bs4).

Authenticated via the same cookie session `shop_api` uses (curl_cffi Chrome
impersonation), NOT the PAT — the caller injects a ready session. Fail-open: any
miss (no cookies, 401/403, parse drift, no sales) returns None, and the caller
simply falls back to the asking-median logic.
"""

import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_HISTORY_URL = "https://www.discogs.com/sell/history/{id}"

# Symbol → ISO code, consistent with evaluator.currency_symbol.
_SYMBOL_TO_CODE = {"€": "EUR", "$": "USD", "£": "GBP", "¥": "JPY"}

# One sales row of the history table. Comment rows (`sales-history-comment`) and
# the header carry no `sales-history-row` class, so they're skipped. The `price`
# cell is the account-currency figure ("Price in your currency").
_ROW_RE = re.compile(r'<tr[^>]*\bsales-history-row\b[^>]*>(.*?)</tr>', re.DOTALL)
_MEDIA_RE = re.compile(r'data-header="Media:"[^>]*>(.*?)</td>', re.DOTALL)
_DATE_RE = re.compile(r'data-header="Order Date:"[^>]*>(.*?)</td>', re.DOTALL)
_PRICE_RE = re.compile(r'<td class="price">(.*?)</td>', re.DOTALL)
_DATE_FMT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── Freshness (copied from shipping_policy._fresh) ───────────────────────────

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


# ── Parse (pure, network-free, unit-testable on fixtures) ────────────────────

def _text(raw: str | None) -> str:
    """Strip tags + surrounding whitespace from a cell's inner HTML."""
    return re.sub(r"<[^>]+>", "", raw or "").strip()


def _parse_money(raw: str | None) -> tuple[float | None, str | None]:
    """Parse a displayed price like '€42.50' or '$1,234.00' into (amount, code).
    Symbol→code mirrors evaluator.currency_symbol. Blank/garbage → (None, None)."""
    if not raw:
        return None, None
    s = str(raw)
    code = next((c for sym, c in _SYMBOL_TO_CODE.items() if sym in s), None)
    cleaned = re.sub(r"[^\d.,-]", "", s).replace(",", "")
    try:
        return float(cleaned), code
    except ValueError:
        return None, code


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def parse_sell_history(html: str | None) -> dict | None:
    """Normalize the sell-history table into per-media-condition sold stats, or None.

    Returns:
        {"currency": str, "last_sold": "YYYY-MM-DD"|None,
         "by_condition": {<full media condition>: {"median", "count", "low", "high", "prices"}}}
    or None when no sales rows parse, or the price column isn't a single currency
    (the price cell is the account currency; anything else means parse drift → fail
    open rather than mix currencies).
    """
    if not html:
        return None
    by_cond: dict[str, list[float]] = {}   # full media condition → account-ccy prices
    codes: set[str] = set()
    dates: list[str] = []
    for row in _ROW_RE.findall(html):
        mm = _MEDIA_RE.search(row)
        pm = _PRICE_RE.search(row)
        if not mm or not pm:
            continue
        amount, code = _parse_money(_text(pm.group(1)))
        cond = _text(mm.group(1))
        if amount is None or not cond:
            continue
        by_cond.setdefault(cond, []).append(amount)
        if code:
            codes.add(code)
        dm = _DATE_RE.search(row)
        if dm:
            d = _DATE_FMT_RE.search(_text(dm.group(1)))
            if d:
                dates.append(d.group(0))
    if not by_cond:
        return None
    if len(codes) != 1:
        logger.debug("sell_history: price column not a single currency (%s) — skipping", codes)
        return None
    by_condition = {
        cond: {
            "median": round(_median(v), 2),
            "count": len(v),
            "low": round(min(v), 2),
            "high": round(max(v), 2),
            # Raw per-sale prices (sorted), so the caller can pool *higher-tier*
            # medians ("this grade and up") that can't be reconstructed from the
            # per-condition median/count/low/high alone. See evaluator.sold_tiers.
            "prices": sorted(round(p, 2) for p in v),
        }
        for cond, v in by_cond.items()
    }
    return {
        "currency": next(iter(codes)),
        "last_sold": max(dates) if dates else None,
        "by_condition": by_condition,
    }


# ── Fetch + persistent cache ─────────────────────────────────────────────────

def _fetch_history_html(release_id, session) -> str | None:
    """The single HTTP locus. Returns the page HTML on 200, else None. Paces 1s
    after an actual fetch (mirror shop_api.fetch_listings). Degrades silently."""
    url = _HISTORY_URL.format(id=release_id)
    try:
        resp = session.get(url, timeout=30)
    except Exception as exc:
        logger.debug("sell_history(%s) network error: %s", release_id, exc)
        return None
    time.sleep(1)
    if resp.status_code != 200:
        logger.debug("sell_history(%s) HTTP %d", release_id, resp.status_code)
        return None
    return resp.text


def get_sell_history(
    release_id,
    *,
    session,
    run_cache: dict,
    persistent: dict,
    ttl_days: int,
) -> dict | None:
    """Return normalized per-condition sold stats for a release, or None.

    Mirrors shipping_policy.get_policy. `run_cache` short-circuits within a run;
    `persistent` (state.json["sell_history"]) survives across runs keyed by
    str(release_id), refetched past `ttl_days`. `session is None` → None (feature
    off / no cookies). None results are cached too (don't hammer; retry post-TTL).
    """
    if session is None or not release_id:
        return None
    rid = str(release_id)

    run_key = f"_sell_history:{rid}"
    if run_key in run_cache:
        return run_cache[run_key]

    ent = (persistent or {}).get(rid)
    if ent and _fresh(ent.get("fetched_at"), ttl_days):
        run_cache[run_key] = ent.get("stats")
        return ent.get("stats")

    stats = parse_sell_history(_fetch_history_html(release_id, session))
    persistent[rid] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "stats": stats}
    run_cache[run_key] = stats
    return stats
