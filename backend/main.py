from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import asyncio
import httpx
from polymarket import fetch_all_markets as fetch_polymarket, fetch_markets_by_ids as fetch_poly_by_ids, fetch_balance as fetch_poly_balance
from kalshi import BASE_URL as KALSHI_BASE_URL, _build_auth_headers as kalshi_auth_headers, fetch_all_markets as fetch_kalshi, fetch_markets_by_tickers as fetch_kalshi_by_tickers, fetch_balance as fetch_kalshi_balance
from depth import analyse_depth
from cache import cache
from trading import EXECUTION_VERSION, OpenTradeRequest, execute_open_trade

app = FastAPI(title="Thanos - Arb Market Explorer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/markets/polymarket")
async def get_polymarket_markets(
    search: str = Query(default=""),
    end_date_min: str = Query(default="", description="ISO8601 min end date, e.g. 2026-06-01"),
    end_date_max: str = Query(default="", description="ISO8601 max end date, e.g. 2026-07-01"),
    volume_min: float = Query(default=0, description="Minimum volume in USD"),
    liquidity_min: float = Query(default=0, description="Minimum liquidity in USD"),
    max_pages: int = Query(default=50, ge=1, description="Max pages to fetch from upstream API"),
    force_refresh: bool = Query(default=False, description="Bypass cache"),
):
    filters = dict(
        end_date_min=end_date_min or None,
        end_date_max=end_date_max or None,
        volume_min=volume_min if volume_min > 0 else None,
        liquidity_min=liquidity_min if liquidity_min > 0 else None,
        max_pages=max_pages,
    )

    if not force_refresh:
        cached = cache.get_polymarket(**filters)
        if cached is not None:
            markets = cached
            from_cache = True
        else:
            markets = await fetch_polymarket(**filters)
            cache.set_polymarket(markets, **filters)
            from_cache = False
    else:
        markets = await fetch_polymarket(**filters)
        cache.set_polymarket(markets, **filters)
        from_cache = False

    if search:
        search_lower = search.lower()
        markets = [m for m in markets if search_lower in m["title"].lower()]

    return {
        "markets": markets,
        "count": len(markets),
        "from_cache": from_cache,
        "cache_age_seconds": cache.poly_age_seconds,
    }


@app.get("/api/markets/kalshi")
async def get_kalshi_markets(
    search: str = Query(default=""),
    end_date_min: str = Query(default="", description="ISO8601 min end date, e.g. 2026-06-01"),
    end_date_max: str = Query(default="", description="ISO8601 max end date, e.g. 2026-07-01"),
    volume_min: float = Query(default=0, description="Minimum volume in USD"),
    liquidity_min: float = Query(default=0, description="Minimum liquidity in USD"),
    max_pages: int = Query(default=50, ge=1, description="Max pages to fetch from upstream API"),
    force_refresh: bool = Query(default=False, description="Bypass cache"),
):
    filters = dict(
        end_date_min=end_date_min or None,
        end_date_max=end_date_max or None,
        volume_min=volume_min if volume_min > 0 else None,
        liquidity_min=liquidity_min if liquidity_min > 0 else None,
        max_pages=max_pages,
    )

    if not force_refresh:
        cached = cache.get_kalshi(**filters)
        if cached is not None:
            markets = cached
            from_cache = True
        else:
            markets = await fetch_kalshi(**filters)
            cache.set_kalshi(markets, **filters)
            from_cache = False
    else:
        markets = await fetch_kalshi(**filters)
        cache.set_kalshi(markets, **filters)
        from_cache = False

    if search:
        search_lower = search.lower()
        markets = [m for m in markets if search_lower in m["title"].lower()]

    return {
        "markets": markets,
        "count": len(markets),
        "from_cache": from_cache,
        "cache_age_seconds": cache.kalshi_age_seconds,
    }


@app.get("/api/markets/pairs")
async def get_pairs_markets(
    poly_ids: str = Query(default="", description="Comma-separated Polymarket market IDs"),
    kalshi_ids: str = Query(default="", description="Comma-separated Kalshi tickers"),
):
    """Fetch fresh data for a specific set of whitelisted market pairs."""
    poly_list = [i.strip() for i in poly_ids.split(",") if i.strip()] if poly_ids else []
    kalshi_list = [i.strip() for i in kalshi_ids.split(",") if i.strip()] if kalshi_ids else []

    poly_markets, kalshi_markets = await asyncio.gather(
        fetch_poly_by_ids(poly_list),
        fetch_kalshi_by_tickers(kalshi_list),
    )

    return {
        "polymarket": poly_markets,
        "kalshi": kalshi_markets,
    }


@app.get("/api/markets/depth")
async def get_market_depth(
    poly_id: str = Query(..., description="Polymarket numeric market ID"),
    kalshi_id: str = Query(..., description="Kalshi market ticker"),
    edge_threshold: float = Query(default=0.005, description="Min acceptable blended edge after slippage"),
    max_leg_slippage: float = Query(default=0.01, description="Max price impact per leg (default 1%)"),
    max_bet_dollars: float = Query(default=100_000.0, description="Hard cap on ideal bet in USD"),
):
    """
    Analyse order-book depth for an arb pair and return the ideal bet size —
    the maximum $ that can be deployed before per-leg slippage exceeds max_leg_slippage
    or the blended edge drops below edge_threshold, whichever comes first.
    """
    return await analyse_depth(poly_id, kalshi_id, edge_threshold, max_leg_slippage, max_bet_dollars)


@app.get("/api/balances")
async def get_balances():
    """Fetch account balances from Polymarket (USDC) and Kalshi."""
    poly, kalshi = await asyncio.gather(fetch_poly_balance(), fetch_kalshi_balance())
    return {"polymarket": poly, "kalshi": kalshi}


@app.post("/api/trades/open")
async def open_trade(req: OpenTradeRequest):
    """
    Open a live arb position on both venues.

    The request carries the prices and gross cost shown in the confirmation UI.
    The execution layer re-fetches prices immediately before placing direct
    buy orders and aborts if either leg has moved beyond those limits.
    """
    try:
        return await execute_open_trade(req)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": str(exc),
                "execution_version": EXECUTION_VERSION,
            },
        ) from exc


@app.get("/api/trades/status")
async def trades_status():
    return {"execution_version": EXECUTION_VERSION}


@app.get("/api/diagnostics/kalshi")
async def kalshi_diagnostics(ticker: str = Query(..., description="Kalshi market ticker")):
    """Read-only Kalshi position/order check for a single ticker."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        pos_path = "/trade-api/v2/portfolio/positions"
        pos_resp = await client.get(
            f"{KALSHI_BASE_URL}/portfolio/positions",
            headers=kalshi_auth_headers("GET", pos_path),
            params={"ticker": ticker},
        )

        orders_path = "/trade-api/v2/portfolio/orders"
        orders_resp = await client.get(
            f"{KALSHI_BASE_URL}/portfolio/orders",
            headers=kalshi_auth_headers("GET", orders_path),
        )

    positions = pos_resp.json() if pos_resp.status_code < 400 else {"error": pos_resp.text[:300]}
    raw_orders = orders_resp.json() if orders_resp.status_code < 400 else {"error": orders_resp.text[:300]}
    orders = raw_orders.get("orders") or raw_orders.get("event_orders") or []
    matching_orders = [
        order for order in orders
        if order.get("ticker") == ticker or order.get("market_ticker") == ticker
    ][:20]

    return {
        "ticker": ticker,
        "positions_status": pos_resp.status_code,
        "positions": positions,
        "orders_status": orders_resp.status_code,
        "orders": matching_orders,
    }


@app.post("/api/cache/invalidate")
async def invalidate_cache():
    cache.invalidate()
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
