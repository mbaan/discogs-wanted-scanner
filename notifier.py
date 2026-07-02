"""
Notification layer.

EmailNotifier sends digest emails (one per flush) and admin alerts (cookie
expiry, watcher health). NtfyNotifier is the push fast-lane client (instant
ntfy pushes for top-tier deals); the Notifier base class is their shared
contract.

The digest renders in the "The Find" visual language: a vintage-reissue card
where the verdict is a shop grading-sticker, a market gauge plots the all-in
price against real sold prices, and (the core honesty fix) a distinct second
card surfaces the cheapest still-listed copy of the same record when one exists.
Edge states downgrade the visuals so a low-confidence or suspect deal can never
masquerade as a trustworthy one (sticker colour is the trust signal).

Everything is table-based + inline-styled for email-client safety (Outlook =
Word engine; no flex/grid except the banner overlay, which is the one tested
exception already in use).
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

import evaluator
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


# ── Low-level rendering helpers ────────────────────────────────────────────────

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
    """`NM/VG+ · 1959 · LP · US` — condition pair first, then year/format/country.
    Each trailing part is dropped when absent; the condition pair always renders."""
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
    """`Artist — Title · 1959 LP US · NM/VG+` — the text-part heading."""
    head = _identity(deal)
    suffix_bits = []
    if deal.release_year:
        suffix_bits.append(str(deal.release_year))
    if deal.release_format:
        suffix_bits.append(deal.release_format)
    if deal.release_country:
        suffix_bits.append(deal.release_country)
    cond_pair = (
        f"{condition_short(deal.media_condition)}/"
        f"{condition_short(deal.sleeve_condition)}"
    )
    suffix = " ".join(suffix_bits)
    return f"{head} · {suffix} · {cond_pair}" if suffix else f"{head} · {cond_pair}"


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
        f"**{_identity(deal)}**",
        f"{condition_short(deal.media_condition)} · "
        f"{_discount_label(deal)} · {_landed_str(deal)} landed",
    ]
    if deal.historical_floor_value is not None:
        lines.append("⬇ All-time low")
    return "\n".join(lines)


def _landed_str(deal) -> str:
    """All-in landed price, e.g. `€52.00`. Uses the VAT-inclusive effective cost
    when present (so non-EU imports match the effective-cost-based discount); falls
    back to bare item+shipping for legacy/persisted deals."""
    ccy = deal.landed_currency or deal.buyer_currency or deal.currency
    amount = deal.effective_cost if deal.effective_cost is not None else deal.landed_price
    return _money(amount, ccy)


def _cost_line(deal) -> str:
    """`€45.00 + €7.00 ship + ~€9.00 VAT = €52.00 landed` — the money path. The VAT
    term shows only for estimated import VAT."""
    item = deal.buyer_price or deal.price
    item_ccy = deal.buyer_currency or deal.currency
    ship = deal.shipping_buyer_price or deal.shipping_price or 0
    landed_ccy = deal.landed_currency or item_ccy
    out = f"{_money(item, item_ccy)} + {_money(ship, landed_ccy)} ship"
    if deal.vat_estimated and deal.vat_amount:
        out += f" + ~{_money(deal.vat_amount, landed_ccy)} VAT"
    out += f" = {_landed_str(deal)} landed"
    return out


def _seller_line(deal) -> str:
    name = deal.seller_username or "—"
    rating = deal.seller_rating
    rating_str = f" {rating:.1f}%" if isinstance(rating, (int, float)) else ""
    region = deal.shipping_region or deal.ships_from or ""
    return f"{name}{rating_str} · {region}" if region else f"{name}{rating_str}"


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
            parts.append(f"Free over {fm} — basket ≈ {_money(hint.get('subtotal'), cur)}, add {_money(gap, cur)}")
        else:
            parts.append(f"Free shipping over {fm}")
    caps = _tier_caps(hint)
    if caps:
        segs = [(f"{_money(p, cur)} (more)" if cap is None else f"{_money(p, cur)} (≤{cap})") for p, cap in caps]
        country = hint.get("country") or ""
        est = " (est.)" if hint.get("basis") == "weight-est" else ""
        parts.append(f"Ships {country}: " + ", ".join(segs) + est)
    return " · ".join(parts)


def _basket_item_str(item: dict, ccy: str | None) -> str:
    artist = item.get("release_artist")
    title = item.get("release_title") or "?"
    name = f"{artist} – {title}" if artist else title
    cond = condition_short(item.get("media_condition"))
    price = _money(item.get("price"), ccy)
    return f"{name} ({cond}, {price})"


def _basket_phrase(basket: dict) -> tuple[str, str]:
    """(headline, detail) for a combine-shipping recommendation."""
    ccy = basket.get("currency")
    items = ", ".join(_basket_item_str(it, ccy) for it in basket.get("add") or [])
    est = " (est.)" if basket.get("basis") == "weight-est" else ""
    if basket.get("kind") == "free_crossing":
        head = f"Combine to save {_money(basket.get('saving'), ccy)} shipping"
        detail = (f"add {items} to reach {_money(basket.get('free_min'), ccy)} "
                  f"→ free shipping{est}")
        return head, detail
    room = basket.get("room_more") or 0
    head = f"Room for {room} more"
    detail = (f"add {items} before shipping steps "
              f"{_money(basket.get('fee_now'), ccy)} → {_money(basket.get('next_fee'), ccy)}{est}")
    return head, detail


def _pick_badge(pct) -> str:
    """Display form of a pick's signed discount: stored +18 reads −18%; stored −15
    reads +15%; 0 reads ±0%. Empty when None."""
    if pct is None:
        return ""
    if pct > 0:
        return f"−{pct}%"
    return f"+{-pct}%" if pct < 0 else "±0%"


def _session_note(days: int | None) -> tuple[str, str]:
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
        return ""
    if total:
        return f"{scanned} of {total} wantlist releases for sale"
    return f"{scanned} wantlist release(s) for sale"


# ── "The Find" design tokens ───────────────────────────────────────────────────

_WAX = "#3B3228"
_CARD = "#F4EFE3"
_OX = "#7B2D26"
_BRASS = "#B98A2E"
_BRASS_DK = "#8a5e14"
_INK = "#2A2622"
_MUTED = "#6B6157"
_FAINT = "#9a8f7d"
_HAIR = "#D9CDB4"
_GAUGE_BG = "#ECE3CF"
_GAUGE_BD = "#DCD0B8"
_GAUGE_BG_MUTED = "#EAE6DA"
_TRACK = "#DCCFB4"
_AMBER_TX = "#7a5b00"

_SERIF = "Georgia,'Iowan Old Style','Times New Roman',serif"
_COND = "'Arial Narrow','Helvetica Neue Condensed',Arial,sans-serif"
_MONO = "'SF Mono',Consolas,Menlo,monospace"

# Discount (%) at or above which a sold-validated deal earns the loud "Strong buy".
_STRONG_BUY_PCT = 20

# Sticker palettes: (bg, border, text, hollow). Solid brass = trust it; solid
# oxblood = the best-price card; hollow amber = check something first; hollow grey
# = no data to judge.
_STICKER = {
    "brass": ("#B98A2E", "#8a6516", "#16130F", False),
    "oxblood": ("#7B2D26", "#5a1f1a", "#F4EFE3", False),
    "amber": ("#F7F1E4", "#C9912E", "#9a6a12", True),
    "neutral": ("#F7F1E4", "#A39A88", "#6f675a", True),
}


def _verdict_kind(deal) -> tuple[str, str, str]:
    """(sticker_kind, verdict_word, gauge_mode) for a deal's confidence.

    sticker_kind ∈ {brass, amber, neutral}; gauge_mode ∈ {sold, muted, none}.
    The worst-applying flag wins the sticker, so a deal can never look more
    trustworthy than its weakest signal."""
    if deal.deal_source == "remote_only":
        return ("neutral", "Only copy", "none")
    if getattr(deal, "detached_low", False):
        return ("amber", "Check first", "muted")
    if deal.sold_tier_caveat:
        return ("amber", "Check grade", "muted" if deal.low_confidence else "sold")
    if deal.low_confidence:
        return ("amber", "Worth a look", "muted")
    word = "Strong buy" if (deal.discount_pct or 0) >= _STRONG_BUY_PCT else "Good price"
    return ("brass", word, "sold")


def _sticker_html(deal, kind: str, word: str) -> str:
    """The grading-sticker on the cover corner: VERDICT / grade · €landed."""
    bg, bd, fg, hollow = _STICKER[kind]
    border = f"2px solid {bd}" if hollow else f"1px solid {bd}"
    grade = condition_short(deal.media_condition)
    # Auto-width (inline-block) so the sticker hugs its text — a short price no
    # longer leaves it floating, and a long one still fits.
    return (
        f'<div style="display:inline-block; margin:-28px 0 0 40px; transform:rotate(-4deg); '
        f'background:{bg}; border:{border}; box-shadow:0 4px 9px rgba(0,0,0,.35); padding:6px 11px;">'
        f'<div style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.1em; '
        f'font-size:12px; font-weight:bold; color:{fg};">{_h(word)}</div>'
        f'<div style="font-family:{_MONO}; font-size:14px; font-weight:bold; color:{fg}; '
        f'margin-top:2px;">{_h(grade)} &middot; {_landed_str(deal)}</div>'
        f'</div>'
    )


def _cover_html(deal) -> str:
    """Real cover when present; a styled dark placeholder otherwise (email clients
    block images by default, so the fallback must read on its own)."""
    if deal.image_url:
        return (
            f'<img src="{_h(deal.image_url)}" alt="" width="150" height="150" '
            f'style="display:block; width:150px; height:150px; object-fit:cover; '
            f'background:#1E1A14; border:1px solid #3a3327;">'
        )
    return (
        f'<div style="width:150px; height:150px; background:#1E1A14; border:1px solid #3a3327; '
        f'box-sizing:border-box; padding:15px;">'
        f'<div style="font-family:{_SERIF}; font-size:14px; line-height:1.25; color:#E8E0D0;">'
        f'{_h(deal.release_title or "")}</div>'
        f'<div style="font-family:{_SERIF}; font-style:italic; font-size:11px; color:#9a907f; '
        f'margin-top:6px;">{_h(deal.release_artist or "")}</div></div>'
    )


def _provenance(deal) -> str:
    """The data behind the gauge: `9 VG+ sales · last sold Apr 2026`, or an honest
    'asking prices' note when there are no real sales."""
    grade = condition_short(deal.media_condition)
    if deal.deal_source == "below_asking_median":
        return "based on asking prices · no recent sales"
    n = deal.sold_data_points
    if n:
        last = f" · last sold {deal.sold_last_date}" if deal.sold_last_date else ""
        return f"{n} {grade} sale{'s' if n != 1 else ''}{last}"
    return ""


def _bar_pct(value, lo, hi):
    if value is None or lo is None or hi is None or hi <= lo:
        return None
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def _gauge_bar(landed, lo, hi, median) -> str:
    """A percentage-width bar plotting the landed price on the sold range. The brass
    span is the *saving* — your price up to the typical (median) — bracketed by two
    markers: ink at your price, oxblood (red) at the median.

    Each marker is a fixed 8px-wide column carried across three rows — a down
    triangle, the bar band, an up triangle. Sharing one table column means the three
    pieces are aligned by the grid (they can't drift) and connect at the bar edges."""
    you = _bar_pct(landed, lo, hi)
    if you is None:
        return ""
    med = _bar_pct(median, lo, hi)
    has_med = med is not None and med > you
    # mark tuples carry the line colour plus its left/right flank fills, so the track
    # and brass run right up to the slim line (no empty gap around the marker).
    if has_med:
        cols = [("fill", you, _TRACK, "L"),
                ("mark", _INK, _TRACK, _BRASS),
                ("fill", med - you, _BRASS, ""),
                ("mark", _OX, _BRASS, _TRACK),
                ("fill", 100 - med, _TRACK, "R")]
    else:
        cols = [("fill", you, _TRACK, "L"),
                ("mark", _INK, _TRACK, _TRACK),
                ("fill", 100 - you, _TRACK, "R")]

    def _tri(color, edge):
        # An 8px-wide, 7px-tall filled triangle that exactly fills the 8px marker
        # column (so it sits dead-centre over the line beneath/above it).
        return (f'<div style="width:0; height:0; margin:0 auto; border-left:4px solid transparent; '
                f'border-right:4px solid transparent; {edge}:7px solid {color};"></div>')

    def _bar_mark(line_color, left_fill, right_fill):
        # 2px flank · 4px line · 2px flank — flanks coloured as the neighbours so the
        # fills meet the line with no gap; the 8px total keeps the triangles aligned.
        return (
            f'<td style="width:8px; height:14px; padding:0;">'
            f'<table role="presentation" width="8" cellpadding="0" cellspacing="0" style="width:8px; height:14px;">'
            f'<tr style="height:14px;">'
            f'<td style="width:2px; background:{left_fill}; height:14px;"></td>'
            f'<td style="width:4px; background:{line_color}; height:14px;"></td>'
            f'<td style="width:2px; background:{right_fill}; height:14px;"></td>'
            f'</tr></table></td>'
        )

    def _row(kind):
        tds = []
        for c in cols:
            if c[0] == "mark":
                _, line_color, left_fill, right_fill = c
                if kind == "bar":
                    tds.append(_bar_mark(line_color, left_fill, right_fill))
                elif kind == "top":
                    tds.append(f'<td style="width:8px; vertical-align:bottom;">{_tri(line_color, "border-top")}</td>')
                else:
                    tds.append(f'<td style="width:8px; vertical-align:top;">{_tri(line_color, "border-bottom")}</td>')
            else:
                _, w, color, side = c
                if kind == "bar":
                    radius = (" border-radius:7px 0 0 7px;" if side == "L"
                              else " border-radius:0 7px 7px 0;" if side == "R" else "")
                    tds.append(f'<td style="width:{w:.1f}%; background:{color}; height:14px;{radius}"></td>')
                else:
                    tds.append(f'<td style="width:{w:.1f}%;"></td>')
        h = "14px" if kind == "bar" else "7px"
        return f'<tr style="height:{h};">{"".join(tds)}</tr>'

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;">'
        f'{_row("top")}{_row("bar")}{_row("bottom")}</table>'
    )


def _gauge_html(deal, mode: str) -> str:
    """The market gauge panel. `sold` plots the all-in price on the real sold range;
    `muted` (asking / sparse / suspect) drops the bar and de-emphasises the figure;
    `none` (remote-only) renders nothing (the card shows a neutral note instead)."""
    if mode == "none":
        return ""
    ccy = deal.landed_currency or deal.buyer_currency or deal.currency
    landed = deal.effective_cost if deal.effective_cost is not None else deal.landed_price
    muted = mode == "muted"
    median = deal.sold_median_value or deal.median_value
    pct = deal.discount_pct
    if pct is not None:
        pct_str = (f"~&minus;{pct}%" if muted else f"&minus;{pct}%")
    else:
        pct_str = "DEAL"
    pct_color = "#b07d1e" if muted else _BRASS_DK
    pct_size = "22px" if muted else "29px"
    vs_word = "asking" if deal.deal_source == "below_asking_median" else "typical"
    vs = f"vs {_money(median, ccy)} {vs_word}" if median else "rough estimate"
    prov = _provenance(deal)
    bar = "" if muted else _gauge_bar(landed, deal.sold_low_value, deal.sold_high_value, deal.sold_median_value)
    range_html = ""
    if bar:
        range_html = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="font-family:{_MONO}; font-size:10px; margin-top:10px; color:{_FAINT};"><tr>'
            f'<td style="text-align:left;">{_money(deal.sold_low_value, ccy)}</td>'
            f'<td style="text-align:center; color:{_MUTED};">median {_money(deal.sold_median_value, ccy)}</td>'
            f'<td style="text-align:right;">{_money(deal.sold_high_value, ccy)}</td></tr></table>'
        )
    prov_html = (
        f'<div style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.14em; '
        f'font-size:9px; color:{_FAINT};">{_h(prov)}</div>' if prov else ""
    )
    right = prov_html + bar + range_html
    if not right:
        right = (f'<div style="font-family:{_COND}; font-size:12px; color:{_FAINT};">'
                 f'rough estimate &mdash; thin data</div>')
    panel_bg = _GAUGE_BG_MUTED if muted else _GAUGE_BG
    return (
        f'<div style="background:{panel_bg}; border:1px solid {_GAUGE_BD}; border-radius:8px; '
        f'padding:13px 15px; margin-top:14px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="96" valign="middle" style="width:96px; border-right:1px solid #D7C9AE; padding-right:14px;">'
        f'<div style="font-family:{_MONO}; font-size:{pct_size}; font-weight:bold; color:{pct_color}; line-height:1;">{pct_str}</div>'
        f'<div style="font-family:{_COND}; font-size:13px; color:{_INK}; margin-top:6px;">you pay <strong>{_landed_str(deal)}</strong></div>'
        f'<div style="font-family:{_COND}; font-size:12px; color:#8a8073;">{_h(vs)}</div>'
        f'</td>'
        f'<td valign="middle" style="padding-left:15px;">{right}</td>'
        f'</tr></table>'
        f'<div style="border-top:1px solid #D7C9AE; margin-top:11px; padding-top:10px; '
        f'font-family:{_MONO}; font-size:13px; color:{_INK};">{_h(_cost_line(deal))}</div>'
        f'</div>'
    )


def _neutral_note_html(deal) -> str:
    """Replaces the gauge for a lone Discogs-flagged listing: nothing to compare to."""
    return (
        f'<div style="background:#EEE7D8; border:1px solid {_GAUGE_BD}; border-radius:8px; '
        f'padding:12px 14px; margin-top:14px; font-family:{_COND}; font-size:13px; line-height:1.45; color:#5f574a;">'
        f'Only copy on the marketplace, and no recent sales to compare against &mdash; Discogs itself '
        f'flagged it. There&rsquo;s no median to hold it to, so the call is yours.</div>'
        f'<div style="margin-top:10px; font-family:{_MONO}; font-size:13px; color:{_INK};">{_h(_cost_line(deal))}</div>'
    )


def _callout(kind: str, lead: str, text: str) -> str:
    if kind == "positive":
        # All-time low: a distinct solid-brass ribbon, never a caution band.
        return (
            f'<div style="background:{_BRASS}; border-radius:6px; padding:8px 12px; margin-top:12px;">'
            f'<span style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.08em; '
            f'font-size:11px; font-weight:bold; color:#16130F;">{_h(lead)}</span> '
            f'<span style="font-family:{_COND}; font-size:12.5px; color:#3a2e10;">{_h(text)}</span></div>'
        )
    return (
        f'<div style="background:#FBEBCB; border-left:3px solid #C9912E; border-radius:4px; '
        f'padding:8px 12px; margin-top:12px;">'
        f'<div style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.07em; '
        f'font-size:11px; font-weight:bold; color:{_AMBER_TX};">{_h(lead)}</div>'
        f'<div style="font-family:{_COND}; font-size:12.5px; line-height:1.45; color:{_AMBER_TX}; '
        f'margin-top:1px;">{_h(text)}</div></div>'
    )


def _callouts_html(deal) -> str:
    """Loud-only-when-they-fire signals: the positive all-time-low ribbon, then any
    cautions (better-grade, verify, asking-only)."""
    out = []
    if deal.historical_floor_pct is not None:
        out.append(_callout("positive", "↓ All-time low",
                            "The cheapest we've seen in 90 days. If it's on your list, this is the moment."))
    if deal.sold_tier_caveat and deal.sold_tier_caveat_grade:
        ccy = deal.sold_median_currency or deal.landed_currency
        out.append(_callout(
            "caution", "Check the grade",
            f"A {deal.sold_tier_caveat_grade} copy sells for about "
            f"{_money(deal.sold_tier_caveat_value, ccy)} — this "
            f"{condition_short(deal.media_condition)} discount is measured against worse-condition copies.",
        ))
    if getattr(deal, "detached_low", False):
        out.append(_callout(
            "caution", "Verify before buying",
            "Far below the other copies — likely a different pressing or a grading slip. "
            "Check the photos and the seller's notes first.",
        ))
    elif deal.deal_source == "below_asking_median" and not deal.sold_tier_caveat:
        out.append(_callout(
            "caution", "Heads up",
            "No recent sales — this is compared to asking prices, which run high. "
            "A rough guide, not a proven deal.",
        ))
    return "".join(out)


def _combine_html(deal) -> str:
    """The 'more from this seller' box: free-ship nudge + fee tiers + ≥5 wantlist
    picks ordered by deal-ness. Shown only when the seller has other wantlist items
    (so the tiers appear only where combining is actually possible)."""
    hint = deal.shipping_hint
    picks = deal.seller_picks or []
    others = deal.seller_total_others or 0
    if not others and not picks:
        return ""
    seller = _h((hint or {}).get("seller") or deal.seller_username or "this seller")
    rows = [
        f'<div style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.1em; '
        f'font-size:11px; font-weight:bold; color:#4a4339;">'
        f'<span style="color:{_BRASS};">&#9642;</span>&nbsp; More from {seller} on your wantlist</div>'
    ]
    if hint:
        summ = _shipping_summary(hint)
        if summ:
            rows.append(f'<div style="font-family:{_COND}; font-size:12px; color:{_OX}; margin-top:3px;">{_h(summ)}</div>')
    if getattr(deal, "basket", None):
        head, detail = _basket_phrase(deal.basket)
        rows.append(f'<div style="font-family:{_COND}; font-size:12px; color:{_OX}; margin-top:3px;">'
                    f'<strong>{_h(head)}</strong> — {_h(detail)}</div>')
    pick_rows = []
    for p in picks:
        amt = _money(p.get("buyer_price"), p.get("buyer_currency"))
        cond = condition_short(p.get("media_condition"))
        title = _h((f'{p.get("release_artist")} – ' if p.get("release_artist") else "") + (p.get("release_title") or "?"))
        url = _h(p.get("listing_url") or "#")
        pct = p.get("discount_pct")
        badge = _pick_badge(pct)
        color = _BRASS_DK if (pct or 0) > 0 else _FAINT
        pick_rows.append(
            f'<tr><td width="50" valign="top" style="font-family:{_MONO}; font-size:12px; font-weight:bold; color:{color}; padding-top:6px;">{badge}</td>'
            f'<td valign="top" style="font-family:{_COND}; font-size:13px; color:#4a4339; padding-top:6px;">'
            f'<strong style="font-family:{_MONO};">{amt}</strong> &middot; {cond} &middot; '
            f'<a href="{url}" style="color:#4a4339; text-decoration:none;">{title}</a></td></tr>'
        )
    if pick_rows:
        rows.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{"".join(pick_rows)}</table>')
    if others > len(picks):
        rows.append(f'<div style="font-family:{_COND}; font-size:12px; color:{_FAINT}; margin-top:9px;">+ {others - len(picks)} more on your wantlist</div>')
    return (
        f'<div style="background:#FBF8F0; border:1px dashed #D7C9AE; border-radius:8px; '
        f'padding:13px 15px; margin-top:14px;">{"".join(rows)}</div>'
    )


def _best_alt_note(primary, alt) -> str:
    """`−€8, better grade & free shipping` — why the alt wins, for its meta line."""
    bits = []
    ccy = alt.landed_currency or alt.buyer_currency
    p_landed = primary.effective_cost if primary.effective_cost is not None else primary.landed_price
    a_landed = alt.effective_cost if alt.effective_cost is not None else alt.landed_price
    if p_landed is not None and a_landed is not None:
        diff = a_landed - p_landed
        if diff < 0:
            bits.append(f"&minus;{_money(-diff, ccy)}")     # cheaper
        elif diff > 0:
            bits.append(f"+{_money(diff, ccy)}")            # dearer (runner-up)
    pr = evaluator._CONDITION_RANK.get(primary.media_condition or "", 0)
    ar = evaluator._CONDITION_RANK.get(alt.media_condition or "", 0)
    if ar > pr:
        bits.append("better grade")
    elif ar < pr:
        bits.append("lower grade")
    else:
        bits.append("same grade")
    if not (alt.shipping_buyer_price or alt.shipping_price or 0):
        bits.append("free shipping")
    return ", ".join(bits)


def _cta_html(deal) -> str:
    return (
        f'<a href="{_h(deal.listing_url or "#")}" style="display:inline-block; font-family:{_COND}; '
        f'text-transform:uppercase; letter-spacing:.09em; font-size:12px; font-weight:bold; color:{_CARD}; '
        f'background:{_OX}; padding:7px 15px; border-radius:4px; text-decoration:none; white-space:nowrap;">'
        f'View listing&nbsp;&rarr;</a>'
    )


def _card_html(deal, *, variant: str = "find", primary=None) -> str:
    """One deal card. `variant="best"` is the oxblood best-price-for-this-record card
    (with a savings note vs the primary); else the find card with its verdict
    sticker. Each card carries its own gauge, comment, seller line and combine box."""
    kind, word, gauge_mode = _verdict_kind(deal)
    chip = ""
    if variant == "best":
        sticker = _sticker_html(deal, "oxblood", "Best price")
        eyebrow = "&#9670;&nbsp; Best price for this record"
        left_border = f" border-left:5px solid {_OX};"
        meta = _h(_meta_line(deal))
        note = _best_alt_note(primary, deal) if primary else ""
        if note:
            meta += f' &nbsp;&middot;&nbsp; <span style="color:{_OX}; font-weight:bold;">{note}</span>'
    elif variant == "next":
        # Runner-up skin: neutral sticker, grey rail — you're already cheaper, this
        # is shown for context, so it must never look like a call to act.
        sticker = _sticker_html(deal, "neutral", "Next best")
        eyebrow = "&#9671;&nbsp; Next best copy"
        left_border = " border-left:5px solid #A39A88;"
        meta = _h(_meta_line(deal))
        note = _best_alt_note(primary, deal) if primary else ""
        if note:
            meta += f' &nbsp;&middot;&nbsp; <span style="color:#6f675a; font-weight:bold;">{note}</span>'
    else:
        sticker = _sticker_html(deal, kind, word)
        eyebrow = "Today's find"
        left_border = ""
        meta = _h(_meta_line(deal))
        chip = _cheapest_chip_html(deal)

    body = _neutral_note_html(deal) if gauge_mode == "none" else _gauge_html(deal, gauge_mode)
    callouts = _callouts_html(deal)
    comment = ""
    c = (deal.comments or "").strip()
    if c:
        comment = (
            f'<div style="border-left:2px solid #C9A24A; padding-left:11px; font-family:{_SERIF}; '
            f'font-style:italic; font-size:13px; line-height:1.4; color:{_MUTED}; margin-top:13px;">'
            f'&ldquo;{_h(c)}&rdquo;</div>'
        )
    combine = _combine_html(deal)

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_CARD}; border-radius:10px;{left_border}">'
        f'<tr><td style="padding:24px 26px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="162" valign="top" style="width:162px;">{_cover_html(deal)}{sticker}</td>'
        f'<td valign="top" style="padding-left:24px;">'
        f'<div style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.22em; '
        f'font-size:10px; font-weight:bold; color:{_OX};">{eyebrow}</div>'
        f'<div style="font-family:{_SERIF}; font-size:21px; line-height:1.2; color:{_INK}; margin-top:6px;">{_h(_identity(deal))}</div>'
        f'<div style="font-family:{_COND}; font-size:13px; letter-spacing:.03em; color:{_MUTED}; margin-top:6px;">{meta}</div>'
        f'{chip}{body}{comment}{callouts}'
        f'</td></tr></table>'
        f'<div style="border-top:1px solid {_HAIR}; margin-top:16px;"></div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px;"><tr>'
        f'<td valign="middle"><div style="font-family:{_COND}; font-size:14px; letter-spacing:.02em; color:#4a4339;">{_h(_seller_line(deal))}</div></td>'
        f'<td valign="middle" align="right">{_cta_html(deal)}</td>'
        f'</tr></table>'
        f'{combine}'
        f'</td></tr></table>'
    )


def _connector_html(beats: bool = True) -> str:
    text = (
        "Heads up &mdash; for this record, a better copy is on sale right now:"
        if beats else
        "You&rsquo;ve got the cheapest &mdash; here&rsquo;s the runner-up, so you can see what you&rsquo;re beating:"
    )
    return (
        f'<div style="font-family:{_SERIF}; font-style:italic; font-size:15px; color:#c2b8a2;">'
        f'&darr;&nbsp; {text}</div>'
    )


def _cheapest_chip_html(deal) -> str:
    """Green reassurance chip on the find card: this copy is the cheapest of the
    live field (or the only copy). Renders only under an authoritative live fetch."""
    if not (getattr(deal, "market_authoritative", False) and getattr(deal, "is_cheapest", False)):
        return ""
    n = deal.market_total or 0
    label = "Only copy listed right now" if n <= 1 else f"&#10003; Cheapest of {n} listed"
    return (
        f'<div style="display:inline-block; font-family:{_COND}; font-size:11px; font-weight:bold; '
        f'color:#3a6a30; background:#e6efe0; border:1px solid #b8ccb0; border-radius:20px; '
        f'padding:2px 9px; margin-top:8px;">{label}</div>'
    )


def _ladder_html(deal) -> str:
    """The 'rest of the field' box: every other live copy of this record, ranked,
    with below-floor copies dimmed and tagged so a cheap wrong-grade copy can't pose
    as an alternative. Capped, with a '+K more' overflow line."""
    copies = deal.market_copies or []
    if not copies:
        return ""
    cap = 4
    shown = copies[:cap]
    ccy = deal.landed_currency or deal.buyer_currency
    n = deal.market_total or 0
    rng = ""
    if deal.market_low is not None and deal.market_high is not None:
        rng = f"{_money(deal.market_low, ccy)}&ndash;{_money(deal.market_high, ccy)}"
    header = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.12em; '
        f'font-size:10px; color:{_FAINT};">&#9642; The rest of the field</td>'
        f'<td style="text-align:right; font-family:{_COND}; text-transform:uppercase; '
        f'letter-spacing:.12em; font-size:10px; color:{_FAINT};">{n} copies{" &middot; " + rng if rng else ""}</td>'
        f'</tr></table>'
    )
    row_html = []
    for r in shown:
        amt = _money(r["effective_cost"], r["currency"]) if r.get("effective_cost") is not None else "+ ship?"
        grade = f'{condition_short(r["media_condition"])}/{condition_short(r["sleeve_condition"])}'
        below = r.get("below_floor")
        color = "#9a8f7d" if below else "#4a4339"
        tag = (f' &middot; <span style="color:#b06a1e;">below your floor</span>') if below else ""
        url = _h(r.get("listing_url") or "#")
        row_html.append(
            f'<tr><td width="66" valign="top" style="font-family:{_MONO}; font-size:12.5px; '
            f'color:{color}; padding-top:4px;">{amt}</td>'
            f'<td width="74" valign="top" style="font-family:{_MONO}; font-size:12px; color:{color}; padding-top:4px;">{grade}</td>'
            f'<td valign="top" style="font-family:{_COND}; font-size:12.5px; color:{color}; padding-top:4px;">'
            f'<a href="{url}" style="color:{color}; text-decoration:none;">{_h(r.get("seller_username") or "?")}</a>{tag}</td></tr>'
        )
    overflow = ""
    if len(copies) > cap:
        overflow = (f'<div style="font-family:{_COND}; font-size:12px; color:{_FAINT}; margin-top:7px;">'
                    f'+ {len(copies) - cap} more</div>')
    return (
        f'<div style="background:#FBF8F0; border:1px dashed #D7C9AE; border-radius:8px; padding:11px 14px;">'
        f'{header}<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:5px;">'
        f'{"".join(row_html)}</table>{overflow}</div>'
    )


def _deal_html(deal) -> str:
    """A deal block: the find card, plus (when a cheaper still-listed copy exists)
    a connector and the best-price card. Returns one or more <tr> rows."""
    rows = [f'<tr><td>{_card_html(deal, variant="find")}</td></tr>']
    if getattr(deal, "market_authoritative", False):
        # Authoritative live field: always show the best OTHER offer (oxblood
        # "Best price" when it beats, neutral "Next best" when you're cheaper),
        # then the rest-of-field ladder.
        if deal.best_alt is not None:
            beats = not deal.is_cheapest
            rows.append(f'<tr><td style="padding:16px 12px 12px;">{_connector_html(beats)}</td></tr>')
            variant = "best" if beats else "next"
            rows.append(f'<tr><td>{_card_html(deal.best_alt, variant=variant, primary=deal)}</td></tr>')
        ladder = _ladder_html(deal)
        if ladder:
            rows.append(f'<tr><td style="padding:10px 12px 0;">{ladder}</td></tr>')
    elif deal.best_alt is not None:
        # Fallback (live market off / fetch missed): the old evaluated-deals best_alt.
        rows.append(f'<tr><td style="padding:16px 12px 12px;">{_connector_html()}</td></tr>')
        rows.append(f'<tr><td>{_card_html(deal.best_alt, variant="best", primary=deal)}</td></tr>')
    rows.append('<tr><td style="height:22px; line-height:22px; font-size:0;">&nbsp;</td></tr>')
    return "".join(rows)


def _chapter_divider_html(label: str) -> str:
    """A numbered rule on the backdrop that separates one deal (find + best/next +
    ladder) from the next — a clear 'new find starts here' chapter break."""
    edge = 'border-bottom:1px solid #6b5d49; height:9px; font-size:0; line-height:0;'
    return (
        '<tr><td style="padding:6px 18px 20px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="{edge}">&nbsp;</td>'
        f'<td style="white-space:nowrap; padding:0 14px; vertical-align:middle; font-family:{_COND}; '
        f'text-transform:uppercase; letter-spacing:.22em; font-size:11px; font-weight:bold; color:{_BRASS};">'
        f'&#9670;&nbsp; {_h(label)} &nbsp;&#9670;</td>'
        f'<td style="{edge}">&nbsp;</td>'
        '</tr></table></td></tr>'
    )


def _banner_html(n: int, run_str: str, session_days_left, scan_counts) -> str:
    session_label, session_color = _session_note(session_days_left)
    meta = run_str
    if session_label:
        meta += f" &middot; {session_label}"
    s = "s" if n != 1 else ""
    overlay = (
        f'<div style="grid-row:1; grid-column:1; align-self:stretch; padding:24px 26px; '
        f'background:linear-gradient(90deg,rgba(9,7,5,.88) 0%,rgba(9,7,5,.55) 50%,rgba(9,7,5,.08) 100%);">'
        f'<div style="font-family:{_COND}; text-transform:uppercase; letter-spacing:.34em; '
        f'font-size:11px; font-weight:bold; color:#D6A93C;">&#9673;&nbsp;&nbsp;The Watcher</div>'
        f'<div style="font-family:{_SERIF}; font-size:30px; line-height:1.1; color:#F7F2E7; '
        f'margin-top:11px; text-shadow:0 2px 6px rgba(0,0,0,.55);">{n} find{s} on your wantlist</div>'
        f'<div style="font-family:{_COND}; font-size:12px; letter-spacing:.04em; color:#D7CBB4; margin-top:9px;">{_h(meta)}</div>'
        f'</div>'
    )
    if _HEADER_IMG_DATA_URI:
        img = (f'<img src="{_HEADER_IMG_DATA_URI}" alt="" width="680" height="150" '
               f'style="grid-row:1; grid-column:1; display:block; width:100%; height:150px; object-fit:cover;">')
        return f'<div style="display:grid; background:{_WAX}; border-radius:10px; overflow:hidden;">{img}{overlay}</div>'
    return f'<div style="background:{_WAX}; border-radius:10px; overflow:hidden;">{overlay}</div>'


def _footer_html() -> str:
    return (
        f'<div style="font-family:{_COND}; font-size:11px; line-height:1.65; color:#6b6358;">'
        f'<strong style="color:{_FAINT};">−%</strong> = your full delivered cost vs the typical sold price '
        f'(median of real sales). <strong style="color:{_FAINT};">Landed</strong> = record + shipping '
        f'(+ est. import VAT for non-EU). <strong style="color:{_FAINT};">Best price for this record</strong> '
        f'= the cheapest copy on sale at this grade or better. Sticker colour shows how much to trust the price: '
        f'brass = real sales back it, amber = check first, grey = no data.</div>'
    )


def _build_subject(deals: list[Deal]) -> str:
    """Honest subject: lead with the find, and flag a cheaper better copy when one
    exists, so the inbox glance can't mislead either."""
    top = deals[0]
    head = _identity(top)
    grade = condition_short(top.media_condition)
    subject = f"[Discogs Watcher] {head} {grade} {_landed_str(top)}"
    if top.best_alt is not None:
        alt = top.best_alt
        subject += f" — but {condition_short(alt.media_condition)} is {_landed_str(alt)}"
    extra = len(deals) - 1
    if extra:
        subject += f" (+{extra} more)"
    return subject


def _build_html(
    deals: list[Deal], run_time: datetime, extra_count: int,
    session_days_left: int | None = None,
    scan_counts: dict | None = None,
) -> str:
    run_str = run_time.strftime("%d %b %Y · %H:%M UTC")
    banner = _banner_html(len(deals), run_str, session_days_left, scan_counts)
    total = len(deals)
    parts = []
    for i, d in enumerate(deals):
        if i > 0:
            parts.append(_chapter_divider_html(f"Find {i + 1} of {total}"))
        parts.append(_deal_html(d))
    blocks = "".join(parts)
    extra = ""
    if extra_count > 0:
        extra = (f'<tr><td style="padding:2px 12px 14px;"><div style="font-family:{_COND}; '
                 f'font-size:13px; color:#8f8674;">+{extra_count} more deal(s) not shown.</div></td></tr>')
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0; padding:0; background:{_WAX};">
  <div style="background:{_WAX}; padding:30px 16px;">
    <table role="presentation" width="680" align="center" cellpadding="0" cellspacing="0" style="width:680px; max-width:100%; margin:0 auto;">
      <tr><td style="padding:0 0 22px;">{banner}</td></tr>
      {blocks}
      {extra}
      <tr><td style="padding:2px 12px 8px;">{_footer_html()}</td></tr>
    </table>
  </div>
</body></html>"""


# ── Plain-text alternative ─────────────────────────────────────────────────────

def _text_card(deal, *, is_alt: bool = False, primary=None, runner_up: bool = False) -> list[str]:
    lines = []
    _, word, _ = _verdict_kind(deal)
    if is_alt:
        note = _best_alt_note(primary, deal) if primary else ""
        label = "NEXT BEST" if runner_up else "BEST PRICE FOR THIS RECORD"
        lines.append(f"  ↳ {label}" + (f" ({note.replace('&minus;', '−')})" if note else ""))
    lines.append(_heading(deal))
    head = ("Next best" if runner_up else "Best price") if is_alt else word
    primary_line = f"{head} · {_discount_label(deal)} · {_landed_str(deal)} landed"
    lines.append(primary_line)
    if not is_alt and getattr(deal, "market_authoritative", False) and getattr(deal, "is_cheapest", False):
        n = deal.market_total or 0
        lines.append(f"✓ Cheapest of {n} listed" if n > 1 else "Only copy listed right now")
    prov = _provenance(deal)
    if deal.sold_median_value is not None:
        ccy = deal.sold_median_currency or deal.landed_currency
        proof = f"SOLD median {_money(deal.sold_median_value, ccy)}"
        if deal.sold_low_value is not None and deal.sold_high_value is not None:
            proof += f" · {_money(deal.sold_low_value, ccy)}–{_money(deal.sold_high_value, ccy)}"
        if prov:
            proof += f" · {prov}"
        lines.append(proof)
    elif prov:
        lines.append(prov)
    lines.append(_cost_line(deal))
    # Caveats / signals, plain.
    if deal.historical_floor_pct is not None:
        lines.append("↓ all-time low — cheapest in 90 days")
    if deal.sold_tier_caveat and deal.sold_tier_caveat_grade:
        ccy = deal.sold_median_currency or deal.landed_currency
        lines.append(f"⚠ a {deal.sold_tier_caveat_grade} copy sells ~{_money(deal.sold_tier_caveat_value, ccy)} — check the grade")
    if getattr(deal, "detached_low", False):
        lines.append("⚠ far below other copies — verify the pressing/grade")
    if deal.deal_source == "below_asking_median":
        lines.append("⚠ no recent sales — based on asking prices (less reliable)")
    if deal.deal_source == "remote_only":
        lines.append("only copy on the marketplace · flagged by Discogs · no sold history")
    lines.append(_seller_line(deal))
    if deal.comments:
        lines.append(f'"{deal.comments}"')
    lines.append(deal.listing_url or "")
    lines.extend(_combine_text(deal))
    return lines


def _combine_text(deal) -> list[str]:
    hint = deal.shipping_hint
    picks = deal.seller_picks or []
    others = deal.seller_total_others or 0
    if not others and not picks:
        return []
    seller = (hint or {}).get("seller") or deal.seller_username or "this seller"
    out = [f"More from {seller} on your wantlist:"]
    if hint:
        summ = _shipping_summary(hint)
        if summ:
            out.append(f"  {summ}")
    if getattr(deal, "basket", None):
        head, detail = _basket_phrase(deal.basket)
        out.append(f"  {head}: {detail}")
    for p in picks:
        amt = _money(p.get("buyer_price"), p.get("buyer_currency"))
        cond = condition_short(p.get("media_condition"))
        title = (f'{p.get("release_artist")} – ' if p.get("release_artist") else "") + (p.get("release_title") or "?")
        badge = _pick_badge(p.get("discount_pct"))
        badge = f"{badge} · " if badge else ""
        out.append(f"  + {amt} · {badge}{cond} · {title} · {p.get('listing_url', '')}")
    if others > len(picks):
        out.append(f"  +{others - len(picks)} more on your wantlist")
    return out


def _ladder_text(deal) -> list[str]:
    """Plain-text 'rest of the field' mirror of _ladder_html."""
    copies = deal.market_copies or []
    if not copies:
        return []
    ccy = deal.landed_currency or deal.buyer_currency
    rng = ""
    if deal.market_low is not None and deal.market_high is not None:
        rng = f", {_money(deal.market_low, ccy)}–{_money(deal.market_high, ccy)}"
    out = [f"Rest of the field ({deal.market_total or 0} listed{rng}):"]
    for r in copies[:4]:
        amt = _money(r["effective_cost"], r["currency"]) if r.get("effective_cost") is not None else "+ship?"
        grade = f'{condition_short(r["media_condition"])}/{condition_short(r["sleeve_condition"])}'
        tag = " · below your floor" if r.get("below_floor") else ""
        out.append(f"  {amt} · {grade} · {r.get('seller_username', '?')}{tag} · {r.get('listing_url', '')}")
    if len(copies) > 4:
        out.append(f"  +{len(copies) - 4} more")
    return out


def _build_text(
    deals: list[Deal], run_time: datetime, extra_count: int,
    session_days_left: int | None = None,
    scan_counts: dict | None = None,
) -> str:
    run_str = run_time.strftime("%d %b %Y · %H:%M UTC")
    session_label, _ = _session_note(session_days_left)
    s = "s" if len(deals) != 1 else ""
    header = f"The Watcher · {len(deals)} find{s} on your wantlist · {run_str}"
    if session_label:
        header += f" · {session_label}"
    lines = [header, "=" * 64]
    total = len(deals)
    for i, d in enumerate(deals):
        if i > 0:
            lines += ["", f"{'─' * 20}  Find {i + 1} of {total}  {'─' * 20}"]
        lines.append("")
        lines.extend(_text_card(d))
        if d.best_alt is not None:
            lines.append("")
            runner = bool(getattr(d, "market_authoritative", False) and getattr(d, "is_cheapest", False))
            lines.extend(_text_card(d.best_alt, is_alt=True, primary=d, runner_up=runner))
        lines.extend(_ladder_text(d))
    lines.append("-" * 64)
    if extra_count > 0:
        lines.append(f"\n+{extra_count} more deal(s) not shown.")
    return "\n".join(lines)
