import { useState, useCallback } from "react";
import type { Market, MarketFilters } from "../types";

interface UseMarketsReturn {
  polymarkets: Market[];
  kalshiMarkets: Market[];
  loading: boolean;
  error: string | null;
  lastRefreshed: Date | null;
  fromCache: { poly: boolean; kalshi: boolean };
  refresh: (filters: MarketFilters, forceRefresh?: boolean) => Promise<void>;
}

function buildQueryParams(filters: MarketFilters, forceRefresh: boolean): string {
  const params = new URLSearchParams();
  if (filters.endDateMin) params.set("end_date_min", filters.endDateMin);
  if (filters.endDateMax) params.set("end_date_max", filters.endDateMax);
  if (filters.volumeMin > 0) params.set("volume_min", String(filters.volumeMin));
  if (filters.liquidityMin > 0) params.set("liquidity_min", String(filters.liquidityMin));
  if (filters.maxPages > 0) params.set("max_pages", String(filters.maxPages));
  if (forceRefresh) params.set("force_refresh", "true");
  return params.toString();
}

export function useMarkets(): UseMarketsReturn {
  const [polymarkets, setPolymarkets] = useState<Market[]>([]);
  const [kalshiMarkets, setKalshiMarkets] = useState<Market[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [fromCache, setFromCache] = useState({ poly: false, kalshi: false });

  const refresh = useCallback(async (filters: MarketFilters, forceRefresh = false) => {
    setLoading(true);
    setError(null);

    try {
      const qs = buildQueryParams(filters, forceRefresh);
      const [polyRes, kalshiRes] = await Promise.all([
        fetch(`/api/markets/polymarket?${qs}`),
        fetch(`/api/markets/kalshi?${qs}`),
      ]);

      if (!polyRes.ok) {
        throw new Error(`Polymarket API error: ${polyRes.status}`);
      }
      if (!kalshiRes.ok) {
        throw new Error(`Kalshi API error: ${kalshiRes.status}`);
      }

      const polyData = await polyRes.json();
      const kalshiData = await kalshiRes.json();

      setPolymarkets(polyData.markets);
      setKalshiMarkets(kalshiData.markets);
      setFromCache({ poly: polyData.from_cache, kalshi: kalshiData.from_cache });
      setLastRefreshed(new Date());
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown error";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  return { polymarkets, kalshiMarkets, loading, error, lastRefreshed, fromCache, refresh };
}
