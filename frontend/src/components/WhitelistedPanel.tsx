import { useMemo, useState } from "react";
import type { ArbitragePosition, Market, WhitelistedPair } from "../types";

interface DepthResult {
  ideal_bet: number;
  best_direction: "A" | "B";
  edge_threshold_used: number;
  poly_yes_levels: number;
  poly_no_levels: number;
  kalshi_yes_levels: number;
  kalshi_no_levels: number;
  fee_adjusted?: boolean;
  A?: DepthSide;
  B?: DepthSide;
}

interface DepthSide {
  ideal_bet: number;
  initial_edge?: number;
  effective_edge?: number;
  initial_net_edge?: number;
  effective_net_edge?: number;
  slippage?: number;
  max_shares?: number;
  reason?: string;
}

type SortField = "polymarket" | "kalshi" | "edge" | "closes" | "addedAt";
type SortDir = "asc" | "desc";

interface FeeConfig {
  /** Polymarket taker fee: charged as a % of the dollar value of each trade.
   *  e.g. 0.02 → you pay 2¢ extra per $1 of contracts bought. */
  polyFee: number;
  /** Kalshi taker fee: charged as fee_rate × P × (1-P) per contract.
   *  e.g. 0.07 → max 1.75¢/contract at 50¢; 1.3¢ at 25¢; 0.63¢ at 10¢.
   *  Maker (limit order) rate is 0.0175. S&P/Nasdaq markets use 0.035. */
  kalshiFee: number;
}

const DEFAULT_FEES: FeeConfig = { polyFee: 0.02, kalshiFee: 0.07 };
const FEE_STORAGE_KEY = "thanos:fees";
const POLY_MIN_MARKET_BUY_DOLLARS = 1;
const EXPECTED_EXECUTION_VERSION = "open-close-v10-live-pair-claim";

function loadFees(): FeeConfig {
  try {
    const raw = localStorage.getItem(FEE_STORAGE_KEY);
    if (raw) return { ...DEFAULT_FEES, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return DEFAULT_FEES;
}

interface TradeCalc {
  direction: "A" | "B";
  polyAction: "YES" | "NO";
  kalshiAction: "YES" | "NO";
  polyAsk: number;
  kalshiAsk: number;
  contracts: number;        // units purchased
  polySpend: number;        // includes Poly taker fee
  kalshiSpend: number;      // includes Kalshi at-risk fee
  totalCost: number;        // betSize (polySpend + kalshiSpend)
  polyFeesPaid: number;     // dollars paid to Polymarket as fees
  kalshiFeesPaid: number;   // dollars paid to Kalshi as fees
  totalFees: number;
  grossProfit: number;      // profit in a fee-free world (raw market edge)
  grossProfitPct: number;
  netProfit: number;        // profit after all entry-side fees (deterministic)
  netProfitPct: number;
}

interface TradeModal {
  pair: WhitelistedPair;
  poly: Market;
  kalshi: Market;
  calc: TradeCalc;
  mode: "open" | "close";
}

interface WhitelistedPanelProps {
  pairs: WhitelistedPair[];
  allMarkets: Market[];
  liveMarkets?: Market[];
  selectedPoly: Market | null;
  selectedKalshi: Market | null;
  onWhitelist: () => void;
  onRemovePair: (pairId: string) => void;
  onRefreshPairs: () => void;
  pairsRefreshing?: boolean;
  pairsStreamConnected?: boolean;
  onRecordPosition?: (pos: Omit<ArbitragePosition, "id" | "addedAt" | "status">) => void;
}

export function WhitelistedPanel({
  pairs,
  allMarkets,
  liveMarkets = [],
  selectedPoly,
  selectedKalshi,
  onWhitelist,
  onRemovePair,
  onRefreshPairs,
  pairsRefreshing = false,
  pairsStreamConnected = false,
  onRecordPosition,
}: WhitelistedPanelProps) {
  const canWhitelist = selectedPoly !== null && selectedKalshi !== null;
  const [sortField, setSortField] = useState<SortField>("addedAt");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [betSizes, setBetSizes] = useState<Record<string, string>>({});
  const [tradeModal, setTradeModal] = useState<TradeModal | null>(null);
  const [depthResults, setDepthResults] = useState<Record<string, DepthResult>>({});
  const [depthScanning, setDepthScanning] = useState<Record<string, boolean>>({});
  const [fees] = useState<FeeConfig>(loadFees);

  // liveMarkets take priority over allMarkets for price display
  const liveById = useMemo(() => {
    const map = new Map<string, Market>();
    for (const m of liveMarkets) map.set(`${m.source}:${m.id}`, m);
    return map;
  }, [liveMarkets]);

  const marketById = (id: string, source: "polymarket" | "kalshi"): Market | undefined =>
    liveById.get(`${source}:${id}`) ?? allMarkets.find((m) => m.id === id && m.source === source);

  function handleSort(field: SortField) {
    if (sortField === field) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(field === "edge" ? "desc" : "asc");
    }
  }

  function SortIcon({ field }: { field: SortField }) {
    if (sortField !== field) return <span className="text-gray-600 ml-0.5">↕</span>;
    return <span className="ml-0.5">{sortDir === "asc" ? "↑" : "↓"}</span>;
  }

  function closeSortDate(poly?: Market, kalshi?: Market) {
    const dates = [poly?.end_date, kalshi?.end_date].filter(Boolean) as string[];
    return dates.sort()[0] ?? "";
  }

  const sortedPairs = useMemo(() => {
    return [...pairs].sort((a, b) => {
      const polyA = marketById(a.polyId, "polymarket");
      const polyB = marketById(b.polyId, "polymarket");
      const kalshiA = marketById(a.kalshiId, "kalshi");
      const kalshiB = marketById(b.kalshiId, "kalshi");

      let cmp = 0;
      if (sortField === "polymarket") {
        cmp = a.polyTitle.localeCompare(b.polyTitle);
      } else if (sortField === "kalshi") {
        cmp = a.kalshiTitle.localeCompare(b.kalshiTitle);
      } else if (sortField === "edge") {
        const edgeA = polyA && kalshiA ? computeEdge(polyA, kalshiA, fees) : null;
        const edgeB = polyB && kalshiB ? computeEdge(polyB, kalshiB, fees) : null;
        cmp = (edgeA ?? -Infinity) - (edgeB ?? -Infinity);
      } else if (sortField === "closes") {
        const dateA = closeSortDate(polyA, kalshiA);
        const dateB = closeSortDate(polyB, kalshiB);
        cmp = dateA.localeCompare(dateB);
      } else {
        cmp = a.addedAt.localeCompare(b.addedAt);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [pairs, sortField, sortDir, allMarkets, liveById]);

  async function handleScanDepth(pair: WhitelistedPair) {
    setDepthScanning((prev) => ({ ...prev, [pair.id]: true }));
    try {
      // Require a small positive net edge after venue-specific fees.
      const minNetEdge = 0.005;
      const res = await fetch(
        `/api/markets/depth?poly_id=${encodeURIComponent(pair.polyId)}&kalshi_id=${encodeURIComponent(pair.kalshiId)}&edge_threshold=${minNetEdge.toFixed(4)}&poly_fee=${fees.polyFee.toFixed(4)}&kalshi_fee=${fees.kalshiFee.toFixed(4)}`,
      );
      if (res.ok) {
        const data: DepthResult = await res.json();
        setDepthResults((prev) => ({ ...prev, [pair.id]: data }));
        // Pre-fill bet size with ideal bet if not already set
        if (data.ideal_bet > 0 && !betSizes[pair.id]) {
          setBetSizes((prev) => ({ ...prev, [pair.id]: String(Math.floor(data.ideal_bet)) }));
        }
      }
    } finally {
      setDepthScanning((prev) => ({ ...prev, [pair.id]: false }));
    }
  }

  function handleOpenTrade(pair: WhitelistedPair, poly: Market, kalshi: Market) {
    const betRaw = parseFloat(betSizes[pair.id] ?? "");
    if (!betRaw || betRaw <= 0) return;
    const calc = calcTrade(poly, kalshi, betRaw, fees);
    if (!calc) return;
    setTradeModal({ pair, poly, kalshi, calc, mode: "open" });
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold text-white">Whitelisted Pairs</h2>
          <span className="text-xs text-gray-500">{pairs.length} pair{pairs.length !== 1 ? "s" : ""}</span>
          {(selectedPoly || selectedKalshi) && (
            <div className="flex items-center gap-1.5 text-xs text-gray-400">
              <span className="text-gray-600">|</span>
              {selectedPoly ? (
                <span className="text-blue-400 truncate max-w-[180px]" title={selectedPoly.title}>
                  ✓ {selectedPoly.title.slice(0, 30)}{selectedPoly.title.length > 30 ? "…" : ""}
                </span>
              ) : (
                <span className="text-gray-600 italic">select Polymarket…</span>
              )}
              <span className="text-gray-600">+</span>
              {selectedKalshi ? (
                <span className="text-green-400 truncate max-w-[180px]" title={selectedKalshi.title}>
                  ✓ {selectedKalshi.title.slice(0, 30)}{selectedKalshi.title.length > 30 ? "…" : ""}
                </span>
              ) : (
                <span className="text-gray-600 italic">select Kalshi…</span>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {pairs.length > 0 && (
            <span
              className={`rounded border px-2 py-1 text-[10px] font-bold ${
                pairsStreamConnected
                  ? "border-green-800 bg-green-950/30 text-green-300"
                  : "border-gray-700 bg-gray-800/50 text-gray-500"
              }`}
              title={pairsStreamConnected ? "Whitelisted prices are receiving WebSocket snapshots" : "WebSocket stream disconnected; refresh uses REST fallback"}
            >
              WS
            </span>
          )}
          <button
            onClick={onRefreshPairs}
            disabled={pairsRefreshing || pairs.length === 0}
            title="Refresh whitelisted pairs prices"
            className={`px-3 py-1.5 rounded text-sm font-semibold transition-colors flex items-center gap-1.5 ${
              pairsRefreshing || pairs.length === 0
                ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                : "bg-gray-700 hover:bg-gray-600 text-gray-300"
            }`}
          >
            <span className={`inline-block text-base leading-none ${pairsRefreshing ? "animate-spin" : ""}`}>
              ↻
            </span>
            {pairsRefreshing ? "Refreshing…" : "Refresh"}
          </button>
          <button
            onClick={onWhitelist}
            disabled={!canWhitelist}
            className={`px-4 py-1.5 rounded text-sm font-semibold transition-colors ${
              canWhitelist
                ? "bg-blue-600 hover:bg-blue-500 text-white"
                : "bg-gray-700 text-gray-500 cursor-not-allowed"
            }`}
          >
            WHITELIST
          </button>
        </div>
      </div>

      {pairs.length === 0 ? (
        <div className="h-[180px] flex items-center justify-center text-gray-600 text-sm">
          No whitelisted pairs yet — tick one Polymarket and one Kalshi market, then click WHITELIST
        </div>
      ) : (
        <div className="overflow-y-auto overflow-x-auto" style={{ maxHeight: "calc(5 * 72px)" }}>
          <table className="w-full text-sm" style={{ minWidth: "900px" }}>
            <thead className="sticky top-0 bg-gray-900 z-10">
              <tr className="border-b border-gray-700 text-left text-xs uppercase tracking-wide">
                {(["polymarket", "kalshi", "edge", "closes"] as SortField[]).map((field) => (
                  <th
                    key={field}
                    onClick={() => handleSort(field)}
                    className={`px-3 py-2 cursor-pointer select-none hover:text-white transition-colors ${
                      sortField === field ? "text-gray-200" : "text-gray-500"
                    } ${field === "edge" ? "w-[8%] text-center" : field === "closes" ? "w-[12%] text-right" : "w-[22%]"}`}
                  >
                    {field.charAt(0).toUpperCase() + field.slice(1)}
                    <SortIcon field={field} />
                  </th>
                ))}
                <th className="px-3 py-2 text-gray-500 w-[13%] text-center whitespace-nowrap" title="Scan order book depth to find ideal position size">Ideal / Bet</th>
                <th className="px-2 py-2 w-[6%]" />
                <th className="px-2 py-2 w-[4%]" />
              </tr>
            </thead>
            <tbody>
              {sortedPairs.map((pair) => {
                const poly = marketById(pair.polyId, "polymarket");
                const kalshi = marketById(pair.kalshiId, "kalshi");
                const edgeGross = poly && kalshi ? computeEdge(poly, kalshi, null) : null;
                const edgeNet = poly && kalshi ? computeEdge(poly, kalshi, fees) : null;
                const edgeUnavailableReason = !poly
                  ? "Polymarket live data unavailable for this whitelisted ID"
                  : !kalshi
                    ? "Kalshi live data unavailable for this whitelisted ticker"
                    : "One or both venues have no usable ask prices";
                const polyEndDate = poly?.end_date ?? "";
                const kalshiEndDate = kalshi?.end_date ?? "";
                const betRaw = parseFloat(betSizes[pair.id] ?? "");
                const betValid = betRaw > 0;
                const calc = betValid && poly && kalshi ? calcTrade(poly, kalshi, betRaw, fees) : null;
                const hasEdge = edgeNet !== null && edgeNet > 0;
                const canOpen = betValid && calc !== null && hasEdge;
                const depthResult = depthResults[pair.id];

                return (
                  <tr key={pair.id} className="border-b border-gray-800 hover:bg-gray-800/40 transition-colors" style={{ height: "72px" }}>
                    {/* Polymarket */}
                    <td className="px-3 py-2">
                      <div className="truncate text-blue-300 text-xs font-medium" title={pair.polyTitle}>
                        {pair.polyTitle}
                      </div>
                      {poly && (
                        <div className="text-[11px] text-gray-500 font-mono mt-0.5">
                          Y: {poly.yes_bid > 0 ? `${fmtCent(poly.yes_bid)}/${fmtCent(poly.yes_ask)}` : "—"}
                          &nbsp;·&nbsp;
                          N: {poly.no_bid > 0 ? `${fmtCent(poly.no_bid)}/${fmtCent(poly.no_ask)}` : "—"}
                        </div>
                      )}
                      {calc && (
                        <div className="text-[10px] text-gray-600 mt-0.5">
                          → Buy {calc.polyAction} · ${calc.polySpend.toFixed(2)}
                        </div>
                      )}
                    </td>

                    {/* Kalshi */}
                    <td className="px-3 py-2">
                      <div className="truncate text-green-300 text-xs font-medium" title={pair.kalshiTitle}>
                        {pair.kalshiTitle}
                      </div>
                      {kalshi && (
                        <div className="text-[11px] text-gray-500 font-mono mt-0.5">
                          Y: {kalshi.yes_bid > 0 ? `${fmtCent(kalshi.yes_bid)}/${fmtCent(kalshi.yes_ask)}` : "—"}
                          &nbsp;·&nbsp;
                          N: {kalshi.no_bid > 0 ? `${fmtCent(kalshi.no_bid)}/${fmtCent(kalshi.no_ask)}` : "—"}
                        </div>
                      )}
                      {calc && (
                        <div className="text-[10px] text-gray-600 mt-0.5">
                          → {calc.kalshiAction === "YES" ? "Buy YES" : "Buy NO"} · ${calc.kalshiSpend.toFixed(2)}
                        </div>
                      )}
                    </td>

                    {/* Edge */}
                    <td className="px-3 py-2 text-center">
                      {edgeNet !== null ? (
                        <span
                          className={`text-xs font-mono font-bold px-2 py-0.5 rounded ${
                            edgeNet > 0.02
                              ? "bg-green-900/50 text-green-300"
                              : edgeNet < -0.02
                                ? "bg-red-900/50 text-red-300"
                                : "bg-gray-700 text-gray-400"
                          }`}
                          title={`Gross: ${edgeGross !== null ? (edgeGross > 0 ? "+" : "") + (edgeGross * 100).toFixed(1) + "%" : "—"} · Net after fees: ${edgeNet > 0 ? "+" : ""}${(edgeNet * 100).toFixed(1)}%`}
                        >
                          {edgeNet > 0 ? "+" : ""}{(edgeNet * 100).toFixed(1)}%
                        </span>
                      ) : (
                        <span className="text-gray-600 text-xs cursor-help" title={edgeUnavailableReason}>—</span>
                      )}
                      {edgeGross !== null && edgeNet !== null && (
                        <div className="text-[10px] text-gray-600 mt-0.5 font-mono">
                          gross {edgeGross > 0 ? "+" : ""}{(edgeGross * 100).toFixed(1)}%
                        </div>
                      )}
                      {calc && (
                        <div className={`text-[10px] mt-0.5 font-mono ${calc.netProfit > 0 ? "text-green-500" : "text-red-400"}`}>
                          {calc.netProfit >= 0 ? "+" : ""}${calc.netProfit.toFixed(2)}
                        </div>
                      )}
                    </td>

                    {/* Closes */}
                    <td className="px-3 py-2 text-right text-[11px] text-gray-400 whitespace-nowrap">
                      <div className="flex flex-col gap-0.5 leading-tight">
                        <div title={`Polymarket closes ${formatDate(polyEndDate)}`}>
                          <span className="text-blue-500/80 mr-1">Poly</span>
                          <span>{formatDate(polyEndDate)}</span>
                        </div>
                        <div title={`Kalshi closes ${formatDate(kalshiEndDate)}`}>
                          <span className="text-green-500/80 mr-1">Kalshi</span>
                          <span>{formatDate(kalshiEndDate)}</span>
                        </div>
                      </div>
                    </td>

                    {/* Ideal / Bet Size */}
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2 whitespace-nowrap">
                        <div className="flex items-center gap-1 min-w-0 flex-1">
                          <span className="text-gray-500 text-xs shrink-0">$</span>
                          <input
                            type="number"
                            min="0"
                            step="10"
                            placeholder="0"
                            value={betSizes[pair.id] ?? ""}
                            onChange={(e) =>
                              setBetSizes((prev) => ({ ...prev, [pair.id]: e.target.value }))
                            }
                            className="w-full min-w-[90px] px-2 py-1 bg-gray-800 border border-gray-700 rounded text-xs text-right text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                          />
                        </div>
                        <button
                          onClick={() => handleScanDepth(pair)}
                          disabled={depthScanning[pair.id]}
                          title="Scan order book depth to find ideal bet size"
                          className={`flex shrink-0 items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold transition-colors ${
                            depthScanning[pair.id]
                              ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                              : "bg-gray-700 hover:bg-gray-600 text-gray-400 hover:text-gray-200"
                          }`}
                        >
                          <span className={depthScanning[pair.id] ? "animate-spin inline-block" : ""}>⚡</span>
                          Scan
                        </button>
                        {depthResult && (
                          <DepthBadge result={depthResult} />
                        )}
                      </div>
                    </td>

                    {/* OPEN */}
                    <td className="px-2 py-2 text-center">
                      <button
                        onClick={() => poly && kalshi && handleOpenTrade(pair, poly, kalshi)}
                        disabled={!canOpen}
                        title={!poly || !kalshi ? edgeUnavailableReason : !betValid ? "Enter a bet size first" : !hasEdge ? "No positive edge" : "Preview trade"}
                        className={`px-2 py-1 rounded text-[11px] font-bold tracking-wide transition-colors whitespace-nowrap ${
                          canOpen
                            ? "bg-green-700 hover:bg-green-600 text-white"
                            : "bg-gray-800 text-gray-600 cursor-not-allowed"
                        }`}
                      >
                        OPEN
                      </button>
                    </td>

                    {/* Remove */}
                    <td className="px-1 py-2 text-center">
                      <button
                        onClick={() => onRemovePair(pair.id)}
                        aria-label="Remove pair"
                        className="inline-flex h-8 w-8 items-center justify-center rounded text-xl leading-none text-gray-500 transition-colors hover:bg-red-950/50 hover:text-red-300 focus:outline-none focus:ring-1 focus:ring-red-500"
                        title="Remove pair"
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Trade preview modal */}
      {tradeModal && (
        <TradePreviewModal
          modal={tradeModal}
          fees={fees}
          onClose={() => setTradeModal(null)}
          onRecordPosition={onRecordPosition}
        />
      )}
    </div>
  );
}

// ─── Depth Badge ─────────────────────────────────────────────────────────────

function DepthBadge({ result }: { result: DepthResult }) {
  const best = result.best_direction === "A" ? result.A : result.B;
  const ideal = result.ideal_bet;

  if (ideal <= 0) {
    const label = depthFailureLabel(result);
    const tooltip = depthFailureTooltip(result);
    return (
      <span className="text-[10px] text-red-400 font-mono" title={tooltip}>
        {label}
      </span>
    );
  }

  const slippage = best?.slippage ?? 0;
  const edge = result.fee_adjusted ? best?.effective_net_edge : best?.effective_edge;
  const levels = result.best_direction === "A"
    ? Math.min(result.poly_yes_levels, result.kalshi_no_levels)
    : Math.min(result.kalshi_yes_levels, result.poly_no_levels);

  const color = ideal >= 5000 ? "text-green-400" : ideal >= 1000 ? "text-yellow-400" : "text-orange-400";
  const label = ideal >= 1_000_000
    ? `$${(ideal / 1_000_000).toFixed(1)}M`
    : ideal >= 1_000
      ? `$${(ideal / 1_000).toFixed(1)}K`
      : `$${ideal.toFixed(0)}`;

  const tooltip = [
    `Ideal bet: ${label}`,
    edge !== undefined ? `${result.fee_adjusted ? "Net" : "Gross"} edge at max: ${(edge * 100).toFixed(2)}%` : "",
    `Slippage at max: ${(slippage * 100).toFixed(2)}%`,
    `Shares: ${best?.max_shares?.toFixed(0) ?? "?"}`,
    `Order book levels: ${levels}`,
    `Direction: ${result.best_direction === "A" ? "YES Poly + NO Kalshi" : "YES Kalshi + NO Poly"}`,
  ].filter(Boolean).join("\n");

  return (
    <span className={`text-[10px] font-mono font-semibold ${color}`} title={tooltip}>
      {label}
    </span>
  );
}

function depthFailureLabel(result: DepthResult): string {
  const reasons = [result.A?.reason, result.B?.reason].filter(Boolean).join(" | ").toLowerCase();
  if (reasons.includes("missing prices")) return "missing";
  if (reasons.includes("no net edge") || reasons.includes("no edge")) return "no edge";
  if (reasons.includes("insufficient order book depth")) return "shallow";
  return "no depth";
}

function depthFailureTooltip(result: DepthResult): string {
  const aReason = result.A?.reason ?? "unavailable";
  const bReason = result.B?.reason ?? "unavailable";
  return [
    "No executable depth found.",
    `A: ${aReason}`,
    `B: ${bReason}`,
    `Levels Y/N: Poly ${result.poly_yes_levels}/${result.poly_no_levels}, Kalshi ${result.kalshi_yes_levels}/${result.kalshi_no_levels}`,
    result.fee_adjusted ? "Threshold is net of configured fees." : "Threshold is gross edge.",
  ].join("\n");
}


// ─── Trade Preview Modal ────────────────────────────────────────────────────

function TradePreviewModal({
  modal,
  fees,
  onClose,
  onRecordPosition,
}: {
  modal: TradeModal;
  fees: FeeConfig;
  onClose: () => void;
  onRecordPosition?: (pos: Omit<ArbitragePosition, "id" | "addedAt" | "status">) => void;
}) {
  const { pair, poly, kalshi, calc, mode } = modal;
  const [executing, setExecuting] = useState(false);
  const [executionError, setExecutionError] = useState<string | null>(null);
  const executableContracts = Math.floor(calc.contracts);
  const executionCalc = executableContracts > 0
    ? calcTradeForContracts(calc.direction, calc.polyAsk, calc.kalshiAsk, executableContracts, fees)
    : null;
  const displayCalc = executionCalc ?? calc;
  const minPolyContracts = calc.polyAsk > 0 ? Math.ceil(POLY_MIN_MARKET_BUY_DOLLARS / calc.polyAsk) : Infinity;
  const minTotalCost = Number.isFinite(minPolyContracts)
    ? minPolyContracts * (
      calc.polyAsk * (1 + fees.polyFee) +
      calc.kalshiAsk +
      fees.kalshiFee * calc.kalshiAsk * (1 - calc.kalshiAsk)
    )
    : Infinity;
  const sizeRuleMessage = executableContracts < minPolyContracts
    ? `Minimum for this pair is ${minPolyContracts} contracts, about ${fmtDollar(minTotalCost)} total at current prices.`
    : null;
  const canExecuteLiveOpen = mode === "open" && !executing && !sizeRuleMessage && !!executionCalc && !!onRecordPosition;

  async function executeOpenTrade() {
    if (sizeRuleMessage) {
      setExecutionError(sizeRuleMessage);
      return;
    }
    setExecuting(true);
    setExecutionError(null);
    try {
      if (!onRecordPosition) {
        throw new Error("Position recording is unavailable, so live open is disabled.");
      }
      const statusRes = await fetch("/api/trades/status");
      const status = await statusRes.json().catch(() => null);
      if (!statusRes.ok || status?.execution_version !== EXPECTED_EXECUTION_VERSION) {
        throw new Error(
          `Backend trading code is stale. Restart the backend; expected ${EXPECTED_EXECUTION_VERSION}.`,
        );
      }
      const res = await fetch("/api/trades/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          poly_id: pair.polyId,
          kalshi_id: pair.kalshiId,
          poly_action: calc.polyAction,
          kalshi_action: calc.kalshiAction,
          contracts: executableContracts,
          max_poly_price: calc.polyAsk,
          max_kalshi_price: calc.kalshiAsk,
          max_total_cost: executionCalc?.totalCost ?? calc.totalCost,
          first_venue: "auto",
          market_data_max_age_ms: 300,
          execution_strategy: "sequential",
          max_leg_slippage: 0.01,
          liquidity_buffer: 0.75,
          allow_shrink: true,
        }),
      });

      const data = await res.json().catch(() => null);
      if (!res.ok) {
        throw new Error(formatTradeError(data?.detail, res.status));
      }

      const openedContracts = Number(data?.contracts ?? executableContracts);
      const polyPrice = Number(data?.poly?.price ?? calc.polyAsk);
      const kalshiPrice = Number(data?.kalshi?.price ?? calc.kalshiAsk);
      const polySpend = Number(data?.poly?.spend ?? openedContracts * polyPrice);
      const kalshiSpend = Number(data?.kalshi?.spend ?? openedContracts * kalshiPrice);
      const polyFeesPaid = openedContracts * polyPrice * fees.polyFee;
      const kalshiFeesPaid = openedContracts * fees.kalshiFee * kalshiPrice * (1 - kalshiPrice);
      const totalFeesPaid = polyFeesPaid + kalshiFeesPaid;
      const polyCapital = polySpend + polyFeesPaid;
      const kalshiCapital = kalshiSpend + kalshiFeesPaid;
      const totalCapital = polyCapital + kalshiCapital;
      const lockedProfit = openedContracts * (1 - polyPrice - kalshiPrice) - totalFeesPaid;
      const lockedProfitPct = totalCapital > 0 ? (lockedProfit / totalCapital) * 100 : 0;

      onRecordPosition?.({
        polyId:          pair.polyId,
        polyTitle:       pair.polyTitle,
        polyEndDate:     poly.end_date ?? "",
        kalshiId:        pair.kalshiId,
        kalshiTitle:     pair.kalshiTitle,
        kalshiEndDate:   kalshi.end_date ?? "",
        direction:       calc.direction,
        polyAction:      calc.polyAction,
        kalshiAction:    calc.kalshiAction,
        polyEntryAsk:    polyPrice,
        kalshiEntryAsk:  kalshiPrice,
        contracts:       openedContracts,
        polyCapital,
        kalshiCapital,
        totalCapital,
        polyFeesPaid,
        kalshiFeesPaid,
        totalFeesPaid,
        lockedProfit,
        lockedProfitPct,
      });
      onClose();
    } catch (err) {
      setExecutionError(err instanceof Error ? err.message : "Live open failed");
    } finally {
      setExecuting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onClick={() => {
        if (!executing) onClose();
      }}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-[480px] max-w-[95vw] p-6"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between mb-5">
          <div>
            <h3 className="text-base font-bold text-white">
              {mode === "open" ? "Open Position" : "Close Position"}
            </h3>
            <p className="text-xs text-gray-500 mt-0.5">
              {mode === "open"
                ? "Arbitrage entry — buy both sides simultaneously"
                : "Exit both positions to realise profit"}
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={executing}
            className="text-gray-500 hover:text-white disabled:hover:text-gray-500 disabled:cursor-not-allowed text-xl leading-none ml-4"
          >
            ×
          </button>
        </div>

        {/* Live execution banner */}
        <div className="mb-5 px-3 py-2 rounded-lg bg-red-950/40 border border-red-800/60 text-red-200 text-xs flex items-center gap-2">
          <span className="text-base">⚡</span>
          <span>Live trading enabled. This places direct buy orders on both venues using these price caps.</span>
        </div>

        {/* Trade breakdown */}
        {mode === "open" ? (
          <div className="space-y-3">
            <div className="rounded-lg bg-gray-800 p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs text-gray-500 mb-0.5">Polymarket</div>
                  <div className="text-sm text-blue-300 font-medium truncate max-w-[260px]" title={pair.polyTitle}>
                    {pair.polyTitle}
                  </div>
                </div>
                <div className="text-right shrink-0 ml-3">
                  <div className={`text-sm font-bold ${calc.polyAction === "YES" ? "text-green-400" : "text-red-400"}`}>
                    Buy {calc.polyAction}
                  </div>
                  <div className="text-xs text-gray-400 font-mono">{fmtCent(calc.polyAsk)} ask · ${displayCalc.polySpend.toFixed(2)}</div>
                </div>
              </div>

              <div className="border-t border-gray-700" />

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs text-gray-500 mb-0.5">Kalshi</div>
                  <div className="text-sm text-green-300 font-medium truncate max-w-[260px]" title={pair.kalshiTitle}>
                    {pair.kalshiTitle}
                  </div>
                </div>
                <div className="text-right shrink-0 ml-3">
                  <div className={`text-sm font-bold ${calc.kalshiAction === "YES" ? "text-green-400" : "text-red-400"}`}>
                    Buy {calc.kalshiAction}
                  </div>
                  <div className="text-xs text-gray-400 font-mono">{fmtCent(calc.kalshiAsk)} ask · ${displayCalc.kalshiSpend.toFixed(2)}</div>
                </div>
              </div>
            </div>

            {/* Summary row */}
            <div className="rounded-lg bg-gray-800/60 px-4 py-3 space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <div className="text-gray-400 text-xs">Executable contracts</div>
                <div className="font-mono text-xs text-gray-300">
                  {executableContracts.toLocaleString()} whole
                </div>
              </div>
              <div className="flex items-center justify-between">
                <div className="text-gray-400 text-xs">Total committed</div>
                <div className="font-bold text-white font-mono">${displayCalc.totalCost.toFixed(2)}</div>
              </div>
              <div className="flex items-center justify-between">
                <div className="text-gray-400 text-xs">Gross profit (before fees)</div>
                <div className="text-gray-300 font-mono text-xs">
                  +${displayCalc.grossProfit.toFixed(2)}&nbsp;
                  <span className="text-gray-500">({displayCalc.grossProfitPct.toFixed(1)}%)</span>
                </div>
              </div>
              <div className="flex items-center justify-between border-t border-gray-700 pt-2">
                <div className="text-gray-400 text-xs">
                  Poly fee (taker, {(fees.polyFee * 100).toFixed(1)}%)
                </div>
                <div className="text-red-400 font-mono text-xs">−${displayCalc.polyFeesPaid.toFixed(2)}</div>
              </div>
              <div className="flex items-center justify-between">
                <div className="text-gray-400 text-xs">
                  Kalshi fee (taker, {(fees.kalshiFee * 100).toFixed(1)}% × P×(1−P))
                </div>
                <div className="text-red-400 font-mono text-xs">−${displayCalc.kalshiFeesPaid.toFixed(2)}</div>
              </div>
              <div className="flex items-center justify-between">
                <div className="text-gray-400 text-xs">Total fees</div>
                <div className="text-red-400 font-mono text-xs">−${displayCalc.totalFees.toFixed(2)}</div>
              </div>
              <div className="flex items-center justify-between border-t border-gray-700 pt-2">
                <div className="text-gray-300 text-xs font-semibold">Net profit</div>
                <div className={`font-bold font-mono ${displayCalc.netProfit >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {displayCalc.netProfit >= 0 ? "+" : ""}${displayCalc.netProfit.toFixed(2)}&nbsp;
                  <span className="text-xs">({displayCalc.netProfitPct.toFixed(1)}%)</span>
                </div>
              </div>
            </div>

            <p className="text-[11px] text-gray-600 leading-relaxed">
              Fees are charged at entry: Polymarket as a % of trade value, Kalshi as a % of the
              at-risk amount per contract. Adjust rates via the Fees button in the panel header.
            </p>
            {sizeRuleMessage && (
              <div className="rounded-lg border border-yellow-800 bg-yellow-950/40 px-3 py-2 text-xs text-yellow-100">
                {sizeRuleMessage}
              </div>
            )}
            {executionError && (
              <div className="rounded-lg border border-red-800 bg-red-950/40 px-3 py-2 text-xs text-red-200">
                {executionError}
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="rounded-lg bg-gray-800 p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs text-gray-500 mb-0.5">Polymarket</div>
                  <div className="text-sm text-blue-300 font-medium truncate max-w-[260px]" title={pair.polyTitle}>
                    {pair.polyTitle}
                  </div>
                </div>
                <div className="text-right shrink-0 ml-3">
                  <div className="text-sm font-bold text-orange-400">Sell {calc.polyAction}</div>
                  <div className="text-xs text-gray-400 font-mono">~${calc.polySpend.toFixed(2)} position</div>
                </div>
              </div>

              <div className="border-t border-gray-700" />

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs text-gray-500 mb-0.5">Kalshi</div>
                  <div className="text-sm text-green-300 font-medium truncate max-w-[260px]" title={pair.kalshiTitle}>
                    {pair.kalshiTitle}
                  </div>
                </div>
                <div className="text-right shrink-0 ml-3">
                  <div className="text-sm font-bold text-orange-400">Sell {calc.kalshiAction}</div>
                  <div className="text-xs text-gray-400 font-mono">~${calc.kalshiSpend.toFixed(2)} position</div>
                </div>
              </div>
            </div>

            <div className="rounded-lg bg-gray-800/60 px-4 py-3 text-sm">
              <p className="text-gray-400 text-xs mb-1">Note on closing</p>
              <p className="text-gray-300 text-xs leading-relaxed">
                Closing sells both sides at current market prices. Profit realised depends on
                how prices have moved since entry. Current spread at close may differ from entry.
              </p>
            </div>
          </div>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={executing}
            className="px-4 py-2 rounded text-sm text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 disabled:hover:bg-gray-800 disabled:hover:text-gray-400 disabled:cursor-not-allowed transition-colors"
          >
            Cancel
          </button>
          {mode === "open" ? (
            <button
              onClick={executeOpenTrade}
              disabled={!canExecuteLiveOpen}
              title={sizeRuleMessage ?? undefined}
              className="px-5 py-2 rounded text-sm font-bold bg-green-700 hover:bg-green-600 disabled:bg-gray-700 disabled:text-gray-500 disabled:cursor-not-allowed text-white transition-colors"
            >
              {executing ? "Executing..." : "Execute Live Open"}
            </button>
          ) : (
            <button
              disabled
              title="Trading API not yet connected"
              className="px-5 py-2 rounded text-sm font-bold bg-gray-700 text-gray-500 cursor-not-allowed"
            >
              Execute Close — coming soon
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────────

/**
 * Net arb edge after properly-modelled entry-side fees.
 * fees=null → gross edge (no fee deduction).
 *
 * Polymarket: taker fee = polyFee × polyAsk (% of trade value).
 * Kalshi:     at-risk fee = kalshiFee × min(kalshiAsk, 1-kalshiAsk) per contract.
 * Both fees increase the effective entry cost; payout is always $1 per unit.
 */
function computeEdge(poly: Market, kalshi: Market, fees: FeeConfig | null): number | null {
  const pf = fees?.polyFee ?? 0;
  const kf = fees?.kalshiFee ?? 0;

  let edgeA: number | null = null;
  let edgeB: number | null = null;

  if (poly.yes_ask > 0 && kalshi.no_ask > 0) {
    const polyEff   = poly.yes_ask * (1 + pf);
    const kalshiEff = kalshi.no_ask + kf * kalshi.no_ask * (1 - kalshi.no_ask);
    edgeA = 1 - polyEff - kalshiEff;
  }
  if (kalshi.yes_ask > 0 && poly.no_ask > 0) {
    const kalshiEff = kalshi.yes_ask + kf * kalshi.yes_ask * (1 - kalshi.yes_ask);
    const polyEff   = poly.no_ask * (1 + pf);
    edgeB = 1 - kalshiEff - polyEff;
  }

  if (edgeA === null && edgeB === null) return null;
  if (edgeA === null) return edgeB;
  if (edgeB === null) return edgeA;
  return Math.max(edgeA, edgeB);
}

function calcTrade(poly: Market, kalshi: Market, betSize: number, fees: FeeConfig): TradeCalc | null {
  // Pick best direction by gross edge (fees don't change which direction wins)
  const edgeA = poly.yes_ask > 0 && kalshi.no_ask > 0 ? 1 - poly.yes_ask - kalshi.no_ask : -Infinity;
  const edgeB = kalshi.yes_ask > 0 && poly.no_ask > 0 ? 1 - kalshi.yes_ask - poly.no_ask : -Infinity;

  if (edgeA === -Infinity && edgeB === -Infinity) return null;

  const useA = edgeA >= edgeB;
  const polyAsk   = useA ? poly.yes_ask   : poly.no_ask;
  const kalshiAsk = useA ? kalshi.no_ask  : kalshi.yes_ask;

  if (polyAsk <= 0 || kalshiAsk <= 0) return null;

  // Kalshi fee basis: P × (1-P) per contract (bell curve, max 0.25 at 50¢)
  const kalshiFeeBasis = kalshiAsk * (1 - kalshiAsk);

  // Effective entry costs including fees
  const polyEffCost   = polyAsk * (1 + fees.polyFee);
  const kalshiEffCost = kalshiAsk + fees.kalshiFee * kalshiFeeBasis;
  const totalEffCost  = polyEffCost + kalshiEffCost;

  // Units = how many contracts we can buy with betSize
  const units = betSize / totalEffCost;

  // Fee breakdown
  const polyFeesPaid   = units * polyAsk * fees.polyFee;
  const kalshiFeesPaid = units * fees.kalshiFee * kalshiFeeBasis;
  const totalFees      = polyFeesPaid + kalshiFeesPaid;

  // Spending breakdown (base position + fee)
  const polySpend   = units * polyEffCost;
  const kalshiSpend = units * kalshiEffCost;

  // Gross: profit if fees were zero (raw market edge × units)
  const grossProfit    = units * (1 - (polyAsk + kalshiAsk));
  const grossProfitPct = (grossProfit / betSize) * 100;

  // Net: payout ($1/unit) minus total effective entry cost
  const netProfit    = units * (1 - totalEffCost);
  const netProfitPct = (netProfit / betSize) * 100;

  return {
    direction:     useA ? "A" : "B",
    polyAction:    useA ? "YES" : "NO",
    kalshiAction:  useA ? "NO"  : "YES",
    polyAsk,
    kalshiAsk,
    contracts: units,
    polySpend,
    kalshiSpend,
    totalCost: betSize,
    polyFeesPaid,
    kalshiFeesPaid,
    totalFees,
    grossProfit,
    grossProfitPct,
    netProfit,
    netProfitPct,
  };
}

function calcTradeForContracts(
  direction: "A" | "B",
  polyAsk: number,
  kalshiAsk: number,
  contracts: number,
  fees: FeeConfig,
): TradeCalc | null {
  if (contracts <= 0 || polyAsk <= 0 || kalshiAsk <= 0) return null;
  const kalshiFeeBasis = kalshiAsk * (1 - kalshiAsk);
  const polyFeesPaid = contracts * polyAsk * fees.polyFee;
  const kalshiFeesPaid = contracts * fees.kalshiFee * kalshiFeeBasis;
  const totalFees = polyFeesPaid + kalshiFeesPaid;
  const polySpend = contracts * polyAsk + polyFeesPaid;
  const kalshiSpend = contracts * kalshiAsk + kalshiFeesPaid;
  const totalCost = polySpend + kalshiSpend;
  const grossProfit = contracts * (1 - polyAsk - kalshiAsk);
  const netProfit = contracts - totalCost;

  return {
    direction,
    polyAction: direction === "A" ? "YES" : "NO",
    kalshiAction: direction === "A" ? "NO" : "YES",
    polyAsk,
    kalshiAsk,
    contracts,
    polySpend,
    kalshiSpend,
    totalCost,
    polyFeesPaid,
    kalshiFeesPaid,
    totalFees,
    grossProfit,
    grossProfitPct: totalCost > 0 ? (grossProfit / totalCost) * 100 : 0,
    netProfit,
    netProfitPct: totalCost > 0 ? (netProfit / totalCost) * 100 : 0,
  };
}

function fmtCent(value: number): string {
  return `${Math.round(value * 100)}¢`;
}

function fmtDollar(value: number): string {
  if (!Number.isFinite(value)) return "unknown";
  return `$${value.toFixed(2)}`;
}

function formatTradeError(detail: unknown, status: number): string {
  if (typeof detail === "string") return friendlyTradeMessage(detail, status);
  if (detail && typeof detail === "object") {
    const obj = detail as { message?: unknown; execution_version?: unknown };
    const message = typeof obj.message === "string"
      ? friendlyTradeMessage(obj.message, status)
      : `Live open rejected with HTTP ${status}`;
    return typeof obj.execution_version === "string"
      ? `${message} (${obj.execution_version})`
      : message;
  }
  return `Live open rejected with HTTP ${status}`;
}

function friendlyTradeMessage(message: string, status: number): string {
  const lowered = message.toLowerCase();
  if (lowered.includes("fill_or_kill_insufficient_resting_volume") || lowered.includes("resting volume")) {
    return "FOK could not fill the whole size at current resting volume. The book moved or thinned; retry with the fresh depth-guarded size.";
  }
  if (lowered.includes("fresh order-book depth is too thin") || lowered.includes("fresh depth")) {
    return message;
  }
  return message || `Live open rejected with HTTP ${status}`;
}

function formatDate(dateStr: string): string {
  if (!dateStr) return "—";
  try {
    return new Date(dateStr).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return dateStr;
  }
}
