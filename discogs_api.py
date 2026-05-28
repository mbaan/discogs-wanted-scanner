"""
Official Discogs API client (PAT-authenticated).

Used for annotation signals that the wantlist marketplace pool cannot
provide on its own:
  - price_suggestions: Discogs-wide median ask per condition
  - wantlist_size: total releases on the user's wantlist

Rate limit (authenticated): 60 req / 60s sliding window. Defenses:
  - 1s sleep between calls (≈ 60/min ceiling, but sliding window can still
    trip near the edge)
  - on 429 we back off 60s and retry once; persistent 429 sets a
    cache-wide sentinel that aborts further calls for the rest of the run
  - X-Discogs-Ratelimit-Remaining is logged at DEBUG when it drops below 10

Note: /marketplace/price_suggestions/ requires the authenticated user to
have Discogs seller settings configured (currency + shipping policy). No
listings need to exist, but the seller profile must be set up; otherwise
the endpoint 404s with "You must fill out your seller settings first."
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


def price_suggestions(
    release_id: int,
    *,
    token: str,
    cache: dict,
) -> dict | None:
    """
    Fetch Discogs-wide median asking price per condition for a release.

    Returns a dict shaped like:
        {"Mint (M)": {"value": 42.5, "currency": "EUR"}, ...}
    in the seller-account's configured currency, or None when the release
    has no comparable asks (empty {} body), the call fails, or the run is
    throttled. Result is memoized into `cache` (caller-owned, keyed by
    release_id) so multiple deals on the same release cost one call.
    """
    if release_id in cache:
        return cache[release_id]
    if _is_throttled(cache) or cache.get("_price_suggestions_blocked"):
        return None

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    resp = _get_with_429_retry(
        f"{_BASE}/marketplace/price_suggestions/{release_id}",
        headers=headers, params=None, cache=cache,
        label=f"price_suggestions({release_id})",
    )
    if resp is None:
        cache[release_id] = None
        return None

    if resp.status_code == 404:
        if "seller settings" in resp.text.lower():
            if not cache.get("_seller_settings_warned"):
                logger.error(
                    "price_suggestions: Discogs requires seller settings on the "
                    "token account (currency + shipping policy). Configure at "
                    "https://www.discogs.com/settings/seller (no listings "
                    "required), or unset DISCOGS_TOKEN to skip wide-median "
                    "annotation."
                )
                cache["_seller_settings_warned"] = True
            cache["_price_suggestions_blocked"] = True
        cache[release_id] = None
        return None
    if resp.status_code == 401:
        # Bad/revoked token, or endpoint-specific OAuth gating. Won't recover
        # within this run — stop calling price_suggestions but let other
        # endpoints (e.g. wants) keep trying since they may use different gating.
        if not cache.get("_price_suggestions_401_warned"):
            logger.error(
                "price_suggestions(%s) HTTP 401: %.200s — disabling "
                "wide-median annotation for the rest of this run",
                release_id, resp.text,
            )
            cache["_price_suggestions_401_warned"] = True
        cache["_price_suggestions_blocked"] = True
        cache[release_id] = None
        return None
    if resp.status_code != 200:
        logger.warning("price_suggestions(%s) HTTP %d: %.200s",
                       release_id, resp.status_code, resp.text)
        cache[release_id] = None
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.warning("price_suggestions(%s) non-JSON response", release_id)
        cache[release_id] = None
        return None

    cache[release_id] = data if isinstance(data, dict) and data else None
    return cache[release_id]


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
