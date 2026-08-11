"""Time-based features: expiration, session, historical win rates."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kalshi_bot.domain import MarketSnapshot, utc_datetime


@dataclass(frozen=True)
class TemporalSnapshot:
    """Calendar and time-to-expiry context."""

    minutes_until_expiration: float
    day_of_week: int  # 0=Monday
    hour_of_day: int  # UTC
    market_session: str  # ASIA, EUROPE, US, OVERLAP
    historical_win_rate: float | None
    historical_sample_count: int
    minute_bucket: str


SESSION_HOURS = {
    "ASIA": range(0, 8),
    "EUROPE": range(7, 16),
    "US": range(13, 22),
    "OVERLAP": range(13, 16),
}


def classify_session(hour_utc: int) -> str:
    if hour_utc in SESSION_HOURS["OVERLAP"]:
        return "OVERLAP"
    if hour_utc in SESSION_HOURS["ASIA"]:
        return "ASIA"
    if hour_utc in SESSION_HOURS["EUROPE"]:
        return "EUROPE"
    if hour_utc in SESSION_HOURS["US"]:
        return "US"
    return "OFF_HOURS"


def minute_bucket(seconds_remaining: float) -> str:
    minutes = seconds_remaining / 60.0
    if minutes <= 1:
        return "0-1m"
    if minutes <= 3:
        return "1-3m"
    if minutes <= 5:
        return "3-5m"
    if minutes <= 7:
        return "5-7m"
    if minutes <= 10:
        return "7-10m"
    return "10-15m"


class TemporalWinRateStore:
    """Load historical win rates by minute bucket from the decision journal."""

    def __init__(self, journal_path: Path | str = Path("data/journal.db")) -> None:
        self.journal_path = Path(journal_path)

    def win_rate_for_bucket(self, bucket: str) -> tuple[float | None, int]:
        if not self.journal_path.exists():
            return None, 0
        try:
            with sqlite3.connect(self.journal_path) as conn:
                rows = conn.execute(
                    """
                    SELECT outcome, seconds_remaining
                    FROM decisions
                    WHERE outcome IS NOT NULL AND traded = 1
                    """,
                ).fetchall()
        except sqlite3.Error:
            return None, 0

        wins = 0
        total = 0
        for outcome, seconds in rows:
            if seconds is None:
                continue
            if minute_bucket(float(seconds)) != bucket:
                continue
            total += 1
            if float(outcome) >= 0.5:
                wins += 1
        if total < 5:
            return None, total
        return wins / total, total


def compute_temporal(
    market: MarketSnapshot,
    now: datetime | None = None,
    *,
    win_rate_store: TemporalWinRateStore | None = None,
) -> TemporalSnapshot:
    observed = utc_datetime(now or datetime.now(timezone.utc))
    seconds_remaining = max(0.0, (market.expiration - observed).total_seconds())
    minutes = seconds_remaining / 60.0
    bucket = minute_bucket(seconds_remaining)
    store = win_rate_store or TemporalWinRateStore()
    win_rate, sample_count = store.win_rate_for_bucket(bucket)

    return TemporalSnapshot(
        minutes_until_expiration=minutes,
        day_of_week=observed.weekday(),
        hour_of_day=observed.hour,
        market_session=classify_session(observed.hour),
        historical_win_rate=win_rate,
        historical_sample_count=sample_count,
        minute_bucket=bucket,
    )
