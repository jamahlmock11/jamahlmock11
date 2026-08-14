"""Trade quality scoring and Do-Not-Trade filter."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, MarketSnapshot, ProbabilityEstimate, Regime, TradeTier
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.intelligence.model_agreement import ModelAgreementAssessment
from kalshi_bot.strategies.forecast_setup import ForecastSetupAssessment


@dataclass(frozen=True)
class TradeQualityAssessment:
    """Unified trade-worthiness score — the ability to skip mediocre setups."""

    trade_quality_score: float  # 0–100
    do_not_trade_score: float  # 0–100, higher = worse
    confidence_pct: float
    expected_edge_pct: float
    liquidity_label: str
    historical_match_count: int
    recommendation: str  # EXECUTE or SKIP
    trade_tier: TradeTier
    size_multiplier: float
    reasons: tuple[str, ...]

    @property
    def should_execute(self) -> bool:
        return self.recommendation == "EXECUTE"


def _liquidity_label(score: float) -> str:
    if score >= 75:
        return "Excellent"
    if score >= 55:
        return "Good"
    if score >= 35:
        return "Fair"
    return "Poor"


def assess_trade_quality(
    *,
    forecast: ProbabilityEstimate,
    features: FeatureSnapshot,
    market: MarketSnapshot,
    enriched: EnrichedFeatures,
    model_agreement: ModelAgreementAssessment,
    edge: float | None,
    regime: Regime,
    min_quality_score: float = 65.0,
    max_dnt_score: float = 40.0,
    setup_assessment: ForecastSetupAssessment | None = None,
    setup_score_weight: float = 0.30,
) -> TradeQualityAssessment:
    """Score whether the market is worth trading at all."""
    micro = enriched.microstructure
    price = enriched.price_action
    reasons: list[str] = []

    # Trade quality components (0–100)
    edge_score = min((edge or 0.0) / 0.30, 1.0) * 25.0
    confidence_score = forecast.confidence * 20.0
    agreement_score = model_agreement.agreement * 15.0
    liquidity_score = micro.liquidity_score * 0.15
    data_score = features.data_completeness * 10.0

    trade_quality = (
        edge_score
        + confidence_score
        + agreement_score
        + liquidity_score
        + data_score
    )
    if setup_assessment is not None and setup_score_weight > 0:
        trade_quality += setup_assessment.score * setup_score_weight
        if not setup_assessment.in_sweet_spot:
            reasons.append("outside entry sweet-spot window")

    # Do-not-trade penalties (0–100)
    dnt = 0.0
    if micro.liquidity_score < 35:
        dnt += 25
        reasons.append("low liquidity")
    spread = max(
        micro.spread_yes or 0.0,
        micro.spread_no or 0.0,
    )
    if spread > 0.10:
        dnt += 20
        reasons.append("wide spread")
    if price.fake_breakout:
        dnt += 15
        reasons.append("fake breakout detected")
    if not model_agreement.models_agree:
        dnt += 20
        reasons.append("models disagree")
    if regime in {Regime.CHAOTIC_UNSTABLE, Regime.UNCERTAIN, Regime.CHOPPY}:
        dnt += 15
        reasons.append("erratic regime")
    if micro.cancellation_rate > 0.65:
        dnt += 10
        reasons.append("high cancellation rate")
    if price.volatility_expansion > 2.0:
        dnt += 10
        reasons.append("volatility spike")

    trade_quality = max(0.0, min(100.0, trade_quality - dnt * 0.3))
    dnt = max(0.0, min(100.0, dnt))

    # Trade tier classification (15m)
    tier = TradeTier.NONE
    size_mult = 0.0
    if trade_quality >= 85 and edge and edge >= 0.25:
        tier = TradeTier.A_PLUS
        size_mult = 1.0
    elif trade_quality >= 75 and edge and edge >= 0.22:
        tier = TradeTier.A
        size_mult = 0.75
    elif trade_quality >= 65 and edge and edge >= 0.20:
        tier = TradeTier.B
        size_mult = 0.5

    execute = (
        trade_quality >= min_quality_score
        and dnt <= max_dnt_score
        and model_agreement.models_agree
        and tier is not TradeTier.NONE
        and not price.fake_breakout
    )

    return TradeQualityAssessment(
        trade_quality_score=round(trade_quality, 1),
        do_not_trade_score=round(dnt, 1),
        confidence_pct=round(forecast.confidence * 100, 1),
        expected_edge_pct=round((edge or 0.0) * 100, 1),
        liquidity_label=_liquidity_label(micro.liquidity_score),
        historical_match_count=0,
        recommendation="EXECUTE" if execute else "SKIP",
        trade_tier=tier,
        size_multiplier=size_mult,
        reasons=tuple(reasons),
    )
