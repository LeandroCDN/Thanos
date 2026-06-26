import json
import os
import sys
import uuid
from decimal import Decimal, ROUND_UP
from pathlib import Path

import httpx
from dotenv import load_dotenv


POLY_MARKET_ID = "561975"
KALSHI_TICKER = "KXPRESNOMR-28-MR"
MAX_SPEND_PER_PLATFORM = Decimal("2.00")
MAX_TOTAL_LOSS = Decimal("0.20")
MAX_KALSHI_FLATTEN_CASH_COST = Decimal("11.02")
MIN_KALSHI_SELL_ALL_PROCEEDS = Decimal("22.94")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"


def abort(message: str) -> None:
    print(f"ABORT {message}")
    raise SystemExit(2)


def load_poly_yes_token() -> tuple[str, str]:
    raw = httpx.get(
        f"https://gamma-api.polymarket.com/markets/{POLY_MARKET_ID}",
        timeout=20,
    ).json()
    question = raw.get("question") or raw.get("title") or POLY_MARKET_ID
    tokens = raw.get("clobTokenIds")
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if not tokens:
        abort("Polymarket market did not expose CLOB token IDs")
    return question, str(tokens[0])


def run_poly_preflight() -> dict[str, object]:
    from polymarket import SecureClient

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    wallet = os.getenv("POLYMARKET_FUNDER_ADDRESS") or None
    if not private_key:
        abort("missing POLYMARKET_PRIVATE_KEY")

    question, yes_token = load_poly_yes_token()
    with SecureClient.create(private_key=private_key, wallet=wallet) as client:
        balance = client.get_balance_allowance(asset_type="COLLATERAL")
        book = client.get_order_book(token_id=yes_token)
        asks = list(book.asks or [])
        bids = list(book.bids or [])
        if not asks or not bids:
            abort("Polymarket YES order book is empty")
        best_ask = min(Decimal(str(level.price)) for level in asks)
        best_bid = max(Decimal(str(level.price)) for level in bids)
        buy_amount = (best_ask * Decimal("1")).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        )
        worst_loss = buy_amount - best_bid
        print(f"POLY_PREFLIGHT question={question}")
        print(
            "POLY_PREFLIGHT "
            f"yes_token_len={len(yes_token)} bid={best_bid} ask={best_ask} "
            f"buy_amount={buy_amount} worst_loss={worst_loss}"
        )
        print(
            "POLY_PREFLIGHT "
            f"collateral_balance_base_units={balance.balance} "
            f"allowances={list(balance.allowances.keys())}"
        )
        if buy_amount > MAX_SPEND_PER_PLATFORM:
            abort(f"Polymarket spend {buy_amount} > {MAX_SPEND_PER_PLATFORM}")
        if worst_loss > MAX_TOTAL_LOSS:
            abort(f"Polymarket loss {worst_loss} > {MAX_TOTAL_LOSS}")
        return {
            "client": client,
            "yes_token": yes_token,
            "buy_amount": buy_amount,
            "best_ask": best_ask,
            "best_bid": best_bid,
            "worst_loss": worst_loss,
        }


def run_poly_live() -> Decimal:
    from polymarket import SecureClient

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    wallet = os.getenv("POLYMARKET_FUNDER_ADDRESS") or None
    preflight = run_poly_preflight()
    yes_token = str(preflight["yes_token"])
    buy_amount = Decimal(str(preflight["buy_amount"]))
    best_ask = Decimal(str(preflight["best_ask"]))
    best_bid = Decimal(str(preflight["best_bid"]))

    with SecureClient.create(private_key=private_key, wallet=wallet) as client:
        try:
            buy = client.place_market_order(
                token_id=yes_token,
                side="BUY",
                amount=str(buy_amount),
                max_spend=str(MAX_SPEND_PER_PLATFORM),
                max_price=str(best_ask),
                order_type="FOK",
            )
        except Exception as exc:
            abort(f"Polymarket buy rejected before fill: {exc}")
        print(f"POLY_BUY ok={buy.ok} status={getattr(buy, 'status', '')}")
        if not buy.ok:
            abort(f"Polymarket buy rejected: {buy.code} {buy.message}")
        try:
            sell = client.place_market_order(
                token_id=yes_token,
                side="SELL",
                shares="1",
                min_price=str(best_bid),
                order_type="FOK",
            )
        except Exception as exc:
            abort(f"Polymarket sell rejected after buy: {exc}")
        print(f"POLY_SELL ok={sell.ok} status={getattr(sell, 'status', '')}")
        if not sell.ok:
            abort(f"Polymarket sell rejected after buy: {sell.code} {sell.message}")
        loss = buy_amount - best_bid
        print(f"POLY_DONE estimated_loss={loss}")
        return loss


def _kalshi_imports():
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from kalshi import BASE_URL, _build_auth_headers

    return BASE_URL, _build_auth_headers


def _kalshi_request(method: str, api_path: str, **kwargs) -> httpx.Response:
    base_url, build_auth_headers = _kalshi_imports()
    auth_path = f"/trade-api/v2{api_path}"
    headers = kwargs.pop("headers", {})
    headers.update(build_auth_headers(method, auth_path))
    if not headers.get("KALSHI-ACCESS-KEY"):
        abort("Kalshi credentials are not configured")
    return httpx.request(
        method,
        f"{base_url}{api_path}",
        headers=headers,
        timeout=20,
        **kwargs,
    )


def _kalshi_market() -> dict:
    resp = _kalshi_request("GET", f"/markets/{KALSHI_TICKER}")
    if resp.status_code != 200:
        abort(f"Kalshi market fetch failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json().get("market", {})


def _kalshi_market_by_ticker(ticker: str) -> dict:
    resp = _kalshi_request("GET", f"/markets/{ticker}")
    if resp.status_code != 200:
        abort(f"Kalshi market fetch failed for {ticker}: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json().get("market", {})


def _kalshi_cents(raw: dict, field: str, dollar_field: str) -> int:
    if raw.get(field) not in (None, ""):
        return int(raw[field])
    value = Decimal(str(raw.get(dollar_field, "0")))
    return int((value * Decimal("100")).to_integral_value(rounding=ROUND_UP))


def run_kalshi_preflight() -> dict[str, object]:
    market = _kalshi_market()
    title = market.get("title") or KALSHI_TICKER
    no_ask = _kalshi_cents(market, "no_ask", "no_ask_dollars")
    no_bid = _kalshi_cents(market, "no_bid", "no_bid_dollars")
    if no_ask <= 0 or no_bid <= 0:
        abort("Kalshi NO book has no executable top-of-book")
    spend = Decimal(no_ask) / Decimal("100")
    loss = Decimal(no_ask - no_bid) / Decimal("100")
    print(f"KALSHI_PREFLIGHT title={title}")
    print(
        f"KALSHI_PREFLIGHT ticker={KALSHI_TICKER} no_bid={no_bid}c "
        f"no_ask={no_ask}c spend={spend} worst_loss={loss}"
    )
    if spend > MAX_SPEND_PER_PLATFORM:
        abort(f"Kalshi spend {spend} > {MAX_SPEND_PER_PLATFORM}")
    if loss > MAX_TOTAL_LOSS:
        abort(f"Kalshi loss {loss} > {MAX_TOTAL_LOSS}")
    return {"no_ask": no_ask, "no_bid": no_bid, "spend": spend, "loss": loss}


def _kalshi_create_order(payload: dict) -> dict:
    resp = _kalshi_request("POST", "/portfolio/events/orders", json=payload)
    if resp.status_code >= 400:
        abort(f"Kalshi order failed: HTTP {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _kalshi_fill_count(order: dict) -> int:
    for field in ("fill_count", "filled_count", "filled_quantity", "remaining_count", "count"):
        if field in order and order[field] not in (None, ""):
            try:
                value = int(Decimal(str(order[field])))
            except (TypeError, ValueError):
                continue
            if field == "remaining_count":
                return max(0, 1 - value)
            return value
    status = str(order.get("status", "")).lower()
    return 1 if status in {"executed", "filled"} else 0


def run_kalshi_live() -> Decimal:
    preflight = run_kalshi_preflight()
    buy_price = int(preflight["no_ask"])
    buy_yes_price = (Decimal(100 - buy_price) / Decimal("100")).quantize(Decimal("0.0001"))
    buy_payload = {
        "ticker": KALSHI_TICKER,
        "client_order_id": str(uuid.uuid4()),
        "side": "ask",
        "count": "1.00",
        "price": str(buy_yes_price),
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": False,
    }
    buy = _kalshi_create_order(buy_payload)
    print(
        "KALSHI_BUY "
        f"status={buy.get('status')} order_id={buy.get('order_id') or buy.get('id')}"
    )
    if _kalshi_fill_count(buy) < 1:
        abort(f"Kalshi buy did not fill: {buy}")

    refreshed = run_kalshi_preflight()
    sell_price = int(refreshed["no_bid"])
    sell_yes_price = (Decimal(100 - sell_price) / Decimal("100")).quantize(Decimal("0.0001"))
    loss = Decimal(buy_price - sell_price) / Decimal("100")
    if loss > MAX_TOTAL_LOSS:
        abort(f"Kalshi sell loss {loss} > {MAX_TOTAL_LOSS}; leaving bought position")
    sell_payload = {
        "ticker": KALSHI_TICKER,
        "client_order_id": str(uuid.uuid4()),
        "side": "bid",
        "count": "1.00",
        "price": str(sell_yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": True,
    }
    sell = _kalshi_create_order(sell_payload)
    print(
        "KALSHI_SELL "
        f"status={sell.get('status')} order_id={sell.get('order_id') or sell.get('id')}"
    )
    if _kalshi_fill_count(sell) < 1:
        abort(f"Kalshi sell did not fill after buy: {sell}")
    print(f"KALSHI_DONE estimated_loss={loss}")
    return loss


def run_kalshi_sell_only() -> Decimal:
    refreshed = run_kalshi_preflight()
    sell_price = int(refreshed["no_bid"])
    sell_yes_price = (Decimal(100 - sell_price) / Decimal("100")).quantize(Decimal("0.0001"))
    max_entry_price = sell_price + int(MAX_TOTAL_LOSS * Decimal("100"))
    sell_payload = {
        "ticker": KALSHI_TICKER,
        "client_order_id": str(uuid.uuid4()),
        "side": "bid",
        "count": "1.00",
        "price": str(sell_yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "reduce_only": True,
    }
    sell = _kalshi_create_order(sell_payload)
    print(
        "KALSHI_SELL_ONLY "
        f"status={sell.get('status')} order_id={sell.get('order_id') or sell.get('id')}"
    )
    if _kalshi_fill_count(sell) < 1:
        abort(f"Kalshi sell-only did not fill: {sell}")
    loss_cap = Decimal(max_entry_price - sell_price) / Decimal("100")
    print(f"KALSHI_SELL_ONLY_DONE max_loss_cap_used={loss_cap}")
    return loss_cap


def run_kalshi_position_check() -> None:
    resp = _kalshi_request("GET", "/portfolio/positions", params={"ticker": KALSHI_TICKER})
    if resp.status_code >= 400:
        abort(f"Kalshi position check failed: HTTP {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    market_positions = data.get("market_positions") or data.get("positions") or []
    relevant = [
        p for p in market_positions
        if p.get("ticker") == KALSHI_TICKER or p.get("market_ticker") == KALSHI_TICKER
    ]
    print(f"KALSHI_POSITION_CHECK count={len(relevant)}")
    for pos in relevant:
        print("KALSHI_POSITION " + json.dumps(pos, sort_keys=True))


def run_kalshi_all_positions_check() -> None:
    resp = _kalshi_request("GET", "/portfolio/positions")
    if resp.status_code >= 400:
        abort(f"Kalshi position check failed: HTTP {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    market_positions = data.get("market_positions") or data.get("positions") or []
    open_positions = []
    for pos in market_positions:
        raw_position = pos.get("position_fp", pos.get("position", "0"))
        try:
            position = Decimal(str(raw_position))
        except Exception:
            position = Decimal("0")
        resting = int(pos.get("resting_orders_count") or 0)
        exposure = Decimal(str(pos.get("market_exposure_dollars", "0")))
        if position != 0 or resting or exposure != 0:
            open_positions.append(pos)
    print(f"KALSHI_ALL_POSITIONS count={len(open_positions)}")
    total_exposure = Decimal("0")
    for pos in open_positions:
        total_exposure += Decimal(str(pos.get("market_exposure_dollars", "0")))
        print("KALSHI_POSITION " + json.dumps(pos, sort_keys=True))
    print(f"KALSHI_ALL_POSITIONS total_market_exposure_dollars={total_exposure}")


def _kalshi_open_positions() -> list[dict]:
    resp = _kalshi_request("GET", "/portfolio/positions")
    if resp.status_code >= 400:
        abort(f"Kalshi position check failed: HTTP {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    market_positions = data.get("market_positions") or data.get("positions") or []
    open_positions = []
    for pos in market_positions:
        raw_position = pos.get("position_fp", pos.get("position", "0"))
        try:
            position = Decimal(str(raw_position))
        except Exception:
            position = Decimal("0")
        if position != 0:
            item = dict(pos)
            item["_position_decimal"] = position
            open_positions.append(item)
    return open_positions


def run_kalshi_flatten_preflight() -> None:
    positions = _kalshi_open_positions()
    print(f"KALSHI_FLATTEN_PREFLIGHT count={len(positions)}")
    total_worst_cost = Decimal("0")
    for pos in positions:
        ticker = str(pos.get("ticker") or pos.get("market_ticker"))
        position = Decimal(pos["_position_decimal"])
        count = abs(position)
        market = _kalshi_market_by_ticker(ticker)
        yes_bid = _kalshi_cents(market, "yes_bid", "yes_bid_dollars")
        yes_ask = _kalshi_cents(market, "yes_ask", "yes_ask_dollars")
        if position < 0:
            close_side = "bid"
            close_desc = "buy YES to close short-YES/long-NO"
            limit_cents = yes_ask
            worst_cost = count * Decimal(limit_cents) / Decimal("100")
        else:
            close_side = "ask"
            close_desc = "sell YES to close long-YES"
            limit_cents = yes_bid
            worst_cost = Decimal("0")
        total_worst_cost += worst_cost
        print(
            "KALSHI_FLATTEN_ORDER "
            f"ticker={ticker} position={position} count={count} "
            f"close_side={close_side} limit={limit_cents}c "
            f"yes_bid={yes_bid}c yes_ask={yes_ask}c "
            f"worst_cash_cost={worst_cost} note={close_desc}"
        )
    print(f"KALSHI_FLATTEN_PREFLIGHT total_worst_cash_cost={total_worst_cost}")


def run_kalshi_sell_all_preflight() -> None:
    positions = _kalshi_open_positions()
    print(f"KALSHI_SELL_ALL_PREFLIGHT count={len(positions)}")
    total_expected_proceeds = Decimal("0")
    for pos in positions:
        ticker = str(pos.get("ticker") or pos.get("market_ticker"))
        position = Decimal(pos["_position_decimal"])
        count = abs(position)
        market = _kalshi_market_by_ticker(ticker)
        yes_bid = _kalshi_cents(market, "yes_bid", "yes_bid_dollars")
        yes_ask = _kalshi_cents(market, "yes_ask", "yes_ask_dollars")
        no_bid = _kalshi_cents(market, "no_bid", "no_bid_dollars")
        no_ask = _kalshi_cents(market, "no_ask", "no_ask_dollars")
        if position < 0:
            owned_side = "NO"
            sale_limit_cents = no_bid
        else:
            owned_side = "YES"
            sale_limit_cents = yes_bid
        expected_proceeds = count * Decimal(sale_limit_cents) / Decimal("100")
        total_expected_proceeds += expected_proceeds
        print(
            "KALSHI_SELL_ALL_ORDER "
            f"ticker={ticker} owned_side={owned_side} count={count} "
            f"limit={sale_limit_cents}c expected_proceeds={expected_proceeds} "
            f"yes_bid={yes_bid}c yes_ask={yes_ask}c no_bid={no_bid}c no_ask={no_ask}c"
        )
    print(f"KALSHI_SELL_ALL_PREFLIGHT total_expected_proceeds={total_expected_proceeds}")


def _kalshi_sell_all_plan() -> tuple[list[dict], Decimal]:
    positions = _kalshi_open_positions()
    orders = []
    total_expected_proceeds = Decimal("0")
    for pos in positions:
        ticker = str(pos.get("ticker") or pos.get("market_ticker"))
        position = Decimal(pos["_position_decimal"])
        count = abs(position)
        market = _kalshi_market_by_ticker(ticker)
        yes_bid = _kalshi_cents(market, "yes_bid", "yes_bid_dollars")
        yes_ask = _kalshi_cents(market, "yes_ask", "yes_ask_dollars")
        no_bid = _kalshi_cents(market, "no_bid", "no_bid_dollars")
        no_ask = _kalshi_cents(market, "no_ask", "no_ask_dollars")
        if position < 0:
            if no_bid <= 0 or yes_ask <= 0:
                abort(f"no executable NO bid for {ticker}")
            owned_side = "NO"
            api_side = "bid"
            api_price_cents = yes_ask
            sale_limit_cents = no_bid
        else:
            if yes_bid <= 0:
                abort(f"no executable YES bid for {ticker}")
            owned_side = "YES"
            api_side = "ask"
            api_price_cents = yes_bid
            sale_limit_cents = yes_bid
        expected_proceeds = count * Decimal(sale_limit_cents) / Decimal("100")
        total_expected_proceeds += expected_proceeds
        orders.append(
            {
                "ticker": ticker,
                "position": position,
                "count": count,
                "owned_side": owned_side,
                "api_side": api_side,
                "api_price_cents": api_price_cents,
                "sale_limit_cents": sale_limit_cents,
                "expected_proceeds": expected_proceeds,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
            }
        )
    return orders, total_expected_proceeds


def run_kalshi_sell_all_live() -> None:
    orders, total_expected_proceeds = _kalshi_sell_all_plan()
    print(
        f"KALSHI_SELL_ALL_LIVE_PRECHECK count={len(orders)} "
        f"total_expected_proceeds={total_expected_proceeds}"
    )
    if total_expected_proceeds < MIN_KALSHI_SELL_ALL_PROCEEDS:
        abort(
            f"expected proceeds {total_expected_proceeds} below confirmed minimum "
            f"{MIN_KALSHI_SELL_ALL_PROCEEDS}"
        )
    for order in orders:
        price = (Decimal(order["api_price_cents"]) / Decimal("100")).quantize(Decimal("0.0001"))
        count = Decimal(order["count"]).quantize(Decimal("0.01"))
        payload = {
            "ticker": order["ticker"],
            "client_order_id": str(uuid.uuid4()),
            "side": order["api_side"],
            "count": str(count),
            "price": str(price),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "reduce_only": True,
        }
        result = _kalshi_create_order(payload)
        order_id = result.get("order_id") or result.get("id")
        print(
            "KALSHI_SELL_ALL_SENT "
            f"ticker={order['ticker']} owned_side={order['owned_side']} "
            f"count={count} limit={order['sale_limit_cents']}c "
            f"expected_proceeds={order['expected_proceeds']} "
            f"order_id={order_id} status={result.get('status')}"
        )
    run_kalshi_all_positions_check()


def _kalshi_flatten_plan() -> tuple[list[dict], Decimal]:
    positions = _kalshi_open_positions()
    orders = []
    total_worst_cost = Decimal("0")
    for pos in positions:
        ticker = str(pos.get("ticker") or pos.get("market_ticker"))
        position = Decimal(pos["_position_decimal"])
        count = abs(position)
        market = _kalshi_market_by_ticker(ticker)
        yes_bid = _kalshi_cents(market, "yes_bid", "yes_bid_dollars")
        yes_ask = _kalshi_cents(market, "yes_ask", "yes_ask_dollars")
        if position < 0:
            if yes_ask <= 0:
                abort(f"no executable YES ask for {ticker}")
            side = "bid"
            limit_cents = yes_ask
            worst_cost = count * Decimal(limit_cents) / Decimal("100")
        else:
            if yes_bid <= 0:
                abort(f"no executable YES bid for {ticker}")
            side = "ask"
            limit_cents = yes_bid
            worst_cost = Decimal("0")
        total_worst_cost += worst_cost
        orders.append(
            {
                "ticker": ticker,
                "position": position,
                "count": count,
                "side": side,
                "limit_cents": limit_cents,
                "worst_cost": worst_cost,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
            }
        )
    return orders, total_worst_cost


def run_kalshi_flatten_live() -> None:
    orders, total_worst_cost = _kalshi_flatten_plan()
    print(
        f"KALSHI_FLATTEN_LIVE_PRECHECK count={len(orders)} "
        f"total_worst_cash_cost={total_worst_cost}"
    )
    if total_worst_cost > MAX_KALSHI_FLATTEN_CASH_COST:
        abort(
            f"flatten cost {total_worst_cost} exceeds confirmed cap "
            f"{MAX_KALSHI_FLATTEN_CASH_COST}"
        )
    for order in orders:
        price = (Decimal(order["limit_cents"]) / Decimal("100")).quantize(Decimal("0.0001"))
        count = Decimal(order["count"]).quantize(Decimal("0.01"))
        payload = {
            "ticker": order["ticker"],
            "client_order_id": str(uuid.uuid4()),
            "side": order["side"],
            "count": str(count),
            "price": str(price),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "reduce_only": True,
        }
        result = _kalshi_create_order(payload)
        order_id = result.get("order_id") or result.get("id")
        print(
            "KALSHI_FLATTEN_SENT "
            f"ticker={order['ticker']} position={order['position']} "
            f"side={order['side']} count={count} price={price} "
            f"order_id={order_id} status={result.get('status')}"
        )
    run_kalshi_all_positions_check()


def run_kalshi_order_check(order_ids: set[str]) -> None:
    if not order_ids:
        abort("provide one or more Kalshi order IDs after kalshi-order-check")
    resp = _kalshi_request("GET", "/portfolio/orders")
    if resp.status_code >= 400:
        abort(f"Kalshi order list failed: HTTP {resp.status_code} {resp.text[:500]}")
    data = resp.json()
    orders = data.get("orders") or data.get("event_orders") or []
    found = [
        order
        for order in orders
        if order.get("order_id") in order_ids or order.get("id") in order_ids
    ]
    print(f"KALSHI_ORDER_CHECK found={len(found)} returned={len(orders)}")
    for order in found:
        print("KALSHI_ORDER " + json.dumps(order, sort_keys=True))


def main() -> None:
    load_dotenv()
    mode = sys.argv[1] if len(sys.argv) > 1 else "preflight"
    if mode == "poly-preflight":
        run_poly_preflight()
        print("POLY_PREFLIGHT_OK")
    elif mode == "poly-live":
        loss = run_poly_live()
        if loss > MAX_TOTAL_LOSS:
            abort(f"estimated loss {loss} exceeds cap after Polymarket")
    elif mode == "kalshi-preflight":
        run_kalshi_preflight()
        print("KALSHI_PREFLIGHT_OK")
    elif mode == "kalshi-live":
        loss = run_kalshi_live()
        if loss > MAX_TOTAL_LOSS:
            abort(f"estimated loss {loss} exceeds cap after Kalshi")
    elif mode == "kalshi-sell-only":
        run_kalshi_sell_only()
    elif mode == "kalshi-position-check":
        run_kalshi_position_check()
    elif mode == "kalshi-all-positions":
        run_kalshi_all_positions_check()
    elif mode == "kalshi-flatten-preflight":
        run_kalshi_flatten_preflight()
    elif mode == "kalshi-sell-all-preflight":
        run_kalshi_sell_all_preflight()
    elif mode == "kalshi-sell-all-live":
        run_kalshi_sell_all_live()
    elif mode == "kalshi-flatten-live":
        run_kalshi_flatten_live()
    elif mode == "kalshi-order-check":
        run_kalshi_order_check(set(sys.argv[2:]))
    else:
        abort(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
