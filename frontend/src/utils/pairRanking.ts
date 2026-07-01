import type { AiPairReview, AiPairReviewMap, SuggestedCandidate, SuggestedMatch } from "../types";

export function reviewValidityScore(review: AiPairReview): number {
  if (review.status !== "reviewed" && review.status !== "heuristic") return -0.5;
  const statusDiscount = review.status === "heuristic" ? 0.35 : 0;
  if (review.verdict === "analog" || review.verdict === "inverse") return 2 + review.confidence - statusDiscount;
  if (review.verdict === "uncertain") return 0.5 + review.confidence * 0.5 - statusDiscount;
  return -review.confidence - statusDiscount;
}

export function pairRankingScore(review: AiPairReview | undefined, discoveryScore: number): number {
  if (!review) return discoveryScore * 0.25;
  return reviewValidityScore(review) + discoveryScore * 0.1;
}

export function bestReviewForMatch(
  match: SuggestedMatch | undefined,
  reviews: AiPairReviewMap,
): AiPairReview | null {
  if (!match) return null;
  const reviewed = match.candidates
    .map((candidate) => reviews[candidate.pairKey])
    .filter((review): review is AiPairReview => Boolean(review));
  if (!reviewed.length) return null;
  return reviewed.sort((a, b) => {
    const scoreDiff = reviewValidityScore(b) - reviewValidityScore(a);
    if (scoreDiff !== 0) return scoreDiff;
    return b.confidence - a.confidence;
  })[0] ?? null;
}

export function bestCandidateForMatch(
  match: SuggestedMatch | undefined,
  reviews: AiPairReviewMap,
): SuggestedCandidate | null {
  if (!match?.candidates.length) return null;
  return [...match.candidates].sort((a, b) => {
    const scoreDiff =
      pairRankingScore(reviews[b.pairKey], b.score) -
      pairRankingScore(reviews[a.pairKey], a.score);
    if (scoreDiff !== 0) return scoreDiff;
    return b.score - a.score;
  })[0] ?? null;
}

export function aiSortScore(match: SuggestedMatch | undefined, reviews: AiPairReviewMap): number {
  const candidate = bestCandidateForMatch(match, reviews);
  if (!candidate) return -1;
  return pairRankingScore(reviews[candidate.pairKey], candidate.score);
}

export function aiTone(review: AiPairReview): string {
  if (review.status === "unconfigured" || review.status === "error") return "text-gray-500";
  if (review.status === "heuristic" && (review.verdict === "analog" || review.verdict === "inverse")) return "text-cyan-300";
  if (review.status === "heuristic") return "text-blue-300";
  if (review.verdict === "analog" || review.verdict === "inverse") return "text-fuchsia-300";
  if (review.verdict === "uncertain") return "text-yellow-300";
  return "text-gray-500";
}
