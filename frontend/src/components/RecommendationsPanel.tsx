import { useEffect, useState } from "react";
import type { RecommendationResponse, StrikeRecommendation } from "../api/types";

// Per-strike BUY recommendation engine output: which strike, which side,
// how much profit probability, theta risk, target/stop — with reasons.
// Data is supplied by the page (shared with the option-chain table) and
// auto-refreshes, so these signals stay live.
export function RecommendationsPanel({
  data,
  loading,
  reload,
  lastUpdated,
  refreshMs,
  onPick,
  onInfo,
}: {
  data: RecommendationResponse | null;
  loading: boolean;
  reload: () => void;
  lastUpdated?: number | null;
  refreshMs?: number;
  onPick?: (rec: StrikeRecommendation) => void;
  onInfo?: (rec: StrikeRecommendation) => void;
}) {
  // Show only BUY by default; toggle to see WAIT/AVOID too.
  const [showAll, setShowAll] = useState(false);
  const load = reload;

  // Tick every second so the "updated Xs ago" counter stays live.
  const [, forceTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => forceTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);

  if (loading && !data) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-xs text-slate-400">
        Analysing strikes & probabilities…
      </div>
    );
  }
  if (!data || data.error) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-xs text-slate-400">
        Recommendations unavailable{data?.error ? ` (${data.error})` : ""}.
        <button className="ml-2 underline" onClick={load}>
          retry
        </button>
      </div>
    );
  }

  const recs = data.recommendations;
  const buys = recs.filter((r) => r.action === "BUY");
  const shown = showAll ? recs : buys;
  const stanceCls =
    data.stance === "BULLISH"
      ? "text-green-300 bg-green-500/15 border-green-600/40"
      : data.stance === "BEARISH"
      ? "text-red-300 bg-red-500/15 border-red-600/40"
      : "text-amber-300 bg-amber-500/15 border-amber-600/40";

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-lg">🎯</span>
          <span className="text-sm font-bold uppercase tracking-wide text-slate-200">
            Trade Signals
          </span>
          <span className={"rounded-md border px-2.5 py-0.5 text-xs font-bold " + stanceCls}>
            Market {data.stance}
          </span>
          {data.daysToExpiry != null && (
            <span
              className={
                "rounded px-2 py-0.5 text-[11px] font-medium " +
                (data.daysToExpiry <= 2
                  ? "bg-red-500/15 text-red-300"
                  : "bg-slate-700/40 text-slate-300")
              }
            >
              {data.daysToExpiry === 0 ? "Expiry today!" : `${data.daysToExpiry}d to expiry`}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {lastUpdated != null && (
            <span
              className="flex items-center gap-1 rounded bg-green-500/10 px-2 py-0.5 text-[10px] font-semibold text-green-400"
              title={`Signals auto-refresh every ${Math.round((refreshMs ?? 20000) / 1000)}s`}
            >
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-green-400" />
              AUTO · {ageLabel(lastUpdated)}
            </span>
          )}
          <button
            className="text-xs text-slate-400 underline hover:text-slate-200"
            onClick={() => setShowAll((s) => !s)}
          >
            {showAll ? "show BUY only" : "show all strikes"}
          </button>
          <button
            className="text-xs text-slate-400 underline hover:text-slate-200 disabled:opacity-50"
            onClick={load}
            disabled={loading}
          >
            {loading ? "…" : "re-scan"}
          </button>
        </div>
      </div>

      {/* Context strip */}
      <div className="flex flex-wrap gap-3 border-b border-slate-800 px-4 py-2 text-[11px] text-slate-400">
        <span>RSI <b className="text-slate-200">{data.rsi?.toFixed(0) ?? "-"}</b></span>
        <span>Trend <b className="text-slate-200">{data.trend ?? "-"}</b></span>
        <span>
          MACD hist{" "}
          <b className={data.macd && data.macd.hist >= 0 ? "text-green-400" : "text-red-400"}>
            {data.macd ? data.macd.hist.toFixed(1) : "-"}
          </b>
        </span>
        <span>Spot <b className="text-slate-200">{data.spot?.toFixed(0)}</b></span>
      </div>

      {shown.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-slate-500">
          No strong BUY setup right now — market is {data.stance?.toLowerCase()}.{" "}
          <button className="underline" onClick={() => setShowAll(true)}>
            show all strikes
          </button>
        </div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full text-xs">
            <thead className="bg-slate-900 text-slate-400">
              <tr>
                <th className="px-3 py-1.5 text-left font-semibold">Strike</th>
                <th className="px-2 py-1.5 text-left font-semibold">Buy</th>
                <th className="px-2 py-1.5 text-right font-semibold">Profit prob.</th>
                <th className="px-2 py-1.5 text-right font-semibold">Conf.</th>
                <th className="px-2 py-1.5 text-center font-semibold">Theta</th>
                <th className="px-2 py-1.5 text-right font-semibold">Target</th>
                <th className="px-2 py-1.5 text-right font-semibold">Stop</th>
                <th className="px-2 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => (
                <tr key={`${r.strike}-${r.side}-${i}`} className="border-t border-slate-900 hover:bg-slate-800/40">
                  <td className="px-3 py-1.5 font-mono font-semibold text-slate-200">{r.strike}</td>
                  <td className="px-2 py-1.5">
                    <span
                      className={
                        "rounded px-1.5 py-0.5 font-bold " +
                        (r.side === "CE"
                          ? "bg-green-500/15 text-green-400"
                          : "bg-red-500/15 text-red-400")
                      }
                    >
                      {r.action} {r.side}
                    </span>
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <Prob value={r.profitProbability} />
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-slate-300">{r.confidence}%</td>
                  <td className="px-2 py-1.5 text-center">
                    <ThetaDot risk={r.thetaRisk} />
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-green-400">
                    +{r.suggestedTargetPct}%
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-red-400">
                    -{r.suggestedStopPct}%
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <div className="flex items-center justify-end gap-1">
                      {onInfo && (
                        <button
                          className="text-[11px] text-sky-400 hover:text-sky-300"
                          onClick={() => onInfo(r)}
                          title="Why this signal?"
                        >
                          ⓘ
                        </button>
                      )}
                      {onPick && r.action === "BUY" && (
                        <button
                          className="tab px-2 py-0.5 text-[11px]"
                          onClick={() => onPick(r)}
                          title={r.reasons.join(" · ")}
                        >
                          trade
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="px-4 py-2.5 text-[10px] leading-tight text-slate-500">{data.disclaimer}</p>
    </div>
  );
}

function ageLabel(updatedAt: number): string {
  const secs = Math.max(0, Math.round((Date.now() - updatedAt) / 1000));
  if (secs < 3) return "live";
  if (secs < 60) return `${secs}s ago`;
  return `${Math.floor(secs / 60)}m ago`;
}

function Prob({ value }: { value: number }) {
  const color = value >= 65 ? "bg-green-500" : value >= 50 ? "bg-amber-500" : "bg-slate-600";
  return (
    <div className="flex items-center justify-end gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded bg-slate-800">
        <div className={"h-full " + color} style={{ width: `${value}%` }} />
      </div>
      <span className="w-8 font-mono font-semibold text-slate-200">{value}%</span>
    </div>
  );
}

function ThetaDot({ risk }: { risk?: string }) {
  const map: Record<string, [string, string]> = {
    low: ["bg-green-500", "Low time-decay risk"],
    medium: ["bg-amber-500", "Medium time-decay risk"],
    high: ["bg-red-500", "High time-decay risk — theta will eat premium"],
  };
  const [cls, title] = map[risk ?? "low"] ?? map.low;
  return <span className={"inline-block h-2.5 w-2.5 rounded-full " + cls} title={title} />;
}
