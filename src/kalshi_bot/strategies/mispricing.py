"""Primary strategy: Kalshi vs IBIT-options-implied mispricing."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from kalshi_bot.config import AppConfig
from kalshi_bot.data.ibit_options import BRTIProxy, IBITOptionsProvider
from kalshi_bot.models.black_scholes import annualize_horizon_years
from kalshi_bot.models.probability import EdgeSignal, Confidence, detect_mispricing
from kalshi_bot.venues.kalshi import KalshiClient, KalshiMarket

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    signals: list[EdgeSignal]
    markets_scanned: int
    spot: float
    ibit: float
    iv_atm: float | None


def extract_strike(market: KalshiMarket, spot: float) -> float | None:
    """Resolve the BTC strike for probability pricing.

    - Threshold / directional (KXBTCD): floor_strike
    - 15m up/down (KXBTC15M): open reference ≈ floor_strike / yes_sub target,
      else current spot (P(S_T >= S_open))
    """
    if market.floor_strike and market.floor_strike > 1000:
        return market.floor_strike
    # Parse from subtitle / rules e.g. Target Price: $64,963.68
    for text in (market.title, market.rules_primary):
        m = re.search(r"\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)", text)
        if m:
            return float(m.group(1).replace(",", ""))
    # Up/down without explicit strike → use spot as barrier
    if market.series == "KXBTC15M":
        return spot
    return None


class MispricingScanner:
    def __init__(
        self,
        kalshi: KalshiClient,
        options: IBITOptionsProvider,
        brti: BRTIProxy,
        config: AppConfig,
    ):
        self.kalshi = kalshi
        self.options = options
        self.brti = brti
        self.config = config

    def scan(self) -> ScanResult:
        spots = self.options.get_spots()
        btc = self.brti.spot()
        smiles = self.options.get_smiles()
        signals: list[EdgeSignal] = []
        scanned = 0
        iv_atm = smiles[0].atm_iv(self.config.pricing.default_iv) if smiles else None

        for series in self.config.series:
            try:
                markets = self.kalshi.get_markets(series_ticker=series, status="open", limit=100)
            except Exception as exc:
                logger.error("Failed to fetch %s markets: %s", series, exc)
                continue
            for mkt in markets:
                if mkt.status not in ("open", "active"):
                    continue
                # Skip markets about to settle (< 30s) or too far for 15m series
                if mkt.seconds_to_close < 30:
                    continue
                strike = extract_strike(mkt, btc)
                if strike is None:
                    continue
                scanned += 1
                t_years = annualize_horizon_years(mkt.seconds_to_close)
                smile = None
                if smiles:
                    smile = min(smiles, key=lambda s: abs(s.t_years - max(t_years, 1e-8)))

                # Refresh sizes: if ask size missing, treat as 0
                sig = detect_mispricing(
                    ticker=mkt.ticker,
                    series=mkt.series,
                    yes_bid=mkt.yes_bid,
                    yes_ask=mkt.yes_ask if mkt.yes_ask > 0 else 1.0,
                    yes_ask_size=mkt.yes_ask_size,
                    no_ask=mkt.no_ask if mkt.no_ask > 0 else 1.0,
                    no_ask_size=mkt.no_ask_size,
                    strike=strike,
                    spot=btc,
                    seconds_to_expiry=mkt.seconds_to_close,
                    smile=smile,
                    ibit_spot=spots.ibit,
                    r=self.config.pricing.risk_free_rate,
                    fallback_iv=self.config.pricing.default_iv,
                    min_iv=self.config.pricing.min_iv,
                    max_iv=self.config.pricing.max_iv,
                    tiers=self.config.tiers,
                )
                if sig and sig.confidence is not Confidence.PASS:
                    signals.append(sig)

        signals.sort(key=lambda s: s.edge_pp, reverse=True)
        return ScanResult(
            signals=signals,
            markets_scanned=scanned,
            spot=btc,
            ibit=spots.ibit,
            iv_atm=iv_atm,
        )