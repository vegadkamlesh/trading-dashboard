import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { OrderPreviewResponse, OptionRow } from "../api/types";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToasts";

export interface TicketSeed {
  indexKey: string;
  indexName: string;
  optionType: "CE" | "PE";
  transactionType: "BUY" | "SELL";
  row: OptionRow;
}

// Quick profit presets (%), plus a manual field.
const PRESETS = [2.5, 5, 10, 15, 20, 25];

export function OrderTicket({
  seed,
  onClose,
}: {
  seed: TicketSeed;
  onClose: () => void;
}) {
  const { push } = useToast();
  const { fastOrders } = useAuth();
  const leg = seed.optionType === "CE" ? seed.row.ce : seed.row.pe;
  // Default the "ref" price to LTP (fallback ask/bid). This is just the
  // starting value — the trader edits the LIMIT price to whatever they want.
  const refPrice = leg.ltp ?? leg.ask ?? leg.bid ?? 0;

  const [quantity, setQuantity] = useState(1);
  const [price, setPrice] = useState<number>(round(refPrice));
  const [targetPct, setTargetPct] = useState<number>(5);
  const [productType, setProductType] = useState<"INTRADAY" | "MARGIN">("INTRADAY");
  const [orderType, setOrderType] = useState<"LIMIT" | "MARKET">("LIMIT");
  const [noStopLoss, setNoStopLoss] = useState(true);
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<OrderPreviewResponse | null>(null);

  // Lot size for this index (from the server preview). Each index differs
  // (NIFTY 65, SENSEX 20, BANKNIFTY 30, ...), so units = lots × lotSize.
  const lotSize = preview?.preview.lotSize ?? 1;
  const units = quantity * lotSize;

  const body = useMemo(
    () => ({
      indexKey: seed.indexKey,
      securityId: String(leg.securityId ?? ""),
      optionType: seed.optionType,
      transactionType: seed.transactionType,
      side: `${seed.indexName} ${seed.row.strike} ${seed.optionType}`,
      quantity, // LOTS — server multiplies by lot size
      lots: true,
      // MARKET price is validated server-side via the ref price.
      price: orderType === "MARKET" ? refPrice : price,
      targetPct,
      productType,
      orderType,
      noStopLoss,
    }),
    [seed, leg, quantity, price, targetPct, productType, orderType, noStopLoss, refPrice]
  );

  // Fetch a live preview whenever inputs change (simple debounce).
  useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(async () => {
      try {
        const p = await api.post<OrderPreviewResponse>("/api/orders/preview", body);
        if (!cancelled) setPreview(p);
      } catch {
        if (!cancelled) setPreview(null);
      }
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [body]);

  const place = async () => {
    // FAST ORDERS ON → PIN is skipped (backend also skips it); 1 click places.
    if (!fastOrders && !pin) {
      push("error", "Enter your order confirm PIN");
      return;
    }
    setBusy(true);
    try {
      const res = await api.post<{
        ok: boolean;
        dryRun: boolean;
        result?: { orderId?: string; orderStatus?: string };
      }>("/api/orders/place", { ...body, pin });
      if (res.dryRun) {
        push("info", "DRY-RUN: order validated & logged (not sent to Dhan)");
      } else {
        push("success", `Order placed: ${res.result?.orderId} (${res.result?.orderStatus})`);
      }
      onClose();
    } catch (e) {
      if (e instanceof ApiError) {
        const d = e.detail as { errorMessage?: string; hint?: string; detail?: string } | string;
        const msgText =
          typeof d === "string" ? d : d?.errorMessage || d?.detail || "Order failed";
        const low = msgText.toLowerCase();
        // Special-case the balance error (backend returns a plain string).
        if (low.includes("insufficient balance")) {
          push("error", msgText);
        } else if (low.includes("invalid ip")) {
          push(
            "error",
            "Invalid IP (DH-905): Dhan ne order ke liye aapka IP reject kiya. Settings ▸ Register my IP dabao. Agar phir bhi aaye to Dhan ke account me Static IP record kharab hai — Dhan web (My Profile ▸ Static IP) se IP DELETE karke dobara add karo, ya Dhan support se contact karo (getIP match dikhata hai par order engine reject karta hai — ye Dhan ka bug hai)."
          );
        } else {
          push("error", msgText);
        }
      } else {
        push("error", "Order failed");
      }
    } finally {
      setBusy(false);
    }
  };

  const isBuy = seed.transactionType === "BUY";
  const funds = preview?.funds;
  const insufficient = funds ? !funds.sufficient : false;

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4">
      <div className="max-h-[92vh] w-full max-w-lg overflow-auto rounded-xl border border-slate-700 bg-panel p-5 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold">
            {seed.indexName} · {seed.row.strike} {seed.optionType}
          </h2>
          <span
            className={
              "rounded px-2 py-0.5 text-xs font-bold " +
              (isBuy ? "bg-buy/20 text-green-400" : "bg-sell/20 text-red-400")
            }
          >
            {seed.transactionType}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-3 text-sm">
          <Field label="Ref LTP">
            <span className="font-mono">{fmt(refPrice)}</span>
          </Field>
          <Field label="Option Security ID">
            <span className="font-mono">{leg.securityId ?? "-"}</span>
          </Field>

          <Field label="Order Type">
            <select
              className="input"
              value={orderType}
              onChange={(e) => setOrderType(e.target.value as "LIMIT" | "MARKET")}
            >
              <option value="LIMIT">LIMIT (you set the price)</option>
              <option value="MARKET">MARKET (fills at best price)</option>
            </select>
          </Field>
          <Field label="Product">
            <select
              className="input"
              value={productType}
              onChange={(e) => setProductType(e.target.value as "INTRADAY" | "MARGIN")}
            >
              <option value="INTRADAY">INTRADAY</option>
              <option value="MARGIN">MARGIN (carry)</option>
            </select>
          </Field>

          <Field label="Lots (1 lot = fixed units)">
            <div className="flex items-center gap-1">
              <input
                className="input font-mono"
                type="number"
                min={1}
                value={quantity}
                onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
              />
              <button
                className="tab px-2 py-1 text-[10px]"
                onClick={() => setQuantity((q) => q + 1)}
                title="Add 1 lot"
              >
                +1
              </button>
            </div>
            <div className="mt-1 text-[11px] text-slate-400">
              {quantity} lot{quantity > 1 ? "s" : ""} × {lotSize} ={" "}
              <span className="font-mono font-semibold text-sky-300">{units} units</span>
            </div>
          </Field>
          <Field label="Limit Price — you decide">
            <div className="flex items-center gap-1">
              <input
                className="input font-mono"
                type="number"
                step="0.05"
                disabled={orderType === "MARKET"}
                value={price}
                onChange={(e) => setPrice(Number(e.target.value) || 0)}
              />
              <button
                className="tab px-2 py-1 text-[10px]"
                onClick={() => setPrice(round(refPrice))}
                title="Reset to live LTP"
              >
                LTP
              </button>
            </div>
          </Field>
        </div>
        <p className="mt-1 text-[11px] text-slate-500">
          LIMIT order: it fills only when the market hits your price. Type your own price,
          or tap LTP to use the live price.
        </p>

        <div className="mt-3">
          <div className="mb-1 text-xs uppercase tracking-wide text-slate-400">
            Profit Target
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {PRESETS.map((p) => (
              <button
                key={p}
                className={"tab " + (targetPct === p ? "tab-active" : "")}
                onClick={() => setTargetPct(p)}
              >
                {p}%
              </button>
            ))}
            <input
              className="input w-24 font-mono"
              type="number"
              step="0.5"
              value={targetPct}
              onChange={(e) => setTargetPct(Number(e.target.value) || 0)}
            />
            <span className="text-xs text-slate-400">% (manual)</span>
          </div>
        </div>

        {/* Stop-loss option */}
        <div className="mt-3 flex items-center justify-between rounded border border-slate-700 bg-slate-950/40 px-3 py-2">
          <div>
            <div className="text-xs font-semibold text-slate-300">Stop Loss</div>
            <div className="text-[11px] text-slate-500">
              {noStopLoss
                ? "OFF — manage your exit manually from Open Positions."
                : "ON — a safety stop is attached."}
            </div>
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={!noStopLoss}
              onChange={(e) => setNoStopLoss(!e.target.checked)}
            />
            Add stop loss
          </label>
        </div>

        {preview && (
          <div className="mt-4 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-sm">
            <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Order Preview
            </div>
            <Row label="Entry (LIMIT)" value={fmt(preview.preview.entryPrice)} />
            <Row label="Target (SELL)" value={fmt(preview.preview.targetPrice)} accent="green" />
            <Row
              label="Est. profit / unit"
              value={fmt(preview.preview.expectedProfitPerUnit)}
              accent="green"
            />
            <Row
              label={preview.preview.noStopLoss ? "Stop (off)" : "Stop loss"}
              value={
                preview.preview.noStopLoss
                  ? "— none (manual exit)"
                  : fmt(preview.preview.safetyStopPrice)
              }
            />
            <Row
              label="Est. total profit"
              value={fmt(preview.preview.expectedProfitPerUnit * (preview.preview.units ?? units))}
              accent="green"
            />
            <Row
              label="Lots × Units"
              value={`${quantity} × ${preview.preview.lotSize ?? lotSize} = ${preview.preview.units ?? units}`}
            />
            {funds && funds.availableBalance != null && (
              <>
                <Row
                  label="Order needs (cash)"
                  value={`₹${funds.requiredCash.toFixed(2)}`}
                />
                <Row
                  label="Available balance"
                  value={`₹${funds.availableBalance.toFixed(2)}`}
                  accent={funds.sufficient ? "green" : undefined}
                />
              </>
            )}
            <p className="mt-2 text-[11px] text-slate-500">{preview.preview.safetyStopNote}</p>
          </div>
        )}

        {insufficient && funds && (
          <div className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
            <div className="font-bold">⚠️ Insufficient balance</div>
            <div className="mt-0.5">
              This order needs <span className="font-mono">₹{funds.requiredCash.toFixed(2)}</span>{" "}
              but you only have <span className="font-mono">₹{(funds.availableBalance ?? 0).toFixed(2)}</span>{" "}
              (short by <span className="font-mono">₹{funds.shortfall.toFixed(2)}</span>). Reduce the lots.
            </div>
          </div>
        )}

        {fastOrders ? (
          <div className="mt-4 rounded border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-[11px] text-sky-300">
            ⚡ FAST ORDERS ON — PIN skip. 1 click pe order chala jayega.
          </div>
        ) : (
          <div className="mt-4">
            <label className="mb-1 block text-xs uppercase tracking-wide text-slate-400">
              Order Confirm PIN
            </label>
            <input
              className="input font-mono"
              type="password"
              inputMode="numeric"
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              placeholder="••••••"
              autoComplete="off"
            />
          </div>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className={isBuy ? "btn-buy" : "btn-sell"}
            onClick={place}
            disabled={busy || !preview || insufficient}
            title={insufficient ? "Insufficient balance — reduce lots" : undefined}
          >
            {busy ? "Placing…" : `Confirm ${seed.transactionType}`}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-xs uppercase tracking-wide text-slate-400">{label}</div>
      {children}
    </div>
  );
}

function Row({ label, value, accent }: { label: string; value: string; accent?: "green" }) {
  return (
    <div className="flex justify-between py-0.5">
      <span className="text-slate-400">{label}</span>
      <span className={"font-mono " + (accent === "green" ? "text-green-400" : "")}>{value}</span>
    </div>
  );
}

function fmt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return n.toFixed(2);
}

function round(n: number): number {
  return Math.round(n / 0.05) * 0.05;
}
