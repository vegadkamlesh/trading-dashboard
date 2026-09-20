import { useState } from "react";
import { useAuth } from "../hooks/useAuth";

export function Login() {
  const { login } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const doLogin = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await login();
      if (!res.ok) {
        const err = res.error as { errorMessage?: string; detail?: string } | undefined;
        setError(err?.errorMessage || err?.detail || "Login failed");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center">
      <div className="card w-full max-w-sm text-center">
        <h1 className="mb-1 text-xl font-bold">Dhan Options Dashboard</h1>
        <p className="mb-6 text-sm text-slate-400">
          NIFTY &amp; SENSEX option chain · one-click Super Orders
        </p>
        {error && (
          <div className="mb-4 rounded-lg border border-red-700 bg-red-950 px-3 py-2 text-sm text-red-200">
            {error}
          </div>
        )}
        <button className="btn-buy w-full" onClick={doLogin} disabled={busy}>
          {busy ? "Connecting…" : "Connect to Dhan"}
        </button>
        <p className="mt-4 text-xs text-slate-500">
          Your Dhan token stays on the server. This only opens a local session.
        </p>
      </div>
    </div>
  );
}
