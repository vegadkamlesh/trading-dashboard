import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Seasonality } from "../api/types";

// "Aaj ki date / weekday ko last N saal me kya hua?" — seasonal context so the
// trader knows if this day historically leans up or down.
export function SeasonalityPanel({ indexKey }: { indexKey: string }) {
  const [data, setData] = useState<Seasonality | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setData(await api.get<Seasonality>(`/api/intel/seasonality?index=${indexKey}`));
    } catch {
      /* keep previous */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setData(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indexKey]);

  if (loading && !data) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-xs text-slate-400">
        Loading seasonality (5-yr history)…
      </div>
    );
  }
  if (!data || data.error) {
    return (
      <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3 text-xs text-slate-400">
        Seasonality unavailable{data?.error ? ` (${data.error})` : ""}.
        <button className="ml-2 underline" onClick={load}>
          retry
        </button>
      </div>
    );
  }

  const sd = data.sameDate;
  const wd = data.weekdayStats;
  const sdLean =
    sd.avgPct == null ? "neutral" : sd.avgPct > 0.1 ? "up" : sd.avgPct < -0.1 ? "down" : "neutral";
  const wdLean =
    wd.avgPct == null ? "neutral" : wd.avgPct > 0.1 ? "up" : wd.avgPct < -0.1 ? "down" : "neutral";

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40">
      <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-lg">📅</span>
          <span className="text-sm font-bold uppercase tracking-wide text-slate-200">
            Seasonality
          </span>
        </div>
        <span className="text-xs text-slate-500">{data.barsAnalyzed} days analysed</span>
      </div>

      <div className="grid gap-4 p-4 md:grid-cols-2">
        {/* Same date */}
        <div>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              This date ({data.today.slice(5)})
            </span>
            <Lean lean={sdLean} />
          </div>
          <div className="mb-2 text-xs text-slate-400">
            Avg{" "}
            <span className={pctColor(sd.avgPct)}>
              {sd.avgPct != null ? `${sd.avgPct > 0 ? "+" : ""}${sd.avgPct}%` : "-"}
            </span>{" "}
            · Up {sd.upDays}/{sd.total} years
          </div>
          <table className="w-full text-xs">
            <tbody>
              {sd.samples.map((s) => (
                <tr key={s.date} className="border-t border-slate-800">
                  <td className="py-1 font-mono text-slate-400">{s.date}</td>
                  <td className="py-1 text-right font-mono text-slate-400">
                    {s.close.toFixed(0)}
                  </td>
                  <td className={"py-1 text-right font-mono " + pctColor(s.changePct)}>
                    {s.changePct != null ? `${s.changePct > 0 ? "+" : ""}${s.changePct}%` : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Weekday */}
        <div>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              {wd.weekday} effect
            </span>
            <Lean lean={wdLean} />
          </div>
          {wd.samples ? (
            <div className="grid grid-cols-2 gap-2 text-xs">
              <Stat label={`Last ${wd.samples} ${wd.weekday}s`} value={`${wd.avgPct! > 0 ? "+" : ""}${wd.avgPct}%`} color={pctColor(wd.avgPct)} />
              <Stat label="Win rate" value={`${wd.winRate}%`} color={wd.winRate! >= 50 ? "text-green-400" : "text-red-400"} />
              <Stat label="Best" value={`+${wd.bestPct}%`} color="text-green-400" />
              <Stat label="Worst" value={`${wd.worstPct}%`} color="text-red-400" />
              <div className="col-span-2 rounded bg-slate-950/40 px-2 py-1.5 text-slate-400">
                Historically, <span className="font-semibold text-slate-200">{wd.weekday}s</span>{" "}
                close <span className={pctColor(wd.avgPct)}>
                  {wdLean === "up" ? "green" : wdLean === "down" ? "red" : "flat"}
                </span>{" "}
                {wd.winRate}% of the time.
              </div>
            </div>
          ) : (
            <div className="text-xs text-slate-500">No weekday data.</div>
          )}
        </div>
      </div>
      <p className="px-4 pb-3 text-[10px] leading-tight text-slate-500">
        Seasonality is context, not a signal — past patterns can and do break. Use it
        alongside trend, price action and risk management.
      </p>
    </div>
  );
}

function Lean({ lean }: { lean: string }) {
  const map: Record<string, [string, string]> = {
    up: ["text-green-400 bg-green-500/10 border-green-600/40", "▲ leans up"],
    down: ["text-red-400 bg-red-500/10 border-red-600/40", "▼ leans down"],
    neutral: ["text-slate-400 bg-slate-700/20 border-slate-600/40", "▬ flat"],
  };
  const [cls, txt] = map[lean] ?? map.neutral;
  return <span className={"rounded border px-1.5 py-0.5 text-[10px] font-semibold " + cls}>{txt}</span>;
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="rounded bg-slate-950/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={"font-mono font-semibold " + (color ?? "text-slate-200")}>{value}</div>
    </div>
  );
}

function pctColor(v: number | null | undefined): string {
  if (v == null) return "text-slate-400";
  if (v > 0.05) return "text-green-400";
  if (v < -0.05) return "text-red-400";
  return "text-slate-400";
}
