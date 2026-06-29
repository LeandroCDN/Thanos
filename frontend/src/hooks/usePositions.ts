import { useCallback, useEffect, useState } from "react";
import type { ArbitragePosition } from "../types";

const LEGACY_STORAGE_KEY = "thanos:positions";
const MIGRATED_KEY = "thanos:positions:migrated";

function readLegacyPositions(): ArbitragePosition[] {
  try {
    const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function usePositions() {
  const [positions, setPositions] = useState<ArbitragePosition[]>([]);

  const refresh = useCallback(async () => {
    const res = await fetch("/api/positions");
    if (!res.ok) return;
    const data = await res.json();
    setPositions(data.positions ?? []);
  }, []);

  useEffect(() => {
    async function load() {
      if (localStorage.getItem(MIGRATED_KEY) !== "true") {
        const legacy = readLegacyPositions();
        if (legacy.length > 0) {
          await fetch("/api/positions/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ positions: legacy }),
          }).catch(() => undefined);
        }
        localStorage.setItem(MIGRATED_KEY, "true");
      }
      await refresh();
    }
    void load();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const addPosition = useCallback(
    async (data: Omit<ArbitragePosition, "id" | "addedAt" | "status">) => {
      const pos = {
        ...data,
        addedAt: new Date().toISOString(),
        status: "open" as const,
        source: data.source ?? "manual",
        executionMode: data.executionMode ?? "live",
      };
      const res = await fetch("/api/positions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pos),
      });
      if (res.ok) {
        const payload = await res.json();
        setPositions(payload.positions ?? []);
      }
    },
    [],
  );

  const removePosition = useCallback(async (id: string) => {
    const res = await fetch(`/api/positions/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (res.ok) {
      const payload = await res.json();
      setPositions(payload.positions ?? []);
    }
  }, []);

  const closePosition = useCallback(async (id: string, realizedPnl: number) => {
    const res = await fetch(`/api/positions/${encodeURIComponent(id)}/close`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ realizedPnl, reason: "manual" }),
    });
    if (res.ok) {
      const payload = await res.json();
      setPositions(payload.positions ?? []);
    }
  }, []);

  return { positions, addPosition, removePosition, closePosition, refresh };
}
