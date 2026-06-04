# discogs-wanted-scanner

Hourly cron that scans your personal Discogs **wantlist** marketplace listings,
filters them by condition (vinyl *and* sleeve must meet the `MIN_MEDIA_CONDITION`
/ `MIN_SLEEVE_CONDITION` floors, default NM), evaluates how far each is priced
below the typical landed cost for its release, and emails a single collected
HTML digest of the good deals.

## How it gets the listings (no public API)

The official Discogs API has **no endpoint to list marketplace listings by
release** — `/marketplace/search?release_id=` was shut down in 2015. This tool
instead uses the internal JSON API that the Discogs website itself calls on
`/shop/mywants/`:

```
GET https://www.discogs.com/api/shop-page-api/sell_item
    ?sort=listedDate&sortOrder=descending&count=100&offset=0
```

It returns your wantlist marketplace listings, newest-first, as JSON.
Authentication is via your **browser session cookies** (`sid` + `session`),
**not** a personal access token. The session cookie lasts ~6 months; you store
both values in `cookies.json` (gitignored).

The official Discogs API (a PAT) is used only for optional enrichment: the v3
marketplace shipping-policies endpoint (per-deal shipping hints) and
`/users/{username}/wants` (the wantlist-size count). Both are opt-in via
`DISCOGS_TOKEN`.

> **Note on field names:** `shop_api.parse_listing()` reads the documented
> camelCase fields from `/sell_item`. If Discogs changes the response shape,
> run with `DEBUG=1` to dump the raw keys and adjust.

## Setup

```sh
git clone <this repo> && cd discogs-wanted-scanner
uv sync --extra test                           # installs deps + pytest

cp .env.example .env       && $EDITOR .env     # SMTP creds, knobs
cp cookies.json.example cookies.json           # paste sid + session
```

**Get the cookies:** open discogs.com in Chrome/Brave → DevTools (F12) →
Application → Cookies → `https://www.discogs.com` → copy the `sid` and
`session` values into `cookies.json`. They last ~6 months; the watcher emails
you 14 days before expiry (and again if a run is rejected with 401/403).

## Run

```sh
uv run python watcher.py                       # one real run; sends email
uv run python watcher.py --full                # loud full run (re-surface + email + push)
DEBUG=1 uv run python watcher.py               # verbose logging
uv run pytest                                  # unit tests, no network
```

State lives in `state.db` (SQLite, gitignored): last-run timestamp plus the
listings you've already been alerted/pushed on and at what price. The watcher
starts from an empty `state.db` on first run and creates the schema itself
(`state.json` is no longer used).

- `uv run python watcher.py` — normal cron run.
- `uv run python watcher.py --full` — loud full run: re-surface every current
  deal (both the email-alerted and push dedup sets are bypassed for this run
  only), force-refresh sold prices (TTL forced to 0), and email + push the lot —
  capped by the usual limits (`MAX_DEALS_PER_EMAIL`, `PUSH_MAX_PER_RUN`,
  `MAX_EMAILS_PER_DAY`). Runs against and updates the real `state.db`, so price
  history and the dedup sets keep accumulating normally afterwards.

Each run also writes `report.html` — a standalone snapshot of the current deals
(no server, no port). Set `REPORT_HTML` to redirect it elsewhere.

## Cron

Edit your crontab with `crontab -e` and paste:

```cron
7 * * * * $HOME/.local/bin/uv run --directory $HOME/discogs-wanted-scanner python watcher.py >> $HOME/discogs-wanted-scanner/watcher.log 2>&1
```

If your `uv` lives elsewhere, run `which uv` and substitute that path.

## What counts as a deal

For each release on your wantlist with listings in qualifying condition
(Mint / Near Mint / VG+, both media and sleeve), the watcher evaluates each
condition bucket against real sold prices (when available) or the asking median
(as a low-confidence fallback).

- **Landed cost** = item price + shipping, converted to your account currency.
- **Effective cost** = landed cost **plus an estimated import-VAT uplift** for
  non-EU origins (UK is non-EU post-Brexit). EU/domestic prices already include
  VAT and are left untouched, so the comparison stays apples-to-apples.
- **All-in benchmark** = sold/asking median **plus `SHIPPING_ALLOWANCE`**. Sold prices
  are item-only (no shipping), so comparing a buyer's effective cost against the bare
  median would unfairly penalise the shipping every past buyer also paid. The allowance
  corrects for this; `0` gives a strict gate (effective cost ≤ bare median).
- A copy is a deal when its effective cost lands **at or below the all-in benchmark**.
  Copies whose shipping/VAT push them above the benchmark are **dropped** (not shown).
- A listing is a deal depending on data quality:
  - **SOLD path** (enough recent same-condition sales, ≥ `SOLD_PRICE_MIN_POINTS`): the
    item price is compared to the lower tail of real sold prices. A listing qualifies
    when its price is at or below the `SOLD_DEAL_PERCENTILE`-th percentile of those
    prices — i.e. cheaper than most recent buyers actually paid — AND its effective cost
    is at or below the all-in benchmark. Scale-free and self-calibrating per release.
    A small materiality floor (`SOLD_DEAL_MIN_DISCOUNT`) filters out trivially-tight
    markets. The **displayed discount and ranking use effective landed cost** — the
    copy cheapest to actually receive leads, and the rail is coloured red for a notably
    big discount (low-confidence and caveated deals never go red).
  - **ASKING path** (low-confidence fallback when sold data is thin): item price vs the
    asking median, at the steeper `ASKING_DATA_DEAL_THRESHOLD` bar and only when the
    pool has at least `ASKING_MIN_POINTS` listings (small pools produce phantom deals
    off an inflated median), AND effective cost ≤ asking median + `SHIPPING_ALLOWANCE`.
    Each deal is tagged in the digest with its method (✓ SOLD-validated vs ≈ asking-only).
- Deals are ranked **deepest landed (effective-cost) discount first**.
- Solo / Discogs-flagged listings have no computed in-pool discount and sort last.

A previously-alerted listing only re-alerts when its price has since dropped a
further `PRICE_DROP_THRESHOLD` (example 5%).

**All-time low.** Separately from the in-pool median, the watcher remembers the
lowest landed price it has actually observed for each release+condition over a
rolling window (`PRICE_HISTORY_DAYS`). When a new deal undercuts every prior
observation — and there are at least `PRICE_HISTORY_MIN_POINTS` of them — it gets
an **⬇ All-time low** badge. This builds up in `state.db` over time, so the
badge stays quiet until there's enough history to mean something.

## Tuning knobs (`.env`)

Every var below is **required** — the watcher aborts on startup with the full
list of anything unset. Copy `.env.example` and edit. The exceptions, marked
_(optional)_, are feature toggles whose unset state means "feature off", not a
hidden default. The `SMTP_*` keys live in the [Email](#email) section.

| Var | Example | Effect |
|---|---|---|
| `MY_COUNTRY` | `Germany` | Your country — classifies shipping as domestic / EU / international and drives the VAT estimate. |
| `MIN_MEDIA_CONDITION` | `NM` | Minimum vinyl grade kept (`M`/`NM`/`VG+`/… or the full Discogs string). |
| `MIN_SLEEVE_CONDITION` | `NM` | Minimum sleeve grade kept. Both media *and* sleeve must clear their floor. |
| `SOLD_DEAL_PERCENTILE` | `20` | SOLD path: a listing is a deal when its item price is at or below this percentile of real sold prices (cheaper than most recent buyers paid). Self-calibrating per release. |
| `SOLD_DEAL_MIN_DISCOUNT` | `0.05` | SOLD path: materiality floor — must still be this fraction below the sold median so trivially-tight markets don't alert. |
| `ASKING_DATA_DEAL_THRESHOLD` | `0.35` | ASKING path (low-confidence fallback): fraction below the asking median required to qualify. Steeper than the sold bar because asking prices are aspirational. |
| `ASKING_MIN_POINTS` | `5` | ASKING path: minimum pool size before the asking fallback fires (small pools produce phantom deals off an inflated median). |
| `VAT_RATE` | `0.19` | Import-VAT uplift applied to non-EU listings (`0` disables). |
| `SHIPPING_ALLOWANCE` | `7.0` | Allowance (account currency) added to the median to form the all-in deal benchmark; `0` = strict (effective cost ≤ bare median). |
| `PRICE_DROP_THRESHOLD` | `0.05` | Re-alert when a seen listing's price drops this much further. |
| `SELLER_RATING_MIN` | _(optional)_ | Only fetch listings from sellers with ≥ this rating (0–100). |
| `DIGEST_MODE` | `hourly` | `daily` accumulates and emails once at `DIGEST_HOUR_UTC`. |
| `DIGEST_HOUR_UTC` | `7` | Hour (UTC) to flush when `DIGEST_MODE=daily`. |
| `GROUP_BY_RELEASE` | `true` | Collapse multiple sellers per release into primary + siblings. |
| `MAX_SIBLINGS_PER_RELEASE` | `1` | Runner-up listings shown per release (primary + N). |
| `MAX_DEALS_PER_EMAIL` | `0` | `0` = no cap. |
| `MAX_EMAILS_PER_DAY` | `4` | Safety brake against runaway alerting. |
| `MAX_PAGES_PER_RUN` | `30` | Pagination cap on `/sell_item`. |
| `DISCOGS_TOKEN` | _(optional)_ | PAT — enables per-deal shipping hints (see below). |
| `DISCOGS_USERNAME` | _(optional)_ | With `DISCOGS_TOKEN`, adds an "X of Y wantlist releases for sale" summary. |
| `SHIPPING_HINTS` | `true` | Show per-seller shipping policy + other wantlist items from the same seller (needs `DISCOGS_TOKEN`). |
| `EST_GRAMS_PER_VINYL` | `250` | Per-record weight estimate for weight-based shipping tiers. |
| `MAX_SELLER_PICKS` | `5` | Max "also wanted from this seller" rows per deal. |
| `SHIPPING_POLICY_TTL_DAYS` | `30` | How long a fetched shipping policy stays cached. |
| `COMBINE_BASKET` | _(optional, off)_ | Active "add these to save €X shipping" recommendation per deal — which other wantlist items from the same seller cross their free-shipping threshold or fill the fee tier. Needs `SHIPPING_HINTS` + `DISCOGS_TOKEN`. |
| `MAX_BASKET_ITEMS` | `3` | Max items a single combine-shipping suggestion may add. |
| `PRICE_HISTORY_DAYS` | `365` | Rolling window of observed prices kept in `state.db` for the all-time-low signal. |
| `PRICE_HISTORY_MIN_POINTS` | `3` | Observations required before an "⬇ all-time low" badge can fire. |
| `HEALTHCHECK_URL` | _(optional)_ | Ping on each successful run (e.g. healthchecks.io). |

## Optional: Discogs PAT enrichment

A personal access token (https://www.discogs.com/settings/developers) unlocks
per-deal **shipping hints**, paced ≤1 call/sec and well under the 60/min PAT
limit: the seller's shipping policy plus how many more of your wantlist items they
have, so you can combine an order. With `DISCOGS_USERNAME` it also adds the
"X of Y wantlist releases for sale" count in the digest header.

The v3 shipping-policies endpoint is gated by Discogs behind **seller settings**
on the token's account (currency + shipping policy, at
https://www.discogs.com/settings/seller). The account need not list anything for
sale, but the seller profile must exist. If you'd rather not, leave
`DISCOGS_TOKEN` unset and the watcher runs without it.

## Optional: real-time push fast-lane (ntfy)

Routes the strongest, SOLD-validated deals (and every all-time-low find) to an
instant phone push via [ntfy](https://ntfy.sh) the moment they are detected,
instead of waiting for the next email digest. It is **additive** — pushed deals
still appear in the digest exactly as before; the email stays the system of record.

Set `PUSH_ENABLED=true` and a private `NTFY_TOPIC` (any unguessable string), then
subscribe the free ntfy mobile app to that topic. Tapping a push opens the Discogs
listing directly. A deal pushes once and re-pushes only after a further
`PRICE_DROP_THRESHOLD` price drop; pushes are capped at `PUSH_MAX_PER_RUN` per run.
A `--full` run is loud on purpose — it bypasses the push dedup so every current
push-worthy deal re-pushes (still capped at `PUSH_MAX_PER_RUN`). A push failure
never blocks or affects the email digest.

The push fires on the same cron run that detects the deal, so it beats the email by
the digest-flush interval (up to an hour), not by minutes — tighten the cron
interval on the Pi for a truly faster heads-up. See `.env.example` for all keys
(`NTFY_SERVER`, `NTFY_TOKEN`, `PUSH_MIN_DISCOUNT`, `PUSH_PRIORITY`,
`PUSH_MAX_PER_RUN`).

## Email

Any SMTP server works. For a headless box, a local SMTP bridge can expose an
endpoint on `SMTP_HOST=127.0.0.1`, `SMTP_PORT=1025`; generic providers use
STARTTLS (port 587) or SSL (port 465). The header banner image
(`records-header-email.jpg`) is embedded as a base64 data-URI so it renders even
in clients that block remote images.

## Layout

```
watcher.py        entry point: fetch → core → network annotations → email → state
core.py           pure deal pipeline: filter → evaluate → group → sort → all-time-low
models.py         Listing + Deal dataclasses — the typed data surface
evaluator.py      condition filter, effective-cost median, VAT, shipping region
shop_api.py       internal /sell_item client + cookie session auth
discogs_api.py    official PAT API (price suggestions, wantlist size)
shipping_policy.py  v3 shipping-policy fetch + landed-room estimate
notifier.py       EmailNotifier — HTML + plain-text digest, admin alerts
tests/            pytest, no network
```
