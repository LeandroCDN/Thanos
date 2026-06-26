import asyncio
import importlib
import importlib.machinery
import importlib.util
import os
import sys
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Literal

import httpx
from pydantic import BaseModel, Field

import kalshi as kalshi_data
import polymarket as poly_data

EXECUTION_VERSION = "open-v4-simple-fok"


class OpenTradeRequest(BaseModel):
    poly_id: str
    kalshi_id: str
    poly_action: Literal["YES", "NO"]
    kalshi_action: Literal["YES", "NO"]
    contracts: Decimal = Field(gt=0)
    max_poly_price: Decimal = Field(gt=0, lt=1)
    max_kalshi_price: Decimal = Field(gt=0, lt=1)
    max_total_cost: Decimal = Field(gt=0)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def _count(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_DOWN)


def _price(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


async def execute_open_trade(req: OpenTradeRequest) -> dict:
    poly_markets, kalshi_markets = await asyncio.gather(
        poly_data.fetch_markets_by_ids([req.poly_id]),
        kalshi_data.fetch_markets_by_tickers([req.kalshi_id]),
    )
    if not poly_markets:
        raise ValueError(f"Polymarket market not found: {req.poly_id}")
    if not kalshi_markets:
        raise ValueError(f"Kalshi market not found: {req.kalshi_id}")
    return await asyncio.to_thread(_execute_open_trade_sync, req, poly_markets[0], kalshi_markets[0])


def _execute_open_trade_sync(req: OpenTradeRequest, poly_market: dict, kalshi_market: dict) -> dict:
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

    poly_result = _place_poly_buy(
        req.poly_id,
        req.poly_action,
        contracts,
        poly_spend,
        poly_ask,
        max_poly_spend,
    )
    try:
        kalshi_result = _place_kalshi_buy(req.kalshi_id, req.kalshi_action, contracts, kalshi_ask)
    except Exception as exc:
        unwind = _try_unwind_poly(req.poly_id, req.poly_action, contracts)
        raise RuntimeError(
            f"Kalshi leg failed after Polymarket fill: {exc}. "
            f"Polymarket unwind attempted: {unwind}"
        ) from exc

    return {
        "status": "opened",
        "execution_version": EXECUTION_VERSION,
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


def _side_ask(market: dict, action: str) -> Decimal:
    key = "yes_ask" if action == "YES" else "no_ask"
    return Decimal(str(market.get(key) or "0"))


def _side_bid(market: dict, action: str) -> Decimal:
    key = "yes_bid" if action == "YES" else "no_bid"
    return Decimal(str(market.get(key) or "0"))


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
    raw = httpx.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=20).json()
    token_ids = raw.get("clobTokenIds")
    if isinstance(token_ids, str):
        import json

        token_ids = json.loads(token_ids)
    if not token_ids or len(token_ids) < 2:
        raise RuntimeError(f"Polymarket {market_id} did not expose CLOB token ids")
    return str(token_ids[0 if action == "YES" else 1])


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
        return _poly_order_response(result)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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


def _place_kalshi_buy(ticker: str, action: str, contracts: Decimal, ask: Decimal) -> dict:
    if action == "YES":
        side = "bid"
        price = ask
    else:
        side = "ask"
        price = Decimal("1") - ask

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
    order_id = result.get("order_id") or result.get("id")
    verified = _kalshi_lookup_order(order_id) if order_id else result
    fill_count = Decimal(str(verified.get("fill_count_fp") or verified.get("fill_count") or "0"))
    if fill_count < contracts:
        raise RuntimeError(f"Kalshi FOK did not fully fill: {verified}")
    return verified


def _kalshi_order(payload: dict) -> dict:
    path = "/trade-api/v2/portfolio/events/orders"
    headers = kalshi_data._build_auth_headers("POST", path)
    if not headers:
        raise RuntimeError("Kalshi credentials are not configured")
    resp = httpx.post(f"{kalshi_data.BASE_URL}/portfolio/events/orders", headers=headers, json=payload, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
    return resp.json()


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
