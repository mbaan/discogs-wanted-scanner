"""
The Weekly Dig — a once-a-week wantlist status digest, separate from the hourly
deal alerts (and never touching push), so it can be reflective without adding
inbox/notification noise.

Two questions the hourly digest can't answer on its own:

  1. What's being *watched but never turns into a deal*? A copy can ship to you
     every hour yet always sit above what the record actually sells for — so it
     never alerts. This surfaces those, ranked by how close the cheapest copy
     ever got to the all-in going rate (SOLD median + shipping allowance).

  2. Which never-hitting releases have a *more locally-available pressing* worth
     wantlisting instead (the swap suggestions come from `reissue_finder`).

This module is the pure, network-free reconstruction over data the watcher
already persists (`price_history` + `sell_history`) plus the wantlist. The deal
line mirrors the evaluator's all-in gate: a copy is a deal only when its
effective cost lands at or below `SOLD median + shipping_allowance`, so
`cheapest_landed > median + allowance` is a sound "never a deal" (it fails that
gate for certain, VAT only making it worse). Cadence + assembly live in the
caller; rendering in `notifier`.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Re-send guard: a Dig is weekly, but the hourly cron may fire several times in
# the send window. Once sent, suppress until clearly a new week (< this many
# days since the last send → skip). 6 days lets the *next* same-weekday slot
# (7 days out) through while blocking same-day repeats.
_MIN_DAYS_BETWEEN = 6


@dataclass
class NeverDeal:
    """A wantlist release seen for sale (at your grade) but never at deal price,
    described by the condition whose cheapest copy came *closest* to a deal."""
    release_id: int
    artist: str | None
    title: str | None
    condition: str
    cheapest_seen: float   # cheapest landed (item + shipping) ever observed
    sold_median: float     # SOLD median for this condition
    benchmark: float       # sold_median + shipping_allowance (the all-in going rate)
    gap_eur: float         # cheapest_seen − benchmark (> 0: how far over the going rate)
    gap_pct: int           # cheapest_seen vs sold_median, as a signed %
    currency: str


@dataclass
class DigStats:
    total_wantlist: int
    seen_count: int                       # wantlist releases seen for sale (at grade)
    gets_deal: int                        # of those, ever reached deal price
    never_seen: list[dict] = field(default_factory=list)        # {release_id, artist, title, year}
    seen_never_deal: list[NeverDeal] = field(default_factory=list)  # closest-first
    no_benchmark: int = 0                 # seen but < min sold points to judge


def should_send(now: datetime, dow: int, hour: int, last_sent_iso: str | None) -> bool:
    """Fire once on the configured weekday at/after `hour` UTC, then not again
    until the next week. The hourly cron stays the driver; this is the gate."""
    if now.weekday() != dow or now.hour < hour:
        return False
    if last_sent_iso:
        try:
            last = datetime.fromisoformat(last_sent_iso)
        except ValueError:
            last = None
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(days=_MIN_DAYS_BETWEEN):
                return False
    return True


def _index_by_release(price_history: dict) -> dict[int, dict[str, list]]:
    """{'rid:cond': [entries]} → {rid: {cond: [entries]}}."""
    out: dict[int, dict[str, list]] = {}
    for key, entries in (price_history or {}).items():
        rid_str, _, cond = key.partition(":")
        try:
            rid = int(rid_str)
        except ValueError:
            continue
        out.setdefault(rid, {})[cond] = entries
    return out


def compute(wantlist: list[dict], price_history: dict, sell_history: dict, cfg: dict) -> DigStats:
    """Bucket every wantlist release into never-seen / seen-never-a-deal /
    gets-deal, using only persisted data (no network).

    `wantlist`: [{release_id, artist, title, year}, ...]
    `price_history`: {'rid:cond': [{'d','p','c'}]}  (p = landed cost)
    `sell_history`: {str(rid): {'fetched_at', 'stats'}}  (stats = parse_sell_history shape)
    """
    min_points = cfg.get("sold_price_min_points") or 5
    allowance = cfg.get("shipping_allowance") or 0.0

    seen_by_rid = _index_by_release(price_history)

    never_seen: list[dict] = []
    never_deal: list[NeverDeal] = []
    no_benchmark = 0
    gets_deal = 0
    seen_count = 0

    for w in wantlist:
        try:
            rid = int(w["release_id"])
        except (KeyError, TypeError, ValueError):
            continue
        conds = seen_by_rid.get(rid)
        if not conds:
            never_seen.append({"release_id": rid, "artist": w.get("artist"),
                               "title": w.get("title"), "year": w.get("year")})
            continue
        seen_count += 1

        stats = (sell_history.get(str(rid)) or {}).get("stats") or {}
        by_cond = stats.get("by_condition") or {}
        sold_ccy = stats.get("currency")

        best: NeverDeal | None = None   # smallest gap across judged conditions
        dealt = False
        judged = False
        for cond, entries in conds.items():
            bc = by_cond.get(cond)
            if not isinstance(bc, dict) or bc.get("median") is None or (bc.get("count") or 0) < min_points:
                continue
            # Same-currency observations only (a landed price in another currency
            # is not comparable to the sold median); legacy rows without 'c' count.
            prices = [e["p"] for e in entries if e.get("c") in (None, sold_ccy)]
            if not prices:
                continue
            judged = True
            cheapest = min(prices)
            median = float(bc["median"])
            benchmark = median + allowance
            gap = cheapest - benchmark
            if gap <= 0:
                dealt = True
            nd = NeverDeal(
                release_id=rid, artist=w.get("artist"), title=w.get("title"),
                condition=cond, cheapest_seen=round(cheapest, 2), sold_median=median,
                benchmark=round(benchmark, 2), gap_eur=round(gap, 2),
                gap_pct=round((cheapest / median - 1.0) * 100) if median else 0,
                currency=sold_ccy or "EUR",
            )
            if best is None or gap < best.gap_eur:
                best = nd

        if not judged:
            no_benchmark += 1
        elif dealt:
            gets_deal += 1
        elif best is not None:
            never_deal.append(best)

    never_deal.sort(key=lambda nd: nd.gap_pct)   # closest to a deal first

    return DigStats(
        total_wantlist=len(wantlist), seen_count=seen_count, gets_deal=gets_deal,
        never_seen=never_seen, seen_never_deal=never_deal, no_benchmark=no_benchmark,
    )


def problem_releases(stats: DigStats, min_ratio: float) -> list[dict]:
    """The set worth asking Discogs about a better pressing for: every never-seen
    release, plus never-a-deal releases whose cheapest copy sits at least
    `min_ratio`× the sold median (chronic overpricing — a scarcity signal). Each
    item is {release_id, artist, title, year?}. Never-seen first (most likely to
    lack a local copy), then worst overpricing first. Callers cap how many they
    actually look up per run so Discogs isn't hammered."""
    out = list(stats.never_seen)
    over = [nd for nd in stats.seen_never_deal if nd.sold_median and
            nd.cheapest_seen / nd.sold_median >= min_ratio]
    over.sort(key=lambda nd: -(nd.cheapest_seen / nd.sold_median))
    out.extend({"release_id": nd.release_id, "artist": nd.artist, "title": nd.title}
               for nd in over)
    return out
