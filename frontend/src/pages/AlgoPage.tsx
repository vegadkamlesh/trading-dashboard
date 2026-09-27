import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import type {
  AlgoConfig,
  AlgoEvent,
  AlgoLeg,
  AlgoPosition,
  AlgoStatus,
  AlgoStudy,
  AlgoTrade,
} from "../api/types";

// --------------------------------------------------------------------------- //
//  small helpers
// --------------------------------------------------------------------------- //
const n = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });

const rs = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`;

const pnlTone = (v: number) => (v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-300");

const LEG_TONE: Record<string, string> = {
  watching: "bg-sky-500/15 text-sky-300 border-sky-600/40",
  active: "bg-emerald-500/15 text-emerald-300 border-emerald-600/40",
  locked: "bg-slate-700/40 text-slate-400 border-slate-600/40",
  skipped: "bg-amber-500/15 text-amber-300 border-amber-600/40",
  closed: "bg-slate-700/40 text-slate-300 border-slate-600/40",
  preopen: "bg-slate-700/40 text-slate-400 border-slate-600/40",
};

function Chip({ children, tone = "slate" }: { children: React.ReactNode; tone?: string }) {
  const map: Record<string, string> = {
    slate: "bg-slate-800 text-slate-300 border-slate-700",
    green: "bg-emerald-500/15 text-emerald-300 border-emerald-600/40",
    red: "bg-rose-500/15 text-rose-300 border-rose-600/40",
    amber: "bg-amber-500/15 text-amber-300 border-amber-600/40",
    sky: "bg-sky-500/15 text-sky-300 border-sky-600/40",
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold ${map[tone] ?? map.slate}`}>
      {children}
    </span>
  );
}

function Stat({ label, value, tone = "" }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg bg-slate-900/60 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`font-mono text-sm font-semibold tabular-nums ${tone || "text-slate-100"}`}>{value}</div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  main page
// --------------------------------------------------------------------------- //
type SubTab = "live" | "study" | "journal" | "eod" | "tuning";

export function AlgoPage() {
  const [sub, setSub] = useState<SubTab>("live");
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // 1s status poll: the whole "live" screen is fed from this one cheap call.
  const { data: st, refresh } = usePolling<AlgoStatus>(() => api.get("/api/algo/status"), 1000);
  const { data: study, refresh: refreshStudy } = usePolling<AlgoStudy>(
    () => api.get("/api/algo/study"), sub === "study" ? 3000 : 30000
  );

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(t);
  }, [toast]);

  const call = async (path: string, body?: unknown, label?: string) => {
    setBusy(true);
    try {
      await api.post(path, body);
      await refresh();
      if (label) setToast({ kind: "ok", text: label });
    } catch (e) {
      setToast({ kind: "err", text: e instanceof Error ? e.message : "Failed" });
    } finally {
      setBusy(false);
    }
  };

  if (!st) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-400">
        Loading algo status…
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {toast && (
        <div className={`rounded-lg border px-3 py-2 text-sm ${toast.kind === "ok"
          ? "border-emerald-700 bg-emerald-950/50 text-emerald-200"
          : "border-rose-700 bg-rose-950/50 text-rose-200"}`}>
          {toast.text}
        </div>
      )}

      <ControlBar st={st} busy={busy} onCall={call} />
      <NowAndNext st={st} />

      <div className="grid gap-3 lg:grid-cols-2">
        {st.legs.map((leg) => <IndexCard key={leg.index} leg={leg} cfg={st.config} status={st} />)}
      </div>

      {st.position && <PositionCard pos={st.position} cfg={st.config} busy={busy} onCall={call} />}

      <div className="flex flex-wrap gap-1 border-b border-slate-800 pb-1">
        {([["live", "Live feed"], ["study", "5-year study"], ["journal", "Journal (7 din)"],
           ["eod", "EOD / aaj ka result"], ["tuning", "Settings"]] as Array<[SubTab, string]>).map(
          ([k, label]) => (
            <button key={k} className={"tab " + (sub === k ? "tab-active" : "")} onClick={() => setSub(k)}>
              {label}
            </button>
          ))}
      </div>

      {sub === "live" && <LivePanel st={st} />}
      {sub === "study" && <StudyPanel study={study} onRun={() => { call("/api/algo/study/start?years=5", {}, "Study started — ye 5 saal ka data chalayega"); setTimeout(refreshStudy, 1500); }} busy={busy} />}
      {sub === "journal" && <JournalPanel />}
      {sub === "eod" && <EodPanel />}
      {sub === "tuning" && <TuningPanel cfg={st.config} busy={busy} onCall={call} />}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  control bar
// --------------------------------------------------------------------------- //
function ControlBar({ st, busy, onCall }: { st: AlgoStatus; busy: boolean; onCall: (p: string, b?: unknown, l?: string) => void }) {
  const on = st.enabled;
  return (
    <div className="card flex flex-wrap items-center gap-4">
      <button
        onClick={() => onCall(on ? "/api/algo/disarm" : "/api/algo/arm", {}, on ? "Algo DISARMED" : "Algo ARMED")}
        disabled={busy}
        className={`flex items-center gap-3 rounded-xl border px-4 py-2.5 transition-colors ${on
          ? "border-emerald-500 bg-emerald-600/20 hover:bg-emerald-600/30"
          : "border-slate-700 bg-slate-900 hover:bg-slate-800"}`}
        title="Algo ON / OFF — the only thing you need to touch"
      >
        <span className={`relative h-6 w-11 rounded-full transition-colors ${on ? "bg-emerald-500" : "bg-slate-600"}`}>
          <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all ${on ? "left-[22px]" : "left-0.5"}`} />
        </span>
        <span className="text-left">
          <div className={`text-sm font-bold ${on ? "text-emerald-300" : "text-slate-300"}`}>
            {on ? "ALGO ON" : "ALGO OFF"}
          </div>
          <div className="text-[11px] text-slate-500">{on ? "click to stop" : "click to start"}</div>
        </span>
      </button>

      <div className="flex flex-col">
        <div className="text-[10px] uppercase tracking-wider text-slate-500">Ab kya ho raha hai</div>
        <div className="text-sm font-semibold text-slate-100">{st.phaseLabel}</div>
      </div>

      <div className="ml-auto flex flex-wrap items-center gap-2">
        {st.dryRun
          ? <Chip tone="amber">DRY-RUN · paper trade</Chip>
          : <Chip tone="red">🔴 LIVE MONEY</Chip>}
        {st.marketOpen ? <Chip tone="green">market open</Chip> : <Chip>market closed</Chip>}
        {st.feed.webSocket
          ? <Chip tone="green">feed: LIVE tick</Chip>
          : st.feed.mode === "rest"
            ? <Chip tone="amber">feed: 1-sec poll</Chip>
            : <Chip tone="red">feed: offline</Chip>}
        <Chip>{st.serverTimeShort} IST</Chip>

        {st.position && (
          <button className="btn-sell" disabled={busy}
            onClick={() => onCall("/api/algo/exit", {}, "Position square-off bheja gaya")}>
            ⛔ EXIT NOW
          </button>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  now / next
// --------------------------------------------------------------------------- //
function NowAndNext({ st }: { st: AlgoStatus }) {
  const s = st.stats;
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      <div className="card lg:col-span-2">
        <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">Aage kya hoga</div>
        <ul className="space-y-1.5">
          {st.nextActions.map((a, i) => (
            <li key={i} className="flex gap-2 text-sm text-slate-200">
              <span className="text-sky-400">▸</span><span>{a}</span>
            </li>
          ))}
          {st.recovered && (
            <li className="flex gap-2 text-sm text-amber-300">
              <span>⚠</span><span>{st.recovered}</span>
            </li>
          )}
          {st.lastError && (
            <li className="flex gap-2 text-sm text-rose-300">
              <span>✕</span><span>Last error: {st.lastError}</span>
            </li>
          )}
        </ul>
      </div>
      <div className="card">
        <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">Aaj ka natija</div>
        <div className={`font-mono text-2xl font-bold tabular-nums ${pnlTone(s.todayPnl)}`}>
          {rs(s.todayPnl)}
        </div>
        <div className="mt-1 text-xs text-slate-400">
          {s.todayTrades} trade · {s.todayWins} win · {s.todayR >= 0 ? "+" : ""}{s.todayR}R
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Stat label="7 din P&L" value={rs(s.weekPnl)} tone={pnlTone(s.weekPnl)} />
          <Stat label="7 din trades" value={s.weekTrades} />
          <Stat label="All-time P&L" value={rs(s.allTimePnl)} tone={pnlTone(s.allTimePnl)} />
          <Stat label="Balance" value={st.paperBalance !== null ? rs(st.paperBalance) : "live"} />
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  per-index card
// --------------------------------------------------------------------------- //
function IndexCard({ leg, cfg, status }: { leg: AlgoLeg; cfg: AlgoConfig; status: AlgoStatus }) {
  const tone = LEG_TONE[leg.status] ?? LEG_TONE.preopen;
  const isActive = status.position?.index === leg.index;
  const rng = leg.orRange ?? 0;

  // How far along is the spot inside the range? 0% = at OR low, 100% = at OR high.
  const posPct = leg.orLow !== null && leg.orHigh !== null && leg.spot !== null && rng > 0
    ? Math.max(0, Math.min(100, ((leg.spot - leg.orLow) / rng) * 100))
    : null;

  const target = leg.spot !== null && rng > 0
    ? { up: leg.spot + cfg.target_mult * rng, down: leg.spot - cfg.target_mult * rng }
    : null;

  return (
    <div className={`card border ${isActive ? "border-emerald-600/60 ring-1 ring-emerald-700/40" : ""}`}>
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-bold tracking-wide">{leg.name}</span>
          <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${tone}`}>
            {leg.status.toUpperCase()}
          </span>
          {isActive && <Chip tone="green">● IN TRADE</Chip>}
        </div>
        <div className="font-mono text-lg font-bold tabular-nums">
          {n(leg.spot, 1)}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <Stat label="OR High (9:25)" value={n(leg.orHigh, 1)} />
        <Stat label="OR Low" value={n(leg.orLow, 1)} />
        <Stat label="OR Range" value={leg.orRange !== null ? `${n(leg.orRange, 1)} pts` : "—"} />
      </div>

      {posPct !== null && (
        <div className="mt-3">
          <div className="relative h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div className="absolute inset-y-0 left-0 bg-sky-600/40" style={{ width: `${posPct}%` }} />
            <div className="absolute inset-y-0 left-0 w-0.5 bg-sky-400" style={{ left: `${posPct}%` }} />
          </div>
          <div className="mt-1 flex justify-between text-[10px] text-slate-500">
            <span>OR Low</span>
            <span>
              {leg.toUpper !== null && leg.toUpper > 0
                ? `${n(leg.toUpper, 1)} pts to breakout ↑`
                : leg.toLower !== null && leg.toLower > 0
                  ? `${n(leg.toLower, 1)} pts to breakdown ↓`
                  : "at the edge"}
            </span>
            <span>OR High</span>
          </div>
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-2">
        <Stat label="Option (ATM)" value={leg.strike ? `${leg.strike} ${leg.signal === "PUT" ? "PE" : "CE"}` : "—"} />
        <Stat label="Premium" value={leg.optionLtp !== null ? `₹${n(leg.optionLtp, 1)}` : "—"} />
      </div>

      {target && rng > 0 && (
        <div className="mt-3 rounded-lg border border-slate-800 bg-slate-900/40 p-2 text-[11px] text-slate-400">
          <div className="mb-1 font-semibold text-slate-300">Agar yahin se toote to:</div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono">
            <span>↑ CALL target {n(target.up, 1)}</span>
            <span>↓ PUT target {n(target.down, 1)}</span>
            <span className="text-rose-300">↑ CALL stop {n(leg.spot! - cfg.sl_mult * rng, 1)}</span>
            <span className="text-rose-300">↓ PUT stop {n(leg.spot! + cfg.sl_mult * rng, 1)}</span>
          </div>
        </div>
      )}

      {leg.note && <div className="mt-2 text-[11px] text-amber-300">⚠ {leg.note}</div>}
      {leg.expiry && <div className="mt-1 text-[10px] text-slate-500">expiry {leg.expiry} · lot {leg.plannedLots ?? "—"}</div>}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  position
// --------------------------------------------------------------------------- //
function PositionCard({ pos, cfg, busy, onCall }: { pos: AlgoPosition; cfg: AlgoConfig; busy: boolean; onCall: (p: string, b?: unknown, l?: string) => void }) {
  const win = pos.premiumPnl >= 0;
  return (
    <div className={`card border-2 ${win ? "border-emerald-700/50" : "border-rose-700/50"}`}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="text-base font-bold">
            {pos.index} {pos.strike} {pos.optionType === "CALL" ? "CE" : "PE"}
          </span>
          <Chip tone={pos.optionType === "CALL" ? "green" : "red"}>{pos.optionType}</Chip>
          <Chip>{pos.lots} lot · {pos.quantity} qty</Chip>
          {pos.dryRun && <Chip tone="amber">paper</Chip>}
          {pos.breakevenDone && <Chip tone="sky">stop @ break-even</Chip>}
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-[10px] uppercase tracking-wider text-slate-500">Premium P&L</div>
            <div className={`font-mono text-2xl font-bold tabular-nums ${pnlTone(pos.premiumPnl)}`}>
              {rs(pos.premiumPnl)}
            </div>
          </div>
          <button className="btn-sell" disabled={busy} onClick={() => onCall("/api/algo/exit", {}, "Exit order bheja gaya")}>
            EXIT
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Entry spot" value={n(pos.entrySpot, 1)} />
        <Stat label="Spot now" value={n(pos.spot, 1)} tone={pnlTone(pos.points)} />
        <Stat label="Target spot" value={n(pos.targetSpot, 1)} tone="text-emerald-300" />
        <Stat label="Stop spot" value={n(pos.stopSpot, 1)} tone="text-rose-300" />
        <Stat label="Premium" value={`₹${n(pos.entryPremium, 1)} → ₹${n(pos.premium, 1)}`} />
        <Stat label="R multiple" value={`${pos.rMultiple >= 0 ? "+" : ""}${n(pos.rMultiple, 2)}R`} tone={pnlTone(pos.rMultiple)} />
      </div>

      <div className="mt-2 flex flex-wrap gap-4 text-[11px] text-slate-500">
        <span>entry {pos.entryTime}</span>
        <span>MFE {n(pos.mfe, 1)} pts</span>
        <span>MAE {n(pos.mae, 1)} pts</span>
        <span>target = {cfg.target_mult}× OR · stop = {cfg.sl_mult}× OR</span>
        {pos.protectiveOrderId && <span className="text-emerald-500">disaster stop parked at Dhan ✓</span>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  live feed panel
// --------------------------------------------------------------------------- //
const LEVEL_TONE: Record<string, string> = {
  success: "text-emerald-300",
  info: "text-slate-300",
  warn: "text-amber-300",
  error: "text-rose-300",
};

function LivePanel({ st }: { st: AlgoStatus }) {
  const events = useMemo(() => [...st.events].reverse(), [st.events]);
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      <div className="card lg:col-span-2">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wider text-slate-500">Live log</span>
          <span className="text-[10px] text-slate-500">
            feed {st.feed.mode} · {st.feed.ticks.toLocaleString()} ticks
            {st.feed.lastTickSecondsAgo !== null ? ` · last ${st.feed.lastTickSecondsAgo}s ago` : ""}
          </span>
        </div>
        <div className="max-h-96 space-y-1 overflow-y-auto font-mono text-xs">
          {events.length === 0 && <div className="text-slate-500">No events yet today.</div>}
          {events.map((e, i) => (
            <div key={i} className="flex gap-2">
              <span className="shrink-0 text-slate-600">{e.time ?? "—"}</span>
              <span className={LEVEL_TONE[String(e.level)] ?? "text-slate-300"}>{e.msg}</span>
            </div>
          ))}
        </div>
      </div>
      <TradesTable trades={st.todayTrades} title="Aaj ke trades" />
    </div>
  );
}

function TradesTable({ trades, title }: { trades: AlgoTrade[]; title: string }) {
  return (
    <div className="card">
      <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">{title}</div>
      {trades.length === 0 ? (
        <div className="text-sm text-slate-500">Koi trade nahi.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-[10px] uppercase text-slate-500">
              <tr>
                <th className="py-1 text-left">Index</th>
                <th className="text-left">Side</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Exit</th>
                <th className="text-right">R</th>
                <th className="text-right">P&L</th>
                <th className="text-left">Why</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {trades.map((t, i) => (
                <tr key={i} className="border-t border-slate-800">
                  <td className="py-1">{t.index}</td>
                  <td className={t.direction === "CALL" ? "text-emerald-400" : "text-rose-400"}>{t.direction}</td>
                  <td className="text-right">{n(t.entrySpot, 1)}</td>
                  <td className="text-right">{n(t.exitSpot, 1)}</td>
                  <td className={`text-right ${pnlTone(t.rMultiple)}`}>{n(t.rMultiple, 2)}</td>
                  <td className={`text-right ${pnlTone(t.premiumPnl)}`}>{rs(t.premiumPnl)}</td>
                  <td className="pl-2 text-slate-400">{t.exitReason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  study
// --------------------------------------------------------------------------- //
function StudyPanel({ study, onRun, busy }: { study: AlgoStudy | null; onRun: () => void; busy: boolean }) {
  const p = study?.progress;
  const r = study?.result;
  if (!study) return <div className="card text-sm text-slate-400">Loading study…</div>;

  return (
    <div className="flex flex-col gap-3">
      <div className="card flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">5-saal ka ORB study</div>
          <div className="text-xs text-slate-400">
            Har stop/target kombin karke dekha jaata hai — kis par expectancy sabse achhi hai,
            aur brokerage+slippage ke baad kya bachta hai.
          </div>
          {r && (
            <div className="mt-1 text-[11px] text-slate-500">
              {r.range.from} → {r.range.to} · generated {new Date(r.generatedAt * 1000).toLocaleString("en-IN")}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Chip tone={p?.status === "running" ? "amber" : p?.status === "ready" ? "green" : "slate"}>
            {p?.status ?? "idle"}
          </Chip>
          <button className="btn-ghost" onClick={onRun} disabled={busy || p?.status === "running"}>
            {p?.status === "running" ? "Chal raha hai…" : "Study dubara chalao"}
          </button>
        </div>
      </div>

      {p?.status === "running" && (
        <div className="card">
          <div className="mb-1 text-xs text-slate-400">{p.step} — {p.percent}%</div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
            <div className="h-full bg-sky-500 transition-all" style={{ width: `${p.percent}%` }} />
          </div>
        </div>
      )}
      {p?.status === "error" && <div className="card text-sm text-rose-300">Study failed: {p.error}</div>}

      {r?.recommended && (
        <div className="card border border-sky-800/60 bg-sky-950/20">
          <div className="text-[10px] uppercase tracking-wider text-sky-400">History se best config</div>
          <div className="mt-1 flex flex-wrap items-center gap-4">
            <div className="font-mono text-2xl font-bold text-sky-200">
              stop {r.recommended.slMult}× OR · target {r.recommended.targetMult}× OR
            </div>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Expectancy (worst index)" value={`${r.recommended.expectedR}R`} tone="text-emerald-300" />
            <Stat label="Profit factor" value={n(r.recommended.profitFactor, 2)} />
            <Stat label="Trades (min)" value={r.recommended.minTrades} />
            <Stat label="Max DD (pts)" value={n(r.recommended.maxDrawdownPoints, 0)} tone="text-rose-300" />
          </div>
          <div className="mt-2 text-xs text-slate-400">{r.recommended.reason}</div>
        </div>
      )}

      {r?.recommended?.costBreakEvenRupees != null && (
        <div className={`card border-2 ${r.recommended.costBreakEvenRupees >= 150
          ? "border-emerald-700/60 bg-emerald-950/20"
          : "border-amber-700/60 bg-amber-950/20"}`}>
          <div className="text-[10px] uppercase tracking-wider text-amber-400">
            ⚠ Sabse zaroori baat (yahi decide karti hai profit hoga ya nahi)
          </div>

          <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-sm text-slate-300">
              1 lot pe ek trade ka average <b>gross profit</b>:
            </span>
            <span className="font-mono text-xl font-bold text-emerald-300">
              {rs(r.recommended.costBreakEvenRupees)}
            </span>
            <span className="text-xs text-slate-500">
              → isliye break-even bhi lagbhag yahi cost pe hota hai
            </span>
          </div>

          <div className="mt-2 text-sm text-slate-200">
            Matlab: agar aapka <b>brokerage + STT + slippage</b> mila kar ek round-trip pe
            ₹{Math.round(r.recommended.costBreakEvenRupees)} se <b>zyada</b> lagta hai, to yeh strategy
            loss degi — chahe backtest me kitni hi achhi dikhe.
          </div>

          <div className="mt-2 rounded-lg bg-slate-900/60 p-2 text-xs text-slate-400">
            Retail me 1 lot NIFTY ka realistic round-trip cost: brokerage ~₹40 + STT/exchange/GST
            ~₹30 + slippage ~₹40-80 ≈ <b>₹110-150</b>. Yeh break-even se upar hai. Isliye pehle
            <b> dry-run</b> me chala kar apna actual cost naap lo.
          </div>
        </div>
      )}

      {r && Object.entries(r.indices).map(([key, idx]) => (
        <div key={key} className="card">
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <span className="text-sm font-bold">{key}</span>
            <Chip>{idx.days} days</Chip>
            <Chip>avg OR {n(idx.avgOrRange, 1)} pts</Chip>
            <Chip>lot {idx.lotSize ?? "—"}</Chip>
            {idx.best && <Chip tone="green">best: stop {idx.best.sl}× / target {idx.best.target}× → {n(idx.best.expR, 2)}R</Chip>}
          </div>

          {idx.mfe && (
            <div className="mb-3">
              <div className="mb-1 text-[11px] text-slate-400">
                Breakout ke baad spot kitna chalta hai (× OR range): median <b>{n(idx.mfe.median, 2)}×</b>, mean <b>{n(idx.mfe.mean, 2)}×</b>
              </div>
              <div className="flex items-end gap-2">
                {["p30", "p50", "p70", "p80", "p90"].map((k) => {
                  const v = idx.mfe![k] ?? 0;
                  return (
                    <div key={k} className="flex flex-col items-center">
                      <div className="w-10 rounded-t bg-sky-700" style={{ height: `${Math.min(70, v * 28)}px` }} />
                      <div className="mt-1 text-[10px] text-slate-500">{k}</div>
                      <div className="font-mono text-[10px] text-slate-300">{n(v, 2)}×</div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">
            Expectancy (R) heat-map — rows = stop, cols = target
          </div>
          <div className="overflow-x-auto">
            <table className="text-[11px]">
              <thead className="text-slate-500">
                <tr>
                  <th className="px-2 py-1 text-left">stop ↓ / target →</th>
                  {[...new Set((idx.grid ?? []).map((g) => g.target))].map((t) => (
                    <th key={t} className="px-2 py-1 text-right">{t}×</th>
                  ))}
                </tr>
              </thead>
              <tbody className="font-mono tabular-nums">
                {[...new Set((idx.grid ?? []).map((g) => g.sl))].map((sl) => (
                  <tr key={sl} className="border-t border-slate-800">
                    <td className="px-2 py-1 text-slate-400">{sl}×</td>
                    {[...new Set((idx.grid ?? []).map((g) => g.target))].map((t) => {
                      const cell = (idx.grid ?? []).find((g) => g.sl === sl && g.target === t);
                      if (!cell) return <td key={t} className="px-2 py-1 text-right text-slate-700">—</td>;
                      const v = cell.expR;
                      const bg = v > 0.15 ? "bg-emerald-700/50 text-emerald-100"
                        : v > 0.05 ? "bg-emerald-900/40 text-emerald-200"
                          : v > 0 ? "bg-slate-800 text-slate-300"
                            : "bg-rose-900/40 text-rose-200";
                      return (
                        <td key={t} className={`px-2 py-1 text-right ${bg}`} title={`win ${cell.winRate}% · PF ${cell.pf} · ${cell.trades} trades`}>
                          {n(v, 2)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {idx.costCurve && idx.costCurve.length > 0 && (
            <div className="mt-4 rounded-lg border border-slate-800 bg-slate-900/40 p-3">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                Cost sensitivity — ship hone wali config
                {idx.shippedConfig ? ` (${idx.shippedConfig.sl}× / ${idx.shippedConfig.target}×)` : ""},
                1 lot
              </div>

              {idx.grossRupeesPerTrade != null && (
                <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="text-xs text-slate-400">Average gross profit per trade:</span>
                  <span className="font-mono text-lg font-bold text-emerald-300">
                    {rs(idx.grossRupeesPerTrade)}
                  </span>
                  <span className="text-xs text-slate-500">
                    · {idx.tradesPerYear ?? 0} trades/saal
                  </span>
                </div>
              )}

              {idx.costBreakEvenRupees != null && (
                <div className="mt-1 text-xs text-amber-300">
                  ⚠ ₹{idx.costBreakEvenRupees}/round-trip cost pe break-even. Isse zyada = loss.
                </div>
              )}

              <table className="mt-3 w-full text-[11px]">
                <thead className="text-slate-500">
                  <tr>
                    <th className="text-left">aapka cost/round-trip</th>
                    <th className="text-right">net pts</th>
                    <th className="text-right">PF</th>
                    <th className="text-right">5 saal net ₹</th>
                    <th className="text-right">saal ka ₹</th>
                    <th className="text-right">verdict</th>
                  </tr>
                </thead>
                <tbody className="font-mono tabular-nums">
                  {idx.costCurve.map((c) => {
                    const isBE = idx.costBreakEvenRupees != null && c.cost === idx.costBreakEvenRupees;
                    return (
                      <tr key={c.cost}
                        className={`border-t border-slate-800 ${isBE ? "bg-amber-950/40" : ""}`}>
                        <td className="py-1">
                          ₹{c.cost}
                          {isBE && <span className="ml-1 text-amber-400">◀ break-even</span>}
                        </td>
                        <td className="text-right">{n(c.netPoints, 0)}</td>
                        <td className="text-right">{n(c.pf, 2)}</td>
                        <td className={`text-right ${pnlTone(c.netRupees)}`}>{rs(c.netRupees)}</td>
                        <td className={`text-right ${pnlTone(c.netRupees)}`}>{rs(c.netRupees / 5)}</td>
                        <td className={`text-right ${c.netPoints > 0 ? "text-emerald-400" : "text-rose-400"}`}>
                          {c.netPoints > 0 ? "profit" : "loss"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  journal  (last 7 days, straight off disk)
// --------------------------------------------------------------------------- //
function JournalPanel() {
  const { data } = usePolling<{ days: string[]; events: AlgoEvent[]; keepDays: number }>(
    () => api.get("/api/algo/journal?days=7"), 10000
  );
  const groups = useMemo(() => {
    const m = new Map<string, AlgoEvent[]>();
    (data?.events ?? []).forEach((e) => {
      const d = String((e as Record<string, unknown>).date ?? "");
      const key = d || "—";
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(e);
    });
    return m;
  }, [data]);

  return (
    <div className="flex flex-col gap-3">
      <div className="card flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">Journal — last 7 din</div>
          <div className="text-xs text-slate-400">
            Yeh disk pe save hota hai (backend/data/algo). 7 din se purane logs khud-ba-khud delete ho jaate hain.
          </div>
        </div>
        <div className="flex gap-2">
          {(data?.days ?? []).map((d) => <Chip key={d}>{d}</Chip>)}
        </div>
      </div>
      <div className="card">
        {groups.size === 0 && <div className="text-sm text-slate-500">Abhi koi journal entry nahi.</div>}
        <div className="space-y-3">
          {[...groups.entries()].map(([day, evs]) => (
            <div key={day}>
              <div className="mb-1 border-b border-slate-800 pb-1 text-xs font-semibold text-slate-400">{day}</div>
              <div className="max-h-64 space-y-0.5 overflow-y-auto font-mono text-xs">
                {evs.map((e, i) => (
                  <div key={i} className="flex gap-2">
                    <span className="shrink-0 text-slate-600">{e.time}</span>
                    <span className={LEVEL_TONE[String(e.level)] ?? "text-slate-300"}>{e.msg}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  EOD
// --------------------------------------------------------------------------- //
function EodPanel() {
  const { data, refresh } = usePolling<{
    date: string; availableDays: string[];
    events: AlgoEvent[]; trades: AlgoTrade[];
    summary: { events: number; trades: number; wins: number; netRupees: number; netR: number; warnings: number };
  }>(() => api.get("/api/algo/eod"), 15000);
  const [pick, setPick] = useState<string>("");

  if (!data) return <div className="card text-sm text-slate-400">Loading…</div>;
  const day = pick || data.date;

  return (
    <div className="flex flex-col gap-3">
      <div className="card flex flex-wrap items-center gap-3">
        <div className="text-sm font-semibold">EOD summary</div>
        <select className="input w-auto" value={day}
          onChange={(e) => { setPick(e.target.value); api.get(`/api/algo/eod?day=${e.target.value}`).then(refresh); }}>
          {data.availableDays.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <div className="ml-auto flex gap-2">
          <Chip tone="sky">{data.summary.trades} trades</Chip>
          <Chip tone="green">{data.summary.wins} win</Chip>
          <Chip tone={data.summary.netRupees >= 0 ? "green" : "red"}>
            {rs(data.summary.netRupees)}
          </Chip>
          {data.summary.warnings > 0 && <Chip tone="amber">{data.summary.warnings} warnings</Chip>}
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <TradesTable trades={data.trades} title={`${day} — trades`} />
        <div className="card">
          <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">
            {day} — poora din ka timeline
          </div>
          <div className="max-h-[28rem] space-y-0.5 overflow-y-auto font-mono text-xs">
            {data.events.map((e, i) => (
              <div key={i} className="flex gap-2">
                <span className="shrink-0 text-slate-600">{e.time}</span>
                <span className={LEVEL_TONE[String(e.level)] ?? "text-slate-300"}>{e.msg}</span>
              </div>
            ))}
            {data.events.length === 0 && <div className="text-slate-500">Us din koi activity nahi.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  tuning
// --------------------------------------------------------------------------- //
const NUM_FIELDS: Array<[keyof AlgoConfig, string, string]> = [
  ["sl_mult", "Stop (× OR range)", "0.2 = opening range ka 20% (5-saal study ka best, executable)"],
  ["target_mult", "Target (× OR range)", "2.0 = range ka 2 guna (har stop level pe sabse achha nikla)"],
  ["breakeven_at", "Break-even trigger (× OR, 0=off)", "0 = off. On karne se distribution badal jaata hai (untested)"],
  ["risk_pct", "Risk % of balance / trade", "Lot size isi se decide hota hai (compounding)"],
  ["max_capital_pct", "Max % balance as premium", "Ek trade me kitna paisa lagega uski limit"],
  ["max_lots", "Max lots", "Hard cap"],
  ["min_lots", "Min lots", "Isse kam lots bane to trade skip"],
  ["strike_offset", "Strike offset (0=ATM)", "0 = ATM, 1 = ek strike OTM"],
  ["min_or_pct", "Min OR % of price (0=off)", "Chhoti range wale din skip"],
  ["max_or_pct", "Max OR % of price (0=off)", "Bahut bade gap wale din skip"],
  ["protective_sl_mult", "Disaster stop (× normal stop)", "Dhan pe park kiya jaane wala wide SL"],
];

type Draft = Record<string, string | number | boolean | string[]>;

function TuningPanel({ cfg, busy, onCall }: { cfg: AlgoConfig; busy: boolean; onCall: (p: string, b?: unknown, l?: string) => void }) {
  const [draft, setDraft] = useState<Draft>(() => ({ ...cfg } as unknown as Draft));
  useEffect(() => setDraft({ ...cfg } as unknown as Draft), [cfg]);

  const save = () => {
    const current = cfg as unknown as Draft;
    const patch: Record<string, unknown> = {};
    Object.keys(current).forEach((k) => {
      if (k === "indices") return;
      if (JSON.stringify(draft[k]) !== JSON.stringify(current[k])) patch[k] = draft[k];
    });
    if (Object.keys(patch).length === 0) return;
    onCall("/api/algo/config", patch, "Settings save ho gaye");
  };

  return (
    <div className="card">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">Algo settings</div>
          <div className="text-xs text-slate-400">
            Yahan badla hua config turant lagu hota hai aur restart ke baad bhi yaad rehta hai.
          </div>
        </div>
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={() => setDraft({ ...cfg } as unknown as Draft)} disabled={busy}>Reset</button>
          <button className="btn" style={{ background: "#0284c7" }} onClick={save} disabled={busy}>Save</button>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {NUM_FIELDS.map(([key, label, hint]) => (
          <label key={String(key)} className="flex flex-col gap-1">
            <span className="text-[11px] font-semibold text-slate-300">{label}</span>
            <input
              className="input font-mono"
              type="number"
              step="0.05"
              value={String(draft[String(key)] ?? "")}
              onChange={(e) => setDraft({ ...draft, [String(key)]: e.target.value === "" ? 0 : Number(e.target.value) })}
            />
            <span className="text-[10px] text-slate-500">{hint}</span>
          </label>
        ))}

        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-semibold text-slate-300">Entry cutoff (IST)</span>
          <input className="input font-mono" value={String(draft.entry_cutoff ?? "")}
            onChange={(e) => setDraft({ ...draft, entry_cutoff: e.target.value })} />
          <span className="text-[10px] text-slate-500">Iske baad naya trade nahi</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-semibold text-slate-300">Force exit time (IST)</span>
          <input className="input font-mono" value={String(draft.exit_time ?? "")}
            onChange={(e) => setDraft({ ...draft, exit_time: e.target.value })} />
          <span className="text-[10px] text-slate-500">Yahan position band ho jaayegi</span>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-semibold text-slate-300">Order type</span>
          <select className="input" value={String(draft.order_type ?? "MARKET")}
            onChange={(e) => setDraft({ ...draft, order_type: e.target.value })}>
            <option value="MARKET">MARKET (fast, slippage)</option>
            <option value="LIMIT">LIMIT (no slippage, may miss)</option>
          </select>
        </label>
        <label className="flex items-center gap-2 pt-4">
          <input type="checkbox" checked={Boolean(draft.protective_sl)}
            onChange={(e) => setDraft({ ...draft, protective_sl: e.target.checked })} />
          <span className="text-[11px] font-semibold text-slate-300">
            Disaster stop Dhan pe park karo (server crash safety)
          </span>
        </label>
      </div>
    </div>
  );
}
