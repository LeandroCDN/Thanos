import httpx

BASE_URL = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = f"{BASE_URL}/markets"


def _normalize_market(raw: dict) -> dict:
    condition_id = raw.get("conditionId", raw.get("condition_id", ""))
    slug = raw.get("slug", "")
    url = f"https://polymarket.com/event/{slug}" if slug else ""

    outcome_prices = _parse_outcome_prices(raw.get("outcomePrices", ""))
    yes_price = outcome_prices[0] if len(outcome_prices) > 0 else 0.0
    no_price = outcome_prices[1] if len(outcome_prices) > 1 else 0.0

    outcomes_raw = raw.get("outcomes", "")
    if isinstance(outcomes_raw, str):
        import json
        try:
            outcomes_list = json.loads(outcomes_raw) if outcomes_raw else ["Yes", "No"]
        except (json.JSONDecodeError, TypeError):
            outcomes_list = ["Yes", "No"]
    else:
        outcomes_list = outcomes_raw if outcomes_raw else ["Yes", "No"]

    return {
        "id": raw.get("id", ""),
        "title": raw.get("question", raw.get("title", "Unknown")),
        "category": raw.get("category", ""),
        "volume": _parse_float(raw.get("volumeNum", raw.get("volume", 0))),
        "liquidity": _parse_float(raw.get("liquidityNum", raw.get("liquidity", 0))),
        "yes_bid": _parse_float(raw.get("bestBid", 0)),
        "yes_ask": _parse_float(raw.get("bestAsk", 0)),
        "no_bid": no_price,
        "no_ask": no_price,
        "best_bid": _parse_float(raw.get("bestBid", 0)),
        "best_ask": _parse_float(raw.get("bestAsk", 0)),
        "end_date": raw.get("endDate", ""),
        "source": "polymarket",
        "url": url,
        "condition_id": condition_id,
        "rules": raw.get("description", ""),
        "event_title": raw.get("groupItemTitle", raw.get("title", "")),
        "outcomes": outcomes_list,
    }


def _parse_outcome_prices(value) -> list[float]:
    if not value:
        return []
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
            return [float(x) for x in parsed]
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if isinstance(value, list):
        return [float(x) for x in value]
    return []


def _parse_float(value) -> float:
    try:
        return float(value) if value else 0.0
    except (ValueError, TypeError):
        return 0.0


async def fetch_all_markets(
    max_pages: int = 50,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
    volume_min: float | None = None,
    liquidity_min: float | None = None,
) -> list[dict]:
    """
    Fetch active markets from Polymarket with optional server-side filters.
    
    Params:
        end_date_min: ISO8601 string, e.g. "2026-06-01T00:00:00Z"
        end_date_max: ISO8601 string, e.g. "2026-07-01T00:00:00Z"
        volume_min: minimum volume in USD
        liquidity_min: minimum liquidity in USD
    """
    all_markets = []
    limit = 100
    offset = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(max_pages):
            params: dict = {
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "volume",
                "ascending": "false",
            }

            if end_date_min:
                params["end_date_min"] = end_date_min
            if end_date_max:
                params["end_date_max"] = end_date_max
            if volume_min is not None and volume_min > 0:
                params["volume_num_min"] = str(volume_min)
            if liquidity_min is not None and liquidity_min > 0:
                params["liquidity_num_min"] = str(liquidity_min)

            resp = await client.get(MARKETS_ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            for raw in data:
                all_markets.append(_normalize_market(raw))

            if len(data) < limit:
                break

            offset += limit

    return all_markets
