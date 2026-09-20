import { useEffect, useMemo, useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type {
  ChainSnapshot,
  Guidance,
  IndexInfo,
  StrikeRecommendation,
} from "../api/types";
import { OptionChainTable } from "../components/OptionChainTable";
import { OrderTicket, type TicketSeed } from "../components/OrderTicket";
import { NewsPanel } from "../components/NewsPanel";
import { RecommendationsPanel } from "../components/RecommendationsPanel";
import { SeasonalityPanel } from "../components/SeasonalityPanel";
import { SignalDetailModal } from "../components/SignalDetailModal";
import { useRecommendations } from "../hooks/useRecommendations";

// How many strikes to show on each side of the ATM strike (±N).
const STRIKE_RANGE = 10;

// Auto-refresh cadence (ms). Kept gentle so we never trip Dhan's 429 limit:
// the backend serves a warm cache, and we only ask every few seconds.
const AUTO_REFRESH_MS = 5000;

export function OptionChainPage({ indices }: { indices: IndexInfo[] }) {
  const [active, setActive] = useState("NIFTY");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>("");
  const [seed, setSeed] = useState<TicketSeed | null>(null);
  const [guidance, setGuidance] = useState<Guidance | null>(null);
  const [detail, setDetail] = useState<StrikeRecommendation | null>(null);

  // Shared recommendation set (used by both the signals panel and the chain).
  const {
    data: recData,
    loading: recLoading,
    reload: reloadRecs,
    byKey: recsByKey,
    lastUpdated: recUpdated,
    refreshMs: recRefreshMs,
  } = useRecommendations(active);

  // Load expiries whenever the active index changes.
  useEffect(() => {
    setExpiries([]);
    setExpiry("");
    api
      .get<{ expiries: string[] }>(`/api/market/expiries?index=${active}`)
      .then((r) => {
        setExpiries(r.expiries);
        if (r.expiries.length) {
          setExpiry(r.expiries[0]);
          api.post("/api/market/expiry/select", { index: active, expiry: r.expiries[0] });
        }
      })
      .catch(() => {});
  }, [active]);

  // Always-on auto refresh. `active` is the identity: switching index resets
  // the data immediately and refetches (so we never show the previous index).
  const { data: snapshot, refresh } = usePolling<ChainSnapshot>(
    () => api.get(`/api/market/optionchain?index=${active}`),
    AUTO_REFRESH_MS,
    true,
    active
  );

  // Market-level guidance (trend / bull-bear / support-resistance). Refetch on
  // index change; the CE view is used purely for the directional read.
  useEffect(() => {
    let cancelled = false;
    setGuidance(null);
    api
      .get<Guidance>(`/api/intel/guidance?index=${active}&optionType=CE&historyDays=90`)
      .then((g) => {
        if (!cancelled) setGuidance(g);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [active]);

  // Trim to the ±STRIKE_RANGE window around the money.
  const windowed = useMemo(() => {
    if (!snapshot || !snapshot.rows.length) return snapshot;
    const ltp = snapshot.underlyingLtp;
    let atmIdx = 0;
    let bestD = Infinity;
    snapshot.rows.forEach((r, i) => {
      const d = Math.abs(r.strike - ltp);
      if (d < bestD) {
        bestD = d;
        atmIdx = i;
      }
    });
    const start = Math.max(0, atmIdx - STRIKE_RANGE);
    const end = Math.min(snapshot.rows.length, atmIdx + STRIKE_RANGE + 1);
    return { ...snapshot, rows: snapshot.rows.slice(start, end) };
  }, [snapshot]);

  const activeName = indices.find((i) => i.key === active)?.name ?? active;

  const onPick = (p: Omit<TicketSeed, "row" | "indexKey" | "indexName"> & { row: any }) => {
    setSeed({
      indexKey: active,
      indexName: activeName,
      optionType: p.optionType,
      transactionType: p.transactionType,
      row: p.row,
    });
  };

  // Trade from a recommendation: find the matching chain row for the strike.
  const onPickRec = (rec: StrikeRecommendation) => {
    if (!windowed) return;
    const row = windowed.rows.find((r) => r.strike === rec.strike);
    if (!row) return;
    setSeed({
      indexKey: active,
      indexName: activeName,
      optionType: rec.side,
      transactionType: "BUY",
      row,
    });
  };

  const selectExpiry = (e: string) => {
    setExpiry(e);
    api.post("/api/market/expiry/select", { index: active, expiry: e });
  };

  // Bull/bear read from guidance.
  const probUp = guidance ? Math.round(guidance.probabilityUp * 100) : null;
  const sr = guidance?.supportResistance;

  return (
    <div className="space-y-3">
      {/* Index tabs */}
      <div className="flex flex-wrap items-center gap-2">
        {indices.map((i) => (
          <button
            key={i.key}
            className={"tab " + (active === i.key ? "tab-active" : "")}
            onClick={() => setActive(i.key)}
          >
            {i.name}
          </button>
        ))}
      </div>

      {/* Sub-header: LTP + bull/bear + S/R, expiry select, freshness, refresh */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <div className="flex items-center gap-2">
          <span className="text-slate-400">Underlying:</span>
          <span className="font-mono font-bold text-sky-300">
            {snapshot ? snapshot.underlyingLtp.toFixed(2) : "-"}
          </span>
          {probUp != null && (
            <span
              className={
                "rounded-md border px-2 py-0.5 text-xs font-bold " +
                (probUp >= 55
                  ? "border-green-600/50 bg-green-500/10 text-green-400"
                  : probUp <= 45
                  ? "border-red-600/50 bg-red-500/10 text-red-400"
                  : "border-amber-600/50 bg-amber-500/10 text-amber-400")
              }
              title="Model probability the index moves up from here"
            >
              {probUp >= 55 ? "▲ Bullish" : probUp <= 45 ? "▼ Bearish" : "▬ Neutral"} {probUp}%
            </span>
          )}
        </div>

        {sr && (sr.support.length > 0 || sr.resistance.length > 0) && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-slate-500">Support</span>
            <span className="font-mono text-green-400">
              {sr.support.slice(0, 2).map((n) => n.toFixed(0)).join(" / ") || "-"}
            </span>
            <span className="text-slate-600">|</span>
            <span className="text-slate-500">Resistance</span>
            <span className="font-mono text-red-400">
              {sr.resistance.slice(0, 2).map((n) => n.toFixed(0)).join(" / ") || "-"}
            </span>
          </div>
        )}

        <div className="flex items-center gap-2">
          <span className="text-slate-400">Expiry:</span>
          <select
            className="input w-40"
            value={expiry}
            onChange={(e) => selectExpiry(e.target.value)}
          >
            {expiries.map((e) => (
              <option key={e} value={e}>
                {e}
              </option>
            ))}
          </select>
        </div>

        {snapshot && (
          <span
            className={
              "flex items-center gap-1 text-xs " +
              (snapshot.stale ? "text-amber-400" : "text-green-500")
            }
          >
            <span
              className={
                "inline-block h-2 w-2 rounded-full " +
                (snapshot.stale ? "bg-amber-400" : "bg-green-500")
              }
            />
            live · {snapshot.ageSeconds.toFixed(0)}s
          </span>
        )}

        <button
          className="tab px-2 py-0.5 text-xs"
          onClick={refresh}
          title="Refetch the option chain now"
        >
          ⟳ Refresh
        </button>

        <span className="text-xs text-slate-500">
          Auto-refresh ON (every {AUTO_REFRESH_MS / 1000}s) · showing ±{STRIKE_RANGE} strikes
        </span>
      </div>

      {/* Per-strike trade signals (shared data with the chain below) */}
      <RecommendationsPanel
        data={recData}
        loading={recLoading}
        reload={reloadRecs}
        lastUpdated={recUpdated}
        refreshMs={recRefreshMs}
        onPick={onPickRec}
        onInfo={setDetail}
      />

      {windowed ? (
        <OptionChainTable
          snapshot={windowed}
          onPick={onPick}
          recs={recsByKey}
          onInfo={setDetail}
        />
      ) : (
        <div className="p-8 text-center text-sm text-slate-500">Loading option chain…</div>
      )}

      <SeasonalityPanel indexKey={active} />

      <NewsPanel />

      <p className="text-xs text-slate-500">
        Chain auto-refreshes every {AUTO_REFRESH_MS / 1000}s (or hit ⟳ Refresh). Only ±
        {STRIKE_RANGE} strikes around the money are shown. B = BUY, S = SELL — opening a trade
        shows the Trade Advisor before anything is sent. Signals are probability-based, NOT
        guaranteed.
      </p>

      {seed && <OrderTicket seed={seed} onClose={() => setSeed(null)} />}
      {detail && (
        <SignalDetailModal rec={detail} ctx={recData} onClose={() => setDetail(null)} />
      )}
    </div>
  );
}
