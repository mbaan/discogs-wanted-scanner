# discogs-wanted-scanner

Hourly cron that scans your personal Discogs **wantlist** marketplace listings,
filters them to VG+/VG+ condition or better (vinyl *and* sleeve), evaluates how
far each is priced below the typical landed cost for its release, and emails a
single collected HTML digest of the good deals.

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

The official Discogs API (a PAT) is used only for optional enrichment:
`/marketplace/price_suggestions/{release_id}` (per-condition median) and the v3
marketplace shipping-policies endpoint (per-deal shipping hints). Both are
opt-in via `DISCOGS_TOKEN`.

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
DRY_RUN=1 uv run python watcher.py             # no SMTP; writes /tmp/digest.{html,txt}
DEBUG=1 uv run python watcher.py               # verbose logging
uv run pytest                                  # unit tests, no network
```

State lives in `state.json` (gitignored): last-run timestamp plus the listings
you've already been alerted on and at what price. The first run looks back one
hour, so you don't get flooded with old listings.

## Cron

Edit your crontab with `crontab -e` and paste:

```cron
7 * * * * $HOME/.local/bin/uv run --directory $HOME/discogs-wanted-scanner python watcher.py >> $HOME/discogs-wanted-scanner/watcher.log 2>&1
```

If your `uv` lives elsewhere, run `which uv` and substitute that path.

## What counts as a deal

For each release on your wantlist with listings in qualifying condition
(Mint / Near Mint / VG+, both media and sleeve), the watcher computes the
**median effective cost** within each condition bucket and flags any listing a
configurable fraction below it.

- **Landed cost** = item price + shipping, converted to your account currency.
- **Effective cost** = landed cost **plus an estimated import-VAT uplift** for
  non-EU origins (UK is non-EU post-Brexit). EU/domestic prices already include
  VAT and are left untouched, so the median comparison stays apples-to-apples.
- A listing is a deal at `DEAL_THRESHOLD` (default **35%**) below the bucket
  median. Deals are ranked **deepest discount first**; anything at or beyond
  `BIG_DEAL_THRESHOLD` (default 50%) gets a 🔥 badge.
- Solo / Discogs-flagged listings have no computed in-pool discount and sort last.

A previously-alerted listing only re-alerts when its price has since dropped a
further `PRICE_DROP_THRESHOLD` (default 5%).

## Tuning knobs (`.env`)

| Var | Default | Effect |
|---|---|---|
| `MY_COUNTRY` | `Netherlands` | Your country — classifies shipping as domestic / EU / international and drives the VAT estimate. |
| `DEAL_THRESHOLD` | `0.35` | How far below the bucket median to qualify. Lower = more alerts. |
| `BIG_DEAL_THRESHOLD` | `0.50` | Effective discount that earns the 🔥 badge. |
| `VAT_RATE` | `0.21` | Import-VAT uplift applied to non-EU listings (`0` disables). |
| `PRICE_DROP_THRESHOLD` | `0.05` | Re-alert when a seen listing's price drops this much further. |
| `SELLER_RATING_MIN` | (unset) | Only fetch listings from sellers with ≥ this rating (0–100). |
| `DIGEST_MODE` | `hourly` | `daily` accumulates and emails once at `DIGEST_HOUR_UTC`. |
| `DIGEST_HOUR_UTC` | `7` | Hour (UTC) to flush when `DIGEST_MODE=daily`. |
| `GROUP_BY_RELEASE` | `true` | Collapse multiple sellers per release into primary + siblings. |
| `MAX_SIBLINGS_PER_RELEASE` | `1` | Runner-up listings shown per release (primary + N). |
| `MAX_DEALS_PER_EMAIL` | `0` | `0` = no cap. |
| `MAX_EMAILS_PER_DAY` | `4` | Safety brake against runaway alerting. |
| `MAX_PAGES_PER_RUN` | `30` | Pagination cap on `/sell_item`. |
| `DISCOGS_TOKEN` | (unset) | Optional PAT — enables Discogs-wide median annotation + shipping hints (see below). |
| `DISCOGS_USERNAME` | (unset) | With `DISCOGS_TOKEN`, adds an "X of Y wantlist releases for sale" summary. |
| `SHIPPING_HINTS` | `true` | Show per-seller shipping policy + other wantlist items from the same seller (needs `DISCOGS_TOKEN`). |
| `EST_GRAMS_PER_VINYL` | `250` | Per-record weight estimate for weight-based shipping tiers. |
| `MAX_SELLER_PICKS` | `5` | Max "also wanted from this seller" rows per deal. |
| `SHIPPING_POLICY_TTL_DAYS` | `30` | How long a fetched shipping policy stays cached. |
| `HEALTHCHECK_URL` | (unset) | Optional ping on each successful run (e.g. healthchecks.io). |

## Optional: Discogs PAT enrichment

A personal access token (https://www.discogs.com/settings/developers) unlocks two
extras, both paced ≤1 call/sec and well under the 60/min PAT limit:

1. **Discogs-wide median annotation.** For scarce releases the wantlist pool can
   be tiny (often n=2), making "X% below median" hard to trust. Each deal is
   annotated with the Discogs-wide median asking price for its condition.
2. **Per-deal shipping hints.** The seller's shipping policy plus how many more
   of your wantlist items they have, so you can combine an order.

Both depend on the `/marketplace/price_suggestions/` and v3 shipping-policies
endpoints, which Discogs gates behind **seller settings** on the token's account
(currency + shipping policy, at https://www.discogs.com/settings/seller). The
account need not list anything for sale, but the seller profile must exist. If
you'd rather not, leave `DISCOGS_TOKEN` unset and the watcher runs without it.

## Email

Any SMTP server works. For a headless box, [ProtonMail
Bridge](https://proton.me/mail/bridge) exposes a local SMTP endpoint
(`SMTP_HOST=127.0.0.1`, `SMTP_PORT=1025`); generic providers use STARTTLS
(port 587) or SSL (port 465). The header banner image
(`records-header-email.jpg`) is embedded as a base64 data-URI so it renders even
in clients that block remote images.

## Layout

```
watcher.py        entry point + orchestration
shop_api.py       internal /sell_item client + cookie session auth
discogs_api.py    official PAT API (price suggestions, wantlist size)
evaluator.py      condition filter, effective-cost median, VAT, shipping region
shipping_policy.py  v3 shipping-policy fetch + landed-room estimate
notifier.py       EmailNotifier — HTML + plain-text digest, admin alerts
tests/            pytest, no network
```
