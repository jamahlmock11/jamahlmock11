"""Record hourly terminal predictions and causal calibration buckets."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kalshi_bot.calibration.calibration import ProbabilityCalibrator, ReliabilityBin
from kalshi_bot.domain import utc_datetime

CALIBRATION_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.50, 0.55),
    (0.55, 0.60),
    (0.60, 0.65),
    (0.65, 0.70),
    (0.70, 0.75),
    (0.75, 0.80),
    (0.80, 0.85),
    (0.85, 1.01),
)


@dataclass(frozen=True)
class CalibrationBucketSummary:
    lower: float
    upper: float
    count: int
    mean_prediction: float
    empirical_frequency: float
    calibration_gap: float


class PredictionStore:
    """Persist every terminal forecast and resolve outcomes after expiration."""

    def __init__(self, path: str | Path = "data/predictions_1h.db") -> None:
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
                CREATE TABLE IF NOT EXISTS terminal_predictions (
                    id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    ticker TEXT NOT NULL,
                    strike REAL NOT NULL,
                    expiration_ts REAL NOT NULL,
                    brti_price REAL,
                    seconds_remaining REAL,
                    predicted_p_yes REAL,
                    calibrated_p_yes REAL,
                    market_yes_ask REAL,
                    market_no_ask REAL,
                    yes_net_edge REAL,
                    no_net_edge REAL,
                    volatility REAL,
                    regime TEXT,
                    confidence REAL,
                    signal_agreement REAL,
                    action TEXT,
                    outcome REAL,
                    outcome_brti REAL,
                    resolved_ts REAL,
                    payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_terminal_predictions_ticker
                    ON terminal_predictions(ticker, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_terminal_predictions_unresolved
                    ON terminal_predictions(outcome, expiration_ts);
                """
            )

    def record(
        self,
        *,
        timestamp: datetime,
        ticker: str,
        strike: float,
        expiration: datetime,
        brti_price: float,
        seconds_remaining: float,
        predicted_p_yes: float,
        calibrated_p_yes: float,
        market_yes_ask: float | None,
        market_no_ask: float | None,
        yes_net_edge: float | None,
        no_net_edge: float | None,
        volatility: float | None,
        regime: str | None,
        confidence: float | None,
        signal_agreement: float | None,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        prediction_id = uuid.uuid4().hex[:12]
        ts = timestamp.timestamp()
        expiration_ts = expiration.timestamp()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO terminal_predictions (
                    id, ts, ticker, strike, expiration_ts, brti_price, seconds_remaining,
                    predicted_p_yes, calibrated_p_yes, market_yes_ask, market_no_ask,
                    yes_net_edge, no_net_edge, volatility, regime, confidence,
                    signal_agreement, action, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction_id,
                    ts,
                    ticker,
                    strike,
                    expiration_ts,
                    brti_price,
                    seconds_remaining,
                    predicted_p_yes,
                    calibrated_p_yes,
                    market_yes_ask,
                    market_no_ask,
                    yes_net_edge,
                    no_net_edge,
                    volatility,
                    regime,
                    confidence,
                    signal_agreement,
                    action,
                    json.dumps(payload or {}, default=str),
                ),
            )
        return prediction_id

    def resolve_expired(
        self,
        *,
        now: datetime,
        settlement_brti: float,
        ticker: str | None = None,
        strike: float | None = None,
    ) -> int:
        """Resolve unresolved predictions once expiration has passed."""
        now_ts = utc_datetime(now).timestamp()
        resolved = 0
        with self._lock, self._connect() as conn:
            query = """
                SELECT id, strike FROM terminal_predictions
                WHERE outcome IS NULL AND expiration_ts <= ?
            """
            params: list[Any] = [now_ts]
            if ticker is not None:
                query += " AND ticker = ?"
                params.append(ticker)
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                row_strike = float(row["strike"])
                outcome = 1.0 if settlement_brti > row_strike else 0.0
                conn.execute(
                    """
                    UPDATE terminal_predictions
                    SET outcome = ?, outcome_brti = ?, resolved_ts = ?
                    WHERE id = ?
                    """,
                    (outcome, settlement_brti, now_ts, row["id"]),
                )
                resolved += 1
        return resolved

    def unresolved(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM terminal_predictions
                WHERE outcome IS NULL
                ORDER BY expiration_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def build_calibrator(self, *, cutoff: datetime) -> ProbabilityCalibrator:
        calibrator = ProbabilityCalibrator(n_bins=10)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT calibrated_p_yes, outcome, ts, resolved_ts
                FROM terminal_predictions
                WHERE outcome IS NOT NULL
                """,
            ).fetchall()
        for row in rows:
            calibrator.add_sample(
                prediction=float(row["calibrated_p_yes"]),
                outcome=float(row["outcome"]),
                prediction_timestamp=datetime.fromtimestamp(
                    float(row["ts"]), tz=timezone.utc
                ),
                outcome_timestamp=datetime.fromtimestamp(
                    float(row["resolved_ts"] or row["ts"]), tz=timezone.utc
                ),
            )
        if calibrator.samples:
            calibrator.fit(cutoff)
        return calibrator

    def bucket_summaries(self, *, min_samples: int = 25) -> tuple[CalibrationBucketSummary, ...]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT calibrated_p_yes, outcome
                FROM terminal_predictions
                WHERE outcome IS NOT NULL
                """,
            ).fetchall()
        summaries: list[CalibrationBucketSummary] = []
        for lower, upper in CALIBRATION_BUCKETS:
            bucket = [
                row
                for row in rows
                if lower <= float(row["calibrated_p_yes"]) < upper
            ]
            count = len(bucket)
            if count == 0:
                summaries.append(
                    CalibrationBucketSummary(
                        lower=lower,
                        upper=upper,
                        count=0,
                        mean_prediction=0.0,
                        empirical_frequency=0.0,
                        calibration_gap=0.0,
                    )
                )
                continue
            mean_pred = sum(float(r["calibrated_p_yes"]) for r in bucket) / count
            empirical = sum(float(r["outcome"]) for r in bucket) / count
            summaries.append(
                CalibrationBucketSummary(
                    lower=lower,
                    upper=upper,
                    count=count,
                    mean_prediction=mean_pred,
                    empirical_frequency=empirical,
                    calibration_gap=mean_pred - empirical,
                )
            )
        return tuple(summaries)

    def calibration_pass(self, *, min_samples: int = 25, max_gap: float = 0.12) -> bool:
        summaries = self.bucket_summaries(min_samples=min_samples)
        checked = [item for item in summaries if item.count >= min_samples]
        if not checked:
            return True
        return all(abs(item.calibration_gap) <= max_gap for item in checked)

    def reliability_bins(self, calibrator: ProbabilityCalibrator) -> tuple[ReliabilityBin, ...]:
        if calibrator.fit_cutoff is None:
            return ()
        return calibrator.reliability_bins()
