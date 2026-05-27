"""
Notification layer.

EmailNotifier sends digest emails (one per flush) and admin alerts (cookie
expiry, watcher health). The Notifier base class is the extension point for
a future push client.
"""

import html as _html
import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from evaluator import condition_short, currency_symbol

logger = logging.getLogger(__name__)

_CERTAINTY_COLOURS = {
    "HIGH": "#2e7d32",
    "MEDIUM": "#e65100",
    "LOW": "#b71c1c",
}


class Notifier(ABC):
    @abstractmethod
    def send(self, deals: list[dict], run_time: datetime, **kwargs) -> None:
        """Send a batch alert for the given deals."""


class EmailNotifier(Notifier):
    def __init__(self, smtp_host, smtp_port, smtp_user, smtp_pass, smtp_from, alert_to):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_pass = smtp_pass
        self.smtp_from = smtp_from
        self.alert_to = alert_to

    def send(
        self,
        deals: list[dict],
        run_time: datetime,
        extra_count: int = 0,
        session_days_left: int | None = None,
    ) -> None:
        if not deals:
            return
        run_str = run_time.strftime("%Y-%m-%d %H:%M UTC")
        n = len(deals)
        subject = f"[Discogs Watcher] {n} good deal{'s' if n != 1 else ''} — {run_str}"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.smtp_from
        msg["To"] = self.alert_to
        msg.attach(MIMEText(_build_text(deals, run_time, extra_count, session_days_left), "plain"))
        msg.attach(MIMEText(_build_html(deals, run_time, extra_count, session_days_left), "html"))

        self._send_message(msg)
        logger.info("Email sent to %s (%d deal(s), %d extra)", self.alert_to, n, extra_count)

    def send_admin_alert(self, subject: str, body: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Discogs Watcher] {subject}"
        msg["From"] = self.smtp_from
        msg["To"] = self.alert_to
        msg.attach(MIMEText(body, "plain"))
        self._send_message(msg)
        logger.info("Admin alert sent: %s", subject)

    def _send_message(self, msg: MIMEMultipart) -> None:
        port = self.smtp_port
        try:
            if port == 465:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.smtp_host, port, context=ctx) as s:
                    s.login(self.smtp_user, self.smtp_pass)
                    s.sendmail(self.smtp_from, self.alert_to, msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, port, timeout=30) as s:
                    s.ehlo()
                    if s.has_extn("STARTTLS"):
                        s.starttls()
                        s.ehlo()
                    s.login(self.smtp_user, self.smtp_pass)
                    s.sendmail(self.smtp_from, self.alert_to, msg.as_string())
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


def _heading(deal: dict) -> str:
    artist = deal.get("release_artist") or ""
    title = deal.get("release_title") or "Unknown"
    year = deal.get("release_year")
    fmt = deal.get("release_format")
    country = deal.get("release_country")
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
        f"{condition_short(deal.get('media_condition'))}/"
        f"{condition_short(deal.get('sleeve_condition'))}"
    )
    if suffix:
        return f"{head} · {suffix} · {cond_pair}"
    return f"{head} · {cond_pair}"


def _price_line(deal: dict) -> str:
    item = deal.get("buyer_price") or deal.get("price")
    item_ccy = deal.get("buyer_currency") or deal.get("currency")
    ship = deal.get("shipping_buyer_price") or deal.get("shipping_price") or 0
    landed_amt = deal.get("landed_price")
    landed_ccy = deal.get("landed_currency") or item_ccy
    parts = [
        f"{_money(landed_amt, landed_ccy)} landed "
        f"({_money(item, item_ccy)} + {_money(ship, landed_ccy)} ship)",
        deal.get("deal_reason", ""),
        deal.get("certainty_label", ""),
    ]
    return " · ".join(p for p in parts if p)


def _seller_line(deal: dict) -> str:
    name = deal.get("seller_username") or "—"
    rating = deal.get("seller_rating")
    rating_str = f" {rating:.1f}%" if isinstance(rating, (int, float)) else ""
    region = deal.get("shipping_region") or deal.get("ships_from") or ""
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


def _deal_html(deal: dict) -> str:
    cert = deal.get("certainty_label", "")
    cert_color = _CERTAINTY_COLOURS.get(cert, "#555")
    img = deal.get("image_url")
    img_html = (
        f'<img src="{_h(img)}" alt="" width="64" height="64" '
        f'style="display:block; border-radius:4px; object-fit:cover; flex:0 0 auto;">'
        if img else ""
    )

    # Highlight just the certainty word inside the price line
    price_line_raw = _price_line(deal)
    if cert and price_line_raw.endswith(" · " + cert):
        price_line_html = (
            _h(price_line_raw[: -(len(cert))])
            + f'<strong style="color:{cert_color};">{_h(cert)}</strong>'
        )
    else:
        price_line_html = _h(price_line_raw)

    comments = (deal.get("comments") or "").strip()
    comments_html = (
        f'<div style="margin-top:4px; font-size:12px; color:#666; font-style:italic;">'
        f'"{_h(comments)}"</div>'
        if comments else ""
    )

    siblings_html = "".join(_sibling_html(s) for s in (deal.get("_siblings") or []))

    return f"""
  <tr><td style="padding:14px 0; border-bottom:1px solid #eee; font-family:sans-serif;">
    <div style="display:flex; gap:12px; align-items:flex-start;">
      {img_html}
      <div style="flex:1; min-width:0;">
        <div style="font-size:14px; font-weight:bold;">{_h(_heading(deal))}</div>
        <div style="font-size:13px; color:#333; margin-top:4px;">{price_line_html}</div>
        <div style="font-size:13px; color:#666; margin-top:2px;">{_h(_seller_line(deal))}</div>
        {comments_html}
        <div style="margin-top:8px;">
          <a href="{_h(deal.get("listing_url") or "#")}"
             style="background:#333; color:#fff; padding:4px 10px; text-decoration:none;
                    border-radius:3px; font-size:12px;">View listing →</a>
        </div>
        {siblings_html}
      </div>
    </div>
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


def _build_html(
    deals: list[dict], run_time: datetime, extra_count: int,
    session_days_left: int | None = None,
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
              border-radius:6px; overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,.08);">
    <div style="background:#1a1a1a; color:#fff; padding:20px 24px;">
      <h1 style="margin:0; font-size:22px; font-weight:600; letter-spacing:-.01em;">
        Discogs Watcher
      </h1>
      <p style="margin:6px 0 0; color:#bbb; font-size:13px;">
        {n} good deal{s} on your wantlist · {run_str}{session_html}
      </p>
    </div>
    <div style="padding:8px 24px 20px;">
      <table style="width:100%; border-collapse:collapse;">{rows}</table>
      {extra}
      <p style="color:#aaa; font-size:11px; margin-top:14px; border-top:1px solid #eee; padding-top:10px;">
        Landed = item + shipping, in your account currency.
        Median is computed per condition (M / NM / VG+) within this release's wantlist marketplace pool.
      </p>
    </div>
  </div>
</body></html>"""


def _build_text(
    deals: list[dict], run_time: datetime, extra_count: int,
    session_days_left: int | None = None,
) -> str:
    run_str = run_time.strftime("%Y-%m-%d %H:%M UTC")
    session_label, _ = _session_note(session_days_left)
    header = f"Discogs Watcher · {len(deals)} good deal(s) · {run_str}"
    if session_label:
        header += f" · {session_label}"
    lines = [header, "=" * 60]
    for d in deals:
        lines.append("")
        lines.append(_heading(d))
        lines.append(_price_line(d))
        lines.append(_seller_line(d))
        if d.get("comments"):
            lines.append(f'"{d["comments"]}"')
        lines.append(d.get("listing_url", ""))
        for s in d.get("_siblings") or []:
            lines.append(_sibling_text(s))
        lines.append("-" * 60)
    if extra_count > 0:
        lines.append(f"\n+{extra_count} more deal(s) not shown.")
    return "\n".join(lines)
