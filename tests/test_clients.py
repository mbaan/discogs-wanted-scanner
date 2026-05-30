"""The bug-prone network layers, exercised with a fake transport (no live HTTP):
shop_api pagination/cutoff/cookie-invalid, discogs_api's 429 retry + throttle
sentinel, and shipping_policy's run/persistent caching."""
from datetime import datetime, timezone
from pathlib import Path

import discogs_api
import shipping_policy as sp
import shop_api


# ── Fakes ──────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Returns queued responses in order; repeats the last one if over-called."""
    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


def _item(item_id, listed="2026-05-30T12:00:00Z"):
    return {
        "itemId": item_id,
        "availability": {"isAvailable": True},
        "price": {"amount": 10.0, "currencyCode": "EUR", "buyerItemPrice": 10.0, "buyerCurrencyCode": "EUR"},
        "shipping": {},
        "release": {"releaseId": 1, "title": "T", "artists": [{"name": "A"}]},
        "seller": {"uid": 5, "name": "s"},
        "mediaCondition": "Mint (M)", "sleeveCondition": "Mint (M)",
        "listedDate": listed,
    }


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(shop_api, "_load_cookies", lambda p: {})
    monkeypatch.setattr(shop_api, "_make_session", lambda c: session)
    monkeypatch.setattr(shop_api.time, "sleep", lambda s: None)


# ── shop_api.fetch_listings ─────────────────────────────────────────────────────

def test_fetch_stops_at_listed_after_cutoff(monkeypatch):
    page = {"items": [_item(1, "2026-05-30T12:00:00Z"), _item(2, "2026-05-01T00:00:00Z")]}
    _patch_session(monkeypatch, FakeSession([FakeResp(payload=page)]))
    cutoff = datetime(2026, 5, 15, tzinfo=timezone.utc)
    res = shop_api.fetch_listings(Path("x"), listed_after=cutoff, page_size=2)
    assert res.complete is True
    assert [l.id for l in res.listings] == [1]   # #2 is older than cutoff → stop


def test_fetch_empty_page_completes(monkeypatch):
    _patch_session(monkeypatch, FakeSession([FakeResp(payload={"items": []})]))
    res = shop_api.fetch_listings(Path("x"), page_size=2)
    assert res.complete is True and res.listings == []


def test_fetch_hits_max_pages_marks_incomplete(monkeypatch):
    full = FakeResp(payload={"items": [_item(1), _item(2)]})  # always a full page
    _patch_session(monkeypatch, FakeSession([full]))
    res = shop_api.fetch_listings(Path("x"), page_size=2, max_pages=2)
    assert res.complete is False        # capped before exhausting
    assert res.cookie_invalid is False


def test_fetch_401_flags_cookie_invalid(monkeypatch):
    _patch_session(monkeypatch, FakeSession([FakeResp(status=401, text="nope")]))
    res = shop_api.fetch_listings(Path("x"), page_size=2)
    assert res.cookie_invalid is True and res.listings == []


# ── discogs_api 429 handling ────────────────────────────────────────────────────

def test_429_then_200_retries_once(monkeypatch):
    monkeypatch.setattr(discogs_api.time, "sleep", lambda s: None)
    responses = iter([FakeResp(status=429), FakeResp(status=200, payload={"ok": 1})])
    monkeypatch.setattr(discogs_api.requests, "get", lambda *a, **k: next(responses))
    cache = {}
    resp = discogs_api._get_with_429_retry("u", headers={}, params=None, cache=cache, label="t")
    assert resp.status_code == 200
    assert not cache.get(discogs_api._THROTTLED_KEY)


def test_persistent_429_sets_throttle_sentinel(monkeypatch):
    monkeypatch.setattr(discogs_api.time, "sleep", lambda s: None)
    monkeypatch.setattr(discogs_api.requests, "get", lambda *a, **k: FakeResp(status=429))
    cache = {}
    resp = discogs_api._get_with_429_retry("u", headers={}, params=None, cache=cache, label="t")
    assert resp is None
    assert cache[discogs_api._THROTTLED_KEY] is True
    # Sentinel aborts further calls cheaply.
    assert discogs_api._get_with_429_retry("u", headers={}, params=None, cache=cache, label="t2") is None


# ── shipping_policy caching ──────────────────────────────────────────────────────

_RAW = {
    "currency": "EUR",
    "policies": [{
        "countries": ["Netherlands"],
        "methods": [{"name": "S", "range_type": "weight", "ranges": [{"price": 5.0, "max": 999}]}],
        "free_shipping": False, "free_shipping_min_order_val": None,
    }],
}


def test_get_policy_uses_fresh_persistent_cache(monkeypatch):
    # A fresh persistent entry must be served without any network fetch.
    def _boom(*a, **k):
        raise AssertionError("should not fetch when cache is fresh")
    monkeypatch.setattr(sp, "_fetch_and_normalize", _boom)
    fresh = datetime.now(timezone.utc).isoformat()
    persistent = {"5:Netherlands": {"fetched_at": fresh, "policy": {"currency": "EUR"}}}
    pol = sp.get_policy(5, "Netherlands", token="t", run_cache={}, persistent=persistent, ttl_days=30)
    assert pol == {"currency": "EUR"}


def test_get_policy_refetches_when_stale_and_caches_result(monkeypatch):
    calls = []
    monkeypatch.setattr(sp, "_fetch_and_normalize",
                        lambda *a, **k: (calls.append(1), {"currency": "EUR", "tiers": []})[1])
    stale = "2000-01-01T00:00:00+00:00"
    persistent = {"5:Netherlands": {"fetched_at": stale, "policy": {"old": True}}}
    run_cache = {}
    pol = sp.get_policy(5, "Netherlands", token="t", run_cache=run_cache, persistent=persistent, ttl_days=30)
    assert pol["currency"] == "EUR" and len(calls) == 1
    # Re-fetched result is written back to the persistent cache with a fresh stamp.
    assert persistent["5:Netherlands"]["policy"]["currency"] == "EUR"
    # And the run cache short-circuits a second lookup in the same run.
    sp.get_policy(5, "Netherlands", token="t", run_cache=run_cache, persistent=persistent, ttl_days=30)
    assert len(calls) == 1


def test_get_policy_caches_none_result(monkeypatch):
    # A failed lookup (None) is cached too, so we don't hammer it within the run.
    calls = []
    monkeypatch.setattr(sp, "_fetch_and_normalize", lambda *a, **k: (calls.append(1), None)[1])
    run_cache, persistent = {}, {}
    assert sp.get_policy(5, "Netherlands", token="t", run_cache=run_cache, persistent=persistent) is None
    sp.get_policy(5, "Netherlands", token="t", run_cache=run_cache, persistent=persistent)
    assert len(calls) == 1
