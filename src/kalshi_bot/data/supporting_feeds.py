"""Public corroborating BTC/USD feeds; none are settlement benchmarks."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from kalshi_bot.domain import SupportingAggregate, SupportingQuote, utc_datetime


class SupportingFeedError(RuntimeError):
    pass


class InsufficientSupportingFeeds(SupportingFeedError):
    pass


def _timestamp(value: Any, fallback: datetime) -> datetime:
    if value in (None, ""):
        return fallback
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 100_000_000_000_000:
            seconds /= 1_000_000.0
        elif seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return utc_datetime(parsed)


def _positive(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SupportingFeedError(f"{label} is missing or malformed") from exc
    if not math.isfinite(result) or result <= 0:
        raise SupportingFeedError(f"{label} must be positive and finite")
    return result


class PublicBTCUSDFeed:
    source = ""
    endpoint = ""

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 8.0,
        max_age_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self.max_age = timedelta(seconds=max_age_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _parse(self, payload: Mapping[str, Any], received_at: datetime) -> tuple[float, float, float, datetime]:
        raise NotImplementedError

    def get_quote(self, *, now: datetime | None = None) -> SupportingQuote:
        observed_now = utc_datetime(now or self._clock())
        try:
            response = self._http.get(self.endpoint, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise SupportingFeedError("response is not a JSON object")
            price, bid, ask, timestamp = self._parse(payload, observed_now)
        except (httpx.HTTPError, ValueError, KeyError, SupportingFeedError) as exc:
            raise SupportingFeedError(f"{self.source} quote failed: {exc}") from exc
        if bid > ask:
            raise SupportingFeedError(f"{self.source} returned bid above ask")
        age = observed_now - timestamp
        healthy = -timedelta(seconds=5) <= age <= self.max_age
        return SupportingQuote(
            price=price,
            timestamp=timestamp,
            source=self.source,
            bid=bid,
            ask=ask,
            healthy=healthy,
            primary=False,
            error=None if healthy else f"quote age {age.total_seconds():.3f}s",
        )

    quote = get_quote


class CoinbaseFeed(PublicBTCUSDFeed):
    source = "Coinbase BTC-USD"
    endpoint = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

    def _parse(self, payload: Mapping[str, Any], received_at: datetime) -> tuple[float, float, float, datetime]:
        return (
            _positive(payload.get("price"), "price"),
            _positive(payload.get("bid"), "bid"),
            _positive(payload.get("ask"), "ask"),
            _timestamp(payload.get("time"), received_at),
        )


class KrakenFeed(PublicBTCUSDFeed):
    source = "Kraken XBT/USD"
    endpoint = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"

    def _parse(self, payload: Mapping[str, Any], received_at: datetime) -> tuple[float, float, float, datetime]:
        errors = payload.get("error")
        if errors:
            raise SupportingFeedError(f"Kraken errors: {errors}")
        result = payload.get("result")
        if not isinstance(result, Mapping) or not result:
            raise SupportingFeedError("Kraken result is missing")
        ticker = next(iter(result.values()))
        if not isinstance(ticker, Mapping):
            raise SupportingFeedError("Kraken ticker is malformed")
        return (
            _positive((ticker.get("c") or [None])[0], "last"),
            _positive((ticker.get("b") or [None])[0], "bid"),
            _positive((ticker.get("a") or [None])[0], "ask"),
            received_at,
        )


class BitstampFeed(PublicBTCUSDFeed):
    source = "Bitstamp BTC/USD"
    endpoint = "https://www.bitstamp.net/api/v2/ticker/btcusd/"

    def _parse(self, payload: Mapping[str, Any], received_at: datetime) -> tuple[float, float, float, datetime]:
        return (
            _positive(payload.get("last"), "last"),
            _positive(payload.get("bid"), "bid"),
            _positive(payload.get("ask"), "ask"),
            _timestamp(payload.get("timestamp") or payload.get("microtimestamp"), received_at),
        )


def aggregate_supporting_quotes(
    quotes: Sequence[SupportingQuote],
    *,
    minimum_venues: int = 2,
) -> SupportingAggregate:
    """Median is robust to one venue outlier; dispersion is max relative deviation."""
    if minimum_venues < 1:
        raise ValueError("minimum_venues must be positive")
    healthy = [
        quote
        for quote in quotes
        if quote.healthy
        and not quote.primary
        and math.isfinite(quote.price)
        and quote.price > 0
    ]
    unique = {quote.source: quote for quote in healthy}
    healthy = list(unique.values())
    if len(healthy) < minimum_venues:
        raise InsufficientSupportingFeeds(
            f"need {minimum_venues} healthy venues, received {len(healthy)}"
        )
    median = float(statistics.median(quote.price for quote in healthy))
    dispersion = max(abs(quote.price - median) / median for quote in healthy)
    return SupportingAggregate(
        price=median,
        timestamp=max(quote.timestamp for quote in healthy),
        quotes=tuple(sorted(healthy, key=lambda quote: quote.source)),
        dispersion=dispersion,
        healthy_venues=len(healthy),
        required_venues=minimum_venues,
        primary=False,
    )


class SupportingFeeds:
    """Collect venues independently, tolerating failures until quorum is lost."""

    def __init__(
        self,
        feeds: Sequence[PublicBTCUSDFeed] | None = None,
        *,
        minimum_venues: int = 2,
    ) -> None:
        self.feeds = tuple(feeds or (CoinbaseFeed(), KrakenFeed(), BitstampFeed()))
        self.minimum_venues = minimum_venues
        self.last_errors: dict[str, str] = {}

    def get_quotes(self, *, now: datetime | None = None) -> tuple[SupportingQuote, ...]:
        quotes: list[SupportingQuote] = []
        self.last_errors = {}
        for feed in self.feeds:
            try:
                quotes.append(feed.get_quote(now=now))
            except SupportingFeedError as exc:
                self.last_errors[feed.source] = str(exc)
        return tuple(quotes)

    def get_aggregate(self, *, now: datetime | None = None) -> SupportingAggregate:
        return aggregate_supporting_quotes(
            self.get_quotes(now=now),
            minimum_venues=self.minimum_venues,
        )

    def close(self) -> None:
        for feed in self.feeds:
            feed.close()

