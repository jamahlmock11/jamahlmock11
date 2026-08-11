"""ROMA pipeline: MarketDiscovery + PriceFeed + Orderbook → agents → risk."""

from __future__ import annotations

from kalshi_bot.agents.probability_model import evaluate_probability_model, market_yes_mid
from kalshi_bot.agents.sentiment import evaluate_sentiment
from kalshi_bot.agents.types import AgentLine, RomaPipelineReport
from kalshi_bot.config import AgentsConfig
from kalshi_bot.domain import DecisionAction
from kalshi_bot.strategies.forecasting import ForecastCycle


class RomaPipeline:
    def __init__(self, config: AgentsConfig) -> None:
        self.config = config

    def evaluate(self, cycle: ForecastCycle, *, risk_locked: bool = False) -> RomaPipelineReport | None:
        if not self.config.enabled:
            return None
        if cycle.market is None or cycle.forecast is None or cycle.features is None:
            return None

        sentiment = evaluate_sentiment(
            cycle.features,
            cycle.market,
            momentum_weight=self.config.momentum_weight,
            skew_weight=self.config.skew_weight,
        )
        yes_mid = market_yes_mid(cycle.market)
        probability = evaluate_probability_model(
            cycle.forecast,
            yes_mid=yes_mid,
            decision=cycle.decision,
            min_edge=self.config.min_edge,
        )

        lines: list[AgentLine] = [
            AgentLine(
                agent="SentimentAgent",
                headline=f"{sentiment.label} ({sentiment.score:+.2f})",
                detail=f"— {sentiment.explanation}",
            ),
            AgentLine(
                agent="ProbabilityModelAgent",
                headline=(
                    f"P(model)={probability.model_probability:.0%} vs "
                    f"P(market)={probability.market_probability:.0%}, "
                    f"edge {probability.edge * 100:+.0f}%"
                ),
                detail=f"→ {probability.action} — {probability.reason}",
            ),
        ]

        approved = probability.action in {
            DecisionAction.BUY_UP.value,
            DecisionAction.BUY_DOWN.value,
        }
        risk_detail = "APPROVED"
        if risk_locked:
            approved = False
            risk_detail = "REJECTED — risk controls locked"
        elif probability.edge + 1e-12 < self.config.min_edge:
            approved = False
            risk_detail = (
                f"REJECTED — edge {probability.edge:.1%} below minimum "
                f"({self.config.min_edge:.0%})"
            )
        elif cycle.decision is not None and cycle.decision.gate_failures:
            approved = False
            risk_detail = "REJECTED — entry blocked by safety gates"
        elif not approved:
            risk_detail = (
                f"REJECTED — edge {probability.edge:.1%} below minimum "
                f"({self.config.min_edge:.0%})"
            )

        lines.append(
            AgentLine(
                agent="RiskManager",
                headline=risk_detail,
                detail="",
            )
        )

        return RomaPipelineReport(
            lines=tuple(lines),
            min_edge=self.config.min_edge,
            model_probability=probability.model_probability,
            market_probability=probability.market_probability,
            edge=probability.edge,
            sentiment=sentiment.score,
            action=probability.action,
            approved=approved,
        )


def format_roma_report(report: RomaPipelineReport) -> str:
    rows = [
        "MarketDiscovery ──┐",
        "PriceFeed ─────────┼──► SentimentAgent (ROMA) ──► ProbabilityModel (ROMA) ──► RiskManager ──► Execution",
        "Orderbook ─────────┘",
        "",
    ]
    for line in report.lines:
        rows.append(f"{line.agent}: {line.headline}")
        if line.detail:
            rows.append(f"  {line.detail}")
    return "\n".join(rows)
