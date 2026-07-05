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
    recent_obs: int = 0    # days a copy was spotted in the window (backs cheapest_seen)
    sold_count: int = 0    # real sales behind sold_median (benchmark reliability)


@dataclass
class Drift:
    """A wantlist release whose observed floor has risen structurally over the
    retained history — its recent cheapest sits materially above its earlier
    cheapest, so judging it against old prices would flatter it."""
    release_id: int
    artist: str | None
    title: str | None
    condition: str
    earlier_floor: float   # cheapest landed seen BEFORE the recent window
    recent_floor: float    # cheapest landed seen WITHIN the recent window
    pct: int               # recent_floor vs earlier_floor, as a signed %
    currency: str
    recent_n: int = 0      # sightings behind recent_floor
    earlier_n: int = 0     # sightings behind earlier_floor


@dataclass
class DigStats:
    total_wantlist: int
    seen_count: int                       # wantlist releases seen for sale (at grade)
    gets_deal: int                        # of those, ever reached deal price
    never_seen: list[dict] = field(default_factory=list)        # {release_id, artist, title, year}
    seen_never_deal: list[NeverDeal] = field(default_factory=list)  # closest-first
    no_benchmark: int = 0                 # seen but < min sold points to judge
    drifting: list[Drift] = field(default_factory=list)         # floor risen, worst-first


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


def _ref_ccy(entries: list[dict]) -> str | None:
    """Reference currency for a condition's observations = the newest entry's — so
    a drift comparison never straddles two currencies (landed prices are the buyer
    currency, but legacy rows may differ)."""
    return max(entries, key=lambda e: e["d"]).get("c") if entries else None


def compute(wantlist: list[dict], price_history: dict, sell_history: dict,
            cfg: dict, now: datetime) -> DigStats:
    """Bucket every wantlist release into never-seen / seen-never-a-deal /
    gets-deal, and flag releases whose floor has drifted up. Persisted data only
    (no network).

    `wantlist`: [{release_id, artist, title, year}, ...]
    `price_history`: {'rid:cond': [{'d','p','c'}]}  (p = landed cost)
    `sell_history`: {str(rid): {'fetched_at', 'stats'}}  (stats = parse_sell_history shape)

    The deal gap uses only observations in the RECENT window (`dig_recent_days`), so
    a structurally risen record is judged against today's floor — not an old, cheap
    price that would flatter it — while `drifting` reports exactly those risers
    (recent floor vs earlier floor). With `dig_recent_days` large enough to cover the
    whole history, the gap reverts to the all-time-minimum behaviour."""
    min_points = cfg.get("sold_price_min_points") or 5
    allowance = cfg.get("shipping_allowance") or 0.0
    recent_days = cfg.get("dig_recent_days") or 45
    drift_min = cfg.get("dig_drift_min_pct") or 0.20
    cutoff = (now - timedelta(days=recent_days)).date().isoformat()

    seen_by_rid = _index_by_release(price_history)

    never_seen: list[dict] = []
    never_deal: list[NeverDeal] = []
    drifting: list[Drift] = []
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

        # ── Deal gap: cheapest in the RECENT window vs the sold going rate ──
        best: NeverDeal | None = None   # smallest gap across judged conditions
        dealt = False
        judged = False
        for cond, entries in conds.items():
            bc = by_cond.get(cond)
            if not isinstance(bc, dict) or bc.get("median") is None or (bc.get("count") or 0) < min_points:
                continue
            # Same-currency observations only (legacy rows without 'c' count). Prefer
            # the recent window; fall back to all-time so a release seen only long ago
            # isn't silently dropped from the counts.
            recent = [e["p"] for e in entries if e["d"] >= cutoff and e.get("c") in (None, sold_ccy)]
            allp = [e["p"] for e in entries if e.get("c") in (None, sold_ccy)]
            prices = recent or allp
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
                recent_obs=len(prices), sold_count=int(bc.get("count") or 0),
            )
            if best is None or gap < best.gap_eur:
                best = nd

        if not judged:
            no_benchmark += 1
        elif dealt:
            gets_deal += 1
        elif best is not None:
            never_deal.append(best)

        # ── Structural drift: recent floor materially above the earlier floor ──
        # Independent of sold data (it's about our own observations over time), so
        # it needs history on both sides of the window — quiet until that accrues.
        worst: Drift | None = None
        for cond, entries in conds.items():
            ref = _ref_ccy(entries)
            rec = [e["p"] for e in entries if e["d"] >= cutoff and e.get("c") in (None, ref)]
            old = [e["p"] for e in entries if e["d"] < cutoff and e.get("c") in (None, ref)]
            if not rec or not old:
                continue
            rf, of = min(rec), min(old)
            if of <= 0 or (rf / of - 1.0) < drift_min:
                continue
            d = Drift(release_id=rid, artist=w.get("artist"), title=w.get("title"),
                      condition=cond, earlier_floor=round(of, 2), recent_floor=round(rf, 2),
                      pct=round((rf / of - 1.0) * 100), currency=ref or "EUR",
                      recent_n=len(rec), earlier_n=len(old))
            if worst is None or d.pct > worst.pct:
                worst = d
        if worst is not None:
            drifting.append(worst)

    never_deal.sort(key=lambda nd: nd.gap_pct)   # closest to a deal first
    drifting.sort(key=lambda d: -d.pct)          # biggest riser first

    return DigStats(
        total_wantlist=len(wantlist), seen_count=seen_count, gets_deal=gets_deal,
        never_seen=never_seen, seen_never_deal=never_deal, no_benchmark=no_benchmark,
        drifting=drifting,
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
