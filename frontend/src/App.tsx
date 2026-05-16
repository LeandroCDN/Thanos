import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useMarkets } from "./hooks/useMarkets";
import { useInterestedMarkets } from "./hooks/useInterestedMarkets";
import { useWhitelistedPairs } from "./hooks/useWhitelistedPairs";
import { usePositions } from "./hooks/usePositions";
import { MarketTable } from "./components/MarketTable";
import { MarketModal } from "./components/MarketModal";
import { FilterBar } from "./components/FilterBar";
import { WhitelistedPanel } from "./components/WhitelistedPanel";
import { PositionsPanel } from "./components/PositionsPanel";
import { findSuggestedMatches } from "./utils/marketMatcher";
import type { Market, MarketFilters, MarketMatchMap } from "./types";

const DEFAULT_FILTERS: MarketFilters = {
  endDateMin: "",
  endDateMax: "",
  volumeMin: 0,
  liquidityMin: 0,
  maxPages: 50,
};
const EMPTY_MATCHES: MarketMatchMap = {};

export default function App() {
  const { polymarkets, kalshiMarkets, loading, error, lastRefreshed, fromCache, refresh } =
    useMarkets();
  const [searchQuery, setSearchQuery] = useState("");
  const deferredSearchQuery = useDeferredValue(searchQuery);
  const [filters, setFilters] = useState<MarketFilters>(DEFAULT_FILTERS);
  const [suggestedMatchingEnabled, setSuggestedMatchingEnabled] = useState(true);
  const [selectedMarket, setSelectedMarket] = useState<Market | null>(null);
  const { add, remove, isInterested } = useInterestedMarkets();
  const { pairs, addPair, removePair } = useWhitelistedPairs();
  const { positions, addPosition, removePosition } = usePositions();
  const [selectedPoly, setSelectedPoly] = useState<Market | null>(null);
  const [selectedKalshi, setSelectedKalshi] = useState<Market | null>(null);
  const [pairsLiveMarkets, setPairsLiveMarkets] = useState<Market[]>([]);
  const [pairsRefreshing, setPairsRefreshing] = useState(false);
  const didAutoRefreshPairs = useRef(false);

  function handleTogglePoly(market: Market) {
    setSelectedPoly((prev) => (prev?.id === market.id ? null : market));
  }

  function handleToggleKalshi(market: Market) {
    setSelectedKalshi((prev) => (prev?.id === market.id ? null : market));
  }

  async function handleRefreshPairs() {
    if (!pairs.length) return;
    setPairsRefreshing(true);
    try {
      const polyIds = [...new Set(pairs.map((p) => p.polyId))].join(",");
      const kalshiIds = [...new Set(pairs.map((p) => p.kalshiId))].join(",");
      const res = await fetch(
        `/api/markets/pairs?poly_ids=${encodeURIComponent(polyIds)}&kalshi_ids=${encodeURIComponent(kalshiIds)}`,
      );
      if (res.ok) {
        const data = await res.json();
        setPairsLiveMarkets([...(data.polymarket ?? []), ...(data.kalshi ?? [])]);
      }
    } finally {
      setPairsRefreshing(false);
    }
  }

  // Auto-refresh whitelisted pair prices once on mount if any pairs are saved
  useEffect(() => {
    if (!didAutoRefreshPairs.current && pairs.length > 0) {
      didAutoRefreshPairs.current = true;
      handleRefreshPairs();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairs.length]);

  function handleWhitelist() {
    if (!selectedPoly || !selectedKalshi) return;
    addPair(
      { id: selectedPoly.id, title: selectedPoly.title },
      { id: selectedKalshi.id, title: selectedKalshi.title },
    );
    setSelectedPoly(null);
    setSelectedKalshi(null);
  }

  const suggestedMatches = useMemo(
    () => findSuggestedMatches(polymarkets, kalshiMarkets),
    [polymarkets, kalshiMarkets],
  );
  const activeSuggestedMatches = suggestedMatchingEnabled ? suggestedMatches : EMPTY_MATCHES;
  const suggestedGroupCount = new Set(
    Object.values(activeSuggestedMatches).map((match) => match.groupId),
  ).size;

  const allMarkets = useMemo(
    () => [...polymarkets, ...kalshiMarkets],
    [polymarkets, kalshiMarkets],
  );

  function handleRefresh(forceRefresh = false) {
    refresh(filters, forceRefresh);
  }

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-gray-800 px-6 py-4">
        <div className="max-w-[1800px] mx-auto flex items-center justify-between">
          <h1 className="text-xl font-bold tracking-tight">
            <span className="text-purple-400">Thanos</span>{" "}
            <span className="text-gray-400 font-normal">Arb Market Explorer</span>
          </h1>
          <div className="text-xs text-gray-500">
            {polymarkets.length + kalshiMarkets.length > 0 && (
              <span>
                {polymarkets.length} Poly · {kalshiMarkets.length} Kalshi
              </span>
            )}
          </div>
        </div>
      </header>

      <main className="flex-1 px-6 py-4 max-w-[1800px] mx-auto w-full space-y-4">
        <PositionsPanel
          positions={positions}
          onRemove={removePosition}
        />

        <div>
          <FilterBar
            filters={filters}
            onFiltersChange={setFilters}
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            onRefresh={handleRefresh}
            loading={loading}
            lastRefreshed={lastRefreshed}
            fromCache={fromCache}
            suggestedMatchingEnabled={suggestedMatchingEnabled}
            onSuggestedMatchingEnabledChange={setSuggestedMatchingEnabled}
          />
        </div>

        {error && (
          <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-lg text-red-300 text-sm">
            {error}
          </div>
        )}

        <div className="space-y-3">
          {(pairs.length > 0 || lastRefreshed || loading) && (
            <WhitelistedPanel
              pairs={pairs}
              allMarkets={allMarkets}
              liveMarkets={pairsLiveMarkets}
              selectedPoly={selectedPoly}
              selectedKalshi={selectedKalshi}
              onWhitelist={handleWhitelist}
              onRemovePair={removePair}
              onRefreshPairs={handleRefreshPairs}
              pairsRefreshing={pairsRefreshing}
              onRecordPosition={addPosition}
            />
          )}

          {!lastRefreshed && !loading && (
            <div className="flex items-center justify-center h-64 text-gray-500">
              <div className="text-center">
                <p className="text-lg mb-2">Click "Refresh" to load markets</p>
                <p className="text-sm">
                  Data will be fetched from Polymarket and Kalshi
                </p>
              </div>
            </div>
          )}

        {(lastRefreshed || loading) && (
          <div className="space-y-3">
            {suggestedMatchingEnabled && suggestedGroupCount > 0 && (
              <div className="rounded-lg border border-purple-800/60 bg-purple-950/30 px-4 py-2 text-sm text-purple-100">
                {suggestedGroupCount} suggested group{suggestedGroupCount !== 1 ? "s" : ""} sorted to the top.
                Colors are relevance hints, not confirmed matches.
              </div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50">
                  <h2 className="font-semibold text-green-400">Polymarket</h2>
                </div>
                <MarketTable
                  markets={polymarkets}
                  allMarkets={allMarkets}
                  loading={loading}
                  searchQuery={deferredSearchQuery}
                  suggestedMatches={activeSuggestedMatches}
                  onMarketClick={setSelectedMarket}
                  isInterested={isInterested}
                  selectedId={selectedPoly?.id ?? null}
                  onToggleSelect={handleTogglePoly}
                />
              </div>

              <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50">
                  <h2 className="font-semibold text-blue-400">Kalshi</h2>
                </div>
                <MarketTable
                  markets={kalshiMarkets}
                  allMarkets={allMarkets}
                  loading={loading}
                  searchQuery={deferredSearchQuery}
                  suggestedMatches={activeSuggestedMatches}
                  onMarketClick={setSelectedMarket}
                  isInterested={isInterested}
                  selectedId={selectedKalshi?.id ?? null}
                  onToggleSelect={handleToggleKalshi}
                />
              </div>
            </div>
          </div>
        )}
        </div>
      </main>

      <footer className="border-t border-gray-800 px-6 py-3 text-center text-xs text-gray-600">
        Thanos Arb Explorer — Data from public APIs, no trading functionality
      </footer>

      {selectedMarket && (
        <MarketModal
          market={selectedMarket}
          allMarkets={allMarkets}
          suggestedMatches={activeSuggestedMatches}
          isInterested={isInterested(selectedMarket.id, selectedMarket.source)}
          onAddInterested={() => add({ id: selectedMarket.id, source: selectedMarket.source, title: selectedMarket.title })}
          onRemoveInterested={() => remove(selectedMarket.id, selectedMarket.source)}
          onClose={() => setSelectedMarket(null)}
          onWhitelistPair={(poly, kalshi) => {
            addPair({ id: poly.id, title: poly.title }, { id: kalshi.id, title: kalshi.title });
          }}
          whitelistedPairIds={new Set(pairs.map((p) => p.id))}
        />
      )}
    </div>
  );
}
