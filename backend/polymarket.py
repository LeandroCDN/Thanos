import base64
import hashlib
import hmac
import os
import time
import httpx

BASE_URL = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = f"{BASE_URL}/markets"


def _build_auth_headers(method: str, path: str, body: str = "") -> dict:
    """Build Polymarket L2 HMAC authentication headers. Returns empty dict if keys not set."""
    api_key    = os.getenv("POLYMARKET_API_KEY",    "").strip()
    secret     = os.getenv("POLYMARKET_SECRET",     "").strip()
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "").strip()
    address    = os.getenv("POLYMARKET_ADDRESS",    "").strip()

    if not api_key or not secret:
        return {}

    try:
        timestamp = str(int(time.time()))
        message = timestamp + method.upper() + path
        if body:
            message += str(body).replace("'", '"')
        secret_bytes = base64.urlsafe_b64decode(secret)
        signature = base64.urlsafe_b64encode(
            hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        headers = {
            "POLY_API_KEY":    api_key,
            "POLY_TIMESTAMP":  timestamp,
            "POLY_SIGNATURE":  signature,
            "POLY_PASSPHRASE": passphrase,
        }
        if address:
            headers["POLY_ADDRESS"] = address
        return headers
    except Exception:
        return {}


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


CLOB_BASE = "https://clob.polymarket.com"


async def _fetch_clob_prices(client: httpx.AsyncClient, token_id: str) -> tuple[float, float]:
    """Return (bid, ask) for a single CLOB token. Falls back to (0, 0) on error."""
    try:
        import asyncio as _asyncio
        bid_r, ask_r = await _asyncio.gather(
            client.get(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "buy"}),
            client.get(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "sell"}),
        )
        bid = float(bid_r.json().get("price", 0)) if bid_r.status_code == 200 else 0.0
        ask = float(ask_r.json().get("price", 0)) if ask_r.status_code == 200 else 0.0
        return bid, ask
    except Exception:
        return 0.0, 0.0


async def fetch_markets_by_ids(ids: list[str]) -> list[dict]:
    """
    Fetch specific Polymarket markets by numeric ID with real-time CLOB prices.
    The Gamma API prices can be weeks stale; CLOB prices reflect the live order book.
    """
    if not ids:
        return []
    import asyncio as _asyncio
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Step 1: get metadata from Gamma API
        params = [("id", i) for i in ids]
        resp = await client.get(MARKETS_ENDPOINT, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        # Step 2: enrich with live CLOB prices concurrently
        async def enrich(raw: dict) -> dict:
            market = _normalize_market(raw)
            import json as _json
            raw_token_ids = raw.get("clobTokenIds", [])
            if isinstance(raw_token_ids, str):
                try:
                    token_ids = _json.loads(raw_token_ids)
                except Exception:
                    token_ids = []
            else:
                token_ids = list(raw_token_ids) if raw_token_ids else []

            if len(token_ids) >= 2:
                yes_tok, no_tok = token_ids[0], token_ids[1]
                (yes_bid, yes_ask), (no_bid, no_ask) = await _asyncio.gather(
                    _fetch_clob_prices(client, yes_tok),
                    _fetch_clob_prices(client, no_tok),
                )
                if yes_ask > 0:
                    market["yes_bid"] = yes_bid
                    market["yes_ask"] = yes_ask
                    market["best_bid"] = yes_bid
                    market["best_ask"] = yes_ask
                if no_ask > 0:
                    market["no_bid"] = no_bid
                    market["no_ask"] = no_ask
            return market

        return list(await _asyncio.gather(*[enrich(raw) for raw in data]))


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


async def fetch_balance() -> dict:
    """
    Fetch Polymarket balance.
    Uses the CLOB L2 API (authenticated as the EOA signer) for on-chain USDC,
    then falls back to the Gamma API for portfolio value from the proxy wallet.
    """
    api_key    = os.getenv("POLYMARKET_API_KEY",    "").strip()
    secret     = os.getenv("POLYMARKET_SECRET",     "").strip()
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "").strip()
    proxy      = os.getenv("POLYMARKET_PROXY",      "").strip()

    if not api_key or not secret or not passphrase:
        return {"error": "Polymarket CLOB credentials not configured"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try Gamma API portfolio for the proxy wallet (no auth needed)
            if proxy:
                gamma_resp = await client.get(
                    "https://gamma-api.polymarket.com/portfolio",
                    params={"address": proxy},
                )
                if gamma_resp.status_code == 200:
                    gdata = gamma_resp.json()
                    if isinstance(gdata, dict):
                        portfolio = float(gdata.get("portfolio_value", gdata.get("value", 0)) or 0)
                        cash      = float(gdata.get("cash", gdata.get("available", portfolio)) or 0)
                        if portfolio > 0:
                            return {
                                "total":     portfolio,
                                "available": cash,
                                "reserved":  max(0.0, portfolio - cash),
                            }

            # Fall back to CLOB balance-allowance (returns on-chain USDC at EOA)
            path = "/balance-allowance"
            headers = _build_auth_headers("GET", path)
            if not headers:
                return {"error": "Auth signing failed (check POLYMARKET_SECRET format)"}

            resp = await client.get(
                f"https://clob.polymarket.com{path}",
                headers=headers,
                params={"asset_type": "COLLATERAL", "signature_type": 0},
            )
            if resp.status_code == 200:
                data  = resp.json()
                # Balance is raw USDC units (6 decimals); proxy wallet balance
                # is held internally by Polymarket and will show as 0 here.
                total = float(data.get("balance", 0) or 0) / 1_000_000
                return {
                    "total":     total,
                    "available": total,
                    "reserved":  0.0,
                    "note":      "On-chain USDC only. Portfolio value visible on Polymarket website.",
                }
            if resp.status_code == 401:
                return {"error": "CLOB auth failed — check credentials in .env"}
            return {"error": f"HTTP {resp.status_code}: {resp.text[:80]}"}
    except Exception as e:
        return {"error": str(e)}
