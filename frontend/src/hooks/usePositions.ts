import { useCallback, useSyncExternalStore } from "react";
import type { ArbitragePosition } from "../types";

const STORAGE_KEY = "thanos:positions";

type Listener = () => void;
const listeners = new Set<Listener>();
let cachedSnapshot: ArbitragePosition[] = readFromStorage();

function readFromStorage(): ArbitragePosition[] {
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

function getSnapshot(): ArbitragePosition[] {
  return cachedSnapshot;
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePositions() {
  const positions = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const addPosition = useCallback(
    (data: Omit<ArbitragePosition, "id" | "addedAt" | "status">) => {
      const current = readFromStorage();
      const pos: ArbitragePosition = {
        ...data,
        id: crypto.randomUUID(),
        addedAt: new Date().toISOString(),
        status: "open",
      };
      localStorage.setItem(STORAGE_KEY, JSON.stringify([pos, ...current]));
      emitChange();
    },
    [],
  );

  const removePosition = useCallback((id: string) => {
    const current = readFromStorage();
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current.filter((p) => p.id !== id)));
    emitChange();
  }, []);

  const closePosition = useCallback((id: string, realizedPnl: number) => {
    const current = readFromStorage();
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(
        current.map((p) =>
          p.id === id
            ? { ...p, status: "closed" as const, closedAt: new Date().toISOString(), realizedPnl }
            : p,
        ),
      ),
    );
    emitChange();
  }, []);

  return { positions, addPosition, removePosition, closePosition };
}
