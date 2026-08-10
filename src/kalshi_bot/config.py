from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TierConfig(BaseModel):
    high_pp: float = 25.0
    medium_pp: float = 20.0
    low_pp: float = 20.0
    tight_spread_cents: float = 3.0
    min_book_usd: float = 25.0


class CrossVenueConfig(BaseModel):
    enabled: bool = True
    max_pair_cost: float = 0.99
    min_edge_usd: float = 0.01
    order_size: int = 5


class ExecutionConfig(BaseModel):
    dry_run: bool = True
    orders_enabled: bool = True
    max_position_usd: float = 50.0
    max_contracts_per_trade: int = 100
    min_trade_notional_usd: float = Field(default=0.0, ge=0.0)
    poll_interval_sec: float = 3.0
    only_tiers: list[str] = Field(default_factory=lambda: ["HIGH", "MEDIUM"])
    fee_rate: float = 0.0
    fee_per_contract: float = 0.0
    slippage_bps: float = 5.0
    slippage_per_contract: float = 0.0


class HourEdgeConfig(BaseModel):
    minimum_edge: float = Field(default=0.10, ge=0.10, le=0.20)
    preferred_edge: float = Field(default=0.15, ge=0.10)
    strong_edge: float = Field(default=0.20, ge=0.15)
    tier_b_size_mult: float = Field(default=0.5, gt=0.0, le=1.0)
    tier_a_size_mult: float = Field(default=0.75, gt=0.0, le=1.0)
    tier_a_plus_size_mult: float = Field(default=1.0, gt=0.0, le=1.0)
    disable_tier_b: bool = False


class PollConfig(BaseModel):
    favorable_min: float = Field(default=0.85, ge=0.0, le=1.0)
    favorable_max: float = Field(default=0.90, ge=0.0, le=1.0)
    low_poll_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    counter_evidence_min_probability: float = Field(default=0.70, ge=0.0, le=1.0)
    counter_evidence_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    counter_evidence_min_agreement: float = Field(default=0.65, ge=0.0, le=1.0)
    low_poll_min_probability: float = Field(default=0.72, ge=0.0, le=1.0)
    low_poll_min_confidence: float = Field(default=0.68, ge=0.0, le=1.0)
    low_poll_min_agreement: float = Field(default=0.68, ge=0.0, le=1.0)


class LongshotConfig(BaseModel):
    enabled: bool = False
    max_entry_price: float = Field(default=0.45, gt=0.0, le=1.0)
    min_edge: float = Field(default=0.10, ge=0.10, le=0.20)
    min_confidence: float = Field(default=0.50, ge=0.0, le=1.0)
    min_signal_agreement: float = Field(default=0.50, ge=0.0, le=1.0)
    poll_enabled: bool = False
    require_forecast_alignment: bool = False
    position_size_mult: float = Field(default=0.5, gt=0.0, le=1.0)
    take_profit_cents: float = Field(default=0.10, gt=0.0, le=1.0)
    take_profit_price: float = Field(default=0.55, gt=0.0, le=1.0)
    stop_loss_cents: float = Field(default=0.08, gt=0.0, le=1.0)
    time_stop_seconds: float = Field(default=1200.0, ge=0.0)
    reversal_cents: float = Field(default=0.05, gt=0.0, le=1.0)
    reversal_window_seconds: float = Field(default=120.0, ge=0.0)
    entry_window_seconds: float = Field(default=1200.0, ge=0.0)


class HourStrategyConfig(BaseModel):
    series_ticker: str = "KXBTCD"
    market_type: str = "1h"
    contract_duration_seconds: float = Field(default=3600.0, gt=0.0)
    min_seconds_remaining: float = Field(default=30.0, ge=0.0)
    max_entry_seconds_remaining: float = Field(default=900.0, ge=0.0)
    late_window_seconds: float = Field(default=900.0, ge=0.0)
    mid_window_seconds: float = Field(default=1800.0, ge=0.0)
    final_seconds: float = Field(default=60.0, ge=0.0)
    history_seconds: float = Field(default=3700.0, gt=0.0)
    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    min_signal_agreement: float = Field(default=0.55, ge=0.0, le=1.0)
    tier_b_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    tier_b_min_agreement: float = Field(default=0.65, ge=0.0, le=1.0)
    min_data_completeness: float = Field(default=0.65, ge=0.0, le=1.0)
    max_spread: float = Field(default=0.14, ge=0.0, le=1.0)
    order_quantity: float = Field(default=1.0, gt=0.0)
    poll_interval_sec: float = Field(default=5.0, gt=0.0)
    model_version: str = "hour-v1.0.0"
    require_forecast_alignment: bool = True
    forecast_alignment_min_probability: float = Field(default=0.65, ge=0.0, le=1.0)


class StrategyConfig(BaseModel):
    min_edge: float = Field(default=0.20, ge=0.20)
    target_edge: float = Field(default=0.25, ge=0.20)
    min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)
    min_signal_agreement: float = Field(default=0.60, ge=0.0, le=1.0)
    min_data_completeness: float = Field(default=0.75, ge=0.0, le=1.0)
    max_spread: float = Field(default=0.12, ge=0.0, le=1.0)
    min_seconds_remaining: float = Field(default=30.0, ge=0.0)
    max_entry_seconds_remaining: float = Field(default=600.0, ge=0.0)
    late_seconds: float = Field(default=120.0, ge=0.0)
    final_seconds: float = Field(default=60.0, ge=0.0)
    final_min_edge: float = Field(default=0.25, ge=0.20)
    order_quantity: float = Field(default=1.0, gt=0.0)
    min_trade_quality_score: float = Field(default=65.0, ge=0.0, le=100.0)
    max_do_not_trade_score: float = Field(default=40.0, ge=0.0, le=100.0)
    require_trade_quality: bool = True
    min_pattern_matches: int = Field(default=10, ge=0)
    external_data_enabled: bool = False


class RiskConfig(BaseModel):
    max_daily_loss: float = Field(default=100.0, gt=0.0)
    max_contract_exposure: float = Field(default=25.0, gt=0.0)
    max_position_size: float = Field(default=50.0, gt=0.0)
    max_consecutive_losses: int = Field(default=4, gt=0)
    max_trades_per_contract: int = Field(default=2, gt=0)
    max_flips_per_contract: int = Field(default=1, ge=0)
    cooldown_seconds: float = Field(default=30.0, ge=0.0)
    stop_loss_fraction: float = Field(default=0.45, ge=0.0, le=1.0)
    opposite_edge_shift: float = Field(default=0.15, ge=0.0, le=1.0)
    thesis_reversal_margin: float = Field(default=0.10, ge=0.0, le=0.50)
    thesis_reversal_enabled: bool = False
    opposite_edge_exit_enabled: bool = False
    recovery_hold_enabled: bool = False
    recovery_hold_min_probability: float = Field(default=0.58, ge=0.0, le=1.0)
    recovery_hold_min_confidence: float = Field(default=0.58, ge=0.0, le=1.0)
    recovery_hold_min_agreement: float = Field(default=0.58, ge=0.0, le=1.0)
    min_hold_seconds: float = Field(default=0.0, ge=0.0)


class DataConfig(BaseModel):
    benchmark_mode: Literal["official", "constituent_proxy", "kalshi_passthrough"] = "constituent_proxy"
    cf_benchmark_url: str = ""
    cf_benchmark_api_key: str = ""
    cf_benchmark_api_key_header: str = "Authorization"
    cf_benchmark_api_key_prefix: str = "Bearer"
    max_brti_age_seconds: float = Field(default=15.0, gt=0.0)
    min_supporting_venues: int = Field(default=3, ge=2)
    max_supporting_dispersion: float = Field(default=0.003, gt=0.0)


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
    series: list[str] = Field(default_factory=lambda: ["KXBTC15M"])
    horizon: Literal["15m", "1h"] = "15m"
    poll: PollConfig = Field(default_factory=PollConfig)
    longshot: LongshotConfig = Field(default_factory=LongshotConfig)
    hour: HourStrategyConfig = Field(default_factory=HourStrategyConfig)
    hour_edge: HourEdgeConfig = Field(default_factory=HourEdgeConfig)
    tiers: TierConfig = Field(default_factory=TierConfig)
    cross_venue: CrossVenueConfig = Field(default_factory=CrossVenueConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)
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
    cf_benchmark_url: str = ""
    cf_benchmark_api_key: str = ""
    cf_benchmark_api_key_header: str = "Authorization"
    cf_benchmark_api_key_prefix: str = "Bearer"
    benchmark_mode: Literal["official", "constituent_proxy", "kalshi_passthrough"] | None = None

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
    cfg.risk.max_daily_loss = settings.max_daily_loss_usd
    cfg.risk.max_position_size = settings.max_position_usd
    cfg.data.cf_benchmark_url = settings.cf_benchmark_url or cfg.data.cf_benchmark_url
    cfg.data.cf_benchmark_api_key = settings.cf_benchmark_api_key or cfg.data.cf_benchmark_api_key
    cfg.data.cf_benchmark_api_key_header = settings.cf_benchmark_api_key_header
    cfg.data.cf_benchmark_api_key_prefix = settings.cf_benchmark_api_key_prefix
    if settings.benchmark_mode is not None:
        cfg.data.benchmark_mode = settings.benchmark_mode
    return cfg


def ensure_dirs() -> None:
    Path("logs").mkdir(exist_ok=True)
    Path("secrets").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)