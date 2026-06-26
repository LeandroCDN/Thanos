import { useCallback, useSyncExternalStore } from "react";
import type { WhitelistedPair } from "../types";

const STORAGE_KEY = "thanos-whitelisted-pairs";

type Listener = () => void;
const listeners = new Set<Listener>();
let cachedSnapshot: WhitelistedPair[] = readFromStorage();

function readFromStorage(): WhitelistedPair[] {
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

function getSnapshot(): WhitelistedPair[] {
  return cachedSnapshot;
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useWhitelistedPairs() {
  const pairs = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const addPair = useCallback(
    (poly: { id: string; title: string }, kalshi: { id: string; title: string }) => {
      const current = readFromStorage();
      const alreadyExists = current.some(
        (p) => p.polyId === poly.id && p.kalshiId === kalshi.id,
      );
      if (alreadyExists) return;
      const pair: WhitelistedPair = {
        id: `${poly.id}::${kalshi.id}`,
        polyId: poly.id,
        polyTitle: poly.title,
        kalshiId: kalshi.id,
        kalshiTitle: kalshi.title,
        addedAt: new Date().toISOString(),
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify([...current, pair]));
      emitChange();
    },
    [],
  );

  const removePair = useCallback((pairId: string) => {
    const current = readFromStorage();
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(current.filter((p) => p.id !== pairId)),
    );
    emitChange();
  }, []);

  return { pairs, addPair, removePair };
}
