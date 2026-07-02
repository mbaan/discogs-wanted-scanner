"""Live-marketplace parse — the guard against selector drift on the
/sell/release/{id} listings table. The fixture holds five real rows spanning
native currencies (GBP/EUR/USD), a converted (import) price, a shipping-unknown
row with no converted price, and grades G..NM. All offline."""
from pathlib import Path

import marketplace

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name):
    return (FIXTURES / name).read_text()


def _by_id(copies):
    return {c["listing_id"]: c for c in copies}


def test_parse_full_fixture_row_count():
    copies = marketplace.parse_release_listings(_read("marketplace_release.html"))
    assert copies is not None
    assert len(copies) == 5


def test_parse_domestic_eu_row():
    c = _by_id(marketplace.parse_release_listings(_read("marketplace_release.html")))[3802874330]
    assert c["price"] == 7.00
    assert c["currency"] == "EUR"
    assert c["landed"] == 19.90               # converted_price = item + shipping, account ccy
    assert c["landed_currency"] == "EUR"
    assert c["media_condition"] == "Good (G)"
    assert c["sleeve_condition"] == "Fair (F)"
    assert c["seller_username"] == "friedrichfreistil"
    assert c["seller_rating"] == 100.0
    assert c["ships_from"] == "Austria"
    assert c["listing_url"] == "https://www.discogs.com/sell/item/3802874330"


def test_parse_import_row_converts_landed():
    c = _by_id(marketplace.parse_release_listings(_read("marketplace_release.html")))[3001927271]
    assert c["price"] == 12.99
    assert c["currency"] == "USD"
    assert c["landed"] == 42.67               # $48.49 → €42.67, in account ccy
    assert c["landed_currency"] == "EUR"
    assert c["ships_from"] == "United States"


def test_landed_none_when_shipping_unknown():
    c = _by_id(marketplace.parse_release_listings(_read("marketplace_release.html")))[3311872116]
    assert c["currency"] == "GBP"
    assert c["price"] == 8.50
    assert c["landed"] is None                # converted_price empty when shipping unspecified
    assert c["media_condition"] == "Very Good Plus (VG+)"
    assert c["sleeve_condition"] == "Very Good (VG)"


def test_parse_nm_canada_row():
    c = _by_id(marketplace.parse_release_listings(_read("marketplace_release.html")))[2413408244]
    assert c["media_condition"] == "Near Mint (NM or M-)"
    assert c["sleeve_condition"] == "Very Good Plus (VG+)"
    assert c["landed"] == 63.72
    assert c["ships_from"] == "Canada"


def test_none_on_no_rows():
    assert marketplace.parse_release_listings("<html><body>nothing</body></html>") is None
    assert marketplace.parse_release_listings("") is None
    assert marketplace.parse_release_listings(None) is None


# ── get_release_listings: run-cache + fail-open (no persistent TTL — live data) ──

def test_get_none_when_session_off():
    assert marketplace.get_release_listings(123, session=None, run_cache={}) is None


def test_get_parses_and_caches_within_run(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(release_id, session):
        calls["n"] += 1
        return _read("marketplace_release.html")

    monkeypatch.setattr(marketplace, "_fetch_release_html", fake_fetch)
    run_cache = {}
    first = marketplace.get_release_listings(2837, session=object(), run_cache=run_cache)
    assert first is not None and len(first) == 5
    # Second call for the same release must hit the run cache, not refetch.
    second = marketplace.get_release_listings(2837, session=object(), run_cache=run_cache)
    assert second is first
    assert calls["n"] == 1


def test_get_caches_none_result(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(release_id, session):
        calls["n"] += 1
        return None                    # 403 / parse drift

    monkeypatch.setattr(marketplace, "_fetch_release_html", fake_fetch)
    run_cache = {}
    assert marketplace.get_release_listings(9, session=object(), run_cache=run_cache) is None
    assert marketplace.get_release_listings(9, session=object(), run_cache=run_cache) is None
    assert calls["n"] == 1             # None is cached too — don't hammer within a run
