"""SQLite datastore for the watcher's run-to-run state.

One sqlite3.Connection wrapped in typed accessors (load_/save_ per state key) so
the watcher threads it through a run and the pure pipeline (core, evaluator) is
untouched. WAL + busy_timeout on open; DDL is idempotent (CREATE TABLE IF NOT
EXISTS) so both checkouts self-init from an empty state.db on first run — both
boxes cold-start clean, nothing is imported from any prior file. Fail-open: a
corrupt/locked DB logs and falls back to an in-memory throwaway Store so a run
never crashes."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_ALERTED_HARD_CAP = 50_000
_PENDING_HARD_CAP = 500
_BUSY_TIMEOUT_MS = 5000

_DDL = """
CREATE TABLE IF NOT EXISTS price_history (
    release_id INTEGER NOT NULL,
    condition  TEXT    NOT NULL,
    day        TEXT    NOT NULL,
    price      REAL    NOT NULL,
    currency   TEXT,
    PRIMARY KEY (release_id, condition, day)
);
CREATE TABLE IF NOT EXISTS sell_history (
    release_id TEXT PRIMARY KEY,
    fetched_at TEXT,
    stats      TEXT
);
CREATE TABLE IF NOT EXISTS shipping_policies (
    key        TEXT PRIMARY KEY,
    fetched_at TEXT,
    policy     TEXT
);
CREATE TABLE IF NOT EXISTS alerted (
    id         INTEGER PRIMARY KEY,
    last_price REAL
);
CREATE TABLE IF NOT EXISTS pushed (
    id         INTEGER PRIMARY KEY,
    last_price REAL
);
CREATE TABLE IF NOT EXISTS pending_deals (
    seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    deal TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """Typed wrapper over one sqlite3 connection holding the watcher's state.
    WHY a class: the connection + open/close lifecycle is shared state the
    watcher threads through one run; bundling keeps call sites terse."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path) -> "Store":
        """Open + ensure schema. Fail-open: on a corrupt or unopenable DB, log,
        quarantine it, and return an in-memory throwaway Store so the run
        continues (nothing persisted this run)."""
        try:
            store = cls._open_at(path)
        except (sqlite3.DatabaseError, OSError) as exc:
            logger.warning("Could not open %s (%s) — using in-memory store, nothing persisted this run", path, exc)
            cls._quarantine(path)
            return cls._open_at(":memory:")
        return store

    @classmethod
    def _open_at(cls, path) -> "Store":
        conn = sqlite3.connect(str(path))
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
        conn.commit()
        return cls(conn)

    @staticmethod
    def _quarantine(path) -> None:
        """Rename a corrupt DB aside (never delete) so next run retries clean."""
        p = Path(path)
        if str(path) == ":memory:" or not p.exists():
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            os.replace(p, p.with_name(f"{p.name}.corrupt-{ts}"))
        except OSError as exc:
            logger.warning("Could not quarantine corrupt DB %s: %s", p, exc)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── alerted: {listing_id: last_alert_price} ──
    def load_alerted(self) -> dict[int, float]:
        return {
            int(r[0]): float(r[1])
            for r in self.conn.execute("SELECT id, last_price FROM alerted")
            if r[1] is not None
        }

    def save_alerted(self, alerted: dict[int, float]) -> None:
        """Replace the alerted table; the _ALERTED_HARD_CAP prune (keep highest
        IDs — Discogs IDs grow monotonically) lives here so the rule is in one
        place."""
        pruned = self._prune_alerted(alerted)
        self.conn.execute("DELETE FROM alerted")
        self.conn.executemany(
            "INSERT OR REPLACE INTO alerted(id, last_price) VALUES (?, ?)",
            [(int(k), float(v)) for k, v in pruned.items() if v is not None],
        )
        self.conn.commit()

    @staticmethod
    def _prune_alerted(alerted: dict[int, float]) -> dict[int, float]:
        if len(alerted) <= _ALERTED_HARD_CAP:
            return alerted
        keep = sorted(alerted.items(), key=lambda kv: kv[0], reverse=True)[:_ALERTED_HARD_CAP]
        return dict(keep)

    # ── pushed: {listing_id: last_pushed_price} ──
    # A separate dedup set for the push fast-lane so the email and push channels
    # can't starve each other (see push design spec §5.3). Mirrors `alerted`
    # exactly — same SQL shape, same _ALERTED_HARD_CAP "keep highest IDs" prune
    # (reused via _prune_alerted so the cap rule stays in one place).
    def load_pushed(self) -> dict[int, float]:
        return {
            int(r[0]): float(r[1])
            for r in self.conn.execute("SELECT id, last_price FROM pushed")
            if r[1] is not None
        }

    def save_pushed(self, pushed: dict[int, float]) -> None:
        """Replace the pushed table; reuses the alerted prune (keep highest IDs —
        Discogs IDs grow monotonically) so a cold-start backlog can't bloat it."""
        pruned = self._prune_alerted(pushed)
        self.conn.execute("DELETE FROM pushed")
        self.conn.executemany(
            "INSERT OR REPLACE INTO pushed(id, last_price) VALUES (?, ?)",
            [(int(k), float(v)) for k, v in pruned.items() if v is not None],
        )
        self.conn.commit()

    # ── pending_deals: list of Deal.to_pending() dicts (FIFO) ──
    def load_pending(self) -> list[dict]:
        return [
            json.loads(r[0])
            for r in self.conn.execute("SELECT deal FROM pending_deals ORDER BY seq")
        ]

    def save_pending(self, pending: list[dict]) -> None:
        """Replace the queue. Applies _PENDING_HARD_CAP (keep newest = last items)
        before write; INSERT order = list order so the seq AUTOINCREMENT PK
        preserves FIFO."""
        capped = pending[-_PENDING_HARD_CAP:] if len(pending) > _PENDING_HARD_CAP else pending
        self.conn.execute("DELETE FROM pending_deals")
        self.conn.executemany(
            "INSERT INTO pending_deals(deal) VALUES (?)",
            [(json.dumps(d),) for d in capped],
        )
        self.conn.commit()

    # ── shipping_policies: {"uid:country": {"fetched_at","policy"}} ──
    def load_shipping_policies(self) -> dict:
        return {
            r[0]: {"fetched_at": r[1], "policy": json.loads(r[2]) if r[2] is not None else None}
            for r in self.conn.execute("SELECT key, fetched_at, policy FROM shipping_policies")
        }

    def save_shipping_policies(self, cache: dict) -> None:
        self.conn.execute("DELETE FROM shipping_policies")
        self.conn.executemany(
            "INSERT OR REPLACE INTO shipping_policies(key, fetched_at, policy) VALUES (?, ?, ?)",
            [
                (key, ent.get("fetched_at"), json.dumps(ent.get("policy")))
                for key, ent in (cache or {}).items()
            ],
        )
        self.conn.commit()

    # ── sell_history: {"release_id": {"fetched_at","stats"}} ──
    def load_sell_history(self) -> dict:
        return {
            r[0]: {"fetched_at": r[1], "stats": json.loads(r[2]) if r[2] is not None else None}
            for r in self.conn.execute("SELECT release_id, fetched_at, stats FROM sell_history")
        }

    def save_sell_history(self, cache: dict) -> None:
        self.conn.execute("DELETE FROM sell_history")
        self.conn.executemany(
            "INSERT OR REPLACE INTO sell_history(release_id, fetched_at, stats) VALUES (?, ?, ?)",
            [
                (str(rid), ent.get("fetched_at"), json.dumps(ent.get("stats")))
                for rid, ent in (cache or {}).items()
            ],
        )
        self.conn.commit()

    # ── price_history: {"rid:cond": [{"d","p","c"}, ...]} ──
    # The pure pipeline (core.record/prune/annotate) mutates this dict in place;
    # the store is only a serialization boundary, so core.py stays unchanged.
    def load_price_history(self) -> dict:
        out: dict = {}
        rows = self.conn.execute(
            "SELECT release_id, condition, day, price, currency "
            "FROM price_history ORDER BY release_id, condition, day"
        )
        for release_id, condition, day, price, currency in rows:
            out.setdefault(f"{release_id}:{condition}", []).append(
                {"d": day, "p": price, "c": currency}
            )
        return out

    def save_price_history(self, price_history: dict) -> None:
        """Normalize the dict into rows. Full replace so a core.prune_price_history
        drop (days/keys removed) is mirrored, not just upserted."""
        self.conn.execute("DELETE FROM price_history")
        rows = []
        for key, entries in (price_history or {}).items():
            rid_str, _, cond = key.partition(":")
            for e in entries:
                rows.append((int(rid_str), cond, e["d"], e["p"], e.get("c")))
        self.conn.executemany(
            "INSERT OR REPLACE INTO price_history"
            "(release_id, condition, day, price, currency) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    # ── meta scalars ──
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        """Set a scalar meta value; value=None deletes the row (mirrors the
        watcher's state.pop(key, None) for cookie_alert_sent_at)."""
        if value is None:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
            )
        self.conn.commit()

    # ── emails_today: {"date","count"} stored as JSON in meta ──
    def load_emails_today(self) -> dict:
        raw = self.get_meta("emails_today")
        if not raw:
            return {}
        try:
            val = json.loads(raw)
        except ValueError:
            return {}
        return val if isinstance(val, dict) else {}

    def save_emails_today(self, daily: dict) -> None:
        self.set_meta("emails_today", json.dumps(daily))
