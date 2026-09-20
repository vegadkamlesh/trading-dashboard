import { useAuth } from "./hooks/useAuth";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";

export function App() {
  const { authenticated, ready } = useAuth();

  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center text-slate-400">Loading…</div>
    );
  }

  return authenticated ? <Dashboard /> : <Login />;
}
