"""Trade requirement checklist for the Edge Desk dashboard."""

from __future__ import annotations

import json
import math
from typing import Any

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_edge": 0.15,
    "min_signal_agreement": 0.48,
    "min_data_completeness": 0.65,
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

REVERSAL_TIER_LABELS: dict[str, str] = {
    "no_reversal": "No reversal",
    "watch": "Watch",
    "reversal_candidate": "Candidate",
    "strong_reversal_candidate": "Strong",
}

def _parse_payload(row: dict[str, Any]) -> dict[str, Any]:
    return _parse_json(row.get("payload"), {})


def build_reversal_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarize lag-reversal mode and whether a reversal setup is active."""
    lag = payload.get("lag_reversal") or {}
    signal = payload.get("reversal_signal") or {}
    enabled = bool(lag.get("enabled"))
    entry_enabled = bool(lag.get("entry_enabled"))
    if not enabled:
        return {
            "enabled": False,
            "entry_enabled": False,
            "signal_only": False,
            "mode": "disabled",
            "mode_label": "Disabled",
            "active": False,
            "active_label": "Off",
            "score": None,
            "tier": None,
            "tier_label": "—",
            "summary": lag.get("rationale") or "Lag reversal disabled",
            "setup": None,
            "rationale": lag.get("rationale"),
        }

    mode_label = "Entries on" if entry_enabled else "Signal only"
    tier = signal.get("tier")
    score = signal.get("score")
    tier_label = REVERSAL_TIER_LABELS.get(str(tier or ""), tier or "—")
    active = bool(signal.get("active"))
    if score is not None and tier is None:
        active = float(score) >= 50.0

    if signal:
        active_label = tier_label if active else "No setup"
        summary = signal.get("summary") or lag.get("rationale") or mode_label
        setup = signal.get("setup")
    else:
        active_label = "No data"
        summary = lag.get("rationale") or mode_label
        setup = None

    return {
        "enabled": True,
        "entry_enabled": entry_enabled,
        "signal_only": bool(lag.get("signal_only")) or not entry_enabled,
        "mode": "entry" if entry_enabled else "signal",
        "mode_label": mode_label,
        "active": active,
        "active_label": active_label,
        "score": score,
        "tier": tier,
        "tier_label": tier_label,
        "summary": summary,
        "setup": setup,
        "rationale": lag.get("rationale"),
        "min_entry_score": lag.get("min_entry_score"),
    }


GATE_LABELS: dict[str, str] = {
    "market_validity": "Valid market",
    "market_status": "Market open",
    "market_discovery": "Valid market",
    "kalshi_api": "Kalshi API",
    "last_minute": "Entry time window",
    "time_window": "Entry time window",
    "primary_brti": "Official BRTI feed",
    "live_data": "Official BRTI feed",
    "proxy_constituents": "BRTI proxy venues",
    "proxy_dispersion": "BRTI proxy dispersion",
    "proxy_late_contract": "BRTI proxy timing",
    "benchmark_freshness": "BRTI freshness",
    "feature_freshness": "Feature freshness",
    "data_completeness": "Feature history",
    "confidence": "Forecast confidence",
    "late_confidence": "Late confidence",
    "agreement": "Ensemble agreement",
    "yes_spread": "YES spread",
    "no_spread": "NO spread",
    "yes_liquidity": "YES book depth",
    "no_liquidity": "NO book depth",
    "yes_execution": "YES execution",
    "no_execution": "NO execution",
    "yes_kelly_execution": "YES Kelly depth",
    "no_kelly_execution": "NO Kelly depth",
    "minimum_edge": "Minimum edge",
    "edge": "Minimum edge",
    "min_entry_price": "Minimum entry price",
    "exit_liquidity": "Exit bid depth",
    "kelly_sizing": "Kelly position size",
    "risk_lock": "Risk limits",
    "duplicate": "Duplicate intent",
    "open_order": "Resting orders",
    "poll_alignment": "Poll alignment",
    "poll_favorite": "High-probability favorite",
    "intelligence": "Intelligence gate",
}

GATE_TO_REQUIREMENT: dict[str, str] = {
    "market_validity": "market",
    "market_discovery": "market",
    "kalshi_api": "market",
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
    "edge": "edge",
    "min_entry_price": "min_entry_price",
    "exit_liquidity": "exit_liquidity",
    "kelly_sizing": "kelly_size",
    "risk_lock": "risk_lock",
    "duplicate": "duplicate",
    "open_order": "open_orders",
    "poll_alignment": "poll",
    "poll_favorite": "poll",
    "intelligence": "intelligence",
}

EDGE_GATES = frozenset({"minimum_edge", "edge", "kelly_sizing"})


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


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _edge_gap_text(observed: float | None, required: float | None) -> str:
    if observed is None or required is None:
        return "edge unavailable"
    observed_cents = observed * 100.0
    required_cents = required * 100.0
    gap = max(0.0, required_cents - observed_cents)
    if gap <= 0.05:
        surplus = observed_cents - required_cents
        if surplus > 0.05:
            return f"met (+{surplus:.0f}¢ above {required_cents:.0f}¢ min)"
        return f"{observed_cents:.0f}¢ have · {required_cents:.0f}¢ need"
    shortfall = math.ceil(gap - 1e-9)
    return f"need {shortfall:.0f}¢ more ({observed_cents:.0f}¢ have · {required_cents:.0f}¢ need)"


def _format_gate_failure(failure: dict[str, Any]) -> str:
    gate = str(failure.get("gate") or "unknown")
    label = GATE_LABELS.get(gate, gate.replace("_", " ").title())
    reason = str(failure.get("reason") or "").strip()
    observed = _as_float(failure.get("observed"))
    required = _as_float(failure.get("required"))

    if gate in EDGE_GATES:
        if observed is not None and required is not None:
            return f"{label}: {_edge_gap_text(observed, required)}"
        if reason:
            return f"{label}: {reason}"
        return label

    if gate in {"yes_spread", "no_spread"}:
        if observed is not None and required is not None:
            return (
                f"{label}: {_cents(observed)} spread · max {_cents(required)}"
            )
        return f"{label}: {reason or 'spread too wide'}"

    if gate in {"yes_liquidity", "no_liquidity", "yes_execution", "no_execution"}:
        if observed is not None and required is not None:
            return f"{label}: {observed:.1f} depth · need ≥{required:.1f}"
        return f"{label}: {reason or 'insufficient depth'}"

    if gate in {"agreement", "confidence", "late_confidence", "data_completeness"}:
        if observed is not None and required is not None:
            return f"{label}: {_pct(observed)} · need ≥{_pct(required)}"
        return f"{label}: {reason or 'below threshold'}"

    if gate == "min_entry_price":
        if observed is not None and required is not None:
            return f"{label}: {_cents(observed)} · need ≥{_cents(required)}"
        return f"{label}: {reason or 'price too low'}"

    if gate in {"last_minute", "time_window", "proxy_late_contract"}:
        if observed is not None and required is not None:
            return f"{label}: {observed:.0f}s left · need ≥{required:.0f}s"
        return f"{label}: {reason or 'outside window'}"

    if gate == "risk_lock":
        return f"{label}: {reason or 'risk controls locked'}"

    if reason:
        return f"{label}: {reason}"
    return label


def _failure_for_gate(failures: list[dict[str, Any]], *gates: str) -> dict[str, Any] | None:
    gate_set = set(gates)
    for failure in failures:
        if failure.get("gate") in gate_set:
            return failure
    return None


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
    gate_failure_details = [_format_gate_failure(failure) for failure in failures]

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
    spread_failure = _failure_for_gate(failures, "yes_spread", "no_spread")
    if spread_failure:
        spread_detail = _format_gate_failure(spread_failure).split(": ", 1)[-1]
    elif not spread_blocking:
        spread_detail = f"≤{_cents(thresholds['max_spread'])} per side"
    else:
        spread_detail = "spread too wide"
    requirements.append(
        _req(
            "spread",
            "Bid-ask spread",
            status="fail" if spread_blocking else "pass",
            detail=spread_detail,
            blocking=spread_blocking,
        )
    )
    liquidity_blocking = blocked("liquidity")
    liquidity_failure = _failure_for_gate(
        failures,
        "yes_liquidity",
        "no_liquidity",
        "yes_execution",
        "no_execution",
        "yes_kelly_execution",
        "no_kelly_execution",
    )
    if liquidity_failure:
        liquidity_detail = _format_gate_failure(liquidity_failure).split(": ", 1)[-1]
    else:
        liquidity_detail = (
            "insufficient depth" if liquidity_blocking else "executable asks on both sides"
        )
    requirements.append(
        _req(
            "liquidity",
            "Entry book depth",
            status="fail" if liquidity_blocking else "pass",
            detail=liquidity_detail,
            blocking=liquidity_blocking,
        )
    )

    # Edge
    edge_failure = _failure_for_gate(failures, "minimum_edge", "edge", "kelly_sizing")
    required_edge = _as_float(payload.get("required_edge"))
    if required_edge is None:
        required_edge = float(thresholds["min_edge"])
    if edge_failure and edge_failure.get("required") is not None:
        required_edge = float(edge_failure["required"])
    observed_edge = _as_float(edge)
    if edge_failure and edge_failure.get("observed") is not None:
        observed_edge = float(edge_failure["observed"])
    edge_ok = (
        observed_edge is not None
        and float(observed_edge) + 1e-12 >= required_edge
    )
    edge_blocking = blocked("edge") or (
        action == "NO_TRADE" and observed_edge is not None and not edge_ok
    )
    if edge_blocking:
        blocking_ids.add("edge")
    edge_detail = _edge_gap_text(observed_edge, required_edge)
    if edge_failure and edge_failure.get("reason"):
        edge_detail = f"{edge_failure['reason']} · {edge_detail}"
    requirements.append(
        _req(
            "edge",
            "Minimum edge",
            status="pass" if edge_ok and not edge_blocking else "fail",
            detail=edge_detail,
            blocking=edge_blocking,
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

    kelly_enabled = bool(thresholds.get("kelly_enabled", False))
    if kelly_enabled:
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
    else:
        requirements.append(
            _req(
                "position_size",
                "Position size",
                status="pass",
                detail=f"{int(thresholds.get('order_quantity') or 1)} contract(s) · Kelly off",
                blocking=False,
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

    reversal_status = build_reversal_status(payload)
    if reversal_status.get("enabled"):
        score_text = (
            f"{reversal_status['score']:.0f}/100 · {reversal_status['tier_label']}"
            if reversal_status.get("score") is not None
            else reversal_status.get("mode_label") or "—"
        )
        detail = reversal_status.get("setup") or reversal_status.get("summary") or score_text
        requirements.append(
            _req(
                "lag_reversal",
                "Lag reversal signal",
                status="pass" if reversal_status.get("active") else "fail",
                detail=f"{score_text} · {detail}",
                blocking=False,
            )
        )
    elif payload.get("lag_reversal") is not None:
        requirements.append(
            _req(
                "lag_reversal",
                "Lag reversal signal",
                status="na",
                detail=reversal_status.get("summary") or "Disabled",
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

    known_ids = {req["id"] for req in requirements}
    for failure in failures:
        gate = str(failure.get("gate") or "")
        req_id = GATE_TO_REQUIREMENT.get(gate, gate)
        detail = _format_gate_failure(failure)
        detail_body = detail.split(": ", 1)[-1]
        if req_id in known_ids:
            for req in requirements:
                if req["id"] != req_id:
                    continue
                req["blocking"] = True
                req["status"] = "fail"
                req["detail"] = detail_body
            continue
        requirements.append(
            _req(
                req_id,
                GATE_LABELS.get(gate, gate.replace("_", " ").title()),
                status="fail",
                detail=detail_body,
                blocking=True,
            )
        )
        known_ids.add(req_id)

    blocking_labels = [r["label"] for r in requirements if r["blocking"]]
    if not gate_failure_details and blocking_labels:
        gate_failure_details = [
            f"{label}: {next((r['detail'] for r in requirements if r['label'] == label and r['blocking']), '')}"
            for label in blocking_labels
        ]
    blocking_summary = (
        " · ".join(gate_failure_details)
        if gate_failure_details
        else ", ".join(blocking_labels)
    )
    primary_blocker = gate_failure_details[0] if gate_failure_details else ""
    observed_edge_cents = (
        float(observed_edge) * 100.0 if observed_edge is not None else None
    )
    required_edge_cents = (
        float(required_edge) * 100.0 if required_edge is not None else None
    )
    edge_gap_cents = (
        max(0.0, required_edge_cents - observed_edge_cents)
        if observed_edge_cents is not None and required_edge_cents is not None
        else None
    )

    return {
        "requirements": requirements,
        "blocking_gates": [f.get("gate") for f in failures],
        "gate_failure_details": gate_failure_details,
        "blocking_labels": blocking_labels,
        "blocking_summary": blocking_summary,
        "primary_blocker": primary_blocker,
        "required_edge": required_edge,
        "observed_edge_cents": observed_edge_cents,
        "required_edge_cents": required_edge_cents,
        "edge_gap_cents": edge_gap_cents,
        "edge_gap_text": _edge_gap_text(observed_edge, required_edge),
        "pass_count": sum(1 for r in requirements if r["status"] == "pass"),
        "fail_count": sum(1 for r in requirements if r["status"] == "fail"),
    }


def enrich_decision(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    payload = _parse_payload(row)
    enriched.update(build_trade_requirements(row))
    enriched["reversal_status"] = build_reversal_status(payload)
    return enriched
