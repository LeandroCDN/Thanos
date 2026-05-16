"""
Order-book depth analysis for arb pairs.

For each pair we simulate filling both legs of the trade simultaneously
and find the maximum position size where the blended edge stays above
a minimum threshold (default 0.5%).  The result is the "ideal bet size".
"""

import asyncio
import json
import httpx
from kalshi import _build_auth_headers

CLOB_BASE = "https://clob.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ─── Order-book fetchers ─────────────────────────────────────────────────────

async def _poly_clob_token_ids(client: httpx.AsyncClient, poly_id: str) -> tuple[str, str]:
    """Return (yes_token_id, no_token_id) for a Polymarket market by numeric ID."""
    r = await client.get("https://gamma-api.polymarket.com/markets", params={"id": poly_id})
    r.raise_for_status()
    data = r.json()
    if not data:
        return "", ""
    raw_ids = data[0].get("clobTokenIds", "[]")
    if isinstance(raw_ids, str):
        ids = json.loads(raw_ids)
    else:
        ids = list(raw_ids)
    if len(ids) >= 2:
        return ids[0], ids[1]
    return "", ""


async def _poly_book(client: httpx.AsyncClient, token_id: str) -> dict[str, list[tuple[float, float]]]:
    """Return {'asks': [(price, size), ...], 'bids': [...]} sorted ascending for asks."""
    if not token_id:
        return {"asks": [], "bids": []}
    r = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
    if r.status_code != 200:
        return {"asks": [], "bids": []}
    data = r.json()

    def parse(entries: list[dict], ascending: bool) -> list[tuple[float, float]]:
        parsed = [(float(e["price"]), float(e["size"])) for e in entries]
        parsed.sort(key=lambda x: x[0], reverse=not ascending)
        return parsed

    # Poly returns asks sorted descending (highest first); we want ascending for fill sim
    return {
        "asks": parse(data.get("asks", []), ascending=True),
        "bids": parse(data.get("bids", []), ascending=False),
    }


async def _kalshi_book(client: httpx.AsyncClient, ticker: str, depth: int = 50) -> dict[str, list[tuple[float, float]]]:
    """Return {'yes': [(price, size), ...], 'no': [...]} sorted ascending."""
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    headers = _build_auth_headers("GET", path)
    r = await client.get(
        f"{KALSHI_BASE}/markets/{ticker}/orderbook",
        headers=headers,
        params={"depth": depth},
    )
    if r.status_code != 200:
        return {"yes": [], "no": []}
    ob = r.json().get("orderbook_fp", {})

    def parse(entries: list) -> list[tuple[float, float]]:
        parsed = [(float(e[0]), float(e[1])) for e in entries if len(e) == 2]
        parsed.sort(key=lambda x: x[0])  # ascending: cheapest first
        return parsed

    return {
        "yes": parse(ob.get("yes_dollars", [])),
        "no": parse(ob.get("no_dollars", [])),
    }


# ─── Fill simulation ─────────────────────────────────────────────────────────

def _effective_ask(orders: list[tuple[float, float]], shares: float) -> float:
    """
    Compute the volume-weighted average fill price for `shares` shares.
    Returns inf if there is not enough liquidity.
    """
    filled = 0.0
    cost = 0.0
    for price, size in orders:
        take = min(size, shares - filled)
        cost += take * price
        filled += take
        if filled >= shares - 1e-9:
            break
    if filled < shares - 1e-9:
        return float("inf")
    return cost / filled


def _max_shares_at_threshold(
    orders_a: list[tuple[float, float]],
    orders_b: list[tuple[float, float]],
    edge_threshold: float,
    max_leg_slippage: float = 0.01,
    max_bet_dollars: float = 100_000.0,
) -> float:
    """
    Binary-search for the maximum share count where:
      - blended edge ≥ edge_threshold, AND
      - neither leg's effective ask moves more than max_leg_slippage above its initial ask.

    max_leg_slippage caps fills even when all orders sit at one price (zero-slippage resting
    orders from market makers), preventing unrealistically large "ideal" sizes.
    max_bet_dollars is a hard dollar cap applied after shares are found.
    """
    if not orders_a or not orders_b:
        return 0.0

    initial_a = orders_a[0][0]
    initial_b = orders_b[0][0]

    # Upper bound: total available on the shallower side
    max_possible = min(
        sum(s for _, s in orders_a),
        sum(s for _, s in orders_b),
    )
    if max_possible <= 0:
        return 0.0

    # Quick check: is there any edge at all at 1 share?
    ea = _effective_ask(orders_a, 1.0)
    eb = _effective_ask(orders_b, 1.0)
    if ea == float("inf") or eb == float("inf") or 1 - ea - eb <= edge_threshold:
        return 0.0

    lo, hi = 0.0, max_possible
    for _ in range(60):
        mid = (lo + hi) / 2
        if mid < 0.01:
            break
        ea = _effective_ask(orders_a, mid)
        eb = _effective_ask(orders_b, mid)
        if ea == float("inf") or eb == float("inf"):
            hi = mid
        elif (
            1 - ea - eb >= edge_threshold
            and (ea - initial_a) <= max_leg_slippage
            and (eb - initial_b) <= max_leg_slippage
        ):
            lo = mid
        else:
            hi = mid

    # Apply hard dollar cap: convert max dollars to shares at initial prices
    max_shares_by_cap = max_bet_dollars / (initial_a + initial_b) if (initial_a + initial_b) > 0 else lo
    return min(lo, max_shares_by_cap)


# ─── Public API ──────────────────────────────────────────────────────────────

async def analyse_depth(
    poly_id: str,
    kalshi_id: str,
    edge_threshold: float = 0.005,
    max_leg_slippage: float = 0.01,
    max_bet_dollars: float = 100_000.0,
) -> dict:
    """
    Fetch order books for both markets and return ideal bet-size analysis.
    edge_threshold: minimum acceptable blended edge after slippage (default 0.5%).
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        yes_tok, no_tok = await _poly_clob_token_ids(client, poly_id)

        poly_yes_book, poly_no_book, kalshi_book = await asyncio.gather(
            _poly_book(client, yes_tok),
            _poly_book(client, no_tok),
            _kalshi_book(client, kalshi_id),
        )

    poly_yes_asks = poly_yes_book["asks"]
    poly_no_asks  = poly_no_book["asks"]
    kalshi_yes_asks = kalshi_book["yes"]
    kalshi_no_asks  = kalshi_book["no"]

    def best_ask(orders: list[tuple[float, float]]) -> float | None:
        return orders[0][0] if orders else None

    poly_yes_best  = best_ask(poly_yes_asks)
    poly_no_best   = best_ask(poly_no_asks)
    kalshi_yes_best = best_ask(kalshi_yes_asks)
    kalshi_no_best  = best_ask(kalshi_no_asks)

    results = {}

    for direction, leg_a_orders, leg_b_orders, leg_a_name, leg_b_name, a_best, b_best in [
        ("A",  # buy YES Poly + NO Kalshi
         poly_yes_asks, kalshi_no_asks,
         "poly_yes", "kalshi_no",
         poly_yes_best, kalshi_no_best),
        ("B",  # buy YES Kalshi + NO Poly
         kalshi_yes_asks, poly_no_asks,
         "kalshi_yes", "poly_no",
         kalshi_yes_best, poly_no_best),
    ]:
        if a_best is None or b_best is None:
            results[direction] = {"ideal_bet": 0, "reason": "missing prices"}
            continue

        initial_edge = 1 - a_best - b_best
        if initial_edge <= edge_threshold:
            results[direction] = {
                "ideal_bet": 0,
                "initial_edge": round(initial_edge, 4),
                "reason": "no edge at current prices",
            }
            continue

        max_shares = _max_shares_at_threshold(
            leg_a_orders, leg_b_orders, edge_threshold, max_leg_slippage, max_bet_dollars
        )

        if max_shares < 0.01:
            results[direction] = {
                "ideal_bet": 0,
                "initial_edge": round(initial_edge, 4),
                "reason": "insufficient order book depth",
            }
            continue

        eff_a = _effective_ask(leg_a_orders, max_shares)
        eff_b = _effective_ask(leg_b_orders, max_shares)
        ideal_bet = max_shares * (eff_a + eff_b)
        eff_edge = 1 - eff_a - eff_b

        results[direction] = {
            "ideal_bet": round(ideal_bet, 2),
            "max_shares": round(max_shares, 2),
            "eff_ask_a": round(eff_a, 4),
            "eff_ask_b": round(eff_b, 4),
            "initial_ask_a": round(a_best, 4),
            "initial_ask_b": round(b_best, 4),
            "leg_slippage_a": round(eff_a - a_best, 4),
            "leg_slippage_b": round(eff_b - b_best, 4),
            "initial_edge": round(initial_edge, 4),
            "effective_edge": round(eff_edge, 4),
            "slippage": round(initial_edge - eff_edge, 4),
            "leg_a": leg_a_name,
            "leg_b": leg_b_name,
            "capped_by": "leg_slippage" if max_shares < sum(s for _, s in leg_a_orders) else "depth",
        }

    # Best direction = the one with highest ideal_bet
    best_dir = max(results, key=lambda d: results[d].get("ideal_bet", 0))
    return {
        "A": results.get("A", {}),
        "B": results.get("B", {}),
        "best_direction": best_dir,
        "ideal_bet": results[best_dir].get("ideal_bet", 0),
        "edge_threshold_used": edge_threshold,
        "poly_yes_levels": len(poly_yes_asks),
        "poly_no_levels": len(poly_no_asks),
        "kalshi_yes_levels": len(kalshi_yes_asks),
        "kalshi_no_levels": len(kalshi_no_asks),
    }
