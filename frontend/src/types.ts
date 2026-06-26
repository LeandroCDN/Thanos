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
  peerId: string;
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

export interface WhitelistedPair {
  id: string;
  polyId: string;
  polyTitle: string;
  kalshiId: string;
  kalshiTitle: string;
  addedAt: string;
}

export interface ArbitragePosition {
  id: string;
  addedAt: string;
  // Markets
  polyId: string;
  polyTitle: string;
  polyEndDate: string;
  kalshiId: string;
  kalshiTitle: string;
  kalshiEndDate: string;
  // Trade structure
  direction: "A" | "B";
  polyAction: "YES" | "NO";
  kalshiAction: "YES" | "NO";
  // Entry prices (per contract)
  polyEntryAsk: number;
  kalshiEntryAsk: number;
  // Position size
  contracts: number;
  polyCapital: number;   // incl. entry-side fees
  kalshiCapital: number;
  totalCapital: number;
  // Fee breakdown at entry
  polyFeesPaid: number;
  kalshiFeesPaid: number;
  totalFeesPaid: number;
  // Expected P&L at resolution
  lockedProfit: number;
  lockedProfitPct: number;
  // Status
  status: "open" | "closed";
  closedAt?: string;
  realizedPnl?: number;
}

export interface MarketFilters {
  endDateMin: string;
  endDateMax: string;
  volumeMin: number;
  liquidityMin: number;
  maxPages: number;
}
