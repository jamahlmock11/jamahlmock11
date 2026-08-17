"""SQLite journal for signals, fills, and scan snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from kalshi_bot.dashboard.trade_summary import aggregate_trade_stats, summarize_trade


DEFAULT_DB = Path("data/journal.db")


def infer_horizon_from_path(path: Path) -> str:
    name = path.name.lower()
    if "1h" in name:
        return "1h"
    if "15m" in name:
        return "15m"
    return "15m"


class CombinedTradeJournal:
    """Merge multiple bot journals (15m + 1h) for the dashboard."""

    def __init__(self, paths: list[Path] | None = None) -> None:
        default_paths = [
            Path("data/journal.db"),
            Path("data/journal_1h.db"),
        ]
        self.paths = [Path(p) for p in (paths or default_paths)]
        self.journals: list[tuple[str, TradeJournal]] = []
        for path in self.paths:
            if path.exists():
                self.journals.append((infer_horizon_from_path(path), TradeJournal(path)))

    def enriched_trades(self, limit: int = 500) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for horizon, journal in self.journals:
            merged.extend(journal.enriched_trades(limit=limit, horizon=horizon))
        merged.sort(key=lambda row: float(row.get("ts") or 0), reverse=True)
        return merged[:limit]

    def stats(self) -> dict[str, Any]:
        trades = self.enriched_trades(limit=5000)
        trade_stats = aggregate_trade_stats(trades)
        decisions = 0
        last_decision = None
        last_scan = None
        for _, journal in self.journals:
            stats = journal.stats()
            decisions += int(stats.get("decisions") or 0)
            if stats.get("last_decision"):
                candidate = stats["last_decision"]
                if not last_decision or candidate.get("ts", 0) > last_decision.get("ts", 0):
                    last_decision = candidate
            if stats.get("last_scan"):
                candidate = stats["last_scan"]
                if not last_scan or candidate.get("ts", 0) > last_scan.get("ts", 0):
                    last_scan = candidate
        trade_stats["decisions"] = decisions
        trade_stats["last_decision"] = last_decision
        trade_stats["last_scan"] = last_scan
        trade_stats["last_trade_ts"] = trades[0]["ts"] if trades else None
        trade_stats["journals"] = [str(j.path) for _, j in self.journals]
        return trade_stats

    def recent_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for horizon, journal in self.journals:
            for row in journal.recent_decisions(limit):
                item = dict(row)
                item["horizon"] = horizon
                merged.append(item)
        merged.sort(key=lambda row: float(row.get("ts") or 0), reverse=True)
        return merged[:limit]


class TradeJournal:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
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
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    ticker TEXT,
                    strike REAL,
                    brti_price REAL,
                    benchmark_source TEXT,
                    seconds_remaining REAL,
                    yes_bid REAL,
                    yes_ask REAL,
                    no_bid REAL,
                    no_ask REAL,
                    up_probability REAL,
                    down_probability REAL,
                    executable_price REAL,
                    edge REAL,
                    confidence REAL,
                    momentum REAL,
                    acceleration REAL,
                    volatility REAL,
                    regime TEXT,
                    trajectory TEXT,
                    signal_agreement REAL,
                    current_direction TEXT,
                    predicted_direction TEXT,
                    trade_direction TEXT,
                    action TEXT NOT NULL,
                    data_health TEXT,
                    reason TEXT,
                    gate_failures TEXT,
                    position TEXT,
                    dry_run INTEGER,
                    traded INTEGER DEFAULT 0,
                    outcome REAL,
                    pnl REAL,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker, ts DESC);
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
        edge: float | None,
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

    def log_decision(
        self,
        cycle: Any,
        *,
        dry_run: bool,
        traded: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Persist every forecast outcome, including failed-data NO TRADE rows."""
        decision_id = uuid.uuid4().hex[:12]
        market = getattr(cycle, "market", None)
        benchmark = getattr(cycle, "benchmark", None)
        features = getattr(cycle, "features", None)
        forecast = getattr(cycle, "forecast", None)
        decision = getattr(cycle, "decision", None)
        now = getattr(cycle, "timestamp", None)
        ts = now.timestamp() if hasattr(now, "timestamp") else time.time()

        def enum_value(value: Any) -> str | None:
            if value is None:
                return None
            return str(value.value if hasattr(value, "value") else value)

        failures = []
        for failure in getattr(decision, "gate_failures", ()) if decision else ():
            failures.append(
                {
                    "gate": failure.gate,
                    "reason": failure.reason,
                    "observed": failure.observed,
                    "required": failure.required,
                }
            )
        position = getattr(market, "current_position", None) if market else None
        position_payload = (
            {
                "side": enum_value(position.side),
                "quantity": position.quantity,
                "average_price": position.average_price,
            }
            if position
            else None
        )
        seconds_remaining = (
            max(0.0, (market.expiration.timestamp() - ts)) if market else None
        )
        values = (
            decision_id,
            ts,
            getattr(market, "ticker", None),
            getattr(market, "strike", None),
            getattr(benchmark, "price", None),
            getattr(benchmark, "source", None),
            seconds_remaining,
            getattr(market, "yes_bid", None),
            getattr(market, "yes_ask", None),
            getattr(market, "no_bid", None),
            getattr(market, "no_ask", None),
            getattr(forecast, "p_up", None),
            getattr(forecast, "p_down", None),
            getattr(decision, "executable_cost", None),
            getattr(decision, "edge", None),
            getattr(forecast, "confidence", None),
            getattr(features, "short_trend", None),
            getattr(features, "acceleration", None),
            getattr(features, "realized_vol", None),
            enum_value(getattr(cycle, "regime", None)),
            enum_value(getattr(features, "trajectory", None)),
            getattr(forecast, "signal_agreement", None),
            enum_value(getattr(decision, "current_direction", None)),
            enum_value(getattr(decision, "predicted_direction", None)),
            enum_value(getattr(decision, "trade_direction", None)),
            enum_value(getattr(decision, "action", None)) or "NO_TRADE",
            getattr(cycle, "data_health", "FAILED"),
            getattr(cycle, "reason", None) or getattr(decision, "reason", ""),
            json.dumps(failures, default=str),
            json.dumps(position_payload),
            1 if dry_run else 0,
            1 if traded else 0,
            None,
            None,
            json.dumps(payload or {}, default=str),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    id, ts, ticker, strike, brti_price, benchmark_source,
                    seconds_remaining, yes_bid, yes_ask, no_bid, no_ask,
                    up_probability, down_probability, executable_price, edge,
                    confidence, momentum, acceleration, volatility, regime,
                    trajectory, signal_agreement, current_direction,
                    predicted_direction, trade_direction, action, data_health,
                    reason, gate_failures, position, dry_run, traded, outcome,
                    pnl, payload
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
        return decision_id

    def label_latest_entry(
        self,
        ticker: str,
        *,
        outcome: float,
        pnl: float,
    ) -> int:
        """Label the most recent traded entry decision for a contract."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE decisions
                SET outcome = ?, pnl = ?
                WHERE id = (
                    SELECT id FROM decisions
                    WHERE ticker = ?
                      AND traded = 1
                      AND action IN ('BUY_UP', 'BUY_DOWN')
                      AND outcome IS NULL
                    ORDER BY ts DESC
                    LIMIT 1
                )
                """,
                (outcome, pnl, ticker),
            )
            return cursor.rowcount

    def label_decisions(
        self,
        ticker: str,
        *,
        up_won: bool,
        pnl: float | None = None,
    ) -> int:
        """Label resolved forecasts after settlement without rewriting inputs."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE decisions
                SET outcome = CASE
                    WHEN trade_direction = 'UP' THEN ?
                    WHEN trade_direction = 'DOWN' THEN ?
                    ELSE NULL
                END,
                pnl = CASE WHEN traded = 1 THEN ? ELSE pnl END
                WHERE ticker = ? AND outcome IS NULL
                """,
                (1.0 if up_won else 0.0, 0.0 if up_won else 1.0, pnl, ticker),
            )
            return cursor.rowcount

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

    def recent_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def enriched_trades(self, limit: int = 200, *, horizon: str | None = None) -> list[dict[str, Any]]:
        rows = self.recent_trades(limit)
        hz = horizon or infer_horizon_from_path(self.path)
        return [summarize_trade(dict(r), horizon=hz) for r in rows]

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
            decision_n = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
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
            last_decision = conn.execute(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT 1"
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
            "decisions": decision_n,
            "notional_usd": float(notional),
            "avg_edge": float(avg_edge),
            "last_trade_ts": last["ts"] if last else None,
            "last_scan": dict(last_scan) if last_scan else None,
            "last_decision": dict(last_decision) if last_decision else None,
            "by_strategy": [dict(r) for r in by_strategy],
            "by_tier": [dict(r) for r in by_tier],
        }