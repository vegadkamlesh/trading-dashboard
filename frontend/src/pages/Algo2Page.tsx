import { useEffect, useState } from "react";
import { api } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import type { AlgoEvent, AlgoPosition, AlgoTrade } from "../api/types";

// Algo 2 — institutional options-flow engine. Separate ON/OFF + Paper/Live from Algo 1.

type Side = "CALL" | "PUT";
type Account = "master" | "child";

interface Check { key: string; label: string; enabled: boolean; pass: boolean; value: string; gate?: boolean }
interface Score { gatesOk: boolean; gatesFailed: string[]; passed: number; total: number; need: number; ready: boolean }

interface Metrics {
  index: string; name: string; spot: number | null;
  candle: { t: number; high: number; low: number; close: number } | null;
  triggerUp: number | null; triggerDown: number | null; breakout: Side | null;
  netDelta: number | null; netDeltaPrev: number | null; netGex: number | null; pcr: number | null;
  oiVelocity: { ce: number | null; pe: number | null };
  hsmi: number | null; heavyweights: Record<string, number | null>;
  vwap: { vwap: number; upper: number; lower: number; sd: number } | null;
  profile: { poc: number; vah: number; val: number } | null;
  futures: number | null; basis: number | null; basisPrev: number | null; vixMovePct: number | null;
  chainRows: number; candles: number; futCandles: number;
  checks: Record<Side, Check[]>; allPass: Record<Side, boolean>; score?: Record<Side, Score>;
}

interface NewsEvent { id: string; date: string; time?: string | null; name: string; source?: string; impact?: string }

type Algo2Position = AlgoPosition & {
  t1Spot: number | null; t1Done: boolean; t1Pending: boolean; t1Qty: number; t1Premium: number | null;
  t1Time: string | null; heldSeconds: number; holdLimitSeconds: number;
};

interface AccountState {
  enabled: boolean; phase: string; phaseLabel: string; position: Algo2Position | null; nextActions: string[];
  todayTrades: AlgoTrade[]; stats: { todayTrades: number; todayPnl: number; weekPnl: number; allTimePnl: number } | null;
  lastError: string | null; brokerSyncError: string | null; brokerNetQty: number | null; paperBalance: number | null;
  events: AlgoEvent[]; configured: boolean; selected: boolean;
}

type Config = Record<string, unknown> & {
  indices: string[]; conditions: Record<string, boolean>; trade_windows: string; max_trades: number;
  entry_cutoff: string; exit_time: string; max_stop_loss_pct: number; max_hold_minutes: number;
  oi_velocity_spike: number; min_score: number; vwap_band_sd: number; t1_pct: number; target_delta: number; event_window_minutes: number;
  pcr_chop_low: number; pcr_chop_high: number; vix_spike_pct: number; data_sl: boolean; partial_booking: boolean;
  capital_sl_cap: boolean; stop_after_sl: boolean; priority_over_algo1: boolean; trade_limit: boolean; oi_wall_target: boolean; status_report_minutes: number; protective_sl_mult: number;
  entry_buffer: Record<string, number>;
};

interface Algo2Status {
  algo: string; enabled: boolean; dryRun: boolean; entryAccounts: Account[]; serverTime: string; phase: string;
  config: Config; conditionCatalog: Array<{ key: string; label: string; gate?: boolean }>;
  metrics: Record<string, Metrics>;
  news: { freezeActive: boolean; freezeEvent: string | null; windowMinutes: number; today: NewsEvent[];
    upcoming: NewsEvent[]; nseError: string | null };
  data: { futures: Record<string, { securityId: number; symbol?: string }>; futuresError: string | null;
    quoteAgeSeconds: number | null; quoteError: string | null; candleError: string | null; apiCalls: Record<string, number> };
  signals: { day: string | null; count: number; max: number | null; last: Record<string, unknown> | null };
  loopError: string | null;
  nextActions: string[];
  nextActionsAt: string | null;
  reports: Array<{ time: string; text: string }>;
  slStop: string | null;
  accounts: Record<Account, AccountState>;
}

const n = (v: number | null | undefined, d = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const rs = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `₹${Math.round(v).toLocaleString("en-IN")}`;
const tone = (v: number | null | undefined) => (!v ? "text-slate-300" : v > 0 ? "text-emerald-400" : "text-rose-400");
const mmss = (s: number) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, "0")}`;
const errText = (e: unknown) => (e instanceof Error ? e.message : "Failed");

function Stat({ label, value, cls = "" }: { label: string; value: React.ReactNode; cls?: string }) {
  return (
    <div className="rounded-lg bg-slate-900/60 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`font-mono text-sm font-semibold tabular-nums ${cls || "text-slate-100"}`}>{value}</div>
    </div>
  );
}

export function Algo2Page() {
  const { data: st, error, refresh } = usePolling<Algo2Status>(() => api.get("/api/algo2/status"), 1000);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 5000);
    return () => window.clearTimeout(t);
  }, [toast]);

  const call = async (path: string, body?: unknown, label?: string) => {
    setBusy(true);
    try {
      const r = await api.post<{ ok?: boolean; error?: string; results?: Array<{ accountId: string; ok: boolean; error?: string }> }>(path, body);
      refresh();
      if (r && r.ok === false) {
        throw new Error(r.error || r.results?.filter((x) => !x.ok).map((x) => `${x.accountId}: ${x.error}`).join("; ") || "Action failed");
      }
      if (label) setToast({ ok: true, text: label });
    } catch (e) {
      setToast({ ok: false, text: errText(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!st) {
    return <div className="flex h-64 items-center justify-center text-slate-400">
      {error ? `Algo 2 status unavailable: ${error}` : "Loading Algo 2…"}
    </div>;
  }

  const anyOpen = Object.values(st.accounts).some((a) => a.position);

  return (
    <div className="flex flex-col gap-3">
      {error && <div role="alert" className="rounded-lg border border-rose-700 bg-rose-950/50 p-3 text-sm text-rose-200">
        Status refresh failed: {error}. Values below are the last snapshot.
      </div>}
      {toast && <div className={`rounded-lg border px-3 py-2 text-sm ${toast.ok
        ? "border-emerald-700 bg-emerald-950/50 text-emerald-200" : "border-rose-700 bg-rose-950/50 text-rose-200"}`}>{toast.text}</div>}

      <ControlBar st={st} busy={busy} anyOpen={anyOpen} call={call} />

      {st.slStop && <div role="alert" className="rounded border border-rose-700 bg-rose-950/40 p-2 text-sm text-rose-200">
        🛑 Stop-loss hit today ({st.slStop}) — Algo 2 takes no more trades today.
      </div>}
      <div className="card p-3">
        <div className="mb-1 text-sm font-semibold">
          🔮 Aage kya hoga <span className="text-[11px] font-normal text-slate-400">
            (har 5 min + entry/exit pe turant update{st.nextActionsAt ? ` · last ${st.nextActionsAt}` : ""})</span>
        </div>
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-slate-300">
          {(st.nextActions || []).map((x, i) => <li key={i} className={i === 0 ? "font-semibold text-sky-200" : "font-mono"}>{x}</li>)}
        </ul>
        {!st.enabled && <p className="mt-1 text-[11px] text-amber-300">Algo 2 is OFF — turn it ON to scan and get reports/Telegram updates.</p>}
      </div>

      {st.loopError && <div role="alert" className="rounded border border-amber-700 p-2 text-xs text-amber-200">Signal loop: {st.loopError}</div>}
      {(st.data.quoteError || st.data.candleError || st.data.futuresError) && (
        <div className="rounded border border-amber-800 p-2 text-xs text-amber-200">
          {st.data.quoteError && <p>Quotes: {st.data.quoteError}</p>}
          {st.data.candleError && <p>Candles: {st.data.candleError}</p>}
          {st.data.futuresError && <p>Futures: {st.data.futuresError}</p>}
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        {(["master", "child"] as Account[]).map((acc) => (
          <AccountCard key={acc} account={acc} a={st.accounts[acc]} />
        ))}
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        {st.config.indices.map((key) => st.metrics[key]
          ? <IndexPanel key={key} m={st.metrics[key]} st={st} busy={busy} call={call} />
          : <div key={key} className="card p-3 text-sm text-slate-400">{key}: waiting for first data cycle (market hours, Algo 2 ON)…</div>)}
      </div>

      <ReportsPanel st={st} />
      <ExitRulesCard st={st} />
      <NewsPanel st={st} busy={busy} call={call} />
      <TradesPanel st={st} />
      <SettingsCard st={st} busy={busy} call={call} />
      <EventsLog st={st} />
    </div>
  );
}

function ControlBar({ st, busy, anyOpen, call }: {
  st: Algo2Status; busy: boolean; anyOpen: boolean; call: (p: string, b?: unknown, l?: string) => Promise<void>;
}) {
  const toggleAccount = (acc: Account) => {
    const next = st.entryAccounts.includes(acc) ? st.entryAccounts.filter((x) => x !== acc) : [...st.entryAccounts, acc];
    void call("/api/algo2/accounts", { accounts: next }, "Algo 2 accounts updated");
  };
  const switchMode = () => {
    const live = st.dryRun;
    if (live && !window.confirm("Switch ALGO 2 to LIVE? Real INTRADAY orders will be placed on the selected accounts when Algo 2 is ON.")) return;
    void call("/api/algo2/mode", { dry_run: !live }, `Algo 2 mode → ${live ? "LIVE" : "PAPER"}`);
  };
  const toggleEnabled = () => {
    if (!st.enabled && !st.dryRun && !window.confirm("Turn ON Algo 2 in LIVE mode?")) return;
    void call("/api/algo2/enabled", { enabled: !st.enabled }, `Algo 2 ${st.enabled ? "OFF" : "ON"}`);
  };
  return (
    <div className="card flex flex-wrap items-center gap-3 p-3">
      <span className="text-sm font-bold">🤖 Algo 2</span>
      <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${st.dryRun
        ? "border-sky-600/40 bg-sky-500/15 text-sky-300" : "border-rose-600/40 bg-rose-500/20 text-rose-200"}`}>
        {st.dryRun ? "PAPER (SIMULATION)" : "LIVE"}
      </span>
      <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold ${st.enabled
        ? "border-emerald-600/40 bg-emerald-500/15 text-emerald-300" : "border-slate-700 bg-slate-800 text-slate-400"}`}>
        {st.enabled ? "ON" : "OFF"} · {st.phase}
      </span>
      <button className={st.enabled ? "btn-ghost" : "btn-buy"} disabled={busy} onClick={toggleEnabled}>
        {st.enabled ? "Turn OFF" : "Turn ON"}
      </button>
      <button className="btn-ghost" disabled={busy || st.enabled || anyOpen} onClick={switchMode}
        title={st.enabled || anyOpen ? "Turn Algo 2 OFF and close positions first" : ""}>
        {st.dryRun ? "Switch to LIVE" : "Switch to PAPER"}
      </button>
      <span className="text-xs text-slate-400">Accounts:</span>
      {(["master", "child"] as Account[]).map((acc) => (
        <label key={acc} className="flex items-center gap-1 text-xs">
          <input type="checkbox" disabled={busy || st.enabled || !st.accounts[acc]?.configured}
            checked={st.entryAccounts.includes(acc)} onChange={() => toggleAccount(acc)} />
          {acc === "master" ? "Master" : "Child"}{!st.accounts[acc]?.configured && " (not configured)"}
        </label>
      ))}
      <span className="text-xs text-slate-400">Signals today: {st.signals.count}/{st.signals.max ?? "no limit"}</span>
      <span className="ml-auto flex gap-2">
        <button className="btn-ghost" disabled={busy} onClick={() => call("/api/algo2/reconcile", undefined, "Reconciled with Dhan")}>Reconcile</button>
        <button className="btn-sell" disabled={busy || !anyOpen}
          onClick={() => window.confirm("EXIT all Algo 2 positions now?") && call("/api/algo2/exit", undefined, "Algo 2 exit sent")}>
          EXIT Algo 2
        </button>
      </span>
      <span className="w-full text-[11px] text-slate-500">
        Server {st.serverTime} · INTRADAY product only (Algo 1 stays MARGIN) · Paper/Live and ON/OFF here never affect Algo 1.
      </span>
    </div>
  );
}

function AccountCard({ account, a }: { account: Account; a: AccountState }) {
  if (!a) return null;
  const p = a.position;
  return (
    <div className="card p-3">
      <div className="mb-2 flex items-center justify-between text-sm">
        <span className="font-semibold">{account === "master" ? "Master" : "Child"} · {a.phaseLabel || a.phase}</span>
        <span className="text-xs text-slate-400">
          {a.selected ? "selected" : "not selected"}{a.paperBalance != null && ` · paper bal ${rs(a.paperBalance)}`}
        </span>
      </div>
      {a.lastError && <p className="mb-2 text-xs text-rose-300">{a.lastError}</p>}
      {a.brokerSyncError && <p className="mb-2 text-xs text-amber-300">Broker sync: {a.brokerSyncError}</p>}
      {p ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Contract" value={`${p.index} ${p.strike} ${p.optionType === "CALL" ? "CE" : "PE"}`} />
          <Stat label="Qty / lots" value={`${p.quantity} / ${p.lots}${p.t1Done ? ` (+${p.t1Qty} T1)` : ""}`} />
          <Stat label="Entry → LTP" value={`${n(p.entryPremium)} → ${n(p.premium)}`} />
          <Stat label="P&L" value={rs(p.premiumPnl)} cls={tone(p.premiumPnl)} />
          <Stat label="Spot" value={n(p.spot)} />
          <Stat label="SL (spot)" value={n(p.stopSpot)} cls="text-rose-300" />
          <Stat label={p.t1Done ? "T2 (spot)" : "T1 (spot)"} value={n(p.t1Done ? p.targetSpot : p.t1Spot)} cls="text-emerald-300" />
          <Stat label={p.t1Done ? "T1 booked" : "20-min timer"}
            value={p.t1Done ? `${p.t1Qty} @ ${n(p.t1Premium)}` : `${mmss(p.heldSeconds)} / ${mmss(p.holdLimitSeconds)}`}
            cls={!p.t1Done && p.heldSeconds > p.holdLimitSeconds * 0.75 ? "text-amber-300" : ""} />
          <div className="col-span-full text-[11px] text-slate-500">
            {p.dryRun ? "PAPER" : "LIVE"} · {p.executionState}{p.t1Pending && " · T1 order pending"}
            {p.executionError && <span className="text-rose-300"> · {p.executionError}</span>}
          </div>
        </div>
      ) : (
        <ul className="list-disc pl-5 text-xs text-slate-400">
          {(a.nextActions || []).slice(0, 4).map((x, i) => <li key={i}>{x}</li>)}
        </ul>
      )}
      {a.stats && <div className="mt-2 text-xs text-slate-400">
        Today: {a.stats.todayTrades} trades · <span className={tone(a.stats.todayPnl)}>{rs(a.stats.todayPnl)}</span>
        {" · "}Week <span className={tone(a.stats.weekPnl)}>{rs(a.stats.weekPnl)}</span>
        {" · "}All-time <span className={tone(a.stats.allTimePnl)}>{rs(a.stats.allTimePnl)}</span>
      </div>}
    </div>
  );
}

function IndexPanel({ m, st, busy, call }: {
  m: Metrics; st: Algo2Status; busy: boolean; call: (p: string, b?: unknown, l?: string) => Promise<void>;
}) {
  const toggle = (key: string, value: boolean) =>
    call("/api/algo2/config", { conditions: { ...st.config.conditions, [key]: value } }, `${key} ${value ? "ON" : "OFF"}`);
  const sideCls = (s: Side) => (m.allPass[s] ? "text-emerald-300" : "text-slate-400");
  const scoreText = (s: Side) => {
    const sc = m.score?.[s];
    return sc ? `${sc.gatesOk ? "gates ✔" : "gate ✘"} · score ${sc.passed}/${sc.total} (need ${sc.need})` : "";
  };
  return (
    <div className="card p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-bold">{m.name}</span>
        <span className="font-mono">{n(m.spot)}</span>
        {m.breakout && <span className="rounded bg-sky-500/20 px-2 text-xs text-sky-200">breakout {m.breakout}</span>}
        <span className="ml-auto text-xs text-slate-400">
          5m candle H {n(m.candle?.high)} / L {n(m.candle?.low)} · trigger ↑{n(m.triggerUp)} ↓{n(m.triggerDown)}
        </span>
      </div>
      <div className="mb-2 grid grid-cols-3 gap-2 md:grid-cols-6">
        <Stat label="Net Δ" value={n(m.netDelta, 0)} cls={tone(m.netDelta !== null && m.netDeltaPrev !== null ? m.netDelta - m.netDeltaPrev : null)} />
        <Stat label="Net GEX" value={n(m.netGex, 0)} cls={m.netGex !== null && m.netGex < 0 ? "text-amber-300" : ""} />
        <Stat label="PCR" value={n(m.pcr)} />
        <Stat label="OI vel CE/PE" value={`${n(m.oiVelocity.ce, 1)} / ${n(m.oiVelocity.pe, 1)}%`} />
        <Stat label="HSMI" value={n(m.hsmi, 3)} cls={tone(m.hsmi)} />
        <Stat label="VIX move" value={`${n(m.vixMovePct)}%`} />
        <Stat label="Futures" value={n(m.futures)} />
        <Stat label="Basis" value={n(m.basis)} cls={tone(m.basis)} />
        <Stat label="VWAP ±band" value={m.vwap ? `${n(m.vwap.vwap, 1)} (${n(m.vwap.lower, 0)}–${n(m.vwap.upper, 0)})` : "—"} />
        <Stat label="POC / VA" value={m.profile ? `${n(m.profile.poc, 0)} (${n(m.profile.val, 0)}–${n(m.profile.vah, 0)})` : "—"} />
        <Stat label="Chain rows" value={m.chainRows} />
        <Stat label="Candles spot/fut" value={`${m.candles}/${m.futCandles}`} />
      </div>
      <table className="w-full text-xs">
        <thead className="text-slate-500">
          <tr><th className="text-left">On</th><th className="text-left">Condition</th>
            <th className={`text-left ${sideCls("CALL")}`}>CALL {m.allPass.CALL ? "✅" : ""} <span className="font-normal">{scoreText("CALL")}</span></th>
            <th className={`text-left ${sideCls("PUT")}`}>PUT {m.allPass.PUT ? "✅" : ""} <span className="font-normal">{scoreText("PUT")}</span></th></tr>
        </thead>
        <tbody>
          {m.checks.CALL.map((c, i) => {
            const p = m.checks.PUT[i];
            return (
              <tr key={c.key} className={`border-t border-slate-800 ${c.enabled ? "" : "opacity-40"}`}>
                <td>{c.gate ? <span title="Gate - always required">🔒</span>
                  : <input type="checkbox" disabled={busy} checked={c.enabled} onChange={(e) => toggle(c.key, e.target.checked)} />}</td>
                <td className="py-0.5">{c.gate ? "🔒 " : "🎯 "}{c.label}</td>
                <td className={c.pass ? "text-emerald-300" : "text-rose-300"}>{c.pass ? "✔" : "✘"} <span className="text-slate-500">{c.value}</span></td>
                <td className={p?.pass ? "text-emerald-300" : "text-rose-300"}>{p?.pass ? "✔" : "✘"} <span className="text-slate-500">{p?.value}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-1 text-[11px] text-slate-500">🔒 Gates must always pass. 🎯 Score: at least {st.config.min_score} of the ON score conditions must pass (switches apply to both indices).</p>
    </div>
  );
}

function NewsPanel({ st, busy, call }: { st: Algo2Status; busy: boolean; call: (p: string, b?: unknown, l?: string) => Promise<void> }) {
  const [form, setForm] = useState({ date: "", time: "", name: "" });
  const add = async () => {
    await call("/api/algo2/events", { date: form.date, time: form.time || null, name: form.name }, "Event added");
    setForm({ date: "", time: "", name: "" });
  };
  return (
    <div className="card p-3">
      <div className="mb-2 flex items-center gap-2 text-sm">
        <span className="font-semibold">News / event freeze (±{st.news.windowMinutes} min)</span>
        <span className={st.news.freezeActive ? "text-rose-300" : "text-emerald-300"}>
          {st.news.freezeActive ? `🔒 FROZEN — ${st.news.freezeEvent}` : "clear"}
        </span>
        {st.news.nseError && <span className="text-xs text-amber-300">NSE: {st.news.nseError}</span>}
      </div>
      <div className="mb-2 flex flex-wrap gap-2 text-xs">
        <input className="input" type="date" value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value })} />
        <input className="input" type="time" value={form.time} onChange={(e) => setForm({ ...form, time: e.target.value })} />
        <input className="input flex-1" placeholder="Event name (e.g. CPI release)" value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <button className="btn-ghost" disabled={busy || !form.date || !form.name} onClick={add}>Add event</button>
      </div>
      <div className="max-h-48 overflow-auto text-xs">
        {st.news.upcoming.length === 0 && <p className="text-slate-500">No upcoming events.</p>}
        {st.news.upcoming.map((e) => (
          <div key={e.id} className="flex items-center gap-2 border-t border-slate-800 py-0.5">
            <span className="font-mono">{e.date} {e.time || "(all day)"}</span>
            <span className="flex-1">{e.name}</span>
            <span className="text-slate-500">{e.source || "manual"}</span>
            {e.source !== "nse" && <button className="text-rose-300" disabled={busy}
              onClick={() => call(`/api/algo2/events/${encodeURIComponent(e.id)}/delete`, undefined, "Event removed")}>remove</button>}
          </div>
        ))}
      </div>
      <p className="mt-1 text-[11px] text-slate-500">Timed events block trading ±window. Date-only NSE results are shown but don't block.</p>
    </div>
  );
}

function TradesPanel({ st }: { st: Algo2Status }) {
  const rows = (["master", "child"] as Account[]).flatMap((acc) =>
    (st.accounts[acc]?.todayTrades || []).map((t) => ({ ...t, acc })));
  return (
    <div className="card p-3">
      <div className="mb-2 text-sm font-semibold">Today's Algo 2 trades</div>
      {rows.length === 0 ? <p className="text-xs text-slate-500">No trades today.</p> : (
        <table className="w-full text-xs">
          <thead className="text-slate-500"><tr>
            <th className="text-left">Acct</th><th className="text-left">Contract</th><th>Qty</th><th>Entry</th>
            <th>Exit</th><th>Premium</th><th>P&L</th><th className="text-left">Reason</th></tr></thead>
          <tbody>
            {rows.map((t, i) => (
              <tr key={i} className="border-t border-slate-800 text-center">
                <td className="text-left">{t.acc}</td>
                <td className="text-left">{t.index} {t.strike} {t.direction}</td>
                <td>{t.quantity}</td><td>{t.entryTime}</td><td>{t.exitTime}</td>
                <td>{n(t.entryPremium)} → {n(t.exitPremium)}</td>
                <td className={tone(t.premiumPnl)}>{rs(t.premiumPnl)}</td>
                <td className="text-left">{t.exitReason}{t.dryRun ? " (paper)" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

const NUM_FIELDS: Array<[string, string, number]> = [
  ["max_trades", "Max trades / day (only if limit ON)", 1],
  ["max_hold_minutes", "Hold limit (min, 0 = off)", 1],
  ["status_report_minutes", "Status report every (min, 0 = off)", 1],
  ["max_stop_loss_pct", "Capital SL cap % (only if cap ON)", 0.05],
  ["min_score", "Min score to trade (of 10)", 1],
  ["oi_velocity_spike", "OI velocity spike %", 1],
  ["vwap_band_sd", "VWAP band (std dev)", 0.1],
  ["t1_pct", "T1 booking %", 5],
  ["target_delta", "Target delta", 0.05],
  ["event_window_minutes", "Event window (min)", 1],
  ["pcr_chop_low", "PCR chop low", 0.01],
  ["pcr_chop_high", "PCR chop high", 0.01],
  ["vix_spike_pct", "VIX spike %", 0.5],
];

function SettingsCard({ st, busy, call }: { st: Algo2Status; busy: boolean; call: (p: string, b?: unknown, l?: string) => Promise<void> }) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const value = (k: string) => draft[k] ?? String(st.config[k] ?? "");
  const save = () => {
    const patch: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(draft)) {
      if (k === "trade_windows" || k === "entry_cutoff" || k === "exit_time") patch[k] = v;
      else if (k === "indices") patch[k] = v.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
      else patch[k] = Number(v);
    }
    void call("/api/algo2/config", patch, "Algo 2 settings saved").then(() => setDraft({}));
  };
  const flag = (k: "data_sl" | "partial_booking" | "capital_sl_cap" | "stop_after_sl" | "priority_over_algo1"
    | "trade_limit" | "oi_wall_target", label: string) => (
    <label className="flex items-center gap-1 text-xs">
      <input type="checkbox" disabled={busy} checked={!!st.config[k]}
        onChange={(e) => call("/api/algo2/config", { [k]: e.target.checked }, `${label} ${e.target.checked ? "ON" : "OFF"}`)} />
      {label}
    </label>
  );
  return (
    <details className="card p-3" open>
      <summary className="cursor-pointer text-sm font-semibold">Algo 2 settings (ON/OFF switches + values)</summary>
      <div className="mt-2 text-xs font-semibold text-slate-300">
        Entry: 🔒 4 gates always pass + 🎯 at least {st.config.min_score} score conditions (ON ones) pass
      </div>
      <div className="mt-1 grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3">
        {st.conditionCatalog.map((c) => c.gate ? (
          <span key={c.key} className="flex items-center gap-2 text-xs">🔒 {c.label} <span className="text-slate-500">(gate - always)</span></span>
        ) : (
          <label key={c.key} className="flex items-center gap-2 text-xs">
            <input type="checkbox" disabled={busy} checked={st.config.conditions[c.key] !== false}
              onChange={(e) => call("/api/algo2/config", { conditions: { ...st.config.conditions, [c.key]: e.target.checked } },
                `${c.label}: ${e.target.checked ? "ON" : "OFF"}`)} />
            <span className={st.config.conditions[c.key] === false ? "text-slate-500 line-through" : ""}>{c.label}</span>
          </label>
        ))}
      </div>
      <div className="mt-3 text-xs font-semibold text-slate-300">Exit / risk switches</div>
      <div className="mt-1 flex flex-wrap items-center gap-4">
        {flag("trade_limit", `Daily trade limit (max ${st.config.max_trades})`)}
        {flag("stop_after_sl", "1st SL hit → no more trades today")}
        {flag("data_sl", "Data SL (OI trap exit)")}
        {flag("oi_wall_target", "Target at OI wall (OFF = 2R)")}
        {flag("partial_booking", "T1 partial booking (70%)")}
        {flag("capital_sl_cap", "Cap candle SL at capital %")}
        {flag("priority_over_algo1", "Priority: exit Algo 1 if its margin blocks an Algo 2 entry (LIVE)")}
      </div>
      <div className="mt-3 text-xs font-semibold text-slate-300">Values</div>
      <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-4">
        {[["indices", "Indices"], ["trade_windows", "Trade windows"], ["entry_cutoff", "Entry cutoff"], ["exit_time", "Exit time"]].map(([k, label]) => (
          <label key={k} className="text-xs">{label}
            <input className="input w-full" value={k === "indices" ? (draft[k] ?? st.config.indices.join(",")) : value(k)}
              onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
          </label>
        ))}
        {NUM_FIELDS.map(([k, label, step]) => (
          <label key={k} className="text-xs">{label}
            <input className="input w-full" type="number" step={step} value={value(k)}
              onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
          </label>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <button className="btn-buy ml-auto" disabled={busy || Object.keys(draft).length === 0} onClick={save}>Save values</button>
      </div>
      <p className="mt-1 text-[11px] text-slate-500">Settings change only while no Algo 2 position is open. Entry buffer: NIFTY {st.config.entry_buffer?.NIFTY} / SENSEX {st.config.entry_buffer?.SENSEX} pts.</p>
    </details>
  );
}

function ReportsPanel({ st }: { st: Algo2Status }) {
  return (
    <div className="card p-3">
      <div className="mb-1 text-sm font-semibold">🕔 Har {st.config.status_report_minutes || 5} min ka situation report (Telegram pe bhi)</div>
      {(st.reports || []).length === 0
        ? <p className="text-xs text-slate-500">No report yet — reports start during market hours once Algo 2 is ON.</p>
        : <div className="max-h-64 overflow-auto font-mono text-[11px]">
          {st.reports.map((r, i) => (
            <div key={i} className="border-t border-slate-800 py-1">
              <span className="text-sky-300">{r.time}</span>{" "}
              {r.text.split(" || ").map((part, j) => <div key={j} className={j ? "pl-4 text-slate-400" : "inline text-slate-200"}>{part}</div>)}
            </div>
          ))}
        </div>}
    </div>
  );
}

function ExitRulesCard({ st }: { st: Algo2Status }) {
  const c = st.config;
  const rules = [
    `Stop-loss: breakout 5-min candle LOW (CALL) / HIGH (PUT) on spot${c.capital_sl_cap ? `, capped at ${c.max_stop_loss_pct}% of capital` : " — no capital cap"}.`,
    `Broker safety SL order: placed at ${c.protective_sl_mult}× the stop distance (backup if the app/server fails).`,
    `Data SL: ${c.data_sl ? "ON" : "OFF"} — exit early if same-side OI velocity overtakes the other side (institutional trap).`,
    `Target: ${c.oi_wall_target ? "highest-OI wall if ≥ 2R away, else 2R" : "2R (2 × candle stop distance on spot)"}. ${c.partial_booking ? `Book ${c.t1_pct}% there (1 lot = full exit), stop moves to cost, rest trails VWAP / T2.` : "Full exit at target."}`,
    c.max_hold_minutes > 0 ? `${c.max_hold_minutes}-minute rule: T1 not reached in ${c.max_hold_minutes} min → exit (theta).` : "Hold-time rule: OFF.",
    `Time exit: ${c.exit_time} (before Dhan INTRADAY auto square-off). New entries till ${c.entry_cutoff}, windows ${c.trade_windows}.`,
    `Trades: ${c.trade_limit ? `max ${c.max_trades}/day` : "no daily limit"}, one at a time — next only after the previous one closes${c.stop_after_sl ? "; after any stop-loss exit no more trades today" : ""}.`,
    `Priority over Algo 1: ${c.priority_over_algo1 ? "ON — if Algo 1's open LIVE trade leaves too little balance, Algo 1 is exited at market and Algo 2 enters as soon as that SELL fills" : "OFF — Algo 2 skips the signal when balance is short"}.`,
  ];
  return (
    <div className="card p-3">
      <div className="mb-1 text-sm font-semibold">🛡️ SL / exit rules (current values)</div>
      <ol className="list-decimal space-y-0.5 pl-5 text-xs text-slate-300">{rules.map((r, i) => <li key={i}>{r}</li>)}</ol>
    </div>
  );
}

function EventsLog({ st }: { st: Algo2Status }) {
  const rows = (["master", "child"] as Account[]).flatMap((acc) =>
    (st.accounts[acc]?.events || []).map((e) => ({ ...e, acc })))
    .sort((a, b) => Number(b.ts || 0) - Number(a.ts || 0)).slice(0, 60);
  return (
    <details className="card p-3" open>
      <summary className="cursor-pointer text-sm font-semibold">Algo 2 activity log</summary>
      <div className="mt-2 max-h-72 overflow-auto font-mono text-[11px]">
        {rows.length === 0 && <p className="text-slate-500">No activity yet.</p>}
        {rows.map((e, i) => (
          <div key={i} className={e.level === "error" ? "text-rose-300" : e.level === "warn" ? "text-amber-300"
            : e.level === "success" ? "text-emerald-300" : "text-slate-300"}>
            {e.time} [{e.acc}] {e.msg}
          </div>
        ))}
      </div>
    </details>
  );
}
