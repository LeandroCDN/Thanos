from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Any

import asyncio
import httpx
from ai_pair_review import pair_review_status, review_pair_batch
from bot_engine import BotModeRequest, BotSettingsUpdate, bot_controller
from polymarket import fetch_all_markets as fetch_polymarket, fetch_balance as fetch_poly_balance
from kalshi import BASE_URL as KALSHI_BASE_URL, _build_auth_headers as kalshi_auth_headers, fetch_all_markets as fetch_kalshi, fetch_balance as fetch_kalshi_balance
from market_stream import market_stream
from cache import cache
from store import store
from trading import EXECUTION_VERSION, CloseTradeRequest, OpenTradeRequest, execute_close_trade, execute_open_trade

app = FastAPI(title="Thanos - Arb Market Explorer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    await market_stream.start()
    await bot_controller.start()


@app.on_event("shutdown")
async def shutdown_event():
    await bot_controller.shutdown()
    await market_stream.shutdown()


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

    poly_markets, kalshi_markets = await market_stream.get_pair_markets(poly_list, kalshi_list)

    return {
        "polymarket": poly_markets,
        "kalshi": kalshi_markets,
    }


@app.get("/api/markets/depth")
async def get_market_depth(
    poly_id: str = Query(..., description="Polymarket numeric market ID"),
    kalshi_id: str = Query(..., description="Kalshi market ticker"),
    edge_threshold: float = Query(default=0.005, description="Min acceptable edge after slippage; net of fees when fees are supplied"),
    max_leg_slippage: float = Query(default=0.01, description="Max price impact per leg (default 1%)"),
    max_bet_dollars: float = Query(default=100_000.0, description="Hard cap on ideal bet in USD"),
    poly_fee: float = Query(default=0.0, ge=0.0, le=1.0, description="Polymarket taker fee rate"),
    kalshi_fee: float = Query(default=0.0, ge=0.0, le=1.0, description="Kalshi fee rate applied as rate * P * (1-P)"),
):
    """
    Analyse order-book depth for an arb pair and return the ideal bet size —
    the maximum $ that can be deployed before per-leg slippage exceeds max_leg_slippage
    or the blended edge drops below edge_threshold, whichever comes first.
    """
    return await market_stream.analyse_depth(
        poly_id,
        kalshi_id,
        edge_threshold,
        max_leg_slippage,
        max_bet_dollars,
        poly_fee,
        kalshi_fee,
    )


@app.get("/api/market-data/status")
async def get_market_data_status():
    return market_stream.status()


@app.post("/api/ai/pair-reviews/batch")
async def review_pair_candidates(payload: dict[str, Any]):
    try:
        return await review_pair_batch(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ai/status")
async def get_ai_status():
    return pair_review_status()


@app.websocket("/ws/markets")
async def markets_websocket(websocket: WebSocket):
    await websocket.accept()
    poly_ids: list[str] = []
    kalshi_ids: list[str] = []
    sequence = -1
    await websocket.send_json({"type": "ready", "stream": market_stream.status()})

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.5)
                if message.get("type") == "subscribe":
                    poly_ids = [str(i) for i in message.get("polyIds", []) if i]
                    kalshi_ids = [str(i) for i in message.get("kalshiIds", []) if i]
                    await market_stream.ensure_targets(poly_ids, kalshi_ids)
                    snapshot = await market_stream.snapshot(poly_ids, kalshi_ids)
                    sequence = int(snapshot.get("sequence", sequence))
                    await websocket.send_json(snapshot)
            except asyncio.TimeoutError:
                pass

            next_sequence = await market_stream.wait_for_update_after(sequence, timeout=0.5)
            if next_sequence != sequence:
                sequence = next_sequence
                if poly_ids or kalshi_ids:
                    await websocket.send_json(await market_stream.snapshot(poly_ids, kalshi_ids, wait_timeout=0.1))
    except WebSocketDisconnect:
        return


@app.get("/api/balances")
async def get_balances():
    """Fetch account balances from Polymarket (USDC) and Kalshi."""
    poly, kalshi = await asyncio.gather(fetch_poly_balance(), fetch_kalshi_balance())
    return {"polymarket": poly, "kalshi": kalshi}


@app.get("/api/whitelist")
async def list_whitelist():
    return {"pairs": store.list_whitelisted_pairs()}


@app.post("/api/whitelist")
async def add_whitelist_pair(pair: dict[str, Any]):
    try:
        saved = store.upsert_whitelisted_pair(pair)
        return {"pair": saved, "pairs": store.list_whitelisted_pairs()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/whitelist/import")
async def import_whitelist(payload: dict[str, Any]):
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        raise HTTPException(status_code=400, detail="Expected body: { pairs: [...] }")
    return {"pairs": store.import_whitelisted_pairs(pairs)}


@app.delete("/api/whitelist/{pair_id}")
async def delete_whitelist_pair(pair_id: str):
    store.delete_whitelisted_pair(pair_id)
    return {"status": "ok", "pairs": store.list_whitelisted_pairs()}


@app.get("/api/positions")
async def list_positions():
    return {"positions": store.list_positions()}


@app.post("/api/positions")
async def add_position(position: dict[str, Any]):
    try:
        saved = store.upsert_position(position)
        return {"position": saved, "positions": store.list_positions()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/positions/import")
async def import_positions(payload: dict[str, Any]):
    positions = payload.get("positions") if isinstance(payload, dict) else None
    if not isinstance(positions, list):
        raise HTTPException(status_code=400, detail="Expected body: { positions: [...] }")
    return {"positions": store.import_positions(positions)}


@app.delete("/api/positions/{position_id}")
async def delete_position(position_id: str):
    store.delete_position(position_id)
    return {"status": "ok", "positions": store.list_positions()}


@app.post("/api/positions/{position_id}/close")
async def mark_position_closed(position_id: str, payload: dict[str, Any]):
    realized = float(payload.get("realizedPnl") or 0)
    reason = str(payload.get("reason") or "manual")
    closed = store.close_position(position_id, realized, reason)
    if not closed:
        raise HTTPException(status_code=404, detail="Position not found")
    return {"position": closed, "positions": store.list_positions()}


@app.post("/api/trades/open")
async def open_trade(req: OpenTradeRequest):
    """
    Open a live arb position on both venues.

    The request carries the prices and gross cost shown in the confirmation UI.
    The execution layer re-fetches prices immediately before placing direct
    buy orders and aborts if either leg has moved beyond those limits.
    """
    claimed, claim_reason, claim_owner = store.try_claim_live_pair(req.poly_id, req.kalshi_id)
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Pair already has a live open position or in-flight open claim ({claim_reason})",
                "execution_version": EXECUTION_VERSION,
            },
        )
    try:
        return await execute_open_trade(req)
    except Exception as exc:
        store.release_live_pair_claim(req.poly_id, req.kalshi_id, claim_owner)
        raise HTTPException(
            status_code=400,
            detail={
                "message": str(exc),
                "execution_version": EXECUTION_VERSION,
            },
        ) from exc


@app.post("/api/trades/close")
async def close_trade(req: CloseTradeRequest):
    """
    Close a live arb position by selling both legs with FOK marketable orders.
    Bid caps are supplied by the caller and checked again immediately before
    execution.
    """
    try:
        return await execute_close_trade(req)
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


@app.get("/api/bot/status")
async def get_bot_status():
    return bot_controller.status()


@app.patch("/api/bot/settings")
async def update_bot_settings(update: BotSettingsUpdate):
    return bot_controller.update_settings(update)


@app.post("/api/bot/mode")
async def set_bot_mode(req: BotModeRequest):
    return await bot_controller.set_mode(req.mode)


@app.post("/api/bot/scan")
async def run_bot_scan():
    summary = await bot_controller.scan_once("manual")
    return {"summary": summary, "status": bot_controller.status()}


@app.post("/api/bot/telegram/test")
async def test_bot_telegram():
    try:
        return await bot_controller.send_test_telegram()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
