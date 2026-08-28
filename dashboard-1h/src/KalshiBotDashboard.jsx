import React, { useState, useEffect, useCallback } from "react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import {
  Play,
  Pause,
  Square,
  AlertTriangle,
  ChevronUp,
  ChevronDown,
  Minus,
} from "lucide-react";
import { fetchHourBotStatus, postHourBotControl } from "./api";

const FONT_LINK_ID = "kbd-fonts";
const DEFAULT_START_BANKROLL = 20;
const DEFAULT_POLL_MS = 500;

function useInjectFonts() {
  useEffect(() => {
    if (document.getElementById(FONT_LINK_ID)) return;
    const link = document.createElement("link");
    link.id = FONT_LINK_ID;
    link.rel = "stylesheet";
    link.href =
      "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap";
    document.head.appendChild(link);
  }, []);
}

function fmtElapsed(ms) {
  const totalMin = Math.floor(ms / 60000);
  if (totalMin < 60) return `${totalMin}m`;
  return `${Math.floor(totalMin / 60)}h ${totalMin % 60}m`;
}

const LOG_STYLES = {
  scan: { dot: "bg-[#767C86]", text: "text-[#767C86]" },
  signal: { dot: "bg-[#4CC9F0]", text: "text-[#B9EFFB]" },
  reject: { dot: "bg-[#F0A93D]", text: "text-[#F0A93D]" },
  fill: { dot: "bg-[#33D693]", text: "text-[#33D693]" },
  settle: { dot: "bg-[#33D693]", text: "text-[#B7F3DB]" },
  settle_win: { dot: "bg-[#33D693]", text: "text-[#33D693]" },
  settle_loss: { dot: "bg-[#F2495C]", text: "text-[#F2495C]" },
  exit: { dot: "bg-[#F0A93D]", text: "text-[#F0A93D]" },
};

function rowStyle(row) {
  if (row.kind === "settle") {
    const won = row.won ?? row.detail?.won;
    if (won === true) return LOG_STYLES.settle_win;
    if (won === false) return LOG_STYLES.settle_loss;
  }
  return LOG_STYLES[row.kind] ?? LOG_STYLES.scan;
}

function sortMarketsForDisplay(markets) {
  if (!markets?.length) return [];
  return [...markets].sort((a, b) => {
    const rank = (m) => {
      if (m.tradeCandidate) return 0;
      if (m.isExtremeQuote) return 3;
      if (m.isAtm) return 1;
      return 2;
    };
    const ra = rank(a);
    const rb = rank(b);
    if (ra !== rb) return ra - rb;
    const da = Number(a.distFromSpot ?? 9_999_999);
    const db = Number(b.distFromSpot ?? 9_999_999);
    if (da !== db) return da - db;
    return String(a.ticker).localeCompare(String(b.ticker));
  });
}

function marketBadges(m) {
  const badges = [];
  if (m.tradeCandidate) {
    badges.push({ label: "Signal", className: "bg-[#4CC9F0]/15 text-[#4CC9F0]" });
  } else if (m.isAtm) {
    badges.push({ label: "ATM", className: "bg-[#33D693]/15 text-[#33D693]" });
  }
  if (m.isExtremeQuote && !m.tradeCandidate) {
    badges.push({ label: "1/99", className: "bg-[#767C86]/15 text-[#767C86]" });
  }
  return badges;
}

function SettlementOutcome({ row }) {
  if (row.kind !== "settle") return <span className="text-[#767C86]">—</span>;
  const won = row.won ?? row.detail?.won;
  const outcome = row.outcome || row.detail?.outcome || (won === true ? "WIN" : won === false ? "LOSS" : "");
  if (!outcome) return <span className="text-[#767C86]">—</span>;
  const isWin = won === true || outcome === "WIN";
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide ${
        isWin ? "bg-[#33D693]/15 text-[#33D693]" : "bg-[#F2495C]/15 text-[#F2495C]"
      }`}
    >
      {isWin ? "Win" : "Loss"}
    </span>
  );
}

function emptyState() {
  return {
    mode: "paper",
    running: true,
    estop: false,
    series: "KXBTC",
    btcSpot: 0,
    bankroll: DEFAULT_START_BANKROLL,
    startingBankroll: DEFAULT_START_BANKROLL,
    dayPnl: 0,
    unrealized: 0,
    equityHistory: [],
    dailyEntriesUsed: 0,
    winsToday: 0,
    lossesToday: 0,
    sumWinDollars: 0,
    sumLossDollars: 0,
    feesPaidToday: 0,
    feesPaidTotal: 0,
    cumPnlInception: 0,
    pnlBySide: { yes: 0, no: 0 },
    peakEquity: DEFAULT_START_BANKROLL,
    markets: [],
    positions: [],
    logs: [],
    guardrails: {
      dailyLossLimit: 25,
      maxOpenPositions: 3,
      maxCapitalDeployed: 15,
      dailyEntryBudget: 20,
      openPositionsCount: 0,
      capitalDeployed: 0,
    },
    currentHour: { active: false, message: "Waiting for bot status…" },
    journal: [],
    pollIntervalMs: DEFAULT_POLL_MS,
    updatedAt: "",
  };
}

function Mono({ children, className = "" }) {
  return <span className={`font-[IBM_Plex_Mono] ${className}`}>{children}</span>;
}

function Panel({ title, eyebrow, right, children, className = "" }) {
  return (
    <div className={`bg-[#131519] border border-[#24272C] rounded-xl flex flex-col ${className}`}>
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#24272C]">
        <div>
          {eyebrow && (
            <div className="text-[10px] tracking-[0.18em] uppercase text-[#767C86] font-[IBM_Plex_Mono] mb-0.5">
              {eyebrow}
            </div>
          )}
          <div className="text-sm font-semibold text-[#E9E7E2] font-[Space_Grotesk]">{title}</div>
        </div>
        {right}
      </div>
      <div className="flex-1 min-h-0">{children}</div>
    </div>
  );
}

function StatBlock({ label, value, tone = "neutral", sub }) {
  const toneColor =
    tone === "pos" ? "text-[#33D693]" : tone === "neg" ? "text-[#F2495C]" : "text-[#E9E7E2]";
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] tracking-[0.14em] uppercase text-[#767C86] font-[IBM_Plex_Mono]">
        {label}
      </span>
      <span className={`text-2xl font-[Space_Grotesk] font-semibold ${toneColor}`}>{value}</span>
      {sub && <span className="text-[11px] text-[#767C86] font-[IBM_Plex_Mono]">{sub}</span>}
    </div>
  );
}

function GuardrailBar({ label, used, total, format = (v) => v, dangerAt = 0.85 }) {
  const pct = total > 0 ? Math.min(1, used / total) : 0;
  const color = pct >= dangerAt ? "#F2495C" : pct >= 0.6 ? "#F0A93D" : "#33D693";
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline justify-between">
        <span className="text-xs text-[#B7BAC0] font-[IBM_Plex_Mono]">{label}</span>
        <span className="text-xs font-[IBM_Plex_Mono]" style={{ color }}>
          {format(used)} / {format(total)}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-[#1A1D22] overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct * 100}%`, backgroundColor: color }}
        />
      </div>
    </div>
  );
}

function CountdownRing({ expiresAt, windowMin = 60 }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const remainingMs = Math.max(0, expiresAt - now);
  const remainingMin = remainingMs / 60000;
  const frac = Math.max(0, Math.min(1, remainingMin / windowMin));
  const size = 44;
  const stroke = 4;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const urgent = remainingMin <= 10;
  const color = urgent ? "#F2495C" : remainingMin <= 20 ? "#F0A93D" : "#4CC9F0";
  const mm = Math.floor(remainingMs / 60000);
  const ss = Math.floor((remainingMs % 60000) / 1000);

  return (
    <div className="relative flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className={urgent ? "animate-pulse" : ""}>
        <circle cx={size / 2} cy={size / 2} r={r} stroke="#24272C" strokeWidth={stroke} fill="none" />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          stroke={color}
          strokeWidth={stroke}
          fill="none"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - frac)}
          strokeLinecap="round"
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: "stroke-dashoffset 1s linear" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center leading-none">
        <span className="text-[10px] font-[IBM_Plex_Mono] font-medium" style={{ color }}>
          {mm}:{String(ss).padStart(2, "0")}
        </span>
      </div>
    </div>
  );
}

function DeltaTag({ value }) {
  if (value === 0) return <Minus size={12} className="text-[#767C86]" />;
  const pos = value > 0;
  const Icon = pos ? ChevronUp : ChevronDown;
  return (
    <span className={`inline-flex items-center text-[11px] font-[IBM_Plex_Mono] ${pos ? "text-[#33D693]" : "text-[#F2495C]"}`}>
      <Icon size={12} />
      {Math.abs(value)}c
    </span>
  );
}

function TickerTape({ markets }) {
  const ordered = sortMarketsForDisplay(markets).filter((m) => m.isAtm || m.tradeCandidate || !m.isExtremeQuote);
  if (!ordered.length) return null;
  const row = [...ordered, ...ordered, ...ordered];
  return (
    <div className="relative overflow-hidden border-b border-[#24272C] bg-[#0D0F12] h-9 flex items-center">
      <div className="flex gap-10 whitespace-nowrap animate-[tape_28s_linear_infinite] px-4">
        {row.map((m, i) => (
          <span key={i} className="text-[12px] font-[IBM_Plex_Mono] text-[#9AA0A8] flex items-center gap-2">
            <span className="text-[#E9E7E2]">{m.ticker.split("-").slice(-1)[0]}</span>
            <span className="text-[#33D693]">Y{m.yesBid ?? 0}/{m.yesAsk ?? 0}¢</span>
            <span className="text-[#F2495C]">N{m.noBid ?? 0}/{m.noAsk ?? 0}¢</span>
            <span className="text-[#767C86]">{Number(m.impliedProb || 0).toFixed(0)}% impl</span>
          </span>
        ))}
      </div>
      <style>{`
        @keyframes tape { from { transform: translateX(0); } to { transform: translateX(-33.333%); } }
      `}</style>
    </div>
  );
}

function LivePricesTable({ markets }) {
  const ordered = sortMarketsForDisplay(markets);
  if (!ordered.length) {
    return <div className="text-[#767C86] text-xs py-6 text-center">No live quotes yet.</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] font-[IBM_Plex_Mono]">
        <thead>
          <tr className="text-[#767C86] border-b border-[#24272C]">
            <th className="text-left py-2 pr-3 font-normal">Contract</th>
            <th className="text-right py-2 px-2 font-normal">Yes bid</th>
            <th className="text-right py-2 px-2 font-normal">Yes ask</th>
            <th className="text-right py-2 px-2 font-normal">No bid</th>
            <th className="text-right py-2 px-2 font-normal">No ask</th>
            <th className="text-right py-2 px-2 font-normal">Spread</th>
            <th className="text-right py-2 px-2 font-normal">Impl %</th>
            <th className="text-right py-2 pl-2 font-normal">Depth Y/N</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((m) => {
            const badges = marketBadges(m);
            const dimmed = m.isExtremeQuote && !m.tradeCandidate;
            return (
            <tr
              key={m.ticker}
              className={`border-b border-[#1A1D22] last:border-0 hover:bg-[#0D0F12] ${
                dimmed ? "opacity-45" : ""
              } ${m.tradeCandidate ? "bg-[#4CC9F0]/5" : ""}`}
            >
              <td className="py-2 pr-3 text-[#E9E7E2]">
                <div className="flex items-center gap-2">
                  <div className="font-medium">{m.ticker.split("-").slice(-1)[0]}</div>
                  {badges.map((badge) => (
                    <span
                      key={badge.label}
                      className={`px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wide ${badge.className}`}
                    >
                      {badge.label}
                    </span>
                  ))}
                </div>
                {m.subtitle && <div className="text-[10px] text-[#767C86] truncate max-w-[220px]">{m.subtitle}</div>}
                {m.distFromSpot != null && (
                  <div className="text-[10px] text-[#767C86]">${Number(m.strike || 0).toLocaleString()} · {Number(m.distFromSpot).toFixed(0)} from spot</div>
                )}
              </td>
              <td className="py-2 px-2 text-right text-[#33D693]">{m.yesBid ?? "—"}¢</td>
              <td className="py-2 px-2 text-right text-[#33D693]">{m.yesAsk ?? "—"}¢</td>
              <td className="py-2 px-2 text-right text-[#F2495C]">{m.noBid ?? "—"}¢</td>
              <td className="py-2 px-2 text-right text-[#F2495C]">{m.noAsk ?? "—"}¢</td>
              <td className="py-2 px-2 text-right text-[#B7BAC0]">{m.spread ?? 0}¢</td>
              <td className="py-2 px-2 text-right text-[#4CC9F0]">{Number(m.impliedProb || 0).toFixed(1)}%</td>
              <td className="py-2 pl-2 text-right text-[#767C86]">
                {m.depthYes ?? 0}/{m.depthNo ?? 0}
              </td>
            </tr>
          );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DetailJournal({ journal, logs, winsToday, lossesToday }) {
  const [filter, setFilter] = useState("all");
  const [expanded, setExpanded] = useState(null);
  const rows = (journal?.length ? journal : logs) || [];
  const settlements = rows.filter((row) => row.kind === "settle");
  const settleWins = settlements.filter((row) => row.won ?? row.detail?.won).length;
  const settleLosses = settlements.filter((row) => {
    const won = row.won ?? row.detail?.won;
    return won === false;
  }).length;
  const filtered = rows.filter((row) => {
    if (filter === "all") return true;
    if (filter === "win") return row.kind === "settle" && (row.won ?? row.detail?.won) === true;
    if (filter === "loss") return row.kind === "settle" && (row.won ?? row.detail?.won) === false;
    return row.kind === filter;
  });
  const filters = ["all", "signal", "fill", "exit", "settle", "win", "loss", "reject", "scan"];

  return (
    <div className="flex flex-col h-full min-h-[320px]">
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 pt-3 pb-2 border-b border-[#24272C]">
        <div className="flex flex-wrap gap-2">
          {filters.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2.5 py-1 rounded-full text-[10px] uppercase tracking-wide border transition-colors ${
                filter === f
                  ? "border-[#4CC9F0]/50 bg-[#4CC9F0]/10 text-[#4CC9F0]"
                  : "border-[#24272C] text-[#767C86] hover:border-[#3A3E45]"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 text-[10px] font-[IBM_Plex_Mono]">
          <span className="text-[#33D693]">{settleWins}W</span>
          <span className="text-[#767C86]">/</span>
          <span className="text-[#F2495C]">{settleLosses}L</span>
          <span className="text-[#767C86]">settlements</span>
          {(winsToday > 0 || lossesToday > 0) && (
            <span className="text-[#767C86]">· today {winsToday}W/{lossesToday}L</span>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto px-4 pb-4">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-[#131519]">
            <tr className="text-[#767C86] border-b border-[#24272C]">
              <th className="text-left py-2 pr-2 font-normal">Time</th>
              <th className="text-left py-2 pr-2 font-normal">Kind</th>
              <th className="text-left py-2 pr-2 font-normal">Outcome</th>
              <th className="text-left py-2 pr-2 font-normal">Ticker</th>
              <th className="text-left py-2 pr-2 font-normal">Side</th>
              <th className="text-right py-2 pr-2 font-normal">Px</th>
              <th className="text-left py-2 font-normal">Detail</th>
            </tr>
          </thead>
          <tbody>
            {!filtered.length && (
              <tr>
                <td colSpan={7} className="text-[#767C86] py-8 text-center">
                  No journal entries for this filter.
                </td>
              </tr>
            )}
            {filtered.map((row) => {
              const style = rowStyle(row);
              const when = new Date(row.time);
              const open = expanded === row.id;
              const pnl = row.detail?.pnl;
              return (
                <React.Fragment key={row.id}>
                  <tr
                    className="border-b border-[#1A1D22] hover:bg-[#0D0F12] cursor-pointer"
                    onClick={() => setExpanded(open ? null : row.id)}
                  >
                    <td className="py-2 pr-2 whitespace-nowrap text-[#767C86] align-top">
                      {when.toLocaleTimeString([], { hour12: false })}
                      <div className="text-[10px]">{when.toLocaleDateString()}</div>
                    </td>
                    <td className="py-2 pr-2 align-top">
                      <span className={`inline-flex items-center gap-1 ${style.text}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`} />
                        {row.kind}
                      </span>
                    </td>
                    <td className="py-2 pr-2 align-top">
                      <SettlementOutcome row={row} />
                    </td>
                    <td className="py-2 pr-2 align-top text-[#E9E7E2]">
                      {row.ticker ? row.ticker.split("-").slice(-1)[0] : "—"}
                    </td>
                    <td className="py-2 pr-2 align-top uppercase text-[#B7BAC0]">{row.side || "—"}</td>
                    <td className="py-2 pr-2 align-top text-right text-[#E9E7E2]">
                      {row.price != null ? `${row.price}¢` : "—"}
                    </td>
                    <td className={`py-2 align-top ${style.text}`}>
                      {row.text}
                      {row.kind === "settle" && pnl != null && (
                        <span className={`ml-2 font-semibold ${pnl >= 0 ? "text-[#33D693]" : "text-[#F2495C]"}`}>
                          ({pnl >= 0 ? "+" : ""}${Number(pnl).toFixed(2)})
                        </span>
                      )}
                      {row.kind === "settle" && row.detail?.marketResult && (
                        <span className="ml-2 text-[#767C86]">→ market {row.detail.marketResult}</span>
                      )}
                    </td>
                  </tr>
                  {open && row.detail && Object.keys(row.detail).length > 0 && (
                    <tr className="bg-[#0D0F12]">
                      <td colSpan={7} className="py-2 px-2 text-[10px] text-[#9AA0A8] font-[IBM_Plex_Mono]">
                        <pre className="whitespace-pre-wrap break-all">{JSON.stringify(row.detail, null, 2)}</pre>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function KalshiBotDashboard() {
  useInjectFonts();

  const [data, setData] = useState(emptyState);
  const [estopArmed, setEstopArmed] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const [lastFetchMs, setLastFetchMs] = useState(0);

  const pollMs = data.pollIntervalMs || DEFAULT_POLL_MS;

  const refresh = useCallback(async () => {
    const started = Date.now();
    try {
      const payload = await fetchHourBotStatus();
      setData((prev) => ({ ...prev, ...payload }));
      setConnected(true);
      setError(null);
      setLastFetchMs(Date.now() - started);
    } catch (err) {
      setConnected(false);
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, pollMs);
    return () => clearInterval(id);
  }, [refresh, pollMs]);

  const sendControl = async (payload) => {
    await postHourBotControl(payload);
    await refresh();
  };

  const {
    mode,
    running,
    estop,
    series,
    btcSpot,
    bankroll,
    startingBankroll,
    dayPnl,
    unrealized,
    equityHistory,
    dailyEntriesUsed,
    winsToday,
    lossesToday,
    sumWinDollars,
    sumLossDollars,
    feesPaidToday,
    feesPaidTotal,
    cumPnlInception,
    pnlBySide,
    peakEquity,
    markets,
    positions,
    logs,
    guardrails,
    currentHour,
    journal,
    updatedAt,
    pollIntervalMs,
  } = data;

  const dataAgeMs = updatedAt ? Math.max(0, Date.now() - new Date(updatedAt).getTime()) : null;

  const startBankroll = Number(startingBankroll ?? DEFAULT_START_BANKROLL);
  const netEquity = bankroll + unrealized;
  const equityFmt = (v) => `$${Number(v || 0).toFixed(2)}`;
  const displayMarkets = sortMarketsForDisplay(markets);
  const currentDrawdown = peakEquity - netEquity;
  const tradesToday = winsToday + lossesToday;
  const winRate = tradesToday > 0 ? winsToday / tradesToday : 0;
  const avgWin = winsToday > 0 ? sumWinDollars / winsToday : 0;
  const avgLoss = lossesToday > 0 ? sumLossDollars / lossesToday : 0;

  return (
    <div className="min-h-screen w-full bg-[#0A0B0D] text-[#E9E7E2] font-[IBM_Plex_Mono] flex flex-col">
      <TickerTape markets={displayMarkets} />

      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 border-b border-[#24272C]">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span
              className={`w-2 h-2 rounded-full ${
                connected && running && !estop ? "bg-[#33D693] animate-pulse" : "bg-[#767C86]"
              }`}
            />
            <span className="font-[Space_Grotesk] font-semibold text-base tracking-tight">
              {series} <span className="text-[#767C86] font-normal">/ hourly direction bot</span>
            </span>
          </div>
          {!connected && (
            <span className="text-[11px] text-[#F0A93D]">
              {error ? `offline — ${error}` : "connecting…"}
            </span>
          )}
          {connected && (
            <span className="text-[10px] text-[#767C86] font-[IBM_Plex_Mono]">
              refresh {pollIntervalMs || DEFAULT_POLL_MS}ms · fetch {lastFetchMs}ms
              {dataAgeMs != null ? ` · data ${dataAgeMs < 1000 ? `${dataAgeMs}ms` : `${(dataAgeMs / 1000).toFixed(1)}s`} old` : ""}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center bg-[#131519] border border-[#24272C] rounded-full p-1 text-xs">
            <button
              onClick={() => sendControl({ mode: "paper" })}
              className={`px-3 py-1 rounded-full transition-colors ${
                mode === "paper" ? "bg-[#1A1D22] text-[#4CC9F0]" : "text-[#767C86]"
              }`}
            >
              Paper
            </button>
            <button
              onClick={() => sendControl({ mode: "live" })}
              className={`px-3 py-1 rounded-full transition-colors ${
                mode === "live" ? "bg-[#1A1D22] text-[#F0A93D]" : "text-[#767C86]"
              }`}
            >
              Live
            </button>
          </div>

          <button
            onClick={() => sendControl({ running: !running, estop: false })}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-[#24272C] bg-[#131519] text-xs hover:border-[#3A3E45] transition-colors"
          >
            {running && !estop ? <Pause size={13} /> : <Play size={13} />}
            {running && !estop ? "Pause scanning" : "Resume"}
          </button>

          {!estopArmed ? (
            <button
              onClick={() => setEstopArmed(true)}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-[#F2495C]/40 bg-[#F2495C]/10 text-[#F2495C] text-xs hover:bg-[#F2495C]/20 transition-colors"
            >
              <Square size={12} />
              Emergency stop
            </button>
          ) : (
            <div className="flex items-center gap-1.5">
              <button
                onClick={async () => {
                  await sendControl({ estop: true, running: false });
                  setEstopArmed(false);
                }}
                className="px-3 py-1.5 rounded-full bg-[#F2495C] text-[#0A0B0D] text-xs font-semibold"
              >
                Confirm stop
              </button>
              <button
                onClick={() => setEstopArmed(false)}
                className="px-3 py-1.5 rounded-full border border-[#24272C] text-[#767C86] text-xs"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      </div>

      {mode === "live" && (
        <div className="flex items-center gap-2 px-5 py-2 bg-[#F0A93D]/10 border-b border-[#F0A93D]/30 text-[#F0A93D] text-xs">
          <AlertTriangle size={13} />
          Live mode — real orders would be sent to your Kalshi account.
        </div>
      )}

      {estop && (
        <div className="flex items-center gap-2 px-5 py-2 bg-[#F2495C]/10 border-b border-[#F2495C]/30 text-[#F2495C] text-xs">
          <Square size={13} />
          Emergency stop active — scanning halted.
        </div>
      )}

      <div className="px-5 py-3 border-b border-[#24272C] bg-[#0D0F12]">
        <div className="text-[10px] tracking-[0.18em] uppercase text-[#767C86] font-[IBM_Plex_Mono] mb-1">
          Current hour
        </div>
        {currentHour?.active ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span className="font-[Space_Grotesk] font-semibold text-[#E9E7E2]">
              {currentHour.eventTicker}
            </span>
            <span className="text-[#4CC9F0] font-[IBM_Plex_Mono] text-xs">
              {currentHour.minutesRemaining}m to close
            </span>
            <span className="text-[#767C86] font-[IBM_Plex_Mono] text-xs">
              {currentHour.contractsInWindow} strikes in window · scanned {currentHour.marketsScanned}
            </span>
            {currentHour.sampleTicker && (
              <span className="text-[#767C86] font-[IBM_Plex_Mono] text-xs truncate max-w-md">
                nearest: {currentHour.sampleTicker}
              </span>
            )}
          </div>
        ) : (
          <div className="text-sm text-[#F0A93D] font-[IBM_Plex_Mono]">
            {currentHour?.message || "No active hourly window right now"}
          </div>
        )}
      </div>

      <div className="flex-1 grid grid-cols-1 xl:grid-cols-3 gap-4 p-4">
        <Panel title="P&L & equity curve" eyebrow="Real-time" className="xl:col-span-2 min-h-[320px]">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 px-4 pt-4">
            <StatBlock label="Bankroll" value={equityFmt(bankroll)} />
            <StatBlock
              label="Day realized P&L"
              value={`${dayPnl >= 0 ? "+" : ""}${equityFmt(dayPnl)}`}
              tone={dayPnl >= 0 ? "pos" : "neg"}
            />
            <StatBlock
              label="Unrealized"
              value={`${unrealized >= 0 ? "+" : ""}${equityFmt(unrealized)}`}
              tone={unrealized >= 0 ? "pos" : "neg"}
            />
            <StatBlock label="Net equity" value={equityFmt(netEquity)} sub={`start $${startBankroll.toFixed(2)}`} />
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 px-4 pt-4 mt-4 border-t border-[#1A1D22]">
            <StatBlock
              label="Win rate"
              value={`${(winRate * 100).toFixed(0)}%`}
              sub={`${winsToday}W / ${lossesToday}L today`}
              tone={winRate >= 0.5 ? "pos" : "neg"}
            />
            <StatBlock label="Avg win / loss" value={`$${avgWin.toFixed(2)} / $${avgLoss.toFixed(2)}`} />
            <StatBlock
              label="Max drawdown"
              value={`-$${Math.max(0, currentDrawdown).toFixed(2)}`}
              tone={currentDrawdown > 3 ? "neg" : "neutral"}
              sub={`peak $${Number(peakEquity || 0).toFixed(2)}`}
            />
            <StatBlock
              label="Cum. P&L (inception)"
              value={`${cumPnlInception >= 0 ? "+" : ""}$${Number(cumPnlInception || 0).toFixed(2)}`}
              tone={cumPnlInception >= 0 ? "pos" : "neg"}
              sub={`fees paid $${Number(feesPaidTotal || 0).toFixed(2)}`}
            />
          </div>

          <div className="px-4 pt-4">
            <div className="flex items-baseline justify-between mb-1.5">
              <span className="text-[11px] text-[#767C86] font-[IBM_Plex_Mono]">P&L by side (since inception)</span>
              <span className="text-[11px] font-[IBM_Plex_Mono] text-[#767C86]">
                fees today ${Number(feesPaidToday || 0).toFixed(2)}
              </span>
            </div>
            <div className="flex items-center gap-2 text-[11px] font-[IBM_Plex_Mono]">
              <span className="w-8 text-[#33D693]">Yes</span>
              <div className="flex-1 h-2 rounded-full bg-[#1A1D22] overflow-hidden">
                <div
                  className="h-full bg-[#33D693] rounded-full"
                  style={{
                    width: `${Math.min(100, (Math.abs(pnlBySide?.yes || 0) / (Math.abs(pnlBySide?.yes || 0) + Math.abs(pnlBySide?.no || 0) || 1)) * 100)}%`,
                  }}
                />
              </div>
              <span className="w-16 text-right text-[#33D693]">
                {(pnlBySide?.yes || 0) >= 0 ? "+" : ""}${Number(pnlBySide?.yes || 0).toFixed(2)}
              </span>
            </div>
            <div className="flex items-center gap-2 text-[11px] font-[IBM_Plex_Mono] mt-1">
              <span className="w-8 text-[#F2495C]">No</span>
              <div className="flex-1 h-2 rounded-full bg-[#1A1D22] overflow-hidden">
                <div
                  className="h-full bg-[#F2495C] rounded-full"
                  style={{
                    width: `${Math.min(100, (Math.abs(pnlBySide?.no || 0) / (Math.abs(pnlBySide?.yes || 0) + Math.abs(pnlBySide?.no || 0) || 1)) * 100)}%`,
                  }}
                />
              </div>
              <span className="w-16 text-right text-[#F2495C]">
                {(pnlBySide?.no || 0) >= 0 ? "+" : ""}${Number(pnlBySide?.no || 0).toFixed(2)}
              </span>
            </div>
          </div>

          <div className="h-52 px-2 pb-2 pt-3">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={equityHistory?.length ? equityHistory : [{ t: 0, v: bankroll }]} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
                <defs>
                  <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#33D693" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#33D693" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#1A1D22" vertical={false} />
                <XAxis dataKey="t" hide />
                <YAxis
                  domain={["dataMin - 1", "dataMax + 1"]}
                  tick={{ fill: "#767C86", fontSize: 10 }}
                  width={40}
                  tickFormatter={(v) => `$${v.toFixed(0)}`}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  contentStyle={{ background: "#131519", border: "1px solid #24272C", borderRadius: 8, fontSize: 12 }}
                  labelFormatter={() => ""}
                  formatter={(v) => [`$${Number(v).toFixed(2)}`, "balance"]}
                />
                <Area type="monotone" dataKey="v" stroke="#33D693" strokeWidth={2} fill="url(#equityFill)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Panel>

        <Panel title="Risk & exposure guardrails" eyebrow="Live limits">
          <div className="flex flex-col gap-4 p-4">
            <GuardrailBar
              label="Capital deployed"
              used={guardrails?.capitalDeployed || 0}
              total={guardrails?.maxCapitalDeployed || 15}
              format={(v) => `$${Number(v).toFixed(2)}`}
            />
            <GuardrailBar
              label="Open positions"
              used={guardrails?.openPositionsCount || 0}
              total={guardrails?.maxOpenPositions || 3}
              format={(v) => v}
            />
            <GuardrailBar
              label="Hourly contract budget"
              used={guardrails?.hourContractsUsed || 0}
              total={guardrails?.hourMaxContracts || 2}
              format={(v) => `${v} ct`}
              dangerAt={1.0}
            />
            <GuardrailBar
              label="Contracts open now"
              used={guardrails?.hourContractsOpen || 0}
              total={guardrails?.hourMaxContracts || 2}
              format={(v) => `${v} ct`}
            />
            <GuardrailBar
              label="Daily new-entry budget"
              used={dailyEntriesUsed || 0}
              total={guardrails?.dailyEntryBudget || 20}
              format={(v) => v}
            />
            <GuardrailBar
              label="Daily loss limit used"
              used={Math.max(0, -(dayPnl || 0))}
              total={guardrails?.dailyLossLimit || 25}
              format={(v) => `$${Number(v).toFixed(2)}`}
              dangerAt={0.7}
            />
            {guardrails?.stopLoss?.enabled && (
              <div className="mt-1 pt-3 border-t border-[#24272C] text-[11px] text-[#767C86] leading-relaxed space-y-1">
                <div className="text-[#B7BAC0] font-medium">Stop-loss profile</div>
                <div>
                  Premium stop: -{guardrails.stopLoss.cents}¢ or -{Math.round((guardrails.stopLoss.pct || 0) * 100)}%
                </div>
                <div>
                  Take profit: +{guardrails.stopLoss.takeProfitCents}¢ · min hold {guardrails.stopLoss.minHoldSeconds}s
                </div>
                <div>
                  Thesis reversal: {guardrails.stopLoss.thesisReversal ? "on" : "off"}
                </div>
              </div>
            )}
            <div className="mt-1 pt-3 border-t border-[#24272C] text-[11px] text-[#767C86] leading-relaxed">
              Guardrails are enforced by the bot's risk manager. BTC spot: ${Number(btcSpot || 0).toLocaleString()}
            </div>
          </div>
        </Panel>

        <Panel title="Open positions" eyebrow={`${positions?.length || 0} held · live marks`} className="xl:col-span-3">
          <div className="p-4 grid grid-cols-1 gap-3">
            {!positions?.length && (
              <div className="text-[#767C86] text-xs py-6 text-center">No open positions right now.</div>
            )}
            {positions?.map((p) => {
              const unrealizedPnl = p.unrealizedPnl ?? ((p.currentMark - p.entryPrice) * p.count) / 100;
              const breakevenDelta = p.currentMark - p.entryPrice;
              const spotDelta = btcSpot - p.strikeBtc;
              const timeHeldMs = Date.now() - p.entryTime;
              const markSide = p.side === "yes"
                ? { bid: p.yesBid, ask: p.yesAsk }
                : { bid: p.noBid, ask: p.noAsk };
              return (
                <div key={p.id} className="bg-[#0D0F12] border border-[#4CC9F0]/20 rounded-lg p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <span
                        className={`text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded ${
                          p.side === "yes" ? "bg-[#33D693]/15 text-[#33D693]" : "bg-[#F2495C]/15 text-[#F2495C]"
                        }`}
                      >
                        {p.side}
                      </span>
                      <div>
                        <div className="text-sm text-[#E9E7E2] font-medium">{p.ticker}</div>
                        {p.subtitle && <div className="text-[10px] text-[#767C86]">{p.subtitle}</div>}
                      </div>
                    </div>
                    <CountdownRing expiresAt={p.expiresAt} windowMin={60} />
                  </div>

                  {p.signalReason && (
                    <div className="mt-3 text-[11px] text-[#4CC9F0] bg-[#4CC9F0]/5 border border-[#4CC9F0]/20 rounded px-2 py-1.5">
                      Entry signal: {p.signalReason}
                    </div>
                  )}

                  <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mt-4 text-[11px]">
                    <div>
                      <div className="text-[#767C86]">Entry → mark</div>
                      <div className="text-[#E9E7E2] mt-0.5 font-medium">
                        {p.entryPrice}¢ → {p.currentMark}¢ <DeltaTag value={breakevenDelta} />
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Live {p.side} bid/ask</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        {markSide.bid ?? "—"}¢ / {markSide.ask ?? "—"}¢
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Yes / No quotes</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        Y {p.yesBid ?? "—"}/{p.yesAsk ?? "—"}¢ · N {p.noBid ?? "—"}/{p.noAsk ?? "—"}¢
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Unrealized P&L</div>
                      <div className={`mt-0.5 font-medium ${unrealizedPnl >= 0 ? "text-[#33D693]" : "text-[#F2495C]"}`}>
                        {unrealizedPnl >= 0 ? "+" : ""}${Number(unrealizedPnl).toFixed(2)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Cost / value</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        ${Number(p.costBasis ?? 0).toFixed(2)} → ${Number(p.marketValue ?? 0).toFixed(2)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Size / time left</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        {p.count} ct · {p.minutesRemaining ?? "—"}m
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">BTC spot vs strike</div>
                      <div className={`mt-0.5 ${spotDelta >= 0 ? "text-[#33D693]" : "text-[#F2495C]"}`}>
                        {spotDelta >= 0 ? "+" : ""}${spotDelta.toLocaleString()}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Implied / spread</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        {Number(p.impliedProb || 0).toFixed(1)}% · {p.spread ?? 0}¢
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Book depth Y/N</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        {p.depthYes ?? 0} / {p.depthNo ?? 0}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Time held</div>
                      <div className="text-[#E9E7E2] mt-0.5">{fmtElapsed(timeHeldMs)}</div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Stop / target</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        {p.stopLossBidFloor ? `≤${p.stopLossBidFloor}¢ stop` : "—"}
                        {p.takeProfitBid ? ` · ≥${p.takeProfitBid}¢ TP` : ""}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Mark updated</div>
                      <div className="text-[#767C86] mt-0.5 text-[10px]">
                        {p.markUpdatedAt ? new Date(p.markUpdatedAt).toLocaleTimeString([], { hour12: false }) : "—"}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel title="Live contract prices" eyebrow="Bid / ask / depth" className="xl:col-span-3">
          <div className="p-4">
            <LivePricesTable markets={displayMarkets} />
          </div>
        </Panel>

        <Panel title="Active 1-hour markets" eyebrow="Cards" className="xl:col-span-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 p-4">
            {!markets?.length && (
              <div className="text-[#767C86] text-xs py-6 text-center col-span-3">
                No active hourly markets in status feed yet.
              </div>
            )}
            {displayMarkets?.map((m) => {
              const impliedProb = Number(m.impliedProb || (m.yesBid + m.yesAsk) / 2 || 0);
              const ratio = m.depthNo > 0 ? m.depthYes / m.depthNo : 0;
              const skew = ratio >= 1 ? `${ratio.toFixed(1)}x yes` : `${(1 / (ratio || 1)).toFixed(1)}x no`;
              const badges = marketBadges(m);
              const dimmed = m.isExtremeQuote && !m.tradeCandidate;
              return (
                <div
                  key={m.ticker}
                  className={`flex items-center gap-3 bg-[#0D0F12] border rounded-lg p-3 ${
                    m.tradeCandidate ? "border-[#4CC9F0]/40" : "border-[#24272C]"
                  } ${dimmed ? "opacity-50" : ""}`}
                >
                  <CountdownRing expiresAt={m.expiresAt} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <div className="text-xs text-[#E9E7E2] font-medium truncate">{m.ticker}</div>
                      {badges.map((badge) => (
                        <span
                          key={badge.label}
                          className={`px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wide ${badge.className}`}
                        >
                          {badge.label}
                        </span>
                      ))}
                    </div>
                    <div className="flex flex-wrap items-center gap-3 mt-1 text-[11px]">
                      <span className="text-[#33D693]">Y {m.yesBid}/{m.yesAsk}¢</span>
                      <span className="text-[#F2495C]">N {m.noBid}/{m.noAsk}¢</span>
                      <span className="text-[#767C86]">{impliedProb.toFixed(1)}% impl</span>
                      <span className="text-[#767C86]">{m.spread ?? 0}¢ spread</span>
                    </div>
                    <div className="mt-1.5 h-1 rounded-full bg-[#1A1D22] overflow-hidden">
                      <div
                        className="h-full bg-[#4CC9F0]"
                        style={{ width: `${Math.min(100, Math.max(0, impliedProb))}%` }}
                      />
                    </div>
                    <div className="mt-1 text-[10px] text-[#767C86]">book skew: {skew}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel title="Detail journal" eyebrow="Signals · fills · settlements" className="xl:col-span-3 min-h-[360px]">
          <DetailJournal journal={journal} logs={logs} winsToday={winsToday} lossesToday={lossesToday} />
        </Panel>

        <Panel title="Live scan log" eyebrow="Recent cycles" className="xl:col-span-3">
          <div className="h-48 overflow-y-auto px-4 pb-4">
            <table className="w-full text-xs">
              <tbody>
                {!logs?.length && (
                  <tr>
                    <td className="text-[#767C86] py-6 text-center" colSpan={2}>
                      Waiting for the first scan cycle…
                    </td>
                  </tr>
                )}
                {logs?.map((l) => {
                  const style = rowStyle(l);
                  const when = new Date(l.time);
                  return (
                    <tr key={l.id} className="border-b border-[#1A1D22] last:border-0">
                      <td className="py-1.5 pr-3 whitespace-nowrap text-[#767C86] align-top">
                        {when.toLocaleTimeString([], { hour12: false })}
                      </td>
                      <td className="py-1.5 align-top">
                        <span className={`inline-block w-1.5 h-1.5 rounded-full mr-2 ${style.dot}`} />
                        {l.kind === "settle" && <SettlementOutcome row={l} />}
                        <span className={`${l.kind === "settle" ? "ml-2 " : ""}${style.text}`}>{l.text}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>

      <div className="px-5 py-3 text-[10px] text-[#767C86] border-t border-[#24272C]">
        Live data from <Mono>/api/1h-bot/status</Mono> — dashboard polls every {pollIntervalMs || DEFAULT_POLL_MS}ms;
        bot refreshes quotes every ~{2}s when positions are open.
      </div>
    </div>
  );
}
