import { useEffect, useMemo, useState } from "react";
import type { Market, MarketMatchMap } from "../types";
import { getMarketKey } from "../utils/marketMatcher";

type SortField = "title" | "yes_bid" | "no_bid" | "volume" | "end_date" | "edge";
type SortDir = "asc" | "desc";

interface MarketTableProps {
  markets: Market[];
  allMarkets: Market[];
  loading: boolean;
  searchQuery: string;
  suggestedMatches: MarketMatchMap;
  onMarketClick: (market: Market) => void;
  isInterested: (id: string, source: Market["source"]) => boolean;
  selectedId?: string | null;
  onToggleSelect?: (market: Market) => void;
}

const INITIAL_VISIBLE_ROWS = 250;
const LOAD_MORE_STEP = 250;

interface IndexedRow {
  market: Market;
  key: string;
  searchableText: string;
}

const MATCH_COLORS = [
  {
    row: "bg-purple-950/25 hover:bg-purple-900/40 border-l-2 border-l-purple-500 border-b-purple-800/60",
    title: "text-purple-200",
    badge: "bg-purple-800/70 text-purple-100",
  },
  {
    row: "bg-cyan-950/25 hover:bg-cyan-900/40 border-l-2 border-l-cyan-500 border-b-cyan-800/60",
    title: "text-cyan-200",
    badge: "bg-cyan-800/70 text-cyan-100",
  },
  {
    row: "bg-amber-950/25 hover:bg-amber-900/40 border-l-2 border-l-amber-500 border-b-amber-800/60",
    title: "text-amber-200",
    badge: "bg-amber-800/70 text-amber-100",
  },
  {
    row: "bg-emerald-950/25 hover:bg-emerald-900/40 border-l-2 border-l-emerald-500 border-b-emerald-800/60",
    title: "text-emerald-200",
    badge: "bg-emerald-800/70 text-emerald-100",
  },
  {
    row: "bg-rose-950/25 hover:bg-rose-900/40 border-l-2 border-l-rose-500 border-b-rose-800/60",
    title: "text-rose-200",
    badge: "bg-rose-800/70 text-rose-100",
  },
  {
    row: "bg-indigo-950/25 hover:bg-indigo-900/40 border-l-2 border-l-indigo-500 border-b-indigo-800/60",
    title: "text-indigo-200",
    badge: "bg-indigo-800/70 text-indigo-100",
  },
  {
    row: "bg-lime-950/25 hover:bg-lime-900/40 border-l-2 border-l-lime-500 border-b-lime-800/60",
    title: "text-lime-200",
    badge: "bg-lime-800/70 text-lime-100",
  },
  {
    row: "bg-sky-950/25 hover:bg-sky-900/40 border-l-2 border-l-sky-500 border-b-sky-800/60",
    title: "text-sky-200",
    badge: "bg-sky-800/70 text-sky-100",
  },
];

export function MarketTable({ markets, allMarkets, loading, searchQuery, suggestedMatches, onMarketClick, isInterested, selectedId, onToggleSelect }: MarketTableProps) {
  const [sortField, setSortField] = useState<SortField>("volume");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [visibleRowsCount, setVisibleRowsCount] = useState(INITIAL_VISIBLE_ROWS);
  const [localSearch, setLocalSearch] = useState("");

  useEffect(() => {
    setVisibleRowsCount(INITIAL_VISIBLE_ROWS);
  }, [searchQuery, localSearch, sortField, sortDir, markets.length]);

  const indexedRows = useMemo<IndexedRow[]>(
    () =>
      markets.map((market) => ({
        market,
        key: getMarketKey(market),
        searchableText: `${market.title} ${market.category}`.toLowerCase(),
      })),
    [markets],
  );

  const globalWords = useMemo(
    () => searchQuery.toLowerCase().split(/\s+/).filter(Boolean),
    [searchQuery],
  );

  const localWords = useMemo(
    () => localSearch.toLowerCase().split(/\s+/).filter(Boolean),
    [localSearch],
  );

  const filtered = useMemo(() => {
    const allWords = [...globalWords, ...localWords];
    if (allWords.length === 0) return indexedRows;

    return indexedRows.filter((row) => {
      return allWords.every((word) => row.searchableText.includes(word));
    });
  }, [indexedRows, globalWords, localWords]);

  const peerById = useMemo(() => {
    const map = new Map<string, Market>();
    for (const m of allMarkets) map.set(`${m.source}:${m.id}`, m);
    return map;
  }, [allMarkets]);

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      // Edge sort: bypass grouping, rank all markets by arb edge desc
      if (sortField === "edge") {
        const aMatch = suggestedMatches[a.key];
        const bMatch = suggestedMatches[b.key];
        const aPeer = aMatch ? peerById.get(`${aMatch.peerSource}:${aMatch.peerId}`) : undefined;
        const bPeer = bMatch ? peerById.get(`${bMatch.peerSource}:${bMatch.peerId}`) : undefined;
        const edgeA = aPeer ? computeEdge(a.market, aPeer) ?? -Infinity : -Infinity;
        const edgeB = bPeer ? computeEdge(b.market, bPeer) ?? -Infinity : -Infinity;
        return sortDir === "asc" ? edgeA - edgeB : edgeB - edgeA;
      }

      // Default: matched markets float to top sorted by text similarity
      const aMatch = suggestedMatches[a.key];
      const bMatch = suggestedMatches[b.key];

      if (aMatch || bMatch) {
        if (!aMatch) return 1;
        if (!bMatch) return -1;
        const scoreCmp = bMatch.score - aMatch.score;
        if (scoreCmp !== 0) return scoreCmp;
        return aMatch.groupId.localeCompare(bMatch.groupId);
      }

      let cmp = 0;
      if (sortField === "title") {
        cmp = a.market.title.localeCompare(b.market.title);
      } else if (sortField === "end_date") {
        cmp = (a.market.end_date || "").localeCompare(b.market.end_date || "");
      } else {
        cmp = (a.market[sortField] as number) - (b.market[sortField] as number);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [filtered, sortField, sortDir, suggestedMatches, peerById]);

  const visibleRows = useMemo(
    () => sorted.slice(0, visibleRowsCount),
    [sorted, visibleRowsCount],
  );

  function handleSort(field: SortField) {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir("desc");
    }
  }

  function SortIcon({ field }: { field: SortField }) {
    if (sortField !== field) return <span className="text-gray-600 ml-1">↕</span>;
    return <span className="ml-1">{sortDir === "asc" ? "↑" : "↓"}</span>;
  }

  if (loading) {
    return (
      <div className="space-y-2 p-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="h-12 bg-gray-800 rounded animate-pulse" />
        ))}
      </div>
    );
  }

  if (sorted.length === 0) {
    return (
      <div className="p-8 text-center text-gray-500">
        No markets found
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <div className="px-3 py-2 border-b border-gray-800">
        <input
          type="text"
          placeholder="Filter this market..."
          value={localSearch}
          onChange={(e) => setLocalSearch(e.target.value)}
          className="w-full px-3 py-1.5 bg-gray-800 border border-gray-700 rounded text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
      </div>
      <table className="w-full text-sm table-fixed">
        <thead>
          <tr className="border-b border-gray-700 text-left">
            {onToggleSelect && <th className="pl-3 pr-1 py-2 w-[28px]" />}
            <th
              className="px-3 py-2 cursor-pointer hover:text-white w-[40%]"
              onClick={() => handleSort("title")}
            >
              Title <SortIcon field="title" />
            </th>
            <th
              className="px-3 py-2 cursor-pointer hover:text-white whitespace-nowrap w-[15%]"
              onClick={() => handleSort("yes_bid")}
            >
              YES <SortIcon field="yes_bid" />
            </th>
            <th
              className="px-3 py-2 cursor-pointer hover:text-white whitespace-nowrap w-[15%]"
              onClick={() => handleSort("no_bid")}
            >
              NO <SortIcon field="no_bid" />
            </th>
            <th
              className="px-3 py-2 cursor-pointer hover:text-white whitespace-nowrap w-[15%]"
              onClick={() => handleSort("volume")}
            >
              Volume <SortIcon field="volume" />
            </th>
            <th
              className="px-3 py-2 cursor-pointer hover:text-white whitespace-nowrap w-[15%]"
              onClick={() => handleSort("end_date")}
            >
              Closes <SortIcon field="end_date" />
            </th>
            <th
              className={`px-3 py-2 cursor-pointer whitespace-nowrap w-[10%] text-center transition-colors ${
                sortField === "edge" ? "text-yellow-300" : "text-gray-400 hover:text-white"
              }`}
              onClick={() => handleSort("edge")}
              title="Arb edge vs suggested counterpart (click to sort all markets by edge)"
            >
              Edge <SortIcon field="edge" />
            </th>
          </tr>
        </thead>
        <tbody>
          {visibleRows.map((row) => {
            const market = row.market;
            const match = suggestedMatches[row.key];
            const matchColor = match ? MATCH_COLORS[match.colorIndex] : null;
            const interested = isInterested(market.id, market.source);

            const isSelected = selectedId === market.id;

            return (
              <tr
                key={market.id}
                onClick={() => onMarketClick(market)}
                className={`h-12 border-b cursor-pointer transition-colors ${
                  isSelected
                    ? "bg-blue-900/30 border-blue-700"
                    : matchColor
                      ? matchColor.row
                      : "border-gray-800 hover:bg-gray-800/50"
                }`}
              >
                {onToggleSelect && (
                  <td
                    className="pl-3 pr-1 py-2"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => onToggleSelect(market)}
                      className="w-3.5 h-3.5 accent-blue-500 cursor-pointer"
                    />
                  </td>
                )}
                <td className="px-3 py-2 truncate">
                  <div className="flex items-center gap-1.5">
                    {interested && (
                      <span className="shrink-0 text-amber-400 text-xs">&#9733;</span>
                    )}
                    <span className={`truncate ${matchColor ? matchColor.title : "text-gray-100"}`}>
                      {market.title}
                    </span>
                    {match && (
                      <span className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded ${matchColor?.badge}`}>
                        {Math.round(match.score * 100)}%
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2 font-mono text-green-400 whitespace-nowrap">
                  {market.yes_bid > 0 ? `${fmtCent(market.yes_bid)}/${fmtCent(market.yes_ask)}` : "—"}
                </td>
                <td className="px-3 py-2 font-mono text-red-400 whitespace-nowrap">
                  {market.no_bid > 0 ? `${fmtCent(market.no_bid)}/${fmtCent(market.no_ask)}` : "—"}
                </td>
                <td className="px-3 py-2 font-mono whitespace-nowrap text-gray-300">
                  {formatVolume(market.volume)}
                </td>
                <td className="px-3 py-2 text-gray-400 whitespace-nowrap">
                  {formatDate(market.end_date)}
                </td>
                <td className="px-3 py-2 text-center">
                  {(() => {
                    const match = suggestedMatches[row.key];
                    const peer = match ? peerById.get(`${match.peerSource}:${match.peerId}`) : undefined;
                    const edge = peer ? computeEdge(market, peer) : null;
                    if (edge === null) return <span className="text-gray-700 text-xs">—</span>;
                    return (
                      <span className={`text-xs font-mono font-semibold ${
                        edge > 0.02 ? "text-green-400" : edge < -0.02 ? "text-red-400" : "text-gray-500"
                      }`}>
                        {edge > 0 ? "+" : ""}{(edge * 100).toFixed(1)}%
                      </span>
                    );
                  })()}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="px-3 py-2 text-xs text-gray-500 border-t border-gray-800">
        Showing {visibleRows.length} of {sorted.length} market{sorted.length !== 1 ? "s" : ""}
      </div>
      {visibleRows.length < sorted.length && (
        <div className="px-3 pb-3">
          <button
            type="button"
            onClick={() => setVisibleRowsCount((current) => current + LOAD_MORE_STEP)}
            className="w-full rounded bg-gray-800 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 transition-colors"
          >
            Load more ({Math.min(LOAD_MORE_STEP, sorted.length - visibleRows.length)} more)
          </button>
        </div>
      )}
    </div>
  );
}

function getFees(): { polyFee: number; kalshiFee: number } {
  try {
    return { polyFee: 0.02, kalshiFee: 0.07, ...JSON.parse(localStorage.getItem("thanos:fees") ?? "{}") };
  } catch {
    return { polyFee: 0.02, kalshiFee: 0.07 };
  }
}

/** Returns net-of-fee edge using entry-cost fee model. */
function computeEdge(a: Market, b: Market): number | null {
  const poly   = a.source === "polymarket" ? a : b;
  const kalshi = a.source === "kalshi"     ? a : b;
  const { polyFee, kalshiFee } = getFees();

  let edgeA: number | null = null;
  let edgeB: number | null = null;

  if (poly.yes_ask > 0 && kalshi.no_ask > 0) {
    const polyEff   = poly.yes_ask * (1 + polyFee);
    const kalshiEff = kalshi.no_ask + kalshiFee * kalshi.no_ask * (1 - kalshi.no_ask);
    edgeA = 1 - polyEff - kalshiEff;
  }
  if (kalshi.yes_ask > 0 && poly.no_ask > 0) {
    const kalshiEff = kalshi.yes_ask + kalshiFee * kalshi.yes_ask * (1 - kalshi.yes_ask);
    const polyEff   = poly.no_ask * (1 + polyFee);
    edgeB = 1 - kalshiEff - polyEff;
  }

  if (edgeA === null && edgeB === null) return null;
  if (edgeA === null) return edgeB;
  if (edgeB === null) return edgeA;
  return Math.max(edgeA, edgeB);
}

function fmtCent(value: number): string {
  return `${Math.round(value * 100)}¢`;
}

function formatVolume(vol: number): string {
  if (vol >= 1_000_000) return `$${(vol / 1_000_000).toFixed(1)}M`;
  if (vol >= 1_000) return `$${(vol / 1_000).toFixed(1)}K`;
  if (vol > 0) return `$${vol.toFixed(0)}`;
  return "—";
}

function formatDate(dateStr: string): string {
  if (!dateStr) return "—";
  try {
    return new Date(dateStr).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}
