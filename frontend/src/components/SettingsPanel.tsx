import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AppSettings, CacheState, IpStatus } from "../api/types";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToasts";

// The two master switches + how-to info (Hinglish). Lets you flip safety modes
// live from the browser — no .env editing, no restart.
export function SettingsPanel() {
  const { dryRun, fastOrders, setDryRun, setFastOrders } = useAuth();
  const { push } = useToast();
  const [info, setInfo] = useState<AppSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [ip, setIp] = useState<IpStatus | null>(null);
  const [ipBusy, setIpBusy] = useState(false);
  const [cache, setCache] = useState<CacheState | null>(null);
  const [warmBusy, setWarmBusy] = useState(false);

  useEffect(() => {
    api.get<AppSettings>("/api/settings").then(setInfo).catch(() => {});
    api.get<IpStatus>("/api/account/ip").then(setIp).catch(() => {});
    api.get<CacheState>("/api/account/cache-state").then(setCache).catch(() => {});
  }, []);

  const rewarm = async () => {
    setWarmBusy(true);
    try {
      await api.post("/api/account/warm-cache", {});
      push("info", "History cache refresh start ho gaya (background me).");
      setTimeout(() => {
        api.get<CacheState>("/api/account/cache-state").then(setCache).catch(() => {});
      }, 4000);
    } catch {
      push("error", "Warm-up failed");
    } finally {
      setWarmBusy(false);
    }
  };

  const registerIp = async () => {
    setIpBusy(true);
    try {
      const r = await api.post<{ ip: string; flag: string }>("/api/account/ip/register", {
        flag: "PRIMARY",
      });
      push("success", `IP registered : ${r.ip} (${r.flag}). Ab order try karo.`);
      api.get<IpStatus>("/api/account/ip").then(setIp).catch(() => {});
    } catch (e) {
      const d = e instanceof ApiError ? (e.detail as { errorMessage?: string; detail?: string }) : null;
      push("error", d?.errorMessage || d?.detail || "IP register failed");
    } finally {
      setIpBusy(false);
    }
  };

  const toggleDry = async () => {
    setBusy(true);
    try {
      await setDryRun(!dryRun);
      push("info", !dryRun ? "LIVE mode: orders ab Dhan par jayenge!" : "DRY-RUN: orders simulate honge");
    } catch (e) {
      push("error", e instanceof ApiError ? "Toggle failed" : "Toggle failed");
    } finally {
      setBusy(false);
    }
  };

  const toggleFast = async () => {
    setBusy(true);
    try {
      await setFastOrders(!fastOrders);
      push("info", !fastOrders ? "FAST ORDERS ON: 1 click = order (PIN skip)" : "FAST OFF: PIN wapas lagega");
    } catch {
      push("error", "Toggle failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <h2 className="text-base font-bold">Settings — True / False Switches</h2>

      <SwitchCard
        title="DRY-RUN  (app_dry_run)"
        value={dryRun}
        onToggle={toggleDry}
        busy={busy}
        onText="DRY-RUN = true"
        offText="LIVE = false"
        dangerWhenOff
        desc={
          dryRun
            ? "TRUE: Orders sirf validate aur log honge — Dhan par NAHI jayenge. Koi paisa nahi lagega. Safe."
            : "FALSE: Orders asli Dhan par jayenge — REAL trading, asli paisa. Dhyan se!"
        }
      />

      <SwitchCard
        title="FAST ORDERS  (app_fast_orders)"
        value={fastOrders}
        onToggle={toggleFast}
        busy={busy}
        onText="FAST = true (no PIN)"
        offText="SAFE = false (PIN)"
        desc={
          fastOrders
            ? "TRUE: Order PIN nahi poochega — Buy/Sell pe 1 click = turant order. Scalping ke liye. Galti se click ka dhyan rakho!"
            : "FALSE: Har order se pehle PIN maangega. Safe, par slow. Scalping ke time true kar do."
        }
      />

      {/* IP registration — fix for 'Invalid IP' on live orders */}
      <div
        className={
          "rounded-lg border p-4 " +
          (ip?.match === false ? "border-amber-700/60 bg-amber-950/20" : "border-slate-800 bg-slate-900/40")
        }
      >
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-semibold">🌐 Static IP (order ke liye zaroori)</div>
            <div className="mt-1 text-xs text-slate-400">
              Aapka public IP:{" "}
              <span className="font-mono text-sky-300">{ip?.detectedIp ?? "detecting…"}</span>
            </div>
            <div className="mt-0.5 text-xs text-slate-400">
              Dhan aapko is IP se dekhta hai:{" "}
              <span className="font-mono text-sky-300">{ip?.dhanSeenIp ?? "-"}</span>
            </div>
            <div className="mt-0.5 text-xs text-slate-400">
              Dhan par registered:{" "}
              <span className="font-mono text-slate-300">
                {ip?.registered ? formatIp(ip.registered) : "-"}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
              <span
                className={
                  "rounded px-2 py-0.5 font-bold " +
                  (ip?.ordersAllowed
                    ? "bg-green-500/20 text-green-300"
                    : "bg-red-500/20 text-red-300")
                }
              >
                ordersAllowed: {String(ip?.ordersAllowed ?? "?")}
              </span>
              <span className="rounded bg-slate-700/40 px-2 py-0.5 font-mono text-slate-300">
                {ip?.ipMatchStatus ?? "-"}
              </span>
            </div>
            {ip?.match === false && (
              <p className="mt-1 text-xs text-amber-300">
                ⚠️ Aapka IP Dhan par whitelisted NAHI lag raha. Isi wajah se order pe
                "Invalid IP" aata hai. Neeche button dabao (ya Dhan web se Static IP set karo).
              </p>
            )}
            {ip?.match === true && ip?.ordersAllowed && (
              <p className="mt-1 text-xs text-green-400">
                ✓ IP match ho raha hai — orders allowed. Sab theek hai.
              </p>
            )}
          </div>
          <button
            className="btn-ghost whitespace-nowrap px-3 py-1.5 text-xs"
            onClick={registerIp}
            disabled={ipBusy}
            title="Auto-detect aapka public IP aur Dhan par register kar dega"
          >
            {ipBusy ? "Registering…" : "Register my IP"}
          </button>
        </div>
        <p className="mt-2 text-[11px] text-slate-500">
          Note: Dhan order placement ke liye aapke outbound IP ka whitelisted hona zaroori hai.
          Aapka IP badal sakta hai (router restart / ISP) — tab dobara register karo.
        </p>
      </div>

      {/* History cache — warmed at login so predictions are instant */}
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-semibold">📚 History cache (login pe auto-load)</div>
            <div className="mt-1 text-xs text-slate-400">
              {cache?.warmed
                ? "✓ Warm — historical data cache me hai, prediction fast hai."
                : "⏳ Warm ho raha hai… (login pe background me load hota hai)"}
            </div>
          </div>
          <button
            className="btn-ghost whitespace-nowrap px-3 py-1.5 text-xs"
            onClick={rewarm}
            disabled={warmBusy}
            title="Historical data dobara load karo (token/IP change ke baad)"
          >
            {warmBusy ? "Loading…" : "Re-warm"}
          </button>
        </div>
        {cache && Object.keys(cache.indices).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
            {Object.entries(cache.indices).map(([k, v]) => (
              <span
                key={k}
                className={
                  "rounded px-2 py-0.5 font-mono " +
                  (v.candles > 0
                    ? "bg-green-500/10 text-green-400"
                    : "bg-amber-500/10 text-amber-400")
                }
                title={`age ${Math.round(v.ageSeconds)}s, fresh=${v.fresh}`}
              >
                {k}: {v.candles} candles
              </span>
            ))}
          </div>
        )}
        <p className="mt-2 text-[11px] text-slate-500">
          Daily historical candles (5 saal) sirf <strong>ek baar</strong> load hote hain (login pe),
          phir cache se aate hain — isliye signals/prediction turant banti hai, aur rate-limit (429)
          ka risk nahi.
        </p>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 text-sm">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          Info (kaha kya hai)
        </div>
        <ul className="space-y-1 text-slate-300">
          <li>
            📁 <strong>Logs folder:</strong>{" "}
            <code className="rounded bg-slate-800 px-1 text-xs">{info?.logDir ?? "backend/logs"}</code>
          </li>
          <li>
            🗓️ <strong>Logs kitne din:</strong> last <strong>{info?.logRetentionDays ?? 7} days</strong>{" "}
            (purane auto-delete)
          </li>
          <li>
            🌐 <strong>App chalta hai:</strong>{" "}
            <code className="rounded bg-slate-800 px-1 text-xs">
              http://{info?.host ?? "127.0.0.1"}:{info?.port ?? 8000}
            </code>{" "}
            (backend), frontend{" "}
            <code className="rounded bg-slate-800 px-1 text-xs">http://127.0.0.1:5173</code>
          </li>
          <li>
            🔧 <strong>App run:</strong> project folder me <code>Dhan Dashboard.bat</code> dbl-click,
            ya alag-alag <code>Run Backend.bat</code> + <code>Run Frontend.bat</code>
          </li>
          <li>
            📝 <strong>Token kaha:</strong> <code className="rounded bg-slate-800 px-1 text-xs">backend/.env</code> me{" "}
            <code>DHAN_ACCESS_TOKEN</code>
          </li>
        </ul>
        <p className="mt-3 text-xs text-slate-500">
          Note: Ye switches <strong>memory me</strong> rehte hain. Backend restart karne pe <code>.env</code> ke
          default par wapas aa jate hain (safe state).
        </p>
      </div>
    </div>
  );
}

function formatIp(reg: unknown): string {
  if (!reg) return "-";
  if (typeof reg === "string") return reg;
  const obj = reg as { data?: unknown; primaryIP?: string; secondaryIP?: string };
  if (obj.primaryIP) return obj.primaryIP;
  if (Array.isArray(reg)) {
    const first = reg[0] as { ip?: string; primaryIP?: string } | undefined;
    return first?.ip || first?.primaryIP || "-";
  }
  const d = obj.data;
  if (Array.isArray(d) && d[0]) {
    const f = d[0] as { ip?: string; primaryIP?: string };
    return f.ip || f.primaryIP || "-";
  }
  if (d && typeof d === "object") {
    const dd = d as { primaryIP?: string };
    if (dd.primaryIP) return dd.primaryIP;
  }
  return "-";
}

function SwitchCard({
  title,
  value,
  onToggle,
  busy,
  onText,
  offText,
  desc,
  dangerWhenOff,
}: {
  title: string;
  value: boolean;
  onToggle: () => void;
  busy: boolean;
  onText: string;
  offText: string;
  desc: string;
  dangerWhenOff?: boolean;
}) {
  const danger = dangerWhenOff && !value;
  return (
    <div
      className={
        "rounded-lg border p-4 " +
        (danger ? "border-red-700/60 bg-red-950/30" : "border-slate-800 bg-slate-900/40")
      }
    >
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold">{title}</div>
          <div className="mt-0.5 text-xs text-slate-400">
            Current:{" "}
            <span className={"font-mono " + (value ? "text-green-400" : "text-slate-300")}>
              {value ? onText : offText}
            </span>
          </div>
        </div>
        <button
          className={
            "relative h-7 w-14 rounded-full transition-colors " +
            (value ? "bg-green-600" : "bg-slate-600")
          }
          onClick={onToggle}
          disabled={busy}
          title={value ? "Click to turn OFF" : "Click to turn ON"}
        >
          <span
            className={
              "absolute top-0.5 h-6 w-6 rounded-full bg-white transition-all " +
              (value ? "left-7" : "left-0.5")
            }
          />
        </button>
      </div>
      <p className={"mt-2 text-xs " + (danger ? "text-red-300" : "text-slate-400")}>{desc}</p>
    </div>
  );
}
