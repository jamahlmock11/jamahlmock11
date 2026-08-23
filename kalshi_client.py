"""Async Kalshi REST client — RSA-PSS auth, market discovery, and order placement."""

from __future__ import annotations

import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

_CLIENT_ORDER_ID_RE = re.compile(r"[^A-Za-z0-9-]+")


def sanitize_client_order_id(client_order_id: str) -> str:
    cleaned = _CLIENT_ORDER_ID_RE.sub("-", client_order_id).strip("-")
    return cleaned or str(uuid.uuid4())


@dataclass(frozen=True)
class ActiveContract:
    ticker: str
    strike: float
    open_time: datetime
    close_time: datetime
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    yes_ask_size: float

    @property
    def seconds_elapsed(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.open_time).total_seconds())

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, (self.close_time - datetime.now(timezone.utc)).total_seconds())

    @property
    def spread_cents(self) -> int:
        return max(0, int(round((self.yes_ask - self.yes_bid) * 100)))


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_strike(raw: dict[str, Any]) -> float | None:
    for key in ("floor_strike", "strike", "target_price"):
        value = raw.get(key)
        if value is not None:
            strike = _f(value, default=0.0)
            if strike > 0:
                return strike
    return None


class AsyncKalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str = "",
        private_key_path: str = "",
        *,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key = None
        self._private_key_path = (
            private_key_path if private_key_path and Path(private_key_path).exists() else ""
        )
        if self._private_key_path:
            pem = Path(self._private_key_path).read_bytes()
            self._private_key = serialization.load_pem_private_key(pem, password=None)
        self._http = httpx.AsyncClient(timeout=timeout)

    @property
    def authenticated(self) -> bool:
        return bool(self.api_key_id and self._private_key is not None)

    async def close(self) -> None:
        await self._http.aclose()

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if self._private_key is None:
            raise RuntimeError("Kalshi private key not loaded")
        message = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, endpoint: str) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if not self.authenticated:
            return headers
        ts = str(int(time.time() * 1000))
        sign_path = urlparse(f"{self.base_url}{endpoint}").path
        headers.update(
            {
                "KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, sign_path),
            }
        )
        return headers

    async def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = f"{self.base_url}{endpoint}"
        resp = await self._http.request(
            method,
            url,
            headers=self._headers(method, endpoint),
            **kwargs,
        )
        if resp.status_code >= 400:
            logger.error("Kalshi %s %s → %s %s", method, endpoint, resp.status_code, resp.text[:300])
            resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def get(self, endpoint: str, params: dict | None = None) -> Any:
        return await self.request("GET", endpoint, params=params)

    async def post(self, endpoint: str, json: dict | None = None) -> Any:
        return await self.request("POST", endpoint, json=json)

    async def delete(self, endpoint: str) -> Any:
        return await self.request("DELETE", endpoint)

    async def fetch_active_contract(self, series_ticker: str = "KXBTC15M") -> ActiveContract | None:
        """Fetch the currently open 15-minute contract and extract strike + ticker."""
        data = await self.get(
            "/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": 50},
        )
        now = datetime.now(timezone.utc)
        candidates: list[ActiveContract] = []
        for raw in data.get("markets") or []:
            contract = self._to_active_contract(raw)
            if contract is None:
                continue
            if contract.open_time <= now < contract.close_time:
                candidates.append(contract)
        if not candidates:
            return None
        return min(candidates, key=lambda c: c.close_time)

    async def get_orderbook(self, ticker: str, depth: int = 20) -> dict[str, Any]:
        return await self.get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_open_orders(self, ticker: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "resting", "limit": 200}
        if ticker:
            params["ticker"] = ticker
        data = await self.get("/portfolio/orders", params=params)
        return list(data.get("orders") or [])

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self.delete(f"/portfolio/orders/{order_id}")

    async def cancel_all_orders(self, ticker: str | None = None) -> int:
        cancelled = 0
        for order in await self.get_open_orders(ticker=ticker):
            order_id = order.get("order_id") or order.get("id")
            if not order_id:
                continue
            await self.cancel_order(str(order_id))
            cancelled += 1
        return cancelled

    async def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        *,
        yes_price: int | None = None,
        no_price: int | None = None,
        time_in_force: str = "immediate_or_cancel",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        side_l = side.lower()
        action_l = action.lower()
        if side_l == "yes":
            if yes_price is None:
                raise ValueError("yes_price required for YES orders")
            book_side = "bid" if action_l == "buy" else "ask"
            price = yes_price / 100.0
        elif side_l == "no":
            if no_price is None:
                raise ValueError("no_price required for NO orders")
            book_side = "ask" if action_l == "buy" else "bid"
            price = (100 - int(no_price)) / 100.0
        else:
            raise ValueError(f"Unsupported side: {side}")

        body: dict[str, Any] = {
            "ticker": ticker,
            "side": book_side,
            "count": f"{int(count):.2f}",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": sanitize_client_order_id(client_order_id or str(uuid.uuid4())),
            "exchange_index": -1,
        }
        return await self.post("/portfolio/events/orders", json=body)

    def _to_active_contract(self, raw: dict[str, Any]) -> ActiveContract | None:
        ticker = raw.get("ticker") or ""
        close = _parse_dt(raw.get("close_time"))
        open_time = _parse_dt(raw.get("open_time"))
        strike = _extract_strike(raw)
        if not ticker or not close or not open_time or strike is None:
            return None

        yes_bid = _f(raw.get("yes_bid_dollars"))
        yes_ask = _f(raw.get("yes_ask_dollars"))
        if yes_bid == 0 and raw.get("yes_bid") is not None:
            yes_bid = _f(raw.get("yes_bid")) / 100.0
        if yes_ask == 0 and raw.get("yes_ask") is not None:
            yes_ask = _f(raw.get("yes_ask")) / 100.0
        no_bid = max(0.0, 1.0 - yes_ask) if yes_ask else _f(raw.get("no_bid_dollars"))
        no_ask = max(0.0, 1.0 - yes_bid) if yes_bid else _f(raw.get("no_ask_dollars"))

        return ActiveContract(
            ticker=ticker,
            strike=strike,
            open_time=open_time,
            close_time=close,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=_f(raw.get("yes_bid_size_fp") or raw.get("yes_bid_size")),
            yes_ask_size=_f(raw.get("yes_ask_size_fp") or raw.get("yes_ask_size")),
        )

    @staticmethod
    def safe_order_size(
        *,
        limit_price_cents: int,
        depth_at_price: float,
        spread_cents: int,
        max_contracts: int,
        max_spread_cents: int,
        min_book_depth: int,
        max_price_sweep_cents: int,
    ) -> int:
        """Penny-tick sizing: never chase into thin books that sweep multiple cents."""
        if spread_cents > max_spread_cents:
            return 0
        if depth_at_price < min_book_depth:
            return 0
        if limit_price_cents < 1 or limit_price_cents > 99:
            return 0
        sweep_room = max(0, max_price_sweep_cents)
        affordable_depth = min(depth_at_price, max_contracts)
        if spread_cents > sweep_room and affordable_depth > depth_at_price * 0.5:
            affordable_depth = max(1.0, depth_at_price * 0.25)
        return max(0, min(max_contracts, int(affordable_depth)))
