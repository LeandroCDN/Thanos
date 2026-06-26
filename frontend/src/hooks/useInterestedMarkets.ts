import { useCallback, useSyncExternalStore } from "react";

const STORAGE_KEY = "thanos-interested-markets";

interface InterestedEntry {
  id: string;
  source: "polymarket" | "kalshi";
  title: string;
  addedAt: string;
}

type Listener = () => void;

const listeners = new Set<Listener>();
let cachedSnapshot: InterestedEntry[] = readFromStorage();

function readFromStorage(): InterestedEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function emitChange() {
  cachedSnapshot = readFromStorage();
  listeners.forEach((fn) => fn());
}

function getSnapshot(): InterestedEntry[] {
  return cachedSnapshot;
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useInterestedMarkets() {
  const entries = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const add = useCallback((market: { id: string; source: "polymarket" | "kalshi"; title: string }) => {
    const current = readFromStorage();
    if (current.some((e) => e.id === market.id && e.source === market.source)) return;
    const updated = [...current, { ...market, addedAt: new Date().toISOString() }];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(updated));
    emitChange();
  }, []);

  const remove = useCallback((id: string, source: "polymarket" | "kalshi") => {
    const current = readFromStorage();
    const updated = current.filter((e) => !(e.id === id && e.source === source));
    localStorage.setItem(STORAGE_KEY, JSON.stringify(updated));
    emitChange();
  }, []);

  const isInterested = useCallback(
    (id: string, source: "polymarket" | "kalshi") => {
      return entries.some((e) => e.id === id && e.source === source);
    },
    [entries],
  );

  return { entries, add, remove, isInterested };
}
