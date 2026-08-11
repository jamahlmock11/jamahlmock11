"""Trade requirement checklist for the Edge Desk dashboard."""

from __future__ import annotations

import json
from typing import Any

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_edge": 0.20,
    "min_signal_agreement": 0.48,
    "min_data_completeness": 0.75,
    "min_seconds_remaining": 60.0,
    "max_entry_seconds_remaining": 900.0,
    "max_spread": 0.12,
    "min_entry_executable_cost": 0.08,
    "late_favorite_seconds": 420.0,
    "late_favorite_poll_threshold": 0.78,
    "late_favorite_min_edge": 0.04,
    "min_confidence": 0.0,
    "late_confidence_increment": 0.10,
    "late_seconds": 120.0,
}

GATE_TO_REQUIREMENT: dict[str, str] = {
    "market_validity": "market",
    "market_status": "market_open",
    "last_minute": "time_window",
    "time_window": "time_window",
    "primary_brti": "brti",
    "live_data": "brti",
    "proxy_constituents": "brti",
    "proxy_dispersion": "brti",
    "proxy_late_contract": "brti",
    "benchmark_freshness": "brti",
    "feature_freshness": "features",
    "data_completeness": "features",
    "confidence": "confidence",
    "late_confidence": "confidence",
    "agreement": "signal_agreement",
    "yes_spread": "spread",
    "no_spread": "spread",
    "yes_liquidity": "liquidity",
    "no_liquidity": "liquidity",
    "yes_execution": "liquidity",
    "no_execution": "liquidity",
    "yes_kelly_execution": "liquidity",
    "no_kelly_execution": "liquidity",
    "minimum_edge": "edge",
    "min_entry_price": "min_entry_price",
    "exit_liquidity": "exit_liquidity",
    "kelly_sizing": "kelly_size",
    "risk_lock": "risk_lock",
    "duplicate": "duplicate",
    "open_order": "open_orders",
    "poll_alignment": "poll",
    "intelligence": "intelligence",
}


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _cents(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}¢"


def _pct(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def _thresholds(row: dict[str, Any]) -> dict[str, Any]:
    payload = _parse_json(row.get("payload"), {})
    config = payload.get("config") or {}
    merged = dict(DEFAULT_THRESHOLDS)
    merged.update({k: v for k, v in config.items() if v is not None})
    return merged


def _failures(row: dict[str, Any]) -> list[dict[str, Any]]:
    return _parse_json(row.get("gate_failures"), [])


def _blocking_ids(failures: list[dict[str, Any]]) -> set[str]:
    blocked: set[str] = set()
    for failure in failures:
        gate = str(failure.get("gate") or "")
        req_id = GATE_TO_REQUIREMENT.get(gate, gate)
        blocked.add(req_id)
    return blocked


def _req(
    req_id: str,
    label: str,
    *,
    status: str,
    detail: str,
    blocking: bool,
) -> dict[str, Any]:
    return {
        "id": req_id,
        "label": label,
        "status": status,
        "detail": detail,
        "blocking": blocking,
    }


def build_trade_requirements(row: dict[str, Any]) -> dict[str, Any]:
    """Return requirement rows plus blocking summary for one decision journal row."""
    thresholds = _thresholds(row)
    failures = _failures(row)
    blocking_ids = _blocking_ids(failures)
    payload = _parse_json(row.get("payload"), {})
    risk = payload.get("risk") or {}
    strike_ctx = payload.get("strike_context") or {}
    reversal = payload.get("position_reversal") or {}
    action = str(row.get("action") or "NO_TRADE").upper()
    data_health = str(row.get("data_health") or "UNKNOWN").upper()
    seconds = row.get("seconds_remaining")
    edge = row.get("edge")
    executable = row.get("executable_price")
    agreement = row.get("signal_agreement")
    confidence = row.get("confidence")
    traded = bool(row.get("traded"))

    requirements: list[dict[str, Any]] = []

    def blocked(req_id: str) -> bool:
        return req_id in blocking_ids

    # Market & timing
    market_ok = data_health not in {"FAILED"} and not blocked("market")
    requirements.append(
        _req(
            "market",
            "Valid market",
            status="pass" if market_ok else "fail",
            detail=data_health,
            blocking=blocked("market"),
        )
    )
    requirements.append(
        _req(
            "market_open",
            "Market open",
            status="fail" if blocked("market_open") else "pass",
            detail="open/active" if not blocked("market_open") else "closed",
            blocking=blocked("market_open"),
        )
    )
    min_secs = float(thresholds["min_seconds_remaining"])
    max_secs = float(thresholds["max_entry_seconds_remaining"])
    time_ok = (
        seconds is not None
        and float(seconds) >= min_secs
        and float(seconds) <= max_secs
    )
    time_blocking = blocked("time_window")
    if seconds is not None and float(seconds) < min_secs:
        time_detail = f"{float(seconds):.0f}s left · need ≥{min_secs:.0f}s (no final minute)"
    elif seconds is not None and float(seconds) > max_secs:
        time_detail = f"{float(seconds):.0f}s left · max {max_secs:.0f}s"
    else:
        time_detail = (
            f"{float(seconds):.0f}s left"
            if seconds is not None
            else "time unknown"
        )
    requirements.append(
        _req(
            "time_window",
            "Entry time window",
            status="pass" if time_ok and not time_blocking else "fail",
            detail=time_detail,
            blocking=time_blocking,
        )
    )

    # Data
    brti_blocking = blocked("brti")
    if data_health == "FAILED" and not brti_blocking:
        brti_blocking = True
        blocking_ids.add("brti")
    requirements.append(
        _req(
            "brti",
            "Official BRTI feed",
            status="fail" if brti_blocking else "pass",
            detail=row.get("benchmark_source") or ("missing" if brti_blocking else "healthy"),
            blocking=brti_blocking,
        )
    )
    features_blocking = blocked("features")
    requirements.append(
        _req(
            "features",
            "Feature freshness / history",
            status="fail" if features_blocking else "pass",
            detail=(
                failures[0].get("reason", "insufficient history")
                if features_blocking and failures
                else f"≥{_pct(thresholds['min_data_completeness'], 0)} complete"
            ),
            blocking=features_blocking,
        )
    )

    min_agreement = float(thresholds["min_signal_agreement"])
    agreement_ok = agreement is not None and float(agreement) >= min_agreement
    requirements.append(
        _req(
            "signal_agreement",
            "Ensemble agreement",
            status="pass" if agreement_ok and not blocked("signal_agreement") else "fail",
            detail=(
                f"{_pct(agreement)} · need ≥{_pct(min_agreement)}"
                if agreement is not None
                else "unavailable"
            ),
            blocking=blocked("signal_agreement"),
        )
    )

    min_conf = float(thresholds["min_confidence"])
    late_conf = min_conf + float(thresholds["late_confidence_increment"])
    required_conf = (
        late_conf
        if seconds is not None and float(seconds) <= float(thresholds["late_seconds"])
        else min_conf
    )
    conf_ok = confidence is not None and float(confidence) >= required_conf
    requirements.append(
        _req(
            "confidence",
            "Forecast confidence",
            status="pass" if conf_ok and not blocked("confidence") else "fail",
            detail=(
                f"{_pct(confidence)} · need ≥{_pct(required_conf)}"
                if confidence is not None
                else "unavailable"
            ),
            blocking=blocked("confidence"),
        )
    )

    # Liquidity
    spread_blocking = blocked("spread")
    requirements.append(
        _req(
            "spread",
            "Bid-ask spread",
            status="fail" if spread_blocking else "pass",
            detail=(
                f"≤{_cents(thresholds['max_spread'])} per side"
                if not spread_blocking
                else "spread too wide"
            ),
            blocking=spread_blocking,
        )
    )
    liquidity_blocking = blocked("liquidity")
    requirements.append(
        _req(
            "liquidity",
            "Entry book depth",
            status="fail" if liquidity_blocking else "pass",
            detail="executable asks on both sides" if not liquidity_blocking else "insufficient depth",
            blocking=liquidity_blocking,
        )
    )

    # Edge
    edge_failure = next((f for f in failures if f.get("gate") == "minimum_edge"), None)
    required_edge = float(thresholds["min_edge"])
    if edge_failure and edge_failure.get("required") is not None:
        required_edge = float(edge_failure["required"])
    observed_edge = edge
    if edge_failure and edge_failure.get("observed") is not None:
        observed_edge = float(edge_failure["observed"])
    edge_ok = (
        observed_edge is not None
        and float(observed_edge) + 1e-12 >= required_edge
    )
    gap = (
        max(0.0, (required_edge - float(observed_edge)) * 100)
        if observed_edge is not None
        else None
    )
    if edge_ok:
        edge_detail = f"{_cents(observed_edge)} have · {_cents(required_edge)} need"
    elif gap is not None:
        edge_detail = f"need {gap:.0f}¢ more ({_cents(observed_edge)} have · {_cents(required_edge)} need)"
    else:
        edge_detail = f"≥{_cents(required_edge)} required"
    requirements.append(
        _req(
            "edge",
            "Minimum edge",
            status="pass" if edge_ok and not blocked("edge") else "fail",
            detail=edge_detail,
            blocking=blocked("edge"),
        )
    )

    min_price = float(thresholds["min_entry_executable_cost"])
    price_ok = executable is not None and float(executable) >= min_price
    requirements.append(
        _req(
            "min_entry_price",
            "Minimum entry price",
            status="pass" if price_ok and not blocked("min_entry_price") else "fail",
            detail=(
                f"{_cents(executable)} · need ≥{_cents(min_price)}"
                if executable is not None
                else f"≥{_cents(min_price)}"
            ),
            blocking=blocked("min_entry_price"),
        )
    )

    requirements.append(
        _req(
            "exit_liquidity",
            "Exit bid depth",
            status="fail" if blocked("exit_liquidity") else "pass",
            detail="bids cover Kelly size" if not blocked("exit_liquidity") else "cannot exit size",
            blocking=blocked("exit_liquidity"),
        )
    )

    requirements.append(
        _req(
            "kelly_size",
            "Kelly position size",
            status="fail" if blocked("kelly_size") else "pass",
            detail=(
                "zero affordable contracts"
                if blocked("kelly_size")
                else (payload.get("kelly_contracts") or "sized from edge")
            ),
            blocking=blocked("kelly_size"),
        )
    )

    risk_locked = bool(risk.get("locked"))
    requirements.append(
        _req(
            "risk_lock",
            "Risk limits",
            status="fail" if risk_locked or blocked("risk_lock") else "pass",
            detail=risk.get("reason") or "within daily loss / exposure caps",
            blocking=risk_locked or blocked("risk_lock"),
        )
    )

    has_position = False
    position = _parse_json(row.get("position"), None)
    if position and float(position.get("quantity") or 0) > 0:
        has_position = True
    requirements.append(
        _req(
            "no_position",
            "Flat before entry",
            status="na" if action in {"HOLD", "EXIT"} else ("fail" if has_position else "pass"),
            detail=(
                f"holding {position.get('side')} × {position.get('quantity')}"
                if has_position
                else "no open position"
            ),
            blocking=has_position and action in {"BUY_UP", "BUY_DOWN", "NO_TRADE"},
        )
    )

    requirements.append(
        _req(
            "open_orders",
            "No resting orders",
            status="fail" if blocked("open_orders") else "pass",
            detail="order already resting" if blocked("open_orders") else "clear",
            blocking=blocked("open_orders"),
        )
    )

    requirements.append(
        _req(
            "duplicate",
            "No duplicate intent",
            status="fail" if blocked("duplicate") else "pass",
            detail="duplicate blocked" if blocked("duplicate") else "clear",
            blocking=blocked("duplicate"),
        )
    )

    if strike_ctx:
        hold_up = strike_ctx.get("hold_up_probability")
        z_dist = strike_ctx.get("z_distance")
        requirements.append(
            _req(
                "path_hold",
                "BRTI path / strike hold",
                status="pass",
                detail=(
                    f"UP {_pct(hold_up)} · {z_dist:+.2f}σ"
                    if hold_up is not None and z_dist is not None
                    else strike_ctx.get("summary") or "tracked"
                ),
                blocking=False,
            )
        )

    if reversal:
        requirements.append(
            _req(
                "position_reversal",
                "Position reversal check",
                status="fail" if reversal.get("should_reverse") else "pass",
                detail=reversal.get("summary") or reversal.get("reason") or "—",
                blocking=bool(reversal.get("should_reverse")) and action == "EXIT",
            )
        )

    if blocked("poll"):
        requirements.append(
            _req(
                "poll",
                "Poll alignment",
                status="fail",
                detail=next(
                    (f.get("reason") for f in failures if f.get("gate") == "poll_alignment"),
                    "poll gate failed",
                ),
                blocking=True,
            )
        )

    if blocked("intelligence"):
        requirements.append(
            _req(
                "intelligence",
                "Intelligence gate",
                status="fail",
                detail=next(
                    (f.get("reason") for f in failures if f.get("gate") == "intelligence"),
                    "intelligence skip",
                ),
                blocking=True,
            ),
        )

    if action in {"BUY_UP", "BUY_DOWN"} and traded:
        for req in requirements:
            if req["id"] in {"no_position", "duplicate", "open_orders"}:
                req["status"] = "pass"
                req["blocking"] = False

    blocking_labels = [r["label"] for r in requirements if r["blocking"]]

    return {
        "requirements": requirements,
        "blocking_gates": [f.get("gate") for f in failures],
        "blocking_labels": blocking_labels,
        "blocking_summary": ", ".join(blocking_labels) if blocking_labels else "",
        "required_edge": required_edge,
        "pass_count": sum(1 for r in requirements if r["status"] == "pass"),
        "fail_count": sum(1 for r in requirements if r["status"] == "fail"),
    }


def enrich_decision(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    enriched.update(build_trade_requirements(row))
    return enriched
