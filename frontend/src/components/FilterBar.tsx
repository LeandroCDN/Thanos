import type { MarketFilters } from "../types";

interface FilterBarProps {
  filters: MarketFilters;
  onFiltersChange: (filters: MarketFilters) => void;
  searchQuery: string;
  onSearchChange: (query: string) => void;
  suggestedMatchingEnabled: boolean;
  onSuggestedMatchingEnabledChange: (enabled: boolean) => void;
  onRefresh: (forceRefresh?: boolean) => void;
  loading: boolean;
  lastRefreshed: Date | null;
  fromCache: { poly: boolean; kalshi: boolean };
}

export function FilterBar({
  filters,
  onFiltersChange,
  searchQuery,
  onSearchChange,
  suggestedMatchingEnabled,
  onSuggestedMatchingEnabledChange,
  onRefresh,
  loading,
  lastRefreshed,
  fromCache,
}: FilterBarProps) {
  function updateFilter<K extends keyof MarketFilters>(key: K, value: MarketFilters[K]) {
    onFiltersChange({ ...filters, [key]: value });
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex-1 min-w-[200px]">
          <input
            type="text"
            placeholder="Search markets by title..."
            value={searchQuery}
            onChange={(e) => onSearchChange(e.target.value)}
            className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>
        <label className="inline-flex items-center gap-2 text-sm text-gray-300 select-none">
          <input
            type="checkbox"
            checked={suggestedMatchingEnabled}
            onChange={(e) => onSuggestedMatchingEnabledChange(e.target.checked)}
            className="h-4 w-4 rounded border-gray-600 bg-gray-800 text-blue-500 focus:ring-blue-500"
          />
          Suggested matching
        </label>
        <button
          onClick={() => onRefresh(false)}
          disabled={loading}
          className="px-5 py-2 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500 text-white font-medium rounded-lg transition-colors flex items-center gap-2"
        >
          {loading ? (
            <>
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Loading...
            </>
          ) : (
            "Refresh"
          )}
        </button>
        <button
          onClick={() => onRefresh(true)}
          disabled={loading}
          className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:text-gray-600 text-gray-300 text-sm rounded-lg transition-colors"
          title="Force refresh (bypass cache)"
        >
          Force Refresh
        </button>
      </div>

      <div className="flex items-center gap-4 flex-wrap text-sm">
        <div className="flex items-center gap-2">
          <label className="text-gray-400 whitespace-nowrap">Closes after:</label>
          <input
            type="date"
            value={filters.endDateMin}
            onChange={(e) => updateFilter("endDateMin", e.target.value)}
            className="px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-gray-400 whitespace-nowrap">Closes before:</label>
          <input
            type="date"
            value={filters.endDateMax}
            onChange={(e) => updateFilter("endDateMax", e.target.value)}
            className="px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-gray-400 whitespace-nowrap">Min volume ($):</label>
          <input
            type="number"
            min="0"
            step="1000"
            value={filters.volumeMin || ""}
            onChange={(e) => updateFilter("volumeMin", Number(e.target.value) || 0)}
            placeholder="0"
            className="w-28 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-gray-400 whitespace-nowrap">Min liquidity ($):</label>
          <input
            type="number"
            min="0"
            step="100"
            value={filters.liquidityMin || ""}
            onChange={(e) => updateFilter("liquidityMin", Number(e.target.value) || 0)}
            placeholder="0"
            className="w-28 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-gray-400 whitespace-nowrap">Max pages:</label>
          <input
            type="number"
            min="1"
            step="10"
            value={filters.maxPages || ""}
            onChange={(e) => updateFilter("maxPages", Number(e.target.value) || 50)}
            placeholder="50"
            className="w-20 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
      </div>

      {lastRefreshed && (
        <div className="flex items-center gap-4 text-xs text-gray-500">
          <span>Last refreshed: {lastRefreshed.toLocaleTimeString()}</span>
          {(fromCache.poly || fromCache.kalshi) && (
            <span className="text-yellow-600">
              (from cache{fromCache.poly && fromCache.kalshi ? " — both" : fromCache.poly ? " — Poly" : " — Kalshi"})
            </span>
          )}
        </div>
      )}
    </div>
  );
}
