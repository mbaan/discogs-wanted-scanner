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
| `HEALTHCHECK_URL` | (unset) | Optional ping on each successful run (e.g. healthchecks.io). |

## Layout

```
watcher.py        entry point + orchestration
shop_api.py       /sell_item client + cookie session
evaluator.py      condition filter, in-group median, certainty
notifier.py       EmailNotifier (HTML + plain text digest)
tests/            pytest, no network
```
