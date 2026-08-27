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
const START_BANKROLL = 100;
const POLL_MS = 2200;

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
};

function emptyState() {
  return {
    mode: "paper",
    running: true,
    estop: false,
    series: "KXBTC",
    btcSpot: 0,
    bankroll: START_BANKROLL,
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
    peakEquity: START_BANKROLL,
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
  if (!markets.length) return null;
  const row = [...markets, ...markets, ...markets];
  return (
    <div className="relative overflow-hidden border-b border-[#24272C] bg-[#0D0F12] h-9 flex items-center">
      <div className="flex gap-10 whitespace-nowrap animate-[tape_28s_linear_infinite] px-4">
        {row.map((m, i) => (
          <span key={i} className="text-[12px] font-[IBM_Plex_Mono] text-[#9AA0A8] flex items-center gap-2">
            <span className="text-[#E9E7E2]">{m.ticker.split("-").slice(-1)[0]}</span>
            <span className="text-[#33D693]">Y{m.yesBid}¢</span>
            <span className="text-[#F2495C]">N{100 - m.yesAsk}¢</span>
            <span className="text-[#767C86]">{((m.yesBid + m.yesAsk) / 2).toFixed(0)}% impl.</span>
          </span>
        ))}
      </div>
      <style>{`
        @keyframes tape { from { transform: translateX(0); } to { transform: translateX(-33.333%); } }
      `}</style>
    </div>
  );
}

export default function KalshiBotDashboard() {
  useInjectFonts();

  const [data, setData] = useState(emptyState);
  const [estopArmed, setEstopArmed] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const payload = await fetchHourBotStatus();
      setData((prev) => ({ ...prev, ...payload }));
      setConnected(true);
      setError(null);
    } catch (err) {
      setConnected(false);
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

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
  } = data;

  const netEquity = bankroll + unrealized;
  const equityFmt = (v) => `$${Number(v || 0).toFixed(2)}`;
  const currentDrawdown = peakEquity - netEquity;
  const tradesToday = winsToday + lossesToday;
  const winRate = tradesToday > 0 ? winsToday / tradesToday : 0;
  const avgWin = winsToday > 0 ? sumWinDollars / winsToday : 0;
  const avgLoss = lossesToday > 0 ? sumLossDollars / lossesToday : 0;

  return (
    <div className="min-h-screen w-full bg-[#0A0B0D] text-[#E9E7E2] font-[IBM_Plex_Mono] flex flex-col">
      <TickerTape markets={markets} />

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
            <StatBlock label="Net equity" value={equityFmt(netEquity)} sub={`start $${START_BANKROLL.toFixed(2)}`} />
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
            <div className="mt-1 pt-3 border-t border-[#24272C] text-[11px] text-[#767C86] leading-relaxed">
              Guardrails are enforced by the bot's risk manager. BTC spot: ${Number(btcSpot || 0).toLocaleString()}
            </div>
          </div>
        </Panel>

        <Panel title="Open positions" eyebrow={`${positions?.length || 0} held`} className="xl:col-span-3">
          <div className="p-4 grid grid-cols-1 lg:grid-cols-2 gap-3">
            {!positions?.length && (
              <div className="text-[#767C86] text-xs py-6 text-center col-span-2">No open positions right now.</div>
            )}
            {positions?.map((p) => {
              const unrealizedPnl = ((p.currentMark - p.entryPrice) * p.count) / 100;
              const breakevenDelta = p.currentMark - p.entryPrice;
              const spotDelta = btcSpot - p.strikeBtc;
              const timeHeldMs = Date.now() - p.entryTime;
              return (
                <div key={p.id} className="bg-[#0D0F12] border border-[#24272C] rounded-lg p-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span
                        className={`text-[10px] uppercase font-semibold px-1.5 py-0.5 rounded ${
                          p.side === "yes" ? "bg-[#33D693]/15 text-[#33D693]" : "bg-[#F2495C]/15 text-[#F2495C]"
                        }`}
                      >
                        {p.side}
                      </span>
                      <span className="text-xs text-[#E9E7E2] font-medium">{p.ticker}</span>
                    </div>
                    <CountdownRing expiresAt={p.expiresAt} windowMin={60} />
                  </div>
                  <div className="grid grid-cols-3 gap-2 mt-3 text-[11px]">
                    <div>
                      <div className="text-[#767C86]">Entry / mark</div>
                      <div className="text-[#E9E7E2] mt-0.5">
                        {p.entryPrice}¢ <span className="text-[#767C86]">→</span> {p.currentMark}¢
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Unrealized P&L</div>
                      <div className={`mt-0.5 ${unrealizedPnl >= 0 ? "text-[#33D693]" : "text-[#F2495C]"}`}>
                        {unrealizedPnl >= 0 ? "+" : ""}${unrealizedPnl.toFixed(2)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">To breakeven</div>
                      <div className="mt-0.5 text-[#E9E7E2]">
                        <DeltaTag value={breakevenDelta} />
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">BTC spot vs strike</div>
                      <div className={`mt-0.5 ${spotDelta >= 0 ? "text-[#33D693]" : "text-[#F2495C]"}`}>
                        {spotDelta >= 0 ? "+" : ""}${spotDelta.toLocaleString()}
                      </div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Size</div>
                      <div className="mt-0.5 text-[#E9E7E2]">{p.count} contracts</div>
                    </div>
                    <div>
                      <div className="text-[#767C86]">Time held</div>
                      <div className="mt-0.5 text-[#E9E7E2]">{fmtElapsed(timeHeldMs)}</div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel title="Active 1-hour markets" eyebrow="Ticker feed" className="xl:col-span-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 p-4">
            {!markets?.length && (
              <div className="text-[#767C86] text-xs py-6 text-center col-span-3">
                No active hourly markets in status feed yet.
              </div>
            )}
            {markets?.map((m) => {
              const impliedProb = Math.round((m.yesBid + m.yesAsk) / 2);
              const ratio = m.depthNo > 0 ? m.depthYes / m.depthNo : 0;
              const skew = ratio >= 1 ? `${ratio.toFixed(1)}x yes` : `${(1 / (ratio || 1)).toFixed(1)}x no`;
              return (
                <div
                  key={m.ticker}
                  className="flex items-center gap-3 bg-[#0D0F12] border border-[#24272C] rounded-lg p-3"
                >
                  <CountdownRing expiresAt={m.expiresAt} />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs text-[#E9E7E2] font-medium truncate">{m.ticker}</div>
                    <div className="flex items-center gap-3 mt-1 text-[11px]">
                      <span className="text-[#33D693]">Yes {m.yesBid}¢</span>
                      <span className="text-[#F2495C]">No {100 - m.yesAsk}¢</span>
                      <span className="text-[#767C86]">{impliedProb}% impl.</span>
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

        <Panel title="Execution & decision log" eyebrow="Audit trail" className="xl:col-span-3">
          <div className="h-64 overflow-y-auto px-4 pb-4">
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
                  const style = LOG_STYLES[l.kind] ?? LOG_STYLES.scan;
                  const when = new Date(l.time);
                  return (
                    <tr key={l.id} className="border-b border-[#1A1D22] last:border-0">
                      <td className="py-1.5 pr-3 whitespace-nowrap text-[#767C86] align-top">
                        {when.toLocaleTimeString([], { hour12: false })}
                      </td>
                      <td className="py-1.5 align-top">
                        <span className={`inline-block w-1.5 h-1.5 rounded-full mr-2 ${style.dot}`} />
                        <span className={style.text}>{l.text}</span>
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
        Live data from <Mono>/api/1h-bot/status</Mono> — start <Mono>kalshi_btc_bot.py</Mono> and refresh every ~2s.
      </div>
    </div>
  );
}
