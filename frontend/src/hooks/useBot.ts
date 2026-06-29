import { useCallback, useEffect, useState } from "react";
import type { BotMode, BotSettings, BotStatus } from "../types";

export function useBot() {
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/bot/status");
      if (!res.ok) throw new Error(`Bot status failed with HTTP ${res.status}`);
      setStatus(await res.json());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bot status failed");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const setMode = useCallback(
    async (mode: BotMode) => {
      setLoading(true);
      try {
        const res = await fetch("/api/bot/mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        });
        if (!res.ok) throw new Error(`Mode change failed with HTTP ${res.status}`);
        setStatus(await res.json());
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Mode change failed");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  const saveSettings = useCallback(async (settings: Partial<BotSettings>) => {
    setSaving(true);
    try {
      const payload = { ...settings };
      if (payload.telegram_bot_token === "") delete payload.telegram_bot_token;
      const res = await fetch("/api/bot/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`Settings save failed with HTTP ${res.status}`);
      setStatus(await res.json());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Settings save failed");
    } finally {
      setSaving(false);
    }
  }, []);

  const scanNow = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/bot/scan", { method: "POST" });
      if (!res.ok) throw new Error(`Manual scan failed with HTTP ${res.status}`);
      const payload = await res.json();
      setStatus(payload.status);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Manual scan failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const testTelegram = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/bot/telegram/test", { method: "POST" });
      if (!res.ok) throw new Error(`Telegram test failed with HTTP ${res.status}`);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Telegram test failed");
    } finally {
      setLoading(false);
    }
  }, []);

  return { status, loading, saving, error, refresh, setMode, saveSettings, scanNow, testTelegram };
}
