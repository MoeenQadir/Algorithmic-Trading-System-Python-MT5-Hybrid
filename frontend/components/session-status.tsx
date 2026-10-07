"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";

type SessionStatus = {
  mode?: string;
  symbol?: string;
  timeframe?: string;
  paused?: boolean;
  pause_reason?: string | null;
  latest_completed_candle?: string | null;
  last_analysis?: string | null;
  last_signal?: string | null;
  real_market_data_enabled?: boolean;
  worker?: { state?: string; heartbeat?: string | null; consecutive_failures?: number };
  credits?: { status?: string; credits_remaining?: number; stale?: boolean; source?: string };
  paper_trading?: boolean;
  live_trading?: boolean;
};

type Readiness = {
  overall?: string;
  checks?: Record<string, { state?: string; detail?: string }>;
};

function Badge({ label, value, tone }: { label: string; value: string; tone: "ok" | "warn" | "bad" | "muted" }) {
  const tones = {
    ok: "border-emerald-700 bg-emerald-950/60 text-emerald-300",
    warn: "border-amber-700 bg-amber-950/60 text-amber-300",
    bad: "border-red-800 bg-red-950/60 text-red-300",
    muted: "border-[#e0e8e4] bg-white text-[#5a7066] dark:border-[#30443f] dark:bg-[#0d1715] dark:text-[#91aaa2]",
  } as const;
  return (
    <div className={`rounded border px-2 py-1 text-xs ${tones[tone]}`}>
      <span className="opacity-60">{label}: </span>
      <span className="font-semibold">{value}</span>
    </div>
  );
}

function toneFor(value: string | undefined | null): "ok" | "warn" | "bad" | "muted" {
  const v = (value || "").toUpperCase();
  if (["REAL_MARKET_DATA", "RUNNING", "HEALTHY", "OK", "PASS", "READY_FOR_REAL_MARKET_PAPER", "BUY", "SELL"].includes(v)) return "ok";
  if (["CACHE", "PAUSED", "DEGRADED", "WARNING", "UNKNOWN", "CONFIG_REQUIRED", "NO_TRADE", "WAIT"].includes(v)) return "warn";
  if (["OFFLINE", "ERROR", "RATE_LIMITED", "FAIL", "NOT_READY"].includes(v)) return "bad";
  return "muted";
}

export function SessionStatusBar() {
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);

  const load = async () => {
    try {
      const [s, r] = await Promise.all([
        apiGet<SessionStatus>("/status"),
        apiGet<Readiness>("/readiness"),
      ]);
      setSession(s);
      setReadiness(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "status unavailable");
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);

  const syncUsage = async () => {
    setSyncing(true);
    try {
      await apiPost("/providers/twelvedata/api-usage", {});
      await load();
    } catch {
      /* non-fatal */
    } finally {
      setSyncing(false);
    }
  };

  if (error && !session) {
return (
  <div className="rounded border border-[#e0e8e4] bg-white p-3 text-xs text-[#5a7066] dark:border-[#30443f] dark:bg-[#0d1715] dark:text-[#91aaa2]">
    Real-market session status unavailable: {error}
  </div>
);
  }

  const mode = session?.mode || "UNKNOWN";
  const workerState = session?.worker?.state || "STOPPED";
  const creditState = session?.credits?.status || "UNKNOWN";
  const overall = readiness?.overall || "UNKNOWN";

  return (
    <section aria-label="Real-market session status" className="rounded border border-[#e0e8e4] bg-white p-3 text-[#5a7066] dark:border-[#30443f] dark:bg-[#0d1715] dark:text-[#91aaa2]">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Badge label="Mode" value={mode} tone={toneFor(mode)} />
        <Badge label="Provider" value={session?.real_market_data_enabled ? "Twelve Data" : "Local/Demo"} tone="muted" />
        <Badge label="Symbol" value={session?.symbol || "-"} tone="muted" />
        <Badge label="Timeframe" value={session?.timeframe || "-"} tone="muted" />
        <Badge label="Worker" value={workerState} tone={toneFor(workerState)} />
        <Badge label="Credits" value={creditState} tone={toneFor(creditState)} />
        <Badge label="Paper trading" value={session?.paper_trading ? "ENABLED" : "DISABLED"} tone={session?.paper_trading ? "ok" : "warn"} />
        <Badge label="Live trading" value={session?.live_trading ? "ENABLED" : "DISABLED"} tone={session?.live_trading ? "bad" : "ok"} />
      </div>

      <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-[#5a7066] dark:text-[#91aaa2]">
        <span>
          Latest completed candle:{" "}
          <span className="text-[#33453e] dark:text-[#d5e3dd]">{session?.latest_completed_candle || "none"}</span>
        </span>
        <span>
          Last analysis: <span className="text-[#33453e] dark:text-[#d5e3dd]">{session?.last_analysis || "none"}</span>
        </span>
        <span>
Last signal:{" "}
        <span className="text-[#33453e] dark:text-[#d5e3dd]">{session?.last_signal || "-"}</span>
        </span>
        <span>
          Readiness: <span className="text-[#d5e3dd]">{overall}</span>
        </span>
{session?.credits?.credits_remaining !== undefined && (
  <span>
    Credits remaining: <span className="text-[#33453e] dark:text-[#d5e3dd]">{session.credits.credits_remaining}</span>
    {session.credits.stale ? " (stale)" : ""}
  </span>
)}
      </div>

      {session?.paused && session?.pause_reason && (
        <div className="mt-2 rounded border border-amber-800 bg-amber-950/40 px-2 py-1 text-xs text-amber-300">
          Session paused: {session.pause_reason}
        </div>
      )}

      <div className="mt-2 flex items-center gap-3">
<button
  onClick={syncUsage}
  disabled={syncing}
  className="rounded border border-[#e0e8e4] px-2 py-1 text-xs text-[#5a7066] hover:border-[#50d6a0] hover:text-[#50d6a0] dark:border-[#30443f] dark:text-[#91aaa2] dark:hover:border-[#50d6a0] dark:hover:text-[#50d6a0] disabled:opacity-50"
>
  {syncing ? "Syncing..." : "Sync API usage"}
</button>
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
    </section>
  );
}
