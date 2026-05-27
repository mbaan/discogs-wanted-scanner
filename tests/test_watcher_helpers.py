"""Round-trip the state file, exercise grouping and alerted-dict migration."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import watcher


def _deal(id_, release_id, price, certainty="HIGH", currency="EUR"):
    return {
        "id": id_, "release_id": release_id,
        "release_artist": "X", "release_title": f"R{release_id}",
        "buyer_price": price, "buyer_currency": currency,
        "shipping_buyer_price": 5.0,
        "landed_price": price + 5.0, "landed_currency": currency,
        "certainty_label": certainty, "deal_source": "remote_flag",
        "discount_pct": 40, "listing_url": f"https://x/{id_}",
        "seller_username": f"seller{id_}",
    }


def test_group_by_release_picks_cheapest_landed_with_cap():
    """Primary = lowest landed price (what matters to the buyer), even if
    another listing has a higher discount %. Siblings capped at max_siblings."""
    deals = [
        _deal(1, 100, 20.0, "HIGH"),    # landed_price 25.0
        _deal(2, 100, 15.0, "MEDIUM"),  # landed_price 20.0 ← cheapest
        _deal(3, 100, 30.0, "LOW"),     # landed_price 35.0
        _deal(4, 200, 9.0, "HIGH"),     # landed_price 14.0 — solo for release 200
    ]
    # Set discount % such that id 3 has the HIGHEST discount — to confirm
    # landed wins over discount % in the sort.
    deals[0]["discount_pct"] = 30
    deals[1]["discount_pct"] = 40
    deals[2]["discount_pct"] = 99   # would win on discount %, but it's the most expensive
    deals[3]["discount_pct"] = 50

    grouped = watcher._group_by_release(deals, max_siblings=1)
    by_rel = {g["release_id"]: g for g in grouped}
    # Release 100: cheapest landed = #2 → primary. Next cheapest = #1 → sibling.
    assert by_rel[100]["id"] == 2
    assert [s["seller_username"] for s in by_rel[100]["_siblings"]] == ["seller1"]
    # The high-discount but expensive #3 should NOT be primary or sibling.
    assert by_rel[200]["id"] == 4
    assert by_rel[200]["_siblings"] == []


def test_group_by_release_max_siblings_zero_drops_alternatives():
    deals = [
        _deal(1, 100, 20.0, "HIGH"),
        _deal(2, 100, 15.0, "MEDIUM"),
    ]
    deals[0]["discount_pct"] = 30
    deals[1]["discount_pct"] = 60
    grouped = watcher._group_by_release(deals, max_siblings=0)
    assert len(grouped) == 1
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


def test_deal_sort_key_alphabetical_by_artist_then_title():
    a = _deal(1, 1, 10.0, "MEDIUM"); a["release_artist"] = "Zoe Keating"; a["release_title"] = "Into the Trees"
    b = _deal(2, 2, 10.0, "HIGH");   b["release_artist"] = "Aphex Twin";  b["release_title"] = "Selected Ambient Works"
    c = _deal(3, 3, 10.0, "HIGH");   c["release_artist"] = "Aphex Twin";  c["release_title"] = "Drukqs"
    ordered = sorted([a, b, c], key=watcher._deal_sort_key)
    # Aphex Twin (Drukqs, then Selected Ambient Works), then Zoe Keating
    assert [d["id"] for d in ordered] == [3, 2, 1]
