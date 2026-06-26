import base64
import hashlib
import os
import time
import httpx
from datetime import datetime

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
BASE_URL = os.getenv("KALSHI_BASE_URL_OVERRIDE", "").strip() or "https://api.elections.kalshi.com/trade-api/v2"
EVENTS_ENDPOINT = f"{BASE_URL}/events"


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _read_multiline_env_key(var_name: str) -> str:
    """
    Read a multi-line value from the .env file that python-dotenv would truncate.
    Returns the joined value (newlines stripped, base64 body only).
    """
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return ""
    lines = []
    capturing = False
    prefix = f"{var_name}="
    import re
    _env_key_pattern = re.compile(r'^[A-Z_][A-Z0-9_]*=')
    with open(env_path, "r") as f:
        for line in f:
            stripped = line.rstrip("\n")
            if stripped.startswith(prefix):
                capturing = True
                lines.append(stripped[len(prefix):])
            elif capturing:
                # Stop at blank line, comment, or a new KEY= line
                if not stripped or stripped.startswith("#") or _env_key_pattern.match(stripped):
                    break
                lines.append(stripped.strip())
    return "".join(lines)


def _read_private_key() -> str:
    inline = _env("KALSHI_SECRET", "KALSHI_PRIVATE_KEY")
    if inline:
        return inline

    for var_name in ("KALSHI_SECRET", "KALSHI_PRIVATE_KEY"):
        raw = _read_multiline_env_key(var_name).strip()
        if raw:
            return raw

    key_path = _env("KALSHI_PRIVATE_KEY_PATH")
    if not key_path:
        return ""

    candidates = []
    if key_path.startswith(("/", "\\")):
        candidates.append(os.path.normpath(key_path))
        candidates.append(os.path.normpath(os.path.join(PROJECT_ROOT, key_path.lstrip("/\\"))))
    elif os.path.isabs(key_path):
        candidates.append(os.path.normpath(key_path))
        candidates.append(os.path.normpath(os.path.join(PROJECT_ROOT, key_path.lstrip("/\\"))))
    else:
        candidates.append(os.path.normpath(os.path.join(PROJECT_ROOT, key_path)))

    resolved = next((path for path in candidates if os.path.exists(path)), "")
    if not resolved:
        return ""

    with open(resolved, "r") as f:
        return f.read().strip()


def _build_auth_headers(method: str, path: str) -> dict:
    """Build Kalshi RSA-PSS authentication headers. Returns empty dict if keys not set."""
    api_key = _env("KALSHI_API_KEY", "KALSHI_API_KEY_ID")
    secret = _read_private_key()

    if not api_key or not secret:
        return {}

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        # Wrap raw base64 key body in PEM headers if not already present, trying both PKCS#8 and PKCS#1
        if not secret.startswith("-----"):
            private_key = None
            for header, footer in [
                ("-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"),
                ("-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"),
            ]:
                try:
                    pem = f"{header}\n{secret}\n{footer}"
                    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
                    break
                except Exception:
                    continue
            if private_key is None:
                return {}
        else:
            private_key = serialization.load_pem_private_key(secret.encode(), password=None)
        timestamp_ms = str(int(time.time() * 1000))
        msg = (timestamp_ms + method.upper() + path).encode()

        signature = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }
    except Exception:
        return {}


def _normalize_market(raw: dict, event_title: str = "", event_category: str = "") -> dict:
    ticker = raw.get("ticker", "")
    url = f"https://kalshi.com/markets/{ticker}" if ticker else ""
    title = raw.get("title") or raw.get("yes_sub_title") or raw.get("subtitle", "Unknown")
    yes_bid = _parse_float(raw.get("yes_bid_dollars", 0))
    yes_ask = _parse_float(raw.get("yes_ask_dollars", 0))
    no_bid = _parse_float(raw.get("no_bid_dollars", 0))
    no_ask = _parse_float(raw.get("no_ask_dollars", 0))

    return {
        "id": ticker,
        "title": title,
        "category": event_category or raw.get("event_ticker", ""),
        "volume": _parse_fixed_point(raw.get("volume_fp", "0")),
        "liquidity": _estimate_liquidity(raw, yes_bid, yes_ask, no_bid, no_ask),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "best_bid": yes_bid,
        "best_ask": yes_ask,
        "end_date": raw.get("close_time", raw.get("latest_expiration_time", "")),
        "source": "kalshi",
        "url": url,
        "condition_id": ticker,
        "rules": raw.get("rules_primary", raw.get("rules_secondary", "")),
        "event_title": event_title or raw.get("event_ticker", ""),
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


def _estimate_liquidity(raw: dict, yes_bid: float, yes_ask: float, no_bid: float, no_ask: float) -> float:
    """
    Kalshi's events endpoint often returns liquidity_dollars as 0 even when top-of-book
    sizes are present. Fall back to estimated visible dollar depth at the best prices.
    """
    reported = _parse_float(raw.get("liquidity_dollars", 0))
    if reported > 0:
        return reported

    size_fields = {
        "yes_bid_size_fp": yes_bid,
        "yes_ask_size_fp": yes_ask,
        "no_bid_size_fp": no_bid,
        "no_ask_size_fp": no_ask,
    }
    return sum(_parse_fixed_point(raw.get(field, 0)) * price for field, price in size_fields.items())


def _iso_to_unix(iso_str: str) -> int | None:
    """Convert ISO8601 date string to Unix timestamp (seconds)."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


async def fetch_markets_by_tickers(tickers: list[str]) -> list[dict]:
    """Fetch specific Kalshi markets by their tickers, one request each."""
    if not tickers:
        return []
    results = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for ticker in tickers:
            path = f"/trade-api/v2/markets/{ticker}"
            auth_headers = _build_auth_headers("GET", path)
            resp = await client.get(f"{BASE_URL}/markets/{ticker}", headers=auth_headers)
            if resp.status_code == 200:
                raw = resp.json().get("market", {})
                results.append(_normalize_market(raw))
    return results


async def fetch_all_markets(
    max_pages: int = 50,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
    volume_min: float | None = None,
    liquidity_min: float | None = None,
) -> list[dict]:
    """
    Fetch markets from Kalshi via the /events endpoint (with nested markets).
    The /markets endpoint only returns low-volume KXMV experimental markets;
    the real political/economic markets are nested inside events.
    """
    all_markets = []
    cursor = None
    limit = 200  # events endpoint max per request

    use_date_filter = bool(end_date_min or end_date_max)
    min_close_ts = _iso_to_unix(end_date_min) if end_date_min else None
    max_close_ts = _iso_to_unix(end_date_max) if end_date_max else None

    async with httpx.AsyncClient(timeout=60.0) as client:
        for _ in range(max_pages):
            params: dict = {"limit": limit, "with_nested_markets": "true"}

            if use_date_filter:
                if min_close_ts:
                    params["min_close_ts"] = min_close_ts
                if max_close_ts:
                    params["max_close_ts"] = max_close_ts
            else:
                params["status"] = "open"

            if cursor:
                params["cursor"] = cursor

            auth_headers = _build_auth_headers("GET", "/trade-api/v2/events")
            resp = await client.get(EVENTS_ENDPOINT, params=params, headers=auth_headers)
            resp.raise_for_status()
            data = resp.json()

            events = data.get("events", [])
            if not events:
                break

            for event in events:
                event_title = event.get("title", "")
                event_category = event.get("category", "")
                for raw in event.get("markets", []):
                    if use_date_filter:
                        status = raw.get("status", "")
                        if status not in ("active", "open"):
                            continue

                    market = _normalize_market(raw, event_title=event_title, event_category=event_category)

                    if volume_min is not None and volume_min > 0:
                        if market["volume"] < volume_min:
                            continue

                    if liquidity_min is not None and liquidity_min > 0:
                        if market["liquidity"] < liquidity_min:
                            continue

                    all_markets.append(market)

            cursor = data.get("cursor")
            if not cursor:
                break

    return all_markets


async def fetch_balance() -> dict:
    """Fetch Kalshi account balance. Returns dollars (balance is stored in cents)."""
    path = "/trade-api/v2/portfolio/balance"
    auth_headers = _build_auth_headers("GET", path)
    if not auth_headers:
        return {"error": "Kalshi API key/private key not configured or private key path not found"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BASE_URL}/portfolio/balance", headers=auth_headers)
            if resp.status_code == 200:
                data = resp.json()
                # Kalshi returns balance in cents
                total_cents     = data.get("balance", 0)
                reserved_cents  = data.get("payout", 0)   # pending payouts / reserved
                available_cents = max(0, total_cents - reserved_cents)
                return {
                    "total":     total_cents     / 100,
                    "available": available_cents / 100,
                    "reserved":  reserved_cents  / 100,
                }
            return {"error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}
