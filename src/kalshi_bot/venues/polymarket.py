"""Polymarket Gamma + CLOB clients for BTC 15m up/down markets."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


@dataclass
class PolyMarket:
    slug: str
    question: str
    end_ts: float
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    condition_id: str = ""

    @property
    def pair_cost(self) -> float:
        return self.up_price + self.down_price


def current_15m_slug(now: float | None = None) -> str:
    ts = int((now or time.time()) // 900) * 900
    return f"btc-updown-15m-{ts}"


def next_15m_slug(now: float | None = None) -> str:
    ts = int((now or time.time()) // 900) * 900 + 900
    return f"btc-updown-15m-{ts}"


class PolymarketClient:
    def __init__(self, timeout: float = 20.0):
        self._http = httpx.Client(timeout=timeout, base_url=GAMMA_URL)
        self._clob = httpx.Client(timeout=timeout, base_url=CLOB_URL)

    def close(self) -> None:
        self._http.close()
        self._clob.close()

    def get_event_by_slug(self, slug: str) -> dict[str, Any] | None:
        resp = self._http.get("/events", params={"slug": slug})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data or None

    def get_midpoint(self, token_id: str) -> float | None:
        try:
            resp = self._clob.get("/midpoint", params={"token_id": token_id})
            resp.raise_for_status()
            mid = resp.json().get("mid")
            return float(mid) if mid is not None else None
        except Exception as exc:
            logger.debug("midpoint fail %s: %s", token_id[:12], exc)
            return None

    def get_price(self, token_id: str, side: str = "buy") -> float | None:
        try:
            resp = self._clob.get("/price", params={"token_id": token_id, "side": side})
            resp.raise_for_status()
            px = resp.json().get("price")
            return float(px) if px is not None else None
        except Exception as exc:
            logger.debug("price fail %s: %s", token_id[:12], exc)
            return None

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        resp = self._clob.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()

    def best_ask(self, token_id: str) -> float | None:
        try:
            book = self.get_order_book(token_id)
            asks = book.get("asks") or []
            if not asks:
                return self.get_price(token_id, "buy")
            # asks may be sorted ascending or descending depending on API version
            prices = [float(a["price"]) for a in asks if float(a.get("price") or 0) > 0]
            return min(prices) if prices else None
        except Exception as exc:
            logger.debug("best_ask fail: %s", exc)
            return None

    def get_btc_15m(self, prefer_current: bool = True) -> PolyMarket | None:
        slugs = [current_15m_slug()]
        if not prefer_current:
            slugs = [next_15m_slug(), current_15m_slug()]
        else:
            slugs.append(next_15m_slug())
        for slug in slugs:
            mkt = self._parse_event(slug)
            if mkt:
                return mkt
        return None

    def _parse_event(self, slug: str) -> PolyMarket | None:
        event = self.get_event_by_slug(slug)
        if not event:
            return None
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        # clobTokenIds is often a JSON string
        raw_ids = m.get("clobTokenIds") or m.get("clob_token_ids")
        if isinstance(raw_ids, str):
            try:
                raw_ids = json.loads(raw_ids)
            except json.JSONDecodeError:
                raw_ids = [x.strip() for x in raw_ids.strip("[]").split(",")]
        if not raw_ids or len(raw_ids) < 2:
            return None
        up_id, down_id = str(raw_ids[0]), str(raw_ids[1])

        # outcome prices fallback
        outcome_prices = m.get("outcomePrices")
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except json.JSONDecodeError:
                outcome_prices = None

        up_ask = self.best_ask(up_id)
        down_ask = self.best_ask(down_id)
        if up_ask is None and outcome_prices:
            up_ask = float(outcome_prices[0])
        if down_ask is None and outcome_prices and len(outcome_prices) > 1:
            down_ask = float(outcome_prices[1])
        if up_ask is None or down_ask is None:
            return None

        end = event.get("endDate") or m.get("endDate") or ""
        try:
            from datetime import datetime

            end_ts = datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp()
        except Exception:
            end_ts = time.time() + 900

        return PolyMarket(
            slug=slug,
            question=str(m.get("question") or event.get("title") or slug),
            end_ts=end_ts,
            up_token_id=up_id,
            down_token_id=down_id,
            up_price=up_ask,
            down_price=down_ask,
            condition_id=str(m.get("conditionId") or ""),
        )