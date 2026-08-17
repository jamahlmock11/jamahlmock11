(() => {
  const state = {
    autoRefresh: true,
    refreshTimer: null,
    intervalMs: 10000,
  };

  const $ = (id) => document.getElementById(id);

  function fmtClock(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString(undefined, {
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
    const v = Number(n);
    return `${v > 0 ? "+" : ""}${money(v)}`;
  }

  function formatAction(action) {
    const a = (action || "NO_TRADE").toUpperCase().replace(/_/g, " ");
    return a;
  }

  function actionCssClass(action) {
    const a = (action || "NO_TRADE").toLowerCase();
    if (a === "buy_up" || a === "buy_down") return "act-buy_up";
    if (a === "hold") return "act-hold";
    if (a === "exit") return "act-exit";
    return "act-no_trade";
  }

  function heroSkin(rec) {
    if (rec === "buy") return "hero-buy";
    if (rec === "hold") return "hero-hold";
    if (rec === "exit") return "hero-exit";
    return "hero-skip";
  }

  function whyLabel(rec, action) {
    const a = (action || "").toUpperCase();
    if (rec === "skip" || a === "NO_TRADE") return "Why no trade";
    if (a === "HOLD") return "Why hold";
    if (a === "EXIT") return "Why exit";
    if (rec === "buy") return "Why buy";
    return "Reason";
  }

  function renderRules(rules) {
    const grid = $("rulesGrid");
    if (!rules || !rules.length) {
      grid.innerHTML = '<div class="rules-loading">No rules loaded</div>';
      return;
    }
    grid.innerHTML = rules
      .map(
        (r) => `
      <article class="rule-chip">
        <span class="rule-key">${r.key}</span>
        <strong class="rule-value">${r.value}</strong>
      </article>`
      )
      .join("");
  }

  function renderDecisionHeroes(rows) {
    const container = $("decisionHeroes");
    if (!rows || !rows.length) {
      container.innerHTML =
        '<div class="hero-placeholder">No live decisions yet. Bots will populate this after the next scan.</div>';
      return;
    }

    container.innerHTML = rows
      .map((row) => {
        const action = row.decision_action || "NO_TRADE";
        const rec = row.rec || "skip";
        const tau = row.tau_left_min != null ? `${row.tau_left_min}m left` : "—";
        const netEdge =
          row.net_edge_cents != null ? `${row.net_edge_cents}¢` : "—";
        const ensemble =
          row.ensemble_pct != null ? `${Number(row.ensemble_pct).toFixed(0)}%` : "—";
        const quality = row.quality != null ? `${row.quality}%` : "—";
        const whyText = row.blocker || row.reason || "No blockers — requirements met.";
        const whyBlocked = rec === "skip" && row.blocker;
        const horizonLabel = row.horizon === "1h" ? "1 hour" : "15 minute";

        return `
      <article class="decision-hero ${heroSkin(rec)}">
        <div class="hero-top">
          <div class="hero-meta">
            <span class="hero-horizon">BTC · ${horizonLabel}</span>
            <span class="hero-ticker">${row.ticker || "—"}</span>
          </div>
          <span class="hero-tau">${tau}</span>
        </div>
        <div class="hero-action ${actionCssClass(action)}">${formatAction(action)}</div>
        <div class="hero-pick">
          <span class="hero-pick-label">Kalshi pick</span>
          <span class="hero-pick-value">${row.book || "—"}</span>
        </div>
        <div class="hero-metrics">
          <div class="hero-metric">
            <span>Net edge</span>
            <strong>${netEdge}</strong>
          </div>
          <div class="hero-metric">
            <span>Ensemble</span>
            <strong>${ensemble}</strong>
          </div>
          <div class="hero-metric">
            <span>Quality</span>
            <strong>${quality}</strong>
          </div>
        </div>
        <div class="hero-why">
          <span class="hero-why-label">${whyLabel(rec, action)}</span>
          <span class="hero-why-text ${whyBlocked ? "blocked" : ""}">${whyText}</span>
        </div>
      </article>`;
      })
      .join("");
  }

  function renderStatsStrip(stats) {
    const pnl = stats.closed_pnl_usd || 0;
    $("statFills").textContent = String(stats.live_trades || 0);
    const pnlEl = $("statPnl");
    pnlEl.textContent = signedMoney(pnl);
    pnlEl.className = `stat-value ${pnl > 0 ? "pos" : pnl < 0 ? "neg" : ""}`;
    $("statNotional").textContent = money(stats.notional_usd);
    $("statEdge").textContent = `${Number(stats.avg_edge_pct || stats.avg_edge || 0).toFixed(1)}%`;
  }

  function decisionBadgeClass(rec) {
    if (rec === "buy") return "buy";
    if (rec === "hold") return "hold";
    if (rec === "exit") return "exit";
    return "skip";
  }

  function renderAssessments(rows) {
    const body = $("assessmentsBody");
    $("assessmentsEmpty").hidden = rows.length > 0;
    body.innerHTML = rows
      .map((row) => {
        const label = row.horizon ? `${row.horizon}` : "—";
        const tau =
          row.tau_left_min != null ? `${row.tau_left_min}m` : "—";
        const netEdge =
          row.net_edge_cents != null ? `${row.net_edge_cents}¢` : "—";
        const ensemble =
          row.ensemble_pct != null ? `${Number(row.ensemble_pct).toFixed(0)}%` : "—";
        const actionLabel = formatAction(row.decision_action);
        return `
      <tr>
        <td><span class="asset-label">${label}</span></td>
        <td class="mono">${tau}</td>
        <td class="mono">${row.book || "—"}</td>
        <td><span class="decision-badge ${decisionBadgeClass(row.rec)}">${actionLabel}</span></td>
        <td class="mono">${netEdge}</td>
        <td class="mono">${ensemble}</td>
        <td class="blocker-cell">${row.blocker || "—"}</td>
      </tr>`;
      })
      .join("");
  }

  function renderTrades(trades) {
    const body = $("tradesBody");
    const rows = (trades || []).slice(0, 12);
    body.innerHTML = rows
      .map(
        (t) => `
      <tr>
        <td class="mono">${fmtTime(t.ts)}</td>
        <td>${t.horizon || "—"}</td>
        <td class="summary-cell">${t.summary || t.detail || "—"}</td>
        <td class="mono">${signedMoney(t.pnl_usd)}</td>
      </tr>`
      )
      .join("");
  }

  function renderDecisions(decisions) {
    const body = $("decisionsBody");
    body.innerHTML = (decisions || [])
      .slice(0, 20)
      .map((d) => {
        const tau =
          d.seconds_remaining != null
            ? `${(d.seconds_remaining / 60).toFixed(1)}m`
            : "—";
        const edge = d.edge != null ? `${(d.edge * 100).toFixed(0)}¢` : "—";
        const blocker = d.primary_blocker || d.blocking_summary || "—";
        return `
      <tr>
        <td class="mono">${fmtTime(d.ts)}</td>
        <td>${d.action || "—"}</td>
        <td class="mono">${tau}</td>
        <td class="mono">${edge}</td>
        <td class="blocker-cell">${blocker}</td>
      </tr>`;
      })
      .join("");
  }

  function updateScanStatus(payload) {
    const ts = payload.last_scan_ts;
    const mkts = payload.market_count ?? 0;
    const entries = payload.entries ?? 0;
    const exits = payload.exits ?? 0;
    $("scanStatus").textContent =
      `Last scan ${fmtClock(ts)} · ${mkts} market${mkts === 1 ? "" : "s"} · entries ${entries} · exits ${exits}`;
    $("headerMode").textContent = payload.mode || "—";
    $("headerMode").className = `edge-mode ${(payload.mode || "").toLowerCase()}`;
    $("footerPulse").textContent =
      payload.mode === "LIVE" ? "live" : payload.mode === "PAPER" ? "paper" : "connected";
  }

  async function refresh() {
    const btn = $("scanBtn");
    btn.disabled = true;
    try {
      const [desk, trades, decisionsResp] = await Promise.all([
        fetch("/api/edge-desk").then((r) => {
          if (!r.ok) throw new Error(`edge-desk HTTP ${r.status}`);
          return r.json();
        }),
        fetch("/api/trades?limit=50").then((r) => (r.ok ? r.json() : { trades: [] })),
        fetch("/api/decisions?limit=30").then((r) => (r.ok ? r.json() : { decisions: [] })),
      ]);

      $("offlineBanner").hidden = true;
      const assessments = desk.assessments || [];
      renderDecisionHeroes(assessments);
      renderStatsStrip(desk.stats || {});
      renderAssessments(assessments);
      renderRules(desk.rules);
      $("rulesSummary").textContent = desk.rules_summary || "—";
      renderTrades(trades.trades || []);
      renderDecisions(decisionsResp.decisions || []);
      updateScanStatus(desk);
    } catch (err) {
      $("offlineBanner").hidden = false;
      $("footerPulse").textContent = "offline";
      $("scanStatus").textContent = "Cannot reach dashboard API on port 8787.";
      console.error(err);
    } finally {
      btn.disabled = false;
    }
  }

  function scheduleRefresh() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    if (!state.autoRefresh) return;
    state.refreshTimer = setInterval(refresh, state.intervalMs);
  }

  $("scanBtn").addEventListener("click", () => refresh());
  $("autoRefresh").addEventListener("change", (e) => {
    state.autoRefresh = e.target.checked;
    scheduleRefresh();
  });

  refresh();
  scheduleRefresh();
})();
