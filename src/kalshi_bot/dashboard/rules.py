"""Active edge rules for the Edge Desk scanner view."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kalshi_bot.config import AppConfig, load_yaml_config

WORKSPACE = Path(__file__).resolve().parents[3]


def _load_config(path: str) -> AppConfig | None:
    full = WORKSPACE / path
    if not full.exists():
        return None
    try:
        return load_yaml_config(full)
    except Exception:
        return None


def active_edge_rules(*, mode: str = "LIVE", account: str = "API connected") -> dict[str, Any]:
    """Return display-ready rule chips for 15m + 1h bots."""
    cfg_15m = _load_config("config/default.yaml")
    cfg_1h = _load_config("config/1h.yaml")
    strategy = cfg_15m.strategy if cfg_15m else None
    risk = cfg_15m.risk if cfg_15m else None
    hour = cfg_1h.hour if cfg_1h else None
    strategy_1h = cfg_1h.strategy if cfg_1h else None

    min_edge = strategy.min_edge if strategy else 0.20
    min_agreement = strategy.min_signal_agreement if strategy else 0.48
    max_spread = strategy.max_spread if strategy else 0.12
    min_price = strategy.min_entry_executable_cost if strategy else 0.08
    late_fav_secs = strategy.late_favorite_seconds if strategy else 420
    late_fav_poll = strategy.late_favorite_poll_threshold if strategy else 0.78
    late_fav_edge = strategy.late_favorite_min_edge if strategy else 0.04
    min_secs = strategy.min_seconds_remaining if strategy else 60
    max_secs_15m = strategy.max_entry_seconds_remaining if strategy else 900
    max_secs_1h = hour.max_entry_seconds_remaining if hour else 2400

    bankroll = risk.max_position_size if risk else 50
    exposure = risk.max_contract_exposure if risk else 25
    depth = strategy.order_quantity if strategy else 1

    min_poll_1h = strategy_1h.minimum_dominant_poll if strategy_1h else None

    rules = [
        {"key": "Mode", "value": mode},
        {"key": "Env", "value": "prod" if mode == "LIVE" else "paper"},
        {"key": "Account", "value": account},
        {"key": "Assets", "value": "BTC"},
        {
            "key": "Window",
            "value": f"15m: {min_secs:.0f}–{max_secs_15m:.0f}s · 1h: last {max_secs_1h / 60:.0f}m",
        },
        {"key": "Ensemble", "value": f"≥{min_agreement * 100:.0f}%"},
        {"key": "Edge (std)", "value": f"≥{min_edge * 100:.0f}¢ net"},
        {
            "key": "Late favorite",
            "value": (
                f"≤{late_fav_secs / 60:.0f}m @ {late_fav_poll * 100:.0f}% "
                f"→ {late_fav_edge * 100:.0f}¢"
            ),
        },
        {"key": "Price floor", "value": f"≥{min_price * 100:.0f}¢"},
        {"key": "Spread", "value": f"≤{max_spread * 100:.0f}¢"},
        {"key": "Depth", "value": f"≥{depth:.0f} ct"},
        {"key": "Bankroll", "value": f"${bankroll:.0f} / ${exposure:.0f} cap"},
    ]
    if min_poll_1h is not None:
        rules.append(
            {
                "key": "1h poll",
                "value": f"≥{min_poll_1h * 100:.0f}% favorite",
            }
        )

    summary = (
        f"{min_edge * 100:.0f}¢ net edge ({late_fav_edge * 100:.0f}¢ late-favorite), "
        f"≥{min_agreement * 100:.0f}% ensemble, "
        f"15m + 1h windows, BRTI + microstructure gates."
    )
    if min_poll_1h is not None:
        summary += f" 1h requires ≥{min_poll_1h * 100:.0f}% poll favorite."

    return {
        "rules": rules,
        "summary": summary,
        "config_15m": strategy.model_dump() if strategy else {},
        "config_1h": {
            **(hour.model_dump() if hour else {}),
            **(strategy_1h.model_dump() if strategy_1h else {}),
        },
    }
