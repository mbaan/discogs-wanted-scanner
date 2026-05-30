"""Round-trip the state file, exercise grouping and alerted-dict migration."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import watcher


def _deal(id_, release_id, price, discount_pct=40, currency="EUR", ranked=True):
    return {
        "id": id_, "release_id": release_id,
        "release_artist": "X", "release_title": f"R{release_id}",
        "buyer_price": price, "buyer_currency": currency,
        "shipping_buyer_price": 5.0,
        "landed_price": price + 5.0, "landed_currency": currency,
        "deal_source": "below_condition_median" if ranked else "remote_only",
        "discount_pct": discount_pct if ranked else None,
        "effective_discount": (discount_pct / 100.0) if ranked else None,
        "ranked": ranked, "big_deal": ranked and discount_pct >= 50,
        "listing_url": f"https://x/{id_}",
        "seller_username": f"seller{id_}",
    }


def test_group_by_release_picks_deepest_discount_with_cap():
    """Primary = deepest effective discount (the best deal), even if a shallower
    deal is cheaper in absolute terms. Siblings capped at max_siblings."""
    deals = [
        _deal(1, 100, 20.0, discount_pct=30),
        _deal(2, 100, 15.0, discount_pct=40),   # cheapest, but not the deepest deal
        _deal(3, 100, 30.0, discount_pct=55),    # deepest discount → primary
        _deal(4, 200, 9.0, discount_pct=50),     # solo for release 200
    ]
    grouped = watcher._group_by_release(deals, max_siblings=1)
    by_rel = {g["release_id"]: g for g in grouped}
    # Release 100: deepest discount = #3 → primary; next deepest = #2 → sibling.
    assert by_rel[100]["id"] == 3
    assert [s["seller_username"] for s in by_rel[100]["_siblings"]] == ["seller2"]
    # The cheap-but-shallow #1 is dropped beyond the sibling cap.
    assert by_rel[200]["id"] == 4
    assert by_rel[200]["_siblings"] == []


def test_group_by_release_max_siblings_zero_drops_alternatives():
    deals = [
        _deal(1, 100, 20.0, discount_pct=30),
        _deal(2, 100, 15.0, discount_pct=60),
    ]
    grouped = watcher._group_by_release(deals, max_siblings=0)
    assert len(grouped) == 1
    assert grouped[0]["id"] == 2       # deepest discount wins
    assert grouped[0]["_siblings"] == []


def test_strip_for_pending_drops_datetime():
    d = {"id": 1, "listed_at": datetime.now(tz=timezone.utc), "price": 10.0}
    stripped = watcher._strip_for_pending(d)
    assert "listed_at" not in stripped
    assert stripped["price"] == 10.0


def test_state_atomic_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        watcher._STATE_FILE = Path(td) / "state.json"
        state = {
            "last_run": "2026-05-27T10:00:00+00:00",
            "alerted": {"100": 12.0, "200": 8.5},
            "pending_deals": [],
        }
        watcher._save_state(state)
        loaded = watcher._load_state()
        assert loaded == state


def test_migrate_alerted_from_list():
    out = watcher._migrate_alerted({"alerted": [1, 2, 3]})
    assert out == {1: 0.0, 2: 0.0, 3: 0.0}


def test_migrate_alerted_from_dict():
    # 0.0 is a legitimate placeholder ("alerted but price not tracked yet")
    # and must round-trip. None values are filtered.
    out = watcher._migrate_alerted({"alerted": {"1": 10.5, "2": 0, "3": None}})
    assert out == {1: 10.5, 2: 0.0}


def test_migrate_alerted_from_legacy_seen_shape():
    """Prior plan used state['seen'] = {id: {price, currency, ts}}."""
    legacy = {"seen": {"1": {"price": 12.5, "currency": "EUR", "ts": "2026-01-01"}}}
    out = watcher._migrate_alerted(legacy)
    assert out == {1: 12.5}


def test_prune_keeps_highest_ids_under_cap():
    big = {i: float(i) for i in range(60_000)}
    pruned = watcher._prune_alerted(big)
    assert len(pruned) == watcher._ALERTED_HARD_CAP
    assert min(pruned) >= 60_000 - watcher._ALERTED_HARD_CAP


def test_deal_sort_key_deepest_discount_first_unranked_last():
    a = _deal(1, 1, 10.0, discount_pct=40)
    b = _deal(2, 2, 10.0, discount_pct=55)   # deepest → first
    c = _deal(3, 3, 10.0, ranked=False)      # solo/flagged, no discount → last
    ordered = sorted([a, b, c], key=watcher._deal_sort_key)
    assert [d["id"] for d in ordered] == [2, 1, 3]


def test_deal_sort_key_alphabetical_tie_break():
    a = _deal(1, 1, 10.0, discount_pct=40); a["release_artist"] = "Zoe Keating"
    b = _deal(2, 2, 10.0, discount_pct=40); b["release_artist"] = "Aphex Twin"
    ordered = sorted([a, b], key=watcher._deal_sort_key)
    # Equal discount → alphabetical by artist.
    assert [d["id"] for d in ordered] == [2, 1]


# ── Price history (all-time-low signal) ───────────────────────────────────────

def _qlisting(release_id, condition, buyer_price, shipping=5.0):
    return {
        "id": release_id * 10,
        "release_id": release_id,
        "media_condition": condition,
        "buyer_price": buyer_price,
        "buyer_currency": "EUR",
        "shipping_buyer_price": shipping,
    }


def test_record_price_history_creates_entry():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    history = {}
    watcher._record_price_history(history, [_qlisting(100, "Very Good Plus (VG+)", 20.0)], now)
    assert history["100:Very Good Plus (VG+)"][0] == {"d": "2026-05-28", "p": 25.0, "c": "EUR"}


def test_record_price_history_upserts_lower_price_same_day():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    history = {}
    listings = [
        _qlisting(100, "Very Good Plus (VG+)", 20.0),  # 25.0 landed
        _qlisting(100, "Very Good Plus (VG+)", 15.0),  # 20.0 landed — cheaper
    ]
    watcher._record_price_history(history, listings, now)
    key = "100:Very Good Plus (VG+)"
    assert len(history[key]) == 1
    assert history[key][0]["p"] == 20.0


def test_record_price_history_does_not_replace_lower_existing():
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    history = {"100:Very Good Plus (VG+)": [{"d": "2026-05-28", "p": 18.0, "c": "EUR"}]}
    watcher._record_price_history(history, [_qlisting(100, "Very Good Plus (VG+)", 20.0)], now)
    assert history["100:Very Good Plus (VG+)"][0]["p"] == 18.0


def test_prune_price_history_removes_old_entries():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    history = {
        "100:Very Good Plus (VG+)": [
            {"d": "2026-02-25", "p": 15.0, "c": "EUR"},  # 92 days ago → pruned
            {"d": "2026-05-01", "p": 18.0, "c": "EUR"},  # 27 days ago → kept
        ]
    }
    watcher._prune_price_history(history, now, days=90)
    key = "100:Very Good Plus (VG+)"
    assert [e["d"] for e in history[key]] == ["2026-05-01"]


def test_prune_price_history_removes_empty_keys():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    history = {"100:Very Good Plus (VG+)": [{"d": "2026-02-01", "p": 15.0, "c": "EUR"}]}
    watcher._prune_price_history(history, now, days=90)
    assert "100:Very Good Plus (VG+)" not in history


def test_annotate_historical_floor_badges_new_low():
    deal = {"release_id": 100, "media_condition": "Very Good Plus (VG+)", "landed_price": 18.0}
    history = {
        "100:Very Good Plus (VG+)": [
            {"d": "2026-05-01", "p": 22.0, "c": "EUR"},
            {"d": "2026-05-05", "p": 24.0, "c": "EUR"},
            {"d": "2026-05-10", "p": 25.0, "c": "EUR"},
        ]
    }
    watcher._annotate_historical_floor([deal], history, min_points=3)
    assert deal["historical_floor_pct"] == 18  # int((1 - 18/22) * 100)
    assert deal["historical_floor_value"] == 22.0
    assert deal["historical_data_points"] == 3


def test_annotate_historical_floor_no_badge_above_floor():
    deal = {"release_id": 100, "media_condition": "Very Good Plus (VG+)", "landed_price": 26.0}
    history = {
        "100:Very Good Plus (VG+)": [
            {"d": "2026-05-01", "p": 22.0, "c": "EUR"},
            {"d": "2026-05-05", "p": 24.0, "c": "EUR"},
            {"d": "2026-05-10", "p": 25.0, "c": "EUR"},
        ]
    }
    watcher._annotate_historical_floor([deal], history, min_points=3)
    assert "historical_floor_pct" not in deal


def test_annotate_historical_floor_skips_insufficient_history():
    deal = {"release_id": 100, "media_condition": "Very Good Plus (VG+)", "landed_price": 10.0}
    history = {"100:Very Good Plus (VG+)": [{"d": "2026-05-01", "p": 22.0, "c": "EUR"}]}
    watcher._annotate_historical_floor([deal], history, min_points=3)
    assert "historical_floor_pct" not in deal
