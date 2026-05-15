from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from polymarket import fetch_all_markets as fetch_polymarket
from kalshi import fetch_all_markets as fetch_kalshi
from cache import cache

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


@app.post("/api/cache/invalidate")
async def invalidate_cache():
    cache.invalidate()
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
