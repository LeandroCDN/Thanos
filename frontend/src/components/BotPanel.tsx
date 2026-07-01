import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useBot } from "../hooks/useBot";
import type { BotMode, BotSettings } from "../types";

const MODE_LABELS: Record<BotMode, string> = {
  off: "OFF",
  test: "TEST",
  on: "ON",
};

export function BotPanel() {
  const { status, loading, saving, error, setMode, saveSettings, scanNow, testTelegram } = useBot();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [draft, setDraft] = useState<BotSettings | null>(null);
  const [confirmLiveOpen, setConfirmLiveOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");

  useEffect(() => {
    if (!status?.settings) return;
    setDraft((prev) => ({
      ...(settingsOpen && prev ? prev : status.settings),
      telegram_bot_token: prev?.telegram_bot_token ?? "",
    }));
  }, [settingsOpen, status?.settings]);

  const summary = status?.lastSummary ?? {};
  const rejections = summary.rejections ?? [];
  const executions = summary.executions ?? [];
  const errors = summary.errors ?? [];
  const recentLogs = status?.recentLogs ?? [];

  const mode = status?.settings.mode ?? "off";
  const modeTone = mode === "on" ? "text-red-300 border-red-700 bg-red-950/40" : mode === "test" ? "text-yellow-200 border-yellow-700 bg-yellow-950/40" : "text-gray-400 border-gray-700 bg-gray-800/60";
  const scanState = status?.scanning ? "Scanning" : status?.running ? "Running" : "Stopped";
  const canSave = !!draft && !saving;

  function update<K extends keyof BotSettings>(key: K, value: BotSettings[K]) {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev));
  }

  function handleModeClick(nextMode: BotMode) {
    if (nextMode === "on" && mode !== "on") {
      setConfirmText("");
      setConfirmLiveOpen(true);
      return;
    }
    void setMode(nextMode);
  }

  async function confirmLiveMode() {
    if (confirmText.trim().toUpperCase() !== "LIVE") return;
    await setMode("on");
    setConfirmLiveOpen(false);
    setConfirmText("");
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800 bg-gray-900/50 flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 flex-wrap">
          <h2 className="font-semibold text-white">Bot Mode</h2>
          <span className={`text-xs px-2 py-1 rounded border font-semibold ${modeTone}`}>
            {MODE_LABELS[mode]}
          </span>
          <span className="text-xs text-gray-500">{scanState}</span>
          {status && (
            <span className="text-xs text-gray-600">
              Warm-up {status.warmupComplete ? "armed" : "pending"}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {(["off", "test", "on"] as BotMode[]).map((m) => (
            <button
              key={m}
              onClick={() => handleModeClick(m)}
              disabled={loading || mode === m}
              className={`px-3 py-1.5 rounded text-xs font-bold transition-colors ${
                mode === m
                  ? "bg-blue-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:text-white hover:bg-gray-700 disabled:text-gray-600"
              }`}
            >
              {MODE_LABELS[m]}
            </button>
          ))}
          <button
            onClick={scanNow}
            disabled={loading || status?.scanning}
            className="px-3 py-1.5 rounded text-xs font-semibold bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:text-gray-600 text-gray-300 transition-colors"
          >
            {status?.scanning ? "Scanning..." : "Scan Now"}
          </button>
          <button
            onClick={() => setSettingsOpen((v) => !v)}
            className="px-3 py-1.5 rounded text-xs font-semibold bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
          >
            Settings
          </button>
        </div>
      </div>

      <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3 border-b border-gray-800/70">
        <Metric label="Last" value={formatDateTime(status?.lastScanAt)} />
        <Metric label="Next" value={formatDateTime(status?.nextScanAt)} />
        <Metric label="Pairs" value={String(summary.pairsScanned ?? 0)} />
        <Metric label="Eligible" value={String(summary.eligibleCount ?? 0)} tone="text-green-300" />
        <Metric label="Opened" value={String(summary.openedCount ?? 0)} tone="text-blue-300" />
        <Metric label="Closed" value={String(summary.closedCount ?? 0)} tone="text-orange-300" />
        <Metric label="Rejected" value={String(summary.rejectedCount ?? 0)} tone="text-gray-300" />
        <Metric label="Errors" value={String(summary.errorCount ?? 0)} tone={(summary.errorCount ?? 0) > 0 ? "text-red-300" : "text-gray-300"} />
      </div>

      {error && (
        <div className="mx-4 mt-3 rounded border border-red-800 bg-red-950/40 px-3 py-2 text-xs text-red-200">
          {error}
        </div>
      )}

      {settingsOpen && draft && (
        <div className="px-4 py-4 border-b border-gray-800 space-y-4">
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
            <SettingsGroup title="Entry">
              <Toggle label="Open" checked={draft.open_enabled} onChange={(v) => update("open_enabled", v)} />
              <NumberField label="Min net edge %" value={draft.min_net_edge * 100} step={0.1} onChange={(v) => update("min_net_edge", v / 100)} />
              <NumberField label="Max slippage %" value={draft.max_leg_slippage * 100} step={0.1} onChange={(v) => update("max_leg_slippage", v / 100)} />
              <NumberField label="Fixed trade $" value={draft.fixed_trade_dollars} step={10} onChange={(v) => update("fixed_trade_dollars", v)} />
              <NumberField label="Min hours left" value={draft.min_hours_to_close} step={1} onChange={(v) => update("min_hours_to_close", v)} />
            </SettingsGroup>

            <SettingsGroup title="Exit">
              <Toggle
                label="Close"
                checked={draft.close_enabled}
                onChange={(v) => update("close_enabled", v)}
                tooltip="Master switch for automated exits. When off, the bot will not close positions even if a close rule triggers."
              />
              <Toggle
                label="Take profit"
                checked={draft.take_profit_enabled}
                onChange={(v) => update("take_profit_enabled", v)}
                tooltip="Close when the close-now P&L percentage reaches the take-profit threshold."
              />
              <NumberField
                label="Take profit %"
                value={draft.take_profit_pct}
                step={0.1}
                onChange={(v) => update("take_profit_pct", v)}
                tooltip="Close-now P&L percent required for the take-profit rule."
              />
              <Toggle
                label="Stop loss"
                checked={draft.stop_loss_enabled}
                onChange={(v) => update("stop_loss_enabled", v)}
                tooltip="Close when the close-now P&L percentage falls to or below the stop-loss threshold."
              />
              <NumberField
                label="Stop loss %"
                value={draft.stop_loss_pct}
                step={0.1}
                onChange={(v) => update("stop_loss_pct", v)}
                tooltip="Negative P&L percent where the bot should cut the position."
              />
              <Toggle
                label="Spread multiple"
                checked={!!draft.spread_multiple_close_enabled}
                onChange={(v) => update("spread_multiple_close_enabled", v)}
                tooltip="Close when the close-now spread per contract is this many times the initial locked spread per contract."
              />
              <NumberField
                label="Spread x"
                value={draft.spread_multiple_close ?? 2}
                step={0.1}
                onChange={(v) => update("spread_multiple_close", Math.max(1, v))}
                tooltip="Example: 2 closes when the realizable close-now spread is at least 2x the spread locked at entry."
              />
              <Toggle
                label="Edge reversion"
                checked={draft.edge_reversion_enabled}
                onChange={(v) => update("edge_reversion_enabled", v)}
                tooltip="Close profitable positions when the same-direction entry edge has faded below the configured edge threshold."
              />
              <NumberField
                label="Close edge %"
                value={draft.close_edge_below_pct}
                step={0.1}
                onChange={(v) => update("close_edge_below_pct", v)}
                tooltip="For edge reversion: close once the current same-direction net edge is at or below this percent."
              />
              <NumberField
                label="Min close P&L %"
                value={draft.min_close_profit_pct}
                step={0.1}
                onChange={(v) => update("min_close_profit_pct", v)}
                tooltip="Minimum close-now P&L required before edge reversion is allowed to close."
              />
              <Toggle
                label="Before expiry"
                checked={draft.close_before_close_enabled}
                onChange={(v) => update("close_before_close_enabled", v)}
                tooltip="Close positions as they approach the earliest venue close time."
              />
              <NumberField
                label="Expiry hours"
                value={draft.close_before_close_hours}
                step={1}
                onChange={(v) => update("close_before_close_hours", v)}
                tooltip="How many hours before earliest close the expiry rule should trigger."
              />
            </SettingsGroup>

            <SettingsGroup title="Risk">
              <NumberField label="Scan seconds" value={draft.scan_interval_seconds} step={1} onChange={(v) => update("scan_interval_seconds", v)} />
              <Toggle label="Event driven" checked={draft.event_driven} onChange={(v) => update("event_driven", v)} />
              <NumberField label="Debounce ms" value={draft.event_debounce_ms} step={50} onChange={(v) => update("event_debounce_ms", Math.floor(v))} />
              <NumberField label="Data max age ms" value={draft.market_data_max_age_ms} step={100} onChange={(v) => update("market_data_max_age_ms", Math.floor(v))} />
              <NumberField label="Balance cache s" value={draft.balance_cache_seconds} step={1} onChange={(v) => update("balance_cache_seconds", v)} />
              <Toggle
                label="Concurrent FOK"
                checked={draft.execution_strategy === "concurrent_fok"}
                onChange={(v) => update("execution_strategy", v ? "concurrent_fok" : "sequential")}
              />
              <NumberField label="Max capital $" value={draft.max_total_capital} step={100} onChange={(v) => update("max_total_capital", v)} />
              <NumberField label="Max positions" value={draft.max_open_positions} step={1} onChange={(v) => update("max_open_positions", Math.floor(v))} />
              <NumberField label="Daily loss $" value={draft.max_daily_loss} step={10} onChange={(v) => update("max_daily_loss", v)} />
              <NumberField label="Daily trades" value={draft.max_daily_trades} step={1} onChange={(v) => update("max_daily_trades", Math.floor(v))} />
              <NumberField label="Max fatal failures" value={draft.max_consecutive_failures} step={1} onChange={(v) => update("max_consecutive_failures", Math.floor(v))} />
              <Toggle label="Stop on API error" checked={draft.stop_on_api_error} onChange={(v) => update("stop_on_api_error", v)} />
              <NumberField label="Poly fee %" value={draft.poly_fee * 100} step={0.1} onChange={(v) => update("poly_fee", v / 100)} />
              <NumberField label="Kalshi fee %" value={draft.kalshi_fee * 100} step={0.1} onChange={(v) => update("kalshi_fee", v / 100)} />
            </SettingsGroup>

            <SettingsGroup title="Telegram">
              <Toggle label="Telegram" checked={draft.telegram_enabled} onChange={(v) => update("telegram_enabled", v)} />
              <TextField
                label="Bot token"
                value={draft.telegram_bot_token}
                placeholder={status?.settings.telegramConfigured ? "Configured" : ""}
                onChange={(v) => update("telegram_bot_token", v)}
              />
              <TextField label="Chat ID" value={draft.telegram_chat_id} onChange={(v) => update("telegram_chat_id", v)} />
              <Toggle label="Status" checked={draft.notify_bot_status} onChange={(v) => update("notify_bot_status", v)} />
              <Toggle label="Opened" checked={draft.notify_trade_opened} onChange={(v) => update("notify_trade_opened", v)} />
              <Toggle label="Closed" checked={draft.notify_trade_closed} onChange={(v) => update("notify_trade_closed", v)} />
              <Toggle label="Failed" checked={draft.notify_trade_failed} onChange={(v) => update("notify_trade_failed", v)} />
              <Toggle label="Breakers" checked={draft.notify_circuit_breaker} onChange={(v) => update("notify_circuit_breaker", v)} />
              <button
                onClick={testTelegram}
                disabled={loading}
                className="mt-1 px-3 py-1.5 rounded text-xs font-semibold bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:text-gray-600 text-gray-300 transition-colors"
              >
                Test Telegram
              </button>
            </SettingsGroup>
          </div>

          <div className="flex justify-end">
            <button
              onClick={() => draft && saveSettings(draft)}
              disabled={!canSave}
              className="px-4 py-2 rounded text-sm font-bold bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:text-gray-500 text-white transition-colors"
            >
              {saving ? "Saving..." : "Save Settings"}
            </button>
          </div>
        </div>
      )}

      <div className="px-4 py-3 grid grid-cols-1 xl:grid-cols-3 gap-4">
        <DecisionList
          title="Eligible"
          empty="No eligible pairs"
          rows={(summary.eligible ?? []).slice(0, 6).map((item) => describeRecord(item))}
          tone="text-green-300"
        />
        <DecisionList
          title="Rejected"
          empty="No rejections"
          rows={rejections.slice(0, 6).map((r) => `${r.code}: ${r.message}`)}
          tone="text-gray-300"
        />
        <DecisionList
          title="Executions / Errors"
          empty="No executions or errors"
          rows={[
            ...executions.slice(0, 3).map((item) => describeRecord(item)),
            ...errors.slice(0, 3).map((item) => describeRecord(item)),
          ]}
          tone={errors.length ? "text-red-300" : "text-blue-300"}
        />
      </div>

      {recentLogs.length > 0 && (
        <div className="px-4 pb-3">
          <div className="border-t border-gray-800 pt-3">
            <div className="text-xs uppercase tracking-wide text-gray-500 mb-2">Recent Logs</div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
              {recentLogs.slice(0, 8).map((log) => (
                <div key={log.id} className="flex items-center justify-between gap-3 text-xs bg-gray-800/50 rounded px-3 py-2">
                  <span className={log.level === "error" ? "text-red-300" : log.level === "warning" ? "text-yellow-300" : "text-gray-300"}>
                    {log.message}
                  </span>
                  <span className="text-gray-600 shrink-0">{formatTime(log.createdAt)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {confirmLiveOpen && status?.settings && (
        <LiveConfirmModal
          settings={status.settings}
          loading={loading}
          confirmText={confirmText}
          onConfirmTextChange={setConfirmText}
          onCancel={() => {
            setConfirmLiveOpen(false);
            setConfirmText("");
          }}
          onConfirm={confirmLiveMode}
        />
      )}
    </div>
  );
}

function LiveConfirmModal({
  settings,
  loading,
  confirmText,
  onConfirmTextChange,
  onCancel,
  onConfirm,
}: {
  settings: BotSettings;
  loading: boolean;
  confirmText: string;
  onConfirmTextChange: (value: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const canConfirm = confirmText.trim().toUpperCase() === "LIVE" && !loading;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4">
      <div className="w-[520px] max-w-full rounded-lg border border-red-800 bg-gray-950 shadow-2xl">
        <div className="border-b border-red-900/70 px-5 py-4">
          <div className="text-sm font-bold text-red-200">Enable Live Bot Trading</div>
          <div className="mt-1 text-xs text-gray-500">
            ON mode can place real FOK orders on Polymarket and Kalshi.
          </div>
        </div>

        <div className="px-5 py-4 space-y-4">
          <div className="grid grid-cols-2 gap-3 text-xs">
            <ConfirmMetric label="Fixed trade" value={`$${settings.fixed_trade_dollars.toFixed(2)}`} />
            <ConfirmMetric label="Min net edge" value={`${(settings.min_net_edge * 100).toFixed(2)}%`} />
            <ConfirmMetric label="Max capital" value={`$${settings.max_total_capital.toFixed(2)}`} />
            <ConfirmMetric label="Max positions" value={String(settings.max_open_positions)} />
            <ConfirmMetric label="Max slippage" value={`${(settings.max_leg_slippage * 100).toFixed(2)}%`} />
            <ConfirmMetric label="Scan interval" value={`${settings.scan_interval_seconds}s`} />
            <ConfirmMetric label="Decision trigger" value={settings.event_driven ? "WebSocket" : "Scheduled"} />
            <ConfirmMetric label="Execution" value={settings.execution_strategy === "concurrent_fok" ? "Concurrent FOK" : "Sequential"} />
            <ConfirmMetric label="Open rules" value={settings.open_enabled ? "Enabled" : "Disabled"} />
            <ConfirmMetric label="Close rules" value={settings.close_enabled ? "Enabled" : "Disabled"} />
          </div>

          <div className="rounded border border-red-900/70 bg-red-950/30 px-3 py-2 text-xs text-red-100">
            The bot will run one warm-up scan after switching modes, then it may open or close eligible whitelisted positions automatically.
          </div>

          <label className="block text-xs text-gray-400">
            <span className="block mb-1">Type LIVE to confirm</span>
            <input
              value={confirmText}
              onChange={(e) => onConfirmTextChange(e.target.value)}
              autoFocus
              className="w-full rounded border border-gray-700 bg-gray-900 px-3 py-2 font-mono text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-red-500"
            />
          </label>
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-800 px-5 py-4">
          <button
            onClick={onCancel}
            disabled={loading}
            className="rounded bg-gray-800 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700 disabled:text-gray-600"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!canConfirm}
            className="rounded bg-red-700 px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-red-600 disabled:bg-gray-800 disabled:text-gray-600"
          >
            {loading ? "Enabling..." : "Enable ON Mode"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ConfirmMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-gray-800 bg-gray-900/70 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-600">{label}</div>
      <div className="mt-0.5 font-mono text-gray-200">{value}</div>
    </div>
  );
}

function Metric({ label, value, tone = "text-gray-300" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-gray-600">{label}</div>
      <div className={`text-xs font-mono truncate ${tone}`}>{value || "--"}</div>
    </div>
  );
}

function SettingsGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="bg-gray-950/40 border border-gray-800 rounded p-3 space-y-2">
      <div className="text-xs font-semibold text-gray-300">{title}</div>
      {children}
    </div>
  );
}

function NumberField({
  label,
  value,
  step,
  onChange,
  tooltip,
}: {
  label: string;
  value: number;
  step: number;
  onChange: (value: number) => void;
  tooltip?: string;
}) {
  return (
    <label className="flex items-center justify-between gap-2 text-xs text-gray-400" title={tooltip}>
      <span className={tooltip ? "cursor-help" : undefined}>{label}</span>
      <input
        type="number"
        value={Number.isFinite(value) ? value : 0}
        step={step}
        onChange={(e) => onChange(Number(e.target.value) || 0)}
        className="w-24 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-right text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
    </label>
  );
}

function TextField({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block text-xs text-gray-400">
      <span className="block mb-1">{label}</span>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-2 py-1 bg-gray-800 border border-gray-700 rounded text-gray-100 placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
    </label>
  );
}

function Toggle({
  label,
  checked,
  onChange,
  tooltip,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  tooltip?: string;
}) {
  return (
    <label className="flex items-center justify-between gap-2 text-xs text-gray-400" title={tooltip}>
      <span className={tooltip ? "cursor-help" : undefined}>{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-gray-600 bg-gray-800 text-blue-500 focus:ring-blue-500"
      />
    </label>
  );
}

function DecisionList({
  title,
  empty,
  rows,
  tone,
}: {
  title: string;
  empty: string;
  rows: string[];
  tone: string;
}) {
  return (
    <div className="min-w-0">
      <div className="text-xs uppercase tracking-wide text-gray-500 mb-2">{title}</div>
      <div className="space-y-1">
        {rows.length === 0 ? (
          <div className="text-xs text-gray-700">{empty}</div>
        ) : (
          rows.map((row, idx) => (
            <div key={`${row}-${idx}`} className={`text-xs truncate ${tone}`} title={row}>
              {row}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function describeRecord(item: Record<string, unknown>): string {
  const parts = Object.entries(item)
    .filter(([, value]) => typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    .map(([key, value]) => `${key}: ${typeof value === "number" ? Number(value).toFixed(Math.abs(value) < 10 ? 2 : 0) : value}`);
  return parts.join(" | ");
}

function formatDateTime(raw?: string | null): string {
  if (!raw) return "--";
  try {
    return new Date(raw).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return raw;
  }
}

function formatTime(raw: string): string {
  try {
    return new Date(raw).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return raw;
  }
}
