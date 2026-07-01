import { useEffect, useMemo } from "react";
import { buildReviewCandidate, type PairReviewCandidate } from "../hooks/usePairReviews";
import type { AiPairReview, AiPairReviewMap, Market, MarketMatchMap, SuggestedCandidate, WhitelistedPair } from "../types";
import { getMarketKey, getPairKey } from "../utils/marketMatcher";

const MODAL_AUTO_REVIEW_LIMIT = 12;

interface MarketModalProps {
  market: Market;
  allMarkets: Market[];
  suggestedMatches: MarketMatchMap;
  pairReviews: AiPairReviewMap;
  aiReviewsLoading: boolean;
  isInterested: boolean;
  onAddInterested: () => void;
  onRemoveInterested: () => void;
  onClose: () => void;
  onWhitelistPair?: (poly: Market, kalshi: Market) => void;
  whitelistedPairIds?: Set<string>;
  whitelistedPairs?: WhitelistedPair[];
  onReviewPairs?: (pairs: PairReviewCandidate[], force?: boolean) => Promise<void>;
}

interface CounterpartSuggestion {
  candidate: SuggestedCandidate;
  market: Market;
  review: AiPairReview | undefined;
  origin: "suggested" | "whitelisted";
}

export function MarketModal({
  market,
  allMarkets,
  suggestedMatches,
  pairReviews,
  aiReviewsLoading,
  isInterested,
  onAddInterested,
  onRemoveInterested,
  onClose,
  onWhitelistPair,
  whitelistedPairIds,
  whitelistedPairs = [],
  onReviewPairs,
}: MarketModalProps) {
  const key = getMarketKey(market);
  const match = suggestedMatches[key];
  const sourceBadgeClass =
    market.source === "polymarket"
      ? "bg-blue-900/40 text-blue-300"
      : "bg-green-900/40 text-green-300";

  const marketByKey = useMemo(() => {
    const map = new Map<string, Market>();
    for (const m of allMarkets) map.set(getMarketKey(m), m);
    return map;
  }, [allMarkets]);

  const whitelistedCounterparts = useMemo(() => {
    return whitelistedPairs
      .map((pair): CounterpartSuggestion | null => {
        const isSelectedPoly = market.source === "polymarket" && pair.polyId === market.id;
        const isSelectedKalshi = market.source === "kalshi" && pair.kalshiId === market.id;
        if (!isSelectedPoly && !isSelectedKalshi) return null;

        const peerSource = isSelectedPoly ? "kalshi" : "polymarket";
        const peerId = isSelectedPoly ? pair.kalshiId : pair.polyId;
        const peerTitle = isSelectedPoly ? pair.kalshiTitle : pair.polyTitle;
        const pairKey = pair.id || getPairKey(pair.polyId, pair.kalshiId);
        const peer = marketByKey.get(`${peerSource}:${peerId}`) ?? synthesizePeerMarket(peerSource, peerId, peerTitle);
        return {
          candidate: {
            pairKey,
            peerId,
            peerTitle,
            peerSource,
            score: 1,
            tier: "strong",
            reasons: ["already whitelisted"],
            sharedTerms: [],
          },
          market: peer,
          review: pairReviews[pairKey],
          origin: "whitelisted",
        };
      })
      .filter((item): item is CounterpartSuggestion => item !== null);
  }, [market.id, market.source, marketByKey, pairReviews, whitelistedPairs]);

  const counterparts = useMemo(() => {
    const whitelistedKeys = new Set(whitelistedCounterparts.map((item) => item.candidate.pairKey));
    const suggested = match
      ? match.candidates
        .filter((candidate) => !whitelistedKeys.has(candidate.pairKey))
        .map((candidate): CounterpartSuggestion | null => {
          const peer = marketByKey.get(`${candidate.peerSource}:${candidate.peerId}`);
          if (!peer) return null;
          const review = pairReviews[candidate.pairKey];
          return { candidate, market: peer, review, origin: "suggested" as const };
        })
        .filter((item): item is CounterpartSuggestion => item !== null)
        .sort((a, b) => candidateSortScore(b.review, b.candidate) - candidateSortScore(a.review, a.candidate))
      : [];
    return [...whitelistedCounterparts, ...suggested];
  }, [match, marketByKey, pairReviews, whitelistedCounterparts]);
  const preferredPairKey = useMemo(() => {
    const whitelisted = counterparts.find((item) => item.origin === "whitelisted");
    if (whitelisted) return whitelisted.candidate.pairKey;
    const reviewedValid = counterparts.find((item) => isValidAiPair(item.review));
    return (reviewedValid ?? counterparts[0])?.candidate.pairKey ?? null;
  }, [counterparts]);

  const reviewCandidates = useMemo(
    () =>
      counterparts
        .map((item) => buildReviewCandidate(market, item.market, item.candidate))
        .filter((item): item is PairReviewCandidate => Boolean(item)),
    [counterparts, market],
  );

  useEffect(() => {
    if (!onReviewPairs || reviewCandidates.length === 0) return;
    void onReviewPairs(reviewCandidates.slice(0, MODAL_AUTO_REVIEW_LIMIT));
  }, [onReviewPairs, reviewCandidates]);

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
          <span className={`inline-block text-[11px] uppercase tracking-wider font-semibold px-2 py-0.5 rounded mb-2 ${sourceBadgeClass}`}>
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
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-gray-300">
                Suggested Counterparts ({counterparts.length})
              </h3>
              {onReviewPairs && (
                <button
                  onClick={() => onReviewPairs(reviewCandidates, true)}
                  disabled={aiReviewsLoading || reviewCandidates.length === 0}
                  className="rounded bg-gray-800 px-2 py-1 text-[11px] font-semibold text-gray-300 hover:bg-gray-700 disabled:text-gray-600"
                >
                  {aiReviewsLoading ? "Reviewing..." : "Review AI"}
                </button>
              )}
            </div>
            <div className="space-y-2">
              {counterparts.map(({ candidate, market: cp, review, origin }) => {
                const poly = market.source === "polymarket" ? market : cp;
                const kalshi = market.source === "kalshi" ? market : cp;
                const edge = computeEdge(poly, kalshi);
                const pairId = `${poly.id}::${kalshi.id}`;
                const alreadyWhitelisted = whitelistedPairIds?.has(pairId) ?? false;
                const preferred = candidate.pairKey === preferredPairKey;
                const whitelisted = origin === "whitelisted";
                const sourceDescriptor = whitelisted
                  ? "already whitelisted"
                  : `broad ${candidate.tier} ${Math.round(candidate.score * 100)}%`;

                return (
                  <div
                    key={`${cp.source}:${cp.id}`}
                    className={`flex items-start justify-between gap-3 rounded-lg border px-3 py-2 text-sm ${
                      whitelisted
                        ? "border-green-700 bg-green-950/20"
                        : preferred
                          ? "border-fuchsia-600 bg-fuchsia-950/30"
                          : "border-gray-800 bg-gray-800"
                    }`}
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <div className="text-gray-200 truncate">{cp.title}</div>
                        {(preferred || whitelisted) && (
                          <span
                            className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold text-white ${
                              whitelisted ? "bg-green-700" : "bg-fuchsia-700"
                            }`}
                          >
                            {whitelisted ? "Whitelisted" : preferredBadgeLabel(review)}
                          </span>
                        )}
                      </div>
                      <div className={`mt-0.5 text-[11px] uppercase ${cp.source === "polymarket" ? "text-blue-400" : "text-green-400"}`}>
                        {cp.source} - {sourceDescriptor}
                      </div>
                      {!whitelisted && candidate.reasons.length > 0 && (
                        <div className="mt-0.5 text-[11px] text-gray-500">
                          {candidate.reasons.join(" - ")}
                        </div>
                      )}
                      {review && (
                        <div className={`mt-1 text-[11px] ${reviewTone(review)}`} title={review.settlementAnalysis}>
                          {reviewLabel(review)} {review.verdict.replace(/_/g, " ")} - {Math.round(review.confidence * 100)}% - {review.summary}
                        </div>
                      )}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <div className="text-right">
                        <div className="text-green-400 text-xs">Y: ${cp.yes_bid?.toFixed(2) ?? "-"} / ${cp.yes_ask?.toFixed(2) ?? "-"}</div>
                        <div className="text-red-400 text-xs">N: ${cp.no_bid?.toFixed(2) ?? "-"} / ${cp.no_ask?.toFixed(2) ?? "-"}</div>
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
                          {alreadyWhitelisted ? "SAVED" : "WHITELIST"}
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

function synthesizePeerMarket(source: Market["source"], id: string, title: string): Market {
  return {
    id,
    title,
    category: "",
    volume: 0,
    liquidity: 0,
    best_bid: 0,
    best_ask: 0,
    yes_bid: 0,
    yes_ask: 0,
    no_bid: 0,
    no_ask: 0,
    end_date: "",
    source,
    url: "",
    condition_id: id,
    rules: "",
    event_title: "",
    outcomes: ["Yes", "No"],
  };
}

function PriceCard({ label, bid, ask, color }: { label: string; bid: number; ask: number; color: string }) {
  return (
    <div className="rounded-lg bg-gray-800 p-3">
      <div className={`text-xs font-semibold uppercase tracking-wider mb-1 ${color}`}>{label}</div>
      <div className="flex items-baseline gap-3">
        <div>
          <span className="text-[11px] text-gray-500">Bid </span>
          <span className="font-mono text-gray-100">{bid > 0 ? `$${bid.toFixed(2)}` : "-"}</span>
        </div>
        <div>
          <span className="text-[11px] text-gray-500">Ask </span>
          <span className="font-mono text-gray-100">{ask > 0 ? `$${ask.toFixed(2)}` : "-"}</span>
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

function candidateSortScore(review: AiPairReview | undefined, candidate: SuggestedCandidate): number {
  if (!review) return candidate.score;
  const actionableReview = review.status === "reviewed" || review.status === "heuristic";
  if (!actionableReview) return candidate.score * 0.25;

  const statusWeight = review.status === "reviewed" ? 1 : 0.78;
  const validPairBoost = isValidAiPair(review) ? 3 * statusWeight : 0;
  const preferredBoost = review.preferred ? statusWeight : 0;
  const uncertaintyBoost = review.verdict === "uncertain" ? (1 + review.confidence * 0.5) * statusWeight : 0;
  const invalidPenalty = review.verdict === "different" || review.verdict === "related_not_analog" ? review.confidence : 0;
  return validPairBoost + preferredBoost + uncertaintyBoost + review.confidence * statusWeight + candidate.score * 0.25 - invalidPenalty;
}

function isValidAiPair(review: AiPairReview | undefined): boolean {
  return Boolean(
    review
      && (review.status === "reviewed" || review.status === "heuristic")
      && (review.verdict === "analog" || review.verdict === "inverse"),
  );
}

function preferredBadgeLabel(review: AiPairReview | undefined): string {
  if (isValidAiPair(review)) return review?.status === "heuristic" ? "Rule preferred" : "AI preferred";
  if (review?.status === "reviewed") return "Best reviewed";
  if (review?.status === "heuristic") return "Best rule";
  return "Top broad";
}

function reviewTone(review: AiPairReview): string {
  if (review.status === "unconfigured" || review.status === "error") return "text-gray-500";
  if (review.status === "heuristic" && (review.verdict === "analog" || review.verdict === "inverse")) return "text-cyan-300";
  if (review.status === "heuristic") return "text-blue-300";
  if (review.verdict === "analog" || review.verdict === "inverse") return "text-fuchsia-300";
  if (review.verdict === "uncertain") return "text-yellow-300";
  return "text-gray-500";
}

function reviewLabel(review: AiPairReview): string {
  if (review.status === "reviewed") return "AI";
  if (review.status === "heuristic") return "Rule";
  return "Review";
}

function formatVolume(vol: number): string {
  if (vol >= 1_000_000) return `$${(vol / 1_000_000).toFixed(1)}M`;
  if (vol >= 1_000) return `$${(vol / 1_000).toFixed(1)}K`;
  if (vol > 0) return `$${vol.toFixed(0)}`;
  return "-";
}

function formatDate(dateStr: string): string {
  if (!dateStr) return "-";
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
