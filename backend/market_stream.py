import asyncio
from collections import deque
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets

import kalshi as kalshi_data
import polymarket as poly_data
from depth import analyse_depth as analyse_depth_rest
from depth import analyse_depth_from_books
from store import store


POLY_WS_URL = os.getenv("POLYMARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")


Book = dict[str, list[tuple[float, float]]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any) -> float:
    try:
        return float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _kalshi_price(value: Any) -> float:
    price = _to_float(value)
    if price > 1:
        return price / 100.0
    return price


def _copy_market(market: dict[str, Any]) -> dict[str, Any]:
    return dict(market)


def _poly_token_ids(market: dict[str, Any]) -> list[str]:
    token_ids = market.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        try:
            parsed = json.loads(token_ids)
        except json.JSONDecodeError:
            parsed = []
        token_ids = parsed if isinstance(parsed, list) else []
    elif not isinstance(token_ids, list):
        token_ids = list(token_ids) if token_ids else []

    if len(token_ids) >= 2:
        return [str(token_ids[0]), str(token_ids[1])]

    yes_token = market.get("yes_token_id")
    no_token = market.get("no_token_id")
    if yes_token and no_token:
        return [str(yes_token), str(no_token)]

    return []


def _copy_poly_book(book: Book | None) -> Book:
    if not book:
        return {"asks": [], "bids": []}
    return {
        "asks": [(float(price), float(size)) for price, size in book.get("asks", [])],
        "bids": [(float(price), float(size)) for price, size in book.get("bids", [])],
    }


def _copy_kalshi_book(book: dict[str, list[tuple[float, float]]] | None) -> dict[str, list[tuple[float, float]]]:
    if not book:
        return {"yes": [], "no": []}
    return {
        "yes": [(float(price), float(size)) for price, size in book.get("yes", [])],
        "no": [(float(price), float(size)) for price, size in book.get("no", [])],
    }


def _sort_book(book: Book) -> None:
    book["asks"].sort(key=lambda item: item[0])
    book["bids"].sort(key=lambda item: item[0], reverse=True)


def _sort_kalshi_bids(book: dict[str, list[tuple[float, float]]]) -> None:
    book["yes"].sort(key=lambda item: item[0], reverse=True)
    book["no"].sort(key=lambda item: item[0], reverse=True)


def _best_kalshi_bid(levels: list[tuple[float, float]]) -> float:
    return max((price for price, size in levels if price > 0 and size > 0), default=0.0)


def _kalshi_book_is_crossed(book: dict[str, list[tuple[float, float]]]) -> bool:
    yes_bid = _best_kalshi_bid(book.get("yes", []))
    no_bid = _best_kalshi_bid(book.get("no", []))
    return yes_bid > 0 and no_bid > 0 and yes_bid + no_bid > 1.000001


def _kalshi_asks_from_bids(book: dict[str, list[tuple[float, float]]]) -> dict[str, list[tuple[float, float]]]:
    # Kalshi exposes YES/NO bids only. The ask for one side is implied by
    # the opposite side's bid: YES ask = 1 - best NO bid, and vice versa.
    def convert(opposite_bids: list[tuple[float, float]]) -> list[tuple[float, float]]:
        levels = [(1.0 - price, size) for price, size in opposite_bids if 0 < price < 1 and size > 0]
        levels.sort(key=lambda item: item[0])
        return levels

    return {
        "yes": convert(book.get("no", [])),
        "no": convert(book.get("yes", [])),
    }


def _set_level(levels: list[tuple[float, float]], price: float, size: float) -> list[tuple[float, float]]:
    filtered = [(p, s) for p, s in levels if abs(p - price) > 1e-9]
    if size > 0:
        filtered.append((price, size))
    return filtered


def _add_level_delta(levels: list[tuple[float, float]], price: float, delta: float) -> list[tuple[float, float]]:
    current = next((size for p, size in levels if abs(p - price) <= 1e-9), 0.0)
    return _set_level(levels, price, max(0.0, current + delta))


class MarketStream:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition()
        self._task: asyncio.Task | None = None
        self._poly_task: asyncio.Task | None = None
        self._kalshi_task: asyncio.Task | None = None
        self._target_event = asyncio.Event()
        self._manual_poly_ids: set[str] = set()
        self._manual_kalshi_ids: set[str] = set()
        self._active_poly_ids: set[str] = set()
        self._active_kalshi_ids: set[str] = set()
        self._poly_markets: dict[str, dict[str, Any]] = {}
        self._kalshi_markets: dict[str, dict[str, Any]] = {}
        self._poly_market_tokens: dict[str, dict[str, str]] = {}
        self._poly_token_map: dict[str, tuple[str, str]] = {}
        self._poly_books: dict[str, Book] = {}
        self._kalshi_books: dict[str, dict[str, list[tuple[float, float]]]] = {}
        self._kalshi_resyncing: set[str] = set()
        self._sequence = 0
        self._update_history: deque[dict[str, Any]] = deque(maxlen=512)
        self._status: dict[str, Any] = {
            "running": False,
            "polymarketConnected": False,
            "kalshiConnected": False,
            "polymarketLastMessageAt": None,
            "kalshiLastMessageAt": None,
            "lastBootstrapAt": None,
            "lastTargetChangeAt": None,
            "lastError": None,
            "kalshiCrossedBookCount": 0,
            "kalshiLastCrossedBookAt": None,
            "kalshiLastBookResyncAt": None,
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._status["running"] = True
        self._task = asyncio.create_task(self._manager_loop())

    async def shutdown(self) -> None:
        tasks = [task for task in (self._poly_task, self._kalshi_task, self._task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._status["running"] = False

    def status(self) -> dict[str, Any]:
        return {
            **self._status,
            "sequence": self._sequence,
            "targetPolymarketMarkets": len(self._active_poly_ids),
            "targetKalshiMarkets": len(self._active_kalshi_ids),
            "cachedPolymarketMarkets": len(self._poly_markets),
            "cachedKalshiMarkets": len(self._kalshi_markets),
            "cachedPolymarketBooks": len(self._poly_books),
            "cachedKalshiBooks": len(self._kalshi_books),
        }

    async def ensure_targets(self, poly_ids: list[str] | set[str], kalshi_ids: list[str] | set[str]) -> None:
        changed = False
        async with self._lock:
            for market_id in poly_ids:
                if market_id and market_id not in self._manual_poly_ids:
                    self._manual_poly_ids.add(str(market_id))
                    changed = True
            for ticker in kalshi_ids:
                if ticker and ticker not in self._manual_kalshi_ids:
                    self._manual_kalshi_ids.add(str(ticker))
                    changed = True
        if changed:
            self._target_event.set()

    async def get_pair_markets(
        self,
        poly_ids: list[str] | set[str],
        kalshi_ids: list[str] | set[str],
        *,
        wait_timeout: float = 1.5,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        poly_ids = [str(i) for i in poly_ids if i]
        kalshi_ids = [str(i) for i in kalshi_ids if i]
        await self.ensure_targets(poly_ids, kalshi_ids)
        await self._wait_for_cache(poly_ids, kalshi_ids, wait_timeout)

        async with self._lock:
            poly_markets = [_copy_market(self._poly_markets[i]) for i in poly_ids if i in self._poly_markets]
            kalshi_markets = [_copy_market(self._kalshi_markets[i]) for i in kalshi_ids if i in self._kalshi_markets]
            have_poly = {m["id"] for m in poly_markets}
            have_kalshi = {m["id"] for m in kalshi_markets}

        missing_poly = [i for i in poly_ids if i not in have_poly]
        missing_kalshi = [i for i in kalshi_ids if i not in have_kalshi]
        if not missing_poly and not missing_kalshi:
            return poly_markets, kalshi_markets

        fallback_poly, fallback_kalshi = await asyncio.gather(
            poly_data.fetch_markets_by_ids(missing_poly),
            kalshi_data.fetch_markets_by_tickers(missing_kalshi),
        )
        await self._cache_bootstrap_markets(fallback_poly, fallback_kalshi, source="rest-fallback")

        async with self._lock:
            poly_markets = [_copy_market(self._poly_markets[i]) for i in poly_ids if i in self._poly_markets]
            kalshi_markets = [_copy_market(self._kalshi_markets[i]) for i in kalshi_ids if i in self._kalshi_markets]
        return poly_markets, kalshi_markets

    async def analyse_depth(
        self,
        poly_id: str,
        kalshi_id: str,
        edge_threshold: float = 0.005,
        max_leg_slippage: float = 0.01,
        max_bet_dollars: float = 100_000.0,
        poly_fee: float = 0.0,
        kalshi_fee: float = 0.0,
    ) -> dict:
        await self.ensure_targets([poly_id], [kalshi_id])
        await self._wait_for_cache([poly_id], [kalshi_id], 1.0)
        async with self._lock:
            tokens = self._poly_market_tokens.get(poly_id, {})
            yes_book = _copy_poly_book(self._poly_books.get(tokens.get("YES", "")))
            no_book = _copy_poly_book(self._poly_books.get(tokens.get("NO", "")))
            kalshi_bids = _copy_kalshi_book(self._kalshi_books.get(kalshi_id))
            kalshi_asks = _kalshi_asks_from_bids(kalshi_bids)

        if _kalshi_book_is_crossed(kalshi_bids):
            self._schedule_kalshi_resync(kalshi_id)
        elif yes_book["asks"] and no_book["asks"] and (kalshi_asks["yes"] or kalshi_asks["no"]):
            result = analyse_depth_from_books(
                yes_book,
                no_book,
                kalshi_asks,
                edge_threshold=edge_threshold,
                max_leg_slippage=max_leg_slippage,
                max_bet_dollars=max_bet_dollars,
                poly_fee=poly_fee,
                kalshi_fee=kalshi_fee,
            )
            result["source"] = "websocket-cache"
            return result

        return await analyse_depth_rest(
            poly_id,
            kalshi_id,
            edge_threshold,
            max_leg_slippage,
            max_bet_dollars,
            poly_fee,
            kalshi_fee,
        )

    async def snapshot(
        self,
        poly_ids: list[str] | set[str],
        kalshi_ids: list[str] | set[str],
        *,
        wait_timeout: float = 0.5,
    ) -> dict[str, Any]:
        poly_markets, kalshi_markets = await self.get_pair_markets(poly_ids, kalshi_ids, wait_timeout=wait_timeout)
        return {
            "type": "market_snapshot",
            "sequence": self._sequence,
            "polymarket": poly_markets,
            "kalshi": kalshi_markets,
            "stream": self.status(),
        }

    async def wait_for_update_after(self, sequence: int, timeout: float | None = None) -> int:
        async with self._condition:
            if self._sequence > sequence:
                return self._sequence
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._sequence > sequence),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass
            return self._sequence

    async def wait_for_update(self, timeout: float | None = None) -> bool:
        sequence = self._sequence
        return await self.wait_for_update_after(sequence, timeout) > sequence

    async def wait_for_change_after(self, sequence: int, timeout: float | None = None) -> dict[str, Any]:
        next_sequence = await self.wait_for_update_after(sequence, timeout=timeout)
        async with self._lock:
            if next_sequence <= sequence:
                return {
                    "changed": False,
                    "sequence": next_sequence,
                    "all": False,
                    "polyIds": [],
                    "kalshiIds": [],
                }
            history = [item for item in self._update_history if sequence < item["sequence"] <= next_sequence]
            history_missed = len(history) == 0
            all_changed = history_missed or any(item.get("all") for item in history)
            poly_ids: set[str] = set()
            kalshi_ids: set[str] = set()
            for item in history:
                poly_ids.update(item.get("polyIds") or [])
                kalshi_ids.update(item.get("kalshiIds") or [])
            return {
                "changed": True,
                "sequence": next_sequence,
                "all": all_changed,
                "polyIds": sorted(poly_ids),
                "kalshiIds": sorted(kalshi_ids),
            }

    async def _manager_loop(self) -> None:
        while True:
            try:
                poly_ids, kalshi_ids = self._store_targets()
                async with self._lock:
                    poly_ids |= set(self._manual_poly_ids)
                    kalshi_ids |= set(self._manual_kalshi_ids)

                if poly_ids != self._active_poly_ids or kalshi_ids != self._active_kalshi_ids:
                    await self._retarget(poly_ids, kalshi_ids)

                self._target_event.clear()
                try:
                    await asyncio.wait_for(self._target_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status["lastError"] = f"market stream manager: {exc}"
                await asyncio.sleep(2.0)

    def _store_targets(self) -> tuple[set[str], set[str]]:
        pairs = store.list_whitelisted_pairs()
        positions = store.list_positions("open")
        poly_ids = {str(item.get("polyId")) for item in [*pairs, *positions] if item.get("polyId")}
        kalshi_ids = {str(item.get("kalshiId")) for item in [*pairs, *positions] if item.get("kalshiId")}
        return poly_ids, kalshi_ids

    async def _retarget(self, poly_ids: set[str], kalshi_ids: set[str]) -> None:
        for task in (self._poly_task, self._kalshi_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *[task for task in (self._poly_task, self._kalshi_task) if task],
            return_exceptions=True,
        )

        self._active_poly_ids = set(poly_ids)
        self._active_kalshi_ids = set(kalshi_ids)
        self._status["lastTargetChangeAt"] = _utc_now()
        self._status["polymarketConnected"] = False
        self._status["kalshiConnected"] = False

        await self._bootstrap_targets(poly_ids, kalshi_ids)

        asset_ids = sorted(self._poly_token_map.keys())
        if asset_ids:
            self._poly_task = asyncio.create_task(self._polymarket_loop(asset_ids))
        else:
            self._poly_task = None

        if kalshi_ids:
            self._kalshi_task = asyncio.create_task(self._kalshi_loop(sorted(kalshi_ids)))
        else:
            self._kalshi_task = None

        await self._notify_update(all_changed=True)

    async def _bootstrap_targets(self, poly_ids: set[str], kalshi_ids: set[str]) -> None:
        poly_markets, kalshi_markets = await asyncio.gather(
            poly_data.fetch_markets_by_ids(sorted(poly_ids)),
            kalshi_data.fetch_markets_by_tickers(sorted(kalshi_ids)),
        )
        await self._cache_bootstrap_markets(poly_markets, kalshi_markets, source="rest-bootstrap")
        await self._bootstrap_books()
        self._status["lastBootstrapAt"] = _utc_now()

    async def _cache_bootstrap_markets(
        self,
        poly_markets: list[dict[str, Any]],
        kalshi_markets: list[dict[str, Any]],
        *,
        source: str,
    ) -> None:
        async with self._lock:
            for market in poly_markets:
                market = dict(market)
                market["live_source"] = source
                market["live_updated_at"] = _utc_now()
                self._poly_markets[market["id"]] = market
                token_ids = _poly_token_ids(market)
                if len(token_ids) >= 2:
                    yes_token = token_ids[0]
                    no_token = token_ids[1]
                    market["clobTokenIds"] = [yes_token, no_token]
                    market["yes_token_id"] = yes_token
                    market["no_token_id"] = no_token
                    self._poly_market_tokens[market["id"]] = {"YES": yes_token, "NO": no_token}
                    self._poly_token_map[yes_token] = (market["id"], "YES")
                    self._poly_token_map[no_token] = (market["id"], "NO")

            for market in kalshi_markets:
                market = dict(market)
                market["live_source"] = source
                market["live_updated_at"] = _utc_now()
                self._kalshi_markets[market["id"]] = market
        await self._notify_update(
            changed_poly_ids=[str(market.get("id")) for market in poly_markets if market.get("id")],
            changed_kalshi_ids=[str(market.get("id")) for market in kalshi_markets if market.get("id")],
        )

    async def _bootstrap_books(self) -> None:
        async with self._lock:
            token_ids = sorted(self._poly_token_map.keys())
            tickers = sorted(self._active_kalshi_ids)
        async with httpx.AsyncClient(timeout=20.0) as client:
            poly_results, kalshi_results = await asyncio.gather(
                asyncio.gather(*[self._fetch_poly_book(client, token_id) for token_id in token_ids], return_exceptions=True),
                asyncio.gather(*[self._fetch_kalshi_book(client, ticker) for ticker in tickers], return_exceptions=True),
            )

        async with self._lock:
            changed_poly_ids: set[str] = set()
            changed_kalshi_ids: set[str] = set()
            for token_id, result in zip(token_ids, poly_results):
                if isinstance(result, Exception):
                    continue
                self._poly_books[token_id] = result
                market_id = self._touch_poly_market_for_token(token_id, source="rest-bootstrap")
                if market_id:
                    changed_poly_ids.add(market_id)
            for ticker, result in zip(tickers, kalshi_results):
                if isinstance(result, Exception):
                    continue
                self._kalshi_books[ticker] = result
                market_id = self._touch_kalshi_market_from_book(ticker, source="rest-bootstrap")
                if market_id:
                    changed_kalshi_ids.add(market_id)
        await self._notify_update(changed_poly_ids=changed_poly_ids, changed_kalshi_ids=changed_kalshi_ids)

    async def _fetch_poly_book(self, client: httpx.AsyncClient, token_id: str) -> Book:
        resp = await client.get(f"{poly_data.CLOB_BASE}/book", params={"token_id": token_id})
        if resp.status_code != 200:
            return {"asks": [], "bids": []}
        data = resp.json()
        book = {
            "asks": [(float(item["price"]), float(item["size"])) for item in data.get("asks", [])],
            "bids": [(float(item["price"]), float(item["size"])) for item in data.get("bids", [])],
        }
        _sort_book(book)
        return book

    async def _fetch_kalshi_book(self, client: httpx.AsyncClient, ticker: str) -> dict[str, list[tuple[float, float]]]:
        path = f"/trade-api/v2/markets/{ticker}/orderbook"
        resp = await client.get(
            f"{kalshi_data.BASE_URL}/markets/{ticker}/orderbook",
            headers=kalshi_data._build_auth_headers("GET", path),
            params={"depth": 50},
        )
        if resp.status_code != 200:
            return {"yes": [], "no": []}
        payload = resp.json().get("orderbook_fp") or resp.json().get("orderbook") or {}
        return {
            "yes": self._parse_kalshi_levels(payload.get("yes_dollars") or payload.get("yes") or []),
            "no": self._parse_kalshi_levels(payload.get("no_dollars") or payload.get("no") or []),
        }

    def _parse_kalshi_levels(self, entries: list[Any]) -> list[tuple[float, float]]:
        levels: list[tuple[float, float]] = []
        for entry in entries:
            if isinstance(entry, dict):
                price = _kalshi_price(entry.get("price") or entry.get("price_dollars"))
                size = _to_float(entry.get("size") or entry.get("quantity") or entry.get("count"))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = _kalshi_price(entry[0])
                size = _to_float(entry[1])
            else:
                continue
            if price > 0 and size > 0:
                levels.append((price, size))
        levels.sort(key=lambda item: item[0], reverse=True)
        return levels

    def _note_crossed_kalshi_book(self, ticker: str) -> None:
        self._status["kalshiCrossedBookCount"] = int(self._status.get("kalshiCrossedBookCount") or 0) + 1
        self._status["kalshiLastCrossedBookAt"] = _utc_now()

    def _schedule_kalshi_resync(self, ticker: str) -> None:
        if not ticker or ticker in self._kalshi_resyncing:
            return
        self._kalshi_resyncing.add(ticker)
        asyncio.create_task(self._resync_kalshi_book(ticker))

    async def _resync_kalshi_book(self, ticker: str) -> None:
        changed_kalshi_ids: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                book = await self._fetch_kalshi_book(client, ticker)
            async with self._lock:
                self._kalshi_books[ticker] = book
                if _kalshi_book_is_crossed(book):
                    self._note_crossed_kalshi_book(ticker)
                else:
                    market_id = self._touch_kalshi_market_from_book(ticker, source="rest-resync")
                    if market_id:
                        changed_kalshi_ids.add(market_id)
                    self._status["kalshiLastBookResyncAt"] = _utc_now()
        except Exception as exc:
            async with self._lock:
                self._status["lastError"] = f"kalshi book resync {ticker}: {exc}"
        finally:
            async with self._lock:
                self._kalshi_resyncing.discard(ticker)
        if changed_kalshi_ids:
            await self._notify_update(changed_kalshi_ids=changed_kalshi_ids)

    async def _connect_ws(self, url: str, headers: dict[str, str] | None = None):
        kwargs = {"ping_interval": 10, "ping_timeout": 10, "close_timeout": 2}
        try:
            return await websockets.connect(url, additional_headers=headers or None, **kwargs)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers or None, **kwargs)

    async def _polymarket_loop(self, asset_ids: list[str]) -> None:
        while asset_ids:
            ws = None
            try:
                ws = await self._connect_ws(POLY_WS_URL)
                self._status["polymarketConnected"] = True
                await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market"}))
                async for raw in ws:
                    await self._handle_polymarket_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status["polymarketConnected"] = False
                self._status["lastError"] = f"polymarket websocket: {exc}"
                await asyncio.sleep(2.0)
            finally:
                if ws:
                    await ws.close()

    async def _kalshi_loop(self, tickers: list[str]) -> None:
        headers = kalshi_data._build_auth_headers("GET", "/trade-api/ws/v2")
        if not headers:
            self._status["lastError"] = "kalshi websocket: credentials unavailable"
            return
        while tickers:
            ws = None
            try:
                ws = await self._connect_ws(KALSHI_WS_URL, headers=headers)
                self._status["kalshiConnected"] = True
                await ws.send(json.dumps({
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["orderbook_delta", "ticker"],
                        "market_tickers": tickers,
                    },
                }))
                async for raw in ws:
                    await self._handle_kalshi_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status["kalshiConnected"] = False
                self._status["lastError"] = f"kalshi websocket: {exc}"
                await asyncio.sleep(2.0)
            finally:
                if ws:
                    await ws.close()

    async def _handle_polymarket_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw in ("PING", "PONG"):
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        messages = payload if isinstance(payload, list) else [payload]
        changed = False
        changed_poly_ids: set[str] = set()
        async with self._lock:
            for message in messages:
                event_type = message.get("event_type") or message.get("type")
                if event_type == "book":
                    token_id = str(message.get("asset_id") or message.get("assetId") or "")
                    if not token_id:
                        continue
                    book = {
                        "asks": [(float(item["price"]), float(item["size"])) for item in message.get("asks", [])],
                        "bids": [(float(item["price"]), float(item["size"])) for item in message.get("bids", [])],
                    }
                    _sort_book(book)
                    self._poly_books[token_id] = book
                    market_id = self._touch_poly_market_for_token(token_id, source="websocket")
                    if market_id:
                        changed_poly_ids.add(market_id)
                    changed = True
                elif event_type == "price_change":
                    for change in message.get("price_changes", []) or [message]:
                        token_id = str(change.get("asset_id") or change.get("assetId") or "")
                        if not token_id:
                            continue
                        book = self._poly_books.setdefault(token_id, {"asks": [], "bids": []})
                        price = _to_float(change.get("price"))
                        size = _to_float(change.get("size"))
                        side = str(change.get("side") or "").upper()
                        if side in ("BUY", "BID", "BIDS"):
                            book["bids"] = _set_level(book["bids"], price, size)
                        elif side in ("SELL", "ASK", "ASKS"):
                            book["asks"] = _set_level(book["asks"], price, size)
                        _sort_book(book)
                        market_id = self._touch_poly_market_for_token(token_id, source="websocket")
                        if market_id:
                            changed_poly_ids.add(market_id)
                        changed = True
            if changed:
                self._status["polymarketLastMessageAt"] = _utc_now()
        if changed:
            await self._notify_update(changed_poly_ids=changed_poly_ids)

    async def _handle_kalshi_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        message_type = payload.get("type")
        msg = payload.get("msg") or {}
        changed = False
        changed_kalshi_ids: set[str] = set()
        async with self._lock:
            if message_type == "orderbook_snapshot":
                ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
                if ticker:
                    self._kalshi_books[ticker] = {
                        "yes": self._parse_kalshi_levels(msg.get("yes_dollars_fp") or msg.get("yes_dollars") or msg.get("yes") or []),
                        "no": self._parse_kalshi_levels(msg.get("no_dollars_fp") or msg.get("no_dollars") or msg.get("no") or []),
                    }
                    if _kalshi_book_is_crossed(self._kalshi_books[ticker]):
                        self._note_crossed_kalshi_book(ticker)
                        self._schedule_kalshi_resync(ticker)
                    else:
                        market_id = self._touch_kalshi_market_from_book(ticker, source="websocket")
                        if market_id:
                            changed_kalshi_ids.add(market_id)
                            changed = True
            elif message_type == "orderbook_delta":
                ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
                side = str(msg.get("side") or "").lower()
                if ticker and side in ("yes", "no"):
                    book = self._kalshi_books.setdefault(ticker, {"yes": [], "no": []})
                    price = _kalshi_price(msg.get("price") or msg.get("price_dollars"))
                    delta = _to_float(msg.get("delta") or msg.get("delta_fp") or msg.get("quantity_delta"))
                    book[side] = _add_level_delta(book[side], price, delta)
                    book[side].sort(key=lambda item: item[0], reverse=True)
                    if _kalshi_book_is_crossed(book):
                        self._note_crossed_kalshi_book(ticker)
                        self._schedule_kalshi_resync(ticker)
                    else:
                        market_id = self._touch_kalshi_market_from_book(ticker, source="websocket")
                        if market_id:
                            changed_kalshi_ids.add(market_id)
                            changed = True
            elif message_type == "ticker":
                ticker = str(msg.get("market_ticker") or msg.get("ticker") or "")
                if ticker:
                    market_id = self._touch_kalshi_market_from_ticker(ticker, msg)
                    if market_id:
                        changed_kalshi_ids.add(market_id)
                    changed = True
            if changed:
                self._status["kalshiLastMessageAt"] = _utc_now()
        if changed:
            await self._notify_update(changed_kalshi_ids=changed_kalshi_ids)

    def _touch_poly_market_for_token(self, token_id: str, *, source: str) -> str | None:
        mapped = self._poly_token_map.get(token_id)
        if not mapped:
            return None
        market_id, _ = mapped
        market = self._poly_markets.get(market_id)
        tokens = self._poly_market_tokens.get(market_id, {})
        if not market or not tokens:
            return None
        yes_book = self._poly_books.get(tokens.get("YES", ""))
        no_book = self._poly_books.get(tokens.get("NO", ""))
        if yes_book:
            market["yes_ask"] = yes_book["asks"][0][0] if yes_book["asks"] else market.get("yes_ask", 0)
            market["yes_bid"] = yes_book["bids"][0][0] if yes_book["bids"] else market.get("yes_bid", 0)
            market["best_ask"] = market["yes_ask"]
            market["best_bid"] = market["yes_bid"]
        if no_book:
            market["no_ask"] = no_book["asks"][0][0] if no_book["asks"] else market.get("no_ask", 0)
            market["no_bid"] = no_book["bids"][0][0] if no_book["bids"] else market.get("no_bid", 0)
        market["live_source"] = source
        market["live_updated_at"] = _utc_now()
        return market_id

    def _touch_kalshi_market_from_book(self, ticker: str, *, source: str) -> str | None:
        market = self._kalshi_markets.get(ticker)
        book = self._kalshi_books.get(ticker)
        if not market or not book:
            return None
        _sort_kalshi_bids(book)
        if _kalshi_book_is_crossed(book):
            return None
        yes_bid = _best_kalshi_bid(book.get("yes", []))
        no_bid = _best_kalshi_bid(book.get("no", []))
        yes_ask = max(0.0, 1.0 - no_bid) if no_bid > 0 else _to_float(market.get("yes_ask"))
        no_ask = max(0.0, 1.0 - yes_bid) if yes_bid > 0 else _to_float(market.get("no_ask"))
        if yes_bid > 0:
            market["yes_bid"] = yes_bid
        if no_bid > 0:
            market["no_bid"] = no_bid
        if yes_ask > 0:
            market["yes_ask"] = yes_ask
        if no_ask > 0:
            market["no_ask"] = no_ask
        market["best_ask"] = market.get("yes_ask", 0)
        market["best_bid"] = market.get("yes_bid", 0)
        market["live_source"] = source
        market["live_updated_at"] = _utc_now()
        return ticker

    def _touch_kalshi_market_from_ticker(self, ticker: str, msg: dict[str, Any]) -> str | None:
        market = self._kalshi_markets.get(ticker)
        if not market:
            return None
        yes_bid = _kalshi_price(msg.get("yes_bid") or msg.get("yes_bid_dollars"))
        yes_ask = _kalshi_price(msg.get("yes_ask") or msg.get("yes_ask_dollars"))
        if yes_bid > 0:
            market["yes_bid"] = yes_bid
            market["no_ask"] = max(0.0, 1.0 - yes_bid)
        if yes_ask > 0:
            market["yes_ask"] = yes_ask
            market["no_bid"] = max(0.0, 1.0 - yes_ask)
        market["best_ask"] = market.get("yes_ask", 0)
        market["best_bid"] = market.get("yes_bid", 0)
        market["live_source"] = "websocket"
        market["live_updated_at"] = _utc_now()
        return ticker

    async def _wait_for_cache(self, poly_ids: list[str], kalshi_ids: list[str], timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with self._lock:
                if all(i in self._poly_markets for i in poly_ids) and all(i in self._kalshi_markets for i in kalshi_ids):
                    return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    def poly_token_id(self, market_id: str, action: str) -> str:
        tokens = self._poly_market_tokens.get(str(market_id)) or {}
        return str(tokens.get(action.upper()) or "")

    async def _notify_update(
        self,
        *,
        changed_poly_ids: list[str] | set[str] | None = None,
        changed_kalshi_ids: list[str] | set[str] | None = None,
        all_changed: bool = False,
    ) -> None:
        async with self._condition:
            self._sequence += 1
            self._update_history.append(
                {
                    "sequence": self._sequence,
                    "polyIds": sorted({str(item) for item in changed_poly_ids or [] if item}),
                    "kalshiIds": sorted({str(item) for item in changed_kalshi_ids or [] if item}),
                    "all": all_changed,
                }
            )
            self._condition.notify_all()


market_stream = MarketStream()
