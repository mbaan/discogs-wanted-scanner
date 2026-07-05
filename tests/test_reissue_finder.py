"""reissue_finder — the pure candidate-selection logic (no network)."""
import reissue_finder


def _ver(id_, country, owned, *, fmt="LP", major=("Vinyl",)):
    return {"id": id_, "country": country, "released": "2015", "format": fmt,
            "major_formats": list(major), "stats": {"community": {"in_collection": owned}}}


def test_eu_lp_candidates_filters_and_ranks_by_ownership():
    versions = [
        _ver(10, "US", 500),                               # non-EU → out
        _ver(11, "Europe", 100),
        _ver(12, "Germany", 300),
        _ver(13, "UK", 999),                               # UK-only (Brexit) → out
        _ver(14, "Europe", 50, fmt="CD", major=("CD",)),   # not vinyl → out
        _ver(99, "Europe", 800),                           # self → excluded
    ]
    ids = [c["id"] for c in reissue_finder._eu_lp_candidates(versions, exclude_id=99)]
    assert ids == [12, 11]     # EU vinyl only, most-owned first


def test_eu_lp_accepts_multiregion_euro_pressings():
    versions = [_ver(1, "UK & Europe", 200), _ver(2, "Benelux", 10)]
    ids = [c["id"] for c in reissue_finder._eu_lp_candidates(versions, exclude_id=0)]
    assert ids == [1, 2]


def test_pick_best_requires_min_supply():
    details = [{"id": 1, "num_for_sale": 5, "rating_avg": 4.5, "rating_count": 100}]
    assert reissue_finder._pick_best(details, min_supply=12, rating_min_count=15) is None


def test_pick_best_picks_most_available_and_flags_low_rating():
    details = [
        {"id": 1, "num_for_sale": 30, "rating_avg": 3.2, "rating_count": 40},
        {"id": 2, "num_for_sale": 15, "rating_avg": 4.6, "rating_count": 200},
    ]
    best = reissue_finder._pick_best(details, min_supply=12, rating_min_count=15)
    assert best["id"] == 1 and best["low_rating"] is True


def test_pick_best_low_rating_ignored_when_too_few_votes():
    details = [{"id": 1, "num_for_sale": 30, "rating_avg": 3.0, "rating_count": 5}]
    best = reissue_finder._pick_best(details, min_supply=12, rating_min_count=15)
    assert best["low_rating"] is False


def test_find_reissues_reuses_fresh_negative_conclusion_without_network():
    """A fresh cached 'no suggestion' must NOT trigger a lookup (no token needed)."""
    persistent = {"42": {"fetched_at": "2026-07-05T00:00:00+00:00",
                         "conclusion": {"suggestion": None, "raw": None}}}
    out = reissue_finder.find_reissues(
        [{"release_id": 42, "artist": "A", "title": "T"}],
        token="unused", persistent=persistent, run_cache={},
        ttl_days=60, max_lookups=8, min_supply=12, rating_min_count=15)
    assert out == []   # cached negative → nothing suggested, nothing fetched
