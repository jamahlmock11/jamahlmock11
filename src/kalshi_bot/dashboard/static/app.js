(() => {
  const state = {
    autoRefresh: true,
    refreshTimer: null,
    intervalMs: 5000,
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
    return (action || "NO_TRADE").toUpperCase().replace(/_/g, " ");
  }

  function signalLabel(signal) {
    if (signal === "trade") return "IN TRADE";
    if (signal === "near") return "CLOSE TO TRADE";
    return "NO TRADE";
  }

  function signalClass(signal) {
    return `signal-${signal || "notrade"}`;
  }

  function requirementRowClass(status, blocking) {
    if (blocking) return "req-blocked";
    if (status === "pass") return "req-pass";
    if (status === "fail") return "req-fail";
    return "req-na";
  }

  function renderRequirementsList(requirements) {
    if (!requirements || !requirements.length) {
      return '<p class="req-empty">No requirement data yet.</p>';
    }
    return requirements
      .map((r) => {
        const icon =
          r.blocking ? "✕" : r.status === "pass" ? "✓" : r.status === "fail" ? "!" : "·";
        return `
        <div class="req-row ${requirementRowClass(r.status, r.blocking)}">
          <span class="req-icon">${icon}</span>
          <div class="req-copy">
            <strong>${r.label}</strong>
            <span>${r.detail || ""}</span>
          </div>
        </div>`;
      })
      .join("");
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

  function renderBotRules(containerId, rules) {
    const el = $(containerId);
    if (!el) return;
    if (!rules || !rules.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = rules
      .map(
        (r) =>
          `<span class="bot-rule-chip"><span class="rule-key">${r.key}</span> ${r.value}</span>`
      )
      .join("");
  }

  function buildHeroCard(row) {
    const action = row.decision_action || "NO_TRADE";
    const signal = row.signal || "notrade";
    const tau = row.tau_left_min != null ? `${row.tau_left_min}m left` : "—";
    const netEdge =
      row.net_edge_cents != null ? `${row.net_edge_cents}¢` : "—";
    const ensemble =
      row.ensemble_pct != null ? `${Number(row.ensemble_pct).toFixed(0)}%` : "—";
    const confidence =
      row.confidence_pct != null ? `${Number(row.confidence_pct).toFixed(0)}%` : "—";
    const quality = row.quality != null ? `${row.quality}%` : "—";
    const passFail = `${row.pass_count || 0}/${(row.pass_count || 0) + (row.fail_count || 0)}`;
    const edgeGap = row.edge_gap_text || "—";
    const whyText = row.blocker || row.reason || "All blocking gates cleared.";
    const botMode = row.dry_run ? "PAPER" : "LIVE";
    const brti =
      row.brti_price != null && row.strike != null
        ? `${money(row.brti_price)} / ${money(row.strike)}`
        : "—";
    const bookAsk =
      row.yes_ask != null && row.no_ask != null
        ? `Y ${(row.yes_ask * 100).toFixed(0)}¢ · N ${(row.no_ask * 100).toFixed(0)}¢`
        : "—";
    const positionText =
      row.has_position && row.position
        ? `${row.position.side} × ${row.position.quantity}`
        : "FLAT";
    const reqs = renderRequirementsList(row.requirements);
    const mispricingTag =
      row.horizon === "1h" && row.mispricing_enabled === false
        ? '<span class="hero-tag">Forecast only</span>'
        : "";
    const modelTag =
      row.model_version ? `<span class="hero-tag mono">${row.model_version}</span>` : "";

    const terminalBlock =
      row.terminal_mode && (row.terminal_forecast || row.terminal_p_yes != null)
        ? `
        <div class="hero-terminal">
          <div class="hero-terminal-title">Terminal forecast ${mispricingTag}</div>
          <div class="hero-terminal-grid">
            <span>BRTI ${row.brti_price != null ? money(row.brti_price) : "—"}</span>
            <span>Exp terminal ${row.expected_terminal_brti != null ? money(row.expected_terminal_brti) : "—"}</span>
            <span>P(YES) ${row.terminal_p_yes != null ? (row.terminal_p_yes * 100).toFixed(1) + "%" : "—"}</span>
            <span>P(NO) ${row.terminal_p_no != null ? (row.terminal_p_no * 100).toFixed(1) + "%" : "—"}</span>
            <span>YES edge ${row.yes_net_edge_pp != null ? row.yes_net_edge_pp + "pp" : "—"}</span>
            <span>NO edge ${row.no_net_edge_pp != null ? row.no_net_edge_pp + "pp" : "—"}</span>
            <span>Need ${row.required_edge_pp != null ? row.required_edge_pp + "pp" : "—"}</span>
          </div>
          <pre class="hero-explanation">${row.terminal_explanation || ""}</pre>
        </div>`
        : "";

    const reversalBlock =
      row.reversal_status && row.reversal_status.enabled
        ? `
        <div class="hero-reversal">
          <span class="hero-reversal-label">Lag reversal</span>
          <strong>${row.reversal_status.mode_label || "—"}</strong>
          <span>${row.reversal_status.summary || row.reversal_status.setup || ""}</span>
        </div>`
        : "";

    return `
      <article class="decision-hero ${signalClass(signal)}">
        <div class="hero-signal-bar">
          <span class="hero-signal-label">${signalLabel(signal)}</span>
          <span class="hero-bot-mode">${botMode}</span>
          ${modelTag}
        </div>
        <div class="hero-top">
          <div class="hero-meta">
            <span class="hero-ticker">${row.ticker || "—"}</span>
          </div>
          <span class="hero-tau">${tau}</span>
        </div>
        <div class="hero-action">${formatAction(action)}</div>
        <div class="hero-pick">
          <span class="hero-pick-label">Kalshi pick</span>
          <span class="hero-pick-value">${row.book || "—"}</span>
        </div>
        <div class="hero-metrics">
          <div class="hero-metric">
            <span>${row.mispricing_enabled === false ? "Side prob" : "Net edge"}</span>
            <strong>${netEdge}</strong>
          </div>
          <div class="hero-metric">
            <span>Need / gap</span>
            <strong class="edge-gap">${edgeGap}</strong>
          </div>
          <div class="hero-metric">
            <span>Ensemble</span>
            <strong>${ensemble}</strong>
          </div>
          <div class="hero-metric">
            <span>Confidence</span>
            <strong>${confidence}</strong>
          </div>
          <div class="hero-metric">
            <span>Reqs pass</span>
            <strong>${passFail}</strong>
          </div>
          <div class="hero-metric">
            <span>Quality</span>
            <strong>${quality}</strong>
          </div>
        </div>
        <div class="hero-context">
          <span>BRTI / strike <strong>${brti}</strong></span>
          <span>Book <strong>${bookAsk}</strong></span>
          <span>Position <strong>${positionText}</strong></span>
          <span>Health <strong>${row.data_health || "—"}</strong></span>
          <span>Regime <strong>${row.regime || "—"}</strong></span>
        </div>
        <div class="hero-why">
          <span class="hero-why-label">${signal === "notrade" ? "Why no trade" : "Status"}</span>
          <span class="hero-why-text">${whyText}</span>
        </div>
        ${reversalBlock}
        ${terminalBlock}
        <div class="requirements-panel">
          <div class="requirements-head">
            <h3>Trade requirements</h3>
            <span class="requirements-score">${passFail} pass · ${edgeGap}</span>
          </div>
          <div class="requirements-grid">${reqs}</div>
        </div>
      </article>`;
  }

  function renderBotHero(containerId, row, placeholder) {
    const container = $(containerId);
    if (!container) return;
    if (!row) {
      container.innerHTML = `<div class="hero-placeholder">${placeholder}</div>`;
      return;
    }
    container.innerHTML = buildHeroCard(row);
  }

  function renderStrikeCandidates(row) {
    const wrap = $("strikeCandidates1h");
    const body = $("strikeCandidatesBody");
    if (!wrap || !body) return;
    const candidates = row && row.strike_candidates ? row.strike_candidates : [];
    if (!candidates.length) {
      wrap.hidden = true;
      body.innerHTML = "";
      return;
    }
    wrap.hidden = false;
    const selectedTicker = row.ticker || "";
    body.innerHTML = candidates
      .map((c) => {
        const strike =
          c.strike != null ? money(c.strike).replace("$", "$") : "—";
        const action = formatAction(c.action || "NO_TRADE");
        const pYes =
          c.p_yes != null ? `${(c.p_yes * 100).toFixed(1)}%` : "—";
        const yesEdge =
          c.yes_net_edge != null ? `${(c.yes_net_edge * 100).toFixed(1)}pp` : "—";
        const noEdge =
          c.no_net_edge != null ? `${(c.no_net_edge * 100).toFixed(1)}pp` : "—";
        const need =
          c.required_edge != null ? `${(c.required_edge * 100).toFixed(0)}pp` : "—";
        const book =
          c.yes_ask != null && c.no_ask != null
            ? `Y ${(c.yes_ask * 100).toFixed(0)}¢ N ${(c.no_ask * 100).toFixed(0)}¢`
            : "—";
        const strong = c.strong_evidence ? "yes" : "—";
        const selected = c.ticker === selectedTicker ? "strike-selected" : "";
        return `
        <tr class="${selected}">
          <td class="mono">${strike}</td>
          <td>${action}</td>
          <td class="mono">${pYes}</td>
          <td class="mono">${yesEdge}</td>
          <td class="mono">${noEdge}</td>
          <td class="mono">${need}</td>
          <td class="mono">${book}</td>
          <td>${strong}</td>
        </tr>`;
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
        <td class="mono">${d.horizon || "15m"}</td>
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
    const mode = payload.mode || "—";
    $("scanStatus").textContent =
      `Dashboard live · last bot scan ${fmtClock(ts)} · ${mkts} market${mkts === 1 ? "" : "s"} · entries ${entries} · exits ${exits}`;
    $("headerMode").textContent = mode;
    $("headerMode").className = `edge-mode ${mode.toLowerCase()}`;
    $("footerPulse").textContent = `dashboard live · bots ${mode.toLowerCase()}`;
    $("feedPulse").className = `feed-pulse on ${mode.toLowerCase()}`;
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
      const row15m = desk.assessment_15m || null;
      const row1h = desk.assessment_1h || null;

      renderBotHero(
        "hero15m",
        row15m,
        "Waiting for 15m bot scan… (KXBTC15M polls every ~10s)"
      );
      renderBotHero(
        "hero1h",
        row1h,
        "Waiting for 1h bot scan… (KXBTCD terminal engine polls every ~10s)"
      );
      renderBotRules("rules15m", desk.rules_15m || []);
      renderBotRules("rules1h", desk.rules_1h || []);
      if (row15m) {
        $("summary15m").textContent = `${formatAction(row15m.decision_action)} · ${row15m.tau_left_min ?? "—"}m left · ${fmtClock(row15m.ts)}`;
        const b15 = $("badge15m");
        if (b15) b15.textContent = row15m.dry_run ? "15m PAPER" : "15m LIVE";
      } else {
        $("summary15m").textContent = desk.rules_summary_15m || "Waiting for 15m bot…";
      }
      if (row1h) {
        $("summary1h").textContent = `${formatAction(row1h.decision_action)} · ${row1h.tau_left_min ?? "—"}m left · ${fmtClock(row1h.ts)} — ${desk.rules_summary_1h || ""}`;
      } else {
        $("summary1h").textContent = desk.rules_summary_1h || "Waiting for 1h bot…";
      }
      renderStrikeCandidates(row1h);

      renderStatsStrip(desk.stats || {});
      renderRules(desk.rules);
      $("rulesSummary").textContent = desk.rules_summary || "—";
      renderTrades(trades.trades || []);
      renderDecisions(decisionsResp.decisions || []);
      updateScanStatus(desk);
    } catch (err) {
      $("offlineBanner").hidden = false;
      $("footerPulse").textContent = "dashboard offline";
      $("headerMode").textContent = "OFFLINE";
      $("headerMode").className = "edge-mode offline";
      $("feedPulse").className = "feed-pulse";
      $("scanStatus").textContent = "Cannot reach dashboard API on port 8790.";
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
