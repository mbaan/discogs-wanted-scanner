#!/usr/bin/env python3
"""Discogs wantlist watcher — entry point. See README for setup + cron."""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import core
import discogs_api
import evaluator
import marketplace
import shipping_policy
import shop_api
import sold_prices
from models import Deal
from notifier import EmailNotifier, NtfyNotifier, _build_html
from store import _PENDING_HARD_CAP, Store

# ── Paths ────────────────────────────────────────────────────────────────────

_DIR = Path(__file__).parent
_ENV_FILE = _DIR / ".env"
_COOKIES_FILE = _DIR / "cookies.json"
_STATE_DB = _DIR / "state.db"
_REPORT_FILE = _DIR / "report.html"

# ── Logging ──────────────────────────────────────────────────────────────────

_debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

# cron + minimal-install hosts default to C/latin-1; force UTF-8 so log
# lines with currency symbols and em-dashes don't crash logging.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(
    level=logging.DEBUG if _debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_COOKIE_ALERT_COOLDOWN_HOURS = 24
_SESSION_EXPIRY_WARN_DAYS = 14


# ── Timestamps ───────────────────────────────────────────────────────────────

def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Push fast-lane gating ──────────────────────────────────────────────────────

def _is_push_worthy(deal, min_discount: float) -> bool:
    """High-confidence, strong deals only. Asking-fallback / low-confidence noise
    never pushes (the buzz must mean something). All-time-low always qualifies
    (rarity signal); otherwise it must clear the discount floor.

    - ranked excludes the remote_only lone-listing path (ranked=False).
    - low_confidence excludes every asking-fallback verdict — only SOLD-validated
      (below_sold_median) deals can push.
    - historical_floor_value is not None is the all-time-low badge.
    - otherwise effective_discount (the 0.0–1.0 fraction, NOT the rounded int
      discount_pct) must be >= min_discount.
    """
    if not deal.ranked or deal.low_confidence:
        return False
    return (deal.historical_floor_value is not None
            or (deal.effective_discount is not None
                and deal.effective_discount >= min_discount))


def _push_fresh(cur: float, prev: float | None, threshold: float) -> bool:
    """Mirror the alerted re-alert gate (core.build_deals): a listing is fresh to
    push when it was never pushed (prev is None / <= 0) or its price has dropped by
    at least `threshold` from the last-pushed price. Independent of the email/alerted
    gate so the two channels can't starve each other (see push spec §5.3)."""
    if prev is not None and prev > 0 and cur >= prev * (1 - threshold):
        return False
    return True


# ── Config ───────────────────────────────────────────────────────────────────

# Every knob the watcher reads from .env is *required* — there are NO defaults in
# this file. A run with any required key missing aborts with the full list of
# what's absent; copy .env.example and fill it in. The handful of vars at the
# bottom stay optional because their unset state is a meaningful "feature off",
# not a hidden default.


def _parse_bool(raw: str) -> bool:
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ValueError("expected a boolean (true/false)")


def _parse_digest_mode(raw: str) -> str:
    mode = raw.lower()
    if mode not in ("hourly", "daily"):
        raise ValueError("expected 'hourly' or 'daily'")
    return mode


def _opt(key: str):
    """An optional feature-toggle value: returns the trimmed string or None when
    unset (None = feature off, which is a real state, not a hidden default)."""
    return os.getenv(key, "").strip() or None


def _opt_int(key: str) -> int | None:
    """Optional integer toggle; unset or malformed → None (feature off)."""
    raw = _opt(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, ignoring", key, raw)
        return None


def _opt_float(key: str) -> float | None:
    """Optional float toggle; unset or malformed → None (feature off)."""
    raw = _opt(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, ignoring", key, raw)
        return None


def _opt_bool(key: str) -> bool:
    """Optional boolean toggle; unset or malformed → False (feature off)."""
    raw = _opt(key)
    if raw is None:
        return False
    try:
        return _parse_bool(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, ignoring", key, raw)
        return False


def _load_config() -> dict:
    load_dotenv(_ENV_FILE)
    missing: list[str] = []

    def req(key: str, parse):
        """A required .env value. Missing → recorded (we abort once, below);
        malformed → aborts immediately."""
        raw = os.getenv(key, "").strip()
        if not raw:
            missing.append(key)
            return None  # never used: we abort before cfg is consumed
        try:
            return parse(raw)
        except ValueError as exc:
            logger.error("Invalid %s=%r: %s", key, raw, exc)
            sys.exit(1)

    cfg = {
        "my_country": req("MY_COUNTRY", str),
        "min_media_condition": req("MIN_MEDIA_CONDITION", evaluator.parse_condition),
        "min_sleeve_condition": req("MIN_SLEEVE_CONDITION", evaluator.parse_condition),
        "asking_data_deal_threshold": req("ASKING_DATA_DEAL_THRESHOLD", float),
        "vat_rate": req("VAT_RATE", float),
        "price_drop_threshold": req("PRICE_DROP_THRESHOLD", float),
        "smtp_host": req("SMTP_HOST", str),
        "smtp_port": req("SMTP_PORT", int),
        "smtp_user": req("SMTP_USER", str),
        "smtp_pass": req("SMTP_PASS", str),
        "smtp_from": req("SMTP_FROM", str),
        "smtp_to": req("SMTP_TO", str),
        "digest_mode": req("DIGEST_MODE", _parse_digest_mode),
        "digest_hour_utc": req("DIGEST_HOUR_UTC", int),
        "max_deals_per_email": req("MAX_DEALS_PER_EMAIL", int),  # 0 = no cap
        "max_emails_per_day": req("MAX_EMAILS_PER_DAY", int),
        "group_by_release": req("GROUP_BY_RELEASE", _parse_bool),
        "max_siblings_per_release": req("MAX_SIBLINGS_PER_RELEASE", int),
        "max_pages_per_run": req("MAX_PAGES_PER_RUN", int),
        "shipping_hints": req("SHIPPING_HINTS", _parse_bool),
        "est_grams_per_vinyl": req("EST_GRAMS_PER_VINYL", int),
        "max_seller_picks": req("MAX_SELLER_PICKS", int),
        "shipping_policy_ttl_days": req("SHIPPING_POLICY_TTL_DAYS", int),
        "price_history_days": req("PRICE_HISTORY_DAYS", int),
        "price_history_min_points": req("PRICE_HISTORY_MIN_POINTS", int),
        # ── Optional feature toggles (unset = feature off, not a hidden default) ──
        "seller_rating_min": _opt_int("SELLER_RATING_MIN"),
        "healthcheck_url": _opt("HEALTHCHECK_URL"),
        "discogs_token": _opt("DISCOGS_TOKEN"),
        "discogs_username": _opt("DISCOGS_USERNAME"),
        # Combine-shipping basket optimizer (sub-feature of SHIPPING_HINTS):
        # when on, compute & render a concrete "add these to save shipping" line.
        "combine_basket": _opt_bool("COMBINE_BASKET"),
        "max_basket_items": _opt_int("MAX_BASKET_ITEMS") or 3,
        "sold_prices": _opt_bool("SOLD_PRICES"),
        "sold_price_ttl_days": _opt_int("SOLD_PRICE_TTL_DAYS"),
        # Live competitive picture: scrape every current copy of an *alerting*
        # release so the digest shows the true field (best other offer + ladder).
        # Cookie-gated (no PAT). Bounded per run since it fetches live (uncacheable).
        "live_market": _opt_bool("LIVE_MARKET"),
        "live_market_max_per_run": _opt_int("LIVE_MARKET_MAX_PER_RUN") or 25,
        "sold_price_min_points": _opt_int("SOLD_PRICE_MIN_POINTS"),
        "sold_deal_percentile": _opt_float("SOLD_DEAL_PERCENTILE"),
        "sold_deal_min_discount": _opt_float("SOLD_DEAL_MIN_DISCOUNT"),
        "shipping_allowance": _opt_float("SHIPPING_ALLOWANCE"),
        "asking_min_points": _opt_int("ASKING_MIN_POINTS"),
        # Better-grade caveat: how far below the nearest *trusted* better grade's
        # sold median a copy must sit to escape the "a better copy costs ~the same"
        # warning, and how many sales that better grade needs before we trust it.
        # Both default silently so the Pi keeps running without .env edits.
        "sold_tier_caveat_gap": _opt_float("SOLD_TIER_CAVEAT_GAP"),
        "sold_tier_caveat_min_points": _opt_int("SOLD_TIER_CAVEAT_MIN_POINTS"),
        # ── Real-time push fast-lane (ntfy) — optional feature toggle ──
        "push_enabled": _opt_bool("PUSH_ENABLED"),
        "push_channel": _opt("PUSH_CHANNEL"),
        "ntfy_server": _opt("NTFY_SERVER"),
        "ntfy_topic": _opt("NTFY_TOPIC"),
        "ntfy_token": _opt("NTFY_TOKEN"),
        "push_min_discount": _opt_float("PUSH_MIN_DISCOUNT"),
        "push_priority": _opt("PUSH_PRIORITY"),
        "push_max_per_run": _opt_int("PUSH_MAX_PER_RUN"),
    }

    # The TTL is the one knob that matters when sold-prices is on — keep the
    # no-hidden-defaults rule for it (but stay silent when the feature is off).
    if cfg["sold_prices"] and cfg["sold_price_ttl_days"] is None:
        missing.append("SOLD_PRICE_TTL_DAYS")
    # Min same-condition sold sales for the sold median to *lead* the verdict
    # (else the asking median does). Has a sensible default, so it's not required.
    if cfg["sold_prices"] and cfg["sold_price_min_points"] is None:
        cfg["sold_price_min_points"] = 5
    # Better-grade caveat knobs — sensible silent defaults when sold-prices is on.
    if cfg["sold_tier_caveat_gap"] is None:
        cfg["sold_tier_caveat_gap"] = 0.10
    if cfg["sold_tier_caveat_min_points"] is None:
        cfg["sold_tier_caveat_min_points"] = 3
    # Sold-gate percentile knobs + asking-pool floor — sensible silent defaults so
    # the watcher runs without forcing .env edits on deploy.
    if cfg["sold_deal_percentile"] is None:
        cfg["sold_deal_percentile"] = 20.0
    if cfg["sold_deal_min_discount"] is None:
        cfg["sold_deal_min_discount"] = 0.05
    # All-in shipping allowance added to the sold/asking median to form the deal
    # benchmark; 0 = strict (deal only when effective cost ≤ the bare median).
    if cfg["shipping_allowance"] is None:
        cfg["shipping_allowance"] = 7.0
    if cfg["asking_min_points"] is None:
        cfg["asking_min_points"] = 5

    # ── Push fast-lane: default the channel first, then enforce NTFY_TOPIC and
    # apply the remaining silent defaults (same pattern as the sold-price knobs). ──
    if cfg["push_enabled"]:
        if cfg["push_channel"] is None:
            cfg["push_channel"] = "ntfy"
        if cfg["ntfy_server"] is None:
            cfg["ntfy_server"] = "https://ntfy.sh"
        if cfg["push_min_discount"] is None:
            cfg["push_min_discount"] = 0.30
        if cfg["push_max_per_run"] is None:
            cfg["push_max_per_run"] = 10
        if cfg["push_channel"] == "ntfy" and not cfg["ntfy_topic"]:
            missing.append("NTFY_TOPIC")
        elif cfg["push_channel"] != "ntfy":
            logger.warning("Unsupported PUSH_CHANNEL=%r — push disabled this run",
                           cfg["push_channel"])
            cfg["push_enabled"] = False

    if missing:
        logger.error(
            "Missing required .env config: %s — set them in .env (copy .env.example).",
            ", ".join(missing),
        )
        sys.exit(1)
    return cfg


# ── Cookie-expiry pre-check ──────────────────────────────────────────────────

def _check_session_health(store, cfg: dict, now: datetime) -> None:
    try:
        cookies = shop_api._load_cookies(_COOKIES_FILE)
    except FileNotFoundError:
        return  # Caught in main()
    exp = shop_api.session_expires_at(cookies)
    if exp is None:
        logger.warning("Could not parse session cookie expiry — refresh recommended")
        return
    days_left = (exp - now).total_seconds() / 86400
    logger.info("session cookie expires %s (%.0f days from now)", exp.date(), days_left)
    if days_left > _SESSION_EXPIRY_WARN_DAYS:
        return
    if days_left < 0:
        msg = f"session cookie EXPIRED on {exp.isoformat()}"
    else:
        msg = f"session cookie expires in {days_left:.0f} day(s) ({exp.isoformat()})"
    _maybe_send_admin_alert(
        store, cfg, now,
        key="session_expiry_alert_sent_at",
        subject="Session cookie expiring soon",
        body=(
            f"Heads-up: your Discogs {msg}.\n\n"
            f"To avoid the watcher silently failing, re-export sid + session from your\n"
            f"browser DevTools (https://www.discogs.com → F12 → Application → Cookies)\n"
            f"and update {_COOKIES_FILE}.\n"
        ),
    )


def _maybe_send_admin_alert(store, cfg, now, key, subject, body):
    last = _parse_ts(store.get_meta(key))
    if last and (now - last) < timedelta(hours=_COOKIE_ALERT_COOLDOWN_HOURS):
        logger.info("Admin alert '%s' already sent within %dh — suppressing",
                    subject, _COOKIE_ALERT_COOLDOWN_HOURS)
        return
    notifier = EmailNotifier(
        smtp_host=cfg["smtp_host"], smtp_port=cfg["smtp_port"],
        smtp_user=cfg["smtp_user"], smtp_pass=cfg["smtp_pass"],
        smtp_from=cfg["smtp_from"], smtp_to=cfg["smtp_to"],
    )
    try:
        notifier.send_admin_alert(subject, body)
        store.set_meta(key, now.isoformat())
    except Exception as exc:
        logger.error("Admin alert send failed (%s): %s", subject, exc)


# ── Network annotations (opt-in via DISCOGS_TOKEN) ───────────────────────────

def _annotate_shipping(deals, seller_groups, cfg, run_cache, policy_cache,
                       sold_stats_by_release=None):
    """Attach a per-seller shipping hint + 'also wanted from this seller' picks.

    Reuses already-fetched wantlist listings for the picks (no extra requests);
    `sold_stats_by_release` (already fetched for evaluation) gives each pick its
    discount vs the sold median. The only network call is one cached v3 policy
    lookup per deal-seller.
    """
    token, country = cfg["discogs_token"], cfg["my_country"]
    # Annotate the best-price alternative the same way as the primary, so its
    # seller's combine-shipping picks render on its card too.
    for d in [*deals, *(d.best_alt for d in deals if d.best_alt)]:
        uid = d.seller_uid
        if uid is None:
            continue
        listings = seller_groups.get(int(uid), [])
        if not listings:
            continue
        picks, total_others = evaluator.seller_picks(
            listings, d.id, cfg["max_seller_picks"],
            sold_stats_by_release=sold_stats_by_release,
        )
        d.seller_picks = picks
        d.seller_total_others = total_others

        policy = shipping_policy.get_policy(
            uid, country, token=token, run_cache=run_cache,
            persistent=policy_cache, ttl_days=cfg["shipping_policy_ttl_days"],
        )
        if policy is None:
            continue
        subtotal = sum(float(l.price or 0.0) for l in listings)
        hint = shipping_policy.estimate_room(policy, len(listings), subtotal, cfg["est_grams_per_vinyl"])
        hint["seller"] = d.seller_username
        hint["country"] = country
        hint["total_others"] = total_others
        d.shipping_hint = hint
        if cfg["combine_basket"]:
            d.basket = shipping_policy.optimize_basket(
                policy, listings, d,
                est_grams=cfg["est_grams_per_vinyl"],
                max_add=cfg["max_basket_items"],
            )


def _fetch_sold_for_releases(listings, cookies_path, run_cache, sell_history_cache, cfg,
                             *, sold_ttl_days=None):
    """Fetch the per-condition SOLD benchmark (sell/history) for every distinct
    release with at least one condition-passing listing — BEFORE evaluation, so the
    sold median can *lead* the verdict (`evaluator._sold_leads`). Returns
    {release_id: stats}.

    Cookie-gated (NOT the PAT): one warmed curl_cffi session, reused. The persistent
    TTL cache makes steady-state cheap — only new/expired releases actually refetch.
    Fail-open: a miss (no cookies, 401/403, parse drift, no sales) just omits that
    release, which then falls back to the asking median. Cookie health is already
    surfaced by _check_session_health, so 401/403 here stays silent.
    """
    try:
        session = shop_api._make_session(shop_api._load_cookies(cookies_path))
    except FileNotFoundError:
        return {}
    shop_api._warm_up(session)  # mint a fresh __cf_bm before the bulk document GETs

    media_ok = evaluator.acceptable_conditions(cfg["min_media_condition"])
    sleeve_ok = evaluator.acceptable_conditions(cfg["min_sleeve_condition"])
    rids = {
        int(l.release_id) for l in listings
        if l.release_id is not None
        and evaluator.passes_condition(l.media_condition, l.sleeve_condition, media_ok, sleeve_ok)
    }

    out: dict[int, dict] = {}
    hit = 0
    for rid in sorted(rids):
        stats = sold_prices.get_sell_history(
            rid, session=session, run_cache=run_cache,
            persistent=sell_history_cache,
            ttl_days=cfg["sold_price_ttl_days"] if sold_ttl_days is None else sold_ttl_days,
        )
        if stats:
            hit += 1
            out[rid] = stats
    logger.info("Sold-history: %d release(s) queried, %d with sold data", len(rids), hit)
    return out


def _fetch_live_market(deals, cookies_path, cfg):
    """Fetch the live marketplace copy set for each distinct *alerting* release, so
    the digest can show the true competitive field. Returns {release_id: [copies]}.

    Bounded to alerting releases (few) and capped at LIVE_MARKET_MAX_PER_RUN — the
    listings are live and can't be TTL-cached, so each is a real request. Cookie-
    gated (NOT the PAT), one warmed session. Fail-open: a miss omits that release,
    which then renders as a non-authoritative fallback (no "cheapest" guarantee)."""
    rids = list(dict.fromkeys(
        int(d.release_id) for d in deals if d.release_id is not None
    ))
    if not rids:
        return {}
    try:
        session = shop_api._make_session(shop_api._load_cookies(cookies_path))
    except FileNotFoundError:
        return {}
    shop_api._warm_up(session)

    cap = cfg["live_market_max_per_run"]
    if len(rids) > cap:
        logger.warning("Live-market: %d alerting release(s) exceed cap %d — %d not fetched",
                       len(rids), cap, len(rids) - cap)
        rids = rids[:cap]

    run_cache: dict = {}
    out: dict[int, list] = {}
    hit = 0
    for rid in rids:
        copies = marketplace.get_release_listings(rid, session=session, run_cache=run_cache)
        if copies:
            out[rid] = copies
            hit += 1
    logger.info("Live-market: %d release(s) queried, %d with listings", len(rids), hit)
    return out


# ── Pending / digest helpers ─────────────────────────────────────────────────

def _emails_today(store, now: datetime) -> int:
    daily = store.load_emails_today()
    return int(daily.get("count") or 0) if daily.get("date") == now.date().isoformat() else 0


def _record_email_sent(store, now: datetime) -> None:
    today = now.date().isoformat()
    daily = store.load_emails_today()
    store.save_emails_today(
        {"date": today, "count": int(daily.get("count") or 0) + 1}
        if daily.get("date") == today else {"date": today, "count": 1}
    )


def _write_report(path, deals, now, *, extra_count, session_days_left, scan_counts) -> None:
    """Render the current run's deals to a standalone HTML snapshot using the same
    notifier._build_html renderer as the digest email. Latest-only, overwritten
    each run, atomic tmp + os.replace (same pattern as the old _save_state). An
    empty list renders a '0 good deals' placeholder so a stale report never
    misleads. Fail-open: a write error logs and is swallowed (the report is a
    convenience, never load-bearing)."""
    try:
        html = _build_html(deals, now, extra_count,
                            session_days_left=session_days_left, scan_counts=scan_counts)
        tmp = Path(path).with_suffix(".html.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("Could not write HTML report %s: %s", path, exc)


def _report_path():
    """Where the static HTML report lands. Always written (cheap, Pi-safe);
    REPORT_HTML, when set, redirects to a custom path (the existing optional
    feature-toggle convention — unset = default report.html, never disabled)."""
    override = _opt("REPORT_HTML")
    return Path(override) if override else _REPORT_FILE


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args(argv=None) -> argparse.Namespace:
    """--full: a LOUD FULL RUN against the real state.db — re-surface every
    current deal (bypass the alerted + pushed dedup), force-refetch sold data,
    and email + push the lot (capped by the usual limits). Persists normally."""
    parser = argparse.ArgumentParser(description="Discogs wantlist watcher")
    parser.add_argument(
        "--full", action="store_true",
        help="Loud full run: re-surface every current deal, force-refresh sold "
             "prices, and email + push the lot (capped by the usual limits)",
    )
    return parser.parse_args(argv)


def _prev_alerted_for(args: argparse.Namespace, alerted: dict) -> dict:
    """--full bypasses the re-alert gate for this run only: build_digest sees an
    empty dict so prev_alerted.get(id) is always None (every qualifying deal
    surfaces). The real alerted dict is still loaded + updated afterwards, so
    normal runs resume deduping."""
    return {} if args.full else alerted


def _prev_pushed_for(args: argparse.Namespace, pushed: dict) -> dict:
    """--full bypasses the push dedup the same way prev_alerted is bypassed: the
    push fast-lane sees an empty map so _push_fresh always returns True and every
    push-worthy deal re-pushes (capped by PUSH_MAX_PER_RUN). The real pushed dict
    is still updated + persisted afterwards, so normal runs resume deduping."""
    return {} if args.full else pushed


def _sold_ttl_for(args: argparse.Namespace, ttl_days):
    """--full force-refetches sold history by forcing an effective TTL of 0
    (sold_prices._fresh(ts, 0) is always False); the fresh result is still
    written back to the persistent cache normally."""
    return 0 if args.full else ttl_days


# ── Main ─────────────────────────────────────────────────────────────────────

def main(args: argparse.Namespace | None = None) -> None:
    if args is None:
        args = _parse_args()
    cfg = _load_config()
    now = datetime.now(tz=timezone.utc)
    store = Store.open(_STATE_DB)

    _check_session_health(store, cfg, now)

    alerted = store.load_alerted()
    pushed = store.load_pushed()
    pending: list[Deal] = [Deal.from_pending(d) for d in store.load_pending()]
    policy_cache: dict = store.load_shipping_policies()
    sell_history_cache: dict = store.load_sell_history()
    price_history: dict = store.load_price_history()

    # ── Fetch listings ────────────────────────────────────────────────────────
    # listed_after=None paginates to completion so price drops on older
    # listings surface (Discogs sorts by listedDate, not by price-change date).
    try:
        fetch_result = shop_api.fetch_listings(
            cookies_path=_COOKIES_FILE,
            seller_rating_min=cfg["seller_rating_min"],
            listed_after=None,
            max_pages=cfg["max_pages_per_run"],
            debug=_debug,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if fetch_result.cookie_invalid:
        _maybe_send_admin_alert(
            store, cfg, now,
            key="cookie_alert_sent_at",
            subject="Session cookies rejected",
            body=(
                "The Discogs shop-page-api rejected the session cookies (HTTP 401/403).\n"
                f"Re-export sid + session from your browser into {_COOKIES_FILE}.\n"
                "The watcher will run but find nothing until you do."
            ),
        )
        store.close()
        sys.exit(0)

    # Clear stale 401-alert flag once auth works again
    store.set_meta("cookie_alert_sent_at", None)

    listings = fetch_result.listings
    logger.info(
        "Fetched %d listing(s); pagination %s",
        len(listings), "complete" if fetch_result.complete else "incomplete",
    )

    # ── Sold-price benchmark (cookie-gated) — fetched BEFORE evaluation so the
    # per-condition sold median can *lead* the verdict where there's enough data.
    sold_run_cache: dict = {}
    sold_stats_by_release: dict[int, dict] = {}
    if cfg["sold_prices"]:
        sold_stats_by_release = _fetch_sold_for_releases(
            listings, _COOKIES_FILE, sold_run_cache, sell_history_cache, cfg,
            sold_ttl_days=_sold_ttl_for(args, cfg["sold_price_ttl_days"]),
        )

    # ── Build deals (pure pipeline: filter → evaluate → group → sort → sold-annotate) ──
    result = core.build_digest(
        listings, _prev_alerted_for(args, alerted), price_history, cfg, now, sold_stats_by_release,
    )
    new_deals = result.deals
    just_alerted = result.just_alerted
    seller_groups = result.seller_groups
    scanned_releases = result.scanned_releases

    # ── Live competitive picture (cookie-gated) ───────────────────────────────
    # Scrape every current copy of each alerting release so each deal can show the
    # best OTHER offer (win or lose) + the field ladder. Overrides the fallback
    # best_alt where the fetch succeeds; a miss leaves the fallback in place.
    if cfg["live_market"] and new_deals:
        live_by_release = _fetch_live_market(new_deals, _COOKIES_FILE, cfg)
        core.annotate_live_market(new_deals, live_by_release, cfg, sold_stats_by_release)
        # Give the live best-other cards their sold-gauge figures (median/range/last).
        live_alts = [d.best_alt for d in new_deals if d.best_alt and d.market_authoritative]
        core.annotate_sold_price(live_alts, sold_stats_by_release)

    # ── Network annotations + counts (opt-in via DISCOGS_TOKEN) ──────────────
    # Shared cache: throttle-sentinel covers all calls below it.
    discogs_cache: dict = {}
    wantlist_total = None
    if cfg["discogs_token"] and cfg["discogs_username"]:
        wantlist_total = discogs_api.wantlist_size(
            cfg["discogs_username"], token=cfg["discogs_token"], cache=discogs_cache,
        )

    logger.info(
        "Wantlist scan: %s release(s) currently for sale%s",
        scanned_releases,
        f" out of {wantlist_total} on wantlist" if wantlist_total else "",
    )

    if cfg["shipping_hints"] and cfg["discogs_token"]:
        _annotate_shipping(new_deals, seller_groups, cfg, discogs_cache, policy_cache,
                           sold_stats_by_release=sold_stats_by_release)

    # (Sold-price display annotation is applied inside core.build_digest, above.)
    pending.extend(new_deals)

    if len(pending) > _PENDING_HARD_CAP:
        dropped = len(pending) - _PENDING_HARD_CAP
        pending = pending[-_PENDING_HARD_CAP:]
        logger.warning("Pending exceeded cap; dropped %d oldest", dropped)

    # ── Real-time push fast-lane (best-effort; additive to the digest) ──────────
    # Fire BEFORE the flush decision so a top-tier find pings now, not at the next
    # digest. Wrapped whole so a push outage can never block or crash the run.
    # On a --full run the push dedup is bypassed (prev_pushed={}, mirroring the
    # email re-alert bypass) so the whole re-surfaced set re-pushes, capped by
    # PUSH_MAX_PER_RUN — a loud full run buzzes the phone on purpose.
    if cfg["push_enabled"]:
        try:
            prev_pushed = _prev_pushed_for(args, pushed)
            candidates = [d for d in new_deals
                          if _is_push_worthy(d, cfg["push_min_discount"])]
            fresh = []
            for d in candidates:
                cur = float(d.buyer_price or d.price or 0.0)
                if _push_fresh(cur, prev_pushed.get(d.id), cfg["price_drop_threshold"]):
                    fresh.append(d)
            if fresh:
                push = NtfyNotifier(
                    server=cfg["ntfy_server"], topic=cfg["ntfy_topic"],
                    token=cfg["ntfy_token"], priority=cfg["push_priority"],
                    max_per_run=cfg["push_max_per_run"],
                )
                # Mark only the deals actually handed to ntfy (its max_per_run cap):
                # overflow beyond the cap stays unmarked so it can push next run
                # instead of being silently deduped without ever buzzing the phone.
                attempted = fresh[: cfg["push_max_per_run"]]
                for d in attempted:
                    pushed[d.id] = float(d.buyer_price or d.price or 0.0)
                delivered = push.send(fresh, now)   # caps at max_per_run internally
                logger.info("Push fast-lane: %d of %d top-tier deal(s) delivered via ntfy",
                            delivered, len(attempted))
        except Exception as exc:
            logger.error("Push fast-lane failed (%s) — continuing; digest unaffected", exc)

    # ── Decide whether to flush ──────────────────────────────────────────────
    should_flush = core.should_flush(len(pending), cfg["digest_mode"], now, cfg["digest_hour_utc"])

    sent_today = _emails_today(store, now)
    if should_flush and sent_today >= cfg["max_emails_per_day"]:
        logger.warning("Email cap reached today (%d/%d) — deferring %d deal(s)",
                       sent_today, cfg["max_emails_per_day"], len(pending))
        should_flush = False

    # ── Send digest ──────────────────────────────────────────────────────────
    try:
        _cookies_now = shop_api._load_cookies(_COOKIES_FILE)
        _session_exp = shop_api.session_expires_at(_cookies_now)
        session_days_left = int((_session_exp - now).total_seconds() / 86400) if _session_exp else None
    except Exception:
        session_days_left = None

    scan_counts = {"scanned_releases": scanned_releases, "wantlist_total": wantlist_total}
    flush_ok = False
    if should_flush:
        cap = cfg["max_deals_per_email"] or len(pending)  # 0 = no cap
        to_send = pending[:cap]
        extra = len(pending) - len(to_send)
        notifier = EmailNotifier(
            smtp_host=cfg["smtp_host"], smtp_port=cfg["smtp_port"],
            smtp_user=cfg["smtp_user"], smtp_pass=cfg["smtp_pass"],
            smtp_from=cfg["smtp_from"], smtp_to=cfg["smtp_to"],
        )
        try:
            notifier.send(to_send, now, extra_count=extra, session_days_left=session_days_left, scan_counts=scan_counts)
            flush_ok = True
        except Exception as exc:
            logger.error("Email send failed: %s — %d deal(s) remain pending", exc, len(pending))
        if flush_ok:
            pending = pending[len(to_send):]
            _record_email_sent(store, now)
    elif not pending:
        logger.info("No pending deals to send")
    else:
        logger.info("Holding %d pending deal(s) (mode=%s, hour=%d)",
                    len(pending), cfg["digest_mode"], now.hour)

    # ── Update alerted dict ──────────────────────────────────────────────────
    for lid, price in just_alerted:
        alerted[lid] = price

    # ── Static HTML report (always written; latest-only snapshot) ─────────────
    _report_cap = cfg["max_deals_per_email"] or len(pending)  # 0 = no cap
    _report_deals = pending[:_report_cap]
    _report_extra = len(pending) - len(_report_deals)
    _write_report(
        _report_path(), _report_deals, now,
        extra_count=_report_extra, session_days_left=session_days_left,
        scan_counts=scan_counts,
    )

    # ── Persist + heartbeat ──────────────────────────────────────────────────
    if fetch_result.complete:
        store.set_meta("last_successful_run", now.isoformat())
        if cfg["healthcheck_url"]:
            try:
                requests.get(cfg["healthcheck_url"], timeout=5)
            except Exception as exc:
                logger.debug("Healthcheck ping failed: %s", exc)

    core.prune_price_history(price_history, now, cfg["price_history_days"])
    store.save_alerted(alerted)              # _ALERTED_HARD_CAP applied inside
    store.save_pushed(pushed)                # push fast-lane dedup set (same cap)
    store.save_pending([d.to_pending() for d in pending])  # _PENDING_HARD_CAP inside
    store.save_shipping_policies(policy_cache)
    store.save_sell_history(sell_history_cache)
    store.save_price_history(price_history)
    store.set_meta("last_run", now.isoformat())

    logger.info(
        "Done. fetched=%d evaluated_deals=%d pending=%d alerted_total=%d emails_today=%d",
        len(listings), len(new_deals), len(pending), len(alerted),
        _emails_today(store, now),
    )
    store.close()


if __name__ == "__main__":
    main()
