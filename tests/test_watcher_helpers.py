"""State-file round-trip, the alerted-dict loader/pruner, and Deal <-> pending
(de)serialization. The pure pipeline lives in test_core.py."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import watcher
from models import Deal


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


# ── State file + alerted dict ──────────────────────────────────────────────────

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


def test_load_alerted_from_dict():
    # 0.0 is a legitimate placeholder ("alerted but price not tracked yet")
    # and must round-trip. None values are filtered.
    out = watcher._load_alerted({"alerted": {"1": 10.5, "2": 0, "3": None}})
    assert out == {1: 10.5, 2: 0.0}


def test_load_alerted_missing_is_empty():
    assert watcher._load_alerted({}) == {}


def test_prune_keeps_highest_ids_under_cap():
    big = {i: float(i) for i in range(60_000)}
    pruned = watcher._prune_alerted(big)
    assert len(pruned) == watcher._ALERTED_HARD_CAP
    assert min(pruned) >= 60_000 - watcher._ALERTED_HARD_CAP
