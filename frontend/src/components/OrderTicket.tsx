import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { OrderPreviewResponse, OptionRow } from "../api/types";
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

  const body = useMemo(
    () => ({
      indexKey: seed.indexKey,
      securityId: String(leg.securityId ?? ""),
      optionType: seed.optionType,
      transactionType: seed.transactionType,
      side: `${seed.indexName} ${seed.row.strike} ${seed.optionType}`,
      quantity,
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
    if (!pin) {
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
        const d = e.detail as { errorMessage?: string; hint?: string; detail?: string };
        // Special-case the IP error so the user knows exactly what to do.
        if ((d?.errorMessage || "").toLowerCase().includes("invalid ip")) {
          push("error", "Invalid IP: your IP isn't whitelisted in Dhan. Go to Connection ▸ Register IP, then retry.");
        } else {
          push("error", d?.errorMessage || d?.detail || "Order failed");
        }
      } else {
        push("error", "Order failed");
      }
    } finally {
      setBusy(false);
    }
  };

  const isBuy = seed.transactionType === "BUY";

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

          <Field label="Quantity (lots × units)">
            <input
              className="input font-mono"
              type="number"
              min={1}
              value={quantity}
              onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
            />
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
              value={fmt(preview.preview.expectedProfitPerUnit * quantity)}
              accent="green"
            />
            <p className="mt-2 text-[11px] text-slate-500">{preview.preview.safetyStopNote}</p>
          </div>
        )}

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

        <div className="mt-5 flex justify-end gap-2">
          <button className="btn-ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className={isBuy ? "btn-buy" : "btn-sell"}
            onClick={place}
            disabled={busy || !preview}
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
