import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Market, WhitelistedPair } from "../types";

function wsUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

export function useLivePairMarkets(pairs: WhitelistedPair[]) {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [connected, setConnected] = useState(false);
  const [streamStatus, setStreamStatus] = useState<Record<string, unknown> | null>(null);
  const reconnectTimer = useRef<number | null>(null);

  const ids = useMemo(() => {
    const polyIds = [...new Set(pairs.map((pair) => pair.polyId).filter(Boolean))];
    const kalshiIds = [...new Set(pairs.map((pair) => pair.kalshiId).filter(Boolean))];
    return { polyIds, kalshiIds, key: `${polyIds.join(",")}::${kalshiIds.join(",")}` };
  }, [pairs]);

  const refresh = useCallback(async () => {
    if (!ids.polyIds.length && !ids.kalshiIds.length) {
      setMarkets([]);
      return;
    }
    setRefreshing(true);
    try {
      const res = await fetch(
        `/api/markets/pairs?poly_ids=${encodeURIComponent(ids.polyIds.join(","))}&kalshi_ids=${encodeURIComponent(ids.kalshiIds.join(","))}`,
      );
      if (res.ok) {
        const data = await res.json();
        setMarkets([...(data.polymarket ?? []), ...(data.kalshi ?? [])]);
      }
    } finally {
      setRefreshing(false);
    }
  }, [ids]);

  useEffect(() => {
    if (!ids.polyIds.length && !ids.kalshiIds.length) {
      setMarkets([]);
      setConnected(false);
      return;
    }

    let closed = false;
    let socket: WebSocket | null = null;

    function connect() {
      socket = new WebSocket(wsUrl("/ws/markets"));

      socket.onopen = () => {
        setConnected(true);
        socket?.send(JSON.stringify({
          type: "subscribe",
          polyIds: ids.polyIds,
          kalshiIds: ids.kalshiIds,
        }));
      };

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.stream) setStreamStatus(payload.stream);
          if (payload.type === "market_snapshot") {
            setMarkets([...(payload.polymarket ?? []), ...(payload.kalshi ?? [])]);
          }
        } catch {
          // Ignore malformed stream frames; the socket will continue receiving.
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (!closed) {
          reconnectTimer.current = window.setTimeout(connect, 1500);
        }
      };

      socket.onerror = () => {
        socket?.close();
      };
    }

    connect();

    return () => {
      closed = true;
      if (reconnectTimer.current !== null) {
        window.clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
      socket?.close();
    };
  }, [ids.key]);

  return { markets, refreshing, connected, streamStatus, refresh };
}
