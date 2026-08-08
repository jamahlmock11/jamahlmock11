from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TierConfig(BaseModel):
    high_pp: float = 15.0
    medium_pp: float = 10.0
    low_pp: float = 5.0
    tight_spread_cents: float = 3.0
    min_book_usd: float = 25.0


class CrossVenueConfig(BaseModel):
    enabled: bool = True
    max_pair_cost: float = 0.99
    min_edge_usd: float = 0.01
    order_size: int = 5


class ExecutionConfig(BaseModel):
    dry_run: bool = True
    max_position_usd: float = 50.0
    max_contracts_per_trade: int = 100
    poll_interval_sec: float = 3.0
    only_tiers: list[str] = Field(default_factory=lambda: ["HIGH", "MEDIUM"])


class PricingConfig(BaseModel):
    risk_free_rate: float = 0.05
    min_iv: float = 0.30
    max_iv: float = 1.50
    default_iv: float = 0.60
    smile_cache_sec: float = 60.0


class SettlementConfig(BaseModel):
    reference: str = "BRTI"
    proxy_symbol: str = "BTC-USD"


class AppConfig(BaseModel):
    series: list[str] = Field(default_factory=lambda: ["KXBTC15M", "KXBTCD"])
    tiers: TierConfig = Field(default_factory=TierConfig)
    cross_venue: CrossVenueConfig = Field(default_factory=CrossVenueConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    settlement: SettlementConfig = Field(default_factory=SettlementConfig)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = "./secrets/kalshi_private.key"
    kalshi_env: str = "prod"
    kalshi_base_url: str = ""

    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 1

    dry_run: bool = True
    max_position_usd: float = 50.0
    max_daily_loss_usd: float = 100.0
    min_book_depth_usd: float = 25.0
    risk_free_rate: float = 0.05
    poll_interval_sec: float = 3.0

    @property
    def kalshi_url(self) -> str:
        if self.kalshi_base_url:
            return self.kalshi_base_url.rstrip("/")
        if self.kalshi_env.lower() == "demo":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"


def load_yaml_config(path: str | Path | None = None) -> AppConfig:
    candidates = [
        Path(path) if path else None,
        Path("config/default.yaml"),
        Path(__file__).resolve().parents[2] / "config" / "default.yaml",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            data: dict[str, Any] = yaml.safe_load(candidate.read_text()) or {}
            return AppConfig.model_validate(data)
    return AppConfig()


def load_settings() -> Settings:
    load_dotenv()
    return Settings()


def merge_runtime(config: AppConfig, settings: Settings) -> AppConfig:
    """Overlay env settings onto YAML config."""
    cfg = config.model_copy(deep=True)
    cfg.execution.dry_run = settings.dry_run
    cfg.execution.max_position_usd = settings.max_position_usd
    cfg.execution.poll_interval_sec = settings.poll_interval_sec
    cfg.pricing.risk_free_rate = settings.risk_free_rate
    cfg.tiers.min_book_usd = max(cfg.tiers.min_book_usd, settings.min_book_depth_usd)
    return cfg


def ensure_dirs() -> None:
    Path("logs").mkdir(exist_ok=True)
    Path("secrets").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)