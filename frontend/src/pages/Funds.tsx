import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type { Funds as FundsType } from "../api/types";

export function Funds() {
  const { data, error } = usePolling<FundsType>(() => api.get("/api/account/funds"), 10000);

  if (error) return <Empty msg={`Error: ${error}`} />;
  if (!data) return <Empty msg="Loading funds…" />;

  const items: Array<[string, number | undefined]> = [
    ["Available Balance", data.availabelBalance],
    ["Start of Day Limit", data.sodLimit],
    ["Collateral", data.collateralAmount],
    ["Utilized Today", data.utilizedAmount],
    ["Withdrawable", data.withdrawableBalance],
  ];

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {items.map(([label, value]) => (
        <div key={label} className="card">
          <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
          <div className="mt-2 font-mono text-2xl font-bold">
            {typeof value === "number" ? `₹ ${value.toLocaleString("en-IN", { maximumFractionDigits: 2 })}` : "-"}
          </div>
        </div>
      ))}
    </div>
  );
}

function Empty({ msg }: { msg: string }) {
  return <div className="p-8 text-center text-sm text-slate-500">{msg}</div>;
}
