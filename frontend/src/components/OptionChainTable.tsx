import { useEffect, useMemo, useRef } from "react";
import type { ChainSnapshot, OptionLeg, OptionRow, StrikeRecommendation } from "../api/types";
import type { TicketSeed } from "./OrderTicket";

type PickHandler = (
  seed: Omit<TicketSeed, "row" | "indexKey" | "indexName"> & { row: OptionRow }
) => void;

// CE columns | STRIKE | PE columns. One-click B (BUY) / S (SELL) per leg,
// plus a compact signal badge (green/red) with the profit % and an ℹ button.
export function OptionChainTable({
  snapshot,
  onPick,
  recs,
  onInfo,
}: {
  snapshot: ChainSnapshot;
  onPick: PickHandler;
  recs?: Map<string, StrikeRecommendation>;
  onInfo?: (rec: StrikeRecommendation) => void;
}) {
  const atm = useMemo(
    () => nearestStrike(snapshot.rows, snapshot.underlyingLtp),
    [snapshot.rows, snapshot.underlyingLtp]
  );

  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const idx = snapshot.rows.findIndex((r) => r.strike === atm);
    if (idx >= 0) {
      const rowH = 34;
      el.scrollTop = Math.max(0, idx * rowH - el.clientHeight / 2);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot.index, snapshot.expiry]);

  return (
    <div
      ref={scroller}
      className="max-h-[calc(100vh-280px)] overflow-auto rounded-lg border border-slate-800"
    >
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 z-10 bg-slate-900 text-slate-400">
          <tr>
            <Th colSpan={7} className="text-center text-green-400">
              CALL (CE)
            </Th>
            <Th className="bg-slate-800 text-center">STRIKE</Th>
            <Th colSpan={7} className="text-center text-red-400">
              PUT (PE)
            </Th>
          </tr>
          <tr className="border-b border-slate-800">
            <Th>OI</Th>
            <Th>IV</Th>
            <Th>Bid</Th>
            <Th>LTP</Th>
            <Th>Ask</Th>
            <Th className="text-center">Signal</Th>
            <Th className="text-center">Trade</Th>
            <Th className="bg-slate-800">—</Th>
            <Th className="text-center">Trade</Th>
            <Th className="text-center">Signal</Th>
            <Th>Bid</Th>
            <Th>LTP</Th>
            <Th>Ask</Th>
            <Th>IV</Th>
            <Th>OI</Th>
          </tr>
        </thead>
        <tbody>
          {snapshot.rows.map((r) => {
            const isAtm = r.strike === atm;
            return (
              <tr
                key={r.strike}
                className={
                  "border-b border-slate-900 " +
                  (isAtm ? "bg-sky-950/40" : "hover:bg-slate-900/60")
                }
              >
                <Td>{abbr(r.ce.oi)}</Td>
                <Td>{iv(r.ce.iv)}</Td>
                <Td mono>{fmt(r.ce.bid)}</Td>
                <Td mono accent="green">
                  {fmt(r.ce.ltp)}
                </Td>
                <Td mono>{fmt(r.ce.ask)}</Td>
                <SignalCell
                  rec={recs?.get(`${r.strike}-CE`)}
                  onInfo={onInfo}
                />
                <TradeCell
                  leg={r.ce}
                  onBuy={() => onPick({ row: r, optionType: "CE", transactionType: "BUY" })}
                  onSell={() => onPick({ row: r, optionType: "CE", transactionType: "SELL" })}
                />

                <Td
                  mono
                  className={
                    "bg-slate-800/70 text-center font-bold " + (isAtm ? "text-sky-300" : "")
                  }
                >
                  {r.strike}
                </Td>

                <TradeCell
                  leg={r.pe}
                  onBuy={() => onPick({ row: r, optionType: "PE", transactionType: "BUY" })}
                  onSell={() => onPick({ row: r, optionType: "PE", transactionType: "SELL" })}
                />
                <SignalCell
                  rec={recs?.get(`${r.strike}-PE`)}
                  onInfo={onInfo}
                />
                <Td mono>{fmt(r.pe.bid)}</Td>
                <Td mono accent="red">
                  {fmt(r.pe.ltp)}
                </Td>
                <Td mono>{fmt(r.pe.ask)}</Td>
                <Td>{iv(r.pe.iv)}</Td>
                <Td>{abbr(r.pe.oi)}</Td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {snapshot.rows.length === 0 && (
        <div className="p-8 text-center text-sm text-slate-500">
          {snapshot.error ? `Error: ${snapshot.error}` : "No strikes loaded yet…"}
        </div>
      )}
    </div>
  );
}

// Compact signal: ▲/▼/▬ + profit %, colour-coded, with an ℹ button that opens
// the full "why" breakdown. Keeps the chain readable without extra windows.
function SignalCell({
  rec,
  onInfo,
}: {
  rec?: StrikeRecommendation;
  onInfo?: (rec: StrikeRecommendation) => void;
}) {
  if (!rec) {
    return (
      <Td className="text-center">
        <span className="text-[10px] text-slate-600">·</span>
      </Td>
    );
  }
  const isBuy = rec.action === "BUY";
  const arrow = rec.side === "CE" ? "▲" : "▼";
  const cls =
    rec.action === "BUY"
      ? "bg-green-500/15 text-green-400 border-green-600/40"
      : rec.action === "WAIT"
      ? "bg-amber-500/15 text-amber-400 border-amber-600/40"
      : "bg-slate-700/20 text-slate-500 border-slate-600/30";

  return (
    <Td className="text-center">
      <div className="flex items-center justify-center gap-1">
        <span
          className={"inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5 text-[10px] font-bold " + cls}
          title={`${rec.action} ${rec.side} · ${rec.profitProbability}% profit probability`}
        >
          {isBuy ? arrow : "▬"}
          {rec.profitProbability}%
        </span>
        {onInfo && (
          <button
            className="text-[10px] text-sky-400 hover:text-sky-300"
            onClick={() => onInfo(rec)}
            title="Why this signal? Show details"
          >
            ⓘ
          </button>
        )}
      </div>
    </Td>
  );
}

function TradeCell({ leg, onBuy, onSell }: { leg: OptionLeg; onBuy: () => void; onSell: () => void }) {
  return (
    <Td>
      <div className="flex justify-center gap-1">
        <button
          className="btn bg-buy px-2 py-0.5 text-[10px] hover:bg-green-700"
          disabled={!leg.securityId}
          onClick={onBuy}
        >
          B
        </button>
        <button
          className="btn bg-sell px-2 py-0.5 text-[10px] hover:bg-red-700"
          disabled={!leg.securityId}
          onClick={onSell}
        >
          S
        </button>
      </div>
    </Td>
  );
}

function Th({
  children,
  colSpan,
  className,
}: {
  children?: React.ReactNode;
  colSpan?: number;
  className?: string;
}) {
  return (
    <th colSpan={colSpan} className={"px-2 py-1.5 text-left font-semibold " + (className ?? "")}>
      {children}
    </th>
  );
}

function Td({
  children,
  className,
  mono,
  accent,
}: {
  children?: React.ReactNode;
  className?: string;
  mono?: boolean;
  accent?: "green" | "red";
}) {
  return (
    <td
      className={
        "px-2 py-1 " +
        (mono ? "font-mono " : "") +
        (accent === "green" ? "text-green-400 " : accent === "red" ? "text-red-400 " : "") +
        (className ?? "")
      }
    >
      {children}
    </td>
  );
}

function fmt(n: number | null | undefined) {
  return n === null || n === undefined ? "-" : n.toFixed(2);
}
function iv(n: number | null | undefined) {
  return n === null || n === undefined ? "-" : n.toFixed(1);
}
function abbr(n: number | null | undefined) {
  if (n === null || n === undefined) return "-";
  if (Math.abs(n) >= 1e7) return (n / 1e7).toFixed(2) + "Cr";
  if (Math.abs(n) >= 1e5) return (n / 1e5).toFixed(2) + "L";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function nearestStrike(rows: OptionRow[], ltp: number): number {
  if (!rows.length) return 0;
  let best = rows[0].strike;
  let bestD = Math.abs(best - ltp);
  for (const r of rows) {
    const d = Math.abs(r.strike - ltp);
    if (d < bestD) {
      bestD = d;
      best = r.strike;
    }
  }
  return best;
}
