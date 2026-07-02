"""EmailNotifier digest assembly + the "The Find" renderer. The SMTP transport is
stubbed; these guard the rendering (verdict→visual mapping, the market gauge, the
best-price card, edge-state callouts) and the post-send bookkeeping, where a stray
reference once raised *after* the mail had already gone out."""
from datetime import datetime, timezone

import notifier
from models import Deal
from notifier import EmailNotifier

NOW = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)


def _notifier():
    return EmailNotifier(
        smtp_host="localhost", smtp_port=587, smtp_user="u",
        smtp_pass="p", smtp_from="from@x", smtp_to="to@x",
    )


def _deal(id_, **kw):
    base = dict(
        id=id_, release_id=100, release_artist="Artist", release_title="Title",
        media_condition="Near Mint (NM or M-)", sleeve_condition="Near Mint (NM or M-)",
        buyer_price=10.0, buyer_currency="EUR", landed_price=15.0, landed_currency="EUR",
        discount_pct=42, effective_discount=0.42, ranked=True,
        seller_username=f"seller{id_}", listing_url=f"https://x/{id_}",
    )
    base.update(kw)
    return Deal(**base)


def _sold(id_, **kw):
    """A sold-validated deal carrying the gauge's sold fields."""
    base = dict(
        deal_source="below_sold_median", discount_pct=45, effective_discount=0.45,
        sold_median_value=100.0, sold_median_currency="EUR", sold_data_points=8,
        sold_low_value=40.0, sold_high_value=160.0, sold_last_date="2026-01-23",
        buyer_price=45.0, shipping_buyer_price=7.0, landed_price=52.0, effective_cost=52.0,
    )
    base.update(kw)
    return _deal(id_, **base)


# ── send (transport stubbed) ─────────────────────────────────────────────────

def test_send_completes_without_raising_after_transport(monkeypatch):
    """Regression: the success-log line runs after the mail is sent and must not
    raise — otherwise the caller treats a delivered email as a failed flush."""
    n = _notifier()
    sent = []
    monkeypatch.setattr(n, "_send_message", lambda msg: sent.append(msg))

    n.send([_deal(1), _deal(2)], NOW, extra_count=3)

    assert len(sent) == 1
    parts = [p.get_payload(decode=True).decode() for p in sent[0].get_payload()]
    assert any("Title" in p for p in parts)


def test_send_no_deals_is_a_noop(monkeypatch):
    n = _notifier()
    monkeypatch.setattr(n, "_send_message", lambda msg: (_ for _ in ()).throw(AssertionError("should not send")))
    n.send([], NOW)


# ── identity / meta / money (stable helpers) ─────────────────────────────────

def test_identity_with_artist():
    assert notifier._identity(_deal(1, release_artist="Miles Davis", release_title="Kind of Blue")) == "Miles Davis — Kind of Blue"


def test_identity_without_artist():
    assert notifier._identity(_deal(1, release_artist=None, release_title="White Label")) == "White Label"


def test_meta_line_full():
    d = _deal(1, media_condition="Near Mint (NM or M-)", sleeve_condition="Very Good Plus (VG+)",
              release_year=1959, release_format="LP", release_country="US")
    assert notifier._meta_line(d) == "NM/VG+ · 1959 · LP · US"


def test_meta_line_condition_only():
    d = _deal(1, media_condition="Very Good Plus (VG+)", sleeve_condition=None,
              release_year=None, release_format=None, release_country=None)
    assert notifier._meta_line(d) == "VG+/?"


def test_cost_line_with_vat():
    d = _deal(1, buyer_price=45.0, shipping_buyer_price=7.0, landed_price=61.0,
              vat_estimated=True, vat_amount=9.0)
    assert notifier._cost_line(d) == "€45.00 + €7.00 ship + ~€9.00 VAT = €61.00 landed"


def test_cost_line_without_vat():
    d = _deal(1, buyer_price=45.0, shipping_buyer_price=7.0, landed_price=52.0)
    assert notifier._cost_line(d) == "€45.00 + €7.00 ship = €52.00 landed"


def test_landed_str_uses_vat_inclusive_effective_cost():
    d = _deal(1, buyer_price=44.0, shipping_buyer_price=4.0, landed_price=48.0,
              effective_cost=58.08, vat_estimated=True, vat_amount=10.08)
    assert notifier._landed_str(d) == "€58.08"
    assert notifier._cost_line(d) == "€44.00 + €4.00 ship + ~€10.08 VAT = €58.08 landed"


def test_discount_label_deal_shows_minus():
    assert notifier._discount_label(_deal(1, discount_pct=26)) == "−26%"


def test_discount_label_no_discount_shows_deal():
    assert notifier._discount_label(_deal(1, discount_pct=None)) == "DEAL"


# ── verdict → visual mapping (the trust signal) ──────────────────────────────

def test_verdict_kind_maps_each_state():
    assert notifier._verdict_kind(_sold(1, discount_pct=45)) == ("brass", "Strong buy", "sold")
    assert notifier._verdict_kind(_sold(2, discount_pct=10)) == ("brass", "Good price", "sold")
    asking = _deal(3, deal_source="below_asking_median", low_confidence=True, discount_pct=40)
    assert notifier._verdict_kind(asking) == ("amber", "Worth a look", "muted")
    caveat = _sold(4, sold_tier_caveat=True)
    assert notifier._verdict_kind(caveat) == ("amber", "Check grade", "sold")
    detached = _deal(5, deal_source="below_sold_low", low_confidence=True, detached_low=True)
    assert notifier._verdict_kind(detached) == ("amber", "Check first", "muted")
    remote = _deal(6, deal_source="remote_only", discount_pct=None, ranked=False)
    assert notifier._verdict_kind(remote) == ("neutral", "Only copy", "none")


def test_sticker_renders_verdict_grade_and_landed():
    html = notifier._sticker_html(_sold(1, media_condition="Near Mint (NM or M-)"), "brass", "Strong buy")
    assert "Strong buy" in html and "NM" in html and "€52.00" in html


# ── the market gauge ─────────────────────────────────────────────────────────

def test_gauge_sold_has_bar_and_money_path():
    g = notifier._gauge_html(_sold(1), "sold")
    assert "you pay" in g and "€52.00" in g
    assert "vs €100.00 typical" in g
    assert "#B98A2E" in g                       # the brass saving bar is present
    assert "€45.00 + €7.00 ship = €52.00 landed" in g
    assert "8 NM sales" in g                    # provenance


def test_gauge_muted_is_estimate_without_bar():
    d = _deal(1, deal_source="below_asking_median", low_confidence=True,
              median_value=110.0, median_currency="EUR", landed_price=70.0)
    g = notifier._gauge_html(d, "muted")
    assert "you pay" in g
    assert "asking" in g                        # benchmarked against asking, labelled
    assert "~" in g                             # the estimate tilde
    assert "#B98A2E" not in g                   # no saving bar on a muted (asking) gauge


def test_gauge_none_renders_nothing():
    assert notifier._gauge_html(_deal(1, deal_source="remote_only"), "none") == ""


# ── edge-state callouts (loud only when they fire) ───────────────────────────

def test_all_time_low_is_a_positive_ribbon():
    html = notifier._build_html([_sold(1, historical_floor_pct=8)], NOW, 0)
    assert "All-time low" in html
    assert "#B98A2E" in notifier._callouts_html(_sold(1, historical_floor_pct=8))  # brass ribbon


def test_better_grade_caveat_callout_names_the_grade():
    d = _sold(1, media_condition="Very Good Plus (VG+)", sold_tier_caveat=True,
              sold_tier_caveat_grade="M", sold_tier_caveat_value=70.0)
    html = notifier._build_html([d], NOW, 0)
    assert "Check the grade" in html
    assert "sells for about €70.00" in html


def test_detached_low_shows_verify_callout():
    d = _deal(1, deal_source="below_sold_low", low_confidence=True, detached_low=True,
              sold_median_value=30.0, sold_median_currency="EUR", sold_data_points=3,
              sold_low_value=25.0, sold_high_value=40.0)
    assert "Verify before buying" in notifier._build_html([d], NOW, 0)


def test_asking_only_shows_amber_sticker_and_caution():
    d = _deal(1, deal_source="below_asking_median", low_confidence=True,
              median_value=110.0, median_currency="EUR")
    html = notifier._build_html([d], NOW, 0)
    assert "Worth a look" in html               # amber sticker word
    assert "asking prices" in html              # the caution callout


def test_remote_only_is_neutral_with_no_gauge():
    d = _deal(1, deal_source="remote_only", discount_pct=None, is_deal_remote=True, ranked=False)
    html = notifier._build_html([d], NOW, 0)
    assert "Only copy" in html
    assert "Discogs" in html                    # the neutral note
    assert "you pay" not in html                # no gauge for a lone, unbenchmarked listing


def test_comment_renders_when_present():
    html = notifier._deal_html(_sold(1, comments="Light ring wear; plays NM."))
    assert "Light ring wear; plays NM." in html


# ── best price for this record (the second card) ─────────────────────────────

def test_best_alt_renders_a_second_card_with_savings_note():
    alt = _sold(2, media_condition="Near Mint (NM or M-)", buyer_price=44.0,
                shipping_buyer_price=0.0, landed_price=44.0, effective_cost=44.0,
                discount_pct=50, sold_median_value=88.0, sold_low_value=38.0, sold_high_value=150.0)
    primary = _sold(1, media_condition="Very Good Plus (VG+)", best_alt=alt)
    html = notifier._build_html([primary], NOW, 0)
    assert "Best price for this record" in html
    assert "a better copy is on sale" in html     # the connector
    assert "better grade" in html                # NM > VG+ savings note
    text = notifier._build_text([primary], NOW, 0)
    assert "BEST PRICE FOR THIS RECORD" in text


def test_subject_flags_a_cheaper_better_copy():
    alt = _sold(2, media_condition="Near Mint (NM or M-)", landed_price=44.0, effective_cost=44.0)
    primary = _sold(1, release_artist="Bill Evans", release_title="Sunday at the Village Vanguard",
                    media_condition="Very Good Plus (VG+)", best_alt=alt)
    subject = notifier._build_subject([primary])
    assert subject.startswith("[Discogs Watcher]")
    assert "Bill Evans — Sunday at the Village Vanguard" in subject
    assert "but NM is €44.00" in subject


def test_subject_plain_without_best_alt():
    subject = notifier._build_subject([_sold(1, release_artist="Miles Davis", release_title="Kind of Blue")])
    assert subject.startswith("[Discogs Watcher]")
    assert "Kind of Blue" in subject
    assert "but" not in subject


# ── combine box (more from this seller) ──────────────────────────────────────

def test_combine_box_shows_tiers_and_picks_ordered():
    d = _sold(1, seller_total_others=4,
              shipping_hint={"seller": "discland", "currency": "EUR", "country": "Netherlands",
                             "free_shipping": True, "free_min": 75.0, "subtotal": 45.0,
                             "free_gap": 30.0, "tiers": [[2, 7.0], [0, 11.0]], "per_item": 1},
              seller_picks=[
                  {"release_artist": "Wynton Kelly", "release_title": "Kelly Blue",
                   "media_condition": "Very Good Plus (VG+)", "buyer_price": 12.0,
                   "buyer_currency": "EUR", "listing_url": "u2", "discount_pct": 41},
              ])
    html = notifier._build_html([d], NOW, 0)
    assert "More from discland on your wantlist" in html
    assert "Ships Netherlands" in html           # fee tiers, only with other items
    assert "−41%" in html                        # pick badge
    assert "Kelly Blue" in html


def test_combine_box_absent_without_other_items():
    html = notifier._deal_html(_sold(1, seller_total_others=0, seller_picks=[]))
    assert "on your wantlist" not in html        # no combine box at all


def test_build_html_cuts_old_internals():
    d = _sold(1, sold_tier_higher=[{"short": "M", "median": 70.0, "count": 6}],
              sold_tier_at_or_above={"short": "NM↑", "median": 80.0, "count": 11})
    html = notifier._build_html([d], NOW, 0)
    assert "Also sold:" not in html              # the tier ladder is cut
    assert "SOLD-validated" not in html          # old confidence chip gone
    assert "wantlist releases for sale" not in html  # scan summary cut


# ── NtfyNotifier (push fast-lane) ────────────────────────────────────────────

from notifier import NtfyNotifier


class _FakeResp:
    """Stub requests.Response: a no-op raise_for_status (success)."""

    def raise_for_status(self):
        return None


def _ntfy_recorder(monkeypatch, resp=None, raises=None):
    """Patch notifier.requests.post to record calls without any network.

    Returns the `calls` list; each entry is a dict {url, data, json, headers}.
    `resp` overrides the returned stub response; `raises` makes post() raise.
    """
    calls = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "json": json, "headers": headers or {}})
        if raises is not None:
            raise raises
        return resp if resp is not None else _FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return calls


def test_ntfy_posts_correct_url_and_headers(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh/", topic="discogs-deals-x",
                     token="tok123", priority="high")
    d = _deal(1, release_artist="Miles Davis", release_title="Kind of Blue",
              image_url="https://img/cover.jpg", listing_url="https://x/1",
              discount_pct=45, effective_discount=0.45,
              media_condition="Near Mint (NM or M-)")
    n.send([d], datetime.now(timezone.utc))

    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == "https://ntfy.sh"
    p = c["json"]
    assert p["topic"] == "discogs-deals-x"
    assert p["title"] == notifier._push_title(d)
    assert p["message"] == notifier._push_body(d)
    assert p["click"] == "https://x/1"
    assert p["icon"] == "https://img/cover.jpg"
    assert p["markdown"] is True
    assert p["priority"] == 4                       # "high" → 4
    assert c["headers"]["Authorization"] == "Bearer tok123"
    for v in c["headers"].values():
        v.encode("latin-1")                         # must not raise


def test_ntfy_body_is_markdown_and_plaintext_legible():
    d = _deal(1, release_artist="Miles Davis", release_title="Kind of Blue",
              media_condition="Near Mint (NM or M-)",
              buyer_price=45.0, buyer_currency="EUR",
              landed_price=52.0, landed_currency="EUR",
              discount_pct=45, effective_discount=0.45)
    body = notifier._push_body(d)
    assert "Miles Davis — Kind of Blue" in body   # identity
    assert "NM" in body                            # condition_short
    assert "−45%" in body                          # discount label
    assert "landed" in body                        # landed figure label
    assert "All-time low" not in body              # no floor set on this deal


def test_ntfy_body_shows_all_time_low_line():
    d = _deal(1, historical_floor_value=40.0)
    assert "All-time low" in notifier._push_body(d)


def test_ntfy_title_leads_with_discount_then_artist():
    d = _deal(1, release_artist="Miles Davis", discount_pct=45, effective_discount=0.45)
    assert notifier._push_title(d) == "−45% · Miles Davis"


def test_ntfy_title_prefixes_arrow_for_all_time_low():
    d = _deal(1, release_artist="Miles Davis", discount_pct=45,
              effective_discount=0.45, historical_floor_value=40.0)
    assert notifier._push_title(d) == "⬇ −45% · Miles Davis"


def test_ntfy_omits_icon_when_no_image(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t")
    n.send([_deal(1, image_url=None)], datetime.now(timezone.utc))
    assert "icon" not in calls[0]["json"]


def test_ntfy_omits_auth_when_no_token(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t", token=None)
    n.send([_deal(1)], datetime.now(timezone.utc))
    assert "Authorization" not in calls[0]["headers"]


def test_ntfy_omits_priority_when_unset(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t", priority=None)
    n.send([_deal(1)], datetime.now(timezone.utc))
    assert "priority" not in calls[0]["json"]


def test_ntfy_caps_at_max_per_run(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t", max_per_run=10)
    deals = [_deal(i) for i in range(12)]
    n.send(deals, datetime.now(timezone.utc))
    assert len(calls) == 10


def test_ntfy_push_failure_is_swallowed(monkeypatch):
    _ntfy_recorder(monkeypatch, raises=RuntimeError("server down"))
    n = NtfyNotifier(server="https://ntfy.sh", topic="t")
    n.send([_deal(1)], datetime.now(timezone.utc))  # must not raise


def test_ntfy_500_is_swallowed(monkeypatch):
    class _Resp500:
        def raise_for_status(self):
            raise RuntimeError("500 Server Error")

    _ntfy_recorder(monkeypatch, resp=_Resp500())
    n = NtfyNotifier(server="https://ntfy.sh", topic="t")
    n.send([_deal(1)], datetime.now(timezone.utc))  # must not raise


def test_ntfy_no_deals_is_a_noop(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t")
    n.send([], datetime.now(timezone.utc))
    assert calls == []


def test_ntfy_unicode_title_stays_in_json_body_not_headers(monkeypatch):
    calls = _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t", token="tok")
    d = _deal(1, release_artist="坂本龍一", discount_pct=45, effective_discount=0.45)
    n.send([d], datetime.now(timezone.utc))
    p = calls[0]["json"]
    assert "坂本龍一" in p["title"]
    assert "−45%" in p["title"]                     # U+2212, intact in the body
    for v in calls[0]["headers"].values():
        v.encode("latin-1")                         # headers must stay latin-1-safe


def test_ntfy_send_returns_delivered_count(monkeypatch):
    _ntfy_recorder(monkeypatch)
    n = NtfyNotifier(server="https://ntfy.sh", topic="t")
    assert n.send([_deal(1), _deal(2)], datetime.now(timezone.utc)) == 2


def test_ntfy_send_count_excludes_failures(monkeypatch):
    _ntfy_recorder(monkeypatch, raises=RuntimeError("down"))
    n = NtfyNotifier(server="https://ntfy.sh", topic="t")
    assert n.send([_deal(1), _deal(2)], datetime.now(timezone.utc)) == 0


def test_ntfy_priority_mapping():
    assert notifier._ntfy_priority("high") == 4
    assert notifier._ntfy_priority("5") == 5
    assert notifier._ntfy_priority("bogus") is None
    assert notifier._ntfy_priority(None) is None
