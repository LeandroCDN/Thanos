from datetime import datetime, timezone
from typing import Any, Literal


Action = Literal["YES", "NO"]
Direction = Literal["A", "B"]


def side_ask(market: dict[str, Any], action: Action) -> float:
    return float(market.get("yes_ask" if action == "YES" else "no_ask") or 0)


def side_bid(market: dict[str, Any], action: Action) -> float:
    return float(market.get("yes_bid" if action == "YES" else "no_bid") or 0)


def kalshi_fee(price: float, fee_rate: float) -> float:
    return fee_rate * price * (1 - price)


def direction_actions(direction: Direction) -> tuple[Action, Action]:
    if direction == "A":
        return "YES", "NO"
    return "NO", "YES"


def compute_edges(poly: dict[str, Any], kalshi: dict[str, Any], poly_fee: float, kalshi_fee_rate: float) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for direction in ("A", "B"):
        poly_action, kalshi_action = direction_actions(direction)  # type: ignore[arg-type]
        poly_ask = side_ask(poly, poly_action)
        kalshi_ask = side_ask(kalshi, kalshi_action)
        if poly_ask <= 0 or kalshi_ask <= 0:
            continue
        gross_edge = 1 - poly_ask - kalshi_ask
        net_edge = 1 - poly_ask * (1 + poly_fee) - (
            kalshi_ask + kalshi_fee(kalshi_ask, kalshi_fee_rate)
        )
        candidates.append(
            {
                "direction": direction,
                "polyAction": poly_action,
                "kalshiAction": kalshi_action,
                "polyAsk": poly_ask,
                "kalshiAsk": kalshi_ask,
                "grossEdge": gross_edge,
                "netEdge": net_edge,
            }
        )
    candidates.sort(key=lambda item: item["netEdge"], reverse=True)
    return candidates


def calc_trade(poly: dict[str, Any], kalshi: dict[str, Any], bet_size: float, poly_fee: float, kalshi_fee_rate: float) -> dict[str, Any] | None:
    candidates = compute_edges(poly, kalshi, poly_fee, kalshi_fee_rate)
    if not candidates:
        return None
    best = candidates[0]
    poly_ask = best["polyAsk"]
    kalshi_ask = best["kalshiAsk"]
    poly_eff_cost = poly_ask * (1 + poly_fee)
    kalshi_eff_cost = kalshi_ask + kalshi_fee(kalshi_ask, kalshi_fee_rate)
    total_eff_cost = poly_eff_cost + kalshi_eff_cost
    if total_eff_cost <= 0:
        return None

    units = bet_size / total_eff_cost
    poly_fees_paid = units * poly_ask * poly_fee
    kalshi_fees_paid = units * kalshi_fee(kalshi_ask, kalshi_fee_rate)
    total_fees = poly_fees_paid + kalshi_fees_paid
    poly_spend = units * poly_eff_cost
    kalshi_spend = units * kalshi_eff_cost
    gross_profit = units * (1 - (poly_ask + kalshi_ask))
    net_profit = units * (1 - total_eff_cost)

    return {
        **best,
        "contracts": units,
        "polySpend": poly_spend,
        "kalshiSpend": kalshi_spend,
        "totalCost": bet_size,
        "polyFeesPaid": poly_fees_paid,
        "kalshiFeesPaid": kalshi_fees_paid,
        "totalFees": total_fees,
        "grossProfit": gross_profit,
        "grossProfitPct": (gross_profit / bet_size) * 100 if bet_size > 0 else 0,
        "netProfit": net_profit,
        "netProfitPct": (net_profit / bet_size) * 100 if bet_size > 0 else 0,
    }


def calc_trade_for_contracts(
    direction: Direction,
    poly_ask: float,
    kalshi_ask: float,
    contracts: float,
    poly_fee: float,
    kalshi_fee_rate: float,
) -> dict[str, Any] | None:
    if contracts <= 0 or poly_ask <= 0 or kalshi_ask <= 0:
        return None
    poly_action, kalshi_action = direction_actions(direction)
    poly_fees_paid = contracts * poly_ask * poly_fee
    kalshi_fees_paid = contracts * kalshi_fee(kalshi_ask, kalshi_fee_rate)
    total_fees = poly_fees_paid + kalshi_fees_paid
    poly_spend = contracts * poly_ask + poly_fees_paid
    kalshi_spend = contracts * kalshi_ask + kalshi_fees_paid
    total_cost = poly_spend + kalshi_spend
    gross_profit = contracts * (1 - poly_ask - kalshi_ask)
    net_profit = contracts - total_cost
    return {
        "direction": direction,
        "polyAction": poly_action,
        "kalshiAction": kalshi_action,
        "polyAsk": poly_ask,
        "kalshiAsk": kalshi_ask,
        "grossEdge": 1 - poly_ask - kalshi_ask,
        "netEdge": (net_profit / contracts) if contracts > 0 else 0,
        "contracts": contracts,
        "polySpend": poly_spend,
        "kalshiSpend": kalshi_spend,
        "totalCost": total_cost,
        "polyFeesPaid": poly_fees_paid,
        "kalshiFeesPaid": kalshi_fees_paid,
        "totalFees": total_fees,
        "grossProfit": gross_profit,
        "grossProfitPct": (gross_profit / total_cost) * 100 if total_cost > 0 else 0,
        "netProfit": net_profit,
        "netProfitPct": (net_profit / total_cost) * 100 if total_cost > 0 else 0,
    }


def position_from_trade(
    pair: dict[str, Any],
    poly: dict[str, Any],
    kalshi: dict[str, Any],
    trade: dict[str, Any],
    *,
    source: str,
    execution_mode: str,
) -> dict[str, Any]:
    return {
        "polyId": pair["polyId"],
        "polyTitle": pair["polyTitle"],
        "polyEndDate": poly.get("end_date") or "",
        "kalshiId": pair["kalshiId"],
        "kalshiTitle": pair["kalshiTitle"],
        "kalshiEndDate": kalshi.get("end_date") or "",
        "direction": trade["direction"],
        "polyAction": trade["polyAction"],
        "kalshiAction": trade["kalshiAction"],
        "polyEntryAsk": trade["polyAsk"],
        "kalshiEntryAsk": trade["kalshiAsk"],
        "contracts": trade["contracts"],
        "polyCapital": trade["polySpend"],
        "kalshiCapital": trade["kalshiSpend"],
        "totalCapital": trade["totalCost"],
        "polyFeesPaid": trade["polyFeesPaid"],
        "kalshiFeesPaid": trade["kalshiFeesPaid"],
        "totalFeesPaid": trade["totalFees"],
        "lockedProfit": trade["netProfit"],
        "lockedProfitPct": trade["netProfitPct"],
        "source": source,
        "executionMode": execution_mode,
    }


def calc_unrealized_pnl(
    position: dict[str, Any],
    poly: dict[str, Any],
    kalshi: dict[str, Any],
    poly_fee: float,
    kalshi_fee_rate: float,
) -> dict[str, float]:
    contracts = float(position.get("contracts") or 0)
    poly_bid = side_bid(poly, position.get("polyAction", "YES"))
    kalshi_bid = side_bid(kalshi, position.get("kalshiAction", "YES"))
    if contracts <= 0 or poly_bid <= 0 or kalshi_bid <= 0:
        return {"pnl": 0.0, "pct": 0.0, "polyBid": poly_bid, "kalshiBid": kalshi_bid}

    poly_proceeds = contracts * poly_bid * (1 - poly_fee)
    kalshi_proceeds = contracts * (kalshi_bid - kalshi_fee(kalshi_bid, kalshi_fee_rate))
    total_capital = float(position.get("totalCapital") or 0)
    pnl = poly_proceeds + kalshi_proceeds - total_capital
    pct = (pnl / total_capital) * 100 if total_capital > 0 else 0
    return {"pnl": pnl, "pct": pct, "polyBid": poly_bid, "kalshiBid": kalshi_bid}


def hours_to_earliest_close(poly: dict[str, Any], kalshi: dict[str, Any]) -> float | None:
    dates = [poly.get("end_date"), kalshi.get("end_date")]
    parsed = []
    for raw in dates:
        if not raw:
            continue
        try:
            parsed.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            continue
    if not parsed:
        return None
    earliest = min(parsed)
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=timezone.utc)
    return (earliest - datetime.now(timezone.utc)).total_seconds() / 3600
