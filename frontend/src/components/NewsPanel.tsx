import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { NewsResponse, Sentiment } from "../api/types";

// Market headlines from free RSS feeds, each tagged with an explainable
// sentiment (BULLISH / BEARISH / NEUTRAL). Auto-refreshes every 5 minutes so the
// trader never has to click — but a manual ⟳ button is there too.
const AUTO_MS = 5 * 60_000;

export function NewsPanel() {
  const [data, setData] = useState<NewsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const load = async (refresh: boolean) => {
    setLoading(true);
    try {
      const q = refresh ? "?refresh=1&_=" + Date.now() : "";
      setData(await api.get<NewsResponse>(`/api/intel/news${q}`));
    } catch {
      /* keep previous */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(false);
    // Auto-refresh every 5 min, and also when the tab regains focus.
    const schedule = () => {
      timer.current = window.setTimeout(async () => {
        await load(true);
        schedule();
      }, AUTO_MS);
    };
    schedule();
    const onFocus = () => load(false);
    window.addEventListener("focus", onFocus);
    return () => {
      if (timer.current) clearTimeout(timer.current);
      window.removeEventListener("focus", onFocus);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const stanceColor = (s?: string) =>
    s === "BULLISH"
      ? "text-green-400 border-green-600/50 bg-green-500/10"
      : s === "BEARISH"
      ? "text-red-400 border-red-600/50 bg-red-500/10"
      : "text-slate-300 border-slate-600/50 bg-slate-500/10";

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="text-lg">📰</span>
          <span className="text-sm font-bold uppercase tracking-wide text-slate-200">
            Market News
          </span>
          {data?.summary && (
            <span
              className={
                "rounded-md border px-2.5 py-0.5 text-xs font-bold " +
                stanceColor(data.summary.stance)
              }
            >
              {data.summary.stance} · {data.summary.bullish}↑ / {data.summary.bearish}↓
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {data?.ageSeconds != null && (
            <span className="text-xs text-slate-500">
              updated {Math.round(data.ageSeconds)}s ago
            </span>
          )}
          <button
            className="rounded-md border border-slate-700 px-2.5 py-1 text-xs font-medium text-sky-400 transition-colors hover:border-sky-600 hover:text-sky-300 disabled:opacity-50"
            onClick={() => load(true)}
            disabled={loading}
          >
            {loading ? "Refreshing…" : "⟳ Refresh"}
          </button>
        </div>
      </div>

      {/* Body */}
      {!data ? (
        <div className="py-8 text-center text-sm text-slate-500">Loading headlines…</div>
      ) : data.items.length === 0 ? (
        <div className="py-8 text-center text-sm text-slate-500">
          No headlines{data.error ? ` (${data.error})` : ""}.
        </div>
      ) : (
        <ul className="divide-y divide-slate-800">
          {sortedByTime(data.items).map((n, i) => (
            <li key={i}>
              <a
                href={n.link}
                target="_blank"
                rel="noreferrer noopener"
                className="group flex gap-3 px-4 py-3.5 transition-colors hover:bg-slate-800/50"
              >
                <SentimentBadge s={n.sentiment} />
                <div className="min-w-0 flex-1">
                  <p className="text-[15px] font-medium leading-relaxed text-slate-100 group-hover:text-sky-300">
                    {n.title}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <span className="rounded bg-slate-800 px-2 py-0.5 font-medium text-slate-400">
                      {n.source}
                    </span>
                    {n.published && <span>{relTime(n.published)}</span>}
                    {n.sentiment.impact === "high" && (
                      <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-amber-400">
                        high impact
                      </span>
                    )}
                    {n.sentiment.matched?.length > 0 && (
                      <span className="text-[10px] italic text-slate-600">
                        why: {n.sentiment.matched.slice(0, 3).join(", ")}
                      </span>
                    )}
                  </div>
                </div>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SentimentBadge({ s }: { s: Sentiment }) {
  const cls =
    s.label === "BULLISH"
      ? "text-green-400 bg-green-500/10 border-green-600/40"
      : s.label === "BEARISH"
      ? "text-red-400 bg-red-500/10 border-red-600/40"
      : "text-slate-400 bg-slate-700/20 border-slate-600/40";
  const arrow = s.label === "BULLISH" ? "▲" : s.label === "BEARISH" ? "▼" : "▬";
  return (
    <span
      className={
        "mt-0.5 flex h-8 w-8 shrink-0 flex-col items-center justify-center rounded-md border text-[10px] font-bold leading-none " +
        cls
      }
      title={`${s.label} (${s.confidence}% confidence, ${s.impact} impact) — matched: ${s.matched.join(", ") || "none"}`}
    >
      <span className="text-sm">{arrow}</span>
      <span>{s.confidence}</span>
    </span>
  );
}

// Newest first. `ts` is the parsed epoch from the backend; if missing, parse
// `published`; undated items sink to the bottom.
function itemTs(n: { ts?: number | null; published?: string | null }): number {
  if (typeof n.ts === "number") return n.ts;
  if (n.published) {
    const t = Date.parse(n.published);
    if (!Number.isNaN(t)) return t;
  }
  return 0;
}
function sortedByTime(items: NewsResponse["items"]) {
  return [...items].sort((a, b) => itemTs(b) - itemTs(a));
}

// "2h ago", "3d ago", etc. Falls back to the raw string if unparseable.
function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const secs = Math.max(0, (Date.now() - t) / 1000);
  if (secs < 60) return "just now";
  const mins = secs / 60;
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  const hrs = mins / 60;
  if (hrs < 24) return `${Math.floor(hrs)}h ago`;
  const days = hrs / 24;
  return `${Math.floor(days)}d ago`;
}
