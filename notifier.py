"""
Notification layer.

EmailNotifier sends digest emails (one per flush) and admin alerts (cookie
expiry, watcher health). NtfyNotifier is the push fast-lane client (instant
ntfy pushes for top-tier deals); the Notifier base class is their shared
contract.
"""

import base64
import html as _html
import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

from evaluator import condition_short, currency_symbol
from models import Deal

logger = logging.getLogger(__name__)

# Header banner: a 680×150 JPEG embedded as a base64 data-URI <img> (not a CSS
# background-image — some email clients strip those). Loaded once at import. If the
# asset is missing we degrade to a plain text header rather than crash a run.
_HEADER_IMG_PATH = Path(__file__).parent / "records-header-email.jpg"
try:
    _HEADER_IMG_DATA_URI = (
        "data:image/jpeg;base64,"
        + base64.b64encode(_HEADER_IMG_PATH.read_bytes()).decode()
    )
except OSError as exc:
    logger.warning("Header image %s unavailable (%s) — using text-only header", _HEADER_IMG_PATH, exc)
    _HEADER_IMG_DATA_URI = None


class Notifier(ABC):
    @abstractmethod
    def send(self, deals: list[Deal], run_time: datetime, **kwargs) -> int | None:
        """Send a batch alert for the given deals. Push implementations return
        the delivered count; email returns None."""


class EmailNotifier(Notifier):
    def __init__(self, smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, smtp_to):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.smtp_from = smtp_from
        self.smtp_to = smtp_to

    def send(
        self,
        deals: list[Deal],
        run_time: datetime,
        extra_count: int = 0,
        session_days_left: int | None = None,
        scan_counts: dict | None = None,
    ) -> None:
        if not deals:
            return
        subject = _build_subject(deals)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_from
        msg["To"] = self.smtp_to
        msg.attach(MIMEText(_build_text(deals, run_time, extra_count, session_days_left, scan_counts=scan_counts), "plain"))
        msg.attach(MIMEText(_build_html(deals, run_time, extra_count, session_days_left, scan_counts=scan_counts), "html"))

        self._send_message(msg)
        logger.info("Email sent to %s (%d deal(s), %d extra)", self.smtp_to, len(deals), extra_count)

    def send_admin_alert(self, subject: str, body: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Discogs Watcher] {subject}"
        msg["From"] = self.smtp_from
        msg["To"] = self.smtp_to
        msg.attach(MIMEText(body, "plain"))
        self._send_message(msg)
        logger.info("Admin alert sent: %s", subject)

    def _send_message(self, msg: MIMEMultipart) -> None:
        port = self.smtp_port
        try:
            # send_message() serializes via as_bytes(), so non-ASCII content
            # (€, em-dash, 🔥, accented seller names) is encoded correctly;
            # sendmail()+as_string() can mangle or raise on it.
            if port == 465:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.smtp_host, port, context=ctx) as s:
                    s.login(self.smtp_user, self.smtp_pass)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, port, timeout=30) as s:
                    s.ehlo()
                    if s.has_extn("STARTTLS"):
                        s.starttls()
                        s.ehlo()
                    s.login(self.smtp_user, self.smtp_pass)
                    s.send_message(msg)
        except smtplib.SMTPException as exc:
            logger.error("SMTP send failed: %s", exc)
            raise


# ── Push fast-lane (ntfy) ─────────────────────────────────────────────────────

class NtfyNotifier(Notifier):
    """Best-effort instant push for top-tier deals via ntfy (https://ntfy.sh).

    WHY a sibling of EmailNotifier rather than a flag on it: the two channels have
    nothing in common at the transport layer (SMTP vs one HTTP POST per deal) and
    different bodies (a full HTML digest vs a one-line markdown push). The shared
    contract is the Notifier.send() signature; everything else differs, so two
    classes is cleaner than one branchy one.
    """

    def __init__(self, server: str, topic: str, token: str | None = None,
                 priority: str | None = None, max_per_run: int = 10,
                 timeout: float = 10.0):
        self.server = server
        self.topic = topic
        self.token = token
        self.priority = priority
        self.max_per_run = max_per_run
        self.timeout = timeout

    def send(self, deals: list[Deal], run_time: datetime, **kwargs) -> int:
        """POST one push per deal (capped at max_per_run). Never raises: a push is
        a nicety, the digest is the record (see _post). `deals` is the caller's
        already-selected, already-capped push-worthy subset. Returns the number
        actually delivered, so the caller logs the truth rather than the attempt
        count (a failed POST must not read as a success)."""
        return sum(self._post(deal) for deal in deals[: self.max_per_run])

    def _post(self, deal: Deal) -> bool:
        """Publish one deal to ntfy; return True iff delivered.

        Uses ntfy's JSON publish format (POST to the server root with the topic in
        the body), NOT the per-topic header API. WHY: HTTP header values must be
        latin-1, but our title carries a U+2212 minus sign, an ⬇ emoji, and
        arbitrary-script artist names — all of which raise UnicodeEncodeError when
        set as a `Title` header (requests encodes headers as latin-1). The JSON
        body is UTF-8, so it carries any Unicode cleanly. The only header is the
        ASCII bearer token; nothing Unicode ever rides in a header.
        Ref: https://docs.ntfy.sh/publish/#publish-as-json"""
        payload: dict = {
            "topic": self.topic,
            "title": _push_title(deal),
            "message": _push_body(deal),
            "markdown": True,
        }
        if deal.listing_url:
            payload["click"] = deal.listing_url
        if deal.image_url:
            payload["icon"] = deal.image_url          # JPEG cover thumbnail
        prio = _ntfy_priority(self.priority)
        if prio is not None:
            payload["priority"] = prio
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            resp = requests.post(self.server.rstrip("/"), json=payload,
                                 headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except Exception as exc:                      # fail-open: log, never propagate
            logger.warning("ntfy push failed for listing %s: %s", deal.id, exc)
            return False


_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def _ntfy_priority(value: str | None) -> int | None:
    """Map a configured ntfy priority to the JSON API's integer (1–5), or None to
    omit. The header API accepted names ('high') or digits; the JSON API wants an
    int, so translate. Unrecognised → None (omit; ntfy applies its default)."""
    if not value:
        return None
    v = str(value).strip().lower()
    if v.isdigit():
        n = int(v)
        return n if 1 <= n <= 5 else None
    return _NTFY_PRIORITY.get(v)


# ── Rendering ────────────────────────────────────────────────────────────────

def _h(s: str | None) -> str:
    return _html.escape(s) if s else ""


def _money(amount: float | int | None, ccy: str | None) -> str:
    if amount is None:
        return "—"
    return f"{currency_symbol(ccy)}{float(amount):.2f}"


def _identity(deal) -> str:
    """`Artist — Title` (or just the title when there's no artist)."""
    artist = deal.release_artist or ""
    title = deal.release_title or "Unknown"
    return f"{artist} — {title}" if artist else title


def _meta_line(deal) -> str:
    """`NM/VG+ · 1959 · LP · US` — the condition pair first (decision-relevant and
    tied to the better-grade caveat), then year/format/country. Each trailing part
    is dropped when absent; the condition pair always renders (matches `_heading`)."""
    cond_pair = (
        f"{condition_short(deal.media_condition)}/"
        f"{condition_short(deal.sleeve_condition)}"
    )
    bits = [cond_pair]
    if deal.release_year:
        bits.append(str(deal.release_year))
    if deal.release_format:
        bits.append(deal.release_format)
    if deal.release_country:
        bits.append(deal.release_country)
    return " · ".join(bits)


def _heading(deal) -> str:
    artist = deal.release_artist or ""
    title = deal.release_title or "Unknown"
    year = deal.release_year
    fmt = deal.release_format
    country = deal.release_country
    head = f"{artist} — {title}" if artist else title
    suffix_bits = []
    if year:
        suffix_bits.append(str(year))
    if fmt:
        suffix_bits.append(fmt)
    if country:
        suffix_bits.append(country)
    suffix = " ".join(suffix_bits)
    cond_pair = (
        f"{condition_short(deal.media_condition)}/"
        f"{condition_short(deal.sleeve_condition)}"
    )
    if suffix:
        return f"{head} · {suffix} · {cond_pair}"
    return f"{head} · {cond_pair}"


def _discount_label(deal) -> str:
    """Hero discount string: `−45%` for a deal, or `DEAL` when there's no computed
    discount (solo listing that only fired on Discogs' own flag)."""
    if deal.discount_pct is not None:
        return f"−{deal.discount_pct}%"
    return "DEAL"


def _push_title(deal) -> str:
    """Short glanceable headline: discount leads (survives OS truncation), then
    artist. An all-time-low deal gets a leading ⬇."""
    head = deal.release_artist or deal.release_title or "Deal"
    title = f"{_discount_label(deal)} · {head}"
    if deal.historical_floor_value is not None:
        title = f"⬇ {title}"
    return title


def _push_body(deal) -> str:
    """One-glance push body. Reads fine as raw text (mobile apps don't render
    markdown yet) AND as markdown (web app). Lines, not paragraphs."""
    lines = [
        f"**{_identity(deal)}**",                              # Artist — Title
        f"{condition_short(deal.media_condition)} · "
        f"{_discount_label(deal)} · {_landed_str(deal)} landed",
    ]
    if deal.historical_floor_value is not None:
        lines.append("⬇ All-time low")
    return "\n".join(lines)


def _build_subject(deals: list[Deal]) -> str:
    """Lead the subject with the strongest deal — discount, record, price — so
    the hook survives inbox truncation. Deals arrive deepest-discount-first, so
    `deals[0]` is the headline; remaining ones become a `+N more` tail. The
    `[Discogs Watcher]` prefix stays put for mail filters."""
    top = deals[0]
    title = top.release_title or "Unknown"
    head = f"{top.release_artist} — {title}" if top.release_artist else title
    price = _money(top.buyer_price, top.buyer_currency)
    if top.discount_pct is not None:
        lead = f"−{top.discount_pct}% off"
    else:
        lead = "Deal"
    subject = f"[Discogs Watcher] {lead}: {head} · {price}"
    extra = len(deals) - 1
    if extra:
        subject += f" (+{extra} more)"
    return subject


def _landed_str(deal) -> str:
    """All-in landed price, e.g. `€52.00` — the hero figure beneath the discount.
    Uses the VAT-inclusive effective cost when present, so for non-EU imports the
    figure matches the (effective-cost-based) discount and the footer's definition
    of landed; falls back to the bare item+shipping when effective cost is unset
    (legacy/persisted deals)."""
    ccy = deal.landed_currency or deal.buyer_currency or deal.currency
    amount = deal.effective_cost if deal.effective_cost is not None else deal.landed_price
    return _money(amount, ccy)


def _cost_line(deal) -> str:
    """`€45.00 + €7.00 ship + ~€9.00 VAT = €52.00 landed` — the explicit landed-cost
    equation, tying the rail's landed figure back to its parts. The VAT term shows
    only for estimated import VAT; the footer explains the benchmark."""
    item = deal.buyer_price or deal.price
    item_ccy = deal.buyer_currency or deal.currency
    ship = deal.shipping_buyer_price or deal.shipping_price or 0
    landed_ccy = deal.landed_currency or item_ccy
    out = f"{_money(item, item_ccy)} + {_money(ship, landed_ccy)} ship"
    if deal.vat_estimated and deal.vat_amount:
        out += f" + ~{_money(deal.vat_amount, landed_ccy)} VAT"
    out += f" = {_landed_str(deal)} landed"
    return out


def _proof_line(deal) -> str:
    """The single market-proof line: `SOLD median €42.00 · €26.00–€80.00 · last
    2026-01-23`. Shows the real per-condition sold value behind the verdict for
    every sold-bearing path. The sale count rides the confidence chip for
    SOLD-validated deals (`below_sold_median`), so it's repeated here only for the
    sparse path, where the chip omits it. Empty when there's no sold data at all
    (pure asking / remote) — the chip already says so. `sold_last_date` keeps its
    existing ISO string (no reformatting)."""
    v = deal.sold_median_value
    if v is None:
        return ""
    ccy = deal.sold_median_currency or deal.landed_currency
    out = f"SOLD median {_money(v, ccy)}"
    if deal.deal_source != "below_sold_median" and deal.sold_data_points:
        out += f" ({deal.sold_data_points} sold)"
    lo, hi = deal.sold_low_value, deal.sold_high_value
    if lo is not None and hi is not None:
        out += f" · {_money(lo, ccy)}–{_money(hi, ccy)}"
    if deal.sold_last_date:
        out += f" · last {deal.sold_last_date}"
    return out


def _sold_tiers_snippet(deal) -> str:
    """`Also sold: VG+↑ €38 (12), NM €52 (6), M €70 (3)` — what the record fetches
    at this grade-and-up (pooled) and at each *better* grade alone, so the condition
    premium is visible. Empty when there's no better grade with sold data (nothing
    to compare up to; the exact grade is already covered by the snippets above)."""
    higher = [
        t for t in (deal.sold_tier_higher or [])
        if isinstance(t, dict) and t.get("median") is not None
    ]
    if not higher:
        return ""
    ccy = deal.sold_median_currency or deal.landed_currency

    def _one(short: str, median, count) -> str:
        cnt = f" ({count})" if count else ""
        return f"{short} {_money(median, ccy)}{cnt}"

    parts = []
    pooled = deal.sold_tier_at_or_above
    if isinstance(pooled, dict) and pooled.get("median") is not None:
        parts.append(_one(pooled.get("short", ""), pooled["median"], pooled.get("count")))
    parts.extend(_one(t.get("short", ""), t["median"], t.get("count")) for t in higher)
    return "Also sold: " + ", ".join(parts)


def _seller_line(deal) -> str:
    name = deal.seller_username or "—"
    rating = deal.seller_rating
    rating_str = f" {rating:.1f}%" if isinstance(rating, (int, float)) else ""
    region = deal.shipping_region or deal.ships_from or ""
    return f"{name}{rating_str} · {region}" if region else f"{name}{rating_str}"


def _sibling_html(sib: dict) -> str:
    amt = sib.get("landed_price")
    ccy = sib.get("landed_currency")
    seller = _h(sib.get("seller_username")) or "—"
    cond = condition_short(sib.get("media_condition"))
    pct = sib.get("discount_pct")
    extra = f" · {pct}%" if pct else ""
    url = _h(sib.get("listing_url") or "#")
    return (
        f'<div style="margin-top:6px; font-size:13px; color:#555;">'
        f'+ <a href="{url}" style="color:#555;">{_money(amt, ccy)}</a> landed · {cond} · {seller}{extra}'
        f'</div>'
    )


def _sibling_text(sib: dict) -> str:
    amt = sib.get("landed_price")
    ccy = sib.get("landed_currency")
    seller = sib.get("seller_username") or "—"
    cond = condition_short(sib.get("media_condition"))
    pct = sib.get("discount_pct")
    extra = f" · {pct}%" if pct else ""
    return f"+ {_money(amt, ccy)} landed · {cond} · {seller}{extra} · {sib.get('listing_url', '')}"


def _tier_caps(hint: dict) -> list[tuple]:
    """[(price, cap_or_None)] — cap = records that ship at that fee. None = top tier."""
    per = hint.get("per_item") or 1
    caps = []
    for tier in hint.get("tiers") or []:
        mx, price = tier[0], tier[1]
        caps.append((price, None if mx == 0 else max(1, int(mx // per))))
    return caps


def _shipping_summary(hint: dict) -> str:
    """Compact one-line policy summary: free-shipping nudge and/or fee tiers."""
    cur = hint.get("currency")
    parts = []
    if hint.get("free_shipping") and hint.get("free_min") is not None:
        fm = _money(hint["free_min"], cur)
        gap = hint.get("free_gap")
        if gap and gap > 0:
            parts.append(f"Free over {fm} — matches ≈ {_money(hint.get('subtotal'), cur)}, add {_money(gap, cur)}")
        else:
            parts.append(f"Free shipping over {fm}")
    caps = _tier_caps(hint)
    if caps:
        segs = [(f"{_money(p, cur)} (more)" if cap is None else f"{_money(p, cur)} (≤{cap})") for p, cap in caps]
        country = hint.get("country") or ""
        est = " (est.)" if hint.get("basis") == "weight-est" else ""
        parts.append(f"Ship {country}: " + ", ".join(segs) + est)
    return " · ".join(parts)


def _basket_item_str(item: dict, ccy: str | None) -> str:
    """`Bill Evans – Waltz for Debby (VG+, €9.00)` for one add-to-order item.
    Prices are the basket's native (policy-currency) figures, formatted with the
    basket currency — not buyer_price (see optimize_basket §6)."""
    artist = item.get("release_artist")
    title = item.get("release_title") or "?"
    name = f"{artist} – {title}" if artist else title
    cond = condition_short(item.get("media_condition"))
    price = _money(item.get("price"), ccy)
    return f"{name} ({cond}, {price})"


def _basket_phrase(basket: dict) -> tuple[str, str]:
    """(headline, detail) for a basket recommendation, currency-consistent in
    basket['currency']. Used by both HTML and text renderers."""
    ccy = basket.get("currency")
    items = ", ".join(_basket_item_str(it, ccy) for it in basket.get("add") or [])
    est = " (est.)" if basket.get("basis") == "weight-est" else ""
    if basket.get("kind") == "free_crossing":
        head = f"Combine to save {_money(basket.get('saving'), ccy)} shipping"
        detail = (f"add {items} to reach {_money(basket.get('free_min'), ccy)} "
                  f"→ free shipping{est}")
        return head, detail
    # tier_room
    room = basket.get("room_more") or 0
    head = f"Room for {room} more"
    detail = (f"add {items} before shipping steps "
              f"{_money(basket.get('fee_now'), ccy)} → {_money(basket.get('next_fee'), ccy)}{est}")
    return head, detail


def _basket_html(deal) -> str:
    basket = getattr(deal, "basket", None)
    if not basket:
        return ""
    head, detail = _basket_phrase(basket)
    return (
        f'<div style="margin-top:4px; font-size:12px; color:#1b5e20; font-weight:600;">'
        f'🛒 {_h(head)} — <span style="font-weight:400; color:#555;">{_h(detail)}</span></div>'
    )


def _basket_text(deal) -> str:
    basket = getattr(deal, "basket", None)
    if not basket:
        return ""
    head, detail = _basket_phrase(basket)
    return f"  🛒 {head}: {detail}"


def _pick_badge(pct) -> str:
    """Display form of a pick's signed discount: stored +18 (18% below the sold
    median) reads −18%; stored −15 reads +15%; 0 reads ±0%. Empty when None."""
    if pct is None:
        return ""
    if pct > 0:
        return f"−{pct}%"
    return f"+{-pct}%" if pct < 0 else "±0%"


def _shipping_html(deal) -> str:
    hint = deal.shipping_hint
    picks = deal.seller_picks or []
    others = deal.seller_total_others or 0
    if not hint and not picks:
        return ""
    seller = _h((hint or {}).get("seller") or deal.seller_username or "this seller")
    head = f"📦 {seller}" + (f" — {others} more on your wantlist" if others else "")
    rows = [f'<div style="margin-top:8px; font-size:13px; color:#444; font-weight:600;">{head}</div>']
    if hint:
        summ = _shipping_summary(hint)
        if summ:
            rows.append(f'<div style="font-size:12px; color:#666; margin-top:2px;">{_h(summ)}</div>')
    basket_html = _basket_html(deal)
    if basket_html:
        rows.append(basket_html)
    for p in picks:
        amt = _money(p.get("buyer_price"), p.get("buyer_currency"))
        cond = condition_short(p.get("media_condition"))
        title = _h((f'{p.get("release_artist")} – ' if p.get("release_artist") else "") + (p.get("release_title") or "?"))
        url = _h(p.get("listing_url") or "#")
        pct = p.get("discount_pct")
        badge = _pick_badge(pct)
        if badge:
            style = "color:#1b5e20; font-weight:600;" if pct > 0 else "color:#999;"
            badge = f'<span style="{style}">{badge}</span> · '
        rows.append(
            f'<div style="margin-top:4px; font-size:12px; color:#555;">'
            f'+ <a href="{url}" style="color:#555;">{amt}</a> · {badge}{cond} · {title}</div>'
        )
    if others > len(picks):
        rows.append(f'<div style="margin-top:2px; font-size:12px; color:#999;">+{others - len(picks)} more</div>')
    return '<div style="margin-top:6px; padding-top:6px; border-top:1px dashed #eee;">' + "".join(rows) + "</div>"


def _shipping_text(deal) -> list[str]:
    hint = deal.shipping_hint
    picks = deal.seller_picks or []
    others = deal.seller_total_others or 0
    if not hint and not picks:
        return []
    seller = (hint or {}).get("seller") or deal.seller_username or "this seller"
    lines = [f"📦 {seller}" + (f" — {others} more on your wantlist" if others else "")]
    if hint:
        summ = _shipping_summary(hint)
        if summ:
            lines.append("  " + summ)
    basket_text = _basket_text(deal)
    if basket_text:
        lines.append(basket_text)
    for p in picks:
        amt = _money(p.get("buyer_price"), p.get("buyer_currency"))
        cond = condition_short(p.get("media_condition"))
        title = (f'{p.get("release_artist")} – ' if p.get("release_artist") else "") + (p.get("release_title") or "?")
        badge = _pick_badge(p.get("discount_pct"))
        badge = f"{badge} · " if badge else ""
        lines.append(f"  + {amt} · {badge}{cond} · {title} · {p.get('listing_url', '')}")
    if others > len(picks):
        lines.append(f"  +{others - len(picks)} more")
    return lines


# Hero-rail palettes, keyed by discount depth. A notably big discount gets the loud
# red; an ordinary deal gets a calm savings-green; a listing that only fired on
# Discogs' own flag (no computed discount) gets neutral amber.
_RAIL_BIG = ("#ffe0e0", "#f0b0b0", "#b71c1c")
_RAIL_DEAL = ("#e8f5e9", "#b9dfbd", "#1b5e20")
_RAIL_REMOTE = ("#fff3c4", "#f0d678", "#7a5b00")

# Discount (%) at or above which the rail goes loud red. Colour tracks the headline
# number so it agrees with the deepest-first sort. Low-confidence (asking-only /
# sparse) and better-grade-caveat deals never go red — we don't shout about a price
# we don't trust.
_RAIL_RED_DISCOUNT_PCT = 20


def _rail_html(deal) -> str:
    """Left hero rail: big discount %, landed price, and the cover beneath."""
    if deal.discount_pct is None:
        bg, border, fg = _RAIL_REMOTE
    elif (deal.discount_pct >= _RAIL_RED_DISCOUNT_PCT
          and not deal.low_confidence and not deal.sold_tier_caveat):
        bg, border, fg = _RAIL_BIG
    else:
        bg, border, fg = _RAIL_DEAL
    img = deal.image_url
    img_html = (
        f'<img src="{_h(img)}" alt="" width="88" height="88" '
        f'style="display:block; border-radius:4px; object-fit:cover; margin:10px auto 0;">'
        if img else ""
    )
    return (
        f'<div style="background:{bg}; border:1px solid {border}; border-radius:6px; '
        f'padding:10px 6px; text-align:center;">'
        f'<div style="font-size:22px; font-weight:800; color:{fg}; line-height:1; '
        f'letter-spacing:-.02em;">{_h(_discount_label(deal))}</div>'
        f'<div style="font-size:12px; font-weight:600; color:{fg}; margin-top:4px;">'
        f'{_h(_landed_str(deal))}</div>'
        f'</div>'
        f'{img_html}'
    )


def _method_label(deal) -> tuple[str, str]:
    """How the verdict was reached, as (text, kind) with kind ∈ {'sold','asking',''}."""
    cond = condition_short(deal.media_condition)
    if deal.deal_source == "below_sold_median":
        pts = f" · {deal.sold_data_points} sales" if deal.sold_data_points else ""
        return f"✓ SOLD-validated{pts}", "sold"
    if deal.deal_source == "below_sold_low":
        return f"≈ below lowest {cond} sale (sparse data)", "asking"
    if deal.deal_source == "below_asking_median":
        return f"≈ asking-only · no {cond} sold data", "asking"
    if deal.deal_source == "below_condition_median":  # legacy persisted deals
        return "≈ asking-only", "asking"
    return "", ""


# Method-chip palettes: SOLD-validated reads as trusted (green); asking-only reads as
# a caveat (amber) — the benchmark is aspirational asking, not realised sales.
_METHOD_CHIP_CSS = {
    "sold": "background:#0b6e4f; color:#fff; border:1px solid #0e8c63;",
    "asking": "background:#fbe6c2; color:#7a4d00; border:1px solid #efce95;",
}


def _method_chip_html(deal) -> str:
    """The confidence chip, promoted to sit directly under the identity line:
    green `✓ SOLD-validated` vs amber `≈ asking-only`. Empty for remote-only
    listings (the rail's neutral `DEAL` already speaks for them)."""
    text, kind = _method_label(deal)
    if not kind:
        return ""
    return (
        '<div style="margin-top:6px;">'
        f'<span style="{_METHOD_CHIP_CSS[kind]} padding:1px 6px; border-radius:8px; '
        f'font-size:11px; font-weight:600;">{_h(text)}</span></div>'
    )


def _signal_chips_html(deal) -> str:
    """Per-deal warning/signal chips: better-grade caveat, detached-low verify,
    ★ Discogs Deal, ⬇ All-time low. The confidence chip is rendered separately,
    higher up (see `_method_chip_html`). Returns "" — and its row is omitted — when
    none apply."""
    chips = []
    if deal.sold_tier_caveat and deal.sold_tier_caveat_grade:
        ccy = deal.sold_median_currency or deal.landed_currency
        warn = f"⚠ {deal.sold_tier_caveat_grade} sells ~{_money(deal.sold_tier_caveat_value, ccy)}"
        chips.append(
            '<span style="background:#fde2cf; color:#8a3b00; padding:1px 6px; '
            'border-radius:8px; font-size:11px; font-weight:600; '
            f'border:1px solid #f0b98a;">{_h(warn)}</span>'
        )
    if getattr(deal, "detached_low", False):
        chips.append(
            '<span style="background:#fff3c4; color:#7a5b00; padding:1px 6px; '
            'border-radius:8px; font-size:11px; font-weight:600; '
            'border:1px solid #f0d678;">⚠ far below other copies — verify</span>'
        )
    if deal.is_deal_remote:
        chips.append(
            '<span style="background:#fff3c4; color:#7a5b00; padding:1px 6px; '
            'border-radius:8px; font-size:11px; font-weight:600; '
            'border:1px solid #f0d678;">★ Discogs Deal</span>'
        )
    # `is not None`: a deal can tie/beat the floor by < 1% (pct rounds to 0) and
    # must still badge — the push path keys off the floor the same way.
    if deal.historical_floor_pct is not None:
        chips.append(
            '<span style="background:#00695c; color:#fff; padding:1px 6px; '
            'border-radius:8px; font-size:11px; font-weight:600; '
            'border:1px solid #00897b;">⬇ All-time low</span>'
        )
    if not chips:
        return ""
    return '<div style="margin-top:6px;">' + " ".join(chips) + "</div>"


def _deal_html(deal) -> str:
    comments = (deal.comments or "").strip()
    comments_html = (
        f'<div style="margin-top:6px; font-size:12px; color:#666; font-style:italic;">'
        f'"{_h(comments)}"</div>'
        if comments else ""
    )

    proof = _proof_line(deal)
    proof_html = (
        f'<div style="font-size:13px; color:#444; margin-top:4px;">{_h(proof)}</div>'
        if proof else ""
    )

    siblings_html = "".join(_sibling_html(s) for s in (deal.siblings or []))
    shipping_html = _shipping_html(deal)
    extras_html = ""
    if siblings_html or shipping_html:
        extras_html = (
            '<div style="margin-top:10px; padding-top:8px; border-top:1px dashed #eee;">'
            '<div style="font-size:11px; text-transform:uppercase; letter-spacing:.08em; '
            'color:#999; margin-bottom:2px;">more copies / this seller</div>'
            f'{siblings_html}{shipping_html}</div>'
        )

    # Two-cell table (not flex): the hero rail is load-bearing, and Outlook drops
    # flex layouts. Left = discount/landed/cover rail; right = details, read top to
    # bottom: identity → confidence → proof → cost → signals → seller → act → extras.
    return f"""
  <tr><td style="padding:14px 0; border-bottom:1px solid #eee; font-family:sans-serif;">
    <table style="width:100%; border-collapse:collapse;"><tr>
      <td width="96" valign="top" style="width:96px; padding:0;">{_rail_html(deal)}</td>
      <td valign="top" style="padding-left:14px;">
        <div style="font-size:15px; font-weight:bold;">{_h(_identity(deal))}</div>
        <div style="font-size:13px; color:#666; margin-top:2px;">{_h(_meta_line(deal))}</div>
        {_method_chip_html(deal)}
        {proof_html}
        <div style="font-size:13px; color:#666; margin-top:2px;">{_h(_cost_line(deal))}</div>
        {_signal_chips_html(deal)}
        <div style="font-size:13px; color:#666; margin-top:8px;">{_h(_seller_line(deal))}</div>
        {comments_html}
        <div style="margin-top:8px;">
          <a href="{_h(deal.listing_url or "#")}"
             style="background:#333; color:#fff; padding:4px 10px; text-decoration:none;
                    border-radius:3px; font-size:12px;">View listing →</a>
        </div>
        {extras_html}
      </td>
    </tr></table>
  </td></tr>"""


def _session_note(days: int | None) -> tuple[str, str]:
    """Returns (label, color). Label is empty when days is None."""
    if days is None:
        return ("", "#888")
    if days < 0:
        return (f"cookie EXPIRED {-days}d ago", "#b71c1c")
    if days <= 14:
        return (f"cookie expires in {days}d", "#e65100")
    return (f"cookie {days}d valid", "#4caf50")


def _scan_summary(scan_counts: dict | None) -> str:
    """`50 of 2000 wantlist releases for sale` — empty if counts unavailable."""
    if not scan_counts:
        return ""
    scanned = scan_counts.get("scanned_releases")
    total = scan_counts.get("wantlist_total")
    if scanned is None:
        return ""  # count unavailable — 0 is a real (reportable) result
    if total:
        return f"{scanned} of {total} wantlist releases for sale"
    return f"{scanned} wantlist release(s) for sale"


def _build_html(
    deals: list[Deal], run_time: datetime, extra_count: int,
    session_days_left: int | None = None,
    scan_counts: dict | None = None,
) -> str:
    run_str = run_time.strftime("%Y-%m-%d %H:%M UTC")
    rows = "".join(_deal_html(d) for d in deals)
    n = len(deals)
    s = "s" if n != 1 else ""
    session_label, session_color = _session_note(session_days_left)
    session_html = (
        f'<span style="background:rgba(255,255,255,.15); color:#fff; padding:2px 8px; '
        f'border-radius:10px; font-size:11px; margin-left:8px; '
        f'border:1px solid {session_color};">{session_label}</span>'
        if session_label else ""
    )
    scan_summary = _scan_summary(scan_counts)
    scan_html = (
        f'<div style="margin-top:6px; color:rgba(255,255,255,0.7); font-size:12px;">{_h(scan_summary)}</div>'
        if scan_summary else ""
    )
    header_img_html = (
        f'<img src="{_HEADER_IMG_DATA_URI}" alt="" width="680" height="150" '
        f'style="grid-row:1; grid-column:1; display:block; width:100%; height:150px; object-fit:cover;">'
        if _HEADER_IMG_DATA_URI else ""
    )
    extra = ""
    if extra_count > 0:
        extra = (
            f'<p style="color:#888; font-size:13px;">'
            f"+{extra_count} more deal(s) not shown.</p>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#f0f0f0; padding:20px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:680px; margin:0 auto; background:#fff;
              border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,.08);">
    <div style="display:grid; background:#111;">
      {header_img_html}
      <div style="grid-row:1; grid-column:1; align-self:stretch; padding:22px 24px 20px;
                  background:linear-gradient(90deg,rgba(0,0,0,0.72) 0%,rgba(0,0,0,0.45) 50%,rgba(0,0,0,0.1) 100%);">
        <div style="font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.14em;
                    color:rgba(255,255,255,0.7); margin-bottom:10px;">&#9673; Discogs Watcher</div>
        <h1 style="margin:0; font-size:28px; font-weight:800; color:#fff; line-height:1.1;
                   letter-spacing:-.02em; text-shadow:0 2px 6px rgba(0,0,0,0.5);">{n} good deal{s}</h1>
        <p style="margin:4px 0 0; font-size:14px; color:rgba(255,255,255,0.88);">on your wantlist</p>
        <div style="margin-top:12px; font-size:12px; color:rgba(255,255,255,0.82);">{run_str}{session_html}</div>
        {scan_html}
      </div>
    </div>
    <div style="padding:8px 24px 20px;">
      <table style="width:100%; border-collapse:collapse;">{rows}</table>
      {extra}
      <p style="color:#aaa; font-size:11px; margin-top:14px; border-top:1px solid #eee; padding-top:10px;">
        Landed = item + shipping, in your account currency, plus an estimated
        import-VAT uplift for non-EU origins. Discount is how far below the typical
        all-in cost — the SOLD median (or, low-confidence, the wantlist asking
        median) plus a typical shipping allowance — each copy lands. Sorted deepest
        discount first; copies that shipping or VAT push above that all-in cost are
        not shown.
      </p>
    </div>
  </div>
</body></html>"""


def _build_text(
    deals: list[Deal], run_time: datetime, extra_count: int,
    session_days_left: int | None = None,
    scan_counts: dict | None = None,
) -> str:
    run_str = run_time.strftime("%Y-%m-%d %H:%M UTC")
    session_label, _ = _session_note(session_days_left)
    header = f"Discogs Watcher · {len(deals)} good deal(s) · {run_str}"
    if session_label:
        header += f" · {session_label}"
    scan_summary = _scan_summary(scan_counts)
    lines = [header]
    if scan_summary:
        lines.append(scan_summary)
    lines.append("=" * 60)
    for d in deals:
        lines.append("")
        lines.append(_heading(d))
        # Primary mirrors the rail: discount % + landed, plus the inline signal tags.
        primary = f"{_discount_label(d)} · {_landed_str(d)} landed"
        if d.sold_tier_caveat and d.sold_tier_caveat_grade:
            ccy = d.sold_median_currency or d.landed_currency
            primary += f" · ⚠ {d.sold_tier_caveat_grade} sells ~{_money(d.sold_tier_caveat_value, ccy)}"
        if d.historical_floor_pct is not None:
            primary += " · ⬇ all-time low"
        if getattr(d, "detached_low", False):
            primary += " · ⚠ verify pressing/grade"
        if d.is_deal_remote:
            primary += " · ★ Discogs Deal"
        lines.append(primary)
        method_text, _ = _method_label(d)
        if method_text:
            lines.append(method_text)
        proof = _proof_line(d)
        if proof:
            lines.append(proof)
        lines.append(_cost_line(d))
        tiers = _sold_tiers_snippet(d)
        if tiers:
            lines.append(tiers)
        lines.append(_seller_line(d))
        if d.comments:
            lines.append(f'"{d.comments}"')
        lines.append(d.listing_url or "")
        for s in d.siblings or []:
            lines.append(_sibling_text(s))
        lines.extend(_shipping_text(d))
        lines.append("-" * 60)
    if extra_count > 0:
        lines.append(f"\n+{extra_count} more deal(s) not shown.")
    return "\n".join(lines)
