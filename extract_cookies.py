#!/usr/bin/env python3
"""Interactively turn a browser cookie string into cookies.json.

Run it, paste the cookie string from DevTools (Network → any discogs.com
request → Request Headers → Cookie) or the console (`document.cookie`), and it
writes cookies.json next to this script:

    python extract_cookies.py

It keeps only the cookies that matter: sid + session authenticate; cf_clearance
(if present) helps past Cloudflare. An existing cookies.json is replaced only
after you confirm (or backed up to cookies.json.bak when run non-interactively).
"""
import json
import sys
from pathlib import Path

_COOKIES_FILE = Path(__file__).parent / "cookies.json"

# __cf_bm is intentionally omitted — shop_api._load_cookies drops _-prefixed
# keys, so it would never reach the request anyway.
_WANTED = ("sid", "session", "cf_clearance")


def parse_cookie_string(raw: str) -> dict:
    """Browser 'k=v; k=v' string -> dict. Split each pair on the FIRST '=' only:
    the session token's value itself contains '=', '&' and '%'."""
    jar = {}
    for pair in raw.strip().strip(";").split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        jar[name.strip()] = value.strip()
    return jar


def extract(raw: str) -> dict:
    """Pull the cookies we care about, in a stable order, from a browser string."""
    jar = parse_cookie_string(raw)
    return {k: jar[k] for k in _WANTED if k in jar}


def _read_cookie_string() -> str:
    if sys.stdin.isatty():
        print("Paste your Discogs cookie string, then press Enter:", file=sys.stderr)
        return input("> ")
    return sys.stdin.read()


def _confirm_overwrite() -> bool:
    if not _COOKIES_FILE.exists():
        return True
    if sys.stdin.isatty():
        ans = input(f"{_COOKIES_FILE.name} already exists — overwrite? [y/N] ").strip().lower()
        return ans in ("y", "yes")
    # Non-interactive (piped): don't silently destroy — keep a backup, then proceed.
    backup = _COOKIES_FILE.with_name(_COOKIES_FILE.name + ".bak")
    backup.write_text(_COOKIES_FILE.read_text())
    print(f"backed up existing {_COOKIES_FILE.name} -> {backup.name}", file=sys.stderr)
    return True


def main() -> None:
    cookies = extract(_read_cookie_string())

    missing = [k for k in ("sid", "session") if k not in cookies]
    if missing:
        sys.exit(f"Aborting — required cookie(s) not found in the string: {', '.join(missing)}")

    # Best-effort expiry readout (reuses the canonical parser) — never blocks output.
    try:
        import shop_api
        exp = shop_api.session_expires_at(cookies)
        if exp is not None:
            print(f"session expires {exp.date()} ({exp.isoformat()})", file=sys.stderr)
    except Exception:
        pass

    if not _confirm_overwrite():
        sys.exit("Left cookies.json unchanged.")

    payload = {
        "_comment": "sid + session authenticate to Discogs; cf_clearance helps past "
                    "Cloudflare. Written by extract_cookies.py.",
        **cookies,
    }
    _COOKIES_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {_COOKIES_FILE} ({', '.join(cookies)}).", file=sys.stderr)


if __name__ == "__main__":
    main()
