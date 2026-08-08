"""IBIT options chain → volatility smile."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import yfinance as yf

from kalshi_bot.models.vol_smile import SmilePoint, VolSmile, select_otm_points

logger = logging.getLogger(__name__)


@dataclass
class SpotQuotes:
    btc: float
    ibit: float
    ratio: float  # ibit / btc
    ts: float


class MarketDataError(RuntimeError):
    pass


class IBITOptionsProvider:
    """Pull IBIT options chain via Yahoo Finance and build OTM vol smiles."""

    def __init__(self, cache_sec: float = 60.0, default_iv: float = 0.60):
        self.cache_sec = cache_sec
        self.default_iv = default_iv
        self._smile_cache: tuple[float, list[VolSmile]] | None = None
        self._spot_cache: tuple[float, SpotQuotes] | None = None

    def get_spots(self) -> SpotQuotes:
        now = time.time()
        if self._spot_cache and now - self._spot_cache[0] < 15:
            return self._spot_cache[1]
        btc = self._last_price("BTC-USD")
        ibit = self._last_price("IBIT")
        if btc <= 0 or ibit <= 0:
            raise MarketDataError(f"Invalid spots BTC={btc} IBIT={ibit}")
        q = SpotQuotes(btc=btc, ibit=ibit, ratio=ibit / btc, ts=now)
        self._spot_cache = (now, q)
        return q

    def get_smiles(self) -> list[VolSmile]:
        now = time.time()
        if self._smile_cache and now - self._smile_cache[0] < self.cache_sec:
            return self._smile_cache[1]
        smiles = self._fetch_smiles()
        self._smile_cache = (now, smiles)
        return smiles

    def nearest_smile(self, target_t_years: float) -> VolSmile | None:
        smiles = self.get_smiles()
        if not smiles:
            return None
        return min(smiles, key=lambda s: abs(s.t_years - max(target_t_years, 1e-8)))

    def _last_price(self, symbol: str) -> float:
        t = yf.Ticker(symbol)
        # fast_info can be flaky; fall back to history
        try:
            fi = t.fast_info
            px = float(getattr(fi, "last_price", None) or fi.get("last_price") or 0)  # type: ignore[attr-defined]
            if px > 0:
                return px
        except Exception:
            pass
        hist = t.history(period="1d", interval="1m")
        if hist is None or hist.empty:
            hist = t.history(period="5d")
        if hist is None or hist.empty:
            raise MarketDataError(f"No price for {symbol}")
        return float(hist["Close"].iloc[-1])

    def _fetch_smiles(self) -> list[VolSmile]:
        spots = self.get_spots()
        ticker = yf.Ticker("IBIT")
        try:
            expirations = list(ticker.options or [])
        except Exception as exc:
            logger.warning("IBIT options expirations unavailable: %s", exc)
            return []
        if not expirations:
            logger.warning("No IBIT option expirations returned")
            return []

        now = time.time()
        smiles: list[VolSmile] = []
        # Take nearest few expiries — short Kalshi horizons map to front smiles
        for exp in expirations[:4]:
            try:
                chain = ticker.option_chain(exp)
            except Exception as exc:
                logger.debug("Skip expiry %s: %s", exp, exc)
                continue
            calls = self._points_from_frame(chain.calls, "call")
            puts = self._points_from_frame(chain.puts, "put")
            otm = select_otm_points(spots.ibit, calls, puts)
            if len(otm) < 3:
                continue
            # Yahoo expiry is date string YYYY-MM-DD → assume 16:00 ET ≈ 20:00 UTC
            try:
                from datetime import datetime, timezone

                exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(
                    hour=20, minute=0, tzinfo=timezone.utc
                )
                exp_ts = exp_dt.timestamp()
            except ValueError:
                continue
            t_years = max(exp_ts - now, 3600) / (365.25 * 24 * 3600)
            smile = VolSmile(
                underlying="IBIT",
                spot=spots.ibit,
                expiry_ts=exp_ts,
                t_years=t_years,
                points=otm,
            ).build()
            smiles.append(smile)
        logger.info("Built %d IBIT vol smiles (spot=%.2f)", len(smiles), spots.ibit)
        return smiles

    @staticmethod
    def _points_from_frame(frame, option_type: str) -> list[SmilePoint]:
        points: list[SmilePoint] = []
        if frame is None or frame.empty:
            return points
        for _, row in frame.iterrows():
            try:
                strike = float(row.get("strike") or 0)
                iv = float(row.get("impliedVolatility") or 0)
                bid = float(row.get("bid") or 0)
                ask = float(row.get("ask") or 0)
                oi = float(row.get("openInterest") or 0)
            except (TypeError, ValueError):
                continue
            if strike <= 0 or iv <= 0.01 or iv > 5.0:
                continue
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None
            points.append(
                SmilePoint(
                    strike=strike,
                    iv=iv,
                    option_type=option_type,
                    mid=mid,
                    oi=oi,
                )
            )
        return points


class BRTIProxy:
    """Settlement reference proxy for CF Benchmarks BRTI.

    Production settlement uses CF Benchmarks BRTI 60-second average.
    Without a CF license we proxy with BTC-USD (Yahoo / Coinbase).
    The absolute level is used for strike distance; relative moves dominate
    15m/1h probability — proxy basis risk is small vs options-edge signal.
    """

    def __init__(self, provider: IBITOptionsProvider):
        self.provider = provider

    def spot(self) -> float:
        return self.provider.get_spots().btc