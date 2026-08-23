"""Live market assessment rows for the Edge Desk scanner table."""

from __future__ import annotations

import json
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


def _parse_position(decision: dict[str, Any]) -> dict[str, Any] | None:
    pos = decision.get("position")
    if isinstance(pos, str):
        try:
            pos = json.loads(pos)
        except json.JSONDecodeError:
            pos = None
    return pos if isinstance(pos, dict) else None


def _has_position(decision: dict[str, Any]) -> bool:
    pos = _parse_position(decision)
    return bool(pos and float(pos.get("quantity") or 0) > 0)


def _side_label(decision: dict[str, Any]) -> str:
    action = str(decision.get("action") or "NO_TRADE").upper()
    trade_dir = str(decision.get("trade_direction") or "").upper()
    if action in {"BUY_UP"} or trade_dir == "UP":
        return "UP"
    if action in {"BUY_DOWN"} or trade_dir == "DOWN":
        return "DOWN"
    if action == "HOLD":
        pos = _parse_position(decision)
        if pos and pos.get("side"):
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


def _signal_state(
    decision: dict[str, Any],
    rec: str,
    pass_count: int,
    fail_count: int,
) -> str:
    """trade = green, near = yellow, notrade = red."""
    action = str(decision.get("action") or "NO_TRADE").upper()
    has_pos = _has_position(decision)

    if rec == "buy" or action in {"BUY_UP", "BUY_DOWN"}:
        return "trade"
    if has_pos and action in {"HOLD", "EXIT"}:
        return "trade"

    total = pass_count + fail_count
    if total == 0:
        return "notrade"

    pass_rate = pass_count / total
    gap = decision.get("edge_gap_cents")
    blockers = len(decision.get("blocking_gates") or [])

    if pass_rate >= 0.72 and blockers <= 1:
        if gap is None or float(gap) <= 5:
            return "near"
    if pass_rate >= 0.58 and blockers <= 2:
        if gap is None or float(gap) <= 8:
            return "near"

    return "notrade"


def build_assessment(decision: dict[str, Any]) -> dict[str, Any]:
    """Map one enriched decision journal row to a live assessment row."""
    horizon = decision.get("horizon") or "15m"
    secs = decision.get("seconds_remaining")
    tau_min = round(float(secs) / 60.0, 1) if secs is not None else None

    book, side_poll, _dominant = _book_label(decision)
    edge = decision.get("edge")
    observed_edge_cents = decision.get("observed_edge_cents")
    if observed_edge_cents is not None:
        net_edge_cents = round(float(observed_edge_cents))
        net_edge_decimal = float(observed_edge_cents) / 100.0
    elif edge is not None:
        net_edge_decimal = float(edge)
        net_edge_cents = round(net_edge_decimal * 100.0)
    else:
        net_edge_decimal = None
        net_edge_cents = None

    agreement = decision.get("signal_agreement")
    ensemble_pct = round(float(agreement) * 100.0) if agreement is not None else None
    confidence_pct = (
        round(float(decision.get("confidence")) * 100.0)
        if decision.get("confidence") is not None
        else None
    )

    pass_count = int(decision.get("pass_count") or 0)
    fail_count = int(decision.get("fail_count") or 0)
    total = pass_count + fail_count
    quality = round(100.0 * pass_count / total) if total > 0 else None

    rec = _recommendation(decision)
    blocker = decision.get("primary_blocker") or decision.get("blocking_summary") or ""
    reason = decision.get("reason") or ""
    if rec != "skip":
        blocker = ""

    pos = _parse_position(decision)
    signal = _signal_state(decision, rec, pass_count, fail_count)

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
        "net_edge_text": (
            f"{net_edge_cents}¢"
            if net_edge_cents is not None
            else "—"
        ),
        "net_edge_pct": (
            round(net_edge_decimal * 100.0, 1) if net_edge_decimal is not None else None
        ),
        "ensemble_pct": ensemble_pct,
        "confidence_pct": confidence_pct,
        "quality": quality,
        "rec": rec,
        "blocker": blocker,
        "reason": reason,
        "decision_action": decision.get("action"),
        "ts": decision.get("ts"),
        "dry_run": bool(decision.get("dry_run")),
        "signal": signal,
        "has_position": _has_position(decision),
        "position": pos,
        "requirements": decision.get("requirements") or [],
        "edge_gap_text": decision.get("edge_gap_text") or "",
        "edge_gap_cents": decision.get("edge_gap_cents"),
        "observed_edge_cents": decision.get("observed_edge_cents"),
        "required_edge_cents": decision.get("required_edge_cents"),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "blocking_gates": decision.get("blocking_gates") or [],
        "gate_failure_details": decision.get("gate_failure_details") or [],
        "brti_price": decision.get("brti_price"),
        "strike": decision.get("strike"),
        "yes_ask": decision.get("yes_ask"),
        "no_ask": decision.get("no_ask"),
        "data_health": decision.get("data_health"),
        "regime": decision.get("regime"),
        "reversal_status": decision.get("reversal_status"),
        "terminal_mode": decision.get("terminal_mode"),
        "terminal_forecast": decision.get("terminal_forecast"),
        "terminal_explanation": decision.get("terminal_explanation"),
        "expected_terminal_brti": decision.get("expected_terminal_brti"),
        "terminal_p_yes": decision.get("terminal_p_yes"),
        "terminal_p_no": decision.get("terminal_p_no"),
        "yes_net_edge_pp": decision.get("yes_net_edge_pp"),
        "no_net_edge_pp": decision.get("no_net_edge_pp"),
        "required_edge_pp": decision.get("required_edge_pp"),
        "strike_candidates": decision.get("strike_candidates") or [],
        "mispricing_enabled": decision.get("mispricing_enabled"),
        "model_version": decision.get("model_version"),
        "terminal_mode": decision.get("terminal_mode"),
    }


def assessments_by_horizon(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """Latest enriched assessment per bot horizon."""
    by_horizon: dict[str, dict[str, Any] | None] = {"15m": None, "1h": None}
    for row in decisions:
        horizon = row.get("horizon") or "15m"
        if horizon in by_horizon and by_horizon[horizon] is None:
            by_horizon[horizon] = build_assessment(row)
    return by_horizon


def latest_assessments(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One assessment per horizon (15m, 1h), newest first within each."""
    by_horizon: dict[str, dict[str, Any]] = {}
    for row in decisions:
        horizon = row.get("horizon") or "15m"
        if horizon not in by_horizon:
            by_horizon[horizon] = build_assessment(row)
    order = ["15m", "1h", "other"]
    return [by_horizon[key] for key in order if key in by_horizon]
