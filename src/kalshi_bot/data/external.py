"""External data providers (economic calendar, news, sentiment)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ExternalDataSnapshot:
    """Optional external context — defaults to neutral when unavailable."""

    economic_events_nearby: bool
    news_sentiment: float  # -1 to 1
    news_momentum: float  # 0 to 1
    social_momentum: float  # 0 to 1
    uncertainty_score: float  # 0 to 1, higher = more uncertainty
    sources_available: tuple[str, ...]
    notes: tuple[str, ...]


class ExternalDataProvider:
    """Fetch or stub external data sources.

    Live integrations can be added without changing the forecasting pipeline.
    For BTC 15m contracts, most external feeds are informational only.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def fetch(self, now: datetime | None = None) -> ExternalDataSnapshot:
        if not self.enabled:
            return ExternalDataSnapshot(
                economic_events_nearby=False,
                news_sentiment=0.0,
                news_momentum=0.0,
                social_momentum=0.0,
                uncertainty_score=0.0,
                sources_available=(),
                notes=("external data disabled",),
            )

        # Placeholder for future API integrations (FRED, news APIs, etc.)
        observed = now or datetime.now(timezone.utc)
        hour = observed.hour
        # US market open hours tend to have more macro noise
        uncertainty = 0.3 if 13 <= hour <= 21 else 0.1

        return ExternalDataSnapshot(
            economic_events_nearby=False,
            news_sentiment=0.0,
            news_momentum=0.0,
            social_momentum=0.0,
            uncertainty_score=uncertainty,
            sources_available=("session_heuristic",),
            notes=("external APIs not configured; using session heuristic",),
        )
