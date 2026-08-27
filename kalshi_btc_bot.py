"""
kalshi_btc_bot.py — Single-file Kalshi hourly-BTC direction bot.

Everything in one script: config/env loading, the Kalshi REST client with
RSA-PSS request signing, the order-book-imbalance + midpoint-momentum
strategy, risk management (position sizing, daily stop-loss, cooldowns), and
the main execution loop with paper/live modes.

IMPORTANT — verify endpoints before running live:
Kalshi's production hostnames and exact RSA-PSS signing recipe are the
things most likely to have drifted since this was written. Confirm
KALSHI_REST_BASE_URL / KALSHI_WS_BASE_URL and KalshiAuth.headers() against
the current docs at https://trading-api.readme.io/reference before trusting
this against a real account.

Setup:
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt        # requests, cryptography, python-dotenv, websockets
    cp .env.example .env                   # fill in KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH
    python kalshi_btc_bot.py                # KALSHI_TRADING_MODE=paper by default

This is not financial advice and comes with no performance guarantee.
Paper-trade before risking real money.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIG
# ============================================================================

TRADING_MODE = os.getenv("KALSHI_TRADING_MODE", "paper").strip().lower()
assert TRADING_MODE in ("paper", "live"), "KALSHI_TRADING_MODE must be 'paper' or 'live'"

# Overridable via env so a wrong default here can't silently break execution.
KALSHI_REST_BASE_URL = os.getenv(
    "KALSHI_REST_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
KALSHI_WS_BASE_URL = os.getenv(
    "KALSHI_WS_BASE_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"
)

# Kalshi v2 uses an API Key ID + RSA private key (PEM), not a key/secret pair.
# KALSHI_API_SECRET is accepted as an alias for the PEM contents so the
# originally-requested env var names still work.
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY") or os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_API_SECRET") or os.getenv("KALSHI_PRIVATE_KEY_PEM")


def _require_credentials() -> None:
    if not KALSHI_API_KEY_ID:
        raise RuntimeError("Set KALSHI_API_KEY (your Kalshi API Key ID) in the environment.")
    if not (KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM):
        raise RuntimeError(
            "Set either KALSHI_PRIVATE_KEY_PATH (path to your RSA private key .pem) "
            "or KALSHI_API_SECRET / KALSHI_PRIVATE_KEY_PEM (the PEM contents itself)."
        )

# Confirm this series ticker matches the actual hourly BTC product in the
# Kalshi UI/API before relying on it.
BTC_SERIES_TICKER = os.getenv("KALSHI_BTC_SERIES_TICKER", "KXBTC")

# Strategy window (time-to-expiration gate)
MIN_TIME_LEFT_MINUTES = float(os.getenv("MIN_TIME_LEFT_MINUTES", "10"))
MAX_TIME_LEFT_MINUTES = float(os.getenv("MAX_TIME_LEFT_MINUTES", "50"))

# Order book imbalance thresholds
IMBALANCE_RATIO_THRESHOLD = float(os.getenv("IMBALANCE_RATIO_THRESHOLD", "3.0"))
PRICE_BAND_LOW = int(os.getenv("PRICE_BAND_LOW", "40"))    # cents
PRICE_BAND_HIGH = int(os.getenv("PRICE_BAND_HIGH", "55"))  # cents
DEPTH_LEVELS = int(os.getenv("DEPTH_LEVELS", "3"))

# Execution / polling
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "15"))
ORDER_TYPE = os.getenv("ORDER_TYPE", "limit")  # "limit" or "market"

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "kalshi_bot.log")


@dataclass
class RiskConfig:
    max_contracts_per_trade: int = int(os.getenv("MAX_CONTRACTS_PER_TRADE", "5"))
    max_dollars_per_trade: float = float(os.getenv("MAX_DOLLARS_PER_TRADE", "5.00"))
    daily_loss_limit_dollars: float = float(os.getenv("DAILY_LOSS_LIMIT", "25.00"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    cooldown_seconds_after_trade: int = int(os.getenv("COOLDOWN_SECONDS", "120"))
    starting_bankroll: float = float(os.getenv("STARTING_BANKROLL", "100.00"))


RISK = RiskConfig()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE)],
)


# ============================================================================
# API CLIENT
# ============================================================================

log_api = logging.getLogger("kalshi.api")


class KalshiAuth:
    """Builds the three signed headers Kalshi requires on every private request."""

    def __init__(self, key_id: str, private_key_path: Optional[str], private_key_pem: Optional[str]):
        self.key_id = key_id
        if private_key_path:
            with open(private_key_path, "rb") as f:
                pem_bytes = f.read()
        elif private_key_pem:
            pem_bytes = private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem
        else:
            raise RuntimeError("No private key provided to KalshiAuth")

        self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)

    def headers(self, method: str, path_with_prefix: str) -> dict:
        """
        path_with_prefix must include the /trade-api/v2 prefix and MUST NOT
        include the query string, per Kalshi's signing spec.
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path_with_prefix}".encode("utf-8")

        signature = self.private_key.sign(
            message,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }


class KalshiClient:
    def __init__(self):
        self.base_url = KALSHI_REST_BASE_URL.rstrip("/")
        self._api_prefix = urlparse(self.base_url).path.rstrip("/")

        self.auth = KalshiAuth(KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_PRIVATE_KEY_PEM)
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body: dict | None = None, authed: bool = True) -> Optional[dict]:
        full_path_for_signing = f"{self._api_prefix}{path}"
        url = f"{self.base_url}{path}"

        headers = {}
        if authed:
            headers = self.auth.headers(method, full_path_for_signing)

        try:
            resp = self.session.request(
                method, url, params=params, json=json_body, headers=headers, timeout=10
            )
        except requests.RequestException as e:
            log_api.error("Network error on %s %s: %s", method, path, e)
            return None

        if resp.status_code >= 400:
            log_api.error("Kalshi API error %s on %s %s: %s", resp.status_code, method, path, resp.text[:500])
            return None

        try:
            return resp.json()
        except ValueError:
            log_api.error("Non-JSON response from %s %s", method, path)
            return None

    # -- market data -----------------------------------------------------

    def get_events_for_series(self, series_ticker: str, status: str = "open") -> list[dict]:
        data = self._request(
            "GET", "/events", params={"series_ticker": series_ticker, "status": status, "limit": 100}
        )
        return (data or {}).get("events", [])

    def get_markets_for_event(self, event_ticker: str) -> list[dict]:
        data = self._request("GET", "/markets", params={"event_ticker": event_ticker})
        return (data or {}).get("markets", [])

    def get_btc_hourly_markets(self) -> list[dict]:
        markets: list[dict] = []
        for event in self.get_events_for_series(BTC_SERIES_TICKER, status="open"):
            event_ticker = event.get("event_ticker")
            if not event_ticker:
                continue
            markets.extend(self.get_markets_for_event(event_ticker))
        return markets

    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[dict]:
        data = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        if not data:
            return None
        book = data.get("orderbook", {})
        yes_levels = sorted(book.get("yes") or [], key=lambda lvl: -lvl[0])
        no_levels = sorted(book.get("no") or [], key=lambda lvl: -lvl[0])
        return {"yes": yes_levels, "no": no_levels}

    def get_market(self, ticker: str) -> Optional[dict]:
        data = self._request("GET", f"/markets/{ticker}")
        return (data or {}).get("market")

    def get_balance(self) -> Optional[float]:
        data = self._request("GET", "/portfolio/balance")
        if not data:
            return None
        cents = data.get("balance")
        return None if cents is None else cents / 100.0

    def get_positions(self) -> list[dict]:
        data = self._request("GET", "/portfolio/positions")
        return (data or {}).get("market_positions", [])

    def get_fills(self, ticker: Optional[str] = None, limit: int = 50) -> list[dict]:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = self._request("GET", "/portfolio/fills", params=params)
        return (data or {}).get("fills", [])

    def place_order(self, ticker: str, side: str, count: int, price_cents: int,
                     order_type: str = "limit", action: str = "buy") -> Optional[dict]:
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": action,
            "side": side,
            "type": order_type,
            "count": count,
        }
        if order_type == "limit":
            if side == "yes":
                body["yes_price"] = price_cents
            else:
                body["no_price"] = price_cents

        data = self._request("POST", "/portfolio/orders", json_body=body)
        if not data:
            return None
        return data.get("order", data)

    def cancel_order(self, order_id: str) -> bool:
        return self._request("DELETE", f"/portfolio/orders/{order_id}") is not None


# ============================================================================
# STRATEGY
# ============================================================================

log_strategy = logging.getLogger("kalshi.strategy")


@dataclass
class Signal:
    ticker: str
    side: str          # "yes" or "no"
    limit_price: int   # cents
    reason: str


class MarketImbalanceStrategy:
    """
    Combines order-book imbalance (heavy resting size on one side while price
    is still near a coin-flip) with midpoint momentum (recent drift direction)
    for confirmation. An extreme imbalance (>= threshold + 1.0) can act alone.
    """

    def __init__(self, midpoint_history_len: int = 6):
        self._midpoint_history: dict[str, deque] = {}
        self._history_len = midpoint_history_len

    @staticmethod
    def _midpoint(orderbook: dict) -> float | None:
        yes = orderbook.get("yes") or []
        no = orderbook.get("no") or []
        if not yes or not no:
            return None
        best_yes_bid = yes[0][0]
        best_yes_ask = 100 - no[0][0]
        if best_yes_ask <= best_yes_bid:
            return float(best_yes_bid)
        return (best_yes_bid + best_yes_ask) / 2.0

    def _record_midpoint(self, ticker: str, midpoint: float) -> None:
        hist = self._midpoint_history.setdefault(ticker, deque(maxlen=self._history_len))
        hist.append(midpoint)

    def _momentum_direction(self, ticker: str) -> str | None:
        hist = self._midpoint_history.get(ticker)
        if not hist or len(hist) < 3:
            return None
        delta = hist[-1] - hist[0]
        if delta >= 3:
            return "yes"
        if delta <= -3:
            return "no"
        return None

    def evaluate_market(self, ticker: str, orderbook: dict) -> Signal | None:
        yes = orderbook.get("yes") or []
        no = orderbook.get("no") or []
        if not yes or not no:
            return None

        midpoint = self._midpoint(orderbook)
        if midpoint is None:
            return None
        self._record_midpoint(ticker, midpoint)

        best_yes_bid = yes[0][0]
        best_no_bid = no[0][0]

        depth_n = DEPTH_LEVELS
        yes_depth = sum(lvl[1] for lvl in yes[:depth_n])
        no_depth = sum(lvl[1] for lvl in no[:depth_n])
        if yes_depth == 0 or no_depth == 0:
            return None

        yes_no_ratio = yes_depth / no_depth
        no_yes_ratio = no_depth / yes_depth
        momentum = self._momentum_direction(ticker)

        low, high = PRICE_BAND_LOW, PRICE_BAND_HIGH
        threshold = IMBALANCE_RATIO_THRESHOLD

        if yes_no_ratio >= threshold and low <= best_yes_bid <= high:
            if momentum == "yes" or yes_no_ratio >= threshold + 1.0:
                return Signal(
                    ticker=ticker, side="yes", limit_price=min(best_yes_bid + 1, 99),
                    reason=f"book_imbalance={yes_no_ratio:.2f}x yes, momentum={momentum}",
                )

        if no_yes_ratio >= threshold and low <= best_no_bid <= high:
            if momentum == "no" or no_yes_ratio >= threshold + 1.0:
                return Signal(
                    ticker=ticker, side="no", limit_price=min(best_no_bid + 1, 99),
                    reason=f"book_imbalance={no_yes_ratio:.2f}x no, momentum={momentum}",
                )

        return None


# ============================================================================
# RISK MANAGEMENT
# ============================================================================

log_risk = logging.getLogger("kalshi.risk")


@dataclass
class Fill:
    ticker: str
    side: str
    count: int
    price_cents: int
    timestamp: float


@dataclass
class RiskManager:
    starting_bankroll: float = field(default_factory=lambda: RISK.starting_bankroll)
    bankroll: float = field(init=False)
    realized_pnl_today: float = 0.0
    open_positions: dict = field(default_factory=dict)  # ticker -> Fill
    last_trade_time: float = 0.0
    trade_day: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def __post_init__(self):
        self.bankroll = self.starting_bankroll

    def _roll_day_if_needed(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.trade_day:
            log_risk.info(
                "New trading day. Yesterday's realized P&L: $%.2f. Resetting daily loss counter.",
                self.realized_pnl_today,
            )
            self.trade_day = today
            self.realized_pnl_today = 0.0

    def approve_trade(self, ticker: str, count: int, price_cents: int) -> tuple[bool, str]:
        self._roll_day_if_needed()
        cost = (count * price_cents) / 100.0

        if self.realized_pnl_today <= -RISK.daily_loss_limit_dollars:
            return False, (
                f"Daily loss limit hit (${self.realized_pnl_today:.2f} <= "
                f"-${RISK.daily_loss_limit_dollars:.2f}). No more trades today."
            )
        if ticker in self.open_positions:
            return False, f"Already holding a position in {ticker}; skipping to avoid stacking."
        if len(self.open_positions) >= RISK.max_open_positions:
            return False, f"Max open positions ({RISK.max_open_positions}) reached."

        elapsed = time.time() - self.last_trade_time
        if elapsed < RISK.cooldown_seconds_after_trade:
            return False, f"Cooldown active, {RISK.cooldown_seconds_after_trade - elapsed:.0f}s remaining."
        if count > RISK.max_contracts_per_trade:
            return False, f"Requested count {count} exceeds max_contracts_per_trade."
        if cost > RISK.max_dollars_per_trade:
            return False, f"Trade cost ${cost:.2f} exceeds max_dollars_per_trade ${RISK.max_dollars_per_trade:.2f}."
        if cost > self.bankroll:
            return False, f"Trade cost ${cost:.2f} exceeds available bankroll ${self.bankroll:.2f}."

        return True, "ok"

    def size_trade(self, requested_count: int, price_cents: int) -> int:
        max_by_contracts = RISK.max_contracts_per_trade
        max_by_dollars = int((RISK.max_dollars_per_trade * 100) // max(price_cents, 1))
        max_by_bankroll = int((self.bankroll * 100) // max(price_cents, 1))
        return max(0, min(requested_count, max_by_contracts, max_by_dollars, max_by_bankroll))

    def record_fill(self, ticker: str, side: str, count: int, price_cents: int):
        cost = (count * price_cents) / 100.0
        self.bankroll -= cost
        self.open_positions[ticker] = Fill(
            ticker=ticker, side=side, count=count, price_cents=price_cents, timestamp=time.time()
        )
        self.last_trade_time = time.time()
        log_risk.info(
            "FILL %s %s x%d @ %d\u00a2 | cost=$%.2f | bankroll=$%.2f",
            ticker, side, count, price_cents, cost, self.bankroll,
        )

    def record_settlement(self, ticker: str, won: bool, payout_per_contract_cents: int = 100):
        pos = self.open_positions.pop(ticker, None)
        if pos is None:
            log_risk.warning("Settlement received for %s but no open position was tracked.", ticker)
            return
        proceeds = (pos.count * payout_per_contract_cents / 100.0) if won else 0.0
        cost = (pos.count * pos.price_cents) / 100.0
        pnl = proceeds - cost
        self.bankroll += proceeds
        self.realized_pnl_today += pnl
        log_risk.info(
            "SETTLED %s %s | %s | pnl=$%.2f | bankroll=$%.2f | day_pnl=$%.2f",
            ticker, pos.side, "WON" if won else "LOST", pnl, self.bankroll, self.realized_pnl_today,
        )

    def status_line(self) -> str:
        return (
            f"bankroll=${self.bankroll:.2f} | day_pnl=${self.realized_pnl_today:.2f} "
            f"| open_positions={len(self.open_positions)}"
        )


# ============================================================================
# MAIN LOOP
# ============================================================================

log_main = logging.getLogger("kalshi.main")


def minutes_to_expiration(expiration_time_str: str) -> float:
    expiry = datetime.fromisoformat(expiration_time_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (expiry - now).total_seconds() / 60.0


class PaperBroker:
    """Simulates fills against the real order book without sending real orders."""

    def __init__(self, client: KalshiClient):
        self.client = client

    def place_order(self, ticker: str, side: str, count: int, price_cents: int, order_type: str):
        log_main.info("[PAPER] Would submit %s %s x%d @ %d\u00a2 (%s)", ticker, side, count, price_cents, order_type)
        return {"order_id": f"paper-{ticker}-{int(time.time())}", "status": "filled"}


def check_settlements(client: KalshiClient, risk: RiskManager):
    for ticker in list(risk.open_positions.keys()):
        market = client.get_market(ticker)
        if not market:
            continue
        if market.get("status") == "finalized" and market.get("result") in ("yes", "no"):
            held_side = risk.open_positions[ticker].side
            won = held_side == market.get("result")
            risk.record_settlement(ticker, won=won)


def run_cycle(client: KalshiClient, broker, strategy: MarketImbalanceStrategy, risk: RiskManager):
    markets = client.get_btc_hourly_markets()
    if not markets:
        log_main.debug("No open BTC hourly markets found this cycle.")
        return

    for market in markets:
        ticker = market.get("ticker")
        expiration_time = market.get("expiration_time")
        if not ticker or not expiration_time:
            continue

        try:
            mins_left = minutes_to_expiration(expiration_time)
        except ValueError:
            log_main.warning("Could not parse expiration_time for %s: %r", ticker, expiration_time)
            continue

        if mins_left <= 0:
            continue
        if not (MIN_TIME_LEFT_MINUTES <= mins_left <= MAX_TIME_LEFT_MINUTES):
            continue

        orderbook = client.get_orderbook(ticker)
        if not orderbook:
            continue

        signal = strategy.evaluate_market(ticker, orderbook)
        if not signal:
            continue

        size = risk.size_trade(RISK.max_contracts_per_trade, signal.limit_price)
        if size <= 0:
            continue

        approved, reason = risk.approve_trade(signal.ticker, size, signal.limit_price)
        if not approved:
            log_main.info("Signal on %s rejected by risk manager: %s", signal.ticker, reason)
            continue

        log_main.info(
            "SIGNAL %s side=%s price=%d\u00a2 size=%d | %s | %.1fm to expiry",
            signal.ticker, signal.side, signal.limit_price, size, signal.reason, mins_left,
        )

        order_res = broker.place_order(signal.ticker, signal.side, size, signal.limit_price, ORDER_TYPE)
        if order_res:
            risk.record_fill(signal.ticker, signal.side, size, signal.limit_price)
            log_main.info(risk.status_line())
            # One trade per cycle: keeps things simple and avoids stacking new
            # positions faster than the cooldown allows.
            break


def main():
    _require_credentials()
    log_main.info("Starting Kalshi hourly-BTC bot in %s mode.", TRADING_MODE.upper())
    log_main.info(
        "Risk config: max_contracts=%d max_$=%.2f daily_loss_limit=%.2f cooldown=%ds max_open=%d",
        RISK.max_contracts_per_trade, RISK.max_dollars_per_trade,
        RISK.daily_loss_limit_dollars, RISK.cooldown_seconds_after_trade, RISK.max_open_positions,
    )

    client = KalshiClient()
    strategy = MarketImbalanceStrategy()
    risk = RiskManager()

    broker = PaperBroker(client) if TRADING_MODE == "paper" else client

    try:
        while True:
            check_settlements(client, risk)
            run_cycle(client, broker, strategy, risk)
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log_main.info("Shutting down. Final status: %s", risk.status_line())


if __name__ == "__main__":
    main()
