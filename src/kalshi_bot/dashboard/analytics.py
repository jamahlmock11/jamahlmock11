"""Post-trade analytics for the Edge Desk dashboard."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_bot.features.temporal import classify_session, minute_bucket
from kalshi_bot.learning.trade_recorder import TradeRecorder


@dataclass(frozen=True)
class AnalyticsReport:
    """Aggregated post-trade metrics."""

    win_rate_by_market_type: dict[str, float]
    win_rate_by_time_remaining: dict[str, float]
    win_rate_by_hour: dict[str, float]
    win_rate_by_session: dict[str, float]
    confidence_vs_outcome: list[dict[str, float]]
    profit_by_strategy: dict[str, float]
    profit_by_hour: dict[str, float]
    feature_importance: dict[str, float]
    largest_loss_causes: list[dict[str, Any]]
    learning_summary: dict[str, Any]
    total_trades: int
    total_pnl: float


def _load_decisions(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    """
                    SELECT * FROM decisions
                    WHERE traded = 1
                    ORDER BY ts DESC
                    LIMIT 5000
                    """
                ):
                    rows.append(dict(row))
        except sqlite3.Error:
            continue
    return rows


def _parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def build_analytics(
    journal_paths: list[Path] | None = None,
) -> AnalyticsReport:
    paths = journal_paths or [Path("data/journal.db"), Path("data/journal_1h.db")]
    decisions = _load_decisions(paths)

    by_type: dict[str, list[bool]] = defaultdict(list)
    by_time: dict[str, list[bool]] = defaultdict(list)
    by_hour: dict[str, list[bool]] = defaultdict(list)
    by_session: dict[str, list[bool]] = defaultdict(list)
    by_strategy: dict[str, float] = defaultdict(float)
    by_hour_pnl: dict[str, float] = defaultdict(float)
    confidence_buckets: dict[str, list[bool]] = defaultdict(list)
    loss_causes: dict[str, int] = defaultdict(int)
    feature_scores: dict[str, float] = defaultdict(float)
    total_pnl = 0.0

    for row in decisions:
        outcome = row.get("outcome")
        if outcome is None:
            continue
        won = float(outcome) >= 0.5
        pnl = float(row.get("pnl") or 0.0)
        total_pnl += pnl

        horizon = _parse_payload(row.get("payload")).get("horizon", "15m")
        by_type[horizon].append(won)

        seconds = row.get("seconds_remaining")
        if seconds is not None:
            bucket = minute_bucket(float(seconds))
            by_time[bucket].append(won)

        ts = row.get("ts")
        if ts:
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            hour_key = f"{dt.hour:02d}:00"
            by_hour[hour_key].append(won)
            by_hour_pnl[hour_key] += pnl
            by_session[classify_session(dt.hour)].append(won)

        strategy = _parse_payload(row.get("payload")).get("strategy", "forecast")
        by_strategy[strategy] += pnl

        conf = row.get("confidence")
        if conf is not None:
            bucket = f"{int(float(conf) * 10) * 10}%"
            confidence_buckets[bucket].append(won)

        if not won and pnl < 0:
            reason = row.get("reason") or "unknown"
            # Extract first gate failure as cause
            try:
                failures = json.loads(row.get("gate_failures") or "[]")
                if failures:
                    reason = failures[0].get("gate", reason)
            except json.JSONDecodeError:
                pass
            loss_causes[reason[:80]] += 1

        payload = _parse_payload(row.get("payload"))
        tq = payload.get("trade_quality", {})
        if tq:
            for key in ("liquidity_score", "trade_quality_score", "do_not_trade_score"):
                val = tq.get(key)
                if val is not None:
                    feature_scores[key] += float(val) * (1 if won else -0.5)

    def win_rate(groups: dict[str, list[bool]]) -> dict[str, float]:
        return {
            k: round(sum(v) / len(v), 3) if v else 0.0
            for k, v in sorted(groups.items())
        }

    confidence_vs_outcome = [
        {
            "bucket": bucket,
            "win_rate": round(sum(wins) / len(wins), 3),
            "count": len(wins),
        }
        for bucket, wins in sorted(confidence_buckets.items())
        if wins
    ]

    largest_losses = sorted(
        [{"cause": k, "count": v} for k, v in loss_causes.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    # Normalize feature importance
    max_score = max(abs(v) for v in feature_scores.values()) if feature_scores else 1.0
    feature_importance = {
        k: round(abs(v) / max_score, 3)
        for k, v in sorted(feature_scores.items(), key=lambda x: -abs(x[1]))
    }

    learning = TradeRecorder().summary()

    return AnalyticsReport(
        win_rate_by_market_type=win_rate(by_type),
        win_rate_by_time_remaining=win_rate(by_time),
        win_rate_by_hour=win_rate(by_hour),
        win_rate_by_session=win_rate(by_session),
        confidence_vs_outcome=confidence_vs_outcome,
        profit_by_strategy={k: round(v, 2) for k, v in by_strategy.items()},
        profit_by_hour={k: round(v, 2) for k, v in sorted(by_hour_pnl.items())},
        feature_importance=feature_importance,
        largest_loss_causes=largest_losses,
        learning_summary=learning,
        total_trades=len([r for r in decisions if r.get("outcome") is not None]),
        total_pnl=round(total_pnl, 2),
    )


def analytics_to_dict(report: AnalyticsReport) -> dict[str, Any]:
    return {
        "win_rate_by_market_type": report.win_rate_by_market_type,
        "win_rate_by_time_remaining": report.win_rate_by_time_remaining,
        "win_rate_by_hour": report.win_rate_by_hour,
        "win_rate_by_session": report.win_rate_by_session,
        "confidence_vs_outcome": report.confidence_vs_outcome,
        "profit_by_strategy": report.profit_by_strategy,
        "profit_by_hour": report.profit_by_hour,
        "feature_importance": report.feature_importance,
        "largest_loss_causes": report.largest_loss_causes,
        "learning_summary": report.learning_summary,
        "total_trades": report.total_trades,
        "total_pnl": report.total_pnl,
    }
