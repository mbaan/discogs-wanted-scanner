"""Deal <-> pending (de)serialization + the alerted accessors (now Store-backed).
The pure pipeline lives in test_core.py."""
import json
import os
from datetime import datetime, timezone

import shipping_policy
import watcher
from models import Deal, Listing


def _deal(id_, price, currency="EUR"):
    return Deal(
        id=id_, release_id=100, release_artist="X", release_title="R100",
        buyer_price=price, buyer_currency=currency,
        shipping_buyer_price=5.0, landed_price=price + 5.0, landed_currency=currency,
        deal_source="below_condition_median", discount_pct=40, effective_discount=0.40,
        ranked=True, listing_url=f"https://x/{id_}", seller_username=f"seller{id_}",
    )


# ── Deal <-> pending (state.json) ──────────────────────────────────────────────

def test_to_pending_drops_datetime_and_roundtrips():
    d = _deal(1, 10.0)
    d.listed_at = datetime.now(tz=timezone.utc)
    pending = d.to_pending()
    assert "listed_at" not in pending
    json.dumps(pending)  # must be JSON-serializable for state.json
    restored = Deal.from_pending(pending)
    assert restored.id == 1
    assert restored.landed_price == 15.0
    assert restored.listed_at is None


def test_pending_roundtrips_sold_fields():
    d = _deal(1, 10.0)
    d.sold_median_value = 16.27
    d.sold_median_currency = "EUR"
    d.sold_low_value = 4.99
    d.sold_high_value = 40.0
    d.sold_last_date = "2026-02-03"
    d.sold_data_points = 8
    restored = Deal.from_pending(d.to_pending())
    assert restored.sold_median_value == 16.27
    assert restored.sold_median_currency == "EUR"
    assert restored.sold_low_value == 4.99
    assert restored.sold_high_value == 40.0
    assert restored.sold_last_date == "2026-02-03"
    assert restored.sold_data_points == 8


def test_from_pending_tolerates_legacy_keys():
    # A pre-model flat dict with an unknown key must not crash; unknown dropped,
    # missing fields defaulted.
    restored = Deal.from_pending({"id": 7, "landed_price": 9.0, "price_usd": 11.0})
    assert restored.id == 7 and restored.landed_price == 9.0


def test_deal_basket_round_trips():
    # A populated basket dict survives to_pending() -> from_pending() unchanged.
    d = _deal(1, 10.0)
    d.basket = {
        "kind": "free_crossing",
        "currency": "EUR",
        "add": [{"release_artist": "B", "release_title": "Y",
                 "media_condition": "Very Good Plus (VG+)", "price": 9.0,
                 "currency": "EUR", "listing_url": "u2"}],
        "new_subtotal": 51.0, "free_min": 50.0,
        "fee_before": 7.0, "fee_after": 0.0, "saving": 7.0,
        "reachable": True, "basis": None,
    }
    restored = Deal.from_pending(d.to_pending())
    assert restored.basket == d.basket
    # Legacy state.json without the key rebuilds with basket is None.
    legacy = Deal.from_pending({"id": 9, "landed_price": 5.0})
    assert legacy.basket is None


# ── alerted via Store (replaces the old watcher state-dict helpers) ────────────

from store import Store


def test_store_alerted_roundtrip():
    with Store.open(":memory:") as s:
        # 0.0 is a legitimate placeholder and must round-trip; None is filtered
        # at the watcher edge, so we only feed non-None here.
        s.save_alerted({1: 10.5, 2: 0.0})
        assert s.load_alerted() == {1: 10.5, 2: 0.0}


def test_store_alerted_missing_is_empty():
    with Store.open(":memory:") as s:
        assert s.load_alerted() == {}


def test_store_prune_keeps_highest_ids_under_cap():
    with Store.open(":memory:") as s:
        s.save_alerted({i: float(i) for i in range(60_000)})
        loaded = s.load_alerted()
    assert len(loaded) == 50_000
    assert min(loaded) >= 60_000 - 50_000


# ── Combine-basket config ───────────────────────────────────────────────────


def _set_required_env(monkeypatch):
    """Minimal required .env so _load_config doesn't abort on missing keys."""
    required = {
        "MY_COUNTRY": "Netherlands", "MIN_MEDIA_CONDITION": "VG+",
        "MIN_SLEEVE_CONDITION": "VG+", "ASKING_DATA_DEAL_THRESHOLD": "0.25",
        "VAT_RATE": "0.21", "PRICE_DROP_THRESHOLD": "0.05",
        "SMTP_HOST": "h", "SMTP_PORT": "587", "SMTP_USER": "u",
        "SMTP_PASS": "p", "SMTP_FROM": "f@x", "SMTP_TO": "t@x",
        "DIGEST_MODE": "daily", "DIGEST_HOUR_UTC": "8",
        "MAX_DEALS_PER_EMAIL": "10", "MAX_EMAILS_PER_DAY": "5",
        "GROUP_BY_RELEASE": "true", "MAX_SIBLINGS_PER_RELEASE": "3",
        "MAX_PAGES_PER_RUN": "30", "SHIPPING_HINTS": "true",
        "EST_GRAMS_PER_VINYL": "250", "MAX_SELLER_PICKS": "5",
        "SHIPPING_POLICY_TTL_DAYS": "30", "PRICE_HISTORY_DAYS": "365",
        "PRICE_HISTORY_MIN_POINTS": "3",
    }
    for k, v in required.items():
        monkeypatch.setenv(k, v)


def test_combine_basket_config_defaults_off(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("COMBINE_BASKET", raising=False)
    monkeypatch.delenv("MAX_BASKET_ITEMS", raising=False)
    monkeypatch.setattr(watcher, "load_dotenv", lambda *a, **k: None)
    cfg = watcher._load_config()
    assert cfg["combine_basket"] is False
    assert cfg["max_basket_items"] == 3   # default applied even when off


def test_combine_basket_config_on_with_custom_cap(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("COMBINE_BASKET", "true")
    monkeypatch.setenv("MAX_BASKET_ITEMS", "2")
    monkeypatch.setattr(watcher, "load_dotenv", lambda *a, **k: None)
    cfg = watcher._load_config()
    assert cfg["combine_basket"] is True
    assert cfg["max_basket_items"] == 2


# ── _annotate_shipping basket assembly ──────────────────────────────────────


def _ship_listing(lid, price):
    return Listing(id=lid, seller_uid=100, price=price, currency="EUR",
                   buyer_price=price, buyer_currency="EUR",
                   media_condition="Very Good Plus (VG+)",
                   sleeve_condition="Very Good Plus (VG+)",
                   release_artist=f"A{lid}", release_title=f"T{lid}",
                   listing_url=f"https://www.discogs.com/sell/item/{lid}")


_FREE_POLICY = {
    "currency": "EUR", "free_shipping": True, "free_min": 50.0,
    "range_type": "weight", "method_name": "Standard",
    "tiers": [(999, 7.0), (0, 12.0)],
}

_BASE_CFG = {
    "discogs_token": "tok", "my_country": "Netherlands", "max_seller_picks": 5,
    "shipping_policy_ttl_days": 30, "est_grams_per_vinyl": 250,
    "max_basket_items": 3,
}


def test_annotate_shipping_sets_basket_when_enabled(monkeypatch):
    monkeypatch.setattr(shipping_policy, "get_policy",
                        lambda *a, **k: _FREE_POLICY)
    deal = Deal(id=1, seller_uid=100, price=38.0, currency="EUR",
                buyer_price=38.0, buyer_currency="EUR",
                release_artist="A1", release_title="T1",
                listing_url="https://www.discogs.com/sell/item/1")
    groups = {100: [_ship_listing(1, 38.0), _ship_listing(2, 9.0), _ship_listing(3, 4.0)]}
    cfg = {**_BASE_CFG, "combine_basket": True}
    watcher._annotate_shipping([deal], groups, cfg, {}, {})
    assert deal.basket is not None
    assert deal.basket["kind"] == "free_crossing"
    assert deal.basket["saving"] == 7.0


def test_annotate_shipping_skips_basket_when_disabled(monkeypatch):
    monkeypatch.setattr(shipping_policy, "get_policy",
                        lambda *a, **k: _FREE_POLICY)
    deal = Deal(id=1, seller_uid=100, price=38.0, currency="EUR",
                buyer_price=38.0, buyer_currency="EUR",
                release_artist="A1", release_title="T1",
                listing_url="https://www.discogs.com/sell/item/1")
    groups = {100: [_ship_listing(1, 38.0), _ship_listing(2, 9.0), _ship_listing(3, 4.0)]}
    cfg = {**_BASE_CFG, "combine_basket": False}
    watcher._annotate_shipping([deal], groups, cfg, {}, {})
    assert deal.basket is None              # toggle off -> not computed
    assert deal.shipping_hint is not None   # the existing hint still set


# ── _is_push_worthy predicate (push fast-lane) ────────────────────────────────


def _pw_deal(**kw):
    """A push-candidate Deal: ranked, SOLD-validated, with a discount."""
    base = dict(
        id=1, release_artist="Miles Davis", release_title="Kind of Blue",
        ranked=True, low_confidence=False, effective_discount=0.45,
        discount_pct=45,
    )
    base.update(kw)
    return Deal(**base)


def test_push_worthy_strong_sold_deal_selected():
    assert watcher._is_push_worthy(_pw_deal(effective_discount=0.45), 0.30) is True


def test_push_worthy_discount_boundary_at_threshold_selected():
    # 0.30 >= 0.30 → selected.
    assert watcher._is_push_worthy(_pw_deal(effective_discount=0.30), 0.30) is True


def test_push_worthy_discount_just_below_threshold_excluded():
    # 0.29 < 0.30 → excluded (boundary).
    assert watcher._is_push_worthy(_pw_deal(effective_discount=0.29), 0.30) is False


def test_push_worthy_all_time_low_selected_despite_small_discount():
    # historical_floor_value set → qualifies regardless of headline discount.
    d = _pw_deal(effective_discount=0.05, historical_floor_value=40.0)
    assert watcher._is_push_worthy(d, 0.30) is True


def test_push_worthy_low_confidence_excluded():
    d = _pw_deal(effective_discount=0.90, low_confidence=True)
    assert watcher._is_push_worthy(d, 0.30) is False


def test_push_worthy_unranked_excluded():
    # remote_only lone-listing path is ranked=False.
    d = _pw_deal(effective_discount=0.90, ranked=False)
    assert watcher._is_push_worthy(d, 0.30) is False


def test_push_worthy_none_discount_no_floor_excluded():
    # effective_discount None and no floor → cannot clear the gate.
    d = _pw_deal(effective_discount=None, historical_floor_value=None)
    assert watcher._is_push_worthy(d, 0.30) is False


# ── _push_fresh dedup gate (push fast-lane) ───────────────────────────────────


def test_push_fresh_new_listing_is_fresh():
    # Not in pushed yet → fresh.
    assert watcher._push_fresh(cur=20.0, prev=None, threshold=0.10) is True


def test_push_fresh_same_price_already_pushed_is_stale():
    # Already pushed at 20.0, current still 20.0 → not a drop → stale.
    assert watcher._push_fresh(cur=20.0, prev=20.0, threshold=0.10) is False


def test_push_fresh_small_drop_below_threshold_is_stale():
    # Pushed at 20.0, now 19.0 (5% drop) < 10% threshold → stale.
    assert watcher._push_fresh(cur=19.0, prev=20.0, threshold=0.10) is False


def test_push_fresh_drop_exactly_at_threshold_is_stale():
    # Pushed at 20.0, now 18.0 == prev*(1-0.10): the gate mirrors core.build_deals'
    # `cur >= prev*(1-threshold)` exactly, so a drop landing *on* the threshold
    # price is still treated as "near this price" → stale (must drop strictly below).
    assert watcher._push_fresh(cur=18.0, prev=20.0, threshold=0.10) is False


def test_push_fresh_drop_past_threshold_is_fresh():
    # Pushed at 20.0, now 17.0 (15% drop) below the 10% threshold price → fresh again.
    assert watcher._push_fresh(cur=17.0, prev=20.0, threshold=0.10) is True


def test_push_fresh_zero_prev_treated_as_fresh():
    # prev <= 0 is a degenerate placeholder; treat as fresh (mirrors alerted gate's
    # `prev > 0` guard).
    assert watcher._push_fresh(cur=20.0, prev=0.0, threshold=0.10) is True


# ── Push fast-lane config keys (_load_config) ─────────────────────────────────

import pytest


def _clear_push_env(monkeypatch):
    """Ensure no stray push env leaks in from the real shell/.env."""
    monkeypatch.setattr(watcher, "load_dotenv", lambda *a, **k: None)
    for k in ("PUSH_ENABLED", "PUSH_CHANNEL", "NTFY_SERVER", "NTFY_TOPIC",
              "NTFY_TOKEN", "PUSH_MIN_DISCOUNT", "PUSH_PRIORITY", "PUSH_MAX_PER_RUN"):
        monkeypatch.delenv(k, raising=False)


def test_config_push_disabled_by_default(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_push_env(monkeypatch)
    cfg = watcher._load_config()
    assert cfg["push_enabled"] is False


def test_config_push_enabled_applies_silent_defaults(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_push_env(monkeypatch)
    monkeypatch.setenv("PUSH_ENABLED", "true")
    monkeypatch.setenv("NTFY_TOPIC", "discogs-deals-7Kq9x2")
    cfg = watcher._load_config()
    assert cfg["push_enabled"] is True
    assert cfg["push_channel"] == "ntfy"
    assert cfg["ntfy_server"] == "https://ntfy.sh"
    assert cfg["push_min_discount"] == 0.30
    assert cfg["push_max_per_run"] == 10
    assert cfg["ntfy_token"] is None
    assert cfg["push_priority"] is None


def test_config_push_enabled_without_topic_aborts(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_push_env(monkeypatch)
    monkeypatch.setenv("PUSH_ENABLED", "true")  # NTFY_TOPIC deliberately unset
    with pytest.raises(SystemExit):
        watcher._load_config()


def test_config_push_overrides_respected(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_push_env(monkeypatch)
    monkeypatch.setenv("PUSH_ENABLED", "true")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.setenv("NTFY_SERVER", "https://push.example.com")
    monkeypatch.setenv("NTFY_TOKEN", "tok")
    monkeypatch.setenv("PUSH_MIN_DISCOUNT", "0.40")
    monkeypatch.setenv("PUSH_PRIORITY", "high")
    monkeypatch.setenv("PUSH_MAX_PER_RUN", "25")
    cfg = watcher._load_config()
    assert cfg["ntfy_server"] == "https://push.example.com"
    assert cfg["ntfy_token"] == "tok"
    assert cfg["push_min_discount"] == 0.40
    assert cfg["push_priority"] == "high"
    assert cfg["push_max_per_run"] == 25


def test_config_push_non_ntfy_channel_warns_and_disables(monkeypatch):
    _set_required_env(monkeypatch)
    _clear_push_env(monkeypatch)
    monkeypatch.setenv("PUSH_ENABLED", "true")
    monkeypatch.setenv("PUSH_CHANNEL", "telegram")  # unsupported → off, no abort
    cfg = watcher._load_config()
    assert cfg["push_enabled"] is False


# ── Push fast-lane integration in main(): disabled / enabled / --full ─────────

import types

import notifier


class _FakeResp:
    def raise_for_status(self):
        return None


def _push_worthy_deal():
    return Deal(
        id=1, release_id=100, release_artist="Miles Davis", release_title="Kind of Blue",
        media_condition="Near Mint (NM or M-)", buyer_price=45.0, buyer_currency="EUR",
        landed_price=52.0, landed_currency="EUR", discount_pct=45,
        effective_discount=0.45, ranked=True, low_confidence=False,
        listing_url="https://x/1", image_url="https://img/1.jpg",
        deal_source="below_sold_median",
    )


def _run_main_with_stubs(monkeypatch, tmp_path, env_overrides, full=False, should_flush=False):
    """Drive watcher.main() offline: temp DB, stubbed fetch + digest, SMTP spied,
    spied ntfy post. Returns (push_posts, digest_sends) — the list of recorded
    push POSTs and the list of digest-email send() invocations.

    main(args=...) takes the parsed namespace directly, so we feed it via the real
    _parse_args seam rather than monkeypatching argparse. `should_flush` lets a
    test force the digest path (the flush gate is otherwise pipeline-internal).
    """
    _set_required_env(monkeypatch)
    _clear_push_env(monkeypatch)
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)

    # Point the real state DB + report at temp paths.
    monkeypatch.setattr(watcher, "_STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(watcher, "_REPORT_FILE", tmp_path / "report.html")

    # Stub session-health (touches cookies on disk otherwise).
    monkeypatch.setattr(watcher, "_check_session_health", lambda *a, **k: None)

    # Stub the network fetch: one complete page, no cookie problems.
    fetch = types.SimpleNamespace(listings=[], complete=True, cookie_invalid=False)
    monkeypatch.setattr(watcher.shop_api, "fetch_listings", lambda **k: fetch)

    # Stub the pure pipeline to return our push-worthy deal as new_deals.
    result = watcher.core.PipelineResult(
        deals=[_push_worthy_deal()], just_alerted=[],
        seller_groups={}, scanned_releases=1,
    )
    monkeypatch.setattr(watcher.core, "build_digest", lambda *a, **k: result)
    monkeypatch.setattr(watcher.core, "should_flush", lambda *a, **k: should_flush)
    monkeypatch.setattr(watcher.core, "prune_price_history", lambda *a, **k: None)

    # Spy the digest email send (the real method, with SMTP transport no-op'd).
    digest_sends = []
    monkeypatch.setattr(watcher.EmailNotifier, "_send_message", lambda self, msg: None)
    real_send = watcher.EmailNotifier.send
    monkeypatch.setattr(
        watcher.EmailNotifier, "send",
        lambda self, deals, *a, **k: (digest_sends.append(len(deals))
                                      or real_send(self, deals, *a, **k)),
    )

    # Spy the ntfy POST.
    calls = []
    monkeypatch.setattr(
        notifier.requests, "post",
        lambda url, data=None, json=None, headers=None, timeout=None: (
            calls.append({"url": url, "json": json, "headers": headers or {}}) or _FakeResp()
        ),
    )

    args = watcher._parse_args(["--full"] if full else [])
    watcher.main(args)
    return calls, digest_sends


def test_main_push_disabled_fires_nothing(monkeypatch, tmp_path):
    calls, _ = _run_main_with_stubs(monkeypatch, tmp_path, {})  # PUSH_ENABLED unset
    assert calls == []


def test_main_push_enabled_fires_push(monkeypatch, tmp_path):
    calls, _ = _run_main_with_stubs(
        monkeypatch, tmp_path,
        {"PUSH_ENABLED": "true", "NTFY_TOPIC": "t", "PUSH_MIN_DISCOUNT": "0.30"},
    )
    assert len(calls) == 1
    assert calls[0]["json"]["topic"] == "t"      # topic rides in the JSON body now


def test_main_normal_run_only_pushes_new_deals(monkeypatch, tmp_path):
    """A normal run pushes the NEW push-worthy deal once; re-running (same deal,
    same price, now already in the pushed dedup set) pushes nothing more."""
    env = {"PUSH_ENABLED": "true", "NTFY_TOPIC": "t", "PUSH_MIN_DISCOUNT": "0.30"}
    first, _ = _run_main_with_stubs(monkeypatch, tmp_path, env)
    assert len(first) == 1                       # new deal pushes
    second, _ = _run_main_with_stubs(monkeypatch, tmp_path, env)
    assert second == []                          # already pushed → deduped


def test_main_full_run_emails_and_pushes(monkeypatch, tmp_path):
    """--full is a LOUD FULL RUN: the digest email IS sent AND the push DOES fire,
    even for an already-pushed/alerted deal (both dedup sets are bypassed)."""
    env = {"PUSH_ENABLED": "true", "NTFY_TOPIC": "t", "PUSH_MIN_DISCOUNT": "0.30"}
    # Pre-seed the dedup sets so a normal run would suppress both channels.
    db_path = tmp_path / "state.db"
    with Store.open(db_path) as seed:
        seed.save_alerted({1: 45.0})
        seed.save_pushed({1: 45.0})
    calls, digest_sends = _run_main_with_stubs(
        monkeypatch, tmp_path, env, full=True, should_flush=True,
    )
    assert len(calls) == 1                       # push re-fires despite prior push
    assert digest_sends == [1]                   # digest email sent (not suppressed)
