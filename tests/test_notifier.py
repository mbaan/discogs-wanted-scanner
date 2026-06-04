"""EmailNotifier digest assembly. The SMTP transport is stubbed; these guard the
rendering + post-send bookkeeping, where a stray reference once raised *after* the
mail had already gone out (so the send looked like a failure to the caller and the
deals re-queued every run)."""
from datetime import datetime, timezone

import notifier
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


# ── sold-price snippet ─────────────────────────────────────────────────────────

def _sold_deal():
    return _deal(1, sold_median_value=16.27, sold_median_currency="EUR",
                 sold_low_value=4.99, sold_high_value=40.0, sold_last_date="2026-02-03")


def test_sold_fields_render_through_html_and_text():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    html = notifier._build_html([_sold_deal()], now, 0)
    text = notifier._build_text([_sold_deal()], now, 0)
    assert "SOLD median €16.27" in html
    assert "SOLD median €16.27" in text


def _sold_led_deal():
    return _deal(1, media_condition="Very Good Plus (VG+)",
                 deal_source="below_sold_median", median_value=100.0, median_currency="EUR",
                 sold_median_value=100.0, sold_median_currency="EUR", sold_data_points=8,
                 sold_low_value=40.0, sold_high_value=160.0, sold_last_date="2026-01-23",
                 deal_reason="50% below VG+ SOLD median €100.00 of 8 sales")


def test_sold_led_deal_proof_line_and_chip():
    d = _sold_led_deal()
    # Proof line carries the sold figure; the count is omitted (it rides the chip).
    assert notifier._proof_line(d) == "SOLD median €100.00 · €40.00–€160.00 · last 2026-01-23"
    assert "SOLD-validated" in notifier._method_chip_html(d)


def test_sold_led_deal_renders_through_html_and_text():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    html = notifier._build_html([_sold_led_deal()], now, 0)
    text = notifier._build_text([_sold_led_deal()], now, 0)
    assert "SOLD median €100.00" in html and "SOLD-validated" in html
    assert "SOLD median €100.00" in text


# ── higher-tier sold breakdown + better-grade caveat ────────────────────────────

def _tiered_deal(**kw):
    base = dict(
        media_condition="Very Good Plus (VG+)", sold_median_currency="EUR",
        sold_tier_at_or_above={"short": "VG+↑", "median": 50.0, "count": 14,
                               "low": 15.0, "high": 120.0, "grades": ["M", "NM", "VG+"]},
        sold_tier_higher=[{"short": "M", "median": 70.0, "count": 6},
                          {"short": "NM", "median": 50.0, "count": 5}],
    )
    base.update(kw)
    return _deal(1, **base)


def test_sold_tiers_snippet_renders_pooled_then_better_grades():
    s = notifier._sold_tiers_snippet(_tiered_deal())
    assert s == "Also sold: VG+↑ €50.00 (14), M €70.00 (6), NM €50.00 (5)"


def test_sold_tiers_snippet_empty_without_better_grade():
    assert notifier._sold_tiers_snippet(_deal(1)) == ""
    # at_or_above present but no higher grade ⇒ nothing to compare up to.
    assert notifier._sold_tiers_snippet(
        _deal(1, sold_tier_at_or_above={"short": "M↑", "median": 70.0, "count": 6}),
    ) == ""


def test_better_grade_caveat_chip_and_text():
    d = _tiered_deal(sold_tier_caveat=True, sold_tier_caveat_grade="NM",
                     sold_tier_caveat_value=28.0)
    assert "⚠ NM sells ~€28.00" in notifier._signal_chips_html(d)
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    html = notifier._build_html([d], now, 0)
    text = notifier._build_text([d], now, 0)
    assert "⚠ NM sells ~€28.00" in html
    assert "Also sold: VG+↑ €50.00" not in html   # the ladder no longer rides the HTML card
    assert "⚠ NM sells ~€28.00" in text
    assert "Also sold: VG+↑ €50.00" in text        # kept in the plain-text part


def test_low_confidence_asking_deal_renders_tag():
    import notifier
    from models import Deal
    d = Deal(id=1, release_title="X", release_artist="Y",
             media_condition="Near Mint (NM or M-)", buyer_price=20.0, buyer_currency="EUR",
             landed_price=23.0, landed_currency="EUR", discount_pct=40,
             deal_source="below_asking_median", low_confidence=True, ranked=True)
    assert "asking" in notifier._method_chip_html(d).lower()


def test_discount_label_deal_shows_minus():
    assert notifier._discount_label(_deal(1, discount_pct=26)) == "−26%"


def test_discount_label_no_discount_shows_deal():
    # remote-only listing has no computed discount → neutral "DEAL".
    assert notifier._discount_label(_deal(1, discount_pct=None)) == "DEAL"


def test_rail_color_tracks_discount_magnitude():
    # Colour follows the headline %, so it agrees with the deepest-first sort.
    red, green = "#ffe0e0", "#e8f5e9"
    assert red in notifier._rail_html(_deal(1, discount_pct=31))    # big → red
    assert green in notifier._rail_html(_deal(2, discount_pct=10))  # small → green
    # A big discount we don't trust never goes red.
    assert green in notifier._rail_html(_deal(3, discount_pct=31, low_confidence=True))
    assert green in notifier._rail_html(_deal(4, discount_pct=31, sold_tier_caveat=True))


def test_detached_low_renders_verify_caveat():
    import notifier
    from models import Deal
    d = Deal(id=1, release_title="X", media_condition="Near Mint (NM or M-)",
             buyer_price=8.0, buyer_currency="EUR", landed_price=10.0, landed_currency="EUR",
             discount_pct=90, deal_source="below_asking_median", low_confidence=True,
             detached_low=True, ranked=True)
    assert "verify" in notifier._signal_chips_html(d).lower()


# ── redesigned card helpers ─────────────────────────────────────────────────────

def test_identity_with_artist():
    d = _deal(1, release_artist="Miles Davis", release_title="Kind of Blue")
    assert notifier._identity(d) == "Miles Davis — Kind of Blue"


def test_identity_without_artist():
    d = _deal(1, release_artist=None, release_title="White Label")
    assert notifier._identity(d) == "White Label"


def test_meta_line_full():
    d = _deal(1, media_condition="Near Mint (NM or M-)",
              sleeve_condition="Very Good Plus (VG+)",
              release_year=1959, release_format="LP", release_country="US")
    assert notifier._meta_line(d) == "NM/VG+ · 1959 · LP · US"


def test_meta_line_condition_only():
    d = _deal(1, media_condition="Very Good Plus (VG+)", sleeve_condition=None,
              release_year=None, release_format=None, release_country=None)
    assert notifier._meta_line(d) == "VG+/?"


def test_proof_line_sparse_includes_count_range_date():
    # below_sold_low: the chip omits the count, so the proof line carries it.
    d = _deal(1, deal_source="below_sold_low", sold_median_value=30.0,
              sold_median_currency="EUR", sold_data_points=3,
              sold_low_value=25.0, sold_high_value=40.0, sold_last_date="2025-11-02")
    assert notifier._proof_line(d) == "SOLD median €30.00 (3 sold) · €25.00–€40.00 · last 2025-11-02"


def test_proof_line_sold_led_omits_count():
    # below_sold_median: the count rides the confidence chip, so it's not repeated.
    d = _deal(1, deal_source="below_sold_median", sold_median_value=100.0,
              sold_median_currency="EUR", sold_data_points=8,
              sold_low_value=40.0, sold_high_value=160.0, sold_last_date="2026-01-23")
    assert notifier._proof_line(d) == "SOLD median €100.00 · €40.00–€160.00 · last 2026-01-23"


def test_proof_line_empty_without_sold_data():
    assert notifier._proof_line(_deal(1)) == ""


def test_cost_line_with_vat():
    d = _deal(1, buyer_price=45.0, buyer_currency="EUR", shipping_buyer_price=7.0,
              landed_price=61.0, landed_currency="EUR",
              vat_estimated=True, vat_amount=9.0)
    assert notifier._cost_line(d) == "€45.00 + €7.00 ship + ~€9.00 VAT = €61.00 landed"


def test_cost_line_without_vat():
    d = _deal(1, buyer_price=45.0, buyer_currency="EUR", shipping_buyer_price=7.0,
              landed_price=52.0, landed_currency="EUR")
    assert notifier._cost_line(d) == "€45.00 + €7.00 ship = €52.00 landed"


def test_landed_and_cost_line_show_vat_inclusive_all_in():
    """Regression: the evaluator stores landed_price as item+shipping (VAT-excluded)
    and effective_cost as the VAT-inclusive all-in. The hero/rail/cost-line must show
    the all-in figure, so the equation balances and agrees with the (effective-cost
    based) discount and the footer's definition of "landed"."""
    d = _deal(1, buyer_price=44.0, buyer_currency="EUR", shipping_buyer_price=4.0,
              landed_price=48.0, landed_currency="EUR",
              effective_cost=58.08, vat_estimated=True, vat_amount=10.08)
    assert notifier._landed_str(d) == "€58.08"
    assert notifier._cost_line(d) == "€44.00 + €4.00 ship + ~€10.08 VAT = €58.08 landed"


def test_method_chip_html_sold_validated():
    d = _deal(1, deal_source="below_sold_median", sold_data_points=8)
    html = notifier._method_chip_html(d)
    assert "SOLD-validated" in html and "8 sales" in html


def test_method_chip_html_asking_only():
    d = _deal(1, deal_source="below_asking_median", low_confidence=True,
              media_condition="Near Mint (NM or M-)")
    assert "asking-only" in notifier._method_chip_html(d).lower()


def test_method_chip_html_empty_for_remote_only():
    assert notifier._method_chip_html(_deal(1, deal_source="remote_only")) == ""


def test_signal_chips_html_caveat_detached_remote_atl():
    d = _deal(1, sold_tier_caveat=True, sold_tier_caveat_grade="NM",
              sold_tier_caveat_value=52.0, sold_median_currency="EUR",
              detached_low=True, is_deal_remote=True, historical_floor_pct=18)
    html = notifier._signal_chips_html(d)
    assert "⚠ NM sells ~€52.00" in html
    assert "verify" in html.lower()
    assert "★ Discogs Deal" in html
    assert "⬇ All-time low" in html
    # The confidence (method) chip is NOT in this row.
    assert "SOLD-validated" not in html


def test_signal_chips_html_empty_when_none():
    assert notifier._signal_chips_html(_deal(1)) == ""


def test_deal_html_new_hierarchy_and_cuts():
    d = _deal(1, release_artist="Miles Davis", release_title="Kind of Blue",
              release_year=1959, release_format="LP", release_country="US",
              media_condition="Near Mint (NM or M-)", sleeve_condition="Very Good Plus (VG+)",
              buyer_price=45.0, buyer_currency="EUR", shipping_buyer_price=7.0,
              landed_price=52.0, landed_currency="EUR", discount_pct=45,
              deal_source="below_sold_median", median_value=95.0, median_currency="EUR",
              sold_median_value=95.0, sold_median_currency="EUR", sold_data_points=8,
              sold_low_value=26.0, sold_high_value=160.0, sold_last_date="2026-01-23",
              historical_floor_pct=18, historical_data_points=24)
    html = notifier._deal_html(d)
    # Kept, promoted:
    assert "Miles Davis — Kind of Blue" in html
    assert "NM/VG+ · 1959 · LP · US" in html
    assert "SOLD-validated" in html
    assert "SOLD median €95.00" in html
    assert "€45.00 + €7.00 ship = €52.00 landed" in html
    assert "⬇ All-time low" in html
    # Cut by the redesign:
    assert "Discogs-wide" not in html
    assert "vs NM median" not in html
    assert "vs NM SOLD median" not in html
    assert "all-time low (−18%" not in html  # the snippet dup is gone; the badge stays


def test_deal_html_omits_empty_signal_row_and_extras():
    d = _deal(1, deal_source="below_sold_median", sold_median_value=20.0,
              sold_median_currency="EUR", sold_data_points=5)
    html = notifier._deal_html(d)
    assert "more copies / this seller" not in html  # no siblings/shipping → no extras block


def test_build_text_new_order_and_cuts():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    d = _tiered_deal(deal_source="below_sold_median", sold_median_value=50.0,
                     sold_data_points=14, sold_low_value=15.0, sold_high_value=120.0,
                     sold_last_date="2026-02-10", buyer_price=30.0, buyer_currency="EUR",
                     shipping_buyer_price=9.0, landed_price=39.0, landed_currency="EUR",
                     historical_floor_pct=12)
    text = notifier._build_text([d], now, 0)
    assert "SOLD median €50.00" in text                   # proof line
    assert "€30.00 + €9.00 ship = €39.00 landed" in text  # cost equation
    assert "Also sold: VG+↑ €50.00" in text               # ladder kept in text
    assert "⬇ all-time low" in text                        # rides the primary line
    assert "Discogs-wide" not in text                      # cut
    assert "vs VG+ median" not in text                     # cut
    assert "all-time low (−12%" not in text                # snippet dup gone


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
    # JSON publish format: POST to the server root (rstrip '/'); topic + everything
    # Unicode rides in the body. Only the (ASCII) bearer token is a header.
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
    # No Unicode ever rides in a header (the latin-1 bug that broke the first run).
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
    d = _deal(1, release_artist="Miles Davis", discount_pct=45,
              effective_discount=0.45)
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
    # post() raises → send() returns normally, no exception escapes (fail-open).
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
    # Regression: the first live run died with
    #   'latin-1' codec can't encode character '−'
    # because the U+2212 minus in the title was set as an HTTP header; a non-Latin
    # artist name would break it too. With the JSON format the title rides in the
    # UTF-8 body and headers stay ASCII.
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
