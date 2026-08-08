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

  function renderStats(stats) {
    $("statTrades").textContent = String(stats.trades || 0);
    $("statTradeSplit").textContent = `dry ${stats.dry_trades || 0} · live ${stats.live_trades || 0}`;
    $("statNotional").textContent = money(stats.notional_usd);
    $("statEdge").textContent = `${Number(stats.avg_edge || 0).toFixed(1)}pp`;
    $("statSignals").textContent = String(stats.signals || 0);

    const scan = stats.last_scan;
    if (scan) {
      $("mode").textContent = scan.mode || "—";
      $("statSpot").textContent = `BTC ${money(scan.spot)}`;
      $("lastScan").textContent = `Last scan ${fmtTime(scan.ts)} · ${scan.markets_scanned} mkts · ${scan.signal_count} signals`;
      $("pulse").classList.remove("off");
    } else {
      $("mode").textContent = "IDLE";
      $("pulse").classList.add("off");
    }
  }

  async function refresh() {
    try {
      const [stats, trades, signals, scans] = await Promise.all([
        fetch("/api/stats").then((r) => r.json()),
        fetch("/api/trades?limit=150").then((r) => r.json()),
        fetch("/api/signals?limit=100").then((r) => r.json()),
        fetch("/api/scans?limit=40").then((r) => r.json()),
      ]);
      state.trades = trades.trades || [];
      renderStats(stats);
      renderTrades();
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
