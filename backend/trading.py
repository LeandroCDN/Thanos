import asyncio
import importlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

import kalshi as kalshi_data
import polymarket as poly_data
from market_stream import market_stream

EXECUTION_VERSION = "open-close-v10-live-pair-claim"
ExecutionStrategy = Literal["sequential", "concurrent_fok"]
_POLY_TOKEN_CACHE: dict[tuple[str, str], str] = {}
DEFAULT_OPEN_MAX_LEG_SLIPPAGE = Decimal("0.01")
DEFAULT_OPEN_LIQUIDITY_BUFFER = Decimal("0.85")


class OpenTradeRequest(BaseModel):
    poly_id: str
    kalshi_id: str
    poly_action: Literal["YES", "NO"]
    kalshi_action: Literal["YES", "NO"]
    contracts: Decimal = Field(gt=0)
    max_poly_price: Decimal = Field(gt=0, lt=1)
    max_kalshi_price: Decimal = Field(gt=0, lt=1)
    max_total_cost: Decimal = Field(gt=0)
    first_venue: Literal["auto", "polymarket", "kalshi"] = "auto"
    market_data_max_age_ms: int = Field(default=1000, ge=100, le=60_000)
    execution_strategy: ExecutionStrategy = "sequential"
    max_leg_slippage: Decimal = Field(default=DEFAULT_OPEN_MAX_LEG_SLIPPAGE, ge=0, le=Decimal("0.5"))
    liquidity_buffer: Decimal = Field(default=DEFAULT_OPEN_LIQUIDITY_BUFFER, ge=Decimal("0.1"), le=Decimal("1"))
    allow_shrink: bool = False


class CloseTradeRequest(BaseModel):
    poly_id: str
    kalshi_id: str
    poly_action: Literal["YES", "NO"]
    kalshi_action: Literal["YES", "NO"]
    contracts: Decimal = Field(gt=0)
    min_poly_price: Decimal = Field(gt=0, lt=1)
    min_kalshi_price: Decimal = Field(gt=0, lt=1)
    min_total_proceeds: Decimal = Field(ge=0)
    first_venue: Literal["auto", "polymarket", "kalshi"] = "auto"
    market_data_max_age_ms: int = Field(default=1000, ge=100, le=60_000)
    execution_strategy: ExecutionStrategy = "sequential"


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _count(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_DOWN)


def _price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


async def execute_open_trade(req: OpenTradeRequest) -> dict:
    requested_contracts = _count(req.contracts)
    poly_markets, kalshi_markets = await _fresh_pair_markets(
        req.poly_id,
        req.kalshi_id,
        req.market_data_max_age_ms,
    )
    if not poly_markets:
        raise ValueError(f"Polymarket market not found: {req.poly_id}")
    if not kalshi_markets:
        raise ValueError(f"Kalshi market not found: {req.kalshi_id}")
    guarded_req, liquidity_guard = await _guard_open_trade_liquidity(req, poly_markets[0], kalshi_markets[0])
    try:
        result = await asyncio.to_thread(_execute_open_trade_sync, guarded_req, poly_markets[0], kalshi_markets[0])
    except Exception as exc:
        if not _can_retry_open_after_liquidity_rejection(exc, guarded_req):
            raise
        retry_req, retry_guard = await _build_liquidity_retry_request(guarded_req, poly_markets[0], kalshi_markets[0])
        try:
            result = await asyncio.to_thread(_execute_open_trade_sync, retry_req, poly_markets[0], kalshi_markets[0])
        except Exception as retry_exc:
            raise RuntimeError(f"{exc}. Smaller retry also failed: {retry_exc}") from retry_exc
        liquidity_guard = {
            **(liquidity_guard or {}),
            "retry": retry_guard,
        }
    filled_contracts = _count(Decimal(str(result.get("contracts") or "0")))
    remaining_contracts = max(Decimal("0"), requested_contracts - filled_contracts)
    result["requested_contracts"] = str(requested_contracts)
    result["remaining_contracts"] = str(remaining_contracts)
    result["shrink_applied"] = remaining_contracts > 0
    if liquidity_guard:
        result["liquidity_guard"] = liquidity_guard
    return result


async def execute_close_trade(req: CloseTradeRequest) -> dict:
    poly_markets, kalshi_markets = await _fresh_pair_markets(
        req.poly_id,
        req.kalshi_id,
        req.market_data_max_age_ms,
    )
    if not poly_markets:
        raise ValueError(f"Polymarket market not found: {req.poly_id}")
    if not kalshi_markets:
        raise ValueError(f"Kalshi market not found: {req.kalshi_id}")
    return await asyncio.to_thread(_execute_close_trade_sync, req, poly_markets[0], kalshi_markets[0])


def _execute_open_trade_sync(req: OpenTradeRequest, poly_market: dict, kalshi_market: dict) -> dict:
    _remember_poly_market_tokens(poly_market)
    contracts = _count(req.contracts)
    if contracts <= 0:
        raise ValueError("Trade size rounds below 1 whole contract")

    poly_ask = _side_ask(poly_market, req.poly_action)
    kalshi_ask = _side_ask(kalshi_market, req.kalshi_action)
    if poly_ask <= 0:
        raise ValueError(f"Polymarket {req.poly_action} ask is unavailable")
    if kalshi_ask <= 0:
        raise ValueError(f"Kalshi {req.kalshi_action} ask is unavailable")
    if poly_ask > req.max_poly_price:
        raise ValueError(f"Polymarket {req.poly_action} ask moved to {poly_ask}, above max {req.max_poly_price}")
    if kalshi_ask > req.max_kalshi_price:
        raise ValueError(f"Kalshi {req.kalshi_action} ask moved to {kalshi_ask}, above max {req.max_kalshi_price}")

    poly_spend = _money(contracts * poly_ask)
    kalshi_spend = _money(contracts * kalshi_ask)
    total_spend = poly_spend + kalshi_spend
    if total_spend > req.max_total_cost:
        raise ValueError(f"Current gross spend {total_spend} exceeds max {req.max_total_cost}")
    if poly_spend < Decimal("1.00"):
        min_contracts = (Decimal("1.00") / poly_ask).to_integral_value(rounding=ROUND_CEILING)
        min_total = _money(min_contracts * (poly_ask + kalshi_ask))
        raise ValueError(
            f"Minimum for this pair is {min_contracts} contracts, "
            f"about ${min_total} total at current prices before fees. "
            f"Polymarket marketable orders require at least about $1.00; "
            f"this leg is only ${poly_spend}."
        )

    max_poly_spend = _money(req.max_total_cost - kalshi_spend)
    if max_poly_spend < poly_spend:
        raise ValueError(f"Polymarket spend cap {max_poly_spend} is below required spend {poly_spend}")

    first_venue = _resolve_first_venue(req.first_venue, poly_market, kalshi_market)

    if req.execution_strategy == "concurrent_fok":
        poly_result, kalshi_result = _execute_concurrent_open(
            req,
            contracts,
            poly_spend,
            poly_ask,
            max_poly_spend,
        )
        first_venue = "concurrent_fok"
    elif first_venue == "polymarket":
        poly_result = _place_poly_buy(
            req.poly_id,
            req.poly_action,
            contracts,
            poly_spend,
            poly_ask,
            max_poly_spend,
        )
        try:
            kalshi_result = _place_kalshi_buy(
                req.kalshi_id,
                req.kalshi_action,
                contracts,
                req.max_kalshi_price,
            )
        except Exception as exc:
            unwind = _try_unwind_poly(req.poly_id, req.poly_action, contracts)
            raise RuntimeError(
                f"Kalshi leg failed after Polymarket fill: {exc}. "
                f"Polymarket unwind attempted: {unwind}"
            ) from exc
    else:
        kalshi_result = _place_kalshi_buy(
            req.kalshi_id,
            req.kalshi_action,
            contracts,
            req.max_kalshi_price,
        )
        try:
            poly_result = _place_poly_buy(
                req.poly_id,
                req.poly_action,
                contracts,
                poly_spend,
                poly_ask,
                max_poly_spend,
            )
        except Exception as exc:
            unwind = _try_unwind_kalshi(req.kalshi_id, req.kalshi_action, contracts)
            raise RuntimeError(
                f"Polymarket leg failed after Kalshi fill: {exc}. "
                f"Kalshi unwind attempted: {unwind}"
            ) from exc

    return {
        "status": "opened",
        "execution_version": EXECUTION_VERSION,
        "first_venue": first_venue,
        "contracts": str(contracts),
        "poly": {
            "market_id": req.poly_id,
            "action": req.poly_action,
            "price": str(poly_ask),
            "spend": str(poly_spend),
            "order": poly_result,
        },
        "kalshi": {
            "ticker": req.kalshi_id,
            "action": req.kalshi_action,
            "price": str(kalshi_ask),
            "spend": str(kalshi_spend),
            "order": kalshi_result,
        },
        "gross_spend": str(total_spend),
    }


def _execute_close_trade_sync(req: CloseTradeRequest, poly_market: dict, kalshi_market: dict) -> dict:
    _remember_poly_market_tokens(poly_market)
    contracts = _count(req.contracts)
    if contracts <= 0:
        raise ValueError("Trade size rounds below 1 whole contract")

    poly_bid = _side_bid(poly_market, req.poly_action)
    kalshi_bid = _side_bid(kalshi_market, req.kalshi_action)
    if poly_bid <= 0:
        raise ValueError(f"Polymarket {req.poly_action} bid is unavailable")
    if kalshi_bid <= 0:
        raise ValueError(f"Kalshi {req.kalshi_action} bid is unavailable")
    if poly_bid < req.min_poly_price:
        raise ValueError(f"Polymarket {req.poly_action} bid moved to {poly_bid}, below min {req.min_poly_price}")
    if kalshi_bid < req.min_kalshi_price:
        raise ValueError(f"Kalshi {req.kalshi_action} bid moved to {kalshi_bid}, below min {req.min_kalshi_price}")

    poly_proceeds = _money(contracts * poly_bid)
    kalshi_proceeds = _money(contracts * kalshi_bid)
    total_proceeds = poly_proceeds + kalshi_proceeds
    if total_proceeds < req.min_total_proceeds:
        raise ValueError(f"Current gross proceeds {total_proceeds} below min {req.min_total_proceeds}")

    first_venue = _resolve_first_venue(req.first_venue, poly_market, kalshi_market)

    if req.execution_strategy == "concurrent_fok":
        poly_result, kalshi_result = _execute_concurrent_close(req, contracts, poly_bid, kalshi_bid)
        first_venue = "concurrent_fok"
    elif first_venue == "polymarket":
        poly_result = _place_poly_sell(req.poly_id, req.poly_action, contracts, req.min_poly_price)
        try:
            kalshi_result = _place_kalshi_sell(req.kalshi_id, req.kalshi_action, contracts, req.min_kalshi_price)
        except Exception as exc:
            raise RuntimeError(f"Kalshi close leg failed after Polymarket close fill: {exc}") from exc
    else:
        kalshi_result = _place_kalshi_sell(req.kalshi_id, req.kalshi_action, contracts, req.min_kalshi_price)
        try:
            poly_result = _place_poly_sell(req.poly_id, req.poly_action, contracts, req.min_poly_price)
        except Exception as exc:
            raise RuntimeError(f"Polymarket close leg failed after Kalshi close fill: {exc}") from exc

    return {
        "status": "closed",
        "execution_version": EXECUTION_VERSION,
        "first_venue": first_venue,
        "contracts": str(contracts),
        "poly": {
            "market_id": req.poly_id,
            "action": req.poly_action,
            "price": str(poly_bid),
            "gross_proceeds": str(poly_proceeds),
            "order": poly_result,
        },
        "kalshi": {
            "ticker": req.kalshi_id,
            "action": req.kalshi_action,
            "price": str(kalshi_bid),
            "gross_proceeds": str(kalshi_proceeds),
            "order": kalshi_result,
        },
        "gross_proceeds": str(total_proceeds),
    }


def _resolve_first_venue(first_venue: str, poly_market: dict, kalshi_market: dict) -> str:
    if first_venue in ("polymarket", "kalshi"):
        return first_venue
    poly_liq = Decimal(str(poly_market.get("liquidity") or "0"))
    kalshi_liq = Decimal(str(kalshi_market.get("liquidity") or "0"))
    if poly_liq <= 0 and kalshi_liq <= 0:
        return "polymarket"
    if poly_liq <= 0:
        return "polymarket"
    if kalshi_liq <= 0:
        return "kalshi"
    return "polymarket" if poly_liq <= kalshi_liq else "kalshi"


def _side_ask(market: dict, action: str) -> Decimal:
    key = "yes_ask" if action == "YES" else "no_ask"
    return Decimal(str(market.get(key) or "0"))


def _side_bid(market: dict, action: str) -> Decimal:
    key = "yes_bid" if action == "YES" else "no_bid"
    return Decimal(str(market.get(key) or "0"))


async def _guard_open_trade_liquidity(
    req: OpenTradeRequest,
    poly_market: dict,
    kalshi_market: dict,
) -> tuple[OpenTradeRequest, dict[str, Any] | None]:
    requested_contracts = _count(req.contracts)
    direction = _direction_from_actions(req.poly_action, req.kalshi_action)
    if not direction or requested_contracts <= 0:
        return req, None

    depth = await market_stream.analyse_depth(
        req.poly_id,
        req.kalshi_id,
        edge_threshold=0,
        max_leg_slippage=float(req.max_leg_slippage),
        max_bet_dollars=float(req.max_total_cost / req.liquidity_buffer),
    )
    direction_depth = depth.get(direction) or {}
    max_shares = Decimal(str(direction_depth.get("max_shares") or "0"))
    fresh_whole_contracts = _count(max_shares)
    buffered_contracts = _count(max_shares * req.liquidity_buffer)
    poly_ask = _side_ask(poly_market, req.poly_action)
    min_poly_contracts = _min_poly_contracts(poly_ask)

    guard = {
        "direction": direction,
        "source": depth.get("source", ""),
        "requestedContracts": str(requested_contracts),
        "freshWholeContracts": str(fresh_whole_contracts),
        "bufferedContracts": str(buffered_contracts),
        "liquidityBuffer": str(req.liquidity_buffer),
        "maxLegSlippage": str(req.max_leg_slippage),
        "minPolyContracts": str(min_poly_contracts),
    }

    if fresh_whole_contracts < requested_contracts:
        guard["reason"] = "fresh_depth_below_requested"
    elif buffered_contracts < requested_contracts:
        guard["reason"] = "liquidity_buffer_applied"
    else:
        return req, guard

    if not req.allow_shrink:
        raise ValueError(
            "Fresh order-book depth is too thin for this FOK size: "
            f"requested {requested_contracts} contracts, buffered executable size is {buffered_contracts}. "
            "Refresh depth or use a smaller size."
        )

    adjusted_contracts = min(requested_contracts, buffered_contracts)
    if adjusted_contracts < min_poly_contracts:
        raise ValueError(
            "Fresh order-book depth is too thin after the volatility buffer: "
            f"buffered executable size is {buffered_contracts} contracts, but this Polymarket leg needs "
            f"at least {min_poly_contracts} contracts to satisfy the minimum market-order spend."
        )

    guard["adjustedContracts"] = str(adjusted_contracts)
    return req.copy(update={"contracts": adjusted_contracts}), guard


def _direction_from_actions(poly_action: str, kalshi_action: str) -> str | None:
    if poly_action == "YES" and kalshi_action == "NO":
        return "A"
    if poly_action == "NO" and kalshi_action == "YES":
        return "B"
    return None


def _min_poly_contracts(poly_ask: Decimal) -> Decimal:
    if poly_ask <= 0:
        return Decimal("Infinity")
    return (Decimal("1.00") / poly_ask).to_integral_value(rounding=ROUND_CEILING)


def _can_retry_open_after_liquidity_rejection(exc: Exception, req: OpenTradeRequest) -> bool:
    if not req.allow_shrink:
        return False
    message = str(exc).lower()
    if "after" in message or "unwind" in message or "concurrent open failed" in message:
        return False
    return (
        "resting volume" in message
        or "insufficient liquidity" in message
        or "insufficient depth" in message
        or "fill_or_kill" in message
        or "fok" in message
    )


async def _build_liquidity_retry_request(
    req: OpenTradeRequest,
    poly_market: dict,
    kalshi_market: dict,
) -> tuple[OpenTradeRequest, dict[str, Any]]:
    current_contracts = _count(req.contracts)
    poly_ask = _side_ask(poly_market, req.poly_action)
    min_poly_contracts = _min_poly_contracts(poly_ask)
    retry_contracts = current_contracts - Decimal("1")
    if retry_contracts < min_poly_contracts:
        raise ValueError(
            "FOK liquidity vanished and the next smaller size would be below the Polymarket minimum "
            f"of {min_poly_contracts} contracts."
        )
    retry_base = req.copy(update={"contracts": retry_contracts})
    guarded_retry, retry_guard = await _guard_open_trade_liquidity(retry_base, poly_market, kalshi_market)
    retry_guard = dict(retry_guard or {})
    retry_guard["retryFromContracts"] = str(current_contracts)
    retry_guard["retryContracts"] = str(_count(guarded_retry.contracts))
    return guarded_retry, retry_guard


async def _fresh_pair_markets(
    poly_id: str,
    kalshi_id: str,
    max_age_ms: int,
) -> tuple[list[dict], list[dict]]:
    poly_markets, kalshi_markets = await market_stream.get_pair_markets([poly_id], [kalshi_id], wait_timeout=0.05)
    if (
        poly_markets
        and kalshi_markets
        and _market_is_fresh(poly_markets[0], max_age_ms)
        and _market_is_fresh(kalshi_markets[0], max_age_ms)
    ):
        return poly_markets, kalshi_markets

    fallback_poly, fallback_kalshi = await asyncio.gather(
        poly_data.fetch_markets_by_ids([poly_id]),
        kalshi_data.fetch_markets_by_tickers([kalshi_id]),
    )
    return fallback_poly or poly_markets, fallback_kalshi or kalshi_markets


def _market_is_fresh(market: dict, max_age_ms: int) -> bool:
    updated_at = str(market.get("live_updated_at") or "")
    if not updated_at:
        return False
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_ms = (datetime.now(timezone.utc) - parsed).total_seconds() * 1000
    return age_ms <= max_age_ms


def _remember_poly_market_tokens(market: dict) -> None:
    market_id = str(market.get("id") or "")
    if not market_id:
        return
    token_ids = market.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        import json

        try:
            token_ids = json.loads(token_ids)
        except Exception:
            token_ids = []
    if len(token_ids) >= 2:
        _POLY_TOKEN_CACHE[(market_id, "YES")] = str(token_ids[0])
        _POLY_TOKEN_CACHE[(market_id, "NO")] = str(token_ids[1])
    if market.get("yes_token_id"):
        _POLY_TOKEN_CACHE[(market_id, "YES")] = str(market["yes_token_id"])
    if market.get("no_token_id"):
        _POLY_TOKEN_CACHE[(market_id, "NO")] = str(market["no_token_id"])


def _poly_sdk():
    existing = sys.modules.get("polymarket")
    if existing is not None and hasattr(existing, "SecureClient"):
        return existing

    backend_dir = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
    search_paths = [
        path for path in sys.path
        if os.path.normcase(os.path.abspath(path or os.getcwd())) != backend_dir
    ]
    spec = importlib.machinery.PathFinder.find_spec("polymarket", search_paths)
    if not spec or not spec.loader:
        raise RuntimeError("official polymarket-client package is not installed")

    module = importlib.util.module_from_spec(spec)
    sys.modules["polymarket"] = module
    spec.loader.exec_module(module)
    return module


def _poly_credentials() -> tuple[str, str | None]:
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    wallet = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip() or None
    if not private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is not configured")
    return private_key, wallet


def _poly_token_id(market_id: str, action: str) -> str:
    action = action.upper()
    cached = _POLY_TOKEN_CACHE.get((market_id, action)) or market_stream.poly_token_id(market_id, action)
    if cached:
        _POLY_TOKEN_CACHE[(market_id, action)] = cached
        return cached

    raw = httpx.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=20).json()
    token_ids = raw.get("clobTokenIds")
    if isinstance(token_ids, str):
        import json

        token_ids = json.loads(token_ids)
    if not token_ids or len(token_ids) < 2:
        raise RuntimeError(f"Polymarket {market_id} did not expose CLOB token ids")
    yes_token = str(token_ids[0])
    no_token = str(token_ids[1])
    _POLY_TOKEN_CACHE[(market_id, "YES")] = yes_token
    _POLY_TOKEN_CACHE[(market_id, "NO")] = no_token
    return yes_token if action == "YES" else no_token


def _execute_concurrent_open(
    req: OpenTradeRequest,
    contracts: Decimal,
    poly_spend: Decimal,
    poly_ask: Decimal,
    max_poly_spend: Decimal,
) -> tuple[dict, dict]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "poly": executor.submit(
                _place_poly_buy,
                req.poly_id,
                req.poly_action,
                contracts,
                poly_spend,
                poly_ask,
                max_poly_spend,
            ),
            "kalshi": executor.submit(
                _place_kalshi_buy,
                req.kalshi_id,
                req.kalshi_action,
                contracts,
                req.max_kalshi_price,
            ),
        }
        results: dict[str, dict] = {}
        errors: dict[str, Exception] = {}
        for venue, future in futures.items():
            try:
                results[venue] = future.result()
            except Exception as exc:
                errors[venue] = exc

    if errors:
        unwind: dict[str, Any] = {}
        if "poly" in results and "kalshi" in errors:
            unwind["poly"] = _try_unwind_poly(req.poly_id, req.poly_action, contracts)
        if "kalshi" in results and "poly" in errors:
            unwind["kalshi"] = _try_unwind_kalshi(req.kalshi_id, req.kalshi_action, contracts)
        details = "; ".join(f"{venue}: {error}" for venue, error in errors.items())
        raise RuntimeError(f"Concurrent open failed ({details}). Unwind attempted: {unwind}")
    return results["poly"], results["kalshi"]


def _execute_concurrent_close(
    req: CloseTradeRequest,
    contracts: Decimal,
    poly_bid: Decimal,
    kalshi_bid: Decimal,
) -> tuple[dict, dict]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            "poly": executor.submit(_place_poly_sell, req.poly_id, req.poly_action, contracts, poly_bid),
            "kalshi": executor.submit(_place_kalshi_sell, req.kalshi_id, req.kalshi_action, contracts, kalshi_bid),
        }
        results: dict[str, dict] = {}
        errors: dict[str, Exception] = {}
        for venue, future in futures.items():
            try:
                results[venue] = future.result()
            except Exception as exc:
                errors[venue] = exc

    if errors:
        details = "; ".join(f"{venue}: {error}" for venue, error in errors.items())
        filled = sorted(results.keys())
        raise RuntimeError(f"Concurrent close failed ({details}). Filled venues: {filled}")
    return results["poly"], results["kalshi"]


def _place_poly_buy(
    market_id: str,
    action: str,
    contracts: Decimal,
    spend: Decimal,
    max_price: Decimal,
    max_spend: Decimal,
) -> dict:
    sdk = _poly_sdk()
    private_key, wallet = _poly_credentials()
    token_id = _poly_token_id(market_id, action)
    with sdk.SecureClient.create(private_key=private_key, wallet=wallet) as client:
        result = client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=str(spend),
            max_spend=str(max_spend),
            max_price=str(_price(max_price)),
            order_type="FOK",
        )
    if not result.ok:
        raise RuntimeError(f"Polymarket buy rejected: {result.code} {result.message}")
    return _poly_order_response(result, filled_shares=contracts, filled_cost=spend)


def _try_unwind_poly(market_id: str, action: str, contracts: Decimal) -> dict:
    try:
        market = asyncio.run(poly_data.fetch_markets_by_ids([market_id]))[0]
        min_price = _side_bid(market, action)
        if min_price <= 0:
            return {"ok": False, "error": "no bid available"}
        return _place_poly_sell(market_id, action, contracts, min_price)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _try_unwind_kalshi(ticker: str, action: str, contracts: Decimal) -> dict:
    try:
        market = asyncio.run(kalshi_data.fetch_markets_by_tickers([ticker]))[0]
        min_price = _side_bid(market, action)
        if min_price <= 0:
            return {"ok": False, "error": "no bid available"}
        return _place_kalshi_sell(ticker, action, contracts, min_price)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _place_poly_sell(market_id: str, action: str, contracts: Decimal, min_price: Decimal) -> dict:
    sdk = _poly_sdk()
    private_key, wallet = _poly_credentials()
    token_id = _poly_token_id(market_id, action)
    with sdk.SecureClient.create(private_key=private_key, wallet=wallet) as client:
        result = client.place_market_order(
            token_id=token_id,
            side="SELL",
            shares=str(contracts),
            min_price=str(_price(min_price)),
            order_type="FOK",
        )
    if not result.ok:
        raise RuntimeError(f"Polymarket sell rejected: {result.code} {result.message}")
    return _poly_order_response(result, filled_shares=contracts)


def _poly_order_response(
    result,
    filled_shares: Decimal | None = None,
    filled_cost: Decimal | None = None,
) -> dict:
    if not result.ok:
        return {"ok": False, "code": result.code, "message": result.message}
    return {
        "ok": True,
        "order_id": result.order_id,
        "status": result.status,
        "making_amount": str(result.making_amount),
        "taking_amount": str(result.taking_amount),
        "filled_shares": str(filled_shares) if filled_shares is not None else "",
        "filled_cost": str(filled_cost) if filled_cost is not None else "",
        "trade_ids": list(result.trade_ids),
    }


def _place_kalshi_buy(ticker: str, action: str, contracts: Decimal, limit_price: Decimal) -> dict:
    if action == "YES":
        side = "bid"
        price = limit_price
    else:
        side = "ask"
        price = Decimal("1") - limit_price

    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "count": str(contracts),
        "price": str(_price(price)),
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": False,
    }
    result = _kalshi_order(payload)
    verified = _verify_kalshi_fill(result, contracts)
    fill_count = _kalshi_fill_count(verified) or Decimal("0")
    if fill_count < contracts:
        raise RuntimeError(f"Kalshi FOK did not fully fill: {verified}")
    return verified


def _place_kalshi_sell(ticker: str, action: str, contracts: Decimal, min_price: Decimal) -> dict:
    if action == "YES":
        side = "ask"
        price = min_price
    else:
        side = "bid"
        price = Decimal("1") - min_price

    payload = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": side,
        "count": str(contracts),
        "price": str(_price(price)),
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": True,
    }
    result = _kalshi_order(payload)
    verified = _verify_kalshi_fill(result, contracts)
    fill_count = _kalshi_fill_count(verified) or Decimal("0")
    if fill_count < contracts:
        raise RuntimeError(f"Kalshi FOK sell did not fully fill: {verified}")
    return verified


def _kalshi_order(payload: dict) -> dict:
    path = "/trade-api/v2/portfolio/events/orders"
    headers = kalshi_data._build_auth_headers("POST", path)
    if not headers:
        raise RuntimeError("Kalshi credentials are not configured")
    resp = httpx.post(f"{kalshi_data.BASE_URL}/portfolio/events/orders", headers=headers, json=payload, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(_format_kalshi_error(resp.status_code, resp.text))
    return resp.json()


def _format_kalshi_error(status_code: int, response_text: str) -> str:
    try:
        data = json.loads(response_text)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "").strip()
            if code == "fill_or_kill_insufficient_resting_volume":
                return (
                    "Kalshi FOK could not fill the whole order at the current resting volume. "
                    "The book likely moved or thinned before execution; refresh depth or retry with a smaller size."
                )
            return f"Kalshi HTTP {status_code}: {code or message or response_text[:240]}"
    except Exception:
        pass
    return f"Kalshi HTTP {status_code}: {response_text[:300]}"


def _kalshi_lookup_order(order_id: str) -> dict:
    path = "/trade-api/v2/portfolio/orders"
    headers = kalshi_data._build_auth_headers("GET", path)
    resp = httpx.get(f"{kalshi_data.BASE_URL}/portfolio/orders", headers=headers, timeout=20)
    if resp.status_code >= 400:
        return {"order_id": order_id, "lookup_error": f"HTTP {resp.status_code}"}
    orders = resp.json().get("orders") or []
    for order in orders:
        if order.get("order_id") == order_id or order.get("id") == order_id:
            return order
    return {"order_id": order_id, "lookup_error": "not found"}


def _verify_kalshi_fill(result: dict, contracts: Decimal) -> dict:
    order = _kalshi_order_payload(result)
    fill_count = _kalshi_fill_count(order)
    if fill_count is not None and fill_count >= contracts:
        order["verified_from"] = "order_response"
        return order

    order_id = _kalshi_order_id(order) or _kalshi_order_id(result)
    if not order_id:
        return order
    verified = _kalshi_lookup_order(order_id)
    verified["verified_from"] = "order_lookup"
    return verified


def _kalshi_order_payload(result: dict) -> dict:
    for key in ("order", "event_order"):
        value = result.get(key)
        if isinstance(value, dict):
            return dict(value)
    return dict(result)


def _kalshi_order_id(order: dict) -> str:
    return str(order.get("order_id") or order.get("id") or "")


def _kalshi_fill_count(order: dict) -> Decimal | None:
    for key in ("fill_count", "filled_count", "fill_count_fp", "filled_count_fp"):
        value = order.get(key)
        if value not in (None, ""):
            try:
                return Decimal(str(value))
            except Exception:
                return None
    return None
