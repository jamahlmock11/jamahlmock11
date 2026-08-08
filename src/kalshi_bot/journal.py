"""SQLite journal for signals, fills, and scan snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("data/journal.db")


class TradeJournal:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    spot REAL,
                    ibit REAL,
                    iv_atm REAL,
                    markets_scanned INTEGER,
                    signal_count INTEGER,
                    mode TEXT
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    scan_id TEXT,
                    ts REAL NOT NULL,
                    ticker TEXT,
                    series TEXT,
                    side TEXT,
                    confidence TEXT,
                    edge_pp REAL,
                    kalshi_prob REAL,
                    options_prob REAL,
                    iv REAL,
                    book_usd REAL,
                    strike REAL,
                    spot REAL,
                    reason TEXT,
                    traded INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    strategy TEXT,
                    ticker TEXT,
                    side TEXT,
                    count REAL,
                    price REAL,
                    notional REAL,
                    edge REAL,
                    confidence TEXT,
                    dry_run INTEGER,
                    ok INTEGER,
                    detail TEXT,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);
                """
            )

    def log_scan(
        self,
        *,
        spot: float,
        ibit: float,
        iv_atm: float | None,
        markets_scanned: int,
        signal_count: int,
        mode: str,
    ) -> str:
        scan_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scans (id, ts, spot, ibit, iv_atm, markets_scanned, signal_count, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    time.time(),
                    spot,
                    ibit,
                    iv_atm,
                    markets_scanned,
                    signal_count,
                    mode,
                ),
            )
        return scan_id

    def log_signal(self, scan_id: str, signal: Any, traded: bool = False) -> str:
        sig_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO signals (
                    id, scan_id, ts, ticker, series, side, confidence, edge_pp,
                    kalshi_prob, options_prob, iv, book_usd, strike, spot, reason, traded
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig_id,
                    scan_id,
                    time.time(),
                    signal.ticker,
                    signal.series,
                    signal.side.value if hasattr(signal.side, "value") else str(signal.side),
                    signal.confidence.value if hasattr(signal.confidence, "value") else str(signal.confidence),
                    float(signal.edge_pp),
                    float(signal.kalshi_prob),
                    float(signal.options_prob),
                    float(signal.iv),
                    float(signal.book_usd),
                    float(signal.strike),
                    float(signal.spot),
                    signal.reason,
                    1 if traded else 0,
                ),
            )
        return sig_id

    def log_trade(
        self,
        *,
        strategy: str,
        ticker: str,
        side: str,
        count: float,
        price: float,
        notional: float,
        edge: float,
        confidence: str,
        dry_run: bool,
        ok: bool,
        detail: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        trade_id = uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades (
                    id, ts, strategy, ticker, side, count, price, notional,
                    edge, confidence, dry_run, ok, detail, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    time.time(),
                    strategy,
                    ticker,
                    side,
                    count,
                    price,
                    notional,
                    edge,
                    confidence,
                    1 if dry_run else 0,
                    1 if ok else 0,
                    detail,
                    json.dumps(payload or {}),
                ),
            )
        return trade_id

    def recent_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_scans(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scans ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            trade_n = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
            live_n = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE dry_run=0 AND ok=1"
            ).fetchone()["n"]
            dry_n = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE dry_run=1 AND ok=1"
            ).fetchone()["n"]
            fail_n = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE ok=0"
            ).fetchone()["n"]
            signal_n = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
            notional = conn.execute(
                "SELECT COALESCE(SUM(notional),0) AS s FROM trades WHERE ok=1"
            ).fetchone()["s"]
            avg_edge = conn.execute(
                "SELECT COALESCE(AVG(edge),0) AS e FROM trades WHERE ok=1"
            ).fetchone()["e"]
            last = conn.execute(
                "SELECT ts FROM trades ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            last_scan = conn.execute(
                "SELECT * FROM scans ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            by_strategy = conn.execute(
                """
                SELECT strategy, COUNT(*) AS n, COALESCE(SUM(notional),0) AS notional
                FROM trades WHERE ok=1 GROUP BY strategy
                """
            ).fetchall()
            by_tier = conn.execute(
                """
                SELECT confidence, COUNT(*) AS n
                FROM trades WHERE ok=1 GROUP BY confidence
                """
            ).fetchall()
        return {
            "trades": trade_n,
            "live_trades": live_n,
            "dry_trades": dry_n,
            "failed_trades": fail_n,
            "signals": signal_n,
            "notional_usd": float(notional),
            "avg_edge": float(avg_edge),
            "last_trade_ts": last["ts"] if last else None,
            "last_scan": dict(last_scan) if last_scan else None,
            "by_strategy": [dict(r) for r in by_strategy],
            "by_tier": [dict(r) for r in by_tier],
        }