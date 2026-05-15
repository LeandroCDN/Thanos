export interface Market {
  id: string;
  title: string;
  category: string;
  volume: number;
  liquidity: number;
  best_bid: number;
  best_ask: number;
  yes_bid: number;
  yes_ask: number;
  no_bid: number;
  no_ask: number;
  end_date: string;
  source: "polymarket" | "kalshi";
  url: string;
  condition_id: string;
  rules: string;
  event_title: string;
  outcomes: string[];
}

export interface SuggestedMatch {
  groupId: string;
  colorIndex: number;
  score: number;
  peerTitle: string;
  peerSource: Market["source"];
  sharedTerms: string[];
}

export type MarketMatchMap = Record<string, SuggestedMatch>;

export interface MarketsResponse {
  markets: Market[];
  count: number;
  from_cache: boolean;
  cache_age_seconds: number | null;
}

export interface MarketFilters {
  endDateMin: string;
  endDateMax: string;
  volumeMin: number;
  liquidityMin: number;
  maxPages: number;
}
