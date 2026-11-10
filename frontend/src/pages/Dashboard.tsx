import { useState } from "react";
import { useAuth } from "../hooks/useAuth";
import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type { IndexInfo } from "../api/types";
import { ConnectionBar } from "../components/ConnectionBar";
import { OptionChainPage } from "./OptionChainPage";
import { AlgoPage } from "./AlgoPage";
import { Algo2Page } from "./Algo2Page";
import { Positions } from "./Positions";
import { Orders } from "./SuperOrders";
import { Funds } from "./Funds";
import { EodSummary } from "./EodSummary";
import { SettingsPanel } from "../components/SettingsPanel";
import { Readme } from "./Readme";
import { TradingAccountsProvider, useTradingAccounts } from "../hooks/useTradingAccounts";
import { TradingAccountBar } from "../components/TradingAccountBar";

type Tab = "algo" | "algo2" | "chain" | "positions" | "orders" | "funds" | "eod" | "settings" | "readme";

const TABS: Array<[Tab, string]> = [
  ["algo", "🤖 Algo 1"],
  ["algo2", "🤖 Algo 2"],
  ["chain", "Option Chain"],
  ["positions", "Positions"],
  ["orders", "Orders"],
  ["funds", "Funds"],
  ["eod", "EOD Summary"],
  ["settings", "Settings"],
  ["readme", "Readme"],
];

export function Dashboard() {
  return <TradingAccountsProvider><TradingDashboard /></TradingAccountsProvider>;
}

function TradingDashboard() {
  const { view, data: accounts } = useTradingAccounts();
  const { logout } = useAuth();
  const [tab, setTab] = useState<Tab>("algo");
  const { data: indices } = usePolling<IndexInfo[]>(() => api.get("/api/market/indices"), 60000);
  const mockAccounts = accounts?.accounts.filter((account) => account.configured && account.mock) ?? [];
  const realAccounts = accounts?.accounts.some((account) => account.configured && !account.mock) ?? false;

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 flex items-center justify-between border-b border-slate-800 bg-slate-950/95 px-4 py-2 backdrop-blur">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-bold tracking-wide">DHAN OPTIONS</h1>
          <nav className="flex gap-1">
            {TABS.map(([key, label]) => (
              <button
                key={key}
                className={"tab " + (tab === key ? "tab-active" : "")}
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            ))}
          </nav>
        </div>
        <ConnectionBar key={view} onLogout={logout} />
      </header>

      <TradingAccountBar />
      <main key={view} className="flex-1 p-4">
        {(accounts?.dataSourceMock || mockAccounts.length > 0) && (
          <div role="alert" className="mb-3 rounded border border-amber-500 bg-amber-950/60 px-3 py-2 text-sm text-amber-100">
            {accounts?.dataSourceMock && (
              <p><strong>MOCK MARKET DATA:</strong> Prices and history are synthetic, not live Dhan quotes. Do not trade based on them.</p>
            )}
            {mockAccounts.length > 0 && (
              <p><strong>MOCK BROKER:</strong> {mockAccounts.map((account) => account.name).join(", ")} uses simulated broker responses.</p>
            )}
            {realAccounts && (
              <p>Accounts with real broker credentials can place real orders when LIVE mode is enabled.</p>
            )}
          </div>
        )}
        <p className="mb-3 text-xs text-sky-300">Viewing {view === "master" ? "Master Dhan" : "Child Dhan"} — account-specific holdings, orders, funds and EXIT</p>
        <BrokerSyncBar />
        {tab === "algo" && <AlgoPage />}
        {tab === "algo2" && <Algo2Page />}
        {tab === "chain" && <OptionChainPage indices={indices ?? []} />}
        {tab === "positions" && <Positions />}
        {tab === "orders" && <Orders />}
        {tab === "funds" && <Funds />}
        {tab === "eod" && <EodSummary />}
        {tab === "settings" && <SettingsPanel />}
        {tab === "readme" && <Readme />}
      </main>
    </div>
  );
}

function BrokerSyncBar() {
  const { data, error } = usePolling<{
    observedAt: Record<string, number>; errors: Record<string, string>;
    orderStreamConnected: boolean; orderStreamError: string | null;
  }>(() => api.get("/api/account/sync"), 1000);
  const observed = data?.observedAt["/positions"];
  const age = observed ? Math.max(0, Date.now() / 1000 - observed) : null;
  const stale = !!error || age === null || age > 3 || !!data?.errors["/positions"];
  return <div role={stale ? "alert" : undefined}
    className={`mb-3 rounded border px-3 py-2 text-xs ${stale
      ? "border-amber-700 text-amber-200" : "border-slate-800 text-slate-400"}`}>
    Broker holdings: {stale ? "UNVERIFIED / STALE" : "recent Dhan snapshot"}
    {age !== null && ` · ${age.toFixed(1)}s since response`}
    {" · "}Order notifications: {data?.orderStreamConnected ? "connected" : "REST fallback"}
    {" · "}Both accounts refresh in backend, even when another view is selected.
    {error && <p>{error}</p>}
    {Object.entries(data?.errors || {}).map(([path, message]) => <p key={path}>{path}: {message}</p>)}
    {data?.orderStreamError && <p>{data.orderStreamError}</p>}
  </div>;
}
