import { useState } from "react";
import { useAuth } from "../hooks/useAuth";
import { usePolling } from "../hooks/usePolling";
import { api } from "../api/client";
import type { IndexInfo } from "../api/types";
import { ConnectionBar } from "../components/ConnectionBar";
import { OptionChainPage } from "./OptionChainPage";
import { Positions } from "./Positions";
import { SuperOrders } from "./SuperOrders";
import { Funds } from "./Funds";
import { EodSummary } from "./EodSummary";
import { SettingsPanel } from "../components/SettingsPanel";
import { Readme } from "./Readme";

type Tab = "chain" | "positions" | "orders" | "funds" | "eod" | "settings" | "readme";

const TABS: Array<[Tab, string]> = [
  ["chain", "Option Chain"],
  ["positions", "Positions"],
  ["orders", "Super Orders"],
  ["funds", "Funds"],
  ["eod", "EOD Summary"],
  ["settings", "Settings"],
  ["readme", "Readme"],
];

export function Dashboard() {
  const { logout } = useAuth();
  const [tab, setTab] = useState<Tab>("chain");
  const { data: indices } = usePolling<IndexInfo[]>(() => api.get("/api/market/indices"), 60000);

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
        <ConnectionBar onLogout={logout} />
      </header>

      <main className="flex-1 p-4">
        {tab === "chain" && <OptionChainPage indices={indices ?? []} />}
        {tab === "positions" && <Positions />}
        {tab === "orders" && <SuperOrders />}
        {tab === "funds" && <Funds />}
        {tab === "eod" && <EodSummary />}
        {tab === "settings" && <SettingsPanel />}
        {tab === "readme" && <Readme />}
      </main>
    </div>
  );
}


