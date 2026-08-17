"""Continuous learning: record trades and outcomes for retraining."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TradeRecord:
    """One trade lifecycle for continuous learning."""

    ticker: str
    entry_ts: float
    features: dict[str, Any]
    prediction: float
    confidence: float
    edge: float
    action: str
    reason: str
    outcome: float | None = None
    pnl: float | None = None
    exit_ts: float | None = None


@dataclass
class TradeRecorder:
    """Save entry features and outcomes after every trade."""

    db_path: Path = field(default_factory=lambda: Path("data/learning.db"))
    pending: dict[str, TradeRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    entry_ts REAL NOT NULL,
                    exit_ts REAL,
                    features TEXT NOT NULL,
                    prediction REAL,
                    confidence REAL,
                    edge REAL,
                    action TEXT,
                    reason TEXT,
                    outcome REAL,
                    pnl REAL
                );
                CREATE INDEX IF NOT EXISTS idx_trade_records_ticker
                    ON trade_records(ticker, entry_ts DESC);
                """
            )

    def record_entry(
        self,
        *,
        ticker: str,
        features: dict[str, Any],
        prediction: float,
        confidence: float,
        edge: float,
        action: str,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc).timestamp()
        record = TradeRecord(
            ticker=ticker,
            entry_ts=now,
            features=features,
            prediction=prediction,
            confidence=confidence,
            edge=edge,
            action=action,
            reason=reason,
        )
        self.pending[ticker] = record
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trade_records (
                    ticker, entry_ts, features, prediction, confidence,
                    edge, action, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticker,
                    now,
                    json.dumps(features, default=str),
                    prediction,
                    confidence,
                    edge,
                    action,
                    reason,
                ),
            )

    def record_outcome(
        self,
        ticker: str,
        *,
        outcome: float,
        pnl: float,
    ) -> None:
        now = datetime.now(timezone.utc).timestamp()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE trade_records
                SET outcome = ?, pnl = ?, exit_ts = ?
                WHERE id = (
                    SELECT id FROM trade_records
                    WHERE ticker = ? AND outcome IS NULL
                    ORDER BY entry_ts DESC
                    LIMIT 1
                )
                """,
                (outcome, pnl, now, ticker),
            )
        pending = self.pending.pop(ticker, None)
        if pending is not None:
            pending.outcome = outcome
            pending.pnl = pnl
            pending.exit_ts = now

    def training_rows(self, limit: int = 5000) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM trade_records
                WHERE outcome IS NOT NULL
                ORDER BY entry_ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0]
            resolved = conn.execute(
                "SELECT COUNT(*) FROM trade_records WHERE outcome IS NOT NULL"
            ).fetchone()[0]
            avg_pnl = conn.execute(
                "SELECT AVG(pnl) FROM trade_records WHERE pnl IS NOT NULL"
            ).fetchone()[0]
        return {
            "total_records": total,
            "resolved_records": resolved,
            "average_pnl": float(avg_pnl or 0.0),
            "pending": len(self.pending),
        }
