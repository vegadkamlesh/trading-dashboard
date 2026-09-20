import type { RecommendationResponse, StrikeRecommendation } from "../api/types";

// Explains exactly WHY a strike got its signal: every factor, every reason,
// the market context, and the math — so the trader learns and trusts it.
export function SignalDetailModal({
  rec,
  ctx,
  onClose,
}: {
  rec: StrikeRecommendation;
  ctx?: RecommendationResponse | null;
  onClose: () => void;
}) {
  const actionCls =
    rec.action === "BUY"
      ? "bg-green-500/15 text-green-300 border-green-600/40"
      : rec.action === "WAIT"
      ? "bg-amber-500/15 text-amber-300 border-amber-600/40"
      : "bg-slate-700/30 text-slate-300 border-slate-600/40";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-lg overflow-auto rounded-xl border border-slate-700 bg-panel p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold">
            {rec.strike} {rec.side} · <span className="text-slate-400">{ctx?.index}</span>
          </h2>
          <button className="text-slate-400 hover:text-slate-200" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="mb-4 flex items-center gap-3">
          <span className={"rounded-md border px-3 py-1 text-base font-bold " + actionCls}>
            {rec.action} {rec.side}
          </span>
          <div className="flex-1">
            <div className="flex justify-between text-xs text-slate-400">
              <span>Profit probability</span>
              <span className="font-mono">{rec.profitProbability}%</span>
            </div>
            <div className="mt-1 h-2 w-full overflow-hidden rounded bg-slate-800">
              <div
                className={
                  "h-full " +
                  (rec.profitProbability >= 65
                    ? "bg-green-500"
                    : rec.profitProbability >= 50
                    ? "bg-amber-500"
                    : "bg-slate-600")
                }
                style={{ width: `${rec.profitProbability}%` }}
              />
            </div>
          </div>
        </div>

        {/* Key numbers */}
        <div className="grid grid-cols-3 gap-2 text-xs">
          <Stat label="Confidence" value={`${rec.confidence}%`} />
          <Stat label="LTP" value={rec.ltp != null ? rec.ltp.toFixed(2) : "-"} />
          <Stat label="IV" value={rec.iv != null ? rec.iv.toFixed(1) : "-"} />
          <Stat label="Delta" value={rec.delta != null ? rec.delta.toFixed(3) : "-"} />
          <Stat label="Theta" value={rec.theta != null ? rec.theta.toFixed(2) : "-"} />
          <Stat
            label="Theta risk"
            value={rec.thetaRisk ?? "-"}
            color={rec.thetaRisk === "high" ? "text-red-400" : undefined}
          />
        </div>

        {/* Suggested plan */}
        <div className="mt-3 flex items-center justify-between rounded border border-slate-700 bg-slate-950/40 px-3 py-2 text-xs">
          <span className="text-slate-400">
            Suggested target{" "}
            <span className="font-mono text-green-400">+{rec.suggestedTargetPct}%</span> · stop{" "}
            <span className="font-mono text-red-400">-{rec.suggestedStopPct}%</span>
          </span>
        </div>

        {/* WHY — reasons */}
        <div className="mt-4">
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Why this signal
          </div>
          <ul className="space-y-1 text-sm text-slate-300">
            {rec.reasons.map((r, i) => (
              <li key={i} className="flex gap-1.5">
                <span className="text-slate-500">•</span>
                <span>{r}</span>
              </li>
            ))}
          </ul>
        </div>

        {/* Market context */}
        {ctx && (
          <div className="mt-4 rounded border border-slate-800 bg-slate-950/40 p-3">
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Market context
            </div>
            <div className="mb-1 flex flex-wrap gap-3 text-xs text-slate-400">
              <span>Stance <b className="text-slate-200">{ctx.stance}</b></span>
              <span>RSI <b className="text-slate-200">{ctx.rsi?.toFixed(0) ?? "-"}</b></span>
              <span>Trend <b className="text-slate-200">{ctx.trend ?? "-"}</b></span>
              <span>DTE <b className="text-slate-200">{ctx.daysToExpiry ?? "-"}</b></span>
            </div>
            <ul className="space-y-1 text-xs text-slate-400">
              {ctx.contextReasons?.map((r, i) => (
                <li key={i}>• {r}</li>
              ))}
            </ul>
          </div>
        )}

        <p className="mt-4 text-[10px] leading-tight text-slate-500">
          Probability-based and explainable — NOT advice or a guarantee. Theta and gaps can
          wipe out premium. Always manage risk.
        </p>
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="rounded bg-slate-950/40 px-2 py-1">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={"font-mono " + (color ?? "text-slate-200")}>{value}</div>
    </div>
  );
}
