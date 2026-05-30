"""EmailNotifier digest assembly. The SMTP transport is stubbed; these guard the
rendering + post-send bookkeeping, where a stray reference once raised *after* the
mail had already gone out (so the send looked like a failure to the caller and the
deals re-queued every run)."""
from datetime import datetime, timezone

from models import Deal
from notifier import EmailNotifier


def _notifier():
    return EmailNotifier(
        smtp_host="localhost", smtp_port=587, smtp_user="u",
        smtp_pass="p", smtp_from="from@x", smtp_to="to@x",
    )


def _deal(id_, **kw):
    base = dict(
        id=id_, release_id=100, release_artist="Artist", release_title="Title",
        buyer_price=10.0, buyer_currency="EUR", landed_price=15.0, landed_currency="EUR",
        discount_pct=42, effective_discount=0.42, ranked=True,
        seller_username=f"seller{id_}", listing_url=f"https://x/{id_}",
    )
    base.update(kw)
    return Deal(**base)


def test_send_completes_without_raising_after_transport(monkeypatch):
    """Regression: the success-log line ran after the mail was sent and must not
    raise — otherwise the caller treats a delivered email as a failed flush."""
    n = _notifier()
    sent = []
    monkeypatch.setattr(n, "_send_message", lambda msg: sent.append(msg))

    n.send([_deal(1), _deal(2)], datetime.now(timezone.utc), extra_count=3)

    assert len(sent) == 1
    # Parts are base64 transfer-encoded; decode to check the rendered digest.
    parts = [p.get_payload(decode=True).decode() for p in sent[0].get_payload()]
    assert any("Title" in p for p in parts)


def test_send_no_deals_is_a_noop(monkeypatch):
    n = _notifier()
    monkeypatch.setattr(n, "_send_message", lambda msg: (_ for _ in ()).throw(AssertionError("should not send")))
    n.send([], datetime.now(timezone.utc))
