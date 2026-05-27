"""Parse the /sell_item fixture and verify field extraction + edge cases.

If tests/fixtures_sell_item.json is present (a real live capture, gitignored
because it contains account-specific buyer prices) we use it; otherwise we
fall back to a synthetic minimal example that mirrors the documented schema.
"""
import json
from pathlib import Path

import shop_api

FIXTURE = Path(__file__).parent / "fixtures_sell_item.json"

_SYNTHETIC = {
    "totalCount": 1,
    "items": [{
        "itemId": 4178676786,
        "allowsOffers": False,
        "availability": {"isAvailable": True, "reason": None},
        "comments": "Sealed. Re-press.",
        "imageUrl": "https://i.discogs.com/test.jpeg",
        "inCart": False,
        "isDeal": False,
        "listedDate": "2026-05-27T11:48:26Z",
        "mediaCondition": "Mint (M)",
        "sleeveCondition": "Mint (M)",
        "offerMade": False,
        "previousPrice": {"amount": 22.5, "amountUsd": 26.16, "currencyCode": "EUR",
                          "buyerItemPrice": 22.5, "buyerCurrencyCode": "EUR"},
        "price": {"amount": 22.5, "amountUsd": 26.16, "currencyCode": "EUR",
                  "buyerItemPrice": 22.5, "buyerCurrencyCode": "EUR"},
        "release": {"releaseId": 14270021, "title": "Midnight Marauders", "year": 2019,
                    "country": "Europe", "majorFormat": "Vinyl",
                    "formatNames": ["Vinyl", "Album", "Reissue", "LP"],
                    "artists": [{"artistId": 3268, "name": "A Tribe Called Quest"}],
                    "genres": [{"genreId": 2, "name": "Hip Hop"}],
                    "labels": [{"labelId": 2186, "name": "Jive", "catno": "01241414901"}],
                    "rating": 5.0, "styles": []},
        "seller": {"uid": 533430, "name": "mvc.be", "rating": 99.804,
                   "ratingCount": 18541, "shipsFrom": "Belgium",
                   "minBuyerRating": 0.0, "independentSeller": True},
        "shipping": {"shippingPrice": 8.5, "buyerShippingPrice": 8.5, "freeShippingMin": None},
    }]
}


def _fixture():
    if FIXTURE.exists():
        return json.loads(FIXTURE.read_text())
    return _SYNTHETIC


def test_fixture_has_items():
    data = _fixture()
    assert data["items"], "live fixture should have items"
    assert "totalCount" in data


def test_parse_listing_minimal_fields():
    item = _fixture()["items"][0]
    parsed = shop_api.parse_listing(item)
    assert parsed is not None
    # Required identifiers
    assert isinstance(parsed["id"], int)
    assert parsed["release_id"] is not None
    assert parsed["release_title"]
    assert parsed["release_artist"]
    # Buyer-currency price is what we compare against medians
    assert parsed["buyer_price"] > 0
    assert parsed["buyer_currency"]
    assert parsed["price"] > 0
    # Discogs' deal flag is captured (bool, default False if absent)
    assert isinstance(parsed["is_deal_remote"], bool)
    # Shipping fields present (may be None for free shipping)
    assert "shipping_price" in parsed
    assert "shipping_buyer_price" in parsed
    # Image + comments captured
    assert "image_url" in parsed
    assert "comments" in parsed


def test_parse_skips_unavailable_listing():
    item = dict(_fixture()["items"][0])
    item["availability"] = {"isAvailable": False, "reason": "sold"}
    assert shop_api.parse_listing(item) is None


def test_parse_returns_none_without_item_id():
    item = dict(_fixture()["items"][0])
    item.pop("itemId", None)
    assert shop_api.parse_listing(item) is None


def test_session_expiry_parses():
    # Shape mirrors Discogs' real `session` cookie. _expires base64-encodes a
    # unix timestamp (here: 1795431039 → 2026-11-23).
    cookies = {
        "session": "tok=?_expires=MTc5NTQzMTAzOQ==&created_at=ImlnbW9yZWQi",
    }
    exp = shop_api.session_expires_at(cookies)
    assert exp is not None
    assert exp.tzinfo is not None
    assert exp.year == 2026 and exp.month == 11


def test_session_expiry_handles_missing():
    assert shop_api.session_expires_at({}) is None
    assert shop_api.session_expires_at({"session": "no-expires-here"}) is None
    assert shop_api.session_expires_at({"session": "tok=?_expires=garbage"}) is None
