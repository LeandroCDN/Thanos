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
  live_source?: "websocket" | "websocket-cache" | "rest-bootstrap" | "rest-fallback" | "rest";
  live_updated_at?: string;
  clobTokenIds?: string[];
  yes_token_id?: string;
  no_token_id?: string;
}

export interface SuggestedMatch {
  groupId: string;
  colorIndex: number;
  score: number;
  peerId: string;
  peerTitle: string;
  peerSource: Market["source"];
  sharedTerms: string[];
  candidates: SuggestedCandidate[];
}

export type MarketMatchMap = Record<string, SuggestedMatch>;

export interface SuggestedCandidate {
  pairKey: string;
  peerId: string;
  peerTitle: string;
  peerSource: Market["source"];
  score: number;
  tier: "strong" | "possible" | "weak" | "related";
  reasons: string[];
  sharedTerms: string[];
}

export interface AiPairReview {
  pairKey: string;
  polyId: string;
  kalshiId: string;
  status: "reviewed" | "heuristic" | "pending" | "unconfigured" | "error";
  verdict: "analog" | "inverse" | "related_not_analog" | "different" | "uncertain";
  confidence: number;
  yesMapping: "kalshi_yes" | "kalshi_no" | "unknown";
  preferred: boolean;
  summary: string;
  settlementAnalysis: string;
  riskFlags: string[];
  model?: string;
  reviewedAt?: string;
  error?: string;
}

export type AiPairReviewMap = Record<string, AiPairReview>;

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
  source?: "manual" | "bot";
  executionMode?: "paper" | "live";
  closeReason?: string;
}

export interface MarketFilters {
  endDateMin: string;
  endDateMax: string;
  volumeMin: number;
  liquidityMin: number;
  maxPages: number;
}

export type BotMode = "off" | "test" | "on";

export interface BotSettings {
  mode: BotMode;
  scan_interval_seconds: number;
  event_driven: boolean;
  event_debounce_ms: number;
  open_enabled: boolean;
  close_enabled: boolean;
  min_net_edge: number;
  max_leg_slippage: number;
  fixed_trade_dollars: number;
  max_total_capital: number;
  max_open_positions: number;
  min_hours_to_close: number;
  poly_fee: number;
  kalshi_fee: number;
  take_profit_enabled: boolean;
  take_profit_pct: number;
  stop_loss_enabled: boolean;
  stop_loss_pct: number;
  close_before_close_enabled: boolean;
  close_before_close_hours: number;
  edge_reversion_enabled: boolean;
  close_edge_below_pct: number;
  min_close_profit_pct: number;
  max_daily_loss: number;
  max_daily_trades: number;
  max_consecutive_failures: number;
  stop_on_api_error: boolean;
  market_data_max_age_ms: number;
  balance_cache_seconds: number;
  execution_strategy: "sequential" | "concurrent_fok";
  telegram_enabled: boolean;
  telegram_bot_token: string;
  telegram_chat_id: string;
  telegramConfigured?: boolean;
  notify_bot_status: boolean;
  notify_trade_opened: boolean;
  notify_trade_closed: boolean;
  notify_trade_failed: boolean;
  notify_circuit_breaker: boolean;
}

export interface BotLog {
  id: number;
  createdAt: string;
  level: "info" | "warning" | "error" | string;
  eventType: string;
  message: string;
  data: Record<string, unknown>;
}

export interface BotSummary {
  trigger?: string;
  mode?: BotMode;
  allowExecute?: boolean;
  pairsScanned?: number;
  eligibleCount?: number;
  rejectedCount?: number;
  openedCount?: number;
  closedCount?: number;
  errorCount?: number;
  openPositions?: number;
  rejections?: Array<{ id: string; code: string; message: string; data?: Record<string, unknown> }>;
  eligible?: Array<Record<string, unknown>>;
  executions?: Array<Record<string, unknown>>;
  errors?: Array<Record<string, unknown>>;
}

export interface BotStatus {
  running: boolean;
  scanning: boolean;
  warmupComplete: boolean;
  settings: BotSettings;
  lastScanAt?: string;
  nextScanAt?: string | null;
  lastSummary: BotSummary;
  recentLogs: BotLog[];
  dailyTradeCount: number;
  dailyRealizedPnl: number;
  consecutiveFailures: number;
  marketData?: Record<string, unknown>;
}
