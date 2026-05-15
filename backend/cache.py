import time
from dataclasses import dataclass, field


@dataclass
class CacheEntry:
    data: list[dict] = field(default_factory=list)
    timestamp: float = 0.0
    filters_key: str = ""


class MarketCache:
    """Simple in-memory cache with TTL for market data."""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._poly_cache = CacheEntry()
        self._kalshi_cache = CacheEntry()

    def _make_key(self, **filters) -> str:
        return str(sorted(filters.items()))

    def get_polymarket(self, **filters) -> list[dict] | None:
        key = self._make_key(**filters)
        if (
            self._poly_cache.filters_key == key
            and self._poly_cache.data
            and (time.time() - self._poly_cache.timestamp) < self._ttl
        ):
            return self._poly_cache.data
        return None

    def set_polymarket(self, data: list[dict], **filters):
        self._poly_cache = CacheEntry(
            data=data,
            timestamp=time.time(),
            filters_key=self._make_key(**filters),
        )

    def get_kalshi(self, **filters) -> list[dict] | None:
        key = self._make_key(**filters)
        if (
            self._kalshi_cache.filters_key == key
            and self._kalshi_cache.data
            and (time.time() - self._kalshi_cache.timestamp) < self._ttl
        ):
            return self._kalshi_cache.data
        return None

    def set_kalshi(self, data: list[dict], **filters):
        self._kalshi_cache = CacheEntry(
            data=data,
            timestamp=time.time(),
            filters_key=self._make_key(**filters),
        )

    def invalidate(self):
        self._poly_cache = CacheEntry()
        self._kalshi_cache = CacheEntry()

    @property
    def poly_age_seconds(self) -> float | None:
        if self._poly_cache.timestamp == 0:
            return None
        return time.time() - self._poly_cache.timestamp

    @property
    def kalshi_age_seconds(self) -> float | None:
        if self._kalshi_cache.timestamp == 0:
            return None
        return time.time() - self._kalshi_cache.timestamp


cache = MarketCache(ttl_seconds=300)
