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

  function actionClass(action) {
    if (action === "pass") return "act-pass";
    if (action === "block") return "act-block";
    return "";
  }

  function recClass(rec) {
    if (rec === "buy") return "rec-buy";
    if (rec === "skip") return "rec-skip";
    if (rec === "hold") return "rec-hold";
    if (rec === "exit") return "rec-exit";
    return "";
  }

  function qualityClass(q) {
    if (q == null) return "";
    if (q >= 80) return "q-high";
    if (q >= 50) return "q-mid";
    return "q-low";
  }

  function renderAssessments(rows) {
    const body = $("assessmentsBody");
    $("assessmentsEmpty").hidden = rows.length > 0;
    body.innerHTML = rows
      .map((row) => {
        const label = row.horizon ? `${row.asset} · ${row.horizon}` : row.asset;
        const tau =
          row.tau_left_min != null ? `${row.tau_left_min}m` : "—";
        const sidePoll =
          row.side_poll_pct != null ? `${Number(row.side_poll_pct).toFixed(0)}%` : "—";
        const netEdge =
          row.net_edge_cents != null ? `${row.net_edge_cents}¢` : "—";
        const ensemble =
          row.ensemble_pct != null ? `${Number(row.ensemble_pct).toFixed(0)}%` : "—";
        const quality =
          row.quality != null ? String(row.quality) : "—";
        return `
      <tr>
        <td><span class="asset-label">${label}</span></td>
        <td class="mono">${tau}</td>
        <td class="mono">${row.book || "—"}</td>
        <td><span class="gate-action ${actionClass(row.action)}">${row.action || "—"}</span></td>
        <td class="mono">${row.side || "—"}</td>
        <td class="mono">${sidePoll}</td>
        <td class="mono">${netEdge}</td>
        <td class="mono">${ensemble}</td>
        <td><span class="quality ${qualityClass(row.quality)}">${quality}</span></td>
        <td><span class="rec ${recClass(row.rec)}">${row.rec || "—"}</span></td>
        <td class="blocker-cell">${row.blocker || "—"}</td>
      </tr>`;
      })
      .join("");
  }

  function renderMiniStats(stats) {
    const el = $("miniStats");
    const pnl = stats.closed_pnl_usd || 0;
    el.innerHTML = `
      <article><span>Fills</span><strong>${stats.live_trades || 0} live</strong></article>
      <article><span>P/L</span><strong class="${pnl >= 0 ? "pos" : "neg"}">${signedMoney(pnl)}</strong></article>
      <article><span>Notional</span><strong>${money(stats.notional_usd)}</strong></article>
      <article><span>Avg edge</span><strong>${Number(stats.avg_edge_pct || stats.avg_edge || 0).toFixed(1)}%</strong></article>
    `;
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
    $("scanStatus").textContent = `Last scan: ${fmtClock(ts)} | ${mkts} market${mkts === 1 ? "" : "s"} | entries=${entries} exits=${exits}`;
    $("headerMode").textContent = payload.mode || "—";
    $("headerMode").className = `edge-mode ${(payload.mode || "").toLowerCase()}`;
    $("footerPulse").textContent = payload.mode === "LIVE" ? "live" : payload.mode === "PAPER" ? "paper" : "connected";
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
      renderRules(desk.rules);
      $("rulesSummary").textContent = desk.rules_summary || "—";
      renderAssessments(desk.assessments || []);
      renderMiniStats(desk.stats || {});
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
