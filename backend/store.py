import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any


PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.getenv("THANOS_DB_PATH", os.path.join(DATA_DIR, "thanos.sqlite3"))


DEFAULT_BOT_SETTINGS: dict[str, Any] = {
    "mode": "off",
    "scan_interval_seconds": 5,
    "open_enabled": True,
    "close_enabled": True,
    "min_net_edge": 0.05,
    "max_leg_slippage": 0.01,
    "fixed_trade_dollars": 100.0,
    "max_total_capital": 1000.0,
    "max_open_positions": 3,
    "min_hours_to_close": 24.0,
    "poly_fee": 0.02,
    "kalshi_fee": 0.07,
    "take_profit_enabled": True,
    "take_profit_pct": 2.0,
    "stop_loss_enabled": False,
    "stop_loss_pct": -10.0,
    "close_before_close_enabled": False,
    "close_before_close_hours": 6.0,
    "edge_reversion_enabled": True,
    "close_edge_below_pct": 0.0,
    "min_close_profit_pct": 0.0,
    "max_daily_loss": 100.0,
    "max_daily_trades": 20,
    "max_consecutive_failures": 3,
    "stop_on_api_error": True,
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "notify_bot_status": True,
    "notify_trade_opened": True,
    "notify_trade_closed": True,
    "notify_trade_failed": True,
    "notify_circuit_breaker": True,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS whitelisted_pairs (
                    id TEXT PRIMARY KEY,
                    poly_id TEXT NOT NULL,
                    poly_title TEXT NOT NULL,
                    kalshi_id TEXT NOT NULL,
                    kalshi_title TEXT NOT NULL,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    closed_at TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def get_json(self, table: str, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(f"SELECT value_json FROM {table} WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value_json"])
            except json.JSONDecodeError:
                return default

    def set_json(self, table: str, key: str, value: Any) -> None:
        now = utc_now()
        with self._lock:
            self._conn.execute(
                f"""
                INSERT INTO {table} (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), now),
            )
            self._conn.commit()

    def get_bot_settings(self) -> dict[str, Any]:
        saved = self.get_json("app_settings", "bot_settings", {}) or {}
        return {**DEFAULT_BOT_SETTINGS, **saved}

    def update_bot_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.get_bot_settings()
        for key, value in updates.items():
            if key not in DEFAULT_BOT_SETTINGS:
                continue
            if key == "telegram_bot_token" and value == "":
                continue
            current[key] = value
        self.set_json("app_settings", "bot_settings", current)
        return current

    def set_bot_mode(self, mode: str) -> dict[str, Any]:
        return self.update_bot_settings({"mode": mode})

    def list_whitelisted_pairs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, poly_id, poly_title, kalshi_id, kalshi_title, added_at
                FROM whitelisted_pairs
                ORDER BY added_at DESC
                """
            ).fetchall()
        return [self._pair_from_row(row) for row in rows]

    def upsert_whitelisted_pair(self, pair: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        pair_id = pair.get("id") or f"{pair['polyId']}::{pair['kalshiId']}"
        added_at = pair.get("addedAt") or now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO whitelisted_pairs
                    (id, poly_id, poly_title, kalshi_id, kalshi_title, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    poly_title = excluded.poly_title,
                    kalshi_title = excluded.kalshi_title
                """,
                (
                    pair_id,
                    pair["polyId"],
                    pair["polyTitle"],
                    pair["kalshiId"],
                    pair["kalshiTitle"],
                    added_at,
                ),
            )
            self._conn.commit()
        return {
            "id": pair_id,
            "polyId": pair["polyId"],
            "polyTitle": pair["polyTitle"],
            "kalshiId": pair["kalshiId"],
            "kalshiTitle": pair["kalshiTitle"],
            "addedAt": added_at,
        }

    def import_whitelisted_pairs(self, pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for pair in pairs:
            if all(pair.get(k) for k in ("polyId", "polyTitle", "kalshiId", "kalshiTitle")):
                self.upsert_whitelisted_pair(pair)
        return self.list_whitelisted_pairs()

    def delete_whitelisted_pair(self, pair_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM whitelisted_pairs WHERE id = ?", (pair_id,))
            self._conn.commit()

    def list_positions(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM positions"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY added_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        positions = []
        for row in rows:
            try:
                positions.append(json.loads(row["payload_json"]))
            except json.JSONDecodeError:
                continue
        return positions

    def upsert_position(self, position: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        pos = dict(position)
        pos.setdefault("id", os.urandom(16).hex())
        pos.setdefault("addedAt", now)
        pos.setdefault("status", "open")
        status = pos.get("status", "open")
        closed_at = pos.get("closedAt")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO positions (id, status, added_at, closed_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    closed_at = excluded.closed_at,
                    payload_json = excluded.payload_json
                """,
                (pos["id"], status, pos["addedAt"], closed_at, json.dumps(pos)),
            )
            self._conn.commit()
        return pos

    def import_positions(self, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for position in positions:
            if position.get("id"):
                self.upsert_position(position)
        return self.list_positions()

    def delete_position(self, position_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
            self._conn.commit()

    def close_position(
        self,
        position_id: str,
        realized_pnl: float,
        close_reason: str = "",
        closed_at: str | None = None,
    ) -> dict[str, Any] | None:
        closed_at = closed_at or utc_now()
        positions = self.list_positions()
        for pos in positions:
            if pos.get("id") != position_id:
                continue
            pos["status"] = "closed"
            pos["closedAt"] = closed_at
            pos["realizedPnl"] = realized_pnl
            if close_reason:
                pos["closeReason"] = close_reason
            return self.upsert_position(pos)
        return None

    def log(self, level: str, event_type: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        created_at = utc_now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO bot_logs (created_at, level, event_type, message, data_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (created_at, level, event_type, message, json.dumps(data or {})),
            )
            self._conn.commit()
            log_id = cur.lastrowid
        return {
            "id": log_id,
            "createdAt": created_at,
            "level": level,
            "eventType": event_type,
            "message": message,
            "data": data or {},
        }

    def recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, created_at, level, event_type, message, data_json
                FROM bot_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._log_from_row(row) for row in rows]

    def daily_trade_count(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM bot_logs
                WHERE substr(created_at, 1, 10) = ?
                  AND event_type IN ('trade_opened', 'trade_closed', 'paper_trade_opened', 'paper_trade_closed')
                """,
                (today,),
            ).fetchone()
        return int(row["count"] or 0)

    def daily_realized_pnl(self) -> float:
        total = 0.0
        today = datetime.now(timezone.utc).date().isoformat()
        for position in self.list_positions():
            closed_at = str(position.get("closedAt") or "")
            if closed_at.startswith(today):
                try:
                    total += float(position.get("realizedPnl") or 0)
                except (TypeError, ValueError):
                    continue
        return total

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.get_json("bot_state", key, default)

    def set_state(self, key: str, value: Any) -> None:
        self.set_json("bot_state", key, value)

    @staticmethod
    def _pair_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "polyId": row["poly_id"],
            "polyTitle": row["poly_title"],
            "kalshiId": row["kalshi_id"],
            "kalshiTitle": row["kalshi_title"],
            "addedAt": row["added_at"],
        }

    @staticmethod
    def _log_from_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            data = json.loads(row["data_json"])
        except json.JSONDecodeError:
            data = {}
        return {
            "id": row["id"],
            "createdAt": row["created_at"],
            "level": row["level"],
            "eventType": row["event_type"],
            "message": row["message"],
            "data": data,
        }


store = Store()
