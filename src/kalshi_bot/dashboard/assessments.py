"""Live market assessment rows for the Edge Desk scanner table."""

from __future__ import annotations

from typing import Any


def _book_label(decision: dict[str, Any]) -> tuple[str, float | None, str | None]:
    yes_ask = decision.get("yes_ask")
    yes_bid = decision.get("yes_bid")
    up_prob = decision.get("up_probability")

    mid: float | None = None
    if yes_ask is not None and yes_bid is not None:
        mid = (float(yes_ask) + float(yes_bid)) / 2.0
    elif up_prob is not None:
        mid = float(up_prob)
    elif yes_ask is not None:
        mid = float(yes_ask)

    if mid is None:
        return "—", None, None

    up_pct = mid * 100.0
    down_pct = (1.0 - mid) * 100.0
    if up_pct + 1e-9 >= down_pct:
        return f"UP {up_pct:.0f}%", up_pct, "UP"
    return f"DOWN {down_pct:.0f}%", down_pct, "DOWN"


def _side_label(decision: dict[str, Any]) -> str:
    action = str(decision.get("action") or "NO_TRADE").upper()
    trade_dir = str(decision.get("trade_direction") or "").upper()
    if action in {"BUY_UP"} or trade_dir == "UP":
        return "UP"
    if action in {"BUY_DOWN"} or trade_dir == "DOWN":
        return "DOWN"
    if action == "HOLD":
        pos = decision.get("position")
        if isinstance(pos, str):
            try:
                import json

                pos = json.loads(pos)
            except Exception:
                pos = None
        if isinstance(pos, dict) and pos.get("side"):
            return str(pos["side"]).upper()
    return "—"


def _recommendation(decision: dict[str, Any]) -> str:
    action = str(decision.get("action") or "NO_TRADE").upper()
    if action in {"BUY_UP", "BUY_DOWN"}:
        return "buy"
    if action == "HOLD":
        return "hold"
    if action == "EXIT":
        return "exit"
    return "skip"


def _gate_action(decision: dict[str, Any]) -> str:
    blocking = decision.get("primary_blocker") or decision.get("blocking_summary")
    fail_count = int(decision.get("fail_count") or 0)
    if blocking or fail_count > 0:
        return "block"
    return "pass"


def build_assessment(decision: dict[str, Any]) -> dict[str, Any]:
    """Map one enriched decision journal row to a live assessment row."""
    horizon = decision.get("horizon") or "15m"
    secs = decision.get("seconds_remaining")
    tau_min = round(float(secs) / 60.0, 1) if secs is not None else None

    book, side_poll, _dominant = _book_label(decision)
    edge = decision.get("edge")
    net_edge_cents = round(float(edge) * 100.0) if edge is not None else None

    agreement = decision.get("signal_agreement")
    ensemble_pct = round(float(agreement) * 100.0) if agreement is not None else None

    pass_count = int(decision.get("pass_count") or 0)
    fail_count = int(decision.get("fail_count") or 0)
    total = pass_count + fail_count
    quality = round(100.0 * pass_count / total) if total > 0 else None

    rec = _recommendation(decision)
    blocker = decision.get("primary_blocker") or decision.get("blocking_summary") or ""
    if rec != "skip":
        blocker = ""

    return {
        "asset": "BTC",
        "horizon": horizon,
        "ticker": decision.get("ticker"),
        "tau_left_min": tau_min,
        "book": book,
        "action": _gate_action(decision),
        "side": _side_label(decision),
        "side_poll_pct": side_poll,
        "net_edge_cents": net_edge_cents,
        "ensemble_pct": ensemble_pct,
        "quality": quality,
        "rec": rec,
        "blocker": blocker,
        "decision_action": decision.get("action"),
        "ts": decision.get("ts"),
    }


def latest_assessments(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One assessment per horizon (15m, 1h), newest first within each."""
    by_horizon: dict[str, dict[str, Any]] = {}
    for row in decisions:
        horizon = row.get("horizon") or "15m"
        if horizon not in by_horizon:
            by_horizon[horizon] = build_assessment(row)
    order = ["15m", "1h", "other"]
    return [by_horizon[key] for key in order if key in by_horizon]
