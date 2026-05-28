# discogs-wanted-scanner

Hourly cron that scans your personal Discogs wantlist marketplace listings,
finds the ones priced meaningfully below the typical landed price for their
release, and emails a digest.

## Setup

```sh
git clone <this repo> && cd discogs-wanted-scanner
uv sync --extra test                           # installs deps + pytest

cp .env.example .env       && $EDITOR .env     # SMTP creds, knobs
cp cookies.json.example cookies.json           # paste sid + session
```

**Get the cookies:** discogs.com in Brave/Chrome → DevTools (F12) →
Application → Cookies → `https://www.discogs.com` → copy the `sid` and
`session` values into `cookies.json`. Both last ~6 months; the watcher
emails you 14 days before expiry.

## Run

```sh
uv run python watcher.py                       # one real run; sends email
DRY_RUN=1 uv run python watcher.py             # no SMTP; writes /tmp/digest.{html,txt}
DEBUG=1 uv run python watcher.py               # verbose logging
uv run pytest                                  # unit tests, no network
```

## Cron

Edit your crontab with `crontab -e` and paste:

```cron
7 * * * * $HOME/.local/bin/uv run --directory $HOME/discogs-wanted-scanner python watcher.py >> $HOME/discogs-wanted-scanner/watcher.log 2>&1
```

If your `uv` lives elsewhere, run `which uv` and substitute that path.

## What counts as a deal

For each release on your wantlist with at least two listings in qualifying
condition (Mint / Near Mint / VG+, both media and sleeve), the watcher
computes the **median landed price** (item + shipping, in your account
currency) within each condition bucket and flags any listing at least
`DEAL_THRESHOLD` (default 25%) below that bucket's median.

The state file (`state.json`) records which listings you've been alerted on
and at what price; re-alerts fire when a price has since dropped a further
`PRICE_DROP_THRESHOLD` (default 5%).

## Tuning knobs (`.env`)

| Var | Default | Effect |
|---|---|---|
| `DEAL_THRESHOLD` | `0.25` | How far below the bucket median to qualify. Lower = more alerts. |
| `PRICE_DROP_THRESHOLD` | `0.05` | Re-alert when a seen listing's price drops this much further. |
| `MIN_CERTAINTY` | `MEDIUM` | `HIGH` / `MEDIUM` / `LOW` — drop alerts below this confidence band. |
| `DIGEST_MODE` | `hourly` | `daily` accumulates and emails once at `DIGEST_HOUR_UTC`. |
| `GROUP_BY_RELEASE` | `true` | Collapse multiple sellers per release into primary + siblings. |
| `MAX_SIBLINGS_PER_RELEASE` | `1` | Runner-up listings shown per release (primary + N). |
| `MAX_DEALS_PER_EMAIL` | `0` | `0` = no cap. |
| `MAX_EMAILS_PER_DAY` | `4` | Safety brake against runaway alerting. |
| `SELLER_RATING_MIN` | (unset) | Only show listings from sellers with ≥ this rating. |
| `MAX_PAGES_PER_RUN` | `30` | Pagination cap on `/sell_item`. |
| `DISCOGS_TOKEN` | (unset) | Optional PAT. When set, each deal is annotated with the Discogs-wide median asking price for its condition (one extra API call per deal-candidate release, paced 1/s, well under the 60/min PAT limit). Requires Discogs seller settings on the account — see below. |
| `HEALTHCHECK_URL` | (unset) | Optional ping on each successful run (e.g. healthchecks.io). |

## Optional: Discogs-wide price annotation

For scarce releases the wantlist marketplace pool can be tiny (often n=2),
which makes the "X% below median" claim hard to trust. With `DISCOGS_TOKEN`
set, each deal is annotated in the digest with the Discogs-wide median
asking price for that condition (one extra API call per deal-candidate
release, cached per run, paced 1/s, well under the 60/min PAT limit).

The endpoint Discogs exposes for this (`/marketplace/price_suggestions/`)
requires **seller settings** on the token's account — currency + shipping
policy, configured at https://www.discogs.com/settings/seller. The account
does not have to list anything for sale, but the seller profile must
exist. That's a personal-info step; if you'd rather not, leave
`DISCOGS_TOKEN` unset and the watcher runs exactly as before without it.

## Layout

```
watcher.py        entry point + orchestration
shop_api.py       /sell_item client + cookie session
evaluator.py      condition filter, in-group median, certainty
notifier.py       EmailNotifier (HTML + plain text digest)
tests/            pytest, no network
```
