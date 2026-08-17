"""Terminal BRTI finish-above-strike probability engine for 1-hour contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from kalshi_bot.domain import FeatureSnapshot, ProbabilityEstimate, Regime
from kalshi_bot.hour.probability_model import (
    HourProbabilityModel,
    _breakout_adjustment,
    _mean_reversion_adjustment,
    _trend_adjustment,
)
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot
from kalshi_bot.models.ensemble import _terminal_probability, SECONDS_PER_YEAR

TERMINAL_CORE_WEIGHT = 0.55
ENSEMBLE_SUPPORT_WEIGHT = 0.45


@dataclass(frozen=True)
class TerminalForecast:
    """Expiration-outcome distribution summary for one hourly contract."""

    p_yes: float
    p_no: float
    raw_p_yes: float
    calibrated_p_yes: float
    calibrated_p_no: float
    expected_terminal_brti: float
    terminal_volatility: float
    distance_from_strike: float
    normalized_strike_distance: float
    confidence: float
    signal_agreement: float
    component_probabilities: dict[str, float]
    strike: float
    current_brti: float
    seconds_remaining: float
    settlement_reference: str
    notes: tuple[str, ...] = ()

    def as_probability_estimate(self, regime: Regime | None = None) -> ProbabilityEstimate:
        return ProbabilityEstimate(
            p_up=self.calibrated_p_yes,
            p_down=self.calibrated_p_no,
            confidence=self.confidence,
            signal_agreement=self.signal_agreement,
            component_probabilities=dict(self.component_probabilities),
            regime=regime or Regime.TREND_UP,
            raw_p_up=self.raw_p_yes,
            calibrated=True,
            notes=self.notes,
        )


class TerminalProbabilityEngine:
    """Estimate where BRTI finishes relative to the actual contract strike at expiry."""

    def __init__(
        self,
        *,
        model: HourProbabilityModel | None = None,
        model_version: str = "terminal-v1",
        terminal_core_weight: float = TERMINAL_CORE_WEIGHT,
    ) -> None:
        self.model = model or HourProbabilityModel(model_version=model_version)
        self.model_version = model_version
        self.terminal_core_weight = terminal_core_weight

    def _resolve_strike(self, features: FeatureSnapshot, market_strike: float | None) -> float:
        if market_strike is not None and math.isfinite(market_strike) and market_strike > 0:
            strike = float(market_strike)
        else:
            strike = features.settlement_effective_strike
            if strike is None or not math.isfinite(strike) or strike <= 0:
                strike = features.strike
        if strike is None or not math.isfinite(strike) or strike <= 0:
            raise ValueError("actual contract strike is missing or invalid")
        return float(strike)

    def _blend_volatility(
        self,
        features: FeatureSnapshot,
        vol: VolatilitySnapshot,
        options_volatility: float | None,
    ) -> float:
        realized = max(features.realized_vol, 0.10)
        short_horizon = abs(features.changes.get(60, features.short_trend)) * math.sqrt(60)
        short_vol = max(short_horizon * 8.0, 0.10)
        iv = options_volatility if options_volatility is not None else realized
        iv = max(min(iv, 2.50), 0.10)
        blended = 0.45 * realized + 0.20 * short_vol + 0.35 * iv
        if vol.vol_expansion > 0.2:
            blended *= 1.0 + min(vol.vol_expansion, 0.5) * 0.15
        return max(min(blended, 2.50), 0.10)

    def _expected_terminal_brti(
        self,
        spot: float,
        seconds_remaining: float,
        terminal_vol: float,
    ) -> float:
        years = max(seconds_remaining, 1.0) / SECONDS_PER_YEAR
        sigma_sq_t = terminal_vol * terminal_vol * years
        return spot * math.exp(0.5 * sigma_sq_t)

    def estimate(
        self,
        features: FeatureSnapshot,
        regime: Regime,
        trend: TrendSnapshot,
        vol: VolatilitySnapshot,
        *,
        market_strike: float | None = None,
        settlement_reference: str = "CME CF Bitcoin Real Time Index (BRTI)",
        options_volatility: float | None = None,
        market_prior: float | None = None,
        window_regime=None,
        calibrator=None,
    ) -> TerminalForecast:
        strike = self._resolve_strike(features, market_strike)
        spot = float(features.current_price)
        if spot <= 0:
            raise ValueError("current BRTI must be positive")

        seconds = max(float(features.seconds_remaining), 1.0)
        blended_vol = self._blend_volatility(features, vol, options_volatility)
        terminal_vol = blended_vol * math.sqrt(seconds / SECONDS_PER_YEAR)

        core_p_yes = _terminal_probability(spot, strike, seconds, blended_vol)

        ensemble = self.model.estimate(
            features,
            regime,
            trend,
            vol,
            options_volatility=options_volatility,
            market_prior=market_prior,
            window_regime=window_regime,
        )

        adjustment = (
            _trend_adjustment(trend)
            + _breakout_adjustment(features, vol)
            + _mean_reversion_adjustment(features, trend)
            + trend.trend_consistency * _trend_adjustment(trend) * 0.20
        )
        if regime in {Regime.CHOPPY, Regime.UNCERTAIN}:
            adjustment *= 0.5

        support_p_yes = max(
            0.03,
            min(0.97, ensemble.p_up + adjustment),
        )
        raw_p_yes = max(
            0.03,
            min(
                0.97,
                self.terminal_core_weight * core_p_yes
                + (1.0 - self.terminal_core_weight) * support_p_yes,
            ),
        )

        calibrated_p_yes = raw_p_yes
        if calibrator is not None and getattr(calibrator, "fit_cutoff", None) is not None:
            calibrated_p_yes = float(calibrator.transform(raw_p_yes))

        distance = spot - strike
        normalized = distance / max(spot * terminal_vol, 1e-9)

        confidence = ensemble.confidence
        if regime in {Regime.CHOPPY, Regime.UNCERTAIN, Regime.HIGH_VOLATILITY}:
            confidence *= 0.85
        if trend.trend_consistency >= 0.7:
            confidence = min(1.0, confidence * 1.05)
        confidence = max(0.0, min(1.0, confidence))

        agreement = min(
            1.0,
            ensemble.signal_agreement * 0.55 + trend.trend_consistency * 0.45,
        )

        components = {
            "terminal_core": core_p_yes,
            "ensemble_support": support_p_yes,
            **{f"ensemble_{k}": v for k, v in ensemble.component_probabilities.items()},
        }

        notes = (
            f"terminal engine {self.model_version}",
            f"strike=${strike:,.2f}",
            f"brti=${spot:,.2f}",
            f"core_p_yes={core_p_yes:.4f}",
            f"support_p_yes={support_p_yes:.4f}",
            f"blended_vol={blended_vol:.3f}",
        )

        return TerminalForecast(
            p_yes=raw_p_yes,
            p_no=1.0 - raw_p_yes,
            raw_p_yes=raw_p_yes,
            calibrated_p_yes=calibrated_p_yes,
            calibrated_p_no=1.0 - calibrated_p_yes,
            expected_terminal_brti=self._expected_terminal_brti(spot, seconds, blended_vol),
            terminal_volatility=terminal_vol,
            distance_from_strike=distance,
            normalized_strike_distance=normalized,
            confidence=confidence,
            signal_agreement=agreement,
            component_probabilities=components,
            strike=strike,
            current_brti=spot,
            seconds_remaining=seconds,
            settlement_reference=settlement_reference,
            notes=notes,
        )

    @staticmethod
    def probability_stability(
        history: tuple[float, ...],
        *,
        max_swing: float,
    ) -> tuple[bool, float]:
        """Return whether recent calibrated YES probabilities are stable enough."""
        if len(history) < 2:
            return True, 0.0
        swing = max(history) - min(history)
        return swing <= max_swing, swing
