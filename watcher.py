#!/usr/bin/env python3
"""Discogs wantlist watcher — entry point. See README for setup + cron."""

import json
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
import shipping_policy
import shop_api
from models import Deal
from notifier import EmailNotifier

# ── Paths ────────────────────────────────────────────────────────────────────

_DIR = Path(__file__).parent
_ENV_FILE = _DIR / ".env"
_COOKIES_FILE = _DIR / "cookies.json"
_STATE_FILE = _DIR / "state.json"

# ── Logging ──────────────────────────────────────────────────────────────────

_debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
_dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

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

_ALERTED_HARD_CAP = 50_000
_PENDING_HARD_CAP = 500
_COOKIE_ALERT_COOLDOWN_HOURS = 24
_SESSION_EXPIRY_WARN_DAYS = 14


# ── State ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not _STATE_FILE.exists():
        return {}
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read state.json (%s) — starting empty", exc)
        return {}


def _save_state(state: dict) -> None:
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, _STATE_FILE)


def _load_alerted(state: dict) -> dict[int, float]:
    """state['alerted'] schema: {str(id): last_alert_price_in_buyer_currency}."""
    raw = state.get("alerted")
    if isinstance(raw, dict):
        return {int(k): float(v) for k, v in raw.items() if v is not None}
    return {}


def _prune_alerted(alerted: dict[int, float]) -> dict[int, float]:
    if len(alerted) <= _ALERTED_HARD_CAP:
        return alerted
    # Keep the most-recent (highest IDs); IDs grow monotonically at Discogs
    keep = sorted(alerted.items(), key=lambda kv: kv[0], reverse=True)[:_ALERTED_HARD_CAP]
    return dict(keep)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Config ───────────────────────────────────────────────────────────────────

# Every knob the watcher reads from .env is *required* — there are no silent
# production defaults. A normal run with any required key missing aborts with the
# full list of what's absent. A DRY_RUN smoke-test is the one exception: missing
# keys fall back to the example values so the pipeline runs without a populated
# .env. The handful of vars below stay optional because their unset state is a
# meaningful "feature off", not a hidden default.

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")


def _parse_bool(raw: str) -> bool:
    low = raw.lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    raise ValueError(f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)})")


def _parse_digest_mode(raw: str) -> str:
    mode = raw.lower()
    if mode not in ("hourly", "daily"):
        raise ValueError("expected 'hourly' or 'daily'")
    return mode


def _opt_int(key: str, default: int | None) -> int | None:
    """Optional integer for a feature-toggle knob (unset = feature off)."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, ignoring", key, raw)
        return default


def _load_config() -> dict:
    load_dotenv(_ENV_FILE)
    missing: list[str] = []

    def req(key: str, parse, fallback):
        """A required .env value. Missing → recorded (and, in dry-run only,
        replaced by `fallback`); malformed → aborts immediately."""
        raw = os.getenv(key, "").strip()
        if not raw:
            missing.append(key)
            return fallback
        try:
            return parse(raw)
        except ValueError as exc:
            logger.error("Invalid %s=%r: %s", key, raw, exc)
            sys.exit(1)

    cfg = {
        "my_country": req("MY_COUNTRY", str, "Netherlands"),
        "min_media_condition": req("MIN_MEDIA_CONDITION", evaluator.parse_condition, "Near Mint (NM or M-)"),
        "min_sleeve_condition": req("MIN_SLEEVE_CONDITION", evaluator.parse_condition, "Near Mint (NM or M-)"),
        "deal_threshold": req("DEAL_THRESHOLD", float, 0.4),
        "vat_rate": req("VAT_RATE", float, 0.21),
        "big_deal_threshold": req("BIG_DEAL_THRESHOLD", float, 0.6),
        "price_drop_threshold": req("PRICE_DROP_THRESHOLD", float, 0.05),
        "smtp_host": req("SMTP_HOST", str, "127.0.0.1"),
        "smtp_port": req("SMTP_PORT", int, 1025),
        "smtp_user": req("SMTP_USER", str, "you@example.com"),
        "smtp_pass": req("SMTP_PASS", str, "your_smtp_password"),
        "smtp_from": req("SMTP_FROM", str, "you@example.com"),
        "smtp_to": req("SMTP_TO", str, "you@example.com"),
        "digest_mode": req("DIGEST_MODE", _parse_digest_mode, "hourly"),
        "digest_hour_utc": req("DIGEST_HOUR_UTC", int, 7),
        "max_deals_per_email": req("MAX_DEALS_PER_EMAIL", int, 0),  # 0 = no cap
        "max_emails_per_day": req("MAX_EMAILS_PER_DAY", int, 4),
        "group_by_release": req("GROUP_BY_RELEASE", _parse_bool, True),
        "max_siblings_per_release": req("MAX_SIBLINGS_PER_RELEASE", int, 1),
        "max_pages_per_run": req("MAX_PAGES_PER_RUN", int, 30),
        "shipping_hints": req("SHIPPING_HINTS", _parse_bool, True),
        "est_grams_per_vinyl": req("EST_GRAMS_PER_VINYL", int, 250),
        "max_seller_picks": req("MAX_SELLER_PICKS", int, 5),
        "shipping_policy_ttl_days": req("SHIPPING_POLICY_TTL_DAYS", int, 30),
        "price_history_days": req("PRICE_HISTORY_DAYS", int, 365),
        "price_history_min_points": req("PRICE_HISTORY_MIN_POINTS", int, 3),
        # ── Optional feature toggles (unset = feature off, not a hidden default) ──
        "seller_rating_min": _opt_int("SELLER_RATING_MIN", None),
        "healthcheck_url": os.getenv("HEALTHCHECK_URL", "").strip() or None,
        "discogs_token": os.getenv("DISCOGS_TOKEN", "").strip() or None,
        "discogs_username": os.getenv("DISCOGS_USERNAME", "").strip() or None,
    }

    if missing:
        if _dry_run:
            logger.warning("DRY_RUN: using example values for unset .env keys: %s", ", ".join(missing))
        else:
            logger.error(
                "Missing required .env config: %s — set them in .env "
                "(see .env.example), or run with DRY_RUN=1 to use example values.",
                ", ".join(missing),
            )
            sys.exit(1)
    return cfg


# ── Cookie-expiry pre-check ──────────────────────────────────────────────────

def _check_session_health(state: dict, cfg: dict, now: datetime) -> None:
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
        state, cfg, now,
        key="session_expiry_alert_sent_at",
        subject="Session cookie expiring soon",
        body=(
            f"Heads-up: your Discogs {msg}.\n\n"
            f"To avoid the watcher silently failing, re-export sid + session from your\n"
            f"browser DevTools (https://www.discogs.com → F12 → Application → Cookies)\n"
            f"and update {_COOKIES_FILE}.\n"
        ),
    )


def _maybe_send_admin_alert(state, cfg, now, key, subject, body):
    last = _parse_ts(state.get(key))
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
        state[key] = now.isoformat()
    except Exception as exc:
        logger.error("Admin alert send failed (%s): %s", subject, exc)


# ── Network annotations (opt-in via DISCOGS_TOKEN) ───────────────────────────

def _annotate_discogs_wide_median(deals: list[Deal], token: str, cache: dict) -> None:
    """Mutate each deal in-place with Discogs-wide median for its media condition.

    Calls /marketplace/price_suggestions per unique release_id, 1/s, cached
    per run. Failures degrade silently — annotation is optional UI signal.
    """
    for d in deals:
        rid = d.release_id
        if not rid:
            continue
        suggestions = discogs_api.price_suggestions(int(rid), token=token, cache=cache)
        if not suggestions:
            continue
        bucket = suggestions.get(d.media_condition or "")
        if not isinstance(bucket, dict):
            continue
        d.discogs_wide_median_value = bucket.get("value")
        d.discogs_wide_median_currency = bucket.get("currency")
    release_hits = sum(1 for k, v in cache.items() if isinstance(k, int) and v)
    release_total = sum(1 for k in cache if isinstance(k, int))
    logger.info("Discogs-wide median: %d release(s) queried, %d hit",
                release_total, release_hits)


def _annotate_shipping(deals, seller_groups, cfg, run_cache, policy_cache):
    """Attach a per-seller shipping hint + 'also wanted from this seller' picks.

    Reuses already-fetched wantlist listings for the picks (no extra requests);
    the only network call is one cached v3 policy lookup per deal-seller.
    """
    token, country = cfg["discogs_token"], cfg["my_country"]
    for d in deals:
        uid = d.seller_uid
        if uid is None:
            continue
        listings = seller_groups.get(int(uid), [])
        if not listings:
            continue
        picks, total_others = evaluator.seller_picks(listings, d.id, cfg["max_seller_picks"])
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


# ── Pending / digest helpers ─────────────────────────────────────────────────

def _emails_today(state: dict, now: datetime) -> int:
    daily = state.get("emails_today") or {}
    return int(daily.get("count") or 0) if daily.get("date") == now.date().isoformat() else 0


def _record_email_sent(state: dict, now: datetime) -> None:
    today = now.date().isoformat()
    daily = state.get("emails_today") or {}
    state["emails_today"] = (
        {"date": today, "count": int(daily.get("count") or 0) + 1}
        if daily.get("date") == today else {"date": today, "count": 1}
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = _load_config()
    now = datetime.now(tz=timezone.utc)
    state = _load_state()

    _check_session_health(state, cfg, now)

    alerted = _load_alerted(state)
    pending: list[Deal] = [Deal.from_pending(d) for d in (state.get("pending_deals") or [])]
    policy_cache: dict = state.get("shipping_policies") or {}
    price_history: dict = state.get("price_history") or {}

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
            state, cfg, now,
            key="cookie_alert_sent_at",
            subject="Session cookies rejected",
            body=(
                "The Discogs shop-page-api rejected the session cookies (HTTP 401/403).\n"
                f"Re-export sid + session from your browser into {_COOKIES_FILE}.\n"
                "The watcher will run but find nothing until you do."
            ),
        )
        _save_state(state)
        sys.exit(0)

    # Clear stale 401-alert flag once auth works again
    state.pop("cookie_alert_sent_at", None)

    listings = fetch_result.listings
    logger.info(
        "Fetched %d listing(s); pagination %s",
        len(listings), "complete" if fetch_result.complete else "incomplete",
    )

    # ── Build deals (pure pipeline: filter → evaluate → group → sort) ────────
    result = core.build_deals(listings, alerted, price_history, cfg, now)
    new_deals = result.deals
    just_alerted = result.just_alerted
    seller_groups = result.seller_groups
    scanned_releases = result.scanned_releases

    # ── Network annotations + counts (opt-in via DISCOGS_TOKEN) ──────────────
    # Shared cache: throttle-sentinel covers all calls below it.
    discogs_cache: dict = {}
    wantlist_total = None
    if cfg["discogs_token"]:
        if new_deals:
            _annotate_discogs_wide_median(new_deals, cfg["discogs_token"], discogs_cache)
        if cfg["discogs_username"]:
            wantlist_total = discogs_api.wantlist_size(
                cfg["discogs_username"], token=cfg["discogs_token"], cache=discogs_cache,
            )

    logger.info(
        "Wantlist scan: %s release(s) currently for sale%s",
        scanned_releases,
        f" out of {wantlist_total} on wantlist" if wantlist_total else "",
    )

    if cfg["shipping_hints"] and cfg["discogs_token"]:
        _annotate_shipping(new_deals, seller_groups, cfg, discogs_cache, policy_cache)

    pending.extend(new_deals)

    if len(pending) > _PENDING_HARD_CAP:
        dropped = len(pending) - _PENDING_HARD_CAP
        pending = pending[-_PENDING_HARD_CAP:]
        logger.warning("Pending exceeded cap; dropped %d oldest", dropped)

    # ── Decide whether to flush ──────────────────────────────────────────────
    should_flush = core.should_flush(len(pending), cfg["digest_mode"], now, cfg["digest_hour_utc"])

    sent_today = _emails_today(state, now)
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

    flush_ok = False
    if should_flush:
        cap = cfg["max_deals_per_email"] or len(pending)  # 0 = no cap
        to_send = pending[:cap]
        extra = len(pending) - len(to_send)
        scan_counts = {"scanned_releases": scanned_releases, "wantlist_total": wantlist_total}
        if _dry_run:
            from notifier import _build_html, _build_text
            html_path = Path("/tmp/digest.html")
            text_path = Path("/tmp/digest.txt")
            html_path.write_text(_build_html(to_send, now, extra, session_days_left, scan_counts=scan_counts))
            text_path.write_text(_build_text(to_send, now, extra, session_days_left, scan_counts=scan_counts))
            logger.info("DRY_RUN: digest written to %s and %s (%d deal(s), %d extra)",
                        html_path, text_path, len(to_send), extra)
            flush_ok = True
        else:
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
            _record_email_sent(state, now)
    elif not pending:
        logger.info("No pending deals to send")
    else:
        logger.info("Holding %d pending deal(s) (mode=%s, hour=%d)",
                    len(pending), cfg["digest_mode"], now.hour)

    # ── Update alerted dict ──────────────────────────────────────────────────
    for lid, price in just_alerted:
        alerted[lid] = price

    # ── Persist + heartbeat ──────────────────────────────────────────────────
    if fetch_result.complete:
        state["last_successful_run"] = now.isoformat()
        if cfg["healthcheck_url"]:
            try:
                requests.get(cfg["healthcheck_url"], timeout=5)
            except Exception as exc:
                logger.debug("Healthcheck ping failed: %s", exc)

    core.prune_price_history(price_history, now, cfg["price_history_days"])
    state["alerted"] = {str(k): v for k, v in _prune_alerted(alerted).items()}
    state["pending_deals"] = [d.to_pending() for d in pending]
    state["shipping_policies"] = policy_cache
    state["price_history"] = price_history
    state["last_run"] = now.isoformat()
    _save_state(state)

    logger.info(
        "Done. fetched=%d evaluated_deals=%d pending=%d alerted_total=%d emails_today=%d",
        len(listings), len(new_deals), len(pending), len(alerted),
        _emails_today(state, now),
    )


if __name__ == "__main__":
    main()
