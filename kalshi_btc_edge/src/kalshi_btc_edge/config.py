from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from kalshi_btc_edge.models import Confidence


@dataclass
class ConfidenceConfig:
    high_pp: float = 15.0
    medium_pp: float = 10.0
    low_pp: float = 5.0
    high_max_spread_cents: float = 4.0
    medium_max_spread_cents: float = 8.0
    # Edges beyond this vs a liquid book are usually model error (bad smile /
    # tenor), not free money — demote to PASS until surface is calibrated.
    max_credible_edge_pp: float = 25.0



@dataclass
class PricingConfig:
    btc_spot_source: str = "coinbase"
    ibit_spot_source: str = "yahoo"
    smile_source: str = "file"
    smile_file: str = "data/ibit_smile.json"
    static_iv: float = 0.65
    risk_free_rate: float = 0.05
    dividend_yield: float = 0.0
    min_smile_points: int = 3


@dataclass
class ExecutionConfig:
    mode: str = "paper"
    min_confidence: Confidence = Confidence.HIGH
    poll_seconds: int = 15
    require_env_live_flag: bool = True
    max_paper_fills_per_scan: int = 5



@dataclass
class RiskConfig:
    bankroll_usd: float = 1000.0
    kelly_fraction: float = 0.25
    max_contracts_per_market: int = 50
    max_notional_per_trade_usd: float = 100.0
    max_open_notional_usd: float = 500.0
    max_spread_cents: float = 10.0


@dataclass
class CrossVenueConfig:
    enabled: bool = True
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    arb_sum_threshold: float = 1.00
    max_end_time_delta_seconds: int = 180
    contract_map: dict[str, str] = field(default_factory=dict)


@dataclass
class MarketsConfig:
    series: list[str] = field(default_factory=lambda: ["KXBTC15M", "KXBTCD"])
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    status: str = "open"
    page_limit: int = 200


@dataclass
class AppConfig:
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    cross_venue: CrossVenueConfig = field(default_factory=CrossVenueConfig)
    logging_level: str = "INFO"
    root: Path = field(default_factory=lambda: Path.cwd())


def _enum_confidence(value: Any) -> Confidence:
    if isinstance(value, Confidence):
        return value
    return Confidence(str(value).upper())


def load_config(path: Optional[str | Path] = None) -> AppConfig:
    root = Path(__file__).resolve().parents[2]
    cfg_path = Path(path) if path else root / "config.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open() as f:
            raw = yaml.safe_load(f) or {}

    m = raw.get("markets", {})
    p = raw.get("pricing", {})
    c = raw.get("confidence", {})
    e = raw.get("execution", {})
    r = raw.get("risk", {})
    xv = raw.get("cross_venue", {})
    log = raw.get("logging", {})

    return AppConfig(
        markets=MarketsConfig(
            series=list(m.get("series", ["KXBTC15M", "KXBTCD"])),
            kalshi_base_url=m.get(
                "kalshi_base_url",
                "https://api.elections.kalshi.com/trade-api/v2",
            ),
            status=m.get("status", "open"),
            page_limit=int(m.get("page_limit", 200)),
        ),
        pricing=PricingConfig(
            btc_spot_source=p.get("btc_spot_source", "coinbase"),
            ibit_spot_source=p.get("ibit_spot_source", "yahoo"),
            smile_source=p.get("smile_source", "file"),
            smile_file=p.get("smile_file", "data/ibit_smile.json"),
            static_iv=float(p.get("static_iv", 0.65)),
            risk_free_rate=float(p.get("risk_free_rate", 0.05)),
            dividend_yield=float(p.get("dividend_yield", 0.0)),
            min_smile_points=int(p.get("min_smile_points", 3)),
        ),
        confidence=ConfidenceConfig(
            high_pp=float(c.get("high_pp", 15.0)),
            medium_pp=float(c.get("medium_pp", 10.0)),
            low_pp=float(c.get("low_pp", 5.0)),
            high_max_spread_cents=float(c.get("high_max_spread_cents", 4.0)),
            medium_max_spread_cents=float(c.get("medium_max_spread_cents", 8.0)),
            max_credible_edge_pp=float(c.get("max_credible_edge_pp", 25.0)),
        ),
        execution=ExecutionConfig(
            mode=str(e.get("mode", "paper")).lower(),
            min_confidence=_enum_confidence(e.get("min_confidence", "HIGH")),
            poll_seconds=int(e.get("poll_seconds", 15)),
            require_env_live_flag=bool(e.get("require_env_live_flag", True)),
            max_paper_fills_per_scan=int(e.get("max_paper_fills_per_scan", 5)),
        ),

        risk=RiskConfig(
            bankroll_usd=float(r.get("bankroll_usd", 1000.0)),
            kelly_fraction=float(r.get("kelly_fraction", 0.25)),
            max_contracts_per_market=int(r.get("max_contracts_per_market", 50)),
            max_notional_per_trade_usd=float(r.get("max_notional_per_trade_usd", 100.0)),
            max_open_notional_usd=float(r.get("max_open_notional_usd", 500.0)),
            max_spread_cents=float(r.get("max_spread_cents", 10.0)),
        ),
        cross_venue=CrossVenueConfig(
            enabled=bool(xv.get("enabled", True)),
            polymarket_gamma_url=xv.get(
                "polymarket_gamma_url", "https://gamma-api.polymarket.com"
            ),
            arb_sum_threshold=float(xv.get("arb_sum_threshold", 1.00)),
            max_end_time_delta_seconds=int(xv.get("max_end_time_delta_seconds", 180)),
            contract_map=dict(xv.get("contract_map") or {}),
        ),
        logging_level=str(log.get("level", "INFO")),
        root=root,
    )
