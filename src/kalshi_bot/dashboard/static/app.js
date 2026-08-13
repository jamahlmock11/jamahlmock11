(() => {
  const state = {
    trades: [],
    filter: "all",
  };

  const $ = (id) => document.getElementById(id);

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function money(n) {
    return `$${(n || 0).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function signedMoney(n) {
    if (n == null) return "—";
    const value = Number(n);
    const prefix = value > 0 ? "+" : "";
    return `${prefix}${money(value)}`;
  }

  function pct(n, digits = 1) {
    return n == null ? "—" : `${Number(n).toFixed(digits)}%`;
  }

  function fixed(n, digits = 2) {
    return n == null ? "—" : Number(n).toFixed(digits);
  }

  function tierBadge(tier) {
    const t = (tier || "").toLowerCase();
    const cls =
      t === "high" ? "high" :
      t === "medium" ? "medium" :
      t === "arb" ? "arb" :
      "low";
    return `<span class="badge ${cls}">${tier || "—"}</span>`;
  }

  function sideBadge(side) {
    const s = (side || "").toLowerCase();
    const cls = s === "yes" || s === "up" ? "yes" : "no";
    return `<span class="badge ${cls}">${side || "—"}</span>`;
  }

  function modeBadge(trade) {
    if (!trade.ok) return `<span class="badge fail">fail</span>`;
    return trade.dry_run
      ? `<span class="badge dry">paper</span>`
      : `<span class="badge live">live</span>`;
  }

  function horizonBadge(trade) {
    const hz = trade.horizon || "other";
    return `<span class="badge ${hz === "1h" ? "arb" : "high"}">${trade.horizon_label || hz}</span>`;
  }

  function pnlClass(pnl) {
    if (pnl == null) return "mono";
    const v = Number(pnl);
    if (v > 0) return "mono edge-pos";
    if (v < 0) return "mono edge-neg";
    return "mono";
  }

  function filteredTrades() {
    const f = state.filter;
    return state.trades.filter((t) => {
      if (f === "all") return true;
      if (f === "dry") return !!t.dry_run;
      if (f === "live") return !t.dry_run;
      if (f === "15m" || f === "1h") return t.horizon === f;
      if (f === "forecast" || f === "forecast_exit") return t.strategy === f;
      return t.strategy === f;
    });
  }

  function reversalBadge(status) {
    if (!status || !status.enabled) {
      return `<span class="badge dry">off</span>`;
    }
    const tier = (status.tier || "").toLowerCase();
    const cls =
      tier === "strong_reversal_candidate" ? "live" :
      tier === "reversal_candidate" ? "high" :
      tier === "watch" ? "medium" :
      "dry";
    const label = status.active
      ? status.tier_label || "Active"
      : "No setup";
    return `<span class="badge ${cls}">${label}</span>`;
  }

  function renderReversalStatus(status) {
    const banner = $("reversalBanner");
    if (!status) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.classList.toggle("reversal-active", Boolean(status.active));
    banner.classList.toggle("reversal-off", !status.enabled);

    const modeBadge = $("reversalModeBadge");
    modeBadge.textContent = status.enabled ? status.mode_label || "On" : "Disabled";
    modeBadge.className = `reversal-mode-badge ${status.entry_enabled ? "entry" : status.enabled ? "signal" : "off"}`;

    const activeBadge = $("reversalActiveBadge");
    if (!status.enabled) {
      activeBadge.textContent = "Off";
      activeBadge.className = "reversal-active-badge off";
    } else if (status.active) {
      activeBadge.textContent = "Reversal active";
      activeBadge.className = "reversal-active-badge on";
    } else {
      activeBadge.textContent = "No reversal";
      activeBadge.className = "reversal-active-badge idle";
    }

    $("reversalScore").textContent =
      status.score != null
        ? `${Number(status.score).toFixed(0)}/100 · ${status.tier_label || "—"}`
        : status.mode_label || "—";
    $("reversalSetup").textContent = status.setup || status.summary || "—";
    $("reversalSummary").textContent = status.summary || status.rationale || "—";
  }

  function requirementStatusClass(status, blocking) {
    if (blocking) return "req-blocked";
    if (status === "pass") return "req-pass";
    if (status === "fail") return "req-fail";
    return "req-na";
  }

  function renderRequirementsList(requirements) {
    if (!requirements || !requirements.length) return "";
    return requirements
      .map(
        (r) => `
        <div class="req-row ${requirementStatusClass(r.status, r.blocking)}">
          <span class="req-icon">${r.blocking ? "✕" : r.status === "pass" ? "✓" : r.status === "fail" ? "!" : "·"}</span>
          <div class="req-copy">
            <strong>${r.label}</strong>
            <span>${r.detail || ""}</span>
          </div>
        </div>`
      )
      .join("");
  }

  function renderRequirementsCell(d) {
    const reqs = d.requirements || [];
    if (!reqs.length) return '<span class="req-muted">—</span>';
    const action = (d.action || "NO_TRADE").toUpperCase();
    const details = d.gate_failure_details || [];
    if (action === "NO_TRADE" && (d.primary_blocker || d.blocking_summary)) {
      const headline = d.primary_blocker || d.blocking_summary;
      const extra =
        details.length > 1
          ? `<span class="req-blocked-more">+${details.length - 1} more</span>`
          : "";
      return `<div class="req-cell blocked"><span class="req-blocked-label">Blocked</span><span class="req-blocked-list">${headline}</span>${extra}</div>`;
    }
    const pass = d.pass_count ?? reqs.filter((r) => r.status === "pass").length;
    const fail = d.fail_count ?? reqs.filter((r) => r.status === "fail").length;
    const blocked = reqs.filter((r) => r.blocking);
    if (blocked.length) {
      const headline =
        blocked[0].detail
          ? `${blocked[0].label}: ${blocked[0].detail}`
          : blocked[0].label;
      const extra =
        blocked.length > 1
          ? `<span class="req-blocked-more">+${blocked.length - 1} more</span>`
          : "";
      return `<div class="req-cell blocked"><span class="req-blocked-label">Blocked</span><span class="req-blocked-list">${headline}</span>${extra}</div>`;
    }
    return `<div class="req-cell ok"><span class="req-score">${pass}/${pass + fail} pass</span></div>`;
  }

  function renderRequirementsPanel(d) {
    const panel = $("requirementsPanel");
    const grid = $("requirementsGrid");
    const score = $("requirementsScore");
    if (!d || !d.requirements || !d.requirements.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const pass = d.pass_count ?? d.requirements.filter((r) => r.status === "pass").length;
    const fail = d.fail_count ?? d.requirements.filter((r) => r.status === "fail").length;
    const blocked = d.blocking_summary || d.primary_blocker || "all clear";
    score.textContent =
      (d.action || "NO_TRADE").toUpperCase() === "NO_TRADE"
        ? blocked
        : `${pass}/${pass + fail} requirements met`;
    grid.innerHTML = renderRequirementsList(d.requirements);
  }

  function edgeGapText(d) {
    if (!d) return "—";
    if (d.edge_gap_text) return d.edge_gap_text;
    let observed = d.edge != null ? Number(d.edge) * 100 : null;
    let required = 20;
    try {
      const failures = JSON.parse(d.gate_failures || "[]");
      const edgeGate = failures.find((g) => g.gate === "minimum_edge");
      if (edgeGate) {
        if (edgeGate.observed != null) observed = Number(edgeGate.observed) * 100;
        if (edgeGate.required != null) required = Number(edgeGate.required) * 100;
      } else if (d.required_edge != null) {
        required = Number(d.required_edge) * 100;
      }
    } catch (_) {
      if (d.required_edge != null) required = Number(d.required_edge) * 100;
    }
    if (observed == null) return "Edge unavailable";
    const gap = Math.max(0, required - observed);
    if (gap <= 0.05) {
      const surplus = observed - required;
      if (surplus > 0.05) {
        return `Met (+${surplus.toFixed(0)}¢ above ${required.toFixed(0)}¢ min)`;
      }
      return `Met (${observed.toFixed(0)}¢ have · ${required.toFixed(0)}¢ need)`;
    }
    return `Need ${Math.ceil(gap)}¢ more (${observed.toFixed(0)}¢ have · ${required.toFixed(0)}¢ need)`;
  }

  function renderTrades() {
    const rows = filteredTrades();
    const body = $("tradesBody");
    $("tradeCount").textContent = `${rows.length} fill${rows.length === 1 ? "" : "s"} shown`;
    $("tradesEmpty").hidden = rows.length > 0;
    body.innerHTML = rows
      .map(
        (t) => `
      <tr>
        <td class="mono">${fmtTime(t.ts)}</td>
        <td>${horizonBadge(t)}</td>
        <td class="summary-cell">${t.summary || t.detail || "—"}</td>
        <td class="mono">${t.ticker || "—"}</td>
        <td>${sideBadge(t.side)}</td>
        <td class="mono">${Number(t.count || 0).toFixed(0)}</td>
        <td class="mono">${t.price_cents != null ? `${t.price_cents}¢` : fixed(t.price)}</td>
        <td class="${pnlClass(t.pnl_usd)}">${signedMoney(t.pnl_usd)}</td>
        <td class="mono">${t.action_type === "EXIT" || t.strategy === "forecast_exit" ? "—" : t.edge_pct != null ? `${Number(t.edge_pct).toFixed(1)}%` : fixed(t.edge, 1)}</td>
        <td>${modeBadge(t)}</td>
      </tr>`
      )
      .join("");
  }

  function renderSignals(signals) {
    const body = $("signalsBody");
    $("signalsEmpty").hidden = signals.length > 0;
    body.innerHTML = signals
      .slice(0, 40)
      .map(
        (s) => `
      <tr>
        <td class="mono">${fmtTime(s.ts)}</td>
        <td>${tierBadge(s.confidence)}</td>
        <td class="mono">${s.ticker}</td>
        <td>${sideBadge(s.side)}</td>
        <td class="mono">${(s.kalshi_prob * 100).toFixed(1)}%</td>
        <td class="mono">${(s.options_prob * 100).toFixed(1)}%</td>
        <td class="edge-pos mono">${Number(s.edge_pp).toFixed(1)}</td>
        <td>${s.traded ? '<span class="badge live">yes</span>' : '<span class="badge dry">no</span>'}</td>
      </tr>`
      )
      .join("");
  }

  function renderScans(scans) {
    const body = $("scansBody");
    $("scansEmpty").hidden = scans.length > 0;
    body.innerHTML = scans
      .slice(0, 20)
      .map(
        (s) => `
      <tr>
        <td class="mono">${fmtTime(s.ts)}</td>
        <td class="mono">${s.mode || "—"}</td>
        <td class="mono">${money(s.spot)}</td>
        <td class="mono">${s.iv_atm != null ? (s.iv_atm * 100).toFixed(1) + "%" : "—"}</td>
        <td class="mono">${s.markets_scanned ?? "—"}</td>
        <td class="mono">${s.signal_count ?? "—"}</td>
      </tr>`
      )
      .join("");
  }

  function renderCurrentDecision(d) {
    if (!d) return;
    const action = d.action || "NO_TRADE";
    $("decisionAction").textContent = action;
    $("decisionAction").className = `decision-action ${action.toLowerCase()}`;
    $("decisionTicker").textContent = d.ticker || "—";
    $("decisionPrices").textContent =
      d.brti_price != null && d.strike != null
        ? `${money(d.brti_price)} / ${money(d.strike)}`
        : "—";
    $("decisionTime").textContent =
      d.seconds_remaining != null ? `${Math.max(0, d.seconds_remaining).toFixed(0)}s` : "—";
    $("decisionProbability").textContent = `${pct(d.up_probability)} / ${pct(d.down_probability)}`;
    $("decisionBook").textContent =
      d.yes_ask != null && d.no_ask != null
        ? `${pct(d.yes_ask, 0)} / ${pct(d.no_ask, 0)}`
        : "—";
    $("decisionEdge").textContent = pct(d.edge);
    $("decisionEdgeGap").textContent = edgeGapText(d);
    $("decisionDirection").textContent =
      `${d.current_direction || "FLAT"} → ${d.predicted_direction || "FLAT"}`;
    $("decisionRegime").textContent = `${d.regime || "—"} / ${d.trajectory || "—"}`;
    $("decisionConfidence").textContent =
      `${pct(d.confidence)} / ${pct(d.signal_agreement)}`;
    $("decisionMotion").textContent =
      `${fixed(d.momentum, 6)} / ${fixed(d.acceleration, 8)}`;
    $("decisionHealth").textContent =
      `${pct(d.volatility)} / ${d.data_health || "UNKNOWN"}`;
    let position = "FLAT";
    try {
      const parsed = d.position ? JSON.parse(d.position) : null;
      if (parsed) position = `${parsed.side} × ${parsed.quantity}`;
    } catch (_) {}
    let risk = "OK";
    try {
      const payload = d.payload ? JSON.parse(d.payload) : null;
      if (payload?.risk?.locked) risk = `LOCKED: ${payload.risk.reason || "limit"}`;
      else if (payload?.risk) risk = `OK · P/L ${money(payload.risk.realized_pnl)}`;
    } catch (_) {}
    $("decisionPosition").textContent = `${position} / ${risk}`;

    let tradeQuality = "—";
    let modelAgreement = "—";
    let liquidity = "—";
    try {
      const payload = d.payload ? JSON.parse(d.payload) : null;
      const tq = payload?.trade_quality;
      if (tq) {
        tradeQuality = `${tq.score}/100 · ${tq.recommendation}`;
        liquidity = `${tq.liquidity_label || "—"} · ${tq.historical_match_count || 0} matches`;
      }
      const ma = payload?.model_agreement;
      if (ma) {
        modelAgreement = `${pct(ma.agreement * 100)} ${ma.consensus} · ${ma.models_agree ? "agree" : "disagree"}`;
      }
    } catch (_) {}
    $("decisionTradeQuality").textContent = tradeQuality;
    $("decisionModelAgreement").textContent = modelAgreement;
    $("decisionLiquidity").textContent = liquidity;

    $("whyLabel").textContent =
      action === "NO_TRADE" ? "WHY NO TRADE" :
      action === "EXIT" ? "WHY EXIT" :
      action === "HOLD" ? "WHY HOLD" : "WHY BUY";
    $("decisionReason").textContent = d.reason || "No explanation recorded.";
    if (action === "NO_TRADE" && (d.primary_blocker || d.blocking_summary)) {
      $("decisionReason").textContent = d.primary_blocker || d.blocking_summary;
    } else if (d.blocking_summary && action === "NO_TRADE") {
      $("decisionReason").textContent = `${d.reason || "No trade."} · ${d.blocking_summary}`;
    }
    if (d.edge_gap_text) {
      $("decisionEdgeGap").textContent = d.edge_gap_text;
    }
    renderRequirementsPanel(d);
    renderReversalStatus(d.reversal_status);
  }

  function renderDecisions(decisions) {
    const body = $("decisionsBody");
    $("decisionsEmpty").hidden = decisions.length > 0;
    body.innerHTML = decisions.slice(0, 100).map((d) => `
      <tr>
        <td class="mono">${fmtTime(d.ts)}</td>
        <td>${tierBadge(d.action)}</td>
        <td class="mono">${d.ticker || "—"}</td>
        <td class="mono">${pct(d.up_probability)}</td>
        <td class="mono">${pct(d.executable_price)}</td>
        <td class="edge-pos mono">${pct(d.edge)}</td>
        <td class="mono">${pct(d.confidence)}</td>
        <td>${d.regime || "—"}</td>
        <td>${d.data_health || "—"}</td>
        <td>${reversalBadge(d.reversal_status)}</td>
        <td class="requirements-cell">${renderRequirementsCell(d)}</td>
        <td class="reason-cell">${(d.action || "").toUpperCase() === "NO_TRADE" && d.primary_blocker ? d.primary_blocker : (d.reason || "—")}</td>
      </tr>`).join("");
  }

  function renderStats(stats) {
    const live = stats.live_trades || 0;
    const dry = stats.dry_trades || 0;
    $("statTrades").textContent = String(live);
    $("statTradeSplit").textContent = `dry ${dry} · live ${live} · ${stats.exits || 0} exits`;
    $("statNotional").textContent = money(stats.notional_usd);
    $("statEdge").textContent = `${Number(stats.avg_edge_pct || stats.avg_edge || 0).toFixed(1)}%`;
    const pnl = stats.closed_pnl_usd || 0;
    $("statPnl").textContent = signedMoney(pnl);
    $("statPnl").className = `value ${pnl > 0 ? "edge-pos" : pnl < 0 ? "edge-neg" : ""}`;
    $("statWinLoss").textContent = `${stats.wins || 0} wins · ${stats.losses || 0} losses`;

    const decision = stats.last_decision;
    const scan = stats.last_scan;
    if (decision || scan) {
      $("mode").textContent = decision ? (decision.dry_run ? "PAPER" : "LIVE") : (scan.mode || "—");
      $("statSpot").textContent = decision
        ? `${decision.horizon || "bot"} · ${decision.action}`
        : "15m + 1h bots";
      $("lastScan").textContent = decision
        ? `Last decision ${fmtTime(decision.ts)} · ${decision.action} · ${decision.data_health}`
        : `Last scan ${fmtTime(scan.ts)} · ${scan.markets_scanned} mkts`;
      $("pulse").classList.remove("off");
    } else {
      $("mode").textContent = live > 0 ? "LIVE" : "IDLE";
      $("pulse").classList.add("off");
    }
  }

  function renderAnalytics(analytics) {
    if (!analytics) return;
    const fmt = (obj) =>
      Object.entries(obj || {})
        .map(([k, v]) => `${k}: ${typeof v === "number" ? (v * 100).toFixed(1) + "%" : v}`)
        .join("\n") || "—";
    $("analyticsTimeRemaining").textContent = fmt(analytics.win_rate_by_time_remaining);
    $("analyticsSession").textContent = fmt(analytics.win_rate_by_session);
    $("analyticsStrategy").textContent = Object.entries(analytics.profit_by_strategy || {})
      .map(([k, v]) => `${k}: $${Number(v).toFixed(2)}`)
      .join("\n") || "—";
    $("analyticsLossCauses").textContent = (analytics.largest_loss_causes || [])
      .map((c) => `${c.cause}: ${c.count}`)
      .join("\n") || "—";
  }

  async function refresh() {
    try {
      const [stats, trades, decisions, signals, scans, analytics] = await Promise.all([
        fetch("/api/stats").then((r) => {
          if (!r.ok) throw new Error(`stats HTTP ${r.status}`);
          return r.json();
        }),
        fetch("/api/trades?limit=300").then((r) => {
          if (!r.ok) throw new Error(`trades HTTP ${r.status}`);
          return r.json();
        }),
        fetch("/api/decisions?limit=100").then((r) => {
          if (!r.ok) throw new Error(`decisions HTTP ${r.status}`);
          return r.json();
        }),
        fetch("/api/signals?limit=100").then((r) => r.ok ? r.json() : { signals: [] }),
        fetch("/api/scans?limit=40").then((r) => r.ok ? r.json() : { scans: [] }),
        fetch("/api/analytics").then((r) => r.ok ? r.json() : null),
      ]);
      $("offlineBanner").hidden = true;
      state.trades = trades.trades || [];
      renderStats(stats);
      renderTrades();
      renderDecisions(decisions.decisions || []);
      renderCurrentDecision((decisions.decisions || [])[0]);
      renderSignals(signals.signals || []);
      renderScans(scans.scans || []);
      renderAnalytics(analytics);
    } catch (err) {
      $("pulse").classList.add("off");
      $("mode").textContent = "OFFLINE";
      $("offlineBanner").hidden = false;
      $("decisionReason").textContent =
        "Cannot reach the dashboard API. Open port 8787 from the Cursor Ports tab.";
      console.error(err);
    }
  }

  $("tradeFilters").addEventListener("click", (e) => {
    const btn = e.target.closest(".chip");
    if (!btn) return;
    state.filter = btn.dataset.filter;
    [...$("tradeFilters").children].forEach((c) => c.classList.toggle("active", c === btn));
    renderTrades();
  });

  refresh();
  setInterval(refresh, 3000);
})();
