"""Kalshi REST client — RSA-PSS auth, markets, orderbook, orders."""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

logger = logging.getLogger(__name__)


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    series: str
    title: str
    status: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_bid_size: float
    yes_ask_size: float
    no_bid_size: float
    no_ask_size: float
    floor_strike: float | None
    close_time: datetime
    open_time: datetime | None
    rules_primary: str
    strike_type: str
    volume: float

    @property
    def seconds_to_close(self) -> float:
        return max(0.0, (self.close_time - datetime.now(timezone.utc)).total_seconds())

    @property
    def spread(self) -> float:
        return max(0.0, self.yes_ask - self.yes_bid)


def _f(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def series_from_ticker(ticker: str) -> str:
    # KXBTC15M-26AUG080330-30 → KXBTC15M ; KXBTCD-26AUG0804-T73799.99 → KXBTCD
    for prefix in ("KXBTC15M", "KXBTCD", "KXBTC"):
        if ticker.startswith(prefix):
            return prefix
    return ticker.split("-")[0]


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str = "",
        private_key_path: str = "",
        timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key = None
        if private_key_path and Path(private_key_path).exists():
            pem = Path(private_key_path).read_bytes()
            self._private_key = serialization.load_pem_private_key(pem, password=None)
        self._http = httpx.Client(timeout=timeout)

    @property
    def authenticated(self) -> bool:
        return bool(self.api_key_id and self._private_key)

    def close(self) -> None:
        self._http.close()

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if not self._private_key:
            raise RuntimeError("Kalshi private key not loaded")
        # Sign path without query string
        path = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, full_path: str) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if not self.authenticated:
            return headers
        ts = str(int(time.time() * 1000))
        # full_path is /trade-api/v2/...
        headers.update(
            {
                "KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, full_path),
            }
        )
        return headers

    def _url_path(self, endpoint: str) -> str:
        """Return the path component used for signing (/trade-api/v2/...)."""
        url = f"{self.base_url}{endpoint}"
        return urlparse(url).path

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(1, 4), reraise=True)
    def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = f"{self.base_url}{endpoint}"
        sign_path = self._url_path(endpoint.split("?")[0])
        headers = self._headers(method, sign_path)
        resp = self._http.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def get(self, endpoint: str, params: dict | None = None) -> Any:
        return self.request("GET", endpoint, params=params)

    def post(self, endpoint: str, json: dict | None = None) -> Any:
        return self.request("POST", endpoint, json=json)

    def get_markets(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 200,
    ) -> list[KalshiMarket]:
        markets: list[KalshiMarket] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": status,
                "limit": min(limit, 1000),
            }
            if cursor:
                params["cursor"] = cursor
            data = self.get("/markets", params=params)
            for raw in data.get("markets") or []:
                m = self._to_market(raw, series_ticker)
                if m:
                    markets.append(m)
            cursor = data.get("cursor") or ""
            if not cursor or len(markets) >= limit:
                break
        return markets[:limit]

    def get_orderbook(self, ticker: str, depth: int = 20) -> dict[str, Any]:
        return self.get(f"/markets/{ticker}/orderbook", params={"depth": depth})

    def get_balance(self) -> dict[str, Any]:
        return self.get("/portfolio/balance")

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        yes_price: int | None = None,
        no_price: int | None = None,
        order_type: str = "limit",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Place order. Prices are in cents (1-99) for legacy endpoint.

        Uses /portfolio/orders (still widely supported). Prefer dry-run unless
        credentials are configured.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side.lower(),  # "yes" | "no"
            "action": action.lower(),  # "buy" | "sell"
            "count": int(count),
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = int(yes_price)
        if no_price is not None:
            body["no_price"] = int(no_price)
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self.post("/portfolio/orders", json=body)

    @staticmethod
    def _to_market(raw: dict[str, Any], series_hint: str = "") -> KalshiMarket | None:
        ticker = raw.get("ticker") or ""
        close = _parse_dt(raw.get("close_time"))
        if not ticker or not close:
            return None
        # Prefer dollar fields; fall back to cents
        yes_bid = _f(raw.get("yes_bid_dollars"))
        yes_ask = _f(raw.get("yes_ask_dollars"))
        no_bid = _f(raw.get("no_bid_dollars"))
        no_ask = _f(raw.get("no_ask_dollars"))
        if yes_bid == 0 and raw.get("yes_bid") is not None:
            yes_bid = _f(raw.get("yes_bid")) / 100.0
        if yes_ask == 0 and raw.get("yes_ask") is not None:
            yes_ask = _f(raw.get("yes_ask")) / 100.0
        if no_ask == 0:
            no_ask = max(0.0, 1.0 - yes_bid) if yes_bid else _f(raw.get("no_ask"), 0) / 100.0
        if no_bid == 0:
            no_bid = max(0.0, 1.0 - yes_ask) if yes_ask else _f(raw.get("no_bid"), 0) / 100.0

        floor = raw.get("floor_strike")
        floor_f = _f(floor) if floor is not None else None
        if floor_f == 0.0 and floor is None:
            floor_f = None

        return KalshiMarket(
            ticker=ticker,
            event_ticker=str(raw.get("event_ticker") or ""),
            series=series_hint or series_from_ticker(ticker),
            title=str(raw.get("title") or ""),
            status=str(raw.get("status") or ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=_f(raw.get("yes_bid_size_fp") or raw.get("yes_bid_size")),
            yes_ask_size=_f(raw.get("yes_ask_size_fp") or raw.get("yes_ask_size")),
            no_bid_size=_f(raw.get("no_bid_size_fp") or raw.get("no_bid_size")),
            no_ask_size=_f(raw.get("no_ask_size_fp") or raw.get("no_ask_size")),
            floor_strike=floor_f,
            close_time=close,
            open_time=_parse_dt(raw.get("open_time")),
            rules_primary=str(raw.get("rules_primary") or ""),
            strike_type=str(raw.get("strike_type") or ""),
            volume=_f(raw.get("volume_fp") or raw.get("volume")),
        )