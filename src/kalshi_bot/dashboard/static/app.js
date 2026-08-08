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

  function pct(n, digits = 1) {
    return n == null ? "—" : `${(Number(n) * 100).toFixed(digits)}%`;
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
      ? `<span class="badge dry">dry</span>`
      : `<span class="badge live">live</span>`;
  }

  function filteredTrades() {
    const f = state.filter;
    return state.trades.filter((t) => {
      if (f === "all") return true;
      if (f === "dry") return !!t.dry_run;
      if (f === "live") return !t.dry_run;
      return t.strategy === f;
    });
  }

  function renderTrades() {
    const rows = filteredTrades();
    const body = $("tradesBody");
    $("tradeCount").textContent = `${rows.length} fill${rows.length === 1 ? "" : "s"}`;
    $("tradesEmpty").hidden = rows.length > 0;
    body.innerHTML = rows
      .map(
        (t) => `
      <tr>
        <td class="mono">${fmtTime(t.ts)}</td>
        <td>${t.strategy === "cross_venue_arb" ? "arb" : "mispricing"}</td>
        <td class="mono">${t.ticker || "—"}</td>
        <td>${sideBadge(t.side)}</td>
        <td class="mono">${Number(t.count || 0).toFixed(0)}</td>
        <td class="mono">${Number(t.price || 0).toFixed(2)}</td>
        <td class="mono">${money(t.notional)}</td>
        <td class="edge-pos mono">${Number(t.edge || 0).toFixed(1)}pp</td>
        <td>${tierBadge(t.confidence)}</td>
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
    $("whyLabel").textContent =
      action === "NO_TRADE" ? "WHY NO TRADE" :
      action === "EXIT" ? "WHY EXIT" :
      action === "HOLD" ? "WHY HOLD" : "WHY BUY";
    $("decisionReason").textContent = d.reason || "No explanation recorded.";
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
        <td class="reason-cell">${d.reason || "—"}</td>
      </tr>`).join("");
  }

  function renderStats(stats) {
    $("statTrades").textContent = String(stats.trades || 0);
    $("statTradeSplit").textContent = `dry ${stats.dry_trades || 0} · live ${stats.live_trades || 0}`;
    $("statNotional").textContent = money(stats.notional_usd);
    $("statEdge").textContent = `${Number(stats.avg_edge || 0).toFixed(1)}pp`;
    $("statSignals").textContent = String(stats.decisions || 0);

    const decision = stats.last_decision;
    const scan = stats.last_scan;
    if (decision || scan) {
      $("mode").textContent = decision ? (decision.dry_run ? "PAPER" : "LIVE") : (scan.mode || "—");
      $("statSpot").textContent = decision ? `REF ${money(decision.brti_price)}` : `BTC ${money(scan.spot)}`;
      $("lastScan").textContent = decision
        ? `Last decision ${fmtTime(decision.ts)} · ${decision.action} · ${decision.data_health}`
        : `Last scan ${fmtTime(scan.ts)} · ${scan.markets_scanned} mkts`;
      $("pulse").classList.remove("off");
    } else {
      $("mode").textContent = "IDLE";
      $("pulse").classList.add("off");
    }
  }

  async function refresh() {
    try {
      const [stats, trades, decisions, signals, scans] = await Promise.all([
        fetch("/api/stats").then((r) => r.json()),
        fetch("/api/trades?limit=150").then((r) => r.json()),
        fetch("/api/decisions?limit=100").then((r) => r.json()),
        fetch("/api/signals?limit=100").then((r) => r.json()),
        fetch("/api/scans?limit=40").then((r) => r.json()),
      ]);
      state.trades = trades.trades || [];
      renderStats(stats);
      renderTrades();
      renderDecisions(decisions.decisions || []);
      renderCurrentDecision((decisions.decisions || [])[0]);
      renderSignals(signals.signals || []);
      renderScans(scans.scans || []);
    } catch (err) {
      $("pulse").classList.add("off");
      $("mode").textContent = "OFFLINE";
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
