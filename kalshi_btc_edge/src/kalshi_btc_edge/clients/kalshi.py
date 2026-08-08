from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from kalshi_btc_edge.models import BookQuote, KalshiMarket

log = logging.getLogger(__name__)


def _parse_ts(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _series_from_ticker(ticker: str) -> str:
    # KXBTC15M-26AUG080045-45 → KXBTC15M; KXBTCD-26AUG0817-T74249.99 → KXBTCD
    if ticker.startswith("KXBTC15M"):
        return "KXBTC15M"
    if ticker.startswith("KXBTCD"):
        return "KXBTCD"
    return ticker.split("-", 1)[0]


class KalshiClient:
    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def get_markets(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 200,
    ) -> list[KalshiMarket]:
        markets: list[KalshiMarket] = []
        cursor: Optional[str] = None
        # API accepts status=open; some responses use status=active
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "limit": min(limit, 1000),
            }
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            url = f"{self.base_url}/markets"
            resp = self.session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
            for raw in payload.get("markets", []):
                markets.append(self._to_market(raw, series_ticker))
            cursor = payload.get("cursor") or None
            if not cursor or len(markets) >= limit:
                break
        return markets[:limit]

    def _to_market(self, raw: dict[str, Any], series_hint: str) -> KalshiMarket:
        ticker = str(raw["ticker"])
        book = BookQuote(
            yes_bid=_f(raw.get("yes_bid_dollars")),
            yes_ask=_f(raw.get("yes_ask_dollars")),
            yes_bid_size=_f(raw.get("yes_bid_size_fp")),
            yes_ask_size=_f(raw.get("yes_ask_size_fp")),
        )
        floor = raw.get("floor_strike")
        return KalshiMarket(
            ticker=ticker,
            event_ticker=str(raw.get("event_ticker", "")),
            series_ticker=series_hint or _series_from_ticker(ticker),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", "")),
            close_time=_parse_ts(raw["close_time"]),
            floor_strike=float(floor) if floor is not None else None,
            strike_type=str(raw.get("strike_type", "")),
            book=book,
            rules_primary=str(raw.get("rules_primary", "")),
            volume=_f(raw.get("volume_fp")),
            open_interest=_f(raw.get("open_interest_fp")),
        )
