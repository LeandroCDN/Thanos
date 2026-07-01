import asyncio
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

import kalshi as kalshi_data
import polymarket as poly_data
from arb_math import (
    calc_trade,
    calc_trade_for_contracts,
    calc_unrealized_pnl,
    compute_edges,
    hours_to_earliest_close,
    position_from_trade,
    side_ask,
    side_bid,
)
from market_stream import market_stream
from store import store, utc_now
from trading import CloseTradeRequest, OpenTradeRequest, execute_close_trade, execute_open_trade


BotMode = Literal["off", "test", "on"]
ExecutionStrategy = Literal["sequential", "concurrent_fok"]
_UNSET = object()
BOT_OPEN_LIQUIDITY_BUFFER = Decimal("0.75")
BOT_OPEN_MAX_ATTEMPTS = 2


class BotModeRequest(BaseModel):
    mode: BotMode


class BotSettingsUpdate(BaseModel):
    mode: BotMode | None = None
    scan_interval_seconds: float | None = Field(default=None, ge=1, le=3600)
    event_driven: bool | None = None
    event_debounce_ms: int | None = Field(default=None, ge=0, le=5000)
    open_enabled: bool | None = None
    close_enabled: bool | None = None
    min_net_edge: float | None = Field(default=None, ge=0, le=1)
    max_leg_slippage: float | None = Field(default=None, ge=0, le=0.5)
    fixed_trade_dollars: float | None = Field(default=None, gt=0)
    max_total_capital: float | None = Field(default=None, ge=0)
    max_open_positions: int | None = Field(default=None, ge=0)
    min_hours_to_close: float | None = Field(default=None, ge=0)
    poly_fee: float | None = Field(default=None, ge=0, le=1)
    kalshi_fee: float | None = Field(default=None, ge=0, le=1)
    take_profit_enabled: bool | None = None
    take_profit_pct: float | None = None
    stop_loss_enabled: bool | None = None
    stop_loss_pct: float | None = None
    spread_multiple_close_enabled: bool | None = None
    spread_multiple_close: float | None = Field(default=None, ge=1)
    close_before_close_enabled: bool | None = None
    close_before_close_hours: float | None = Field(default=None, ge=0)
    edge_reversion_enabled: bool | None = None
    close_edge_below_pct: float | None = None
    min_close_profit_pct: float | None = None
    max_daily_loss: float | None = Field(default=None, ge=0)
    max_daily_trades: int | None = Field(default=None, ge=0)
    max_consecutive_failures: int | None = Field(default=None, ge=0)
    stop_on_api_error: bool | None = None
    market_data_max_age_ms: int | None = Field(default=None, ge=100, le=60_000)
    balance_cache_seconds: float | None = Field(default=None, ge=0, le=300)
    execution_strategy: ExecutionStrategy | None = None
    telegram_enabled: bool | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notify_bot_status: bool | None = None
    notify_trade_opened: bool | None = None
    notify_trade_closed: bool | None = None
    notify_trade_failed: bool | None = None
    notify_circuit_breaker: bool | None = None


class BotController:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._scan_lock = asyncio.Lock()
        self._last_mode: str | None = None
        self._warmup_complete = False
        self._balance_cache: tuple[float, dict[str, Any]] | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._warmup_complete = False
        store.set_state("bot_warmup_complete", False)
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def status(self) -> dict[str, Any]:
        settings = store.get_bot_settings()
        state = store.get_state("bot_runtime", {}) or {}
        return {
            "running": bool(self._task and not self._task.done()),
            "scanning": self._scan_lock.locked(),
            "warmupComplete": self._warmup_complete,
            "settings": self._public_settings(settings),
            "lastScanAt": state.get("lastScanAt"),
            "nextScanAt": state.get("nextScanAt"),
            "lastSummary": state.get("lastSummary") or {},
            "recentLogs": store.recent_logs(50),
            "dailyTradeCount": store.daily_trade_count(),
            "dailyRealizedPnl": store.daily_realized_pnl(),
            "consecutiveFailures": store.get_state("bot_consecutive_failures", 0) or 0,
            "marketData": market_stream.status(),
        }

    async def set_mode(self, mode: BotMode) -> dict[str, Any]:
        settings = store.set_bot_mode(mode)
        self._warmup_complete = False
        store.set_state("bot_warmup_complete", False)
        store.set_state("bot_consecutive_failures", 0)
        store.log("info", "mode_changed", f"Bot mode set to {mode.upper()}", {"mode": mode})
        await self._notify(settings, "notify_bot_status", f"Bot mode set to {mode.upper()}")
        return self.status()

    def update_settings(self, update: BotSettingsUpdate) -> dict[str, Any]:
        updates = update.dict(exclude_none=True)
        mode_changed = "mode" in updates and updates["mode"] != store.get_bot_settings().get("mode")
        settings = store.update_bot_settings(updates)
        if mode_changed:
            self._warmup_complete = False
            store.set_state("bot_warmup_complete", False)
            store.set_state("bot_consecutive_failures", 0)
        store.log("info", "settings_updated", "Bot settings updated", {"keys": sorted(updates.keys())})
        return self.status()

    async def scan_once(self, trigger: str = "manual", market_change: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._scan_lock:
            settings = store.get_bot_settings()
            mode = settings["mode"]
            allow_execute = mode in ("test", "on") and self._warmup_complete
            summary = await self._run_scan(
                settings,
                allow_execute=allow_execute,
                trigger=trigger,
                market_change=market_change,
            )
            if mode in ("test", "on") and not self._warmup_complete:
                self._warmup_complete = True
                store.set_state("bot_warmup_complete", True)
                store.log(
                    "info",
                    "warmup_complete",
                    "Warm-up scan complete; bot execution is armed for the next scan.",
                    {},
                )
                await self._notify(
                    settings,
                    "notify_bot_status",
                    "Warm-up scan complete. Bot execution is armed for the next scan.",
                )
            return summary

    async def send_test_telegram(self) -> dict[str, Any]:
        settings = store.get_bot_settings()
        await self._send_telegram(settings, "Thanos bot Telegram test notification.")
        return {"status": "sent"}

    async def _loop(self) -> None:
        trigger = "scheduled"
        market_change: dict[str, Any] | None = None
        sequence = int(market_stream.status().get("sequence") or 0)
        while True:
            settings = store.get_bot_settings()
            mode = settings["mode"]
            if mode != self._last_mode:
                self._last_mode = mode
                self._warmup_complete = False
                store.set_state("bot_warmup_complete", False)

            if mode == "off":
                self._set_runtime(next_scan_at=None)
                trigger = "scheduled"
                market_change = None
                sequence = int(market_stream.status().get("sequence") or sequence)
                await asyncio.sleep(1)
                continue

            await self.scan_once(trigger, market_change=market_change)
            interval = max(1.0, float(settings.get("scan_interval_seconds") or 5))
            next_scan_at = datetime.now(timezone.utc) + timedelta(seconds=interval)
            self._set_runtime(next_scan_at=next_scan_at.isoformat())
            if settings.get("event_driven", True):
                change = await market_stream.wait_for_change_after(sequence, timeout=interval)
                sequence = int(change.get("sequence") or sequence)
                if change.get("changed"):
                    debounce = max(0.0, float(settings.get("event_debounce_ms") or 0) / 1000.0)
                    if debounce > 0:
                        await asyncio.sleep(debounce)
                        extra = await market_stream.wait_for_change_after(sequence, timeout=0)
                        if extra.get("changed"):
                            change = self._merge_market_changes(change, extra)
                            sequence = int(change.get("sequence") or sequence)
                    trigger = "market_update"
                    market_change = change
                else:
                    trigger = "scheduled"
                    market_change = None
            else:
                await asyncio.sleep(interval)
                sequence = int(market_stream.status().get("sequence") or sequence)
                trigger = "scheduled"
                market_change = None

    async def _run_scan(
        self,
        settings: dict[str, Any],
        *,
        allow_execute: bool,
        trigger: str,
        market_change: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        mode = settings["mode"]
        summary: dict[str, Any] = {
            "trigger": trigger,
            "mode": mode,
            "allowExecute": allow_execute,
            "pairsScanned": 0,
            "eligibleCount": 0,
            "rejectedCount": 0,
            "openedCount": 0,
            "closedCount": 0,
            "errorCount": 0,
            "openPositions": 0,
            "rejections": [],
            "eligible": [],
            "executions": [],
            "errors": [],
        }

        all_pairs = store.list_whitelisted_pairs()
        all_open_positions = store.list_positions("open")
        pairs, positions_to_close, targeted = self._scan_scope(all_pairs, all_open_positions, market_change)
        summary["pairsScanned"] = len(pairs)
        summary["totalPairs"] = len(all_pairs)
        summary["targeted"] = targeted
        summary["openPositions"] = len(all_open_positions)
        if targeted:
            summary["changedPolymarketMarkets"] = len(market_change.get("polyIds", []) if market_change else [])
            summary["changedKalshiMarkets"] = len(market_change.get("kalshiIds", []) if market_change else [])

        if await self._circuit_breaker_hit(settings, summary):
            self._finish_scan(started_at, summary)
            return summary

        if not pairs and not positions_to_close:
            self._finish_scan(started_at, summary)
            return summary

        poly_ids = sorted({p["polyId"] for p in pairs} | {p["polyId"] for p in positions_to_close})
        kalshi_ids = sorted({p["kalshiId"] for p in pairs} | {p["kalshiId"] for p in positions_to_close})

        try:
            poly_markets, kalshi_markets = await market_stream.get_pair_markets(poly_ids, kalshi_ids)
        except Exception as exc:
            await self._record_error(settings, summary, "market_fetch_failed", str(exc))
            self._finish_scan(started_at, summary)
            return summary

        poly_by_id = {m["id"]: m for m in poly_markets}
        kalshi_by_id = {m["id"]: m for m in kalshi_markets}

        await self._evaluate_closes(settings, summary, positions_to_close, poly_by_id, kalshi_by_id, allow_execute)
        all_open_positions = store.list_positions("open")
        await self._evaluate_opens(settings, summary, pairs, all_open_positions, poly_by_id, kalshi_by_id, allow_execute)

        self._finish_scan(started_at, summary)
        return summary

    async def _evaluate_opens(
        self,
        settings: dict[str, Any],
        summary: dict[str, Any],
        pairs: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
        poly_by_id: dict[str, dict[str, Any]],
        kalshi_by_id: dict[str, dict[str, Any]],
        allow_execute: bool,
    ) -> None:
        if not settings.get("open_enabled", True):
            return

        balances = await self._fetch_balances(settings, summary) if settings["mode"] == "on" else None
        if settings["mode"] == "on" and balances is None:
            return
        blocking_positions = [p for p in open_positions if self._blocks_new_open(settings, p)]
        current_capital = sum(float(p.get("totalCapital") or 0) for p in blocking_positions)
        open_count = len(blocking_positions)

        for pair in pairs:
            pair_id = pair["id"]
            poly = poly_by_id.get(pair["polyId"])
            kalshi = kalshi_by_id.get(pair["kalshiId"])
            if not poly or not kalshi:
                self._reject(summary, pair_id, "missing_live_market", "Live market data unavailable")
                continue
            if any(p.get("polyId") == pair["polyId"] and p.get("kalshiId") == pair["kalshiId"] for p in blocking_positions):
                self._reject(summary, pair_id, "already_open", "Pair already has an open dashboard position")
                continue
            if self._too_close_to_expiry(settings, poly, kalshi):
                self._reject(summary, pair_id, "near_expiry", "Market closes inside the configured minimum window")
                continue
            if open_count >= int(settings.get("max_open_positions") or 0):
                self._reject(summary, pair_id, "max_open_positions", "Maximum open position count reached")
                continue
            if current_capital + float(settings["fixed_trade_dollars"]) > float(settings["max_total_capital"]):
                self._reject(summary, pair_id, "max_total_capital", "Maximum deployed capital reached")
                continue

            trade = calc_trade(
                poly,
                kalshi,
                float(settings["fixed_trade_dollars"]),
                float(settings["poly_fee"]),
                float(settings["kalshi_fee"]),
            )
            if not trade:
                self._reject(summary, pair_id, "missing_prices", "Both directions lack complete ask prices")
                continue
            if trade["netEdge"] < float(settings["min_net_edge"]):
                self._reject(
                    summary,
                    pair_id,
                    "edge_below_threshold",
                    f"Best net edge {trade['netEdge'] * 100:.2f}% is below threshold",
                    {"netEdge": trade["netEdge"], "grossEdge": trade["grossEdge"]},
                )
                continue

            contracts = math.floor(float(trade["contracts"]))
            if contracts < 1:
                self._reject(summary, pair_id, "size_below_one_contract", "Fixed trade size rounds below 1 contract")
                continue

            depth_info = await self._depth_ok(settings, pair, trade, summary)
            if not depth_info:
                continue

            sized_trade = calc_trade_for_contracts(
                trade["direction"],
                trade["polyAsk"],
                trade["kalshiAsk"],
                contracts,
                float(settings["poly_fee"]),
                float(settings["kalshi_fee"]),
            )
            if not sized_trade:
                self._reject(summary, pair_id, "sizing_failed", "Could not calculate whole-contract trade")
                continue
            min_contracts = math.ceil(1.0 / float(sized_trade["polyAsk"]))
            poly_gross_spend = contracts * float(sized_trade["polyAsk"])
            if poly_gross_spend < 1.0:
                min_total = min_contracts * (float(sized_trade["polyAsk"]) + float(sized_trade["kalshiAsk"]))
                self._reject(
                    summary,
                    pair_id,
                    "size_below_polymarket_minimum",
                    (
                        f"Minimum for this pair is {min_contracts} contracts, about ${min_total:.2f} "
                        "total at current prices before fees."
                    ),
                    {"contracts": contracts, "minContracts": min_contracts, "polySpend": poly_gross_spend},
                )
                continue

            if balances and not self._balances_cover(balances, sized_trade):
                self._reject(summary, pair_id, "insufficient_balance", "Available balances cannot cover both legs")
                continue

            eligible = {
                "pairId": pair_id,
                "polyTitle": pair["polyTitle"],
                "kalshiTitle": pair["kalshiTitle"],
                "direction": sized_trade["direction"],
                "netEdgePct": sized_trade["netEdge"] * 100,
                "grossEdgePct": sized_trade["grossEdge"] * 100,
                "contracts": contracts,
                "depthMode": depth_info["mode"],
            }
            if depth_info["mode"] == "partial":
                eligible["partialDepthContracts"] = depth_info["bufferedContracts"]
            summary["eligible"].append(eligible)
            summary["eligibleCount"] += 1

            if not allow_execute:
                continue

            try:
                position = await self._open_position(settings, pair, poly, kalshi, sized_trade)
                open_positions.append(position)
                blocking_positions.append(position)
                open_count += 1
                current_capital += float(position.get("totalCapital") or 0)
                summary["openedCount"] += 1
                summary["executions"].append(
                    {
                        "type": "open",
                        "pairId": pair_id,
                        "positionId": position["id"],
                        "contracts": position.get("contracts"),
                        "requestedContracts": position.get("requestedContracts", position.get("contracts")),
                        "remainingContracts": position.get("remainingContracts", 0),
                    }
                )
                store.set_state("bot_consecutive_failures", 0)
            except Exception as exc:
                if self._is_recoverable_open_miss(exc):
                    self._record_recoverable_open_miss(summary, pair_id, exc)
                    continue
                await self._record_error(settings, summary, "trade_open_failed", str(exc), {"pairId": pair_id})

    async def _evaluate_closes(
        self,
        settings: dict[str, Any],
        summary: dict[str, Any],
        open_positions: list[dict[str, Any]],
        poly_by_id: dict[str, dict[str, Any]],
        kalshi_by_id: dict[str, dict[str, Any]],
        allow_execute: bool,
    ) -> None:
        if not settings.get("close_enabled", True):
            return

        for position in open_positions:
            poly = poly_by_id.get(position.get("polyId"))
            kalshi = kalshi_by_id.get(position.get("kalshiId"))
            if not poly or not kalshi:
                self._reject(summary, position.get("id", ""), "missing_position_market", "Position live data unavailable")
                continue
            close_eval = self._close_reason(settings, position, poly, kalshi)
            if not close_eval:
                continue
            summary["eligible"].append(
                {
                    "type": "close",
                    "positionId": position["id"],
                    "reason": close_eval["reason"],
                    "unrealizedPct": close_eval["unrealized"]["pct"],
                    **({"spreadMultiple": close_eval["spreadMultiple"]} if "spreadMultiple" in close_eval else {}),
                }
            )
            summary["eligibleCount"] += 1
            if not allow_execute:
                continue
            try:
                closed = await self._close_position(settings, position, poly, kalshi, close_eval)
                summary["closedCount"] += 1
                summary["executions"].append({"type": "close", "positionId": closed["id"], "reason": close_eval["reason"]})
                store.set_state("bot_consecutive_failures", 0)
            except Exception as exc:
                await self._record_error(
                    settings,
                    summary,
                    "trade_close_failed",
                    str(exc),
                    {"positionId": position.get("id")},
                )

    def _blocks_new_open(self, settings: dict[str, Any], position: dict[str, Any]) -> bool:
        if settings.get("mode") == "on" and position.get("executionMode") == "paper":
            return False
        return True

    async def _open_position(
        self,
        settings: dict[str, Any],
        pair: dict[str, Any],
        poly: dict[str, Any],
        kalshi: dict[str, Any],
        trade: dict[str, Any],
    ) -> dict[str, Any]:
        mode = settings["mode"]
        open_meta: dict[str, Any] = {}
        if mode == "on":
            claimed, claim_reason, claim_owner = store.try_claim_live_pair(pair["polyId"], pair["kalshiId"])
            if not claimed:
                raise RuntimeError(f"Pair already has a live open position or in-flight open claim ({claim_reason})")
            try:
                live_trade, open_meta = await self._execute_live_open_with_follow_up(settings, pair, poly, kalshi, trade)
            except Exception:
                store.release_live_pair_claim(pair["polyId"], pair["kalshiId"], claim_owner)
                raise
            position = position_from_trade(pair, poly, kalshi, live_trade, source="bot", execution_mode="live")
            position.update(open_meta)
            position = store.upsert_position(position)
            log_type = "trade_opened"
        else:
            position = position_from_trade(pair, poly, kalshi, trade, source="bot", execution_mode="paper")
            position = store.upsert_position(position)
            log_type = "paper_trade_opened"

        net_edge_pct = float(open_meta.get("openNetEdgePct", trade["netEdge"] * 100))
        gross_edge_pct = float(open_meta.get("openGrossEdgePct", trade["grossEdge"] * 100))
        store.log(
            "info",
            log_type,
            f"Opened {mode.upper()} bot position: {pair['polyTitle']} / {pair['kalshiTitle']}",
            {
                "positionId": position["id"],
                "pairId": pair["id"],
                "netEdgePct": net_edge_pct,
                "grossEdgePct": gross_edge_pct,
                "contracts": position["contracts"],
                "requestedContracts": open_meta.get("requestedContracts", trade["contracts"]),
                "remainingContracts": open_meta.get("remainingContracts", 0),
            },
        )
        await self._notify(
            settings,
            "notify_trade_opened",
            (
                f"Opened {mode.upper()} position\n"
                f"{pair['polyTitle']} / {pair['kalshiTitle']}\n"
                f"Net edge: {net_edge_pct:.2f}% | Contracts: {position['contracts']:.0f}"
            ),
        )
        return position

    async def _execute_live_open_with_follow_up(
        self,
        settings: dict[str, Any],
        pair: dict[str, Any],
        poly: dict[str, Any],
        kalshi: dict[str, Any],
        trade: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        requested_contracts = math.floor(float(trade["contracts"]))
        if requested_contracts < 1:
            raise RuntimeError("Live open size rounds below 1 contract")

        remaining_contracts = requested_contracts
        chunks: list[dict[str, Any]] = []
        chunk_meta: list[dict[str, Any]] = []
        follow_up_status = "not_needed"
        current_poly = poly
        current_kalshi = kalshi

        for attempt in range(1, BOT_OPEN_MAX_ATTEMPTS + 1):
            if remaining_contracts < 1:
                follow_up_status = "completed" if len(chunks) > 1 else "not_needed"
                break

            if attempt > 1:
                fresh_markets = await self._fresh_open_markets(pair)
                if not fresh_markets:
                    follow_up_status = "skipped: fresh_market_data_unavailable"
                    break
                current_poly, current_kalshi = fresh_markets
                if self._too_close_to_expiry(settings, current_poly, current_kalshi):
                    follow_up_status = "skipped: near_expiry"
                    break

            attempt_trade = calc_trade_for_contracts(
                trade["direction"],
                side_ask(current_poly, trade["polyAction"]),
                side_ask(current_kalshi, trade["kalshiAction"]),
                remaining_contracts,
                float(settings["poly_fee"]),
                float(settings["kalshi_fee"]),
            )
            if not attempt_trade:
                follow_up_status = "skipped: missing_prices"
                break
            if float(attempt_trade["netEdge"]) < float(settings["min_net_edge"]):
                follow_up_status = "skipped: edge_below_threshold"
                break
            min_contracts = math.ceil(1.0 / float(attempt_trade["polyAsk"]))
            if remaining_contracts < min_contracts:
                follow_up_status = "skipped: remaining_below_polymarket_minimum"
                break
            if attempt > 1:
                balances_ok, balance_reason = await self._fresh_balances_cover_trade(attempt_trade)
                if not balances_ok:
                    follow_up_status = f"skipped: {balance_reason}"
                    break

            try:
                result = await execute_open_trade(
                    OpenTradeRequest(
                        poly_id=pair["polyId"],
                        kalshi_id=pair["kalshiId"],
                        poly_action=trade["polyAction"],
                        kalshi_action=trade["kalshiAction"],
                        contracts=Decimal(str(remaining_contracts)),
                        max_poly_price=Decimal(str(attempt_trade["polyAsk"])),
                        max_kalshi_price=Decimal(str(attempt_trade["kalshiAsk"])),
                        max_total_cost=Decimal(str(attempt_trade["totalCost"])),
                        first_venue="auto",
                        market_data_max_age_ms=int(settings.get("market_data_max_age_ms") or 1000),
                        execution_strategy=settings.get("execution_strategy", "sequential"),
                        max_leg_slippage=Decimal(str(settings.get("max_leg_slippage") or 0.01)),
                        liquidity_buffer=BOT_OPEN_LIQUIDITY_BUFFER,
                        allow_shrink=True,
                    )
                )
            except Exception as exc:
                if not chunks:
                    raise
                follow_up_status = f"failed: {exc}"
                store.log(
                    "warning",
                    "trade_open_follow_up_failed",
                    f"Follow-up open failed after partial fill: {exc}",
                    {"pairId": pair["id"], "remainingContracts": remaining_contracts},
                )
                break

            filled_contracts = math.floor(float(result.get("contracts") or 0))
            if filled_contracts < 1:
                if not chunks:
                    raise RuntimeError("Live open returned zero filled contracts")
                follow_up_status = "failed: zero_contract_fill"
                break

            filled_trade = calc_trade_for_contracts(
                trade["direction"],
                float(result.get("poly", {}).get("price") or attempt_trade["polyAsk"]),
                float(result.get("kalshi", {}).get("price") or attempt_trade["kalshiAsk"]),
                filled_contracts,
                float(settings["poly_fee"]),
                float(settings["kalshi_fee"]),
            )
            if not filled_trade:
                if not chunks:
                    raise RuntimeError("Live open fill could not be converted into a position")
                follow_up_status = "failed: fill_accounting_failed"
                break

            remaining_contracts = max(0, remaining_contracts - filled_contracts)
            chunks.append({"trade": filled_trade, "result": result, "attempt": attempt})
            chunk_meta.append(
                {
                    "attempt": attempt,
                    "requestedContracts": result.get("requested_contracts", str(filled_contracts + remaining_contracts)),
                    "contracts": filled_contracts,
                    "remainingContracts": remaining_contracts,
                    "polyPrice": filled_trade["polyAsk"],
                    "kalshiPrice": filled_trade["kalshiAsk"],
                    "grossSpend": result.get("gross_spend"),
                    "shrinkApplied": bool(result.get("shrink_applied")),
                    "liquidityGuard": result.get("liquidity_guard", {}),
                }
            )
            self._balance_cache = None

            if remaining_contracts > 0 and attempt == 1:
                follow_up_status = "pending"
                store.log(
                    "info",
                    "trade_open_partial",
                    (
                        f"Opened {filled_contracts} of {requested_contracts} requested contracts; "
                        f"trying follow-up for {remaining_contracts}."
                    ),
                    {
                        "pairId": pair["id"],
                        "filledContracts": filled_contracts,
                        "requestedContracts": requested_contracts,
                        "remainingContracts": remaining_contracts,
                    },
                )

        if not chunks:
            raise RuntimeError(f"Live open skipped before any fill ({follow_up_status})")

        aggregate = self._aggregate_open_chunks(trade, chunks)
        partial_open = int(round(float(aggregate["contracts"]))) < requested_contracts
        if not partial_open and follow_up_status == "pending":
            follow_up_status = "completed"
        if partial_open and follow_up_status in {"not_needed", "pending"}:
            follow_up_status = "partial_remaining"
        return aggregate, {
            "requestedContracts": requested_contracts,
            "remainingContracts": max(0, requested_contracts - int(round(float(aggregate["contracts"])))),
            "partialOpen": partial_open,
            "followUpStatus": follow_up_status,
            "openChunks": chunk_meta,
            "openNetEdgePct": aggregate["netEdge"] * 100,
            "openGrossEdgePct": aggregate["grossEdge"] * 100,
        }

    async def _fresh_open_markets(self, pair: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        poly_markets, kalshi_markets = await market_stream.get_pair_markets(
            [pair["polyId"]],
            [pair["kalshiId"]],
            wait_timeout=0.2,
        )
        poly = next((market for market in poly_markets if market.get("id") == pair["polyId"]), None)
        kalshi = next((market for market in kalshi_markets if market.get("id") == pair["kalshiId"]), None)
        if not poly or not kalshi:
            return None
        return poly, kalshi

    async def _fresh_balances_cover_trade(self, trade: dict[str, Any]) -> tuple[bool, str]:
        try:
            poly_balance, kalshi_balance = await asyncio.gather(poly_data.fetch_balance(), kalshi_data.fetch_balance())
        except Exception as exc:
            return False, f"balance_check_failed ({exc})"
        balances = {"polymarket": poly_balance, "kalshi": kalshi_balance}
        if poly_balance.get("error") or kalshi_balance.get("error"):
            return False, "balance_check_failed"
        if not self._balances_cover(balances, trade):
            return False, "insufficient_balance"
        return True, "ok"

    @staticmethod
    def _aggregate_open_chunks(base_trade: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
        trades = [chunk["trade"] for chunk in chunks]
        contracts = sum(float(trade["contracts"]) for trade in trades)
        if contracts <= 0:
            return base_trade

        poly_ask = sum(float(trade["polyAsk"]) * float(trade["contracts"]) for trade in trades) / contracts
        kalshi_ask = sum(float(trade["kalshiAsk"]) * float(trade["contracts"]) for trade in trades) / contracts
        poly_spend = sum(float(trade["polySpend"]) for trade in trades)
        kalshi_spend = sum(float(trade["kalshiSpend"]) for trade in trades)
        total_cost = sum(float(trade["totalCost"]) for trade in trades)
        poly_fees_paid = sum(float(trade["polyFeesPaid"]) for trade in trades)
        kalshi_fees_paid = sum(float(trade["kalshiFeesPaid"]) for trade in trades)
        total_fees = poly_fees_paid + kalshi_fees_paid
        gross_profit = sum(float(trade["grossProfit"]) for trade in trades)
        net_profit = sum(float(trade["netProfit"]) for trade in trades)
        return {
            "direction": base_trade["direction"],
            "polyAction": base_trade["polyAction"],
            "kalshiAction": base_trade["kalshiAction"],
            "polyAsk": poly_ask,
            "kalshiAsk": kalshi_ask,
            "grossEdge": gross_profit / contracts,
            "netEdge": net_profit / contracts,
            "contracts": contracts,
            "polySpend": poly_spend,
            "kalshiSpend": kalshi_spend,
            "totalCost": total_cost,
            "polyFeesPaid": poly_fees_paid,
            "kalshiFeesPaid": kalshi_fees_paid,
            "totalFees": total_fees,
            "grossProfit": gross_profit,
            "grossProfitPct": (gross_profit / total_cost) * 100 if total_cost > 0 else 0,
            "netProfit": net_profit,
            "netProfitPct": (net_profit / total_cost) * 100 if total_cost > 0 else 0,
        }

    async def _close_position(
        self,
        settings: dict[str, Any],
        position: dict[str, Any],
        poly: dict[str, Any],
        kalshi: dict[str, Any],
        close_eval: dict[str, Any],
    ) -> dict[str, Any]:
        unrealized = close_eval["unrealized"]
        mode = settings["mode"]
        poly_bid = unrealized["polyBid"]
        kalshi_bid = unrealized["kalshiBid"]
        if mode == "on":
            await execute_close_trade(
                CloseTradeRequest(
                    poly_id=position["polyId"],
                    kalshi_id=position["kalshiId"],
                    poly_action=position["polyAction"],
                    kalshi_action=position["kalshiAction"],
                    contracts=Decimal(str(position["contracts"])),
                    min_poly_price=Decimal(str(poly_bid)),
                    min_kalshi_price=Decimal(str(kalshi_bid)),
                    min_total_proceeds=Decimal("0"),
                    first_venue="auto",
                    market_data_max_age_ms=int(settings.get("market_data_max_age_ms") or 1000),
                    execution_strategy=settings.get("execution_strategy", "sequential"),
                )
            )
            log_type = "trade_closed"
        else:
            log_type = "paper_trade_closed"

        closed = store.close_position(position["id"], float(unrealized["pnl"]), close_eval["reason"])
        if not closed:
            raise RuntimeError(f"Position not found: {position['id']}")
        store.log(
            "info",
            log_type,
            f"Closed {mode.upper()} bot position: {close_eval['reason']}",
            {
                "positionId": position["id"],
                "realizedPnl": unrealized["pnl"],
                "realizedPct": unrealized["pct"],
            },
        )
        await self._notify(
            settings,
            "notify_trade_closed",
            (
                f"Closed {mode.upper()} position\n"
                f"Reason: {close_eval['reason']}\n"
                f"P&L: {unrealized['pnl']:.2f} ({unrealized['pct']:.2f}%)"
            ),
        )
        return closed

    def _close_reason(
        self,
        settings: dict[str, Any],
        position: dict[str, Any],
        poly: dict[str, Any],
        kalshi: dict[str, Any],
    ) -> dict[str, Any] | None:
        unrealized = calc_unrealized_pnl(
            position,
            poly,
            kalshi,
            float(settings["poly_fee"]),
            float(settings["kalshi_fee"]),
        )
        if unrealized["polyBid"] <= 0 or unrealized["kalshiBid"] <= 0:
            return None
        pct = unrealized["pct"]
        if settings.get("take_profit_enabled") and pct >= float(settings["take_profit_pct"]):
            return {"reason": "take_profit", "unrealized": unrealized}
        if settings.get("stop_loss_enabled") and pct <= float(settings["stop_loss_pct"]):
            return {"reason": "stop_loss", "unrealized": unrealized}
        spread_multiple = self._close_spread_multiple(position, unrealized)
        if (
            settings.get("spread_multiple_close_enabled")
            and spread_multiple is not None
            and spread_multiple >= float(settings["spread_multiple_close"])
        ):
            return {
                "reason": "spread_multiple",
                "unrealized": unrealized,
                "spreadMultiple": spread_multiple,
            }
        hours = hours_to_earliest_close(poly, kalshi)
        if (
            settings.get("close_before_close_enabled")
            and hours is not None
            and hours <= float(settings["close_before_close_hours"])
        ):
            return {"reason": "close_before_expiry", "unrealized": unrealized}
        if settings.get("edge_reversion_enabled") and pct >= float(settings["min_close_profit_pct"]):
            edges = compute_edges(poly, kalshi, float(settings["poly_fee"]), float(settings["kalshi_fee"]))
            current = next((edge for edge in edges if edge["direction"] == position.get("direction")), None)
            if current and current["netEdge"] * 100 <= float(settings["close_edge_below_pct"]):
                return {"reason": "edge_reversion", "unrealized": unrealized}
        return None

    @staticmethod
    def _close_spread_multiple(position: dict[str, Any], unrealized: dict[str, float]) -> float | None:
        contracts = float(position.get("contracts") or 0)
        if contracts <= 0:
            return None
        initial_spread = float(position.get("lockedProfit") or 0) / contracts
        if initial_spread <= 0:
            return None
        current_close_spread = float(unrealized.get("pnl") or 0) / contracts
        return current_close_spread / initial_spread

    async def _depth_ok(
        self,
        settings: dict[str, Any],
        pair: dict[str, Any],
        trade: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            depth = await market_stream.analyse_depth(
                pair["polyId"],
                pair["kalshiId"],
                edge_threshold=0,
                max_leg_slippage=float(settings["max_leg_slippage"]),
                max_bet_dollars=float(settings["fixed_trade_dollars"]),
            )
        except Exception as exc:
            self._reject(summary, pair["id"], "depth_failed", f"Depth scan failed: {exc}")
            return None

        direction_depth = depth.get(trade["direction"]) or {}
        ideal_bet = float(direction_depth.get("ideal_bet") or 0)
        max_shares = float(direction_depth.get("max_shares") or 0)
        buffered_contracts = math.floor(max_shares * float(BOT_OPEN_LIQUIDITY_BUFFER))
        min_contracts = math.ceil(1.0 / float(trade["polyAsk"])) if float(trade["polyAsk"]) > 0 else math.inf
        if ideal_bet >= float(settings["fixed_trade_dollars"]):
            return {
                "mode": "full",
                "idealBet": ideal_bet,
                "bufferedContracts": buffered_contracts,
                "minContracts": min_contracts,
            }
        if buffered_contracts >= min_contracts:
            return {
                "mode": "partial",
                "idealBet": ideal_bet,
                "bufferedContracts": buffered_contracts,
                "minContracts": min_contracts,
            }

        if max_shares > 0:
            message = (
                "Order-book depth can only support a partial size below the Polymarket minimum "
                f"({buffered_contracts} buffered contracts, needs {min_contracts})"
            )
        else:
            message = "Order-book depth is below the fixed trade size at configured slippage"
        self._reject(
            summary,
            pair["id"],
            "insufficient_depth",
            message,
            {"depth": depth, "bufferedContracts": buffered_contracts, "minContracts": min_contracts},
        )
        return None

    async def _fetch_balances(self, settings: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any] | None:
        ttl = float(settings.get("balance_cache_seconds") or 0)
        now = asyncio.get_running_loop().time()
        if ttl > 0 and self._balance_cache:
            cached_at, cached_balances = self._balance_cache
            if now - cached_at <= ttl:
                return cached_balances
        try:
            poly, kalshi = await asyncio.gather(poly_data.fetch_balance(), kalshi_data.fetch_balance())
        except Exception as exc:
            await self._record_error(settings, summary, "balance_check_failed", str(exc))
            return None
        balances = {"polymarket": poly, "kalshi": kalshi}
        if poly.get("error") or kalshi.get("error"):
            await self._record_error(settings, summary, "balance_check_failed", "One or both balances returned errors", balances)
            return None
        if ttl > 0:
            self._balance_cache = (now, balances)
        return balances

    def _balances_cover(self, balances: dict[str, Any], trade: dict[str, Any]) -> bool:
        poly_available = float(balances.get("polymarket", {}).get("available") or 0)
        kalshi_available = float(balances.get("kalshi", {}).get("available") or 0)
        return poly_available >= float(trade["polySpend"]) and kalshi_available >= float(trade["kalshiSpend"])

    def _too_close_to_expiry(self, settings: dict[str, Any], poly: dict[str, Any], kalshi: dict[str, Any]) -> bool:
        min_hours = float(settings["min_hours_to_close"])
        if min_hours <= 0:
            return False
        hours = hours_to_earliest_close(poly, kalshi)
        return hours is not None and hours < min_hours

    async def _circuit_breaker_hit(self, settings: dict[str, Any], summary: dict[str, Any]) -> bool:
        max_daily_trades = int(settings.get("max_daily_trades") or 0)
        if max_daily_trades > 0 and store.daily_trade_count() >= max_daily_trades:
            await self._trip(settings, summary, "max_daily_trades", "Daily trade limit reached")
            return True
        max_daily_loss = float(settings.get("max_daily_loss") or 0)
        if max_daily_loss > 0 and store.daily_realized_pnl() <= -max_daily_loss:
            await self._trip(settings, summary, "max_daily_loss", "Daily realized loss limit reached")
            return True
        max_failures = int(settings.get("max_consecutive_failures") or 0)
        failures = int(store.get_state("bot_consecutive_failures", 0) or 0)
        if max_failures > 0 and failures >= max_failures:
            await self._trip(settings, summary, "max_consecutive_failures", "Consecutive failure limit reached")
            return True
        return False

    async def _record_error(
        self,
        settings: dict[str, Any],
        summary: dict[str, Any],
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        summary["errorCount"] += 1
        summary["errors"].append({"eventType": event_type, "message": message, "data": data or {}})
        failures = int(store.get_state("bot_consecutive_failures", 0) or 0) + 1
        store.set_state("bot_consecutive_failures", failures)
        store.log("error", event_type, message, data or {})
        await self._notify(settings, "notify_trade_failed", f"Bot error: {message}")
        max_failures = int(settings.get("max_consecutive_failures") or 0)
        if max_failures > 0 and failures >= max_failures:
            await self._trip(settings, summary, "max_consecutive_failures", "Consecutive failure limit reached")
        elif settings.get("stop_on_api_error") and event_type in {"market_fetch_failed", "balance_check_failed"}:
            await self._trip(settings, summary, event_type, message)

    def _record_recoverable_open_miss(self, summary: dict[str, Any], pair_id: str, exc: Exception) -> None:
        message = str(exc)
        self._reject(
            summary,
            pair_id,
            "execution_liquidity_miss",
            "Live entry was skipped because the order book moved or thinned before execution",
            {"error": message},
        )
        store.log(
            "warning",
            "trade_open_liquidity_miss",
            "Live entry skipped; pair remains eligible for future scans if conditions return",
            {"pairId": pair_id, "error": message},
        )

    @staticmethod
    def _is_recoverable_open_miss(exc: Exception) -> bool:
        message = str(exc).lower()
        if any(marker in message for marker in ("after polymarket fill", "after kalshi fill", "unwind", "concurrent open failed")):
            return False
        return any(
            marker in message
            for marker in (
                "fresh order-book depth is too thin",
                "buffered executable size",
                "insufficient liquidity",
                "insufficient depth",
                "resting volume",
                "fill_or_kill",
                "fok",
                "minimum market-order spend",
                "liquidity vanished",
                "below the polymarket minimum",
            )
        )

    async def _trip(self, settings: dict[str, Any], summary: dict[str, Any], reason: str, message: str) -> None:
        if settings.get("mode") != "off":
            store.set_bot_mode("off")
        summary["errors"].append({"eventType": "circuit_breaker", "message": message, "data": {"reason": reason}})
        store.log("error", "circuit_breaker", message, {"reason": reason})
        await self._notify(settings, "notify_circuit_breaker", f"Circuit breaker: {message}")

    def _reject(
        self,
        summary: dict[str, Any],
        item_id: str,
        code: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        summary["rejectedCount"] += 1
        summary["rejections"].append(
            {
                "id": item_id,
                "code": code,
                "message": message,
                "data": data or {},
            }
        )

    def _finish_scan(self, started_at: str, summary: dict[str, Any]) -> None:
        finished_at = utc_now()
        summary["startedAt"] = started_at
        summary["finishedAt"] = finished_at
        self._set_runtime(last_scan_at=finished_at, last_summary=summary)
        store.log(
            "info",
            "scan_completed",
            (
                f"Scan completed: {summary['eligibleCount']} eligible, "
                f"{summary['openedCount']} opened, {summary['closedCount']} closed, "
                f"{summary['rejectedCount']} rejected"
            ),
            {
                "eligibleCount": summary["eligibleCount"],
                "openedCount": summary["openedCount"],
                "closedCount": summary["closedCount"],
                "rejectedCount": summary["rejectedCount"],
                "errorCount": summary["errorCount"],
            },
        )

    def _set_runtime(
        self,
        *,
        last_scan_at: str | None = None,
        next_scan_at: str | None | object = _UNSET,
        last_summary: dict[str, Any] | None = None,
    ) -> None:
        state = store.get_state("bot_runtime", {}) or {}
        if last_scan_at is not None:
            state["lastScanAt"] = last_scan_at
        if next_scan_at is not _UNSET:
            state["nextScanAt"] = next_scan_at
        if last_summary is not None:
            state["lastSummary"] = last_summary
        store.set_state("bot_runtime", state)

    async def _notify(self, settings: dict[str, Any], flag: str, message: str) -> None:
        if not settings.get(flag, True):
            return
        await self._send_telegram(settings, message)

    async def _send_telegram(self, settings: dict[str, Any], message: str) -> None:
        if not settings.get("telegram_enabled"):
            return
        token = str(settings.get("telegram_bot_token") or "").strip()
        chat_id = str(settings.get("telegram_chat_id") or "").strip()
        if not token or not chat_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message},
                )
                resp.raise_for_status()
        except Exception as exc:
            store.log("warning", "telegram_failed", f"Telegram notification failed: {exc}", {})

    @staticmethod
    def _scan_scope(
        pairs: list[dict[str, Any]],
        open_positions: list[dict[str, Any]],
        market_change: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        if not market_change or market_change.get("all"):
            return pairs, open_positions, False
        changed_poly = {str(item) for item in market_change.get("polyIds", [])}
        changed_kalshi = {str(item) for item in market_change.get("kalshiIds", [])}
        if not changed_poly and not changed_kalshi:
            return pairs, open_positions, False

        def touched(item: dict[str, Any]) -> bool:
            return str(item.get("polyId")) in changed_poly or str(item.get("kalshiId")) in changed_kalshi

        return [pair for pair in pairs if touched(pair)], [pos for pos in open_positions if touched(pos)], True

    @staticmethod
    def _merge_market_changes(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
        return {
            "changed": bool(first.get("changed") or second.get("changed")),
            "sequence": max(int(first.get("sequence") or 0), int(second.get("sequence") or 0)),
            "all": bool(first.get("all") or second.get("all")),
            "polyIds": sorted({*(first.get("polyIds") or []), *(second.get("polyIds") or [])}),
            "kalshiIds": sorted({*(first.get("kalshiIds") or []), *(second.get("kalshiIds") or [])}),
        }

    @staticmethod
    def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
        public = dict(settings)
        public["telegramConfigured"] = bool(public.get("telegram_bot_token") and public.get("telegram_chat_id"))
        public["telegram_bot_token"] = ""
        return public


bot_controller = BotController()
