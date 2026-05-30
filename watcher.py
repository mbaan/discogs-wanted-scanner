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

import discogs_api
import evaluator
import shipping_policy
import shop_api
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


def _migrate_alerted(state: dict) -> dict[int, float]:
    """
    state['alerted'] schema: {str(id): last_alert_price_in_buyer_currency}.
    Migrates from older dict-shaped 'seen' if present.
    """
    raw = state.get("alerted")
    if isinstance(raw, dict):
        return {int(k): float(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, list):
        return {int(x): 0.0 for x in raw}
    seen = state.get("seen")  # tolerate older state file shape
    if isinstance(seen, dict):
        out = {}
        for k, v in seen.items():
            if isinstance(v, dict):
                out[int(k)] = float(v.get("price") or 0.0)
            else:
                out[int(k)] = 0.0
        return out
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

def _smtp(key: str) -> str:
    """Like _require, but tolerant of placeholders in dry-run mode."""
    val = os.getenv(key, "").strip()
    if val:
        return val
    if _dry_run:
        return f"<dry-run:{key}>"
    logger.error("Missing required SMTP config: %s (set in .env, or run with DRY_RUN=1)", key)
    sys.exit(1)


def _opt_float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", key, raw, default)
        return default


def _opt_int(key: str, default: int | None) -> int | None:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", key, raw, default)
        return default


def _opt_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _load_config() -> dict:
    load_dotenv(_ENV_FILE)
    min_cert = os.getenv("MIN_CERTAINTY", "MEDIUM").strip().upper()
    if min_cert not in ("HIGH", "MEDIUM", "LOW"):
        logger.warning("Invalid MIN_CERTAINTY=%r, using MEDIUM", min_cert)
        min_cert = "MEDIUM"
    digest_mode = os.getenv("DIGEST_MODE", "hourly").strip().lower()
    if digest_mode not in ("hourly", "daily"):
        logger.warning("Invalid DIGEST_MODE=%r, using hourly", digest_mode)
        digest_mode = "hourly"
    return {
        "my_country": os.getenv("MY_COUNTRY", "Netherlands").strip(),
        "deal_threshold": _opt_float("DEAL_THRESHOLD", 0.25),
        "price_drop_threshold": _opt_float("PRICE_DROP_THRESHOLD", 0.05),
        "seller_rating_min": _opt_int("SELLER_RATING_MIN", None),
        "smtp_host": _smtp("SMTP_HOST"),
        "smtp_port": int(os.getenv("SMTP_PORT", "1025")),
        "smtp_user": _smtp("SMTP_USER"),
        "smtp_pass": _smtp("SMTP_PASS"),
        "smtp_from": _smtp("SMTP_FROM"),
        "alert_to": _smtp("ALERT_TO"),
        "min_certainty": min_cert,
        "digest_mode": digest_mode,
        "digest_hour_utc": _opt_int("DIGEST_HOUR_UTC", 7),
        "max_deals_per_email": _opt_int("MAX_DEALS_PER_EMAIL", 0),  # 0 = no cap, show all
        "max_emails_per_day": _opt_int("MAX_EMAILS_PER_DAY", 4),
        "group_by_release": _opt_bool("GROUP_BY_RELEASE", True),
        "max_siblings_per_release": _opt_int("MAX_SIBLINGS_PER_RELEASE", 1),
        "max_pages_per_run": _opt_int("MAX_PAGES_PER_RUN", 30),
        "healthcheck_url": os.getenv("HEALTHCHECK_URL", "").strip() or None,
        "discogs_token": os.getenv("DISCOGS_TOKEN", "").strip() or None,
        "discogs_username": os.getenv("DISCOGS_USERNAME", "").strip() or None,
        "shipping_hints": _opt_bool("SHIPPING_HINTS", True),
        "est_grams_per_vinyl": _opt_int("EST_GRAMS_PER_VINYL", 250),
        "max_seller_picks": _opt_int("MAX_SELLER_PICKS", 5),
        "shipping_policy_ttl_days": _opt_int("SHIPPING_POLICY_TTL_DAYS", 30),
    }


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
        smtp_from=cfg["smtp_from"], alert_to=cfg["alert_to"],
    )
    try:
        notifier.send_admin_alert(subject, body)
        state[key] = now.isoformat()
    except Exception as exc:
        logger.error("Admin alert send failed (%s): %s", subject, exc)


# ── Per-listing evaluation ───────────────────────────────────────────────────

_CERTAINTY_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}


def _evaluate_release_group(group: list[dict], cfg: dict) -> list[dict]:
    """Evaluate one release's qualifying-condition listings, filter by certainty."""
    deals = evaluator.evaluate_release_group(
        group,
        deal_threshold=cfg["deal_threshold"],
        my_country=cfg["my_country"],
    )
    return [
        d for d in deals
        if evaluator.certainty_passes_min(d["certainty_label"], cfg["min_certainty"])
    ]


# ── Grouping ─────────────────────────────────────────────────────────────────

def _group_by_release(deals: list[dict], max_siblings: int = 1) -> list[dict]:
    """
    Collapse multiple deals for the same release into one primary entry plus
    up to `max_siblings` runner-ups. Primary = lowest landed price (what
    actually matters for the buyer). With max_siblings=1, each release
    contributes at most 2 visible listings.
    """
    by_release: dict[int, list[dict]] = {}
    no_release: list[dict] = []
    for d in deals:
        rid = d.get("release_id")
        if rid:
            by_release.setdefault(int(rid), []).append(d)
        else:
            no_release.append(d)

    grouped: list[dict] = []
    for rid, group in by_release.items():
        group.sort(key=lambda d: d.get("landed_price", 1e9))
        primary = dict(group[0])
        primary["_siblings"] = [
            {
                "landed_price": s.get("landed_price"),
                "landed_currency": s.get("landed_currency"),
                "listing_url": s.get("listing_url"),
                "seller_username": s.get("seller_username"),
                "media_condition": s.get("media_condition"),
                "discount_pct": s.get("discount_pct"),
            }
            for s in group[1:1 + max_siblings]
        ]
        grouped.append(primary)
    grouped.extend(no_release)
    return grouped


def _annotate_discogs_wide_median(deals: list[dict], token: str, cache: dict) -> None:
    """Mutate each deal in-place with Discogs-wide median for its media condition.

    Calls /marketplace/price_suggestions per unique release_id, 1/s, cached
    per run. Failures degrade silently — annotation is optional UI signal.
    """
    for d in deals:
        rid = d.get("release_id")
        if not rid:
            continue
        suggestions = discogs_api.price_suggestions(int(rid), token=token, cache=cache)
        if not suggestions:
            continue
        bucket = suggestions.get(d.get("media_condition") or "")
        if not isinstance(bucket, dict):
            continue
        d["discogs_wide_median_value"] = bucket.get("value")
        d["discogs_wide_median_currency"] = bucket.get("currency")
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
        uid = d.get("seller_uid")
        if uid is None:
            continue
        listings = seller_groups.get(int(uid), [])
        if not listings:
            continue
        picks, total_others = evaluator.seller_picks(listings, d.get("id"), cfg["max_seller_picks"])
        d["_seller_picks"] = picks
        d["_seller_total_others"] = total_others

        policy = shipping_policy.get_policy(
            uid, country, token=token, run_cache=run_cache,
            persistent=policy_cache, ttl_days=cfg["shipping_policy_ttl_days"],
        )
        if policy is None:
            continue
        subtotal = sum(float(l.get("price") or 0.0) for l in listings)
        hint = shipping_policy.estimate_room(policy, len(listings), subtotal, cfg["est_grams_per_vinyl"])
        hint["seller"] = d.get("seller_username")
        hint["country"] = country
        hint["total_others"] = total_others
        d["shipping_hint"] = hint


def _deal_sort_key(d: dict) -> tuple:
    """Alphabetical by artist then title — reads naturally in the digest."""
    return (
        (d.get("release_artist") or "").lower(),
        (d.get("release_title") or "").lower(),
        (d.get("release_year") or 0),
    )


# ── Pending / digest helpers ─────────────────────────────────────────────────

_STRIPPED_BEFORE_PENDING = {"listed_at"}


def _strip_for_pending(deal: dict) -> dict:
    return {k: v for k, v in deal.items() if k not in _STRIPPED_BEFORE_PENDING}


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

    alerted = _migrate_alerted(state)
    pending: list[dict] = state.get("pending_deals") or []
    policy_cache: dict = state.get("shipping_policies") or {}

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

    # ── Group by release, filter to qualifying conditions ────────────────────
    by_release: dict[int, list[dict]] = {}
    skipped_condition = 0
    skipped_no_release = 0
    for l in listings:
        if not evaluator.passes_condition(l.get("media_condition"), l.get("sleeve_condition")):
            skipped_condition += 1
            continue
        rid = l.get("release_id")
        if rid is None:
            skipped_no_release += 1
            continue
        by_release.setdefault(int(rid), []).append(l)
    logger.info(
        "Grouped into %d release(s); skipped %d for condition, %d missing release_id",
        len(by_release), skipped_condition, skipped_no_release,
    )

    # Per-seller view of qualifying wantlist listings — basis for shipping hints.
    seller_groups = evaluator.group_by_seller(listings, passing_only=True)

    # ── Evaluate each release group ──────────────────────────────────────────
    new_deals: list[dict] = []
    just_alerted: list[tuple[int, float]] = []  # (id, buyer_price)

    for release_id, group in by_release.items():
        deals = _evaluate_release_group(group, cfg)

        for d in deals:
            lid = d["id"]
            cur_price = float(d.get("buyer_price") or d.get("price") or 0.0)
            prev_alert = alerted.get(lid)
            if prev_alert is not None and prev_alert > 0 and cur_price >= prev_alert * (1 - cfg["price_drop_threshold"]):
                continue  # already alerted at near this price
            new_deals.append(d)
            just_alerted.append((lid, cur_price))
            logger.info(
                "Deal[%s/%s]: %s — %s | %s%.2f landed | %s",
                d["certainty_label"], d["deal_source"],
                d.get("release_artist") or "?", d.get("release_title") or "?",
                evaluator.currency_symbol(d["landed_currency"]), d["landed_price"],
                d["deal_reason"],
            )

        # Record price for every listing so re-alerts only fire on drops
        for l in group:
            cur_price = float(l.get("buyer_price") or l.get("price") or 0.0)
            just_alerted.append((l["id"], cur_price))

    # ── Discogs-wide annotations + counts (opt-in via DISCOGS_TOKEN) ─────────
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

    scanned_releases = len(by_release)
    logger.info(
        "Wantlist scan: %s release(s) currently for sale%s",
        scanned_releases,
        f" out of {wantlist_total} on wantlist" if wantlist_total else "",
    )

    # ── Group + sort + cap pending ───────────────────────────────────────────
    if cfg["group_by_release"]:
        new_deals = _group_by_release(new_deals, max_siblings=cfg["max_siblings_per_release"])
    new_deals.sort(key=_deal_sort_key)

    if cfg["shipping_hints"] and cfg["discogs_token"]:
        _annotate_shipping(new_deals, seller_groups, cfg, discogs_cache, policy_cache)

    for d in new_deals:
        pending.append(_strip_for_pending(d))

    if len(pending) > _PENDING_HARD_CAP:
        dropped = len(pending) - _PENDING_HARD_CAP
        pending = pending[-_PENDING_HARD_CAP:]
        logger.warning("Pending exceeded cap; dropped %d oldest", dropped)

    # ── Decide whether to flush ──────────────────────────────────────────────
    if cfg["digest_mode"] == "daily":
        should_flush = (now.hour == cfg["digest_hour_utc"]) and bool(pending)
    else:
        should_flush = bool(pending)

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
                smtp_from=cfg["smtp_from"], alert_to=cfg["alert_to"],
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

    state["alerted"] = {str(k): v for k, v in _prune_alerted(alerted).items()}
    state["pending_deals"] = pending
    state["shipping_policies"] = policy_cache
    state["last_run"] = now.isoformat()
    _save_state(state)

    logger.info(
        "Done. fetched=%d evaluated_deals=%d pending=%d alerted_total=%d emails_today=%d",
        len(listings), len(new_deals), len(pending), len(alerted),
        _emails_today(state, now),
    )


if __name__ == "__main__":
    main()
