"""Strict client for the official CME CF Bitcoin Real Time Index (BRTI)."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from kalshi_bot.domain import BenchmarkQuote, utc_datetime

BRTI_SOURCE = "CME CF Bitcoin Real Time Index (BRTI)"


class BenchmarkDataError(RuntimeError):
    """Base class for unusable primary benchmark data."""


class BenchmarkConfigurationError(BenchmarkDataError):
    pass


class BenchmarkSourceError(BenchmarkDataError):
    pass


class BenchmarkPayloadError(BenchmarkDataError):
    pass


class StaleBenchmarkError(BenchmarkDataError):
    pass


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return utc_datetime(value)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            return _parse_timestamp(float(text))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BenchmarkPayloadError(f"invalid BRTI timestamp: {value!r}") from exc
        return utc_datetime(parsed)
    raise BenchmarkPayloadError("BRTI timestamp is missing or malformed")


def _walk_mappings(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        yield payload
        for value in payload.values():
            yield from _walk_mappings(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_mappings(value)


def _values_for_keys(payload: Any, keys: tuple[str, ...]) -> Iterable[Any]:
    nodes = tuple(_walk_mappings(payload))
    for wanted in keys:
        normalized_wanted = wanted.lower().replace("-", "_")
        for node in nodes:
            for key, value in node.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized == normalized_wanted and not isinstance(value, (Mapping, list)):
                    yield value


def _first_key(payload: Any, keys: tuple[str, ...]) -> Any:
    for value in _values_for_keys(payload, keys):
        return value
    return None


def _is_explicit_brti_source(value: Any) -> bool:
    text = " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())
    return (
        text in {"brti", "cme cf brti"}
        or "cme cf bitcoin real time index" in text
        or ("cf benchmarks" in text and "bitcoin real time index" in text)
    )


def _candidate_branches(payload: Any) -> Iterable[Mapping[str, Any]]:
    """Yield narrow branches first so array entries cannot borrow sibling data."""
    if isinstance(payload, Mapping):
        for value in payload.values():
            yield from _candidate_branches(value)
        yield payload
    elif isinstance(payload, list):
        for value in payload:
            yield from _candidate_branches(value)


def parse_brti_payload(
    payload: Any,
    *,
    now: datetime,
    max_age: timedelta,
    future_tolerance: timedelta = timedelta(seconds=5),
) -> BenchmarkQuote:
    """Parse common API envelopes while requiring explicit BRTI provenance."""
    if not isinstance(payload, (Mapping, list)):
        raise BenchmarkPayloadError("BRTI response must be a JSON object or array")

    source_keys = (
        "source",
        "index",
        "index_name",
        "indexName",
        "benchmark",
        "symbol",
        "instrument",
    )
    price_keys = ("price", "value", "index_value", "indexValue", "rate", "last", "last_price")
    timestamp_keys = (
        "timestamp",
        "time",
        "ts",
        "as_of",
        "asOf",
        "published_at",
        "publishedAt",
        "date",
    )
    selected: tuple[Any, Any, Any] | None = None
    for branch in _candidate_branches(payload):
        sources = tuple(_values_for_keys(branch, source_keys))
        source = next((value for value in sources if _is_explicit_brti_source(value)), None)
        if source is None:
            continue
        price_value = _first_key(branch, price_keys)
        timestamp_value = _first_key(branch, timestamp_keys)
        if price_value is not None and timestamp_value is not None:
            selected = source, price_value, timestamp_value
            break
    if selected is None:
        source_values = tuple(_values_for_keys(payload, source_keys))
        if any(_is_explicit_brti_source(value) for value in source_values):
            raise BenchmarkPayloadError("BRTI source is present but price or timestamp is missing")
        raise BenchmarkSourceError(
            f"response sources {source_values!r} do not explicitly identify CME CF Bitcoin Real Time Index/BRTI"
        )
    _, price_value, timestamp_value = selected
    try:
        price = float(price_value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkPayloadError("BRTI price is missing or malformed") from exc
    if not math.isfinite(price) or price <= 0:
        raise BenchmarkPayloadError(f"BRTI price must be positive and finite, got {price!r}")

    timestamp = _parse_timestamp(timestamp_value)
    now = utc_datetime(now)
    age = now - timestamp
    if age > max_age:
        raise StaleBenchmarkError(f"BRTI quote is stale by {age.total_seconds():.3f}s")
    if age < -future_tolerance:
        raise BenchmarkPayloadError(
            f"BRTI quote is {abs(age.total_seconds()):.3f}s in the future"
        )
    return BenchmarkQuote(
        price=price,
        timestamp=timestamp,
        source=BRTI_SOURCE,
        primary=True,
        is_live=True,
        replay=False,
    )


class CFBenchmarkClient:
    """Fetch BRTI without ever falling back to a proxy venue."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        api_key_header: str = "Authorization",
        api_key_prefix: str = "Bearer",
        max_age_seconds: float = 15.0,
        timeout: float = 10.0,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = endpoint.strip()
        self.max_age = timedelta(seconds=max_age_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._headers = {"Accept": "application/json"}
        if api_key:
            prefix = f"{api_key_prefix.strip()} " if api_key_prefix.strip() else ""
            self._headers[api_key_header] = f"{prefix}{api_key}"

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def get_quote(self, *, now: datetime | None = None) -> BenchmarkQuote:
        if not self.endpoint:
            raise BenchmarkConfigurationError("official BRTI endpoint URL is required")
        try:
            response = self._http.get(self.endpoint, headers=self._headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BenchmarkDataError(f"failed to fetch official BRTI data: {exc}") from exc
        return parse_brti_payload(
            payload,
            now=now or self._clock(),
            max_age=self.max_age,
        )

    quote = get_quote


class ReplayBRTIFeed:
    """Controlled in-memory BRTI sequence; always marked replay and never live."""

    def __init__(self, points: Iterable[tuple[datetime, float] | BenchmarkQuote]) -> None:
        replay: list[BenchmarkQuote] = []
        for point in points:
            if isinstance(point, BenchmarkQuote):
                timestamp, price = point.timestamp, point.price
            else:
                timestamp, price = point
            if not math.isfinite(price) or price <= 0:
                raise BenchmarkPayloadError("replay BRTI prices must be positive and finite")
            replay.append(
                BenchmarkQuote(
                    price=price,
                    timestamp=timestamp,
                    source=f"{BRTI_SOURCE} [CONTROLLED REPLAY]",
                    primary=True,
                    is_live=False,
                    replay=True,
                )
            )
        self._quotes = tuple(sorted(replay, key=lambda quote: quote.timestamp))
        self._cursor = 0

    def get_quote(self) -> BenchmarkQuote:
        if self._cursor >= len(self._quotes):
            raise BenchmarkDataError("controlled BRTI replay is exhausted")
        quote = self._quotes[self._cursor]
        self._cursor += 1
        return quote

    quote = get_quote

    def reset(self) -> None:
        self._cursor = 0


ControlledBRTIFeed = ReplayBRTIFeed
