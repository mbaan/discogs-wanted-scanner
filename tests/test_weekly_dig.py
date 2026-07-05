"""The Weekly Dig — bucket reconstruction, cadence gate, problem selection."""
from datetime import datetime, timezone

import weekly_dig

NM = "Near Mint (NM or M-)"
M = "Mint (M)"
CFG = {"sold_price_min_points": 5, "shipping_allowance": 7.0}


def _sell(rid, by_cond, ccy="EUR"):
    """by_cond: {condition: (median, count)}."""
    return {str(rid): {"fetched_at": "2026-07-01T00:00:00+00:00", "stats": {
        "currency": ccy,
        "by_condition": {c: {"median": med, "count": cnt, "prices": [med]}
                         for c, (med, cnt) in by_cond.items()}}}}


# ── compute() bucketing + gap math ────────────────────────────────────────────

def test_never_seen_when_absent_from_price_history():
    wl = [{"release_id": 1, "artist": "A", "title": "T", "year": 2000}]
    stats = weekly_dig.compute(wl, {}, {}, CFG)
    assert stats.total_wantlist == 1 and stats.seen_count == 0
    assert [w["release_id"] for w in stats.never_seen] == [1]


def test_seen_but_never_a_deal_reports_gap():
    # cheapest landed 110, sold median 89 → benchmark 96 → never a deal, +24%
    wl = [{"release_id": 1, "artist": "Underworld", "title": "Beaucoup Fish"}]
    ph = {f"1:{NM}": [{"d": "2026-07-01", "p": 114.0, "c": "EUR"},
                      {"d": "2026-07-02", "p": 110.0, "c": "EUR"}]}
    stats = weekly_dig.compute(wl, ph, _sell(1, {NM: (89.0, 10)}), CFG)
    assert stats.gets_deal == 0 and len(stats.seen_never_deal) == 1
    nd = stats.seen_never_deal[0]
    assert nd.cheapest_seen == 110.0                      # min across days
    assert nd.gap_pct == round((110 / 89 - 1) * 100)      # +24
    assert nd.gap_eur == round(110 - 96.0, 2)


def test_gets_deal_when_cheapest_at_or_below_benchmark():
    wl = [{"release_id": 1}]
    ph = {f"1:{NM}": [{"d": "2026-07-01", "p": 95.0, "c": "EUR"}]}  # <= 89+7
    stats = weekly_dig.compute(wl, ph, _sell(1, {NM: (89.0, 10)}), CFG)
    assert stats.gets_deal == 1 and stats.seen_never_deal == []


def test_no_benchmark_when_too_few_sold_points():
    wl = [{"release_id": 1}]
    ph = {f"1:{NM}": [{"d": "2026-07-01", "p": 110.0, "c": "EUR"}]}
    stats = weekly_dig.compute(wl, ph, _sell(1, {NM: (89.0, 3)}), CFG)  # 3 < 5
    assert stats.no_benchmark == 1 and stats.seen_count == 1
    assert stats.seen_never_deal == []


def test_currency_mismatch_is_not_judged():
    wl = [{"release_id": 1}]
    ph = {f"1:{NM}": [{"d": "2026-07-01", "p": 50.0, "c": "USD"}]}
    stats = weekly_dig.compute(wl, ph, _sell(1, {NM: (89.0, 10)}, ccy="EUR"), CFG)
    assert stats.no_benchmark == 1


def test_closest_condition_wins():
    # Mint is 300% over, NM only 10% over → report NM (the closest shot).
    wl = [{"release_id": 1}]
    ph = {f"1:{NM}": [{"d": "2026-07-01", "p": 98.0, "c": "EUR"}],
          f"1:{M}": [{"d": "2026-07-01", "p": 360.0, "c": "EUR"}]}
    stats = weekly_dig.compute(wl, ph, _sell(1, {NM: (89.0, 10), M: (120.0, 8)}), CFG)
    assert len(stats.seen_never_deal) == 1
    assert stats.seen_never_deal[0].condition == NM


# ── should_send() cadence ─────────────────────────────────────────────────────

def _sun(day, hour):   # a July-2026 Sunday at UTC hour
    return datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc)


def test_should_send_fires_sunday_evening():
    assert weekly_dig.should_send(_sun(5, 18), 6, 18, None) is True


def test_should_send_wrong_day():
    sat = datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc)
    assert weekly_dig.should_send(sat, 6, 18, None) is False


def test_should_send_before_hour():
    assert weekly_dig.should_send(_sun(5, 17), 6, 18, None) is False


def test_should_send_suppressed_same_week():
    last = _sun(5, 18).isoformat()
    assert weekly_dig.should_send(_sun(5, 19), 6, 18, last) is False


def test_should_send_next_week_ok():
    last = _sun(5, 18).isoformat()
    assert weekly_dig.should_send(_sun(12, 18), 6, 18, last) is True


# ── problem_releases() selection ──────────────────────────────────────────────

def test_problem_releases_never_seen_first_then_overpriced():
    stats = weekly_dig.DigStats(
        total_wantlist=3, seen_count=2, gets_deal=0,
        never_seen=[{"release_id": 1, "artist": "A", "title": "T"}],
        seen_never_deal=[
            # (rid, artist, title, cond, cheapest, median, benchmark, gap_eur, gap_pct, ccy)
            weekly_dig.NeverDeal(2, "B", "U", NM, 200.0, 50.0, 57.0, 143.0, 300, "EUR"),  # 4.0x
            weekly_dig.NeverDeal(3, "C", "V", NM, 55.0, 50.0, 57.0, -2.0, 10, "EUR"),     # 1.1x
        ],
        no_benchmark=0)
    ids = [p["release_id"] for p in weekly_dig.problem_releases(stats, 1.8)]
    assert ids[0] == 1          # never-seen first
    assert 2 in ids and 3 not in ids
