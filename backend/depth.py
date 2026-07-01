"""
Order-book depth analysis for arb pairs.

For each pair we simulate filling both legs of the trade simultaneously
and find the maximum position size where the blended edge stays above
a minimum threshold (default 0.5%).  The result is the "ideal bet size".
"""

import asyncio
import json
from collections.abc import Callable
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
    """Return implied asks {'yes': [(price, size), ...], 'no': [...]} sorted ascending."""
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

    def parse_bids(entries: list) -> list[tuple[float, float]]:
        parsed = [(float(e[0]), float(e[1])) for e in entries if len(e) == 2]
        parsed.sort(key=lambda x: x[0], reverse=True)
        return parsed

    def implied_asks(opposite_bids: list[tuple[float, float]]) -> list[tuple[float, float]]:
        parsed = [(1.0 - price, size) for price, size in opposite_bids if 0 < price < 1 and size > 0]
        parsed.sort(key=lambda x: x[0])
        return parsed

    yes_bids = parse_bids(ob.get("yes_dollars", []))
    no_bids = parse_bids(ob.get("no_dollars", []))

    return {
        "yes": implied_asks(no_bids),
        "no": implied_asks(yes_bids),
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


def _kalshi_fee(price: float, fee_rate: float) -> float:
    return fee_rate * price * (1 - price)


def _fee_adjusted_edge(poly_ask: float, kalshi_ask: float, poly_fee: float, kalshi_fee: float) -> float:
    return 1 - poly_ask * (1 + poly_fee) - (kalshi_ask + _kalshi_fee(kalshi_ask, kalshi_fee))


def _fee_adjusted_cost(poly_ask: float, kalshi_ask: float, poly_fee: float, kalshi_fee: float) -> float:
    return poly_ask * (1 + poly_fee) + kalshi_ask + _kalshi_fee(kalshi_ask, kalshi_fee)


def _max_shares_at_threshold(
    orders_a: list[tuple[float, float]],
    orders_b: list[tuple[float, float]],
    edge_threshold: float,
    max_leg_slippage: float = 0.01,
    max_bet_dollars: float = 100_000.0,
    edge_fn: Callable[[float, float], float] | None = None,
    cost_fn: Callable[[float, float], float] | None = None,
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
    edge_fn = edge_fn or (lambda ask_a, ask_b: 1 - ask_a - ask_b)
    cost_fn = cost_fn or (lambda ask_a, ask_b: ask_a + ask_b)

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
    if ea == float("inf") or eb == float("inf") or edge_fn(ea, eb) <= edge_threshold:
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
        elif edge_fn(ea, eb) >= edge_threshold and (ea - initial_a) <= max_leg_slippage and (eb - initial_b) <= max_leg_slippage:
            lo = mid
        else:
            hi = mid

    # Apply hard dollar cap: convert max dollars to shares at initial prices
    initial_cost = cost_fn(initial_a, initial_b)
    max_shares_by_cap = max_bet_dollars / initial_cost if initial_cost > 0 else lo
    return min(lo, max_shares_by_cap)


# ─── Public API ──────────────────────────────────────────────────────────────

def analyse_depth_from_books(
    poly_yes_book: dict[str, list[tuple[float, float]]],
    poly_no_book: dict[str, list[tuple[float, float]]],
    kalshi_book: dict[str, list[tuple[float, float]]],
    edge_threshold: float = 0.005,
    max_leg_slippage: float = 0.01,
    max_bet_dollars: float = 100_000.0,
    poly_fee: float = 0.0,
    kalshi_fee: float = 0.0,
) -> dict:
    """Run ideal-size analysis from already available order-book snapshots."""
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

    fee_adjusted = poly_fee > 0 or kalshi_fee > 0

    for direction, leg_a_orders, leg_b_orders, leg_a_name, leg_b_name, a_best, b_best, edge_fn, cost_fn in [
        ("A",  # buy YES Poly + NO Kalshi
         poly_yes_asks, kalshi_no_asks,
         "poly_yes", "kalshi_no",
         poly_yes_best, kalshi_no_best,
         lambda poly_ask, kalshi_ask: _fee_adjusted_edge(poly_ask, kalshi_ask, poly_fee, kalshi_fee),
         lambda poly_ask, kalshi_ask: _fee_adjusted_cost(poly_ask, kalshi_ask, poly_fee, kalshi_fee)),
        ("B",  # buy YES Kalshi + NO Poly
         kalshi_yes_asks, poly_no_asks,
         "kalshi_yes", "poly_no",
         kalshi_yes_best, poly_no_best,
         lambda kalshi_ask, poly_ask: _fee_adjusted_edge(poly_ask, kalshi_ask, poly_fee, kalshi_fee),
         lambda kalshi_ask, poly_ask: _fee_adjusted_cost(poly_ask, kalshi_ask, poly_fee, kalshi_fee)),
    ]:
        if a_best is None or b_best is None:
            results[direction] = {"ideal_bet": 0, "reason": "missing prices"}
            continue

        initial_edge = 1 - a_best - b_best
        initial_net_edge = edge_fn(a_best, b_best)
        if initial_net_edge <= edge_threshold:
            results[direction] = {
                "ideal_bet": 0,
                "initial_edge": round(initial_edge, 4),
                "initial_net_edge": round(initial_net_edge, 4),
                "reason": "no net edge at current prices" if fee_adjusted else "no edge at current prices",
            }
            continue

        max_shares = _max_shares_at_threshold(
            leg_a_orders,
            leg_b_orders,
            edge_threshold,
            max_leg_slippage,
            max_bet_dollars,
            edge_fn=edge_fn,
            cost_fn=cost_fn,
        )

        if max_shares < 0.01:
            results[direction] = {
                "ideal_bet": 0,
                "initial_edge": round(initial_edge, 4),
                "initial_net_edge": round(initial_net_edge, 4),
                "reason": "insufficient order book depth",
            }
            continue

        eff_a = _effective_ask(leg_a_orders, max_shares)
        eff_b = _effective_ask(leg_b_orders, max_shares)
        ideal_bet = max_shares * cost_fn(eff_a, eff_b)
        eff_edge = 1 - eff_a - eff_b
        eff_net_edge = edge_fn(eff_a, eff_b)

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
            "initial_net_edge": round(initial_net_edge, 4),
            "effective_net_edge": round(eff_net_edge, 4),
            "slippage": round(initial_edge - eff_edge, 4),
            "leg_a": leg_a_name,
            "leg_b": leg_b_name,
            "capped_by": "leg_slippage" if max_shares < sum(s for _, s in leg_a_orders) else "depth",
        }

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
        "fee_adjusted": fee_adjusted,
        "poly_fee": poly_fee,
        "kalshi_fee": kalshi_fee,
    }


async def analyse_depth(
    poly_id: str,
    kalshi_id: str,
    edge_threshold: float = 0.005,
    max_leg_slippage: float = 0.01,
    max_bet_dollars: float = 100_000.0,
    poly_fee: float = 0.0,
    kalshi_fee: float = 0.0,
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

    result = analyse_depth_from_books(
        poly_yes_book,
        poly_no_book,
        kalshi_book,
        edge_threshold=edge_threshold,
        max_leg_slippage=max_leg_slippage,
        max_bet_dollars=max_bet_dollars,
        poly_fee=poly_fee,
        kalshi_fee=kalshi_fee,
    )
    result["source"] = "rest"
    return result
