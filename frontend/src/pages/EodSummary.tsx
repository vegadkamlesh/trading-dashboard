import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type { EodSummary as EodType } from "../api/types";

export function EodSummary() {
  const [hours, setHours] = useState(24);
  const { data, error } = usePolling<EodType>(
    () => api.get(`/api/audit/eod?hours=${hours}`),
    15000
  );

  if (error) return <Empty msg={`Error: ${error}`} />;
  if (!data) return <Empty msg="Loading…" />;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <span className="text-slate-400">Window:</span>
        {[8, 24, 72].map((h) => (
          <button
            key={h}
            className={"tab " + (hours === h ? "tab-active" : "")}
            onClick={() => setHours(h)}
          >
            {h}h
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat label="Total Attempts" value={data.totalAttempts} />
        <Stat label="Dry-run" value={data.dryRunAttempts} />
        <Stat label="Live Success" value={data.liveSuccess} good />
        <Stat label="Live Failed" value={data.liveFailed} bad />
      </div>

      <div className="overflow-auto rounded-lg border border-slate-800">
        <table className="w-full text-xs">
          <thead className="bg-slate-900 text-slate-400">
            <tr>
              {["Time", "Action", "Index", "Type", "Side", "Qty", "Entry", "Target", "Mode", "Status", "Message"].map(
                (h) => (
                  <th key={h} className="px-3 py-2 text-left font-semibold">
                    {h}
                  </th>
                )
              )}
            </tr>
          </thead>
          <tbody>
            {data.entries.map((e: any, i) => (
              <tr key={i} className="border-b border-slate-900 hover:bg-slate-900/60">
                <td className="px-3 py-1.5 text-slate-400">
                  {new Date(e.ts * 1000).toLocaleTimeString("en-IN")}
                </td>
                <td className="px-3 py-1.5">{e.action}</td>
                <td className="px-3 py-1.5">{e.index_key ?? "-"}</td>
                <td className="px-3 py-1.5">{e.option_type ?? "-"}</td>
                <td className="px-3 py-1.5">{e.transaction_type ?? "-"}</td>
                <td className="px-3 py-1.5 font-mono">{e.quantity ?? "-"}</td>
                <td className="px-3 py-1.5 font-mono">{e.price ?? "-"}</td>
                <td className="px-3 py-1.5 font-mono text-green-400">{e.target_price ?? "-"}</td>
                <td className="px-3 py-1.5">{e.dry_run ? "DRY" : "LIVE"}</td>
                <td className="px-3 py-1.5">{e.status ?? "-"}</td>
                <td className="px-3 py-1.5 text-slate-400">{e.message ?? "-"}</td>
              </tr>
            ))}
            {data.entries.length === 0 && (
              <tr>
                <td colSpan={11} className="px-3 py-6 text-center text-slate-500">
                  No order activity in this window.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Stat({ label, value, good, bad }: { label: string; value: number; good?: boolean; bad?: boolean }) {
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={"mt-1 text-2xl font-bold " + (good ? "text-green-400" : bad ? "text-red-400" : "")}>
        {value}
      </div>
    </div>
  );
}
function Empty({ msg }: { msg: string }) {
  return <div className="p-8 text-center text-sm text-slate-500">{msg}</div>;
}
