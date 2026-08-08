from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    PASS = "PASS"


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True)
class BookQuote:
    yes_bid: float
    yes_ask: float
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0

    @property
    def mid(self) -> Optional[float]:
        if self.yes_bid <= 0 and self.yes_ask <= 0:
            return None
        if self.yes_bid <= 0:
            return self.yes_ask
        if self.yes_ask <= 0 or self.yes_ask >= 1.0 and self.yes_bid <= 0:
            return self.yes_bid
        if self.yes_ask < self.yes_bid:
            return None
        return 0.5 * (self.yes_bid + self.yes_ask)

    @property
    def spread_cents(self) -> Optional[float]:
        if self.yes_bid <= 0 or self.yes_ask <= 0:
            return None
        return (self.yes_ask - self.yes_bid) * 100.0

    @property
    def no_ask(self) -> float:
        # Binary complementary ask for NO ≈ 1 - yes_bid
        if self.yes_bid <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 - self.yes_bid))


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    status: str
    close_time: datetime
    floor_strike: Optional[float]
    strike_type: str
    book: BookQuote
    rules_primary: str = ""
    volume: float = 0.0
    open_interest: float = 0.0

    @property
    def is_updown(self) -> bool:
        return self.series_ticker.startswith("KXBTC15M")

    @property
    def seconds_to_close(self) -> float:
        now = datetime.now(timezone.utc)
        close = self.close_time
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        return max(0.0, (close - now).total_seconds())

    @property
    def years_to_close(self) -> float:
        return self.seconds_to_close / (365.25 * 24 * 3600)


@dataclass(frozen=True)
class SmilePoint:
    moneyness: float
    iv: float


@dataclass
class VolSmile:
    underlying: str
    spot: float
    points: list[SmilePoint]
    tenor_days: float = 30.0
    asof: Optional[str] = None

    def iv_at_moneyness(self, moneyness: float) -> float:
        if not self.points:
            raise ValueError("empty smile")
        pts = sorted(self.points, key=lambda p: p.moneyness)
        if moneyness <= pts[0].moneyness:
            return pts[0].iv
        if moneyness >= pts[-1].moneyness:
            return pts[-1].iv
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            if a.moneyness <= moneyness <= b.moneyness:
                w = (moneyness - a.moneyness) / (b.moneyness - a.moneyness)
                return a.iv + w * (b.iv - a.iv)
        return pts[-1].iv

    def iv_at_strike(self, strike: float) -> float:
        if self.spot <= 0:
            raise ValueError("invalid smile spot")
        return self.iv_at_moneyness(strike / self.spot)


@dataclass
class EdgeSignal:
    market_ticker: str
    series: str
    kalshi_mid: float
    options_prob_yes: float
    edge_pp: float
    confidence: Confidence
    side: Side
    strike_btc: Optional[float]
    btc_spot: float
    iv_used: float
    spread_cents: Optional[float]
    reason: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CrossVenueArb:
    kalshi_ticker: str
    polymarket_id: str
    kalshi_side: str
    poly_side: str
    kalshi_ask: float
    poly_ask: float
    combined_ask: float
    edge_usd: float
    end_time_delta_seconds: float
    risk_note: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TradeIntent:
    market_ticker: str
    side: Side
    contracts: int
    limit_price: float
    confidence: Confidence
    edge_pp: float
    strategy: str
    paper: bool = True
