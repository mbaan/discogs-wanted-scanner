"""
Suggest a more-locally-available pressing for a never-hitting wantlist release.

Some wantlist picks never produce a hit because the *specific pressing* is scarce,
foreign, or a collector edition — while a common EU LP of the same master sits
cheap and plentiful. For each problem release this finds the best such swap:

    /releases/{id}      → master_id, community rating, notes (raw, kept for later)
    /masters/{id}/versions → all pressings
    pick EU-pressed LPs, rank by how widely owned (a commonality proxy)
    /releases/{candidate}  → num_for_sale, lowest_price, rating

A candidate is only suggested when it's genuinely more available
(`num_for_sale >= min_supply`). The candidate's SOLD-independent quality signal is
its community rating; a low average is only trusted when enough people rated it
(`rating_count >= rating_min_count`) — a bad rating with few votes is noise, not a
reason to avoid.

PAT-authenticated (same client as `discogs_api`), so no Cloudflare fragility.
Every conclusion — including "no better pressing exists" — is persisted with a
long TTL, so a settled release is never looked up again until the TTL lapses.
Callers cap `max_lookups` per run, so even a cold cache is spread over weeks
rather than hammering Discogs in one go. Fail-open throughout: any miss just
omits that release.
"""

import logging
from datetime import datetime, timezone

from discogs_api import _BASE, _USER_AGENT, _get_with_429_retry
from evaluator import EU_COUNTRIES
from shipping_policy import _fresh

logger = logging.getLogger(__name__)

# EU-pressed = ships within the EU to NL without customs friction. Discogs country
# strings include multi-region pressings; accept the common Euro ones. (UK alone is
# post-Brexit non-EU, so it's deliberately excluded — a UK-only alternative is no
# more reachable than the pick you already have.)
_EU_OK = frozenset(EU_COUNTRIES) | {
    "Europe", "UK & Europe", "Benelux", "Germany, Austria, & Switzerland",
    "France & Benelux", "Scandinavia",
}

_LOW_RATING = 3.8   # below this (with enough votes) → flag as possibly-avoid


# ── Pure decision helpers (unit-tested; no network) ──────────────────────────

def _is_lp(version: dict) -> bool:
    blob = " ".join(version.get("major_formats") or []) + " " + (version.get("format") or "")
    return "Vinyl" in blob or "LP" in blob


def _eu_lp_candidates(versions: list[dict], exclude_id: int) -> list[dict]:
    """EU-pressed LP versions other than the one already wantlisted, most-widely-
    owned first (community in_collection is the best free commonality proxy)."""
    cands = []
    for v in versions or []:
        try:
            vid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if vid == exclude_id or not _is_lp(v):
            continue
        if (v.get("country") or "") not in _EU_OK:
            continue
        owned = ((v.get("stats") or {}).get("community") or {}).get("in_collection") or 0
        cands.append({"id": vid, "country": v.get("country"), "year": v.get("released"),
                      "format": v.get("format"), "in_collection": owned})
    cands.sort(key=lambda c: -c["in_collection"])
    return cands


def _pick_best(details: list[dict], min_supply: int, rating_min_count: int) -> dict | None:
    """From candidate detail rows, choose the most-available one clearing
    `min_supply` copies for sale. Attach `low_rating` when the community average is
    poor AND enough people voted to trust it. None when nothing clears the bar."""
    viable = [d for d in details if (d.get("num_for_sale") or 0) >= min_supply]
    if not viable:
        return None
    best = max(viable, key=lambda d: d.get("num_for_sale") or 0)
    avg, cnt = best.get("rating_avg"), best.get("rating_count") or 0
    best = dict(best)
    best["low_rating"] = bool(avg is not None and avg < _LOW_RATING and cnt >= rating_min_count)
    return best


# ── Network + persistent cache ───────────────────────────────────────────────

def _get(url: str, token: str, run_cache: dict, label: str, params: dict | None = None):
    headers = {"Authorization": f"Discogs token={token}",
               "User-Agent": _USER_AGENT, "Accept": "application/json"}
    resp = _get_with_429_retry(url, headers=headers, params=params, cache=run_cache, label=label)
    if resp is None or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _rating(release: dict) -> tuple[float | None, int | None]:
    rt = (release.get("community") or {}).get("rating") or {}
    return rt.get("average"), rt.get("count")


def _versions(master_id, token: str, run_cache: dict) -> list[dict]:
    out: list[dict] = []
    page = 1
    while page <= 2:   # 200 versions is plenty; deeper masters truncate gracefully
        data = _get(f"{_BASE}/masters/{master_id}/versions", token, run_cache,
                    f"versions({master_id}) p{page}", {"per_page": 100, "page": page})
        if not data:
            break
        out.extend(data.get("versions") or [])
        if page >= ((data.get("pagination") or {}).get("pages") or 1):
            break
        page += 1
    return out


def _analyse(rid: int, token: str, run_cache: dict, *, min_supply: int, rating_min_count: int) -> dict:
    """One release → a conclusion dict (persisted): a suggestion (or None) plus raw
    signals kept for later. Never raises; a network miss yields suggestion None."""
    rel = _get(f"{_BASE}/releases/{rid}", token, run_cache, f"release({rid})", {"curr_abbr": "EUR"})
    if not rel:
        return {"suggestion": None, "raw": None}
    w_avg, w_cnt = _rating(rel)
    raw = {"wantlisted_rating_avg": w_avg, "wantlisted_rating_count": w_cnt,
           "notes": rel.get("notes")}   # notes captured now, unused — free data for later
    master_id = rel.get("master_id")
    if not master_id:
        return {"suggestion": None, "raw": raw}

    cands = _eu_lp_candidates(_versions(master_id, token, run_cache), exclude_id=rid)
    details = []
    for c in cands[:2]:   # only the 2 most-owned EU LPs — bounds calls per release
        cr = _get(f"{_BASE}/releases/{c['id']}", token, run_cache, f"cand({c['id']})", {"curr_abbr": "EUR"})
        if not cr:
            continue
        c_avg, c_cnt = _rating(cr)
        details.append({**c, "num_for_sale": cr.get("num_for_sale"),
                        "lowest_price": cr.get("lowest_price"),
                        "rating_avg": c_avg, "rating_count": c_cnt})
    return {"suggestion": _pick_best(details, min_supply, rating_min_count), "raw": raw}


def find_reissues(
    problem: list[dict],
    *,
    token: str,
    persistent: dict,
    run_cache: dict,
    ttl_days: int,
    max_lookups: int,
    min_supply: int,
    rating_min_count: int,
) -> list[dict]:
    """For each problem release, return a display suggestion where a better EU
    pressing exists. Fresh cached conclusions (including negatives) are reused for
    free; only up to `max_lookups` *new* releases are queried this run — the rest
    wait for a later run, so Discogs is never hammered.

    `persistent` is mutated in place ({str(rid): {'fetched_at','conclusion', ...}}).
    Returns [{wantlisted_id, artist, title, suggestion:{...}}, ...]."""
    now_iso = datetime.now(timezone.utc).isoformat()
    lookups = 0
    suggestions: list[dict] = []
    for item in problem:
        try:
            rid = int(item["release_id"])
        except (KeyError, TypeError, ValueError):
            continue
        rid_s = str(rid)
        ent = (persistent or {}).get(rid_s)
        if ent and _fresh(ent.get("fetched_at"), ttl_days):
            conclusion = ent.get("conclusion") or {}
        elif lookups < max_lookups:
            conclusion = _analyse(rid, token, run_cache,
                                  min_supply=min_supply, rating_min_count=rating_min_count)
            persistent[rid_s] = {"fetched_at": now_iso, "conclusion": conclusion,
                                 "artist": item.get("artist"), "title": item.get("title")}
            lookups += 1
        else:
            continue   # over the per-run budget — leave for a later run
        sug = (conclusion or {}).get("suggestion")
        if sug:
            suggestions.append({"wantlisted_id": rid, "artist": item.get("artist"),
                                "title": item.get("title"), "suggestion": sug})
    if lookups:
        logger.info("Reissue finder: %d new lookup(s), %d suggestion(s) total", lookups, len(suggestions))
    return suggestions
