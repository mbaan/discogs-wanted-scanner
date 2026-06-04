"""Sold-price parsing + cache. The fixture parse is the core guard against
selector drift on the sell/history table; the cache tests mirror shipping_policy's
TTL semantics. All offline — _fetch_history_html is monkeypatched, so no network
and no pacing sleep."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sold_prices

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name):
    return (FIXTURES / name).read_text()


def _row(media, price, conv="€0.00", date="2026-01-01"):
    return (
        '<tr class="odd sales-history-row float_fix">'
        f'<td class="has_header as_float" data-header="Order Date:">{date}</td>'
        f'<td class="has_header as_float clear" data-header="Media:">{media}</td>'
        f'<td class="has_header as_float clear" data-header="Sleeve:">{media}</td>'
        f'<td class="price">{price}</td>'
        f'<td class="converted_price">{conv}</td></tr>'
    )


def _table(rows):
    return ('<table class="sales-history-table"><tbody>'
            + "".join(rows) + "</tbody></table>")


# ── parse_sell_history ───────────────────────────────────────────────────────

def test_parse_full_fixture():
    stats = sold_prices.parse_sell_history(_read("sold_history.html"))
    assert stats is not None
    assert stats["currency"] == "EUR"            # the "Price in your currency" column
    assert stats["last_sold"] == "2026-03-15"    # newest row across all conditions
    by = stats["by_condition"]
    # Per *media* condition (sleeve ignored); comment row excluded from counts.
    # `prices` is the sorted raw account-currency sales (pooled for higher-tier medians).
    assert by["Mint (M)"] == {"median": 70.0, "count": 6, "low": 25.0, "high": 120.0,
                              "prices": [25.0, 50.0, 60.0, 80.0, 100.0, 120.0]}
    assert by["Near Mint (NM or M-)"] == {"median": 50.0, "count": 5, "low": 30.0, "high": 70.0,
                                          "prices": [30.0, 40.0, 50.0, 60.0, 70.0]}
    assert by["Very Good Plus (VG+)"] == {"median": 20.0, "count": 3, "low": 15.0, "high": 25.0,
                                          "prices": [15.0, 20.0, 25.0]}


def test_parse_reads_account_currency_not_original():
    # Rows whose original sale was £/¥/$ still report the €-converted price column,
    # so the whole release is one currency (EUR) — the foreign converted_price is ignored.
    html = _table([
        _row("Mint (M)", "€40.00", conv="£34.00"),
        _row("Mint (M)", "€60.00", conv="$66.00"),
        _row("Mint (M)", "€50.00", conv="¥8,300"),
    ])
    stats = sold_prices.parse_sell_history(html)
    assert stats["currency"] == "EUR"
    assert stats["by_condition"]["Mint (M)"]["count"] == 3
    assert stats["by_condition"]["Mint (M)"]["median"] == 50.0


def test_parse_mixed_price_currency_returns_none():
    # The price column should be a single (account) currency; mixed ⇒ parse drift.
    html = _table([_row("Mint (M)", "€10.00"), _row("Mint (M)", "$12.00")])
    assert sold_prices.parse_sell_history(html) is None


def test_parse_missing_or_empty_returns_none():
    assert sold_prices.parse_sell_history("<html>no table here</html>") is None
    assert sold_prices.parse_sell_history("<table><tbody></tbody></table>") is None
    assert sold_prices.parse_sell_history("") is None
    assert sold_prices.parse_sell_history(None) is None


# ── _parse_money ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("€42.50", (42.50, "EUR")),
    ("$1,234.00", (1234.0, "USD")),
    ("£9", (9.0, "GBP")),
    ("¥8,300", (8300.0, "JPY")),
    ("", (None, None)),
    ("garbage", (None, None)),
    (None, (None, None)),
])
def test_parse_money(raw, expected):
    assert sold_prices._parse_money(raw) == expected


# ── get_sell_history cache (mirror shipping_policy) ──────────────────────────

def _no_fetch(*_a, **_k):
    raise AssertionError("should not fetch")


def test_session_none_returns_none(monkeypatch):
    monkeypatch.setattr(sold_prices, "_fetch_history_html", _no_fetch)
    persistent = {}
    assert sold_prices.get_sell_history(100001, session=None, run_cache={},
                                        persistent=persistent, ttl_days=30) is None
    assert persistent == {}  # nothing cached


def test_fresh_persistent_served_without_fetch(monkeypatch):
    monkeypatch.setattr(sold_prices, "_fetch_history_html", _no_fetch)
    cached = {"currency": "EUR", "by_condition": {}}
    persistent = {"100001": {"fetched_at": datetime.now(timezone.utc).isoformat(), "stats": cached}}
    out = sold_prices.get_sell_history(100001, session=object(), run_cache={},
                                       persistent=persistent, ttl_days=30)
    assert out == cached


def test_stale_refetches_restamps_and_run_cache_short_circuits(monkeypatch):
    calls = []

    def fake_fetch(release_id, session):
        calls.append(release_id)
        return _read("sold_history.html")

    monkeypatch.setattr(sold_prices, "_fetch_history_html", fake_fetch)
    persistent = {"100001": {"fetched_at": "2000-01-01T00:00:00+00:00", "stats": None}}
    run_cache = {}

    out = sold_prices.get_sell_history(100001, session=object(), run_cache=run_cache,
                                       persistent=persistent, ttl_days=30)
    assert out["by_condition"]["Mint (M)"]["count"] == 6
    assert calls == [100001]
    assert sold_prices._fresh(persistent["100001"]["fetched_at"], 30)  # restamped fresh
    assert run_cache["_sell_history:100001"]["currency"] == "EUR"

    # Second call within the run is served from run_cache — no second fetch.
    monkeypatch.setattr(sold_prices, "_fetch_history_html", _no_fetch)
    again = sold_prices.get_sell_history(100001, session=object(), run_cache=run_cache,
                                         persistent=persistent, ttl_days=30)
    assert again is out


def test_none_result_is_cached(monkeypatch):
    monkeypatch.setattr(sold_prices, "_fetch_history_html", lambda *a, **k: None)
    persistent, run_cache = {}, {}
    out = sold_prices.get_sell_history(100001, session=object(), run_cache=run_cache,
                                       persistent=persistent, ttl_days=30)
    assert out is None
    assert persistent["100001"]["stats"] is None
    assert "_sell_history:100001" in run_cache
    # A miss is cached for the run — don't hammer.
    monkeypatch.setattr(sold_prices, "_fetch_history_html", _no_fetch)
    assert sold_prices.get_sell_history(100001, session=object(), run_cache=run_cache,
                                        persistent=persistent, ttl_days=30) is None
