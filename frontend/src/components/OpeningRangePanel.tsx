import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { OpeningRangeResponse, OpeningRangeStudy } from "../api/types";

// "Pichle N saal me har din 9:15–9:25 ka high/low kitna tha?" — the opening
// range. It sizes your stop (a stop tighter than this gets shaken out) and is
// the band ORB traders trade the break of.
//
// A 5-year study is ~21 chunked Dhan calls, so the backend runs it as a
// BACKGROUND JOB: we poll until status becomes "ready".
export function OpeningRangePanel({ indexKey }: { indexKey: string }) {
  const [years, setYears] = useState(5);
  const [data, setData] = useState<OpeningRangeStudy | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [computing, setComputing] = useState(false);
  const [idle, setIdle] = useState(false);
  const timer = useRef<number | null>(null);

  const stop = () => {
    if (timer.current) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  };

  const poll = useCallback(
    async (refresh = false) => {
      stop();
      setComputing(true);
      try {
        const res = await api.get<OpeningRangeResponse>(
          `/api/intel/opening-range?index=${indexKey}&years=${years}${refresh ? "&refresh=1" : ""}`
        );
        if (res.status === "ready" && res.data) {
          setData(res.data);
          setProgress(null);
          setComputing(false);
          setIdle(false);
          return;
        }
        if (res.status === "computing") {
          setProgress(res.progress);
          setComputing(true);
          setIdle(false);
          timer.current = window.setTimeout(() => poll(false), 2000);
          return;
        }
        // idle (never started / expired)
        setComputing(false);
        setIdle(true);
      } catch {
        setComputing(false);
      }
    },
    [indexKey, years]
  );

  useEffect(() => {
    setData(null);
    setProgress(null);
    setIdle(false);
    poll(false);
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indexKey, years]);

  const pctDone =
    progress && progress.total ? Math.round((progress.done / progress.total) * 100) : 0;

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-lg">🕘</span>
          <span className="text-sm font-bold uppercase tracking-wide text-slate-200">
            Opening Range · 9:15–9:25
          </span>
          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
            {indexKey}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded border border-slate-700">
            {[1, 3, 5].map((y) => (
              <button
                key={y}
                className={
                  "px-2 py-0.5 text-[11px] " +
                  (years === y ? "bg-sky-600 text-white" : "text-slate-400 hover:bg-slate-800")
                }
                onClick={() => setYears(y)}
              >
                {y}Y
              </button>
            ))}
          </div>
          <button
            className="tab px-2 py-0.5 text-[11px]"
            onClick={() => poll(true)}
            disabled={computing}
            title="Recompute now"
          >
            ⟳
          </button>
          {data && (
            <span className="text-[11px] text-slate-500">{data.sampleDays} days</span>
          )}
        </div>
      </div>

      {computing && (
        <div className="px-4 py-3">
          <div className="mb-1 flex justify-between text-[11px] text-slate-400">
            <span>
              Computing {years}-year study… (one-time, then cached)
            </span>
            <span className="font-mono">
              {progress ? `${progress.done}/${progress.total}` : "…"}
            </span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded bg-slate-800">
            <div
              className="h-full bg-sky-500 transition-all"
              style={{ width: `${Math.max(4, pctDone)}%` }}
            />
          </div>
        </div>
      )}

      {!computing && idle && !data && (
        <div className="px-4 py-3 text-xs text-slate-400">
          No data yet.
          <button className="ml-2 underline" onClick={() => poll(true)}>
            compute now
          </button>
        </div>
      )}

      {data && (
        <>
          {data.error && (
            <div className="px-4 pt-2 text-[11px] text-amber-400">⚠ {data.error}</div>
          )}
          <div className="grid grid-cols-2 gap-2 p-4 md:grid-cols-4">
            <Stat
              label="Avg range (pts)"
              value={fmt(data.avgRangePoints)}
              sub={`${fmt(data.avgRangePct, 3)}% of price`}
              color="text-sky-300"
            />
            <Stat
              label="Median range"
              value={`${fmt(data.medianRangePct, 3)}%`}
              sub={`p90 ${fmt(data.p90RangePct, 3)}%`}
            />
            <Stat
              label="Biggest / smallest"
              value={`${fmt(data.maxRangePct, 2)}%`}
              sub={`min ${fmt(data.minRangePct, 3)}%`}
            />
            <Stat
              label="Share of day's range"
              value={data.openingShareOfDayPct != null ? `${data.openingShareOfDayPct}%` : "-"}
              sub={`avg day ${fmt(data.avgDayRangePct, 2)}%`}
            />
          </div>

          {/* Breakout behaviour */}
          <div className="grid grid-cols-2 gap-2 px-4 md:grid-cols-4">
            <Stat
              label="Broke above range"
              value={pct(data.breakUpRate)}
              sub="day high > 9:25 high"
              color="text-green-400"
            />
            <Stat
              label="Broke below range"
              value={pct(data.breakDownRate)}
              sub="day low < 9:25 low"
              color="text-red-400"
            />
            <Stat
              label="Closed above"
              value={pct(data.closeAboveRate)}
              sub="strong up day"
              color="text-green-400"
            />
            <Stat
              label="Closed below"
              value={pct(data.closeBelowRate)}
              sub="strong down day"
              color="text-red-400"
            />
          </div>
          <div className="px-4 pt-2 text-[11px] text-slate-400">
            Closed back inside the band on{" "}
            <span className="font-semibold text-slate-200">{pct(data.closeInsideRate)}</span> of
            days (choppy / range-bound) — a real ORB needs a decisive break, not a fakeout.
          </div>

          {/* Distribution */}
          <div className="p-4">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              How big is the opening range?
            </div>
            <div className="space-y-1">
              {data.rangeBuckets.map((b) => (
                <div key={b.label} className="flex items-center gap-2 text-[11px]">
                  <span className="w-20 shrink-0 text-right font-mono text-slate-400">
                    {b.label}
                  </span>
                  <div className="h-3 flex-1 overflow-hidden rounded bg-slate-800">
                    <div
                      className="h-full bg-sky-500/70"
                      style={{ width: `${Math.min(100, b.pct * 2)}%` }}
                    />
                  </div>
                  <span className="w-14 shrink-0 font-mono text-slate-400">
                    {b.pct}%
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Recent sessions */}
          <div className="border-t border-slate-800 p-4">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Recent sessions
            </div>
            <div className="max-h-64 overflow-auto">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-slate-900 text-slate-500">
                  <tr>
                    {["Date", "Open", "9:25 High", "9:25 Low", "Range", "Range %", "Day Close", "Break"].map(
                      (h) => (
                        <th key={h} className="px-2 py-1 text-right font-semibold first:text-left">
                          {h}
                        </th>
                      )
                    )}
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((r) => (
                    <tr key={r.date} className="border-t border-slate-800/70">
                      <td className="px-2 py-1 text-left font-mono text-slate-400">{r.date}</td>
                      <td className="px-2 py-1 text-right font-mono">{fmt(r.open, 2)}</td>
                      <td className="px-2 py-1 text-right font-mono text-green-400">
                        {fmt(r.orHigh, 2)}
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-red-400">
                        {fmt(r.orLow, 2)}
                      </td>
                      <td className="px-2 py-1 text-right font-mono">{fmt(r.rangePoints, 1)}</td>
                      <td className="px-2 py-1 text-right font-mono text-sky-300">
                        {fmt(r.rangePct, 3)}%
                      </td>
                      <td className="px-2 py-1 text-right font-mono text-slate-400">
                        {fmt(r.dayClose, 2)}
                      </td>
                      <td className="px-2 py-1 text-right">
                        {r.brokeUp && <span className="text-green-400">▲</span>}
                        {r.brokeDown && <span className="text-red-400">▼</span>}
                        {!r.brokeUp && !r.brokeDown && <span className="text-slate-500">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <p className="px-4 pb-3 text-[10px] leading-tight text-slate-500">
            Opening range = high/low of the first 10 minutes (09:15–09:25). A stop tighter
            than this band is inside the noise. Past ranges guide expectations, they don't
            guarantee today's move.
          </p>
        </>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="rounded bg-slate-950/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={"font-mono text-sm font-semibold " + (color ?? "text-slate-200")}>
        {value}
      </div>
      {sub && <div className="text-[10px] text-slate-500">{sub}</div>}
    </div>
  );
}

function fmt(v: number | null | undefined, dp = 2): string {
  if (v == null || Number.isNaN(v)) return "-";
  return v.toFixed(dp);
}

function pct(v: number | null | undefined): string {
  if (v == null) return "-";
  return `${v}%`;
}
