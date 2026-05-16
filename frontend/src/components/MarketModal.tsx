import type { Market, MarketMatchMap } from "../types";
import { getMarketKey } from "../utils/marketMatcher";

interface MarketModalProps {
  market: Market;
  allMarkets: Market[];
  suggestedMatches: MarketMatchMap;
  isInterested: boolean;
  onAddInterested: () => void;
  onRemoveInterested: () => void;
  onClose: () => void;
  onWhitelistPair?: (poly: Market, kalshi: Market) => void;
  whitelistedPairIds?: Set<string>;
}

export function MarketModal({
  market,
  allMarkets,
  suggestedMatches,
  isInterested,
  onAddInterested,
  onRemoveInterested,
  onClose,
  onWhitelistPair,
  whitelistedPairIds,
}: MarketModalProps) {
  const key = getMarketKey(market);
  const match = suggestedMatches[key];

  const counterparts = match
    ? allMarkets.filter((m) => {
        const mKey = getMarketKey(m);
        if (mKey === key) return false;
        const mMatch = suggestedMatches[mKey];
        return mMatch && mMatch.groupId === match.groupId;
      })
    : [];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-xl border border-gray-700 bg-gray-900 p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          className="absolute top-4 right-4 text-gray-400 hover:text-white text-xl leading-none"
        >
          &times;
        </button>

        <div className="mb-4">
          <span className="inline-block text-[11px] uppercase tracking-wider font-semibold px-2 py-0.5 rounded bg-gray-800 text-gray-400 mb-2">
            {market.source}
          </span>
          <h2 className="text-lg font-bold text-gray-100">{market.title}</h2>
          {market.category && (
            <span className="text-xs text-gray-500">{market.category}</span>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3 mb-5">
          <PriceCard label="YES" bid={market.yes_bid} ask={market.yes_ask} color="text-green-400" />
          <PriceCard label="NO" bid={market.no_bid} ask={market.no_ask} color="text-red-400" />
        </div>

        <div className="grid grid-cols-3 gap-3 mb-5 text-sm">
          <Stat label="Volume" value={formatVolume(market.volume)} />
          <Stat label="Liquidity" value={formatVolume(market.liquidity)} />
          <Stat label="Closes" value={formatDate(market.end_date)} />
        </div>

        {market.rules && (
          <div className="mb-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-1">Resolution Criteria</h3>
            <p className="text-sm text-gray-400 bg-gray-800 rounded-lg p-3 max-h-40 overflow-y-auto whitespace-pre-wrap">
              {market.rules}
            </p>
          </div>
        )}

        {counterparts.length > 0 && (
          <div className="mb-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-2">
              Suggested Counterparts ({counterparts.length})
            </h3>
            <div className="space-y-2">
              {counterparts.map((cp) => {
                const poly = market.source === "polymarket" ? market : cp;
                const kalshi = market.source === "kalshi" ? market : cp;
                const edge = computeEdge(poly, kalshi);
                const pairId = `${poly.id}::${kalshi.id}`;
                const alreadyWhitelisted = whitelistedPairIds?.has(pairId) ?? false;

                return (
                  <div
                    key={`${cp.source}:${cp.id}`}
                    className="flex items-center justify-between gap-3 rounded-lg bg-gray-800 px-3 py-2 text-sm"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="text-gray-200 truncate">{cp.title}</div>
                      <div className="text-[11px] text-gray-500 uppercase">{cp.source}</div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <div className="text-right">
                        <div className="text-green-400 text-xs">Y: ${cp.yes_bid?.toFixed(2) ?? "—"} / ${cp.yes_ask?.toFixed(2) ?? "—"}</div>
                        <div className="text-red-400 text-xs">N: ${cp.no_bid?.toFixed(2) ?? "—"} / ${cp.no_ask?.toFixed(2) ?? "—"}</div>
                      </div>
                      {edge !== null && (
                        <span
                          className={`text-xs font-mono font-bold px-2 py-1 rounded ${
                            edge > 0
                              ? "bg-green-900/50 text-green-300"
                              : edge < 0
                                ? "bg-red-900/50 text-red-300"
                                : "bg-gray-700 text-gray-400"
                          }`}
                        >
                          {edge > 0 ? "+" : ""}
                          {(edge * 100).toFixed(1)}%
                        </span>
                      )}
                      {onWhitelistPair && (
                        <button
                          onClick={() => onWhitelistPair(poly, kalshi)}
                          disabled={alreadyWhitelisted}
                          className={`text-xs font-semibold px-2 py-1 rounded transition-colors ${
                            alreadyWhitelisted
                              ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                              : "bg-blue-700 hover:bg-blue-600 text-white"
                          }`}
                          title={alreadyWhitelisted ? "Already whitelisted" : "Add to whitelist"}
                        >
                          {alreadyWhitelisted ? "✓" : "WHITELIST"}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        <div className="flex items-center gap-3">
          <button
            onClick={isInterested ? onRemoveInterested : onAddInterested}
            className={`flex-1 rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              isInterested
                ? "bg-amber-800/50 text-amber-200 hover:bg-amber-800/70"
                : "bg-blue-700 text-white hover:bg-blue-600"
            }`}
          >
            {isInterested ? "Remove from Interested" : "Add to Interested"}
          </button>
          <a
            href={market.url}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-lg border border-gray-600 px-4 py-2 text-sm text-gray-300 hover:bg-gray-800 transition-colors"
          >
            Open in {market.source === "polymarket" ? "Polymarket" : "Kalshi"}
          </a>
        </div>
      </div>
    </div>
  );
}

function PriceCard({ label, bid, ask, color }: { label: string; bid: number; ask: number; color: string }) {
  return (
    <div className="rounded-lg bg-gray-800 p-3">
      <div className={`text-xs font-semibold uppercase tracking-wider mb-1 ${color}`}>{label}</div>
      <div className="flex items-baseline gap-3">
        <div>
          <span className="text-[11px] text-gray-500">Bid </span>
          <span className="font-mono text-gray-100">{bid > 0 ? `$${bid.toFixed(2)}` : "—"}</span>
        </div>
        <div>
          <span className="text-[11px] text-gray-500">Ask </span>
          <span className="font-mono text-gray-100">{ask > 0 ? `$${ask.toFixed(2)}` : "—"}</span>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-gray-800 p-2 text-center">
      <div className="text-[11px] text-gray-500 uppercase">{label}</div>
      <div className="text-sm font-mono text-gray-200">{value}</div>
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

function computeEdge(poly: Market, kalshi: Market): number | null {
  const { polyFee, kalshiFee } = getFees();

  let edgeA: number | null = null;
  let edgeB: number | null = null;

  // Direction A: buy YES on Polymarket + buy NO on Kalshi
  if (poly.yes_ask > 0 && kalshi.no_ask > 0) {
    const polyEff   = poly.yes_ask * (1 + polyFee);
    const kalshiEff = kalshi.no_ask + kalshiFee * kalshi.no_ask * (1 - kalshi.no_ask);
    edgeA = 1 - polyEff - kalshiEff;
  }
  // Direction B: buy YES on Kalshi + buy NO on Polymarket
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
      year: "numeric",
    });
  } catch {
    return dateStr;
  }
}
