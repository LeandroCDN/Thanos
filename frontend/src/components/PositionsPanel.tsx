import { useEffect, useMemo, useState } from "react";
import type { ArbitragePosition, Market } from "../types";

const FEE_STORAGE_KEY = "thanos:fees";
const DEFAULT_FEES = { polyFee: 0.02, kalshiFee: 0.07 };

function loadFees() {
  try {
    return { ...DEFAULT_FEES, ...JSON.parse(localStorage.getItem(FEE_STORAGE_KEY) ?? "{}") };
  } catch {
    return DEFAULT_FEES;
  }
}

interface LiveEntry {
  poly?: Market;
  kalshi?: Market;
}

interface Balance {
  total?: number;
  available?: number;
  reserved?: number;
  error?: string;
  note?: string;
}

interface Balances {
  polymarket?: Balance;
  kalshi?: Balance;
}

interface PositionsPanelProps {
  positions: ArbitragePosition[];
  onRemove: (id: string) => void;
  onClose?: (id: string, realizedPnl: number) => void;
}

export function PositionsPanel({ positions, onRemove }: PositionsPanelProps) {
  const [liveData, setLiveData] = useState<Record<string, LiveEntry>>({});
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({});
  const [refreshingAll, setRefreshingAll] = useState(false);
  const [balances, setBalances] = useState<Balances>({});
  const [balancesLoading, setBalancesLoading] = useState(false);
  const fees = loadFees();

  const openPositions = useMemo(() => positions.filter((p) => p.status === "open"), [positions]);

  const totals = useMemo(() => {
    const totalCapital = openPositions.reduce((s, p) => s + p.totalCapital, 0);
    const lockedProfit = openPositions.reduce((s, p) => s + p.lockedProfit, 0);
    const lockedPct = totalCapital > 0 ? (lockedProfit / totalCapital) * 100 : 0;
    return { totalCapital, lockedProfit, lockedPct };
  }, [openPositions]);

  async function fetchBalances() {
    setBalancesLoading(true);
    try {
      const res = await fetch("/api/balances");
      if (res.ok) setBalances(await res.json());
    } finally {
      setBalancesLoading(false);
    }
  }

  // Auto-fetch balances on first mount
  useEffect(() => { fetchBalances(); }, []);

  async function fetchLivePrices(pos: ArbitragePosition) {
    setRefreshing((prev) => ({ ...prev, [pos.id]: true }));
    try {
      const res = await fetch(
        `/api/markets/pairs?poly_ids=${encodeURIComponent(pos.polyId)}&kalshi_ids=${encodeURIComponent(pos.kalshiId)}`,
      );
      if (res.ok) {
        const data = await res.json();
        setLiveData((prev) => ({
          ...prev,
          [pos.id]: {
            poly:   (data.polymarket ?? [])[0] as Market | undefined,
            kalshi: (data.kalshi     ?? [])[0] as Market | undefined,
          },
        }));
      }
    } finally {
      setRefreshing((prev) => ({ ...prev, [pos.id]: false }));
    }
  }

  async function fetchAllLivePrices() {
    if (!openPositions.length) return;
    setRefreshingAll(true);
    try {
      const polyIds   = [...new Set(openPositions.map((p) => p.polyId))].join(",");
      const kalshiIds = [...new Set(openPositions.map((p) => p.kalshiId))].join(",");
      const res = await fetch(
        `/api/markets/pairs?poly_ids=${encodeURIComponent(polyIds)}&kalshi_ids=${encodeURIComponent(kalshiIds)}`,
      );
      if (res.ok) {
        const data = await res.json();
        const polyMap   = new Map<string, Market>((data.polymarket ?? []).map((m: Market) => [m.id, m]));
        const kalshiMap = new Map<string, Market>((data.kalshi     ?? []).map((m: Market) => [m.id, m]));
        const next: Record<string, LiveEntry> = {};
        for (const pos of openPositions) {
          next[pos.id] = { poly: polyMap.get(pos.polyId), kalshi: kalshiMap.get(pos.kalshiId) };
        }
        setLiveData(next);
      }
    } finally {
      setRefreshingAll(false);
    }
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      {/* Header row 1: title + positions stats + refresh buttons */}
      <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50 flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 min-w-0 flex-wrap">
          <h2 className="font-semibold text-white shrink-0">Active Positions</h2>
          <span className="text-xs text-gray-500 shrink-0">
            {openPositions.length} position{openPositions.length !== 1 ? "s" : ""}
          </span>
          {openPositions.length > 0 && (
            <div className="flex items-center gap-3 text-xs ml-1">
              <span className="text-gray-500 shrink-0">
                Capital:{" "}
                <span className="text-gray-300 font-mono">${totals.totalCapital.toFixed(2)}</span>
              </span>
              <span className="text-gray-700">·</span>
              <span className="text-gray-500 shrink-0">
                Locked:{" "}
                <span className={`font-mono font-semibold ${totals.lockedProfit >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {totals.lockedProfit >= 0 ? "+" : ""}${totals.lockedProfit.toFixed(2)}
                </span>
                <span className="text-gray-600 ml-1">({totals.lockedPct.toFixed(1)}%)</span>
              </span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={fetchBalances}
            disabled={balancesLoading}
            title="Refresh account balances"
            className={`px-3 py-1.5 rounded text-sm font-semibold transition-colors flex items-center gap-1.5 ${
              balancesLoading ? "bg-gray-700 text-gray-500 cursor-not-allowed" : "bg-gray-700 hover:bg-gray-600 text-gray-300"
            }`}
          >
            <span className={`inline-block text-base leading-none ${balancesLoading ? "animate-spin" : ""}`}>↻</span>
            Balances
          </button>
          <button
            onClick={fetchAllLivePrices}
            disabled={refreshingAll || openPositions.length === 0}
            title="Refresh live prices for all positions"
            className={`px-3 py-1.5 rounded text-sm font-semibold transition-colors flex items-center gap-1.5 ${
              refreshingAll || openPositions.length === 0
                ? "bg-gray-700 text-gray-500 cursor-not-allowed"
                : "bg-gray-700 hover:bg-gray-600 text-gray-300"
            }`}
          >
            <span className={`inline-block text-base leading-none ${refreshingAll ? "animate-spin" : ""}`}>↻</span>
            {refreshingAll ? "Refreshing…" : "Refresh P&L"}
          </button>
        </div>
      </div>

      {/* Header row 2: account balances */}
      <div className="px-4 py-2.5 border-b border-gray-800/60 bg-gray-900/30 flex items-center gap-6 flex-wrap">
        <BalancePill
          label="Polymarket"
          color="green"
          balance={balances.polymarket}
          invested={totals.totalCapital}
          loading={balancesLoading}
        />
        <span className="text-gray-800 text-sm">|</span>
        <BalancePill
          label="Kalshi"
          color="blue"
          balance={balances.kalshi}
          invested={totals.totalCapital}
          loading={balancesLoading}
        />
      </div>

      {/* Body */}
      {openPositions.length === 0 ? (
        <div className="flex items-center justify-center h-24 text-gray-600 text-sm">
          No open positions — use the{" "}
          <span className="mx-1 px-1.5 py-0.5 rounded text-xs bg-green-900/40 text-green-400 font-semibold">
            OPEN
          </span>{" "}
          button on a whitelisted pair to record one.
        </div>
      ) : (
        <div className="overflow-auto" style={{ maxHeight: "272px" }}>
          <table className="w-full text-sm text-left">
            <thead className="sticky top-0 bg-gray-900/95 backdrop-blur z-10">
              <tr className="text-[11px] text-gray-500 uppercase tracking-wide border-b border-gray-800">
                <th className="px-3 py-2 w-[26%]">Pair</th>
                <th className="px-3 py-2 w-[16%]">Direction · Entry</th>
                <th className="px-3 py-2 w-[9%] text-right">Contracts</th>
                <th className="px-3 py-2 w-[9%] text-right">Capital</th>
                <th className="px-3 py-2 w-[10%] text-right">Locked</th>
                <th className="px-3 py-2 w-[10%] text-right">Unrealized</th>
                <th className="px-3 py-2 w-[10%] text-center">Closes</th>
                <th className="px-3 py-2 w-[10%] text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/50">
              {openPositions.map((pos) => {
                const live       = liveData[pos.id];
                const unrealized = live?.poly && live?.kalshi
                  ? calcUnrealizedPnl(pos, live.poly, live.kalshi, fees)
                  : null;
                const endDate = pos.polyEndDate || pos.kalshiEndDate;
                const isRefreshing = refreshing[pos.id];

                return (
                  <tr key={pos.id} className="hover:bg-gray-800/30 transition-colors">
                    {/* Pair */}
                    <td className="px-3 py-2">
                      <div
                        className="text-[11px] text-green-300 truncate max-w-[180px]"
                        title={pos.polyTitle}
                      >
                        {pos.polyTitle}
                      </div>
                      <div
                        className="text-[11px] text-blue-300 truncate max-w-[180px]"
                        title={pos.kalshiTitle}
                      >
                        {pos.kalshiTitle}
                      </div>
                    </td>

                    {/* Direction + entry prices */}
                    <td className="px-3 py-2">
                      <div className="text-[10px] leading-tight">
                        <span className={pos.polyAction === "YES" ? "text-green-400" : "text-orange-400"}>
                          {pos.polyAction}
                        </span>
                        <span className="text-gray-500"> Poly · </span>
                        <span className="text-gray-400 font-mono">{fmtCent(pos.polyEntryAsk)}</span>
                      </div>
                      <div className="text-[10px] leading-tight">
                        <span className={pos.kalshiAction === "YES" ? "text-green-400" : "text-orange-400"}>
                          {pos.kalshiAction}
                        </span>
                        <span className="text-gray-500"> Kalshi · </span>
                        <span className="text-gray-400 font-mono">{fmtCent(pos.kalshiEntryAsk)}</span>
                      </div>
                      <div className="text-[10px] text-gray-600 font-mono mt-0.5">
                        Entered {formatDate(pos.addedAt)}
                      </div>
                    </td>

                    {/* Contracts */}
                    <td className="px-3 py-2 text-right font-mono text-xs text-gray-300">
                      {pos.contracts.toFixed(1)}
                    </td>

                    {/* Capital */}
                    <td className="px-3 py-2 text-right">
                      <div className="font-mono text-xs text-gray-300">${pos.totalCapital.toFixed(2)}</div>
                      <div className="text-[10px] text-gray-600 font-mono">
                        fees −${pos.totalFeesPaid.toFixed(2)}
                      </div>
                    </td>

                    {/* Locked profit */}
                    <td className="px-3 py-2 text-right">
                      <div
                        className={`text-xs font-mono font-semibold ${pos.lockedProfit >= 0 ? "text-green-400" : "text-red-400"}`}
                      >
                        {pos.lockedProfit >= 0 ? "+" : ""}${pos.lockedProfit.toFixed(2)}
                      </div>
                      <div className="text-[10px] text-gray-600 font-mono">
                        {pos.lockedProfitPct.toFixed(1)}%
                      </div>
                    </td>

                    {/* Unrealized P&L */}
                    <td className="px-3 py-2 text-right">
                      {unrealized !== null ? (
                        <>
                          <div
                            className={`text-xs font-mono font-semibold ${unrealized >= 0 ? "text-green-400" : "text-red-400"}`}
                          >
                            {unrealized >= 0 ? "+" : ""}${unrealized.toFixed(2)}
                          </div>
                          <div className="text-[10px] text-gray-600 font-mono">
                            {((unrealized / pos.totalCapital) * 100).toFixed(1)}%
                          </div>
                        </>
                      ) : (
                        <span className="text-gray-700 text-xs">—</span>
                      )}
                    </td>

                    {/* Closes */}
                    <td className="px-3 py-2 text-center text-xs text-gray-400">
                      {endDate ? formatDate(endDate) : "—"}
                    </td>

                    {/* Actions */}
                    <td className="px-3 py-2">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          onClick={() => fetchLivePrices(pos)}
                          disabled={isRefreshing}
                          title="Refresh live prices"
                          className={`px-2 py-1 rounded text-xs transition-colors ${
                            isRefreshing
                              ? "text-gray-600 cursor-not-allowed"
                              : "text-gray-500 hover:text-gray-200 hover:bg-gray-700"
                          }`}
                        >
                          <span className={isRefreshing ? "animate-spin inline-block" : ""}>↻</span>
                        </button>
                        <button
                          onClick={() => onRemove(pos.id)}
                          title="Remove position"
                          className="px-2 py-1 rounded text-xs text-gray-600 hover:text-red-400 hover:bg-gray-800/60 transition-colors"
                        >
                          ×
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * What you'd net if you sold both legs right now at current bid prices.
 * Closing incurs taker fees on both sides, same formulas as entry.
 */
function calcUnrealizedPnl(
  pos: ArbitragePosition,
  poly: Market,
  kalshi: Market,
  fees: { polyFee: number; kalshiFee: number },
): number {
  const polyBid   = pos.polyAction   === "YES" ? poly.yes_bid   : poly.no_bid;
  const kalshiBid = pos.kalshiAction === "YES" ? kalshi.yes_bid : kalshi.no_bid;
  if (!polyBid || !kalshiBid) return 0;

  // Poly taker fee on sell: 2% of the bid value
  const polyProceeds = pos.contracts * polyBid * (1 - fees.polyFee);
  // Kalshi taker fee on sell: rate × P × (1-P)
  const kalshiProceeds =
    pos.contracts * (kalshiBid - fees.kalshiFee * kalshiBid * (1 - kalshiBid));

  return polyProceeds + kalshiProceeds - pos.totalCapital;
}

function BalancePill({
  label,
  color,
  balance,
  loading,
}: {
  label: string;
  color: "green" | "blue";
  balance?: Balance;
  invested?: number;
  loading: boolean;
}) {
  const accent = color === "green" ? "text-green-400" : "text-blue-400";

  if (loading && !balance) {
    return (
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-semibold ${accent}`}>{label}</span>
        <span className="text-gray-600 animate-pulse">Loading…</span>
      </div>
    );
  }

  if (!balance || balance.error) {
    const err = balance?.error ?? "—";
    const isAuthErr = err.includes("401") || err.includes("CLOB") || err.includes("auth");
    return (
      <div className="flex items-center gap-2 text-xs">
        <span className={`font-semibold ${accent}`}>{label}</span>
        <span
          className="text-gray-600 text-[11px] cursor-help"
          title={isAuthErr ? err : undefined}
        >
          {isAuthErr ? "CLOB L2 credentials required" : err}
        </span>
      </div>
    );
  }

  const total     = balance.total     ?? 0;
  const available = balance.available ?? total;
  const reserved  = balance.reserved  ?? (total - available);
  const hasNote   = !!balance.note;

  return (
    <div className="flex items-center gap-3 text-xs">
      <span className={`font-semibold ${accent} shrink-0`}>{label}</span>
      <div className="flex items-center gap-2">
        <span className="text-gray-400">
          Total{" "}
          <span
            className={`font-mono ${hasNote ? "text-gray-500" : "text-gray-200"}`}
            title={balance.note}
          >
            ${total.toFixed(2)}
            {hasNote && <span className="ml-1 text-gray-600 text-[10px]" title={balance.note}>ⓘ</span>}
          </span>
        </span>
        {!hasNote && (
          <>
            <span className="text-gray-700">·</span>
            <span className="text-gray-400">
              Available <span className="text-gray-200 font-mono">${available.toFixed(2)}</span>
            </span>
            {reserved > 0.01 && (
              <>
                <span className="text-gray-700">·</span>
                <span className="text-gray-400">
                  Invested <span className="text-orange-300 font-mono">${reserved.toFixed(2)}</span>
                </span>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function fmtCent(v: number) {
  return `${Math.round(v * 100)}¢`;
}

function formatDate(d: string) {
  try {
    return new Date(d).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return d;
  }
}
