"""Store round-trip + watcher-seam tests. No network, no mocks of SQLite — real
:memory:/temp-file connections. Mirrors tests/test_watcher_helpers style (stdlib
tempfile/Path)."""
import tempfile
from pathlib import Path

from store import Store


def test_open_creates_schema_and_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "state.db"
        with Store.open(path) as s:
            tables = {
                r[0] for r in s.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        # Re-open over the same file: CREATE TABLE IF NOT EXISTS must not error.
        with Store.open(path) as s2:
            tables2 = {
                r[0] for r in s2.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    expected = {
        "price_history", "sell_history", "shipping_policies", "alerted",
        "pushed", "pending_deals", "meta",
    }
    assert expected <= tables
    assert expected <= tables2


def test_open_enables_wal_on_file_db():
    with tempfile.TemporaryDirectory() as td:
        with Store.open(Path(td) / "state.db") as s:
            mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_alerted_roundtrip_preserves_zero_placeholder():
    with Store.open(":memory:") as s:
        s.save_alerted({1: 10.5, 2: 0.0})
        assert s.load_alerted() == {1: 10.5, 2: 0.0}


def test_alerted_save_replaces_not_appends():
    with Store.open(":memory:") as s:
        s.save_alerted({1: 10.5, 2: 0.0})
        s.save_alerted({3: 5.0})
        assert s.load_alerted() == {3: 5.0}


def test_alerted_cap_keeps_highest_ids():
    with Store.open(":memory:") as s:
        s.save_alerted({i: float(i) for i in range(60_000)})
        loaded = s.load_alerted()
    assert len(loaded) == 50_000
    assert min(loaded) >= 60_000 - 50_000


# ── pushed (push fast-lane dedup set; mirrors alerted) ─────────────────────────

def test_pushed_round_trips():
    with Store.open(":memory:") as s:
        s.save_pushed({1: 10.5, 2: 0.0, 3: 25.0})
        assert s.load_pushed() == {1: 10.5, 2: 0.0, 3: 25.0}


def test_pushed_empty_by_default():
    with Store.open(":memory:") as s:
        assert s.load_pushed() == {}


def test_pushed_independent_of_alerted():
    # The two dedup sets must not share rows.
    with Store.open(":memory:") as s:
        s.save_alerted({1: 5.0})
        s.save_pushed({2: 9.0})
        assert s.load_alerted() == {1: 5.0}
        assert s.load_pushed() == {2: 9.0}


def test_save_pushed_keeps_highest_ids_at_cap(monkeypatch):
    # Lower the cap so the prune is observable on a tiny set. save_pushed reuses
    # the alerted prune, which reads the module-level _ALERTED_HARD_CAP.
    import store
    monkeypatch.setattr(store, "_PUSHED_HARD_CAP", 2, raising=False)
    monkeypatch.setattr(store, "_ALERTED_HARD_CAP", 2, raising=False)
    with Store.open(":memory:") as s:
        s.save_pushed({1: 1.0, 5: 5.0, 9: 9.0})
        kept = s.load_pushed()
        assert set(kept) == {5, 9}     # two highest IDs survive


def _pending_dicts():
    # Mirrors Deal.to_pending() output: a flat dataclass dict (listed_at dropped).
    from models import Deal
    return [
        Deal(id=i, release_id=100 + i, buyer_price=float(i), landed_price=float(i) + 5.0,
             deal_source="below_condition_median", listing_url=f"https://x/{i}").to_pending()
        for i in range(3)
    ]


def test_pending_roundtrips_in_insertion_order():
    from models import Deal
    with Store.open(":memory:") as s:
        s.save_pending(_pending_dicts())
        loaded = s.load_pending()
    assert [d["id"] for d in loaded] == [0, 1, 2]
    # Codec still round-trips through Deal.from_pending/to_pending.
    assert Deal.from_pending(loaded[1]).id == 1


def test_pending_save_replaces_queue():
    with Store.open(":memory:") as s:
        s.save_pending(_pending_dicts())
        s.save_pending([{"id": 99, "landed_price": 1.0}])
        loaded = s.load_pending()
    assert [d["id"] for d in loaded] == [99]


def test_pending_cap_keeps_newest():
    with Store.open(":memory:") as s:
        s.save_pending([{"id": i, "landed_price": float(i)} for i in range(600)])
        loaded = s.load_pending()
    assert len(loaded) == 500
    assert [d["id"] for d in loaded] == list(range(100, 600))


def test_shipping_policies_roundtrip_including_null():
    cache = {
        "111:NL": {"fetched_at": "2026-06-01T00:00:00+00:00",
                   "policy": {"currency": "EUR", "tiers": [[0, 5.0]]}},
        "222:NL": {"fetched_at": "2026-06-02T00:00:00+00:00", "policy": None},
    }
    with Store.open(":memory:") as s:
        s.save_shipping_policies(cache)
        assert s.load_shipping_policies() == cache


def test_shipping_policies_save_replaces():
    with Store.open(":memory:") as s:
        s.save_shipping_policies({"a:NL": {"fetched_at": "t", "policy": None}})
        s.save_shipping_policies({"b:NL": {"fetched_at": "u", "policy": None}})
        assert set(s.load_shipping_policies()) == {"b:NL"}


def test_sell_history_roundtrip_including_null_stats():
    cache = {
        "100": {"fetched_at": "2026-06-01T00:00:00+00:00",
                "stats": {"currency": "EUR", "by_condition": {"Mint (M)": {"median": 12.0}}}},
        "200": {"fetched_at": "2026-06-02T00:00:00+00:00", "stats": None},
    }
    with Store.open(":memory:") as s:
        s.save_sell_history(cache)
        assert s.load_sell_history() == cache


def test_sell_history_save_replaces():
    with Store.open(":memory:") as s:
        s.save_sell_history({"100": {"fetched_at": "t", "stats": None}})
        s.save_sell_history({"200": {"fetched_at": "u", "stats": None}})
        assert set(s.load_sell_history()) == {"200"}


def test_price_history_roundtrip_reconstructs_identical_dict():
    ph = {
        "10168874:Mint (M)": [
            {"d": "2026-06-01", "p": 12.5, "c": "EUR"},
            {"d": "2026-06-02", "p": 11.0, "c": "EUR"},
        ],
        "555:Near Mint (NM or M-)": [{"d": "2026-06-01", "p": 8.0, "c": "GBP"}],
    }
    with Store.open(":memory:") as s:
        s.save_price_history(ph)
        assert s.load_price_history() == ph


def test_price_history_currency_may_be_none():
    ph = {"42:Very Good Plus (VG+)": [{"d": "2026-06-01", "p": 5.0, "c": None}]}
    with Store.open(":memory:") as s:
        s.save_price_history(ph)
        assert s.load_price_history() == ph


def test_price_history_save_mirrors_prune_dropping_old_days():
    with Store.open(":memory:") as s:
        s.save_price_history({"100:Mint (M)": [
            {"d": "2026-05-01", "p": 9.0, "c": "EUR"},
            {"d": "2026-06-01", "p": 8.0, "c": "EUR"},
        ]})
        # Simulate core.prune_price_history dropping the old day in the dict,
        # then re-saving: the stale row must be gone, not left behind.
        s.save_price_history({"100:Mint (M)": [{"d": "2026-06-01", "p": 8.0, "c": "EUR"}]})
        loaded = s.load_price_history()
    assert loaded == {"100:Mint (M)": [{"d": "2026-06-01", "p": 8.0, "c": "EUR"}]}


def test_price_history_save_mirrors_prune_dropping_emptied_key():
    with Store.open(":memory:") as s:
        s.save_price_history({"100:Mint (M)": [{"d": "2026-05-01", "p": 9.0, "c": "EUR"}]})
        s.save_price_history({})  # core.prune_price_history deleted the emptied key
        assert s.load_price_history() == {}


def test_meta_roundtrip_and_none_deletes():
    with Store.open(":memory:") as s:
        s.set_meta("last_run", "2026-06-04T10:00:00+00:00")
        assert s.get_meta("last_run") == "2026-06-04T10:00:00+00:00"
        assert s.get_meta("never_set") is None
        s.set_meta("last_run", None)  # delete the row
        assert s.get_meta("last_run") is None


def test_meta_set_none_on_absent_key_is_noop():
    with Store.open(":memory:") as s:
        s.set_meta("cookie_alert_sent_at", None)  # mirrors state.pop(..., None)
        assert s.get_meta("cookie_alert_sent_at") is None


def test_emails_today_roundtrip_as_json():
    with Store.open(":memory:") as s:
        s.save_emails_today({"date": "2026-06-04", "count": 2})
        assert s.load_emails_today() == {"date": "2026-06-04", "count": 2}


def test_emails_today_default_empty_dict():
    with Store.open(":memory:") as s:
        assert s.load_emails_today() == {}


def test_corrupt_db_fails_open_to_memory():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        db_path.write_bytes(b"this is not a sqlite database, it is garbage bytes")
        # Must not raise; returns a usable in-memory store, nothing persisted.
        with Store.open(db_path) as s:
            s.save_alerted({1: 5.0})
            assert s.load_alerted() == {1: 5.0}
        # Corrupt file quarantined (renamed aside), never deleted.
        assert not db_path.exists()
        assert len(list(Path(td).glob("state.db.corrupt-*"))) == 1


def test_watcher_exposes_store_constants():
    import watcher
    assert watcher._STATE_DB == watcher._DIR / "state.db"
    assert watcher._REPORT_FILE == watcher._DIR / "report.html"


def test_parse_args_defaults_full_false():
    import watcher
    args = watcher._parse_args([])
    assert args.full is False
    assert watcher._parse_args(["--full"]).full is True


def test_full_threads_empty_prev_alerted_but_keeps_real_dict():
    """--full: build_digest sees prev_alerted={} (every deal surfaces) while the
    real alerted dict is still loaded + updated for subsequent normal runs."""
    import watcher
    real_alerted = {123: 50.0}
    # Default: dedup applied (the loaded dict flows into build_digest).
    assert watcher._prev_alerted_for(watcher._parse_args([]), real_alerted) is real_alerted
    # --full: dedup bypassed (empty dict threaded in).
    assert watcher._prev_alerted_for(watcher._parse_args(["--full"]), real_alerted) == {}


def test_full_forces_sold_ttl_zero():
    """--full forces the sold-history TTL to 0 so every release refetches; default
    keeps the configured TTL."""
    import watcher
    assert watcher._sold_ttl_for(watcher._parse_args(["--full"]), 30) == 0
    assert watcher._sold_ttl_for(watcher._parse_args([]), 30) == 30


def test_sold_ttl_zero_treats_any_cache_as_stale():
    """Proof the TTL-0 bypass works without network: a just-fetched timestamp is
    treated stale under ttl_days=0."""
    import sold_prices
    from datetime import datetime, timezone
    fresh_ts = datetime.now(timezone.utc).isoformat()
    assert sold_prices._fresh(fresh_ts, 0) is False
    assert sold_prices._fresh(fresh_ts, 30) is True


def test_write_report_renders_deal_headline(tmp_path):
    import watcher
    from datetime import datetime, timezone
    from models import Deal
    deals = [Deal(
        id=1, release_id=100, release_artist="X", release_title="R100",
        buyer_price=10.0, landed_price=15.0, landed_currency="EUR",
        deal_source="below_condition_median", discount_pct=40, ranked=True,
        listing_url="https://x/1", seller_username="seller1",
    )]
    out = tmp_path / "report.html"
    watcher._write_report(out, deals, datetime(2026, 6, 4, 10, tzinfo=timezone.utc),
                          extra_count=0, session_days_left=None, scan_counts=None)
    html = out.read_text()
    assert out.exists()
    assert "find" in html  # the _build_html headline ("N find(s) on your wantlist")


def test_write_report_empty_list_writes_zero_placeholder(tmp_path):
    import watcher
    from datetime import datetime, timezone
    out = tmp_path / "report.html"
    watcher._write_report(out, [], datetime(2026, 6, 4, 10, tzinfo=timezone.utc),
                          extra_count=0, session_days_left=None, scan_counts=None)
    assert out.exists()
    assert "0 find" in out.read_text()  # _build_html renders n=0


def test_write_report_is_atomic_no_tmp_left(tmp_path):
    import watcher
    from datetime import datetime, timezone
    out = tmp_path / "report.html"
    watcher._write_report(out, [], datetime(2026, 6, 4, 10, tzinfo=timezone.utc),
                          extra_count=0, session_days_left=None, scan_counts=None)
    assert list(tmp_path.glob("*.tmp")) == []


def test_report_path_default_and_override(monkeypatch, tmp_path):
    import watcher
    monkeypatch.setattr(watcher, "_REPORT_FILE", tmp_path / "report.html")
    monkeypatch.delenv("REPORT_HTML", raising=False)
    assert watcher._report_path() == tmp_path / "report.html"
    monkeypatch.setenv("REPORT_HTML", str(tmp_path / "custom.html"))
    assert str(watcher._report_path()) == str(tmp_path / "custom.html")
