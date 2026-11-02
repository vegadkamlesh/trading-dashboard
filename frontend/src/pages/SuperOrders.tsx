import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api, ApiError } from "../api/client";
import { useToast } from "../hooks/useToasts";
import type { SuperOrder } from "../api/types";

export function Orders() {
  const { data, error } = usePolling<Array<{
    orderId: string; orderStatus: string; tradingSymbol?: string; securityId: string;
    transactionType: string; quantity: number; filledQty?: number; averageTradedPrice?: number;
  }>>(() => api.get("/api/orders/book"), 1000);
  return <div className="space-y-4">
    <section className="card">
      <h2 className="mb-2 font-semibold">Standard broker orders</h2>
      <p className="mb-2 text-xs text-slate-400">Dhan website, app and API orders · account-wise · 1s polling.
        Filled BUY history does not mean the holding is still open; see Positions.</p>
      {error ? <div role="alert" className="text-sm text-rose-300">{error}</div> : !data
        ? <Empty msg="Loading broker order book…" /> : data.length === 0
          ? <Empty msg="No standard orders today." /> : <div className="overflow-auto">
            <table className="w-full text-left text-xs">
              <thead><tr>{["Order ID", "Contract", "Side", "Status", "Qty", "Filled", "Avg fill"].map(
                (label) => <th key={label} className="px-2 py-2">{label}</th>)}</tr></thead>
              <tbody>{data.map((row) => <tr key={row.orderId} className="border-t border-slate-800">
                <td className="px-2 py-2">{row.orderId}</td><td>{row.tradingSymbol || row.securityId}</td>
                <td>{row.transactionType}</td><td><span className={badge(row.orderStatus)}>{row.orderStatus}</span></td>
                <td>{row.quantity}</td><td>{row.filledQty ?? "—"}</td><td>{n(row.averageTradedPrice)}</td>
              </tr>)}</tbody>
            </table>
          </div>}
    </section>
    <h2 className="font-semibold">Super broker orders</h2>
    <SuperOrders />
  </div>;
}

export function SuperOrders() {
  const { push } = useToast();
  const { data, error, refresh } = usePolling<SuperOrder[]>(
    () => api.get("/api/orders/super"),
    1000
  );
  const [pin, setPin] = useState("");

  const cancelAll = async (orderId: string) => {
    const p = pin || window.prompt("Enter order confirm PIN to cancel:") || "";
    if (!p) return;
    try {
      const res = await api.post<{ dryRun: boolean }>("/api/orders/cancel", {
        orderId,
        leg: "ENTRY_LEG",
        pin: p,
      });
      push("info", res.dryRun ? "DRY-RUN cancel logged" : "Cancellation requested — wait for broker confirmation");
      refresh();
    } catch (e) {
      push("error", e instanceof ApiError ? String((e.detail as any)?.errorMessage || e.message) : "Cancel failed");
    }
  };

  if (error) return <Empty msg={`Error: ${error}`} />;
  if (!data) return <Empty msg="Loading super orders…" />;
  if (data.length === 0) return <Empty msg="No super orders today." />;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-xs text-slate-400">
        <span className="inline-block h-2 w-2 rounded-full bg-green-500" />
        <span>Broker book · refresh every 1s · TRADED entry does not mean position is still open</span>
        <button className="tab px-2 py-0.5 text-[11px]" onClick={refresh} title="Refresh now">
          ⟳ Refresh
        </button>
        <span className="mx-2 text-slate-600">|</span>
        <span>Cancel PIN:</span>
        <input
          className="input w-28 font-mono"
          type="password"
          value={pin}
          onChange={(e) => setPin(e.target.value)}
          placeholder="••••••"
        />
      </div>
      <div className="overflow-auto rounded-lg border border-slate-800">
        <table className="w-full text-xs">
          <thead className="bg-slate-900 text-slate-400">
            <tr>
              {["Order ID", "Entry status", "Side", "Qty", "Filled", "Entry", "LTP", "Target / status", "SL / status", "Time", ""].map(
                (h) => (
                  <th key={h} className="px-3 py-2 text-left font-semibold">
                    {h}
                  </th>
                )
              )}
            </tr>
          </thead>
          <tbody>
            {data.map((o) => {
              const target = o.legDetails?.find((l) => l.legName === "TARGET_LEG");
              const sl = o.legDetails?.find((l) => l.legName === "STOP_LOSS_LEG");
              return (
                <tr key={o.orderId} className="border-b border-slate-900 hover:bg-slate-900/60">
                  <td className="px-3 py-2 font-mono">{o.orderId}</td>
                  <td className="px-3 py-2">
                    <span className={badge(o.orderStatus)}>{o.orderStatus}</span>
                  </td>
                  <td className="px-3 py-2">{o.transactionType}</td>
                  <td className="px-3 py-2 font-mono">{o.quantity ?? "-"}</td>
                  <td className="px-3 py-2 font-mono">{o.filledQty ?? 0}</td>
                  <td className="px-3 py-2 font-mono">{n(o.price)}</td>
                  <td className="px-3 py-2 font-mono" title={o.quoteError ?? undefined}>{o.quoteError ? "STALE" : n(o.ltp)}</td>
                  <td className="px-3 py-2 font-mono text-green-400">{n(target?.price)} <span className="text-slate-400">{target?.orderStatus}</span></td>
                  <td className="px-3 py-2 font-mono text-red-400">{n(sl?.price)} <span className="text-slate-400">{sl?.orderStatus}</span></td>
                  <td className="px-3 py-2 text-slate-400">{o.createTime ?? "-"}</td>
                  <td className="px-3 py-2">
                    <button className="btn-ghost px-2 py-0.5 text-[10px]" onClick={() => cancelAll(o.orderId)}
                      disabled={!["TRANSIT", "PENDING", "PART_TRADED"].includes(o.orderStatus.toUpperCase())}>
                      Cancel
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function badge(s: string): string {
  const base = "rounded px-1.5 py-0.5 text-[10px] font-bold ";
  if (s === "TRADED" || s === "CLOSED") return base + "bg-green-500/20 text-green-300";
  if (s === "REJECTED" || s === "CANCELLED") return base + "bg-red-500/20 text-red-300";
  if (s === "PENDING" || s === "TRIGGERED" || s === "PART_TRADED")
    return base + "bg-amber-500/20 text-amber-300";
  return base + "bg-slate-700 text-slate-200";
}
function n(v: number | undefined): string {
  return typeof v === "number" ? v.toFixed(2) : "-";
}
function Empty({ msg }: { msg: string }) {
  return <div className="p-8 text-center text-sm text-slate-500">{msg}</div>;
}
