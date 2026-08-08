from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)


@dataclass
class PolyMarket:
    id: str
    question: str
    end_time: Optional[datetime]
    yes_ask: float
    no_ask: float
    slug: str = ""


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class PolymarketClient:
    def __init__(self, gamma_url: str, timeout: float = 20.0):
        self.gamma_url = gamma_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def search_btc_15m(self, limit: int = 50) -> list[PolyMarket]:
        """Best-effort discovery of BTC short-horizon up/down markets."""
        out: list[PolyMarket] = []
        # Gamma search is fuzzy; filter client-side.
        for query in ("bitcoin up down", "btc 15", "bitcoin 15 minutes"):
            try:
                out.extend(self._fetch_markets(query, limit=limit))
            except Exception as exc:  # noqa: BLE001
                log.warning("polymarket search %r failed: %s", query, exc)
        # de-dupe by id
        seen: set[str] = set()
        unique: list[PolyMarket] = []
        for m in out:
            if m.id in seen:
                continue
            seen.add(m.id)
            unique.append(m)
        return unique

    def _fetch_markets(self, query: str, limit: int) -> list[PolyMarket]:
        url = f"{self.gamma_url}/public-search"
        resp = self.session.get(
            url,
            params={"q": query, "limit_per_type": limit},
            timeout=self.timeout,
        )
        if resp.status_code == 404:
            # Fallback: events list
            return self._fetch_events_fallback(limit)
        resp.raise_for_status()
        payload = resp.json()
        markets_raw = payload.get("markets") or payload.get("events") or []
        results: list[PolyMarket] = []
        if isinstance(markets_raw, dict):
            markets_raw = markets_raw.get("data", [])
        for item in markets_raw:
            parsed = self._parse_item(item)
            if parsed:
                results.append(parsed)
        return results

    def _fetch_events_fallback(self, limit: int) -> list[PolyMarket]:
        url = f"{self.gamma_url}/events"
        resp = self.session.get(
            url,
            params={"active": "true", "closed": "false", "limit": limit, "tag": "crypto"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        results: list[PolyMarket] = []
        for event in resp.json():
            for market in event.get("markets") or []:
                parsed = self._parse_item(market)
                if parsed and self._looks_btc_short(parsed.question):
                    results.append(parsed)
        return results

    def _looks_btc_short(self, question: str) -> bool:
        q = question.lower()
        return ("btc" in q or "bitcoin" in q) and (
            "up" in q or "down" in q or "15" in q or "hour" in q
        )

    def _parse_item(self, item: dict[str, Any]) -> Optional[PolyMarket]:
        # Search may return events wrapping markets
        if "markets" in item and isinstance(item["markets"], list) and item["markets"]:
            item = item["markets"][0]
        mid = str(item.get("id") or item.get("conditionId") or "")
        question = str(item.get("question") or item.get("title") or "")
        if not mid or not question:
            return None
        if not self._looks_btc_short(question):
            return None
        yes_ask, no_ask = self._outcome_asks(item)
        end = _parse_ts(
            item.get("endDate")
            or item.get("end_date_iso")
            or item.get("closeTime")
        )
        return PolyMarket(
            id=mid,
            question=question,
            end_time=end,
            yes_ask=yes_ask,
            no_ask=no_ask,
            slug=str(item.get("slug") or ""),
        )

    def _outcome_asks(self, item: dict[str, Any]) -> tuple[float, float]:
        # outcomePrices often JSON string of mids; bestAsk may be absent.
        prices = item.get("outcomePrices") or item.get("outcome_prices")
        if isinstance(prices, str):
            import json

            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        yes = 0.5
        no = 0.5
        if isinstance(prices, list) and len(prices) >= 2:
            yes = float(prices[0])
            no = float(prices[1])
        # Treat mid as ask proxy when true asks unavailable (conservative: pad 1¢)
        return min(0.99, yes + 0.01), min(0.99, no + 0.01)
