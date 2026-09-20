import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Guidance } from "../api/types";

// Shows the explainable advisor verdict for the selected leg. Refetches when
// index/optionType changes. Includes a manual "re-analyse" (refresh) button so
// the heavy history fetch is only triggered on demand.
export function GuidancePanel({
  indexKey,
  optionType,
  onApplyTarget,
}: {
  indexKey: string;
  optionType: "CE" | "PE";
  onApplyTarget?: (pct: number) => void;
}) {
  const [data, setData] = useState<Guidance | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (refresh: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const q = `index=${indexKey}&optionType=${optionType}&historyDays=90`;
      // refresh just busts the client cache feel; server caches history for 12h.
      const g = await api.get<Guidance>(`/api/intel/guidance?${q}${refresh ? "&_=" + Date.now() : ""}`);
      setData(g);
    } catch (e) {
      setError("Could not load guidance (history may be warming up).");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indexKey, optionType]);

  if (loading && !data) {
    return <div className="mt-4 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-xs text-slate-400">Analysing markets & 5-yr history…</div>;
  }
  if (error || !data) {
    return (
      <div className="mt-4 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-xs text-slate-400">
        {error ?? "No guidance available."}
        <button className="ml-2 underline" onClick={() => load(true)}>retry</button>
      </div>
    );
  }

  const verdictColor =
    data.verdict === "BUY"
      ? "bg-green-500/20 text-green-300 border-green-600/40"
      : data.verdict === "SELL"
      ? "bg-red-500/20 text-red-300 border-red-600/40"
      : "bg-amber-500/20 text-amber-300 border-amber-600/40";

  return (
    <div className="mt-4 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-sm">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
          Trade Advisor
        </span>
        <button
          className="text-xs text-slate-400 underline hover:text-slate-200"
          onClick={() => load(true)}
          disabled={loading}
        >
          {loading ? "…" : "re-analyse"}
        </button>
      </div>

      <div className="flex items-center gap-3">
        <span className={`rounded-md border px-3 py-1 text-base font-bold ${verdictColor}`}>
          {data.verdict}
        </span>
        <div className="flex-1">
          <div className="flex justify-between text-xs text-slate-400">
            <span>Confidence</span>
            <span className="font-mono">{data.confidence}%</span>
          </div>
          <div className="mt-1 h-2 w-full overflow-hidden rounded bg-slate-800">
            <div
              className={
                "h-full " +
                (data.verdict === "BUY"
                  ? "bg-green-500"
                  : data.verdict === "SELL"
                  ? "bg-red-500"
                  : "bg-amber-500")
              }
              style={{ width: `${data.confidence}%` }}
            />
          </div>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <Stat label="Trend" value={data.trend ?? "-"} />
        <Stat label="RSI" value={data.rsi?.toFixed(0) ?? "-"} />
        <Stat label="IV" value={data.iv?.toFixed(1) ?? "-"} />
        <Stat label="P(up)" value={`${Math.round(data.probabilityUp * 100)}%`} />
        <Stat label="Win rate" value={data.history?.winRate != null ? `${data.history.winRate.toFixed(0)}%` : "-"} />
        <Stat label="Sample" value={data.history?.sampleDays ? `${data.history.sampleDays}d` : "-"} />
      </div>

      {/* Suggested plan + apply button */}
      <div className="mt-3 flex items-center justify-between rounded border border-slate-700 bg-slate-950/40 px-2 py-1.5 text-xs">
        <span className="text-slate-400">
          Suggested target{" "}
          <span className="font-mono text-green-400">+{data.suggestedTargetPct}%</span>
          {" · "}safety SL{" "}
          <span className="font-mono text-red-400">-{data.suggestedSafetySlPct}%</span>
        </span>
        {onApplyTarget && (
          <button
            className="tab px-2 py-0.5"
            onClick={() => onApplyTarget(data.suggestedTargetPct)}
          >
            use {data.suggestedTargetPct}%
          </button>
        )}
      </div>

      {/* Reasons */}
      <ul className="mt-3 space-y-1 text-xs text-slate-300">
        {data.reasons.map((r, i) => (
          <li key={i} className="flex gap-1.5">
            <span className="text-slate-500">•</span>
            <span>{r}</span>
          </li>
        ))}
      </ul>

      {/* Scenario table: spot move -> option move */}
      {data.scenarios.length > 0 && (
        <div className="mt-3">
          <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">
            If spot moves…
          </div>
          <div className="overflow-hidden rounded border border-slate-800">
            <table className="w-full text-xs">
              <thead className="bg-slate-900 text-slate-400">
                <tr>
                  <th className="px-2 py-1 text-left">Spot</th>
                  <th className="px-2 py-1 text-right">Level</th>
                  <th className="px-2 py-1 text-right">Option est.</th>
                </tr>
              </thead>
              <tbody>
                {data.scenarios.map((s) => (
                  <tr key={s.spotMovePct} className="border-t border-slate-900">
                    <td className="px-2 py-0.5 font-mono">{s.spotMovePct > 0 ? "+" : ""}{s.spotMovePct}%</td>
                    <td className="px-2 py-0.5 text-right font-mono">{s.spotLevel}</td>
                    <td
                      className={
                        "px-2 py-0.5 text-right font-mono " +
                        (s.optionMovePct > 0
                          ? "text-green-400"
                          : s.optionMovePct < 0
                          ? "text-red-400"
                          : "text-slate-400")
                      }
                    >
                      {s.optionMovePct > 0 ? "+" : ""}{s.optionMovePct}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <p className="mt-3 text-[10px] leading-tight text-slate-500">{data.disclaimer}</p>
      {data.history?.error && (
        <p className="mt-1 text-[10px] text-amber-500">History note: {data.history.error}</p>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-slate-950/40 px-2 py-1">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="font-mono text-slate-200">{value}</div>
    </div>
  );
}
