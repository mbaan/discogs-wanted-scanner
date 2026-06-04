# Threshold backtesting (parked)

**Status:** Parked — **depends on the SQLite datastore accumulating history first.**
**Gain:** ★★★ (tune by evidence, not by feel). **Effort:** M, but only *after* SQLite lands
and has filled up.

## The idea

The deal knobs — `SOLD_DEAL_PERCENTILE`, `SOLD_DEAL_MIN_DISCOUNT`,
`ASKING_DATA_DEAL_THRESHOLD`, `ASKING_MIN_POINTS`, `SHIPPING_ALLOWANCE` … — are set by feel.
Backtesting replays real historical data through the deal pipeline at different knob values
to see what *would* have fired, so they can be tuned to taste (fewer false alarms / more
catches).

## Why it leans on accumulated SQLite data

`core.build_deals(listings, …, cfg, …)` is already pure and fully parameterized, so the
replay itself is trivial. The missing piece is **history to replay against**: today only
deals + a thin `price_history` are persisted, not the full per-run listing set. The SQLite
datastore is where each run's observed listings would be logged; backtesting replays over
that stored timeline.

**Cold-start:** this is useless until SQLite has accumulated weeks / months of runs — which
is exactly why it's parked *behind* the datastore rather than built now.

## Lightweight cousin (explicitly NOT this)

A `--tune` sensitivity preview that sweeps thresholds over a *single* current run's listings
needs no history and could ship anytime — but it only answers "how sensitive is today's
result?", not "what would I have caught over time?". This doc is about the latter,
history-backed form.

## How it would land (post-SQLite)

- A `listings` table populated each run (id, release, condition, native + buyer prices,
  ships_from, seen_at, …).
- A `backtest` / `replay` command: load a date range, run `core.build_deals` at a grid of
  cfg overrides, report deals fired + discount distributions per knob set.
- Output: a comparison table (optionally CSV) to choose knobs by evidence.
