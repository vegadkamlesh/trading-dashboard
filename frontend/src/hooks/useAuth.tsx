import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, setCsrfToken } from "../api/client";

interface Profile {
  clientId?: string;
  name?: string;
  activeSegment?: string;
  ddpi?: string;
  dataPlan?: string;
  tokenValidity?: string;
}

interface AuthCtx {
  authenticated: boolean;
  ready: boolean;
  dryRun: boolean;
  fastOrders: boolean;
  profile: Profile | null;
  login: () => Promise<{ ok: boolean; error?: unknown }>;
  logout: () => Promise<void>;
  setDryRun: (v: boolean) => Promise<void>;
  setFastOrders: (v: boolean) => Promise<void>;
  refreshFlags: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>({
  authenticated: false,
  ready: false,
  dryRun: true,
  fastOrders: false,
  profile: null,
  login: async () => ({ ok: false }),
  logout: async () => {},
  setDryRun: async () => {},
  setFastOrders: async () => {},
  refreshFlags: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [ready, setReady] = useState(false);
  const [dryRun, setDryRunState] = useState(true);
  const [fastOrders, setFastOrdersState] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);

  const refreshFlags = useCallback(async () => {
    try {
      const me = await api.get<{
        ok: boolean;
        csrfToken?: string;
        dryRun: boolean;
        fastOrders: boolean;
      }>("/api/auth/me");
      setAuthenticated(!!me.ok);
      setDryRunState(!!me.dryRun);
      setFastOrdersState(!!me.fastOrders);
      // Re-arm the CSRF token: after a reload / new tab the in-memory
      // sessionStorage copy may be gone, which made every POST fail with 403.
      if (me.ok && me.csrfToken) setCsrfToken(me.csrfToken);
    } catch {
      setAuthenticated(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      await refreshFlags();
      setReady(true);
    })();
  }, [refreshFlags]);

  const login = useCallback(async () => {
    const res = await api.post<{
      ok: boolean;
      csrfToken?: string;
      profile?: Profile;
      dryRun?: boolean;
      fastOrders?: boolean;
      error?: unknown;
    }>("/api/auth/login");
    if (res.ok && res.csrfToken) {
      setCsrfToken(res.csrfToken);
      setAuthenticated(true);
      setProfile(res.profile ?? null);
      setDryRunState(!!res.dryRun);
      setFastOrdersState(!!res.fastOrders);
    }
    return { ok: res.ok, error: res.error };
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      setCsrfToken(null);
      setAuthenticated(false);
      setProfile(null);
    }
  }, []);

  const setDryRun = useCallback(async (v: boolean) => {
    const r = await api.post<{ ok: boolean; dryRun: boolean }>("/api/settings/dry-run", {
      value: v,
    });
    setDryRunState(r.dryRun);
  }, []);

  const setFastOrders = useCallback(async (v: boolean) => {
    const r = await api.post<{ ok: boolean; fastOrders: boolean }>(
      "/api/settings/fast-orders",
      { value: v }
    );
    setFastOrdersState(r.fastOrders);
  }, []);

  return (
    <Ctx.Provider
      value={{
        authenticated,
        ready,
        dryRun,
        fastOrders,
        profile,
        login,
        logout,
        setDryRun,
        setFastOrders,
        refreshFlags,
      }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useAuth() {
  return useContext(Ctx);
}
