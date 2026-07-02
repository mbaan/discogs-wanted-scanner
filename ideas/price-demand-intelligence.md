# Price & Demand Intelligence (parked)

**Status:** Parked idea — not scheduled. Captured 2026-06-04.
**Gain:** ★★★★★ (the strongest differentiator — see Competitive context).
**Effort:** M for the MVP, L–XL for the full longitudinal version.
**Pairs with:** the SQLite datastore (feature "B") for the longitudinal variant.

## The idea

Today a deal is judged against a **static** sold-median snapshot. This feature adds a
*market* read on top of value: is the record **rising or cooling**, how **fast** copies
sell (**velocity**), and how **rare** it is on the market (**scarcity**). A deal then
becomes *"below median **AND** the market is hot / rarely available"* — a far stronger
buy signal than price alone, and one no competitor or Discogs itself produces for your
wantlist.

## Key insight (why an MVP is cheap)

The `/sell/history` page the watcher **already fetches** lists recent individual sales
**with dates**. So **velocity and short-term trend are computable from a single fetch** —
no waiting to accumulate. Only the *long-horizon* trend needs history built up over time
(that's the part that wants SQLite).

## How it would land in this codebase

- **`sold_prices.parse_sell_history`** — currently aggregates each condition into
  `median/count/low/high/prices` + one `last_sold` date. Extend it to also carry per-sale
  `(date, media_condition, price)` rows, so velocity/trend can be derived.
- **New signal computer** (in `evaluator` or a small `market.py`):
  - **velocity** = sales/month over the observed window (per release; per condition where data allows)
  - **trend** = recent-half median vs older-half median (or a guarded slope), gated by min sales
  - **scarcity** = sales/year, distinct sellers, time since last sale
- **`models.Deal`** — new optional fields (`market_velocity`, `market_trend`, `market_scarcity`).
- **`core` (annotate stage)** — attach the signals for display.
- **`evaluator` gating (optional)** — let a deal *require* hot/scarce in addition to
  below-median, or just boost ranking + add a badge (start with display-only).
- **`notifier`** — render badges, e.g. `🔥 hot · 4 sold/mo · climbing`, `🦤 scarce · 2 sold/yr`.
- **Longitudinal (with SQLite)** — persist observed sales across runs (dedup by
  date+cond+price), building a real multi-month series → reliable trend + market context
  beyond the current rolling all-time-low.

## Effort split

- **MVP (M):** single-fetch velocity + short trend + scarcity, display-only badges, tests.
- **Full (L–XL):** longitudinal accumulation, optional gating, and a history rich enough
  to later backtest thresholds.

## Risks

- HTML parse fragility — already handled fail-open; reuse that discipline.
- Statistical over-claiming on thin data — reuse the existing min-points gating.
- The `/sell/history` window is only the recent N sales, so velocity is *recent*, not
  lifetime, until accumulation kicks in.

## Competitive context

`discogs_alert`, `discdogs.app`, and Discogs' own notifications all stop at availability +
a fixed price threshold. None read demand/trend. This feature is the clearest moat.
