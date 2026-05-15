import httpx
from datetime import datetime

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MARKETS_ENDPOINT = f"{BASE_URL}/markets"


def _normalize_market(raw: dict) -> dict:
    ticker = raw.get("ticker", "")
    url = f"https://kalshi.com/markets/{ticker}" if ticker else ""

    return {
        "id": ticker,
        "title": raw.get("title") or raw.get("yes_sub_title") or raw.get("subtitle", "Unknown"),
        "category": raw.get("event_ticker", ""),
        "volume": _parse_fixed_point(raw.get("volume_fp", "0")),
        "liquidity": _parse_float(raw.get("liquidity_dollars", 0)),
        "yes_bid": _parse_float(raw.get("yes_bid_dollars", 0)),
        "yes_ask": _parse_float(raw.get("yes_ask_dollars", 0)),
        "no_bid": _parse_float(raw.get("no_bid_dollars", 0)),
        "no_ask": _parse_float(raw.get("no_ask_dollars", 0)),
        "best_bid": _parse_float(raw.get("yes_bid_dollars", 0)),
        "best_ask": _parse_float(raw.get("yes_ask_dollars", 0)),
        "end_date": raw.get("close_time", raw.get("latest_expiration_time", "")),
        "source": "kalshi",
        "url": url,
        "condition_id": ticker,
        "rules": raw.get("rules_primary", raw.get("rules_secondary", "")),
        "event_title": raw.get("event_ticker", ""),
        "outcomes": ["Yes", "No"],
    }


def _parse_float(value) -> float:
    try:
        return float(value) if value else 0.0
    except (ValueError, TypeError):
        return 0.0


def _parse_fixed_point(value) -> float:
    """Kalshi fixed-point fields are string integers representing centis (x100)."""
    try:
        raw = int(value) if value else 0
        return raw / 100.0
    except (ValueError, TypeError):
        return _parse_float(value)


def _iso_to_unix(iso_str: str) -> int | None:
    """Convert ISO8601 date string to Unix timestamp (seconds)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


async def fetch_all_markets(
    max_pages: int = 50,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
    volume_min: float | None = None,
    liquidity_min: float | None = None,
) -> list[dict]:
    """
    Fetch markets from Kalshi with optional filters.
    
    Note: Kalshi's min_close_ts/max_close_ts are incompatible with status=open.
    When date filters are used, we omit the status param and filter client-side.
    Kalshi has no server-side volume/liquidity filter, so we filter client-side.
    """
    all_markets = []
    cursor = None
    limit = 1000  # Kalshi max per request

    use_date_filter = bool(end_date_min or end_date_max)
    min_close_ts = _iso_to_unix(end_date_min) if end_date_min else None
    max_close_ts = _iso_to_unix(end_date_max) if end_date_max else None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(max_pages):
            params: dict = {"limit": limit}

            if use_date_filter:
                # Cannot combine close_ts filters with status=open
                if min_close_ts:
                    params["min_close_ts"] = min_close_ts
                if max_close_ts:
                    params["max_close_ts"] = max_close_ts
            else:
                params["status"] = "open"

            if cursor:
                params["cursor"] = cursor

            resp = await client.get(MARKETS_ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()

            markets = data.get("markets", [])
            if not markets:
                break

            for raw in markets:
                # When using date filters, we get all statuses — filter to open only
                if use_date_filter:
                    status = raw.get("status", "")
                    if status not in ("active", "open"):
                        continue

                market = _normalize_market(raw)

                # Client-side volume filter (Kalshi has no server-side option)
                if volume_min is not None and volume_min > 0:
                    if market["volume"] < volume_min:
                        continue

                # Client-side liquidity filter
                if liquidity_min is not None and liquidity_min > 0:
                    if market["liquidity"] < liquidity_min:
                        continue

                all_markets.append(market)

            cursor = data.get("cursor")
            if not cursor:
                break

    return all_markets
