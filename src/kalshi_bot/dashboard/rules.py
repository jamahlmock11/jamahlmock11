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


def _rules_15m(cfg: AppConfig | None, mode: str, account: str) -> list[dict[str, str]]:
    strategy = cfg.strategy if cfg else None
    risk = cfg.risk if cfg else None
    min_edge = strategy.min_edge if strategy else 0.20
    min_agreement = strategy.min_signal_agreement if strategy else 0.48
    max_spread = strategy.max_spread if strategy else 0.12
    min_price = strategy.min_entry_executable_cost if strategy else 0.08
    late_fav_secs = strategy.late_favorite_seconds if strategy else 420
    late_fav_poll = strategy.late_favorite_poll_threshold if strategy else 0.78
    late_fav_model = strategy.late_favorite_min_model_probability if strategy else 0.88
    persistence = strategy.entry_signal_persistence_polls if strategy else 2
    min_secs = strategy.min_seconds_remaining if strategy else 60
    max_secs = strategy.max_entry_seconds_remaining if strategy else 900
    bankroll = risk.max_position_size if risk else 50
    exposure = risk.max_contract_exposure if risk else 25
    depth = strategy.order_quantity if strategy else 1
    edge_tiers = (
        "OFF"
        if strategy and not strategy.dynamic_edge_enabled and not strategy.mispricing_enabled
        else (
            "15–10m:10¢ · 10–7m:10¢ · 7–5m:8¢ · 5–3m:8¢ · <3m:4¢"
            if strategy and strategy.dynamic_edge_enabled
            else "OFF"
        )
    )

    return [
        {"key": "Mode", "value": mode},
        {"key": "Primary gate", "value": (
            "model prob − executable (tiers)"
            if strategy and strategy.dynamic_edge_enabled and not strategy.mispricing_enabled
            else (
                "model prob − executable price"
                if strategy and strategy.mispricing_enabled
                else "forecast direction"
            )
        )},
        {"key": "Window", "value": f"{min_secs:.0f}–{max_secs:.0f}s left"},
        {"key": "Edge tiers", "value": edge_tiers},
        {
            "key": "Late favorite",
            "value": (
                f"≤{late_fav_secs / 60:.0f}m · poll ≥{late_fav_poll * 100:.0f}% · "
                f"model ≥{late_fav_model * 100:.0f}% · {persistence} polls → 6–8¢"
            ),
        },
        {"key": "Ensemble", "value": f"≥{min_agreement * 100:.0f}%"},
        {"key": "Price floor", "value": f"≥{min_price * 100:.0f}¢"},
        {"key": "Spread", "value": f"≤{max_spread * 100:.0f}¢"},
        {"key": "Depth", "value": f"≥{depth:.0f} ct"},
        {
            "key": "Pyramiding",
            "value": "ON" if strategy and strategy.pyramiding_enabled else "OFF",
        },
        {"key": "Bankroll", "value": f"${bankroll:.0f} / ${exposure:.0f} cap"},
    ]


def _entry_band_label(min_price: float, max_price: float | None) -> str:
    """Format favorite-band display; handles disabled band (no min/max ceiling)."""
    if max_price is None and min_price <= 0:
        return "OFF"
    if max_price is None:
        return f"≥{min_price * 100:.0f}¢"
    if min_price <= 0:
        return f"≤{max_price * 100:.0f}¢"
    return f"{min_price * 100:.0f}–{max_price * 100:.0f}¢ favorites"


def _rules_1h(cfg: AppConfig | None, mode: str) -> list[dict[str, str]]:
    hour = cfg.hour if cfg else None
    terminal = cfg.terminal_probability if cfg else None
    strategy = cfg.strategy if cfg else None
    risk = cfg.risk if cfg else None

    min_secs = hour.min_seconds_remaining if hour else 60
    max_mins = (hour.max_entry_seconds_remaining / 60.0) if hour else 25
    min_conf = terminal.minimum_confidence if terminal else 0.52
    min_agree = terminal.minimum_ensemble if terminal else 0.50
    align = terminal.forecast_alignment_min_probability if terminal else 0.52
    min_price = terminal.min_entry_executable_cost if terminal else 0.0
    max_price = terminal.max_entry_executable_cost if terminal else None
    mispricing = terminal.mispricing_enabled if terminal else False
    dynamic_edge = terminal.dynamic_edge_enabled if terminal else False
    edge_gate = mispricing or dynamic_edge
    all_strikes = hour.evaluate_all_active_strikes if hour else True
    strong = hour.strong_evidence_min_probability if hour else 0.78
    persistence = terminal.signal_persistence_polls if terminal else 2
    stop_loss = risk.stop_loss_fraction if risk else 0.45
    take_profit = risk.take_profit_bid_price if risk else 0.90
    bankroll = risk.max_position_size if risk else 50
    kelly = risk.kelly_enabled if risk else False

    rules = [
        {"key": "Mode", "value": mode},
        {"key": "Window", "value": f"last {max_mins:.0f}m · ≥{min_secs:.0f}s left"},
        {"key": "Strikes", "value": "all active hour books" if all_strikes else "nearest ATM"},
        {
            "key": "Primary gate",
            "value": (
                "model prob − ask (tiers)"
                if edge_gate and not mispricing
                else (
                    "model prob − executable price"
                    if mispricing
                    else "forecast alignment"
                )
            ),
        },
        {"key": "Mispricing", "value": "ON (edge vs book)" if mispricing else "OFF (forecast + tiers)"},
    ]
    if edge_gate and terminal and terminal.dynamic_edge_enabled:
        rules.append(
            {
                "key": "Edge tiers",
                "value": "30–60m:10¢ · 10–30m:8¢ · 5–10m:8¢ · 3–5m:6¢ · <3m:4¢",
            }
        )
    rules.extend([
        {"key": "Entry band", "value": _entry_band_label(min_price, max_price)},
        {"key": "Alignment", "value": f"≥{align * 100:.0f}% on side"},
        {"key": "Confidence", "value": f"≥{min_conf * 100:.0f}%"},
        {"key": "Agreement", "value": f"≥{min_agree * 100:.0f}%"},
        {"key": "Strong thesis", "value": f"≥{strong * 100:.0f}% (rank boost)"},
        {"key": "Persistence", "value": f"{persistence} poll(s)"},
        {"key": "Stop loss", "value": f"{stop_loss * 100:.0f}% premium"},
        {"key": "Take profit", "value": f"bid ≥{take_profit * 100:.0f}¢" if take_profit else "OFF"},
        {"key": "Kelly", "value": "ON" if kelly else "OFF"},
        {"key": "Bankroll", "value": f"${bankroll:.0f} cap"},
    ])
    return rules


def active_edge_rules(*, mode: str = "LIVE", account: str = "API connected") -> dict[str, Any]:
    """Return display-ready rule chips for 15m + 1h bots."""
    cfg_15m = _load_config("config/default.yaml")
    cfg_1h = _load_config("config/1h.yaml")

    rules_15m = _rules_15m(cfg_15m, mode, account)
    rules_1h = _rules_1h(cfg_1h, mode)

    strategy = cfg_15m.strategy if cfg_15m else None
    min_edge = strategy.min_edge if strategy else 0.20
    min_agreement = strategy.min_signal_agreement if strategy else 0.48

    summary = (
        f"15m: {min_edge * 100:.0f}¢ edge, ≥{min_agreement * 100:.0f}% ensemble. "
        f"1h: terminal forecast, last 25m, tiered edge."
    )

    return {
        "rules": rules_15m + rules_1h,
        "rules_15m": rules_15m,
        "rules_1h": rules_1h,
        "summary": summary,
        "summary_15m": (
            f"{min_edge * 100:.0f}¢ net edge, ≥{min_agreement * 100:.0f}% ensemble, "
            f"BRTI + microstructure gates."
        ),
        "summary_1h": (
            "Terminal probability across all hourly strikes in the last 25 minutes; "
            + (
                "mispricing gate on."
                if cfg_1h and cfg_1h.terminal_probability.mispricing_enabled
                else "forecast + tiered edge (mispricing off)."
            )
        ),
        "config_15m": strategy.model_dump() if strategy else {},
        "config_1h": {
            **(cfg_1h.hour.model_dump() if cfg_1h else {}),
            **(cfg_1h.strategy.model_dump() if cfg_1h else {}),
        },
    }
