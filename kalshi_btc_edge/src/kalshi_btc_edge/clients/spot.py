from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class SpotClient:
    """Spot proxies. Settlement is BRTI — Coinbase/Yahoo are pricing proxies only."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.session = requests.Session()

    def btc_usd(self, source: str = "coinbase") -> float:
        source = source.lower()
        if source == "coinbase":
            return self._coinbase_btc()
        if source == "file":
            raise ValueError("file spot source requires explicit override")
        raise ValueError(f"unknown btc_spot_source: {source}")

    def ibit_usd(self, source: str = "yahoo", fallback: Optional[float] = None) -> float:
        source = source.lower()
        if source == "static":
            return float(fallback or 36.5)
        if source == "yahoo":
            try:
                return self._yahoo_last("IBIT")
            except Exception as exc:  # noqa: BLE001
                log.warning("IBIT yahoo spot failed (%s); using fallback", exc)
                if fallback is not None:
                    return float(fallback)
                raise
        if source == "file":
            if fallback is None:
                raise ValueError("file ibit spot needs fallback from smile")
            return float(fallback)
        raise ValueError(f"unknown ibit_spot_source: {source}")

    def _coinbase_btc(self) -> float:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return float(resp.json()["data"]["amount"])

    def _yahoo_last(self, symbol: str) -> float:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        resp = self.session.get(
            url,
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "kalshi-btc-edge/0.1"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta = result["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if price is None:
            raise RuntimeError(f"no yahoo price for {symbol}")
        return float(price)
