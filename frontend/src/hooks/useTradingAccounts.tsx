import { createContext, useContext, useState, type ReactNode } from "react";
import { api, getTradingAccount, setTradingAccount } from "../api/client";
import { usePolling } from "./usePolling";

export type AccountId = "master" | "child";
export interface TradingAccounts {
  selected: AccountId[];
  dataSource: "master";
  dataSourceMock: boolean;
  accounts: Array<{
    id: AccountId; name: string; configured: boolean; mock: boolean; enabled: boolean;
    algoEnabled: boolean; algoEntryEnabled: boolean; positionState: string | null; error: string | null;
  }>;
}
const Context = createContext<{
  view: AccountId; setView: (id: AccountId) => void;
  data: TradingAccounts | null; error: string | null; refresh: () => void;
}>({ view: "master", setView: () => {}, data: null, error: null, refresh: () => {} });

export function TradingAccountsProvider({ children }: { children: ReactNode }) {
  const [view, updateView] = useState<AccountId>(getTradingAccount);
  const { data, error, refresh } = usePolling<TradingAccounts>(
    () => api.get("/api/trading/accounts"), 3000,
  );
  const setView = (id: AccountId) => {
    setTradingAccount(id);
    updateView(id);
  };
  return <Context.Provider value={{ view, setView, data, error, refresh }}>{children}</Context.Provider>;
}

export const useTradingAccounts = () => useContext(Context);

export function accountResultText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    if ("errorMessage" in value) return String(value.errorMessage);
    if ("detail" in value) return String(value.detail);
  }
  return JSON.stringify(value) ?? "Unknown result";
}
