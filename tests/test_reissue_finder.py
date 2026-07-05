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


# ── adoption flag + album dedupe ──────────────────────────────────────────────

def _pos(rid, sug_id, nfs, artist="The Chemical Brothers", title="Further"):
    return {str(rid): {"fetched_at": "2026-07-05T00:00:00+00:00",
                       "conclusion": {"suggestion": {"id": sug_id, "num_for_sale": nfs}, "raw": None},
                       "artist": artist, "title": title}}


def test_already_added_flag_set_when_suggested_id_on_wantlist():
    out = reissue_finder.find_reissues(
        [{"release_id": 1, "artist": "A", "title": "T"}],
        token="x", persistent=_pos(1, 999, 30, "A", "T"), run_cache={},
        ttl_days=60, max_lookups=8, min_supply=12, rating_min_count=15,
        wantlist_ids={999})
    assert out[0]["already_added"] is True


def test_already_added_flag_false_when_absent():
    out = reissue_finder.find_reissues(
        [{"release_id": 1, "artist": "A", "title": "T"}],
        token="x", persistent=_pos(1, 999, 30, "A", "T"), run_cache={},
        ttl_days=60, max_lookups=8, min_supply=12, rating_min_count=15,
        wantlist_ids={55555})
    assert out[0]["already_added"] is False


def test_dedupe_prefers_adopted_over_more_available():
    subs = [
        {"artist": "CB", "title": "Further", "already_added": False, "suggestion": {"id": 1, "num_for_sale": 78}},
        {"artist": "CB", "title": "Further", "already_added": True, "suggestion": {"id": 2, "num_for_sale": 31}},
    ]
    out = reissue_finder._dedupe_by_album(subs)
    assert len(out) == 1 and out[0]["suggestion"]["id"] == 2 and out[0]["already_added"] is True


def test_dedupe_keeps_most_available_when_none_adopted():
    subs = [
        {"artist": "CB", "title": "Further", "already_added": False, "suggestion": {"id": 1, "num_for_sale": 31}},
        {"artist": "CB", "title": "Further", "already_added": False, "suggestion": {"id": 2, "num_for_sale": 78}},
    ]
    out = reissue_finder._dedupe_by_album(subs)
    assert len(out) == 1 and out[0]["suggestion"]["id"] == 2


def test_find_reissues_reproduces_and_fixes_duplicate_further(monkeypatch):
    """Marco's bug: two 'Further' pressings on the wantlist (original + the added
    reissue) each generated a swap → duplicate rows, one pitching a swap AWAY from
    the good copy. Now: one row, acknowledged as already on the wantlist."""
    persistent = {**_pos(9648055, 9273628, 78), **_pos(9273628, 2316042, 31)}
    problem = [
        {"release_id": 9648055, "artist": "The Chemical Brothers", "title": "Further"},
        {"release_id": 9273628, "artist": "The Chemical Brothers", "title": "Further"},
    ]
    out = reissue_finder.find_reissues(
        problem, token="x", persistent=persistent, run_cache={},
        ttl_days=60, max_lookups=8, min_supply=12, rating_min_count=15,
        wantlist_ids={9648055, 9273628})   # Marco added 9273628
    assert len(out) == 1
    assert out[0]["suggestion"]["id"] == 9273628 and out[0]["already_added"] is True
