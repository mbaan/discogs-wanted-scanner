"""
Notification layer.

EmailNotifier sends digest emails (one per flush) and admin alerts (cookie
expiry, watcher health). The Notifier base class is the extension point for
a future push client.
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

from evaluator import condition_short, currency_symbol
from models import Deal

logger = logging.getLogger(__name__)

# Header banner: a 680×150 JPEG embedded as a base64 data-URI <img> (not a CSS
# background-image — ProtonMail strips those). Loaded once at import. If the
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
    def send(self, deals: list[Deal], run_time: datetime, **kwargs) -> None:
        """Send a batch alert for the given deals."""


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


# ── Rendering ────────────────────────────────────────────────────────────────

def _h(s: str | None) -> str:
    return _html.escape(s) if s else ""


def _money(amount: float | int | None, ccy: str | None) -> str:
    if amount is None:
        return "—"
    return f"{currency_symbol(ccy)}{float(amount):.2f}"


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
    """Hero discount string: `−45%`, or `DEAL` when there's no computed discount
    (solo listing that only fired on Discogs' own flag)."""
    if deal.discount_pct is not None:
        return f"−{deal.discount_pct}%"
    return "DEAL"


def _build_subject(deals: list[Deal]) -> str:
    """Lead the subject with the strongest deal — discount, record, price — so
    the hook survives inbox truncation. Deals arrive deepest-discount-first, so
    `deals[0]` is the headline; remaining ones become a `+N more` tail. The
    `[Discogs Watcher]` prefix stays put for mail filters."""
    top = deals[0]
    title = top.release_title or "Unknown"
    head = f"{top.release_artist} — {title}" if top.release_artist else title
    price = _money(top.buyer_price, top.buyer_currency)
    lead = f"−{top.discount_pct}% off" if top.discount_pct is not None else "Deal"
    subject = f"[Discogs Watcher] {lead}: {head} · {price}"
    extra = len(deals) - 1
    if extra:
        subject += f" (+{extra} more)"
    return subject


def _landed_str(deal) -> str:
    """Landed price alone, e.g. `€52.00` — the hero figure beneath the discount."""
    ccy = deal.landed_currency or deal.buyer_currency or deal.currency
    return _money(deal.landed_price, ccy)


def _price_secondary(deal) -> str:
    """Quiet secondary line: cost breakdown + VAT + median context. The landed
    total and the discount % now live in the hero rail, so they're omitted here."""
    item = deal.buyer_price or deal.price
    item_ccy = deal.buyer_currency or deal.currency
    ship = deal.shipping_buyer_price or deal.shipping_price or 0
    landed_ccy = deal.landed_currency or item_ccy
    breakdown = f"{_money(item, item_ccy)} + {_money(ship, landed_ccy)} ship"
    if deal.vat_estimated and deal.vat_amount:
        breakdown += f" + ~{_money(deal.vat_amount, landed_ccy)} est. import VAT"
    parts = [
        breakdown,
        _median_snippet(deal),
        _discogs_wide_snippet(deal),
        _historical_floor_snippet(deal),
    ]
    return " · ".join(p for p in parts if p)


def _median_snippet(deal) -> str:
    """`vs NM median €95.00` — the in-pool comparison; empty for solo/remote deals."""
    if deal.median_value is None:
        return ""
    cond = condition_short(deal.media_condition)
    return f"vs {cond} median {_money(deal.median_value, deal.median_currency)}"


def _discogs_wide_snippet(deal) -> str:
    """`Discogs-wide NM ≈ €X.XX` — empty when no annotation present."""
    value = deal.discogs_wide_median_value
    if value is None:
        return ""
    cond = condition_short(deal.media_condition)
    ccy = deal.discogs_wide_median_currency or deal.landed_currency
    return f"Discogs-wide {cond} ≈ {_money(value, ccy)}"


def _historical_floor_snippet(deal) -> str:
    """`all-time low (−X%, N pts)` — empty when no historical-floor annotation."""
    pct = deal.historical_floor_pct
    if not pct:
        return ""
    pts = deal.historical_data_points
    return f"all-time low (−{pct}%, {pts} pts)" if pts else f"all-time low (−{pct}%)"


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
    for p in picks:
        amt = _money(p.get("buyer_price"), p.get("buyer_currency"))
        cond = condition_short(p.get("media_condition"))
        title = _h((f'{p.get("release_artist")} – ' if p.get("release_artist") else "") + (p.get("release_title") or "?"))
        url = _h(p.get("listing_url") or "#")
        rows.append(
            f'<div style="margin-top:4px; font-size:12px; color:#555;">'
            f'+ <a href="{url}" style="color:#555;">{amt}</a> · {cond} · {title}</div>'
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
    for p in picks:
        amt = _money(p.get("buyer_price"), p.get("buyer_currency"))
        cond = condition_short(p.get("media_condition"))
        title = (f'{p.get("release_artist")} – ' if p.get("release_artist") else "") + (p.get("release_title") or "?")
        lines.append(f"  + {amt} · {cond} · {title} · {p.get('listing_url', '')}")
    if others > len(picks):
        lines.append(f"  +{others - len(picks)} more")
    return lines


# Hero-rail palettes, keyed by deal depth. A 50%+ steal gets the loud red that
# used to be the 🔥 badge; an ordinary deal gets a calm savings-green; a listing
# that only fired on Discogs' own flag (no computed discount) gets neutral amber.
_RAIL_BIG = ("#ffe0e0", "#f0b0b0", "#b71c1c")
_RAIL_DEAL = ("#e8f5e9", "#b9dfbd", "#1b5e20")
_RAIL_REMOTE = ("#fff3c4", "#f0d678", "#7a5b00")


def _rail_html(deal) -> str:
    """Left hero rail: big discount %, landed price, and the cover beneath."""
    if deal.discount_pct is None:
        bg, border, fg = _RAIL_REMOTE
    elif deal.big_deal:
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


def _badges_html(deal) -> str:
    """Secondary signal chips (★ Discogs Deal, ⬇ all-time low). The 🔥 big-deal
    badge is gone — the red rail already carries that meaning."""
    chips = []
    if deal.is_deal_remote:
        chips.append(
            '<span style="background:#fff3c4; color:#7a5b00; padding:1px 6px; '
            'border-radius:8px; font-size:11px; font-weight:600; '
            'border:1px solid #f0d678;">★ Discogs Deal</span>'
        )
    if deal.historical_floor_pct:
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
        f'<div style="margin-top:4px; font-size:12px; color:#666; font-style:italic;">'
        f'"{_h(comments)}"</div>'
        if comments else ""
    )

    siblings_html = "".join(_sibling_html(s) for s in (deal.siblings or []))
    shipping_html = _shipping_html(deal)

    # Two-cell table (not flex): the hero rail is load-bearing, and Outlook drops
    # flex layouts. Left = discount/landed/cover rail; right = details.
    return f"""
  <tr><td style="padding:14px 0; border-bottom:1px solid #eee; font-family:sans-serif;">
    <table style="width:100%; border-collapse:collapse;"><tr>
      <td width="96" valign="top" style="width:96px; padding:0;">{_rail_html(deal)}</td>
      <td valign="top" style="padding-left:14px;">
        <div style="font-size:14px; font-weight:bold;">{_h(_heading(deal))}</div>
        <div style="font-size:13px; color:#666; margin-top:4px;">{_h(_price_secondary(deal))}</div>
        <div style="font-size:13px; color:#666; margin-top:2px;">{_h(_seller_line(deal))}</div>
        {_badges_html(deal)}
        {comments_html}
        <div style="margin-top:8px;">
          <a href="{_h(deal.listing_url or "#")}"
             style="background:#333; color:#fff; padding:4px 10px; text-decoration:none;
                    border-radius:3px; font-size:12px;">View listing →</a>
        </div>
        {siblings_html}
        {shipping_html}
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
    if not scanned:
        return ""
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
        Landed = item + shipping, in your account currency. Discount is vs the same-condition
        median of this release's wantlist pool, measured on effective cost — landed plus an
        estimated import VAT uplift for non-EU origins. Deals are sorted deepest-first.
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
        # Primary line mirrors the HTML hero rail: discount % + landed price,
        # plus the signal badges (🔥 kept here since text has no rail colour).
        primary = f"{_discount_label(d)} · {_landed_str(d)} landed"
        if d.big_deal:
            primary += " · 🔥 50%+"
        if d.is_deal_remote:
            primary += " · ★ Discogs Deal"
        if d.historical_floor_pct:
            primary += " · ⬇ all-time low"
        lines.append(primary)
        secondary = _price_secondary(d)
        if secondary:
            lines.append(secondary)
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
