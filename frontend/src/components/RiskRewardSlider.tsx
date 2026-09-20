import { useMemo } from "react";

// Visual risk/reward projector for a single option position.
//
// Sliders:
//   * Profit target % (where you exit green)
//   * Max loss % (where you cut the trade)
// The bars show ₹ P/L at the target, at break-even, and at the max-loss cap for
// the chosen quantity, so you always see "how much can I make / lose".
export function RiskRewardSlider({
  entryPrice,
  quantity,
  targetPct,
  maxLossPct,
  onTargetPct,
  onMaxLossPct,
}: {
  entryPrice: number;
  quantity: number;
  targetPct: number;
  maxLossPct: number;
  onTargetPct: (v: number) => void;
  onMaxLossPct: (v: number) => void;
}) {
  const { profit, loss, rr, targetPrice, lossPrice } = useMemo(() => {
    const tp = entryPrice * (1 + targetPct / 100);
    const lp = entryPrice * (1 - maxLossPct / 100);
    const p = (tp - entryPrice) * quantity;
    const l = (entryPrice - lp) * quantity;
    return {
      profit: p,
      loss: l,
      rr: l > 0 ? p / l : 0,
      targetPrice: tp,
      lossPrice: lp,
    };
  }, [entryPrice, quantity, targetPct, maxLossPct]);

  const maxBar = Math.max(profit, loss, 1);

  return (
    <div className="mt-3 rounded-lg border border-slate-700 bg-slate-900/60 p-3 text-sm">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
        Risk / Reward
      </div>

      <Slider
        label="Profit target"
        value={targetPct}
        min={1}
        max={50}
        step={1}
        onChange={onTargetPct}
        accent="green"
      />
      <Slider
        label="Max loss (your exit)"
        value={maxLossPct}
        min={1}
        max={50}
        step={1}
        onChange={onMaxLossPct}
        accent="red"
      />

      <div className="mt-3 space-y-2">
        <Bar
          label={`Target +${targetPct}% @ ${targetPrice.toFixed(2)}`}
          amount={profit}
          widthPct={(profit / maxBar) * 100}
          color="bg-green-500"
        />
        <Bar
          label={`Max loss -${maxLossPct}% @ ${lossPrice.toFixed(2)}`}
          amount={-loss}
          widthPct={(loss / maxBar) * 100}
          color="bg-red-500"
        />
      </div>

      <div className="mt-3 flex justify-between text-xs">
        <span className="text-slate-400">
          Risk : Reward{" "}
          <span className="font-mono text-slate-200">1 : {rr.toFixed(2)}</span>
        </span>
        <span className={rr >= 2 ? "text-green-400" : rr >= 1 ? "text-amber-400" : "text-red-400"}>
          {rr >= 2 ? "Favourable" : rr >= 1 ? "Balanced" : "Risky"}
        </span>
      </div>

      <p className="mt-2 text-[10px] leading-tight text-slate-500">
        Note: Dhan Super Orders auto-attach a far safety stop-loss. "Max loss" here is
        your <em>intended</em> exit level — place a matching GTT/alert or watch price to enforce it.
      </p>
    </div>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  accent,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  accent: "green" | "red";
}) {
  return (
    <div className="mt-2">
      <div className="flex justify-between text-xs">
        <span className="text-slate-400">{label}</span>
        <span className={"font-mono " + (accent === "green" ? "text-green-400" : "text-red-400")}>
          {value}%
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className={
          "mt-1 w-full cursor-pointer " + (accent === "green" ? "accent-green-500" : "accent-red-500")
        }
      />
    </div>
  );
}

function Bar({
  label,
  amount,
  widthPct,
  color,
}: {
  label: string;
  amount: number;
  widthPct: number;
  color: string;
}) {
  return (
    <div>
      <div className="flex justify-between text-[11px]">
        <span className="text-slate-400">{label}</span>
        <span className={"font-mono " + (amount >= 0 ? "text-green-400" : "text-red-400")}>
          {amount >= 0 ? "+" : "-"}₹{Math.abs(amount).toFixed(0)}
        </span>
      </div>
      <div className="mt-0.5 h-3 w-full overflow-hidden rounded bg-slate-800">
        <div className={"h-full " + color} style={{ width: `${Math.max(2, widthPct)}%` }} />
      </div>
    </div>
  );
}
