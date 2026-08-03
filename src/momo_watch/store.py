"""SQLite 儲存層。

這個規模（幾十個商品、每分鐘一輪）不需要 ORM，也不需要非同步 DB driver ——
每次寫入是次毫秒等級，直接在 event loop 裡呼叫不會有感。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .models import Availability, Event, Snapshot, Watch

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    code            TEXT PRIMARY KEY,
    label           TEXT,
    price_threshold INTEGER,
    active          INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS states (
    code         TEXT PRIMARY KEY,
    name         TEXT,
    price        INTEGER,
    availability TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL,
    price      INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_code_time ON events(code, created_at DESC);

-- Phase 2：下單流程的嘗試紀錄。
-- 持久化的理由是 max_attempts 必須跨重啟生效 —— 程式重開一次就重新計數的話，
-- 「同一個商品只搶一次」這個閘門形同虛設。
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    reached    TEXT,
    detail     TEXT,
    dry_run    INTEGER NOT NULL,
    total_ms   REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_code ON attempts(code, created_at DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- watches -------------------------------------------------------

    def add_watch(
        self, code: str, label: str | None = None, price_threshold: int | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO watches (code, label, price_threshold, active, added_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(code) DO UPDATE SET
                       label           = COALESCE(excluded.label, watches.label),
                       price_threshold = COALESCE(excluded.price_threshold,
                                                  watches.price_threshold),
                       active          = 1""",
                (code, label, price_threshold, _now()),
            )
            self._conn.commit()

    def remove_watch(self, code: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM watches WHERE code = ?", (code,))
            self._conn.execute("DELETE FROM states WHERE code = ?", (code,))
            self._conn.commit()
            return cur.rowcount > 0

    def list_watches(self, *, active_only: bool = True) -> list[Watch]:
        sql = "SELECT code, label, price_threshold FROM watches"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY added_at"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            Watch(code=r["code"], label=r["label"], price_threshold=r["price_threshold"])
            for r in rows
        ]

    # --- states --------------------------------------------------------

    def get_state(self, code: str) -> Snapshot | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM states WHERE code = ?", (code,)).fetchone()
        if row is None:
            return None
        return Snapshot(
            code=row["code"],
            name=row["name"],
            price=row["price"],
            availability=Availability(row["availability"]),
            fetched_at=datetime.fromisoformat(row["checked_at"]),
        )

    def save_state(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO states (code, name, price, availability, checked_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET
                       name         = excluded.name,
                       price        = excluded.price,
                       availability = excluded.availability,
                       checked_at   = excluded.checked_at""",
                (
                    snapshot.code,
                    snapshot.name,
                    snapshot.price,
                    snapshot.availability.value,
                    snapshot.fetched_at.isoformat(),
                ),
            )
            self._conn.commit()

    # --- events --------------------------------------------------------

    def record_event(self, event: Event) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (code, kind, detail, price, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    event.snapshot.code,
                    event.kind.value,
                    event.detail,
                    event.snapshot.price,
                    _now(),
                ),
            )
            self._conn.commit()

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    # --- attempts（Phase 2）--------------------------------------------

    def record_attempt(
        self,
        code: str,
        outcome: str,
        *,
        reached: str | None = None,
        detail: str | None = None,
        dry_run: bool = True,
        total_ms: float | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO attempts
                       (code, outcome, reached, detail, dry_run, total_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (code, outcome, reached, detail, int(dry_run), total_ms, _now()),
            )
            self._conn.commit()

    def count_attempts(self, code: str, *, include_dry_run: bool = False) -> int:
        """算這個商品試過幾次。

        預設不計演練 —— 演練不會真的下單，不該吃掉 max_attempts 的額度。
        """
        sql = "SELECT COUNT(*) AS n FROM attempts WHERE code = ?"
        if not include_dry_run:
            sql += " AND dry_run = 0"
        with self._lock:
            return self._conn.execute(sql, (code,)).fetchone()["n"]

    def recent_attempts(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM attempts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
