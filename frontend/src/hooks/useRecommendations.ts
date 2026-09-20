import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { RecommendationResponse, StrikeRecommendation } from "../api/types";

// How often the signal engine re-analyses (ms). The chain moves fast, so we
// refresh signals on a steady cadence — but not too aggressively: the backend
// already throttles and the engine re-reads cached history, so ~20s keeps us
// both responsive and well clear of Dhan's rate limits.
const REFRESH_MS = 20000;

// Fetches the per-strike recommendation set, re-analysing on an interval AND
// when the tab regains focus, so signals never go stale. Shared by the signals
// panel and the option-chain table (no duplicate calls).
export function useRecommendations(indexKey: string) {
  const [data, setData] = useState<RecommendationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const inFlight = useRef(false);

  const load = useCallback(
    async (silent = false) => {
      if (inFlight.current) return; // avoid overlapping calls stacking up
      inFlight.current = true;
      if (!silent) setLoading(true);
      try {
        const d = await api.get<RecommendationResponse>(
          `/api/intel/recommend?index=${indexKey}`
        );
        setData(d);
        setLastUpdated(Date.now());
      } catch {
        /* keep previous data on transient errors */
      } finally {
        setLoading(false);
        inFlight.current = false;
      }
    },
    [indexKey]
  );

  // Reset + first load on index change.
  useEffect(() => {
    setData(null);
    setLastUpdated(null);
    load(false);
  }, [load]);

  // Steady auto-refresh while mounted (silent = no spinner flicker).
  useEffect(() => {
    const t = window.setInterval(() => load(true), REFRESH_MS);
    return () => window.clearInterval(t);
  }, [load]);

  // Refresh immediately when the tab regains focus (user comes back to trade).
  useEffect(() => {
    const onFocus = () => load(true);
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  // Build a strike+side -> recommendation lookup for the chain table.
  const byKey = new Map<string, StrikeRecommendation>();
  if (data?.recommendations) {
    for (const r of data.recommendations) {
      byKey.set(`${r.strike}-${r.side}`, r);
    }
  }

  return { data, loading, reload: () => load(false), byKey, lastUpdated, refreshMs: REFRESH_MS };
}
