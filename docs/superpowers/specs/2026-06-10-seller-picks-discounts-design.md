# Discounts on "also from this seller" picks — design

**Date:** 2026-06-10
**Status:** approved

## Goal

The digest's "also from this seller" pick rows show price, condition, and title —
but not whether the pick is itself a good deal. Add a signed discount badge per
pick so it's obvious at a glance which add-ons are good value, which are
overpriced, and which have no benchmark.

## Decisions (settled in brainstorming)

1. **Basis:** pick's buyer price vs the **sold median for its condition** — no
   shipping allowance or VAT in the math. Add-ons ride in the same parcel, so
   item-vs-median is the fair frame. Uses `sold_stats_by_release`, which is
   already fetched for every release in the scan — zero extra network calls.
2. **Coverage:** show both directions. Below median → green discount; above
   median → muted "overpriced" figure. No badge unambiguously means "no sold
   data".
3. **Sorting + cap:** deepest discount first; picks without sold data go last,
   cheapest-first. The `MAX_SELLER_PICKS` cap applies after the sort, so the
   best-value items make the cut.
4. **Scope:** pick rows only. The 🛒 combine-shipping basket lines stay as they
   are (their items usually reappear as pick rows right below). Push messages
   and an asking-median fallback are out of scope.

## Design

### Data flow

`watcher.main` already holds `sold_stats_by_release` when it calls
`_annotate_shipping` (watcher.py:597). Pass it through as a new parameter;
`_annotate_shipping` hands it to `evaluator.seller_picks`, which gains an
optional `sold_stats_by_release=None` parameter (default preserves existing
callers and tests).

### Computation

New private helper in `evaluator`:

```
_pick_discount(listing, stats) -> int | None
```

- `median = stats["by_condition"][listing.media_condition]["median"]` — the
  same lookup pattern `core.annotate_sold_price` uses.
- Currency guard: `stats["currency"]` must equal the pick's display currency
  (`buyer_currency or currency`), because the compared price is
  `buyer_price or price`.
- `pct = round((1 - price / median) * 100)` — **signed**: positive = below the
  sold median (a discount), negative = above it.
- Returns `None` on any missing piece: no stats for the release, no
  same-condition sold data, median ≤ 0, price ≤ 0, or currency mismatch.
  Fail-open, like the rest of the pipeline.
- **No min-points gate.** Display-only context, consistent with the existing
  "SOLD median" digest snippet, which also shows ungated. Deal verdicts stay
  gated by `SOLD_PRICE_MIN_POINTS` as before.

### Pick dict and sorting

`seller_picks` adds one key to each pick dict: `discount_pct: int | None`.

Sort key: `(discount_pct is None, -(discount_pct or 0), price)` — badged picks
first, deepest discount first, no-data picks last cheapest-first. Cap at
`limit` after sorting. `total_others` is unchanged.

Pick dicts are persisted inside pending deals (`Deal.to_pending`); the new key
round-trips automatically, and the renderer reads it with `.get()`, so pending
deals saved before this change render exactly as today.

### Rendering

Both `_shipping_html` and `_shipping_text` insert the badge between price and
condition, mirroring the rail's display convention (`−{pct}%`, notifier.py:259):

```
+ €9.00 · −18% · VG+ · Bill Evans – Waltz for Debby    below median: green #1b5e20, bold
+ €14.00 · +15% · VG+ · Some Other Record              above median: muted #999
+ €7.50 · VG+ · No Sold Data Record                    no badge
```

Display sign is the inverse of the stored sign: stored `+18` (18% below
median) renders `−18%`; stored `−15` renders `+15%`. A stored `0` renders as
muted `±0%`.

### Testing

- `_pick_discount`: below median, above median, exactly at median, missing
  stats, missing condition, currency mismatch, median ≤ 0.
- `seller_picks`: discount-first sort, no-data-last ordering, cap keeps the
  deepest discounts, dict carries `discount_pct`.
- Renderers: green badge, muted badge, no badge, and a legacy pick dict
  without the key (HTML and text).
- `preview_email.py`: sample data updated to demo all three badge states.

## Files touched

| File | Change |
|---|---|
| `evaluator.py` | `_pick_discount` helper; `seller_picks` signature, sort, dict key |
| `watcher.py` | thread `sold_stats_by_release` into `_annotate_shipping` |
| `notifier.py` | badge in `_shipping_html` / `_shipping_text` |
| `preview_email.py` | sample picks demo the three states |
| `tests/` | new/updated tests per above |
