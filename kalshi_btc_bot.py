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
import json
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

try:
    from kalshi_bot.dashboard.hour_bot_status import (
        HourBotControl,
        HourBotStatus,
        default_status,
        new_log_id,
    )
except ImportError:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from kalshi_bot.dashboard.hour_bot_status import (
        HourBotControl,
        HourBotStatus,
        default_status,
        new_log_id,
    )

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

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
MIN_TIME_LEFT_MINUTES = float(os.getenv("MIN_TIME_LEFT_MINUTES", "8"))
MAX_TIME_LEFT_MINUTES = float(os.getenv("MAX_TIME_LEFT_MINUTES", "45"))

# Order book imbalance thresholds
IMBALANCE_RATIO_THRESHOLD = float(os.getenv("IMBALANCE_RATIO_THRESHOLD", "2.2"))
IMBALANCE_EXTREME_THRESHOLD = float(os.getenv("IMBALANCE_EXTREME_THRESHOLD", "3.0"))
PRICE_BAND_LOW = int(os.getenv("PRICE_BAND_LOW", "32"))    # cents
PRICE_BAND_HIGH = int(os.getenv("PRICE_BAND_HIGH", "62"))  # cents
DEPTH_LEVELS = int(os.getenv("DEPTH_LEVELS", "3"))
MOMENTUM_DELTA_CENTS = float(os.getenv("MOMENTUM_DELTA_CENTS", "2.0"))
REQUIRE_MOMENTUM = _env_bool("REQUIRE_MOMENTUM", True)
ALLOW_ONE_SIDED_BOOKS = _env_bool("ALLOW_ONE_SIDED_BOOKS", False)
MIN_SIDE_DEPTH = int(os.getenv("MIN_SIDE_DEPTH", "150"))
MAX_MARKETS_PER_SCAN = int(os.getenv("MAX_MARKETS_PER_SCAN", "40"))

# Execution / polling
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "15"))
QUOTE_REFRESH_SECONDS = float(os.getenv("QUOTE_REFRESH_SECONDS", "2"))
ORDER_TYPE = os.getenv("ORDER_TYPE", "limit")  # "limit" or "market"
HOUR_BOT_POLL_MS = int(os.getenv("HOUR_BOT_POLL_MS", "500"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "kalshi_bot.log")
DAILY_ENTRY_BUDGET = int(os.getenv("DAILY_ENTRY_BUDGET", "20"))


@dataclass
class RiskConfig:
    max_contracts_per_trade: int = int(os.getenv("MAX_CONTRACTS_PER_TRADE", "5"))
    max_dollars_per_trade: float = float(os.getenv("MAX_DOLLARS_PER_TRADE", "5.00"))
    daily_loss_limit_dollars: float = float(os.getenv("DAILY_LOSS_LIMIT", "25.00"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "4"))
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
        book = data.get("orderbook") or {}
        yes_levels = book.get("yes") or []
        no_levels = book.get("no") or []
        if not yes_levels and not no_levels:
            fp = data.get("orderbook_fp") or {}
            yes_levels = [
                [int(round(float(price) * 100)), int(float(qty))]
                for price, qty in (fp.get("yes_dollars") or [])
            ]
            no_levels = [
                [int(round(float(price) * 100)), int(float(qty))]
                for price, qty in (fp.get("no_dollars") or [])
            ]
        yes_levels = sorted(yes_levels, key=lambda lvl: -lvl[0])
        no_levels = sorted(no_levels, key=lambda lvl: -lvl[0])
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
        if not hist or len(hist) < 2:
            return None
        delta = hist[-1] - hist[0]
        if delta >= MOMENTUM_DELTA_CENTS:
            return "yes"
        if delta <= -MOMENTUM_DELTA_CENTS:
            return "no"
        return None

    def _evaluate_one_sided(self, ticker: str, yes: list, no: list) -> Signal | None:
        """Trade resting liquidity when Kalshi only returns bids on one side."""
        low, high = PRICE_BAND_LOW, PRICE_BAND_HIGH
        depth_n = DEPTH_LEVELS

        if yes and not no:
            best = yes[0][0]
            depth = sum(lvl[1] for lvl in yes[:depth_n])
            if low <= best <= high and depth >= MIN_SIDE_DEPTH:
                return Signal(
                    ticker=ticker,
                    side="yes",
                    limit_price=min(best + 1, 99),
                    reason=f"one_sided_yes depth={depth} @ {best}\u00a2",
                )

        if no and not yes:
            best = no[0][0]
            depth = sum(lvl[1] for lvl in no[:depth_n])
            if low <= best <= high and depth >= MIN_SIDE_DEPTH:
                return Signal(
                    ticker=ticker,
                    side="no",
                    limit_price=min(best + 1, 99),
                    reason=f"one_sided_no depth={depth} @ {best}\u00a2",
                )

        return None

    def _imbalance_confirmed(self, side: str, ratio: float, momentum: str | None) -> bool:
        if not REQUIRE_MOMENTUM:
            return True
        if ratio >= IMBALANCE_EXTREME_THRESHOLD:
            return True
        return momentum == side

    def evaluate_market(self, ticker: str, orderbook: dict) -> Signal | None:
        yes = orderbook.get("yes") or []
        no = orderbook.get("no") or []
        if not yes and not no:
            return None
        if not yes or not no:
            if ALLOW_ONE_SIDED_BOOKS:
                return self._evaluate_one_sided(ticker, yes, no)
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

        if yes_no_ratio >= threshold and low <= midpoint <= high:
            if self._imbalance_confirmed("yes", yes_no_ratio, momentum):
                return Signal(
                    ticker=ticker, side="yes", limit_price=min(best_yes_bid + 1, 99),
                    reason=f"book_imbalance={yes_no_ratio:.2f}x yes, momentum={momentum}",
                )

        if no_yes_ratio >= threshold and low <= (100 - midpoint) <= high:
            if self._imbalance_confirmed("no", no_yes_ratio, momentum):
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


@dataclass
class DashboardRecorder:
    """Tracks bot state for the 1-hour React dashboard."""

    mode: str
    logs: list[dict] = field(default_factory=list)
    journal: list[dict] = field(default_factory=list)
    equity_history: list[dict] = field(default_factory=list)
    wins_today: int = 0
    losses_today: int = 0
    sum_win_dollars: float = 0.0
    sum_loss_dollars: float = 0.0
    fees_paid_today: float = 0.0
    fees_paid_total: float = 0.0
    cum_pnl_inception: float = 0.0
    pnl_by_side: dict[str, float] = field(default_factory=lambda: {"yes": 0.0, "no": 0.0})
    daily_entries: int = 0
    peak_equity: float = field(default_factory=lambda: RISK.starting_bankroll)
    position_meta: dict[str, dict] = field(default_factory=dict)
    last_publish_ctx: dict = field(default_factory=dict)
    _tick: int = 0

    def add_log(
        self,
        kind: str,
        text: str,
        *,
        ticker: str = "",
        side: str = "",
        price: int | None = None,
        count: int | None = None,
        won: bool | None = None,
        outcome: str = "",
        detail: dict | None = None,
        journal_only: bool = False,
    ) -> None:
        entry = {
            "id": new_log_id(),
            "time": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "text": text,
            "ticker": ticker,
            "side": side,
            "price": price,
            "count": count,
            "won": won,
            "outcome": outcome,
            "detail": detail or {},
        }
        if not journal_only:
            self.logs.insert(0, entry)
            self.logs = self.logs[:100]
        if kind != "scan" or detail:
            self.journal.insert(0, entry)
            self.journal = self.journal[:300]

    def record_entry(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        *,
        expires_at_ms: int,
        strike_btc: int,
        signal_reason: str = "",
        quote: dict | None = None,
    ):
        self.daily_entries += 1
        self.position_meta[ticker] = {
            "expiresAt": expires_at_ms,
            "strikeBtc": strike_btc,
            "entryTime": int(time.time() * 1000),
            "signalReason": signal_reason,
            "entryQuote": quote or {},
        }
        self.add_log(
            "fill",
            f"FILL {ticker} {side} x{count} @ {price_cents}c — {signal_reason}".strip(" —"),
            ticker=ticker,
            side=side,
            price=price_cents,
            count=count,
            detail={"signalReason": signal_reason, "quote": quote or {}},
        )

    def record_settlement(
        self,
        ticker: str,
        side: str,
        pnl: float,
        won: bool,
        *,
        market_result: str = "",
        count: int = 0,
        entry_price: int = 0,
        cost_basis: float = 0.0,
        payout: float = 0.0,
    ):
        fee = 0.01
        self.fees_paid_today += fee
        self.fees_paid_total += fee
        self.cum_pnl_inception += pnl
        self.pnl_by_side[side] = round(self.pnl_by_side.get(side, 0.0) + pnl, 2)
        if won:
            self.wins_today += 1
            self.sum_win_dollars += max(pnl, 0)
        else:
            self.losses_today += 1
            self.sum_loss_dollars += max(-pnl, 0)
        self.position_meta.pop(ticker, None)
        outcome = "WIN" if won else "LOSS"
        self.add_log(
            "settle",
            (
                f"SETTLED {ticker} {side} — {outcome} — "
                f"market settled {market_result or '?'} — pnl {pnl:+.2f}"
            ),
            ticker=ticker,
            side=side,
            price=entry_price or None,
            count=count or None,
            won=won,
            outcome=outcome,
            detail={
                "won": won,
                "outcome": outcome,
                "marketResult": market_result,
                "heldSide": side,
                "pnl": round(pnl, 2),
                "count": count,
                "entryPrice": entry_price,
                "costBasis": round(cost_basis, 2),
                "payout": round(payout, 2),
            },
        )

    @staticmethod
    def _quote_from_book(orderbook: dict | None) -> dict:
        if not orderbook:
            return {
                "yesBid": 0,
                "yesAsk": 0,
                "noBid": 0,
                "noAsk": 0,
                "spread": 0,
                "impliedProb": 0,
                "depthYes": 0,
                "depthNo": 0,
            }
        yes = orderbook.get("yes") or []
        no = orderbook.get("no") or []
        yes_bid = int(yes[0][0]) if yes else 0
        no_bid = int(no[0][0]) if no else 0
        yes_ask = int(100 - no_bid) if no_bid > 0 else 0
        no_ask = int(100 - yes_bid) if yes_bid > 0 else 0
        if yes_bid > 0 and yes_ask <= 0:
            yes_ask = min(99, yes_bid + 2)
        if no_bid > 0 and no_ask <= 0:
            no_ask = min(99, no_bid + 2)
        if yes_bid > 0 and yes_ask > 0 and yes_ask < yes_bid:
            yes_ask = min(99, yes_bid + 1)
        implied = round((yes_bid + yes_ask) / 2, 1) if yes_bid > 0 and yes_ask > 0 else yes_bid or yes_ask
        spread = max(0, yes_ask - yes_bid) if yes_bid > 0 and yes_ask > 0 else 0
        return {
            "yesBid": yes_bid,
            "yesAsk": yes_ask,
            "noBid": no_bid,
            "noAsk": no_ask,
            "spread": spread,
            "impliedProb": implied,
            "depthYes": sum(int(lvl[1]) for lvl in yes[:DEPTH_LEVELS]),
            "depthNo": sum(int(lvl[1]) for lvl in no[:DEPTH_LEVELS]),
        }

    def _market_row(self, market: dict, orderbook: dict | None) -> dict:
        ticker = market.get("ticker", "")
        expiration_time = market_close_time_str(market) or ""
        try:
            expires_at = int(
                datetime.fromisoformat(expiration_time.replace("Z", "+00:00")).timestamp() * 1000
            )
        except Exception:
            expires_at = int(time.time() * 1000) + 3_600_000

        quote = self._quote_from_book(orderbook)
        if quote["yesBid"] <= 0:
            quote["yesBid"] = int(market.get("yes_bid") or 0)
        if quote["yesAsk"] <= 0:
            quote["yesAsk"] = int(market.get("yes_ask") or 0)
        if quote["yesBid"] > 0 and quote["yesAsk"] <= 0:
            quote["yesAsk"] = min(99, quote["yesBid"] + 2)
        if quote["yesBid"] > 0 and quote["yesAsk"] > 0:
            quote["impliedProb"] = round((quote["yesBid"] + quote["yesAsk"]) / 2, 1)
            quote["spread"] = max(0, quote["yesAsk"] - quote["yesBid"])

        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")
        return {
            "ticker": ticker,
            "expiresAt": expires_at,
            "strike": _strike_from_market(market),
            "subtitle": str(market.get("subtitle") or ""),
            "lastPrice": int(market.get("last_price") or 0),
            **quote,
        }

    def _position_rows(
        self,
        risk: RiskManager,
        client: KalshiClient,
        market_books: dict[str, dict],
    ) -> list[dict]:
        rows: list[dict] = []
        now_ms = int(time.time() * 1000)
        for ticker, fill in risk.open_positions.items():
            meta = self.position_meta.get(ticker, {})
            market = client.get_market(ticker)
            book = market_books.get(ticker) or client.get_orderbook(ticker)
            if book:
                market_books[ticker] = book
            quote = self._quote_from_book(book)

            if fill.side == "yes":
                current_mark = quote["yesBid"] or quote["yesAsk"] or fill.price_cents
            else:
                current_mark = quote["noBid"] or quote["noAsk"] or fill.price_cents

            strike = int(meta.get("strikeBtc") or _strike_from_market(market or {}))
            expires_at = int(meta.get("expiresAt") or now_ms + 3_600_000)
            minutes_remaining = max(0.0, (expires_at - now_ms) / 60_000)
            cost_basis = round((fill.price_cents * fill.count) / 100.0, 2)
            market_value = round((current_mark * fill.count) / 100.0, 2)
            unrealized_pnl = round(market_value - cost_basis, 2)

            rows.append(
                {
                    "id": ticker,
                    "ticker": ticker,
                    "side": fill.side,
                    "entryPrice": fill.price_cents,
                    "currentMark": current_mark,
                    "count": fill.count,
                    "entryTime": int(meta.get("entryTime") or fill.timestamp * 1000),
                    "expiresAt": expires_at,
                    "strikeBtc": strike,
                    "floorStrike": market.get("floor_strike") if market else None,
                    "capStrike": market.get("cap_strike") if market else None,
                    "subtitle": str(market.get("subtitle") or "") if market else "",
                    "signalReason": str(meta.get("signalReason") or ""),
                    "entryQuote": meta.get("entryQuote") or {},
                    "minutesRemaining": round(minutes_remaining, 1),
                    "costBasis": cost_basis,
                    "marketValue": market_value,
                    "unrealizedPnl": unrealized_pnl,
                    "markUpdatedAt": datetime.now(timezone.utc).isoformat(),
                    **quote,
                }
            )
        return rows

    def publish(
        self,
        *,
        risk: RiskManager,
        client: KalshiClient,
        markets: list[dict],
        market_books: dict[str, dict],
        control: HourBotControl,
        btc_spot: float,
        current_hour: dict | None = None,
    ) -> None:
        self._tick += 1
        self.last_publish_ctx = {
            "markets": markets,
            "market_books": dict(market_books),
            "btc_spot": btc_spot,
            "current_hour": current_hour or {},
            "control": control,
        }

        position_rows = self._position_rows(risk, client, market_books)
        unrealized = sum(p["unrealizedPnl"] for p in position_rows)
        capital_deployed = sum(p["costBasis"] for p in position_rows)

        net_equity = risk.bankroll + unrealized
        self.peak_equity = max(self.peak_equity, net_equity)
        if self._tick % 2 == 0:
            self.equity_history.append({"t": self._tick, "v": round(net_equity, 2)})
            self.equity_history = self.equity_history[-120:]

        watched_tickers = {m.get("ticker") for m in markets if m.get("ticker")}
        watched_tickers.update(risk.open_positions.keys())
        market_rows = []
        seen: set[str] = set()
        for market in markets:
            ticker = market.get("ticker", "")
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            market_rows.append(self._market_row(market, market_books.get(ticker)))
        for ticker in risk.open_positions:
            if ticker in seen:
                continue
            market = client.get_market(ticker)
            if market:
                market_rows.append(self._market_row(market, market_books.get(ticker)))

        status = HourBotStatus(
            mode=control.mode,
            running=control.running and not control.estop,
            estop=control.estop,
            series=BTC_SERIES_TICKER,
            btcSpot=round(btc_spot, 2),
            bankroll=round(risk.bankroll, 2),
            dayPnl=round(risk.realized_pnl_today, 2),
            unrealized=round(unrealized, 2),
            equityHistory=self.equity_history,
            dailyEntriesUsed=self.daily_entries,
            winsToday=self.wins_today,
            lossesToday=self.losses_today,
            sumWinDollars=round(self.sum_win_dollars, 2),
            sumLossDollars=round(self.sum_loss_dollars, 2),
            feesPaidToday=round(self.fees_paid_today, 2),
            feesPaidTotal=round(self.fees_paid_total, 2),
            cumPnlInception=round(self.cum_pnl_inception, 2),
            pnlBySide=dict(self.pnl_by_side),
            peakEquity=round(self.peak_equity, 2),
            markets=market_rows[:24],
            positions=position_rows,
            logs=self.logs,
            journal=self.journal,
            guardrails={
                "dailyLossLimit": RISK.daily_loss_limit_dollars,
                "maxOpenPositions": RISK.max_open_positions,
                "maxCapitalDeployed": RISK.max_dollars_per_trade * RISK.max_open_positions,
                "dailyEntryBudget": DAILY_ENTRY_BUDGET,
                "openPositionsCount": len(risk.open_positions),
                "capitalDeployed": round(capital_deployed, 2),
            },
            currentHour=current_hour or {},
            pollIntervalMs=HOUR_BOT_POLL_MS,
        )
        status.save()

    def publish_fast_quotes(self, *, risk: RiskManager, client: KalshiClient, control: HourBotControl) -> None:
        if not self.last_publish_ctx:
            return
        ctx = self.last_publish_ctx
        market_books = dict(ctx.get("market_books") or {})
        tickers_to_refresh = list(risk.open_positions.keys())
        for market in (ctx.get("markets") or [])[:12]:
            ticker = market.get("ticker")
            if ticker and ticker not in tickers_to_refresh:
                tickers_to_refresh.append(ticker)
        for ticker in tickers_to_refresh:
            book = client.get_orderbook(ticker)
            if book:
                market_books[ticker] = book
                time.sleep(0.05)
        self.publish(
            risk=risk,
            client=client,
            markets=ctx.get("markets") or [],
            market_books=market_books,
            control=control,
            btc_spot=float(ctx.get("btc_spot") or 0),
            current_hour=ctx.get("current_hour") or {},
        )


# ============================================================================
# MAIN LOOP
# ============================================================================

log_main = logging.getLogger("kalshi.main")


def minutes_to_expiration(expiration_time_str: str) -> float:
    expiry = datetime.fromisoformat(expiration_time_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (expiry - now).total_seconds() / 60.0


def market_close_time_str(market: dict) -> str | None:
    """Hourly contracts settle on close_time, not the series expiration_time."""
    for field in ("close_time", "expected_expiration_time", "expiration_time"):
        value = market.get(field)
        if value:
            return str(value)
    return None


def markets_in_trading_window(markets: list[dict]) -> list[tuple[float, dict]]:
    rows: list[tuple[float, dict]] = []
    for market in markets:
        close_time = market_close_time_str(market)
        if not close_time:
            continue
        try:
            mins_left = minutes_to_expiration(close_time)
        except ValueError:
            continue
        if MIN_TIME_LEFT_MINUTES <= mins_left <= MAX_TIME_LEFT_MINUTES:
            rows.append((mins_left, market))
    rows.sort(key=lambda row: row[0])
    return rows


def current_hour_snapshot(
    tradeable: list[tuple[float, dict]],
    *,
    total_markets: int,
) -> dict:
    if not tradeable:
        return {
            "active": False,
            "message": (
                f"No contracts in the {MIN_TIME_LEFT_MINUTES:.0f}-"
                f"{MAX_TIME_LEFT_MINUTES:.0f} minute trading window"
            ),
            "contractsInWindow": 0,
            "marketsScanned": total_markets,
        }
    mins_left, market = tradeable[0]
    ticker = str(market.get("ticker") or "")
    event = str(market.get("event_ticker") or ticker.rsplit("-", 1)[0])
    close_time = market_close_time_str(market) or ""
    return {
        "active": True,
        "eventTicker": event,
        "sampleTicker": ticker,
        "minutesRemaining": round(mins_left, 1),
        "closeTime": close_time,
        "contractsInWindow": len(tradeable),
        "marketsScanned": total_markets,
        "message": f"{event} · {mins_left:.1f}m to close · {len(tradeable)} strikes in window",
    }


class PaperBroker:
    """Simulates fills against the real order book without sending real orders."""

    def __init__(self, client: KalshiClient):
        self.client = client

    def place_order(self, ticker: str, side: str, count: int, price_cents: int, order_type: str):
        log_main.info("[PAPER] Would submit %s %s x%d @ %d\u00a2 (%s)", ticker, side, count, price_cents, order_type)
        return {"order_id": f"paper-{ticker}-{int(time.time())}", "status": "filled"}


def check_settlements(client: KalshiClient, risk: RiskManager, dashboard: DashboardRecorder | None = None):
    for ticker in list(risk.open_positions.keys()):
        market = client.get_market(ticker)
        if not market:
            continue
        if market.get("status") == "finalized" and market.get("result") in ("yes", "no"):
            held_side = risk.open_positions[ticker].side
            market_result = str(market.get("result"))
            won = held_side == market_result
            fill = risk.open_positions[ticker]
            cost = (fill.price_cents * fill.count) / 100.0
            proceeds = fill.count * 1.0 if won else 0.0
            pnl = proceeds - cost
            risk.record_settlement(ticker, won=won)
            if dashboard:
                dashboard.record_settlement(
                    ticker,
                    held_side,
                    pnl,
                    won,
                    market_result=market_result,
                    count=fill.count,
                    entry_price=fill.price_cents,
                    cost_basis=cost,
                    payout=proceeds,
                )


def _expiration_ms(market: dict) -> int:
    close_time = market_close_time_str(market)
    if not close_time:
        return int(time.time() * 1000) + 3_600_000
    return int(datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp() * 1000)


def _strike_from_market(market: dict) -> int:
    ticker = str(market.get("ticker") or "")
    if "-B" in ticker:
        suffix = ticker.rsplit("-B", 1)[-1]
        if suffix.startswith("T"):
            try:
                return int(round(float(suffix[1:])))
            except ValueError:
                pass
        else:
            try:
                return int(suffix)
            except ValueError:
                pass
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    if floor is not None and cap is not None:
        return int(round((float(floor) + float(cap)) / 2))
    for key in ("floor_strike", "strike", "cap_strike"):
        if market.get(key) is not None:
            return int(market[key])
    return 0


def _event_prefix(market: dict) -> str:
    event = market.get("event_ticker")
    if event:
        return str(event)
    ticker = str(market.get("ticker") or "")
    parts = ticker.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return ticker


def _btc_spot_from_markets(markets: list[dict], *, event_prefix: str = "") -> float:
    """Use the threshold (T) contract from the active hourly event as spot reference."""
    best = 0.0
    for market in markets:
        ticker = str(market.get("ticker") or "")
        if event_prefix and not ticker.startswith(event_prefix):
            continue
        if "-T" not in ticker:
            continue
        try:
            price = float(ticker.rsplit("-T", 1)[-1])
        except ValueError:
            continue
        if price > best:
            best = price
    return best


def sort_tradeable_for_scan(
    tradeable: list[tuple[float, dict]],
    *,
    btc_spot: float,
) -> list[tuple[float, dict]]:
    """Scan the contested OTM band first, then fall back to strikes near spot."""
    if btc_spot <= 0:
        return tradeable

    band_center = btc_spot - 10_000
    band_lo = btc_spot - 12_000
    band_hi = btc_spot - 8_000

    def sort_key(row: tuple[float, dict]) -> tuple[int, float, float]:
        mins_left, market = row
        strike = _strike_from_market(market)
        in_band = band_lo <= strike <= band_hi
        zone = 0 if in_band else 1
        anchor = band_center if in_band else btc_spot
        return (zone, abs(strike - anchor), mins_left)

    return sorted(tradeable, key=sort_key)


def run_cycle(
    client: KalshiClient,
    broker,
    strategy: MarketImbalanceStrategy,
    risk: RiskManager,
    *,
    dashboard: DashboardRecorder | None = None,
    control: HourBotControl | None = None,
) -> None:
    markets = client.get_btc_hourly_markets()
    tradeable = markets_in_trading_window(markets)
    event_prefix = _event_prefix(tradeable[0][1]) if tradeable else ""
    btc_spot = _btc_spot_from_markets(markets, event_prefix=event_prefix)
    scan_queue = sort_tradeable_for_scan(tradeable, btc_spot=btc_spot)[:MAX_MARKETS_PER_SCAN]
    current_hour = current_hour_snapshot(tradeable, total_markets=len(markets))
    display_markets = [m for _, m in tradeable[:12]] or markets[:12]
    market_books: dict[str, dict] = {}

    for market in display_markets:
        ticker = market.get("ticker")
        if not ticker:
            continue
        book = client.get_orderbook(ticker)
        if book:
            market_books[ticker] = book
        if not btc_spot:
            strike = _strike_from_market(market)
            if strike:
                btc_spot = float(strike)
        time.sleep(0.15)

    if dashboard:
        dashboard.publish(
            risk=risk,
            client=client,
            markets=display_markets,
            market_books=market_books,
            control=control or HourBotControl(),
            btc_spot=btc_spot,
            current_hour=current_hour,
        )

    if control and (control.estop or not control.running):
        if dashboard:
            dashboard.add_log("reject", "Scanning paused — bot halted by dashboard control")
            dashboard.publish(
                risk=risk,
                client=client,
                markets=display_markets,
                market_books=market_books,
                control=control,
                btc_spot=btc_spot,
                current_hour=current_hour,
            )
        return

    if not tradeable:
        if dashboard:
            dashboard.add_log(
                "scan",
                f"No markets in {MIN_TIME_LEFT_MINUTES:.0f}-{MAX_TIME_LEFT_MINUTES:.0f}m window "
                f"(checked {len(markets)} contracts)",
            )
        log_main.debug("No markets in trading window this cycle.")
        if dashboard:
            dashboard.publish(
                risk=risk,
                client=client,
                markets=display_markets,
                market_books=market_books,
                control=control or HourBotControl(),
                btc_spot=btc_spot,
                current_hour=current_hour,
            )
        return

    for mins_left, market in scan_queue:
        ticker = market.get("ticker")
        if not ticker:
            continue

        book = market_books.get(ticker) or client.get_orderbook(ticker)
        if book:
            yes = book.get("yes") or []
            no = book.get("no") or []
            depth_yes = sum(int(lvl[1]) for lvl in yes[:DEPTH_LEVELS]) if yes else 0
            depth_no = sum(int(lvl[1]) for lvl in no[:DEPTH_LEVELS]) if no else 0
            if dashboard:
                dashboard.add_log(
                    "scan",
                    f"scanned {ticker} — book yes:{depth_yes} no:{depth_no}, "
                    f"{mins_left:.1f}m left",
                )

        if mins_left <= 0:
            continue

        if not book:
            continue

        signal = strategy.evaluate_market(ticker, book)
        if not signal:
            continue

        size = risk.size_trade(RISK.max_contracts_per_trade, signal.limit_price)
        if size <= 0:
            continue

        approved, reason = risk.approve_trade(signal.ticker, size, signal.limit_price)
        if not approved:
            log_main.info("Signal on %s rejected by risk manager: %s", signal.ticker, reason)
            if dashboard:
                dashboard.add_log(
                    "reject",
                    f"{signal.ticker} rejected by risk manager — {reason}",
                    ticker=signal.ticker,
                    side=signal.side,
                    price=signal.limit_price,
                    detail={"reason": reason},
                )
            continue

        log_main.info(
            "SIGNAL %s side=%s price=%d\u00a2 size=%d | %s | %.1fm to expiry",
            signal.ticker, signal.side, signal.limit_price, size, signal.reason, mins_left,
        )
        if dashboard:
            dashboard.add_log(
                "signal",
                f"SIGNAL {signal.ticker} side={signal.side} price={signal.limit_price}c | {signal.reason}",
                ticker=signal.ticker,
                side=signal.side,
                price=signal.limit_price,
                count=size,
                detail={"reason": signal.reason, "minutesLeft": round(mins_left, 1)},
            )

        order_res = broker.place_order(signal.ticker, signal.side, size, signal.limit_price, ORDER_TYPE)
        if order_res:
            risk.record_fill(signal.ticker, signal.side, size, signal.limit_price)
            if dashboard:
                entry_quote = dashboard._quote_from_book(book)
                dashboard.record_entry(
                    signal.ticker,
                    signal.side,
                    size,
                    signal.limit_price,
                    expires_at_ms=_expiration_ms(market),
                    strike_btc=_strike_from_market(market),
                    signal_reason=signal.reason,
                    quote=entry_quote,
                )
            log_main.info(risk.status_line())
            break

    if dashboard:
        dashboard.publish(
            risk=risk,
            client=client,
            markets=display_markets,
            market_books=market_books,
            control=control or HourBotControl(),
            btc_spot=btc_spot,
            current_hour=current_hour,
        )


def main():
    _require_credentials()
    control = HourBotControl.load()
    control.mode = TRADING_MODE
    control.save()

    mode = TRADING_MODE
    if mode == "live":
        log_main.warning("LIVE MODE — real orders will be sent to Kalshi (max $%.2f/trade).", RISK.max_dollars_per_trade)
    log_main.info("Starting Kalshi hourly-BTC bot in %s mode.", mode.upper())
    log_main.info(
        "Risk config: max_contracts=%d max_$=%.2f daily_loss_limit=%.2f cooldown=%ds max_open=%d",
        RISK.max_contracts_per_trade, RISK.max_dollars_per_trade,
        RISK.daily_loss_limit_dollars, RISK.cooldown_seconds_after_trade, RISK.max_open_positions,
    )

    client = KalshiClient()
    strategy = MarketImbalanceStrategy()
    risk = RiskManager()
    dashboard = DashboardRecorder(mode=mode)
    dashboard.add_log("scan", f"Bot started in {mode.upper()} mode")

    broker = PaperBroker(client) if mode == "paper" else client

    try:
        while True:
            control = HourBotControl.load()
            check_settlements(client, risk, dashboard)
            run_cycle(client, broker, strategy, risk, dashboard=dashboard, control=control)

            next_scan = time.time() + POLL_INTERVAL_SECONDS
            while time.time() < next_scan:
                control = HourBotControl.load()
                check_settlements(client, risk, dashboard)
                if dashboard and risk.open_positions:
                    dashboard.publish_fast_quotes(risk=risk, client=client, control=control)
                sleep_for = min(QUOTE_REFRESH_SECONDS, max(0.1, next_scan - time.time()))
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        log_main.info("Shutting down. Final status: %s", risk.status_line())


if __name__ == "__main__":
    main()
