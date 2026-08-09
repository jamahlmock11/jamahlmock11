"""Kalshi REST client — RSA-PSS auth, markets, orderbook, orders."""

from __future__ import annotations

import base64
import logging
import re
import subprocess
import tempfile
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

_CLIENT_ORDER_ID_RE = re.compile(r"[^A-Za-z0-9-]+")


def sanitize_client_order_id(client_order_id: str) -> str:
    """Kalshi allows only [A-Za-z0-9-] in client_order_id (no dots in KXBTCD tickers)."""
    cleaned = _CLIENT_ORDER_ID_RE.sub("-", client_order_id).strip("-")
    if not cleaned:
        import uuid as _uuid

        return str(_uuid.uuid4())
    return cleaned


def _sign_pss_openssl(private_key_path: str, message: bytes) -> bytes:
    """RSA-PSS SHA256 sign via openssl (fallback when cryptography rejects PEM)."""
    with tempfile.NamedTemporaryFile(delete=False) as msg_f:
        msg_f.write(message)
        msg_path = msg_f.name
    try:
        proc = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sigopt",
                "rsa_padding_mode:pss",
                "-sigopt",
                "rsa_pss_saltlen:digest",
                "-sign",
                private_key_path,
                msg_path,
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode()[:300] or "openssl sign failed")
        return proc.stdout
    finally:
        Path(msg_path).unlink(missing_ok=True)


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
    market_type: str = ""

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
        self._private_key_path = private_key_path if private_key_path and Path(private_key_path).exists() else ""
        self._sign_backend = "none"
        if self._private_key_path:
            try:
                pem = Path(self._private_key_path).read_bytes()
                self._private_key = serialization.load_pem_private_key(pem, password=None)
                self._sign_backend = "cryptography"
            except ValueError as exc:
                # Some Kalshi-exported PEMs fail cryptography's multiprime checks
                # but still sign correctly via openssl (and Kalshi accepts them).
                logger.warning(
                    "cryptography rejected Kalshi PEM (%s); using openssl signing backend",
                    exc,
                )
                self._sign_backend = "openssl"
        self._http = httpx.Client(timeout=timeout)

    @property
    def authenticated(self) -> bool:
        return bool(self.api_key_id and self._sign_backend in ("cryptography", "openssl"))

    def close(self) -> None:
        self._http.close()

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if self._sign_backend == "none":
            raise RuntimeError("Kalshi private key not loaded")
        # Sign path without query string
        path = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        if self._sign_backend == "openssl":
            sig = _sign_pss_openssl(self._private_key_path, message)
        else:
            assert self._private_key is not None
            sig = self._private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
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
        if resp.status_code >= 400:
            detail = resp.text[:500]
            logger.error("Kalshi %s %s → %s %s", method, endpoint, resp.status_code, detail)
            resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def get(self, endpoint: str, params: dict | None = None) -> Any:
        return self.request("GET", endpoint, params=params)

    def post(self, endpoint: str, json: dict | None = None) -> Any:
        return self.request("POST", endpoint, json=json)

    def delete(self, endpoint: str) -> Any:
        return self.request("DELETE", endpoint)

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

    def get_positions(self, limit: int = 200) -> list[dict[str, Any]]:
        data = self.get("/portfolio/positions", params={"limit": limit})
        return list(data.get("market_positions") or [])

    def get_open_orders(self, ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "resting", "limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = self.get("/portfolio/orders", params=params)
        return list(data.get("orders") or [])

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
        time_in_force: str = "immediate_or_cancel",
    ) -> dict[str, Any]:
        """Place an idempotent buy or sell via CreateOrderV2.

        V2 quotes the YES book only:
          - buy YES  → side=bid at yes price
          - buy NO   → side=ask at (1 - no_price)  [sell YES ≡ buy NO]
          - sell YES → side=ask at yes price
          - sell NO  → side=bid at (1 - no_price)
        Prices args are integer cents (1-99).
        """
        import uuid as _uuid

        side_l = side.lower()
        action_l = action.lower()
        if action_l not in {"buy", "sell"}:
            raise ValueError("action must be buy or sell")

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
            "client_order_id": sanitize_client_order_id(
                client_order_id or str(_uuid.uuid4())
            ),
            "exchange_index": -1,
        }
        return self.post("/portfolio/events/orders", json=body)

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
            market_type=str(raw.get("market_type") or raw.get("type") or ""),
        )