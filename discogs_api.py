"""
Official Discogs API client (PAT-authenticated).

Used for the wantlist-size count (total releases on the user's wantlist)
that the marketplace pool cannot provide on its own.

Rate limit (authenticated): 60 req / 60s sliding window. Defenses:
  - 1s sleep between calls (≈ 60/min ceiling, but sliding window can still
    trip near the edge)
  - on 429 we back off 60s and retry once; persistent 429 sets a
    cache-wide sentinel that aborts further calls for the rest of the run
  - X-Discogs-Ratelimit-Remaining is logged at DEBUG when it drops below 10
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.discogs.com"
_USER_AGENT = "DiscogsWantlistWatcher/1.0"
_TIMEOUT = 15
_THROTTLED_KEY = "_throttled"  # cache sentinel: stop calling for the run
_429_BACKOFF_SECONDS = 60      # full Discogs sliding window
_RATELIMIT_WARN_THRESHOLD = 10


def _is_throttled(cache: dict) -> bool:
    return bool(cache.get(_THROTTLED_KEY))


def _log_ratelimit(resp: requests.Response, endpoint_label: str) -> None:
    remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining")
    if remaining is None:
        return
    try:
        n = int(remaining)
    except ValueError:
        return
    if n < _RATELIMIT_WARN_THRESHOLD:
        logger.debug("Discogs ratelimit-remaining=%d after %s", n, endpoint_label)


def _get_with_429_retry(
    url: str,
    *,
    headers: dict,
    params: dict | None,
    cache: dict,
    label: str,
) -> requests.Response | None:
    """GET with 1s post-call sleep, one full-window retry on 429, then sentinel.

    Returns the requests.Response on success (including non-429 errors so the
    caller can decide how to handle them), or None if we hit the run-wide
    throttle sentinel.
    """
    if _is_throttled(cache):
        return None

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("%s network error: %s", label, exc)
        time.sleep(1)
        return None

    _log_ratelimit(resp, label)
    time.sleep(1)

    if resp.status_code != 429:
        return resp

    # First 429: most often a sliding-window edge. Wait one full window and retry.
    logger.warning("%s rate-limited (429) — backing off %ds and retrying once",
                   label, _429_BACKOFF_SECONDS)
    time.sleep(_429_BACKOFF_SECONDS)

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("%s network error on retry: %s", label, exc)
        time.sleep(1)
        return None

    _log_ratelimit(resp, label + " (retry)")
    time.sleep(1)

    if resp.status_code == 429:
        logger.error("%s still 429 after %ds backoff — aborting further Discogs "
                     "API calls for this run", label, _429_BACKOFF_SECONDS)
        cache[_THROTTLED_KEY] = True
        return None

    return resp


def wantlist_size(
    username: str,
    *,
    token: str,
    cache: dict,
) -> int | None:
    """
    Total releases on the user's wantlist (pagination.items from /wants).

    One PAT call per run; result memoized into the caller's cache dict.
    Returns None on failure or when throttled — caller should treat that as
    "unknown" and skip the annotation rather than crashing.
    """
    key = f"_wantlist_size:{username}"
    if key in cache:
        return cache[key]

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    resp = _get_with_429_retry(
        f"{_BASE}/users/{username}/wants",
        headers=headers, params={"per_page": 1, "page": 1}, cache=cache,
        label=f"wants({username})",
    )
    if resp is None or resp.status_code != 200:
        if resp is not None:
            logger.warning("wants(%s) HTTP %d: %.200s",
                           username, resp.status_code, resp.text)
        cache[key] = None
        return None

    try:
        items = resp.json().get("pagination", {}).get("items")
    except ValueError:
        logger.warning("wants(%s) non-JSON response", username)
        cache[key] = None
        return None

    cache[key] = int(items) if isinstance(items, int) else None
    return cache[key]


def wantlist_releases(
    username: str,
    *,
    token: str,
    cache: dict,
    max_pages: int = 20,
) -> list[dict] | None:
    """The full wantlist as [{release_id, artist, title, year}, ...], or None on
    failure/throttle.

    Paginates /wants (100/page). Used by the Weekly Dig only (once a week), so the
    per-run cost is one short paginated sweep. Memoized into the caller's cache.
    `max_pages` is a safety cap (2000 items); a real wantlist is far smaller.
    Returns None (not []) on any error so the caller can tell "couldn't fetch"
    from "genuinely empty" and skip the Dig rather than report a hollow one."""
    key = f"_wantlist_releases:{username}"
    if key in cache:
        return cache[key]

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    items: list[dict] = []
    page = 1
    while page <= max_pages:
        resp = _get_with_429_retry(
            f"{_BASE}/users/{username}/wants",
            headers=headers, params={"per_page": 100, "page": page}, cache=cache,
            label=f"wants({username}) p{page}",
        )
        if resp is None or resp.status_code != 200:
            if resp is not None:
                logger.warning("wants(%s) HTTP %d: %.200s", username, resp.status_code, resp.text)
            cache[key] = None
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.warning("wants(%s) non-JSON response", username)
            cache[key] = None
            return None
        for w in data.get("wants", []):
            bi = w.get("basic_information") or {}
            artists = bi.get("artists") or []
            items.append({
                "release_id": bi.get("id"),
                "artist": (artists[0].get("name") if artists and isinstance(artists[0], dict) else None),
                "title": bi.get("title"),
                "year": bi.get("year"),
            })
        pages = (data.get("pagination") or {}).get("pages") or 1
        if page >= pages:
            break
        page += 1

    cache[key] = items
    return items
