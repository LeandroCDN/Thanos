import { useCallback, useEffect, useRef, useState } from "react";
import type { AiPairReviewMap, Market, SuggestedCandidate } from "../types";
import { getPairKey } from "../utils/marketMatcher";

const MAX_AUTO_REVIEW_CANDIDATES = 120;
const REQUEST_BATCH_SIZE = 12;

export interface PairReviewCandidate {
  pairKey: string;
  poly: Market;
  kalshi: Market;
  discovery: {
    score: number;
    tier: SuggestedCandidate["tier"];
    reasons: string[];
    sharedTerms: string[];
  };
}

export function usePairReviews(candidates: PairReviewCandidate[], enabled: boolean) {
  const [reviews, setReviews] = useState<AiPairReviewMap>({});
  const [loading, setLoading] = useState(false);
  const requested = useRef(new Set<string>());
  const activeRuns = useRef(0);

  const reviewPairs = useCallback(
    async (items: PairReviewCandidate[], force = false) => {
      const pending = items
        .filter((item) => item.poly && item.kalshi)
        .filter((item) => force || !requested.current.has(item.pairKey));
      if (!pending.length) return;

      activeRuns.current += 1;
      setLoading(true);
      try {
        for (let i = 0; i < pending.length; i += REQUEST_BATCH_SIZE) {
          const batch = pending.slice(i, i + REQUEST_BATCH_SIZE);
          const batchKeys = batch.map((item) => item.pairKey);
          if (!force) {
            batchKeys.forEach((key) => requested.current.add(key));
          }
          const res = await fetch("/api/ai/pair-reviews/batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pairs: batch, force }),
          });
          const data = await res.json().catch(() => null);
          if (!res.ok) {
            if (!force) {
              batchKeys.forEach((key) => requested.current.delete(key));
            }
            throw new Error(typeof data?.detail === "string" ? data.detail : "AI pair review failed");
          }
          const batchReviews: AiPairReviewMap = {};
          for (const review of data?.reviews ?? []) {
            batchReviews[review.pairKey] = review;
            if (force || !isRetryableReview(review)) {
              requested.current.add(review.pairKey);
            } else {
              requested.current.delete(review.pairKey);
            }
          }
          setReviews((prev) => ({ ...prev, ...batchReviews }));
        }
      } catch (err) {
        console.warn(err);
      } finally {
        activeRuns.current = Math.max(0, activeRuns.current - 1);
        if (activeRuns.current === 0) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!enabled || candidates.length === 0) return;
    void reviewPairs(candidates.slice(0, MAX_AUTO_REVIEW_CANDIDATES));
  }, [candidates, enabled, reviewPairs]);

  return { reviews, loading, reviewPairs };
}

function isRetryableReview(review: { status?: string; riskFlags?: string[]; confidence?: number }): boolean {
  if (review.status === "reviewed") return false;
  const flags = new Set(review.riskFlags ?? []);
  return (
    flags.has("ai_not_configured") ||
    flags.has("ai_insufficient_quota") ||
    flags.has("ai_rate_limited") ||
    flags.has("ai_provider_unavailable") ||
    flags.has("openai_insufficient_quota") ||
    flags.has("openai_rate_limited") ||
    flags.has("ai_review_failed") ||
    (review.status === "error" && (review.confidence ?? 0) <= 0)
  );
}

export function buildReviewCandidate(
  market: Market,
  peer: Market,
  candidate: SuggestedCandidate,
): PairReviewCandidate | null {
  const poly = market.source === "polymarket" ? market : peer.source === "polymarket" ? peer : null;
  const kalshi = market.source === "kalshi" ? market : peer.source === "kalshi" ? peer : null;
  if (!poly || !kalshi) return null;

  return {
    pairKey: getPairKey(poly.id, kalshi.id),
    poly,
    kalshi,
    discovery: {
      score: candidate.score,
      tier: candidate.tier,
      reasons: candidate.reasons,
      sharedTerms: candidate.sharedTerms,
    },
  };
}
