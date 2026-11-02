import { useState } from "react";
import { api } from "../api/client";
import { useTradingAccounts } from "../hooks/useTradingAccounts";
import { useToast } from "../hooks/useToasts";

export function TradingAccountBar() {
  const { view, setView, data, error, refresh } = useTradingAccounts();
  const { push } = useToast();
  const [busy, setBusy] = useState(false);
  return <div className="border-b border-slate-800 bg-slate-900 px-4 py-3 text-xs">
    <div className="flex flex-wrap items-center gap-4">
      <strong>Data/feed: Master Dhan only</strong>
      {data?.accounts.map(account => <label key={account.id} className="flex items-center gap-2">
        <input type="checkbox" checked={account.enabled}
          disabled={busy || !!error || !account.configured}
          onChange={async e => {
            const accounts = e.target.checked
              ? [...data.selected, account.id] : data.selected.filter(id => id !== account.id);
            accounts.sort((a, b) => a === b ? 0 : a === "master" ? -1 : 1);
            setBusy(true);
            try { await api.post("/api/trading/selection", { accounts }); refresh(); }
            catch (ex) { push("error", ex instanceof Error ? ex.message : "Selection failed"); }
            finally { setBusy(false); }
          }} />
        {account.name} trading {account.configured ? "" : "(configure backend credentials)"}
        {account.algoEntryEnabled && <span className="text-emerald-300">algo ready</span>}
        {account.positionState && <span className="text-amber-300">{account.positionState}</span>}
      </label>)}
      <label>Viewing: <select className="input ml-2" value={view}
        onChange={e => setView(e.target.value === "child" ? "child" : "master")}>
        <option value="master">Master Dhan</option>
        <option value="child">Child Dhan</option>
      </select></label>
    </div>
    <p className="mt-1 text-slate-400">
      Checkboxes select NEW entries, not the viewed account. Both ON sends one request per account.
      Disarm and finish tracked positions before changing recipients. Existing exits remain active.
    </p>
    {error && <p role="alert" className="text-red-300">Account sync failed: {error}. Do not submit until refreshed.</p>}
    {data?.accounts.filter(a => a.error).map(a =>
      <p key={a.id} role="alert" className="text-amber-300">{a.name}: {a.error}</p>)}
  </div>;
}
