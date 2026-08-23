"""Strike targets, phase windows, and risk caps for the async 15-minute bot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


CONTRACT_SECONDS = 900
PHASE1_END_SEC = 600  # minute 10
PHASE2_END_SEC = 840  # minute 14
HARD_STOP_SEC = 895  # 14:55 — no new orders after this
SETTLEMENT_TICKS = 60


@dataclass(frozen=True)
class PhaseWindows:
    """Seconds elapsed since contract open for each trading phase."""

    phase1_start: int = 0
    phase1_end: int = PHASE1_END_SEC
    phase2_end: int = PHASE2_END_SEC
    hard_stop: int = HARD_STOP_SEC
    contract_end: int = CONTRACT_SECONDS


@dataclass(frozen=True)
class RiskCaps:
    """Penny-tick aware sizing — never sweep wide spreads."""

    max_position_usd: float = 1.50
    max_contracts_per_order: int = 25
    max_spread_cents: int = 3
    min_book_depth_contracts: int = 5
    max_price_sweep_cents: int = 2
    min_order_price_cents: int = 1
    max_order_price_cents: int = 99


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./secrets/kalshi_private.key"
    kalshi_env: str = "prod"
    kalshi_base_url: str = ""
    dry_run: bool = True

    series_ticker: str = "KXBTC15M"
    phase1_drift_pct: float = 0.0065
    vwap_interval_ms: int = 100
    vwap_lookback_sec: float = 1.0

    max_position_usd: float = 1.50
    max_contracts_per_order: int = 25
    max_spread_cents: int = 3
    min_book_depth_contracts: int = 5
    max_price_sweep_cents: int = 2

    phase2_maker_offset_cents: int = 1
    phase3_min_mispricing_cents: int = 3
    phase3_certainty_distance_usd: float = 500.0
    phase3_certainty_max_remaining: int = 15

    @property
    def kalshi_url(self) -> str:
        if self.kalshi_base_url:
            return self.kalshi_base_url.rstrip("/")
        if self.kalshi_env.lower() == "demo":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def phases(self) -> PhaseWindows:
        return PhaseWindows()

    @property
    def risk(self) -> RiskCaps:
        return RiskCaps(
            max_position_usd=self.max_position_usd,
            max_contracts_per_order=self.max_contracts_per_order,
            max_spread_cents=self.max_spread_cents,
            min_book_depth_contracts=self.min_book_depth_contracts,
            max_price_sweep_cents=self.max_price_sweep_cents,
        )


def load_settings() -> BotSettings:
    load_dotenv()
    return BotSettings()


def resolve_private_key_path(path: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    return resolved
