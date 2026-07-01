import { useDeferredValue, useMemo, useState } from "react";
import { useMarkets } from "./hooks/useMarkets";
import { useInterestedMarkets } from "./hooks/useInterestedMarkets";
import { useWhitelistedPairs } from "./hooks/useWhitelistedPairs";
import { usePositions } from "./hooks/usePositions";
import { useLivePairMarkets } from "./hooks/useLivePairMarkets";
import { buildReviewCandidate, usePairReviews } from "./hooks/usePairReviews";
import { MarketTable, type SortDir, type SortField } from "./components/MarketTable";
import { MarketModal } from "./components/MarketModal";
import { FilterBar } from "./components/FilterBar";
import { WhitelistedPanel } from "./components/WhitelistedPanel";
import { PositionsPanel } from "./components/PositionsPanel";
import { BotPanel } from "./components/BotPanel";
import { excludeSuggestedPairs, findSuggestedMatches, getPairKey } from "./utils/marketMatcher";
import { pairRankingScore } from "./utils/pairRanking";
import type { Market, MarketFilters, MarketMatchMap } from "./types";

const DEFAULT_FILTERS: MarketFilters = {
  endDateMin: "",
  endDateMax: "",
  volumeMin: 0,
  liquidityMin: 0,
  maxPages: 50,
};
const EMPTY_MATCHES: MarketMatchMap = {};
const EMPTY_KEY_SET = new Set<string>();

export default function App() {
  const { polymarkets, kalshiMarkets, loading, error, lastRefreshed, fromCache, refresh } =
    useMarkets();
  const [searchQuery, setSearchQuery] = useState("");
  const deferredSearchQuery = useDeferredValue(searchQuery);
  const [filters, setFilters] = useState<MarketFilters>(DEFAULT_FILTERS);
  const [suggestedMatchingEnabled, setSuggestedMatchingEnabled] = useState(true);
  const [allowMultiplePairs, setAllowMultiplePairs] = useState(false);
  const [marketSort, setMarketSort] = useState<{ field: SortField; dir: SortDir }>({
    field: "volume",
    dir: "desc",
  });
  const [selectedMarket, setSelectedMarket] = useState<Market | null>(null);
  const { add, remove, isInterested } = useInterestedMarkets();
  const { pairs, addPair, removePair } = useWhitelistedPairs();
  const { positions, addPosition, removePosition } = usePositions();
  const [selectedPoly, setSelectedPoly] = useState<Market | null>(null);
  const [selectedKalshi, setSelectedKalshi] = useState<Market | null>(null);
  const {
    markets: pairsLiveMarkets,
    refreshing: pairsRefreshing,
    connected: pairsStreamConnected,
    refresh: refreshPairs,
  } = useLivePairMarkets(pairs);

  function handleTogglePoly(market: Market) {
    setSelectedPoly((prev) => (prev?.id === market.id ? null : market));
  }

  function handleToggleKalshi(market: Market) {
    setSelectedKalshi((prev) => (prev?.id === market.id ? null : market));
  }

  function handleWhitelist() {
    if (!effectiveSelectedPoly || !effectiveSelectedKalshi) return;
    addPair(
      { id: effectiveSelectedPoly.id, title: effectiveSelectedPoly.title },
      { id: effectiveSelectedKalshi.id, title: effectiveSelectedKalshi.title },
    );
    setSelectedPoly(null);
    setSelectedKalshi(null);
  }

  const whitelistedPairIds = useMemo(
    () => new Set(pairs.map((pair) => pair.id || getPairKey(pair.polyId, pair.kalshiId))),
    [pairs],
  );
  const whitelistedMarketKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const pair of pairs) {
      keys.add(`polymarket:${pair.polyId}`);
      keys.add(`kalshi:${pair.kalshiId}`);
    }
    return keys;
  }, [pairs]);
  const visiblePolymarkets = useMemo(
    () => allowMultiplePairs
      ? polymarkets
      : polymarkets.filter((market) => !whitelistedMarketKeys.has(`polymarket:${market.id}`)),
    [allowMultiplePairs, polymarkets, whitelistedMarketKeys],
  );
  const visibleKalshiMarkets = useMemo(
    () => allowMultiplePairs
      ? kalshiMarkets
      : kalshiMarkets.filter((market) => !whitelistedMarketKeys.has(`kalshi:${market.id}`)),
    [allowMultiplePairs, kalshiMarkets, whitelistedMarketKeys],
  );
  const discoveredMatches = useMemo(
    () => findSuggestedMatches(polymarkets, kalshiMarkets),
    [polymarkets, kalshiMarkets],
  );
  const suggestedMatches = useMemo(
    () =>
      excludeSuggestedPairs(
        discoveredMatches,
        whitelistedPairIds,
        allowMultiplePairs ? EMPTY_KEY_SET : whitelistedMarketKeys,
      ),
    [allowMultiplePairs, discoveredMatches, whitelistedMarketKeys, whitelistedPairIds],
  );
  const allMarkets = useMemo(
    () => [...polymarkets, ...kalshiMarkets],
    [polymarkets, kalshiMarkets],
  );
  const visibleAllMarkets = useMemo(
    () => [...visiblePolymarkets, ...visibleKalshiMarkets],
    [visiblePolymarkets, visibleKalshiMarkets],
  );
  const modalMarkets = useMemo(() => {
    const byKey = new Map<string, Market>();
    for (const market of allMarkets) byKey.set(`${market.source}:${market.id}`, market);
    for (const market of pairsLiveMarkets) byKey.set(`${market.source}:${market.id}`, market);
    return [...byKey.values()];
  }, [allMarkets, pairsLiveMarkets]);
  const activeSuggestedMatches = suggestedMatchingEnabled ? suggestedMatches : EMPTY_MATCHES;
  const aiReviewCandidates = useMemo(() => {
    if (!suggestedMatchingEnabled) return [];
    const byKey = new Map(visibleAllMarkets.map((market) => [`${market.source}:${market.id}`, market]));
    const byPair = new Map<string, ReturnType<typeof buildReviewCandidate>>();
    for (const market of visibleAllMarkets) {
      const match = suggestedMatches[`${market.source}:${market.id}`];
      if (!match) continue;
      for (const candidate of match.candidates) {
        const peer = byKey.get(`${candidate.peerSource}:${candidate.peerId}`);
        if (!peer) continue;
        const reviewCandidate = buildReviewCandidate(market, peer, candidate);
        if (reviewCandidate) byPair.set(reviewCandidate.pairKey, reviewCandidate);
      }
    }
    return [...byPair.values()]
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .sort((a, b) => b.discovery.score - a.discovery.score);
  }, [visibleAllMarkets, suggestedMatches, suggestedMatchingEnabled]);
  const { reviews: pairReviews, loading: aiReviewsLoading, reviewPairs } = usePairReviews(
    aiReviewCandidates,
    suggestedMatchingEnabled,
  );
  const aiAlignedOrder = useMemo(() => {
    const ranked = aiReviewCandidates
      .map((candidate) => ({
        candidate,
        score: pairRankingScore(pairReviews[candidate.pairKey], candidate.discovery.score),
      }))
      .sort((a, b) => {
        const scoreDiff = b.score - a.score;
        if (scoreDiff !== 0) return scoreDiff;
        return b.candidate.discovery.score - a.candidate.discovery.score;
      });

    const usedPoly = new Set<string>();
    const usedKalshi = new Set<string>();
    const polyKeys: string[] = [];
    const kalshiKeys: string[] = [];
    const polyPeerByKey: Record<string, string> = {};
    const kalshiPeerByKey: Record<string, string> = {};

    for (const { candidate } of ranked) {
      const polyKey = `polymarket:${candidate.poly.id}`;
      const kalshiKey = `kalshi:${candidate.kalshi.id}`;
      if (usedPoly.has(polyKey) || usedKalshi.has(kalshiKey)) continue;
      usedPoly.add(polyKey);
      usedKalshi.add(kalshiKey);
      polyKeys.push(polyKey);
      kalshiKeys.push(kalshiKey);
      polyPeerByKey[polyKey] = kalshiKey;
      kalshiPeerByKey[kalshiKey] = polyKey;
    }

    return { polyKeys, kalshiKeys, polyPeerByKey, kalshiPeerByKey };
  }, [aiReviewCandidates, pairReviews]);
  const suggestedGroupCount = new Set(
    Object.values(activeSuggestedMatches).map((match) => match.groupId),
  ).size;
  const hiddenWhitelistedMarketCount = allowMultiplePairs
    ? 0
    : polymarkets.length + kalshiMarkets.length - visiblePolymarkets.length - visibleKalshiMarkets.length;
  const effectiveSelectedPoly =
    selectedPoly && (allowMultiplePairs || !whitelistedMarketKeys.has(`polymarket:${selectedPoly.id}`))
      ? selectedPoly
      : null;
  const effectiveSelectedKalshi =
    selectedKalshi && (allowMultiplePairs || !whitelistedMarketKeys.has(`kalshi:${selectedKalshi.id}`))
      ? selectedKalshi
      : null;

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

        <BotPanel />

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
              selectedPoly={effectiveSelectedPoly}
              selectedKalshi={effectiveSelectedKalshi}
              onWhitelist={handleWhitelist}
              onRemovePair={removePair}
              onRefreshPairs={refreshPairs}
              pairsRefreshing={pairsRefreshing}
              pairsStreamConnected={pairsStreamConnected}
              onRecordPosition={addPosition}
            />
          )}

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
            allowMultiplePairs={allowMultiplePairs}
            onAllowMultiplePairsChange={setAllowMultiplePairs}
            hiddenWhitelistedMarketCount={hiddenWhitelistedMarketCount}
          />

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
                {suggestedGroupCount} broad discovery group{suggestedGroupCount !== 1 ? "s" : ""} highlighted.
                AI review keeps all candidates visible and flags the strongest analogs{aiReviewsLoading ? "…" : "."}
              </div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50">
                  <h2 className="font-semibold text-blue-400">Polymarket</h2>
                </div>
                <MarketTable
                  markets={visiblePolymarkets}
                  allMarkets={visibleAllMarkets}
                  loading={loading}
                  searchQuery={deferredSearchQuery}
                  suggestedMatches={activeSuggestedMatches}
                  pairReviews={pairReviews}
                  onMarketClick={setSelectedMarket}
                  isInterested={isInterested}
                  selectedId={effectiveSelectedPoly?.id ?? null}
                  onToggleSelect={handleTogglePoly}
                  sortField={marketSort.field}
                  sortDir={marketSort.dir}
                  onSortChange={(field, dir) => setMarketSort({ field, dir })}
                  alignedOrderKeys={aiAlignedOrder.polyKeys}
                  alignedPeerByKey={aiAlignedOrder.polyPeerByKey}
                />
              </div>

              <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50">
                  <h2 className="font-semibold text-green-400">Kalshi</h2>
                </div>
                <MarketTable
                  markets={visibleKalshiMarkets}
                  allMarkets={visibleAllMarkets}
                  loading={loading}
                  searchQuery={deferredSearchQuery}
                  suggestedMatches={activeSuggestedMatches}
                  pairReviews={pairReviews}
                  onMarketClick={setSelectedMarket}
                  isInterested={isInterested}
                  selectedId={effectiveSelectedKalshi?.id ?? null}
                  onToggleSelect={handleToggleKalshi}
                  sortField={marketSort.field}
                  sortDir={marketSort.dir}
                  onSortChange={(field, dir) => setMarketSort({ field, dir })}
                  alignedOrderKeys={aiAlignedOrder.kalshiKeys}
                  alignedPeerByKey={aiAlignedOrder.kalshiPeerByKey}
                />
              </div>
            </div>
          </div>
        )}
        </div>
      </main>

      <footer className="border-t border-gray-800 px-6 py-3 text-center text-xs text-gray-600">
        Thanos Arb Explorer — Data from public APIs, live opens require confirmation
      </footer>

      {selectedMarket && (
        <MarketModal
          market={selectedMarket}
          allMarkets={modalMarkets}
          suggestedMatches={activeSuggestedMatches}
          pairReviews={pairReviews}
          aiReviewsLoading={aiReviewsLoading}
          onReviewPairs={reviewPairs}
          isInterested={isInterested(selectedMarket.id, selectedMarket.source)}
          onAddInterested={() => add({ id: selectedMarket.id, source: selectedMarket.source, title: selectedMarket.title })}
          onRemoveInterested={() => remove(selectedMarket.id, selectedMarket.source)}
          onClose={() => setSelectedMarket(null)}
          onWhitelistPair={(poly, kalshi) => {
            setSelectedMarket(null);
            void addPair({ id: poly.id, title: poly.title }, { id: kalshi.id, title: kalshi.title });
          }}
          whitelistedPairIds={whitelistedPairIds}
          whitelistedPairs={pairs}
        />
      )}
    </div>
  );
}
