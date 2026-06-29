import { useCallback, useEffect, useState } from "react";
import type { WhitelistedPair } from "../types";

const LEGACY_STORAGE_KEY = "thanos-whitelisted-pairs";
const MIGRATED_KEY = "thanos-whitelisted-pairs:migrated";

function readLegacyPairs(): WhitelistedPair[] {
  try {
    const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function useWhitelistedPairs() {
  const [pairs, setPairs] = useState<WhitelistedPair[]>([]);

  const refresh = useCallback(async () => {
    const res = await fetch("/api/whitelist");
    if (!res.ok) return;
    const data = await res.json();
    setPairs(data.pairs ?? []);
  }, []);

  useEffect(() => {
    async function load() {
      if (localStorage.getItem(MIGRATED_KEY) !== "true") {
        const legacy = readLegacyPairs();
        if (legacy.length > 0) {
          await fetch("/api/whitelist/import", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pairs: legacy }),
          }).catch(() => undefined);
        }
        localStorage.setItem(MIGRATED_KEY, "true");
      }
      await refresh();
    }
    void load();
  }, [refresh]);

  const addPair = useCallback(
    async (poly: { id: string; title: string }, kalshi: { id: string; title: string }) => {
      const pair: WhitelistedPair = {
        id: `${poly.id}::${kalshi.id}`,
        polyId: poly.id,
        polyTitle: poly.title,
        kalshiId: kalshi.id,
        kalshiTitle: kalshi.title,
        addedAt: new Date().toISOString(),
      };
      const res = await fetch("/api/whitelist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pair),
      });
      if (res.ok) {
        const data = await res.json();
        setPairs(data.pairs ?? []);
      }
    },
    [],
  );

  const removePair = useCallback(async (pairId: string) => {
    const res = await fetch(`/api/whitelist/${encodeURIComponent(pairId)}`, { method: "DELETE" });
    if (res.ok) {
      const data = await res.json();
      setPairs(data.pairs ?? []);
    }
  }, []);

  return { pairs, addPair, removePair, refresh };
}
