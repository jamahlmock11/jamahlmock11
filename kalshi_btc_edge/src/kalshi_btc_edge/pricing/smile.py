from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from kalshi_btc_edge.config import PricingConfig
from kalshi_btc_edge.models import SmilePoint, VolSmile

log = logging.getLogger(__name__)


def map_btc_strike_to_ibit(
    btc_strike: float,
    btc_spot: float,
    ibit_spot: float,
) -> float:
    """Translate a BTC strike into IBIT strike space via spot ratio.

    ibit_strike = btc_strike * (ibit_spot / btc_spot)
    """
    if btc_spot <= 0 or ibit_spot <= 0:
        raise ValueError("spots must be positive")
    return btc_strike * (ibit_spot / btc_spot)


def load_smile_from_file(path: Path) -> VolSmile:
    with path.open() as f:
        raw: dict[str, Any] = json.load(f)
    points = [
        SmilePoint(moneyness=float(p["moneyness"]), iv=float(p["iv"]))
        for p in raw.get("points", [])
    ]
    return VolSmile(
        underlying=str(raw.get("underlying", "IBIT")),
        spot=float(raw["spot"]),
        points=points,
        tenor_days=float(raw.get("tenor_days", 30)),
        asof=raw.get("asof"),
    )


def static_smile(spot: float, iv: float) -> VolSmile:
    moneyness = [0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15, 1.20]
    # Mild smile: higher IV OTM puts
    points = []
    for m in moneyness:
        skew = 0.08 * max(0.0, 1.0 - m)
        points.append(SmilePoint(moneyness=m, iv=iv + skew))
    return VolSmile(underlying="IBIT", spot=spot, points=points, tenor_days=30.0)


def load_smile(
    cfg: PricingConfig,
    root: Path,
    ibit_spot: Optional[float] = None,
) -> VolSmile:
    source = cfg.smile_source.lower()
    if source == "file":
        path = Path(cfg.smile_file)
        if not path.is_absolute():
            path = root / path
        smile = load_smile_from_file(path)
        if ibit_spot and ibit_spot > 0:
            # Keep relative moneyness; update spot for strike mapping consistency
            smile.spot = ibit_spot
        return smile
    if source == "static":
        spot = ibit_spot if ibit_spot and ibit_spot > 0 else 36.5
        return static_smile(spot, cfg.static_iv)
    if source == "yahoo_ibit":
        log.warning(
            "yahoo_ibit smile often 401s on Yahoo options endpoint; "
            "falling back to file/static. Prefer smile_source=file + OPRA feed."
        )
        path = root / cfg.smile_file
        if path.exists():
            return load_smile(PricingConfig(**{**cfg.__dict__, "smile_source": "file"}), root, ibit_spot)
        return static_smile(ibit_spot or 36.5, cfg.static_iv)
    raise ValueError(f"unknown smile_source: {cfg.smile_source}")


def short_tenor_iv(
    smile: VolSmile,
    moneyness: float,
    t_years: float,
    floor_iv: float = 0.20,
    ceiling_iv: float = 0.90,
) -> float:
    """Map a longer-dated IBIT smile IV onto Kalshi short tenors.

    Full variance scaling (σ√(T_smile/T)) explodes 30d IV into absurd 15m
    levels and *overstates* digital edges vs liquid Kalshi books. Instead:
    keep smile shape, gently dampen toward an intraday regime for T < 1d.
    """
    import math

    base = smile.iv_at_moneyness(moneyness)
    if t_years <= 0:
        return base
    day = 1.0 / 365.25
    if t_years >= day:
        # Multi-day: mild blend between smile and realized-like floor
        return max(floor_iv, min(ceiling_iv, base))
    # Sub-day: dampen toward lower intraday IV (BTC often prints quieter
    # than 30d IV implies over a single session — until it doesn't).
    # w→1 as T→0 pushes toward floor; w→0 as T→1d keeps smile.
    w = 1.0 - math.sqrt(t_years / day)
    intraday = floor_iv + 0.35 * (base - floor_iv)  # ~half the 30d premium
    iv = (1.0 - w) * base + w * intraday
    return max(floor_iv, min(ceiling_iv, iv))

