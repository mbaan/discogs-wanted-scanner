# Discogs Wantlist Watcher — Project Context

## What this does

Hourly cronjob on a Raspberry Pi. Fetches new marketplace listings for the user's
Discogs wantlist, filters for VG+/VG+ condition or better (both vinyl AND sleeve),
evaluates price deals, and sends a single collected HTML email alert.

## Critical technical decision: no public marketplace API

The official Discogs API has **no endpoint to list marketplace listings by release**.
The `/marketplace/search?release_id=` endpoint was shut down in 2015.

**Solution discovered via XHR inspection of /shop/mywants/:**
Discogs' website uses an internal JSON API:
```
GET https://www.discogs.com/api/shop-page-api/sell_item
    ?sort=listedDate&sortOrder=descending&count=100&offset=0
```
This returns wantlist marketplace listings sorted newest-first as JSON.
Auth is via **browser session cookies** (`sid` + `session`), NOT the public PAT.
Session cookie is valid ~6 months and stored in `cookies.json` (gitignored).

The official Discogs API (PAT) is used only for:
- `GET /marketplace/price_suggestions/{release_id}` — median price per condition
- `GET /marketplace/stats/{release_id}` — num_for_sale + lowest_price

## Field names: TBD until first real run

`shop_api._parse_listing()` tries multiple candidate field names (camelCase and
snake_case) since exact names are unknown without calling the live API.
Run `DEBUG=1 python watcher.py` on first use to see raw response keys and verify.

## Architecture

```
watcher.py        — orchestration entrypoint; run this
shop_api.py       — internal shop-page-api client (session cookie auth)
discogs_api.py    — official API (PAT auth, in-run cached)
evaluator.py      — VG+/VG+ filter, deal eval, certainty signal, shipping region
notifier.py       — EmailNotifier (HTML digest) + Notifier base class
```

State is persisted in `state.json` (gitignored): last_run timestamp + seen listing IDs.
First run looks back 1 hour; no flood of old listings.

## User preferences / config

- **Country:** Netherlands — domestic/EU/international shipping is flagged
- **Min condition:** VG+ or better for both media AND sleeve (hard requirement)
- **Deal threshold:** 30% below condition median (configurable via `DEAL_THRESHOLD` in .env)
- **Seller rating:** 90+ minimum (configurable via `SELLER_RATING_MIN`)
- **Email:** ProtonMail Bridge (headless on Pi) — `smtp_host=127.0.0.1 smtp_port=1025`
- **Certainty signal:** HIGH/MEDIUM/LOW based on num_for_sale + discount depth

## Setup on a new machine

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
cp cookies.json.example cookies.json  # paste sid+session from browser DevTools
```

## Cronjob (Pi)

```
0 * * * * /home/pi/discogs_watcher/venv/bin/python /home/pi/discogs_watcher/watcher.py >> /home/pi/discogs_watcher/watcher.log 2>&1
```

## Future Android notifications

`notifier.py` has a `Notifier` abstract base class. `EmailNotifier` is the current
implementation. A `FCMNotifier` (Firebase) or Knock.app notifier can be added later.
Note: Discogs itself uses Knock.app for push notifications (visible in the XHR dump).
