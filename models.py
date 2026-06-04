"""
Typed data model for the watcher.

`Listing` is one normalized `/sell_item` marketplace row (the output of
`shop_api.parse_listing`). `Deal` is an evaluated listing: it *is* a `Listing`
(inherits every field) plus the verdict the evaluator computed and the optional
annotations the orchestrator layers on (all-time-low floor, sibling listings,
per-seller shipping hint).

Why inheritance: a deal is a listing with extra facts, and the renderer reads
both kinds of field off the same object, so a flat `Deal(Listing)` keeps access
uniform (`deal.release_title`, `deal.discount_pct`) without duplicating ~25
field declarations.

Some `Listing` fields (genres, accepts_offers, seller_rating_count,
previous_buyer_price) and some verdict fields (median_value/currency) are not
rendered yet — they're carried deliberately as a typed surface for the UI rework,
not by accident.
"""

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Listing:
    """A normalized marketplace listing for a wantlist release."""

    id: int = 0
    listed_at: datetime | None = None
    media_condition: str | None = None
    sleeve_condition: str | None = None

    # Native (seller) currency.
    price: float = 0.0
    currency: str | None = None

    # Pre-converted to the buyer's account currency (what medians compare against).
    buyer_price: float = 0.0
    buyer_currency: str | None = None
    # Previous buyer-currency price (reserved for a "was €X" UI line); Discogs
    # reports prev == current when there's been no change.
    previous_buyer_price: float | None = None

    shipping_price: float | None = None
    shipping_buyer_price: float | None = None

    image_url: str | None = None
    comments: str = ""
    accepts_offers: bool = False
    is_deal_remote: bool = False  # Discogs' own marketplace "Deal" flag

    release_id: int | None = None
    release_title: str | None = None
    release_artist: str | None = None
    release_year: int | None = None
    release_country: str | None = None
    release_format: str | None = None
    release_genres: list[str] = field(default_factory=list)

    seller_uid: int | None = None
    seller_username: str | None = None
    seller_rating: float | None = None
    seller_rating_count: int | None = None
    ships_from: str | None = None

    listing_url: str = ""


@dataclass
class Deal(Listing):
    """A listing the evaluator flagged as a deal, plus optional annotations."""

    # ── Verdict (set by evaluator) ───────────────────────────────────────────
    deal_reason: str = ""
    deal_source: str = ""
    discount_pct: int | None = None
    effective_discount: float | None = None
    ranked: bool = False
    median_value: float | None = None
    median_currency: str | None = None
    landed_price: float | None = None
    landed_currency: str | None = None
    effective_cost: float | None = None
    vat_amount: float | None = None
    vat_estimated: bool = False
    shipping_region: str = ""
    # Confidence / guard flags (set by evaluator):
    # low_confidence  — asking-fallback verdict (asking pool is aspirational).
    # detached_low    — fallback: cheapest copy sits far below the next cheapest
    #                   (possible mispriced/wrong pressing — verify).
    low_confidence: bool = False
    detached_low: bool = False

    # ── Annotations (set by the orchestrator, all optional) ──────────────────
    historical_floor_value: float | None = None
    historical_floor_pct: int | None = None
    historical_data_points: int | None = None
    sold_median_value: float | None = None
    sold_median_currency: str | None = None
    sold_low_value: float | None = None
    sold_high_value: float | None = None
    sold_last_date: str | None = None
    sold_data_points: int | None = None
    # Higher-tier sold context (set by the orchestrator from evaluator.sold_tiers):
    # what the record fetches at this grade-and-up (pooled) and at each better grade
    # individually, so the email shows the condition premium.
    sold_tier_at_or_above: dict | None = None
    sold_tier_higher: list[dict] = field(default_factory=list)
    # Better-grade caveat (set by the evaluator): a better grade sold for ~the same
    # or less, so the apparent discount is suspect (the digest mutes the red rail).
    sold_tier_caveat: bool = False
    sold_tier_caveat_grade: str | None = None
    sold_tier_caveat_value: float | None = None
    siblings: list[dict] = field(default_factory=list)
    shipping_hint: dict | None = None
    basket: dict | None = None  # combine-shipping recommendation (set by orchestrator)
    seller_picks: list[dict] = field(default_factory=list)
    seller_total_others: int = 0

    @classmethod
    def from_listing(cls, listing: Listing, **verdict) -> "Deal":
        """Build a Deal carrying over every field of `listing`."""
        return cls(**dataclasses.asdict(listing), **verdict)

    def to_pending(self) -> dict:
        """JSON-safe dict for state.json. Drops `listed_at` (a datetime, and
        unused once a deal is queued for the digest)."""
        d = dataclasses.asdict(self)
        d.pop("listed_at", None)
        return d

    @classmethod
    def from_pending(cls, d: dict) -> "Deal":
        """Rebuild a Deal from a persisted dict, tolerating legacy/renamed keys
        (unknown keys dropped, missing keys defaulted)."""
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
