import { useEffect, useRef, useState } from "react";

// Polls a fetcher on an interval, non-overlapping, pausing when the tab is
// hidden (saves CPU + keeps things responsive). Returns latest data + error.
//
// IMPORTANT: the in-flight / cancelled guards are LOCAL to each effect run.
// (A shared useRef here broke under React 18 StrictMode: the first
// mount's fetch left `running` true when the effect was cleaned up and
// re-run, so the second run skipped its fetch and the data never arrived —
// the UI stayed stuck on "Loading…".)
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  enabled = true,
  identity: string | number = ""
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // Bumping this refetches immediately (used by the manual Refresh button).
  const [manualTick, setManualTick] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let inFlight = false;
    let timer: number | undefined;

    const tick = async () => {
      if (cancelled || inFlight) return;
      if (document.hidden) {
        timer = window.setTimeout(tick, intervalMs);
        return;
      }
      inFlight = true;
      setLoading(true);
      try {
        const d = await fetcherRef.current();
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "error");
      } finally {
        inFlight = false;
        if (!cancelled) {
          setLoading(false);
          timer = window.setTimeout(tick, intervalMs);
        }
      }
    };

    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // `identity` change (e.g. switching index) resets the data + refetches now.
  }, [enabled, intervalMs, identity, manualTick]);

  // Reset stale data when the identity changes so we never show the old index.
  useEffect(() => {
    setData(null);
    setError(null);
  }, [identity]);

  return { data, error, loading, refresh: () => setManualTick((t) => t + 1) };
}
