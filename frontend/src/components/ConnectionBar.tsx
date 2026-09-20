import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type { StatusResponse } from "../api/types";
import { useAuth } from "../hooks/useAuth";

// Shows order-path health + dry-run state + whitelisted IP. Non-blocking.
export function ConnectionBar({ onLogout }: { onLogout: () => void }) {
  const { data } = usePolling<StatusResponse>(() => api.get("/api/account/status"), 30000);
  const { dryRun, fastOrders, refreshFlags } = useAuth();

  // Prefer the live auth-context flags (instant after a toggle); fall back to poll.
  const dry = dryRun ?? data?.dryRun ?? true;
  const fast = fastOrders ?? data?.fastOrders ?? false;
  const ok = data?.profileOk ?? false;

  return (
    <div className="flex items-center gap-3 text-xs">
      <span
        className={
          "rounded px-2 py-1 font-bold " +
          (dry ? "bg-amber-500/20 text-amber-300" : "bg-red-500/20 text-red-300")
        }
        title={dry ? "Orders are validated & logged only" : "LIVE orders are being sent to Dhan"}
      >
        {dry ? "DRY-RUN" : "LIVE"}
      </span>
      <span
        className={
          "rounded px-2 py-1 font-bold " +
          (fast ? "bg-sky-500/20 text-sky-300" : "bg-slate-700/40 text-slate-400")
        }
        title={
          fast
            ? "FAST ORDERS ON — order PIN is skipped, 1 click places a trade"
            : "Fast orders off — order PIN required"
        }
      >
        {fast ? "FAST ON" : "FAST OFF"}
      </span>
      <button
        className="text-slate-500 hover:text-slate-300"
        title="Refresh status"
        onClick={() => refreshFlags()}
      >
        ⟳
      </button>
      <span
        className={
          "flex items-center gap-1 " + (ok ? "text-green-400" : "text-red-400")
        }
        title={data?.profileError ? JSON.stringify(data.profileError) : "Profile OK"}
      >
        <span className={"inline-block h-2 w-2 rounded-full " + (ok ? "bg-green-500" : "bg-red-500")} />
        {ok ? "API OK" : "API ERR"}
      </span>
      {data?.ip?.primaryIP && (
        <span className="text-slate-400" title="Whitelisted static IP on your Dhan account">
          IP: <span className="font-mono">{String(data.ip.primaryIP)}</span>
        </span>
      )}
      {data?.activeSegment && <span className="text-slate-500">{data.activeSegment}</span>}
      <button className="text-slate-400 hover:text-slate-200" onClick={onLogout}>
        Logout
      </button>
    </div>
  );
}
