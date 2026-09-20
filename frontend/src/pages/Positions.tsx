import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api, ApiError } from "../api/client";
import { useToast } from "../hooks/useToasts";
import type { Position } from "../api/types";

// Live P&L every ~7s (fetching positions is a light call; we stay well clear of
// Dhan's rate limit). Each open position gets a one-click EXIT (market
// square-off) so you can get out instantly, like a real terminal.
const POLL_MS = 7000;

export function Positions() {
  const { push } = useToast();
  const { data, error, refresh } = usePolling<Position[]>(
    () => api.get("/api/account/positions"),
    POLL_MS
  );
  const [exiting, setExiting] = useState<string | null>(null);

  const exitPosition = async (p: Position) => {
    const qty = Math.abs(p.netQty ?? 0);
    if (!qty) {
      push("info", "Nothing to exit (qty 0).");
      return;
    }
    // To close a LONG (qty>0) you SELL; to close a SHORT you BUY.
    const exitTxn = (p.netQty ?? 0) > 0 ? "SELL" : "BUY";
    if (!confirm(`Exit ${sym(p)} — ${exitTxn} ${qty} @ MARKET?`)) return;

    setExiting(String(p.securityId));
    try {
      const res = await api.post<{ dryRun: boolean; result?: { orderId?: string } }>(
        "/api/account/squareoff",
        {
          securityId: String(p.securityId),
          exchangeSegment: p.exchangeSegment ?? "NSE_FNO",
          quantity: qty,
          productType: p.productType ?? "INTRADAY",
          transactionType: exitTxn,
        }
      );
      push(
        res.dryRun ? "info" : "success",
        res.dryRun ? "DRY-RUN: exit logged (not sent)" : `Exit placed: ${res.result?.orderId ?? ""}`
      );
      refresh();
    } catch (e) {
      if (e instanceof ApiError) {
        const d = e.detail as { errorMessage?: string; detail?: string };
        const msg = d?.errorMessage || d?.detail || "Exit failed";
        if (msg.toLowerCase().includes("invalid ip")) {
          push("error", "Invalid IP: whitelist your IP in Connection ▸ Register IP, then retry.");
        } else {
          push("error", msg);
        }
      } else {
        push("error", "Exit failed");
      }
    } finally {
      setExiting(null);
    }
  };

  if (error) return <Empty msg={`Error: ${error}`} />;
  if (!data) return <Empty msg="Loading positions…" />;
  if (data.length === 0) return <Empty msg="No open positions." />;

  const totalPnl = data.reduce(
    (s, p) => s + (p.realizedProfit ?? 0) + (p.unrealizedProfit ?? 0),
    0
  );

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between px-1">
        <div className="text-xs text-slate-400">
          Live P&amp;L · auto-refresh every {POLL_MS / 1000}s
        </div>
        <div className={"font-mono text-sm font-bold " + pnl(totalPnl)}>
          Total: {money(totalPnl)}
        </div>
      </div>
      <div className="overflow-auto rounded-lg border border-slate-800">
        <table className="w-full text-xs">
          <thead className="bg-slate-900 text-slate-400">
            <tr>
              {[
                "Symbol",
                "Prod",
                "Qty",
                "Buy Avg",
                "Sell Avg",
                "LTP",
                "Realized",
                "Unrealized",
                "Net PnL",
                "",
              ].map((h) => (
                <th key={h} className="px-3 py-2 text-left font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((p, i) => {
              const net = (p.realizedProfit ?? 0) + (p.unrealizedProfit ?? 0);
              const sid = String(p.securityId);
              return (
                <tr key={i} className="border-b border-slate-900 hover:bg-slate-900/60">
                  <td className="px-3 py-2 font-medium">{sym(p)}</td>
                  <td className="px-3 py-2">{p.productType ?? "-"}</td>
                  <td className="px-3 py-2 font-mono">{p.netQty ?? 0}</td>
                  <td className="px-3 py-2 font-mono">{num(p.buyAvg)}</td>
                  <td className="px-3 py-2 font-mono">{num(p.sellAvg)}</td>
                  <td className="px-3 py-2 font-mono text-sky-300">{num(p.ltp)}</td>
                  <td className="px-3 py-2 font-mono">{num(p.realizedProfit)}</td>
                  <td className={"px-3 py-2 font-mono " + pnl(p.unrealizedProfit ?? 0)}>
                    {num(p.unrealizedProfit)}
                  </td>
                  <td className={"px-3 py-2 font-mono font-bold " + pnl(net)}>{num(net)}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      className="rounded bg-red-600 px-2.5 py-1 text-[11px] font-bold text-white hover:bg-red-700 disabled:opacity-50"
                      onClick={() => exitPosition(p)}
                      disabled={exiting === sid}
                      title="Square off this position with a MARKET order"
                    >
                      {exiting === sid ? "…" : "EXIT"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot className="bg-slate-900">
            <tr>
              <td colSpan={8} className="px-3 py-2 text-right font-semibold">
                Total P&amp;L
              </td>
              <td className={"px-3 py-2 font-mono font-bold " + pnl(totalPnl)}>
                {num(totalPnl)}
              </td>
              <td />
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

function sym(p: Position): string {
  if (p.tradingSymbol) return String(p.tradingSymbol);
  const parts = [p.drvOptionType, p.drvStrikePrice].filter(Boolean).join(" ");
  return parts || String(p.securityId ?? "-");
}
function num(n: unknown): string {
  return typeof n === "number" ? n.toFixed(2) : "-";
}
function money(n: number): string {
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}
function pnl(n: number): string {
  return n > 0 ? "text-green-400" : n < 0 ? "text-red-400" : "text-slate-400";
}
function Empty({ msg }: { msg: string }) {
  return <div className="p-8 text-center text-sm text-slate-500">{msg}</div>;
}
