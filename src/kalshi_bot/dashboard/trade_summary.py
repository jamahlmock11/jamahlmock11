"""Parse trade journal rows into human-readable summaries."""

from __future__ import annotations

import json
import re
from typing import Any

_PNL = re.compile(r"pnl=\$([-\d.]+)", re.I)
_EDGE = re.compile(r"edge=([\d.]+)%", re.I)
_MODE_ACTION = re.compile(r"\[(LIVE|PAPER)\]\s*([A-Z_]+)", re.I)
_PRICE_CENTS = re.compile(r"@(\d+)¢")

HORIZON_LABELS = {
    "15m": "15-minute",
    "1h": "1-hour",
}


def infer_horizon(ticker: str) -> str:
    upper = (ticker or "").upper()
    if upper.startswith("KXBTC15M"):
        return "15m"
    if upper.startswith("KXBTCD"):
        return "1h"
    return "other"


def parse_trade_detail(detail: str) -> dict[str, Any]:
    text = detail or ""
    pnl_match = _PNL.search(text)
    edge_match = _EDGE.search(text)
    mode_action = _MODE_ACTION.search(text)
    cents_match = _PRICE_CENTS.search(text)
    return {
        "mode_label": mode_action.group(1).upper() if mode_action else None,
        "action_label": mode_action.group(2).upper() if mode_action else None,
        "pnl_usd": float(pnl_match.group(1)) if pnl_match else None,
        "edge_pct": float(edge_match.group(1)) if edge_match else None,
        "price_cents": int(cents_match.group(1)) if cents_match else None,
    }


def summarize_trade(row: dict[str, Any], *, horizon: str | None = None) -> dict[str, Any]:
    """Enrich a journal trade row with summary fields for the dashboard."""
    trade = dict(row)
    parsed = parse_trade_detail(trade.get("detail") or "")
    payload_raw = trade.get("payload")
    payload: dict[str, Any] = {}
    if payload_raw:
        try:
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else dict(payload_raw)
        except (json.JSONDecodeError, TypeError):
            payload = {}

    strategy = trade.get("strategy") or ""
    action = parsed.get("action_label")
    if not action:
        if strategy == "forecast_exit":
            action = "EXIT"
        elif strategy == "forecast":
            action = "BUY"
        else:
            action = strategy.upper()

    side = (trade.get("side") or "").upper()
    count = float(trade.get("count") or 0)
    price = float(trade.get("price") or 0)
    price_cents = parsed.get("price_cents") or round(price * 100)
    edge_pct = parsed.get("edge_pct")
    if edge_pct is None and trade.get("edge") is not None:
        edge_pct = float(trade["edge"])

    hz = horizon or infer_horizon(trade.get("ticker") or "")
    mode = parsed.get("mode_label") or ("PAPER" if trade.get("dry_run") else "LIVE")
    ok = bool(trade.get("ok"))
    pnl = parsed.get("pnl_usd")

    if action == "EXIT":
        summary = (
            f"{mode} exit {count:.0f} {side} @ {price_cents}¢"
            + (f" · P/L {pnl:+.2f}" if pnl is not None else "")
        )
    elif action in {"BUY_UP", "BUY_DOWN", "BUY"}:
        direction = "UP" if "UP" in action or side == "YES" else "DOWN"
        summary = (
            f"{mode} buy {direction} {count:.0f} @ {price_cents}¢"
            + (f" · edge {edge_pct:.1f}%" if edge_pct is not None else "")
        )
    else:
        summary = trade.get("detail") or f"{mode} {action} {side}"

    trade.update(
        {
            "horizon": hz,
            "horizon_label": HORIZON_LABELS.get(hz, hz),
            "action_type": action,
            "mode_label": mode,
            "price_cents": price_cents,
            "edge_pct": edge_pct,
            "pnl_usd": pnl,
            "summary": summary,
            "ok": ok,
            "payload_action": payload.get("action"),
        }
    )
    return trade


def aggregate_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    live = [t for t in trades if not t.get("dry_run") and t.get("ok")]
    exits = [t for t in live if t.get("action_type") == "EXIT" or t.get("strategy") == "forecast_exit"]
    entries = [
        t
        for t in live
        if t.get("action_type") not in {"EXIT"}
        and t.get("strategy") != "forecast_exit"
    ]
    pnl_values = [t["pnl_usd"] for t in exits if t.get("pnl_usd") is not None]
    total_pnl = sum(pnl_values)
    wins = sum(1 for p in pnl_values if p > 0)
    losses = sum(1 for p in pnl_values if p < 0)
    return {
        "total_trades": len(trades),
        "live_trades": len(live),
        "dry_trades": len([t for t in trades if t.get("dry_run") and t.get("ok")]),
        "failed_trades": len([t for t in trades if not t.get("ok")]),
        "entries": len(entries),
        "exits": len(exits),
        "closed_pnl_usd": round(total_pnl, 4),
        "wins": wins,
        "losses": losses,
        "flat_exits": len(pnl_values) - wins - losses,
        "notional_usd": round(sum(float(t.get("notional") or 0) for t in live), 4),
        "avg_edge_pct": round(
            sum(float(t.get("edge_pct") or t.get("edge") or 0) for t in entries) / max(len(entries), 1),
            2,
        ),
        "by_horizon": _counts_by(trades, "horizon"),
        "by_strategy": _counts_by(trades, "strategy"),
    }


def _counts_by(trades: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for trade in trades:
        label = str(trade.get(key) or "unknown")
        bucket = buckets.setdefault(label, {"label": label, "count": 0, "notional": 0.0})
        bucket["count"] += 1
        if trade.get("ok"):
            bucket["notional"] += float(trade.get("notional") or 0)
    return sorted(buckets.values(), key=lambda row: row["count"], reverse=True)
