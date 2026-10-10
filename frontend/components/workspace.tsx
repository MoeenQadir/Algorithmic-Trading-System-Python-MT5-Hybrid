"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  Activity, AlertTriangle, Bot, BriefcaseBusiness,
  ChartNoAxesCombined, ChevronDown, CircleHelp, Clock3, Cpu, Database, FileText,
  Gauge, LayoutDashboard, ListChecks, RefreshCw, Settings2, ShieldCheck, Signal,
  SlidersHorizontal, Star, Bell, TrendingUp, Wifi, WifiOff, FlaskConical, HeartPulse, Server,
} from "lucide-react";
import { apiGet, apiPost, apiDelete } from "@/lib/api";
import { MarketChart, type CandlePoint } from "@/components/market-chart";
import { SettingsView } from "@/components/SettingsView";
import { SessionStatusBar } from "@/components/session-status";
import { ThemeToggle } from "@/components/ThemeToggle";

// API response shapes vary by endpoint; validate domain values where they are consumed.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Payload = Record<string, any>;
type NavItem = { href: string; label: string; icon: typeof LayoutDashboard };
type ConfigField = { key: string; label: string; step?: number };

const nav: NavItem[] = [
   { href: "dashboard", label: "Overview", icon: LayoutDashboard },
   { href: "watchlist", label: "Watchlist", icon: Star },
   { href: "market", label: "Market", icon: ChartNoAxesCombined },
   { href: "data", label: "Data Manager", icon: Database },
   { href: "alerts", label: "Alerts", icon: Bell },
   { href: "agents", label: "Agents", icon: Bot },
   { href: "signals", label: "Signals", icon: Signal },
   { href: "positions", label: "Positions", icon: BriefcaseBusiness },
   { href: "orders", label: "Orders", icon: ListChecks },
   { href: "trades", label: "Trade journal", icon: FileText },
   { href: "backtests", label: "Backtests", icon: Activity },
   { href: "walkforward", label: "Walk-Forward", icon: TrendingUp },
   { href: "experiments", label: "Experiments", icon: FlaskConical },
   { href: "health", label: "System Health", icon: HeartPulse },
   { href: "performance", label: "Performance", icon: Gauge },
   { href: "strategy", label: "Strategy Config", icon: SlidersHorizontal },
   { href: "settings", label: "Settings", icon: Settings2 },
 ];

const titles: Record<string, string> = {
  dashboard: "Research overview", watchlist: "Watchlist", market: "Market monitor",
  alerts: "Alerts", agents: "Agent desk", signals: "Signal stream",
  positions: "Open positions", orders: "Order book", trades: "Trade journal",
  backtests: "Backtest lab", walkforward: "Walk-Forward Analysis",
  experiments: "Experiment Tracking", health: "System Health",
  performance: "Performance", strategy: "Strategy Configuration",
  settings: "System settings",
};

const endpoints = (symbol: string, timeframe: string) => [
  ["health", "/health"], ["status", "/status"], ["config", "/config"],
  ["market", `/market/${encodeURIComponent(symbol)}?timeframe=${encodeURIComponent(timeframe)}&limit=180`],
  ["agents", `/agents?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`],
  ["signals", `/signals?symbol=${encodeURIComponent(symbol)}&limit=12`],
  ["decisions", `/decisions?symbol=${encodeURIComponent(symbol)}&limit=8`],
  ["positions", "/positions"], ["orders", "/orders?limit=30"],
  ["trades", "/trades?limit=40"], ["portfolio", "/portfolio"],
  ["performance", "/performance"], ["backtests", "/backtests?limit=20"],
  ["watchlist", "/watchlist"], ["alerts", "/alerts"],
] as const;

function money(value: unknown, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "—";
}

function stamp(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function Direction({ value }: { value?: string }) {
  const direction = String(value || "WAIT").toUpperCase();
  return <span className={`tag ${direction === "BUY" || direction === "LONG" ? "positive" : direction === "SELL" || direction === "SHORT" ? "negative" : "neutral"}`}>{direction}</span>;
}

function Metric({ label, value, note, accent = "" }: { label: string; value: string; note?: string; accent?: string }) {
  return <div className="metric"><span className="eyebrow">{label}</span><strong className={accent}>{value}</strong>{note && <small>{note}</small>}</div>;
}

export function Panel({ title, action, children, className = "" }: { title: string; action?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return <section className={`panel ${className}`}><div className="panel-head"><h2>{title}</h2>{action}</div>{children}</section>;
}

export function Workspace() {
  const params = useParams<{ section: string }>();
  const section = params?.section || "dashboard";
  const [symbol, setSymbol] = useState("XAUUSD");
  const [timeframe, setTimeframe] = useState("4h");
  const [data, setData] = useState<Payload>({});
  const [errors, setErrors] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<string>();
  const [actionMessage, setActionMessage] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    const results = await Promise.all(endpoints(symbol, timeframe).map(async ([key, path]) => {
      try {
        return [key, await apiGet(path), ""] as const;
      } catch (error) {
        return [key, null, error instanceof Error ? error.message : "Request failed"] as const;
      }
    }));
    const next: Payload = {};
    const failed: string[] = [];
    for (const [key, value, error] of results) {
      next[key] = value;
      if (error) failed.push(`${key}: ${error}`);
    }
    setData(next);
    setErrors(failed);
    setLastUpdated(new Date().toISOString());
    setLoading(false);
  }, [symbol, timeframe]);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (section !== "signals") return;
    let active = true;
    const refreshSignals = async () => {
      const [signalsResult, decisionsResult] = await Promise.allSettled([
        apiGet(`/signals?symbol=${encodeURIComponent(symbol)}&limit=12`),
        apiGet(`/decisions?symbol=${encodeURIComponent(symbol)}&limit=8`),
      ]);
      if (!active) return;
      setData((current) => ({
        ...current,
        ...(signalsResult.status === "fulfilled" ? { signals: signalsResult.value } : {}),
        ...(decisionsResult.status === "fulfilled" ? { decisions: decisionsResult.value } : {}),
      }));
      setLastUpdated(new Date().toISOString());
    };
    const timer = window.setInterval(() => { void refreshSignals(); }, 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [section, symbol]);

  async function runCycle() {
    setActionMessage("");
    try {
      const result = await apiPost<Payload>(`/cycle?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`, {});
      setActionMessage(`Cycle complete: ${result.status || result.decision?.action || "updated"}`);
      await refresh();
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : "Cycle failed");
    }
  }

  async function runBacktest() {
    setActionMessage("Backtest running…");
    try {
      const result = await apiPost<Payload>("/backtests/run", { symbol, timeframe, params: { max_atr_pct: 0.05, swing_lookback: 10, sweep_max_age: 5, displacement_required: true, displacement_atr: 1.0 } });
      setActionMessage(`Backtest ${result.run_id} complete · ${result.run?.trade_count ?? 0} trades`);
      await refresh();
    } catch (error) {
      setActionMessage(error instanceof Error ? error.message : "Backtest failed");
    }
  }

  const account = data.portfolio?.account || {};
  const closedTrades: Payload[] = (data.trades?.trades || []).filter((trade: Payload) => trade.status === "closed");
  const openPositions: Payload[] = data.positions?.positions || [];
  const candleData: CandlePoint[] = data.market?.candles || [];
  const trading = data.performance?.trading || {};
  const agents = data.agents?.agents || {};
  const activeSection = titles[section] ? section : "dashboard";
  const connected = Boolean(data.health?.status === "ok");

  return <div className="shell">
    <aside className="sidebar">
      <Link href="/dashboard" className="brand"><span className="brand-mark"><ChartNoAxesCombined size={19} /></span><span>FIELDNOTE<small>TRADING SYSTEMS</small></span></Link>
      <div className="side-label">WORKSPACE</div>
      <nav>{nav.map(({ href, label, icon: Icon }) => <Link key={href} href={`/${href}`} className={`nav-link ${activeSection === href ? "active" : ""}`}><Icon size={17} strokeWidth={1.8} /><span>{label}</span>{href === "positions" && openPositions.length > 0 && <b>{openPositions.length}</b>}</Link>)}</nav>
      <div className="sidebar-spacer" />
      <div className="mode-box"><span className="mode-indicator" /><div><strong>Paper environment</strong><small>Real-money routing disabled</small></div></div>
      <div className="side-foot">LOCAL RESEARCH BUILD <span>v1.0</span></div>
    </aside>

    <main className="main-area">
      <header className="topbar">
        <div className="crumb"><span>WORKSPACE</span><span className="crumb-slash">/</span><strong>{titles[activeSection]}</strong></div>
        <div className="top-controls">
          <label className="select-wrap"><span>SYMBOL</span><select value={symbol} onChange={(event) => setSymbol(event.target.value)}><option>XAUUSD</option><option>US100</option><option>BTCUSD</option><option>EURUSD</option></select><ChevronDown size={13} /></label>
          <label className="select-wrap timeframe"><span>TF</span><select value={timeframe} onChange={(event) => setTimeframe(event.target.value)}><option>4h</option><option>5m</option><option>15m</option><option>1h</option><option>1d</option></select><ChevronDown size={13} /></label>
          <span className={`connection ${connected ? "up" : "down"}`}>{connected ? <Wifi size={14} /> : <WifiOff size={14} />}<span>{connected ? "API connected" : "API offline"}</span></span>
          <button className="icon-button" onClick={() => void refresh()} aria-label="Refresh data" title="Refresh data"><RefreshCw size={16} className={loading ? "spin" : ""} /></button>
          <ThemeToggle className="ml-2" />
        </div>
      </header>

      <div className="page-content">
        <div className="page-heading"><div><div className="kicker">PERSONAL RESEARCH CONSOLE <span className="live-dot" /></div><h1>{titles[activeSection]}</h1><p>{symbol} <span>·</span> {timeframe} <span>·</span> {lastUpdated ? `Updated ${stamp(lastUpdated)}` : "Waiting for local API"}</p></div><div className="heading-actions">
          {(() => {
            const mdProvider = data.status?.providers?.market_data;
            const isOffline = !mdProvider || mdProvider.status === "OFFLINE" || mdProvider.status === "ERROR";
            const isRateLimited = mdProvider?.rate_limited;
            const sourceLabel = isOffline ? "OFFLINE" : isRateLimited ? "RATE LIMITED" : "LOCAL CACHE";
            const sourceAttr = isOffline ? "offline" : isRateLimited ? "limited" : "local";
            return (
              <>
                <span className="paper-pill"><ShieldCheck size={14} /> PAPER ONLY</span>
                <span className="data-source-pill" data-source={sourceAttr}>
                  {isOffline ? <WifiOff size={13} /> : isRateLimited ? <Server size={13} /> : <Database size={13} />}
                  {sourceLabel}
                </span>
              </>
            );
          })()}
          {activeSection === "backtests" ? <button className="primary-button" onClick={() => void runBacktest()}><Activity size={15} /> Run backtest</button> : activeSection === "dashboard" ? <button className="primary-button" onClick={() => void runCycle()}><Signal size={15} /> Run analysis</button> : null}
        </div></div>

        {actionMessage && <div className="notice"><CircleHelp size={15} />{actionMessage}<button onClick={() => setActionMessage("")} aria-label="Dismiss">×</button></div>}
        {errors.length > 0 && <div className="warning-banner"><AlertTriangle size={16} /><span>Some services are unavailable. Local data and cached results remain usable.</span><button onClick={() => void refresh()}>Retry</button></div>}

        {activeSection === "dashboard" && <Dashboard data={data} account={account} trading={trading} positions={openPositions} candles={candleData} agents={agents} />}
        {activeSection === "watchlist" && <WatchlistView watchlist={data.watchlist} onRemove={() => void refresh()} />}
        {activeSection === "alerts" && <AlertsView alerts={data.alerts?.alerts || []} symbol={symbol} timeframe={timeframe} />}
        {activeSection === "market" && <MarketView data={data} candles={candleData} symbol={symbol} timeframe={timeframe} />}
        {activeSection === "agents" && <AgentsView agents={agents} llm={data.config?.llm} />}
        {activeSection === "signals" && <SignalsView rows={data.signals?.signals || []} decisions={data.decisions?.decisions || []} />}
        {activeSection === "positions" && <PositionsView rows={openPositions} />}
        {activeSection === "orders" && <OrdersView rows={data.orders?.orders || []} />}
        {activeSection === "trades" && <TradesView rows={data.trades?.trades || []} />}
        {activeSection === "backtests" && <BacktestsView rows={data.backtests?.backtests || []} />}
        {activeSection === "walkforward" && <WalkForwardView symbol={symbol} timeframe={timeframe} />}
        {activeSection === "experiments" && <ExperimentsView />}
        {activeSection === "health" && <HealthView data={data} />}
        {activeSection === "performance" && <PerformanceView data={data} closedTrades={closedTrades} />}
        {activeSection === "strategy" && <StrategyConfigView config={data.config} />}
        {activeSection === "settings" && <SettingsView />}

        <footer className="page-footer"><span><Database size={13} /> Local-first · SQLite · cached market data</span><span><Clock3 size={13} /> No automatic order submission</span></footer>
      </div>
    </main>
  </div>;
}

function Dashboard({ data, account, trading, positions, candles, agents }: Payload & { data: Payload; account: Payload; trading: Payload; positions: Payload[]; candles: CandlePoint[]; agents: Payload }) {
  const market = data.market || {};
  const regime = agents.regime || {};
  const signals: Payload[] = data.signals?.signals || [];
  return (
    <div>
      <SessionStatusBar />
      {market.is_sample && <div className="info-strip"><Database size={15} /> SAMPLE DATA · Synthetic candles for local testing, not real market prices.</div>}
    <div className="metric-grid">
      <Metric label="PAPER EQUITY" value={`$${money(account.equity)}`} note={`Cash $${money(account.cash)}`} />
      <Metric label="REALIZED P&L" value={`${Number(trading.total_pnl || 0) >= 0 ? "+" : "−"}$${money(Math.abs(Number(trading.total_pnl || 0)))}`} note={`${trading.total_trades || 0} closed trades`} accent={Number(trading.total_pnl || 0) >= 0 ? "text-positive" : "text-negative"} />
      <Metric label="OPEN POSITIONS" value={String(positions.length)} note={`${account.unrealized_pnl >= 0 ? "+" : "−"}$${money(Math.abs(Number(account.unrealized_pnl || 0)))} unrealized`} />
      <Metric label="WIN RATE" value={trading.win_rate == null ? "—" : `${(Number(trading.win_rate) * 100).toFixed(1)}%`} note={trading.total_trades ? `${trading.total_trades} completed` : "No completed trades"} />
    </div>

    <div className="dashboard-grid">
      <Panel title={`${market.symbol || "Market"} · ${market.timeframe || ""}`} className="chart-panel" action={<span className="subtle-label">{market.count || 0} BARS · {market.source || "LOCAL"}</span>}>
        {candles.length ? <MarketChart candles={candles} /> : <EmptyState icon={ChartNoAxesCombined} title="No candles available" note="Import local data or check the configured provider." />}
        {candles.length > 0 && <div className="chart-legend"><span><i className="legend-green" />Up candle</span><span><i className="legend-red" />Down candle</span><span>Source: {market.source || "unknown"}</span></div>}
      </Panel>
      <Panel title="Risk & regime" className="risk-panel" action={<ShieldCheck size={16} className="icon-muted" />}>
        <div className="regime-block"><span className="eyebrow">CURRENT REGIME</span><strong>{regime.regime || "UNCERTAIN"}</strong><small>{regime.confidence != null ? `${Math.round(Number(regime.confidence) * 100)}% confidence` : "Awaiting sufficient evidence"}</small></div>
        <div className="risk-line"><span>Risk per trade</span><b>{((Number(data.config?.risk?.max_risk_per_trade) || 0) * 100).toFixed(2)}%</b></div>
        <div className="risk-line"><span>Daily loss limit</span><b>{((Number(data.config?.risk?.max_daily_loss) || 0) * 100).toFixed(2)}%</b></div>
        <div className="risk-line"><span>Position limit</span><b>{data.config?.risk?.max_open_positions ?? "—"}</b></div>
        <div className="risk-state"><ShieldCheck size={15} /><span>Deterministic risk controls active</span></div>
      </Panel>
    </div>

    <div className="dashboard-grid lower-grid">
      <Panel title="Agent evidence" action={<Link href="/agents" className="text-link">View all</Link>}>
        {Object.keys(agents).length ? <div className="agent-mini-list">{Object.entries(agents).filter(([key]) => key !== "news" || (agents.news as Payload)?.status !== "unavailable").slice(0, 5).map(([name, raw]) => {
          const result = raw as Payload;
          return <div className="agent-mini" key={name}><span className="agent-glyph"><Bot size={15} /></span><span className="agent-name">{name}</span><Direction value={result.direction || result.regime} /><span className="agent-confidence">{result.confidence == null ? "—" : `${Math.round(Number(result.confidence) * 100)}%`}</span></div>;
        })}</div> : <EmptyState icon={Bot} title="No agent run yet" note="Run analysis to evaluate current local data." />}
        {data.config?.llm?.configured === false && <div className="inline-note"><Cpu size={14} /> AI provider not configured · deterministic analysis remains available</div>}
      </Panel>
      <Panel title="Recent signals" action={<Link href="/signals" className="text-link">Signal log</Link>}>
        {signals.length ? <div className="compact-list">{signals.slice(0, 5).map((signal) => <div className="signal-row" key={signal.id}><span className="signal-mark"><Signal size={14} /></span><span className="signal-main"><b>{signal.agent_name}</b><small>{signal.symbol} · {stamp(signal.created_at)}</small></span><Direction value={signal.direction} /></div>)}</div> : <EmptyState icon={Signal} title="No saved signals" note="Signals appear after an analysis cycle." />}
      </Panel>
    </div>
    </div>
  );
}

function AlertsView({ alerts }: { alerts: Payload[]; symbol?: string; timeframe?: string }) {
  const [newSymbol, setNewSymbol] = useState("");
  const [newTimeframe, setNewTimeframe] = useState("1h");
  const [newType, setNewType] = useState("price");
  const [newCondition, setNewCondition] = useState("above");
  const [newValue, setNewValue] = useState("");
  const [newMessage, setNewMessage] = useState("");

  async function addAlert() {
    if (!newSymbol.trim() || !newValue) return;
    try {
      await apiPost("/alerts", {
        symbol: newSymbol.toUpperCase(),
        timeframe: newTimeframe,
        alert_type: newType,
        condition: newCondition,
        target_value: parseFloat(newValue),
        message: newMessage,
      });
      setNewSymbol("");
      setNewValue("");
      setNewMessage("");
    } catch (error) {
      console.error("Failed to create alert:", error);
    }
  }

  async function checkAlerts() {
    try {
      await apiPost("/alerts/check", {});
    } catch (error) {
      console.error("Failed to check alerts:", error);
    }
  }

  const conditionLabels: Record<string, string> = {
    above: "Above",
    below: "Below",
    crosses_up: "Crosses Up",
    crosses_down: "Crosses Down",
  };

  return (
    <div className="stack-layout">
      <div className="info-strip"><Bell size={15} /> Create price-based alerts that trigger when market conditions are met.</div>
      <Panel title="Create alert">
        <div className="alert-form">
          <div className="alert-field">
            <label>Symbol</label>
            <input type="text" placeholder="XAUUSD" value={newSymbol} onChange={(e) => setNewSymbol(e.target.value)} className="config-input" />
          </div>
          <div className="alert-field">
            <label>Timeframe</label>
            <select value={newTimeframe} onChange={(e) => setNewTimeframe(e.target.value)} className="config-select">
              <option value="5m">5m</option>
              <option value="15m">15m</option>
              <option value="1h">1h</option>
              <option value="4h">4h</option>
              <option value="1d">1d</option>
            </select>
          </div>
          <div className="alert-field">
            <label>Type</label>
            <select value={newType} onChange={(e) => setNewType(e.target.value)} className="config-select">
              <option value="price">Price</option>
              <option value="indicator">Indicator</option>
              <option value="signal">Signal</option>
            </select>
          </div>
          <div className="alert-field">
            <label>Condition</label>
            <select value={newCondition} onChange={(e) => setNewCondition(e.target.value)} className="config-select">
              <option value="above">Above</option>
              <option value="below">Below</option>
              <option value="crosses_up">Crosses Up</option>
              <option value="crosses_down">Crosses Down</option>
            </select>
          </div>
          <div className="alert-field">
            <label>Value</label>
            <input type="number" step="0.01" placeholder="0.00" value={newValue} onChange={(e) => setNewValue(e.target.value)} className="config-input" />
          </div>
          <div className="alert-field alert-message">
            <label>Message (optional)</label>
            <input type="text" placeholder="Alert message..." value={newMessage} onChange={(e) => setNewMessage(e.target.value)} className="config-input" />
          </div>
          <div className="alert-actions">
            <button className="primary-button" onClick={() => void addAlert()}>
              <Bell size={14} /> Create
            </button>
            <button className="secondary-button" onClick={() => void checkAlerts()}>
              <RefreshCw size={14} /> Check Now
            </button>
          </div>
        </div>
      </Panel>
      <Panel title="Active alerts" action={<span className="subtle-label">{alerts.length} ALERTS</span>}>
        {alerts.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>SYMBOL</th><th>TIMEFRAME</th><th>CONDITION</th><th>TARGET</th><th>CURRENT</th><th>STATUS</th><th>TRIGGERED</th></tr>
              </thead>
              <tbody>
                {alerts.map((alert) => (
                  <tr key={alert.id} className={alert.is_active ? "" : "row-dimmed"}>
                    <td className="cell-strong">{alert.symbol}</td>
                    <td>{alert.timeframe}</td>
                    <td>{conditionLabels[alert.condition] || alert.condition}</td>
                    <td>{alert.target_value}</td>
                    <td>{alert.current_value ?? "—"}</td>
                    <td>
                      <span className={`tag ${alert.is_active ? "positive" : "neutral"}`}>
                        {alert.is_active ? "ACTIVE" : "TRIGGERED"}
                      </span>
                    </td>
                    <td>{alert.triggered_at ? stamp(alert.triggered_at) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Bell} title="No alerts yet" note="Create alerts to get notified when price conditions are met." />
        )}
      </Panel>
    </div>
  );
}

function MarketView({ data, candles, symbol, timeframe }: { data: Payload; candles: CandlePoint[]; symbol: string; timeframe: string }) {
  const [quality, setQuality] = useState<Payload | null>(null);
  const [gaps, setGaps] = useState<Payload | null>(null);
  const [resampled, setResampled] = useState<Payload | null>(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<"chart" | "quality" | "gaps" | "resample">("chart");
  const latest = candles[candles.length - 1];
  const previous = candles[candles.length - 2];
  const change = latest && previous ? latest.close - previous.close : null;

  const loadQuality = useCallback(async () => {
    setQualityLoading(true);
    try {
      const [q, g] = await Promise.all([
        apiGet(`/market/${encodeURIComponent(symbol)}/quality?timeframe=${encodeURIComponent(timeframe)}&limit=500`),
        apiGet(`/market/${encodeURIComponent(symbol)}/gaps?timeframe=${encodeURIComponent(timeframe)}&max_gap=2`),
      ]);
      setQuality(q);
      setGaps(g);
    } catch (error) {
      console.error("Failed to load quality data:", error);
    } finally {
      setQualityLoading(false);
    }
  }, [symbol, timeframe]);

  const loadResample = useCallback(async (target: string) => {
    try {
      const r = await apiGet(`/market/${encodeURIComponent(symbol)}/resample?source_timeframe=${encodeURIComponent(timeframe)}&target_timeframe=${encodeURIComponent(target)}&limit=200`);
      setResampled(r);
    } catch (error) {
      console.error("Failed to resample:", error);
    }
  }, [symbol, timeframe]);

  useEffect(() => {
    if (activeTab === "quality") void loadQuality();
    if (activeTab === "resample") void loadResample("1h");
  }, [activeTab, loadQuality, loadResample]);

  return <div className="stack-layout">
    {data.market?.is_sample && <div className="info-strip"><Database size={15} /> SAMPLE DATA · Synthetic candles for local testing, not real market prices.</div>}
    <div className="metric-grid three"><Metric label="LAST CLOSE" value={latest ? money(latest.close) : "—"} note={latest ? stamp(latest.timestamp) : "No market data"} /><Metric label="BAR CHANGE" value={change == null ? "—" : `${change >= 0 ? "+" : ""}${money(change)}`} accent={change == null ? "" : change >= 0 ? "text-positive" : "text-negative"} /><Metric label="DATA SOURCE" value={data.market?.source || "—"} note={`${data.market?.count || 0} candles loaded`} /></div>

    <div className="tab-bar" role="tablist">
      <button role="tab" aria-selected={activeTab === "chart"} className={activeTab === "chart" ? "active" : ""} onClick={() => setActiveTab("chart")}>Chart</button>
      <button role="tab" aria-selected={activeTab === "quality"} className={activeTab === "quality" ? "active" : ""} onClick={() => setActiveTab("quality")}>Quality</button>
      <button role="tab" aria-selected={activeTab === "gaps"} className={activeTab === "gaps" ? "active" : ""} onClick={() => setActiveTab("gaps")}>Gaps</button>
      <button role="tab" aria-selected={activeTab === "resample"} className={activeTab === "resample" ? "active" : ""} onClick={() => setActiveTab("resample")}>Resample</button>
    </div>

    {activeTab === "chart" && <Panel title={`${symbol} · ${timeframe}`} action={<span className="subtle-label">OHLCV · {candles.length} BARS</span>}>{candles.length ? <MarketChart candles={candles} /> : <EmptyState icon={ChartNoAxesCombined} title="No candles available" note="The system does not fabricate missing market data." />}</Panel>}

    {activeTab === "quality" && <QualityView data={quality} loading={qualityLoading} symbol={symbol} timeframe={timeframe} onRefresh={() => void loadQuality()} />}

    {activeTab === "gaps" && <GapsView data={gaps} symbol={symbol} timeframe={timeframe} />}

    {activeTab === "resample" && <ResampleView data={resampled} symbol={symbol} sourceTimeframe={timeframe} onResample={loadResample} />}
  </div>;
}

function QualityView({ data, loading, symbol, timeframe, onRefresh }: { data: Payload | null; loading: boolean; symbol: string; timeframe: string; onRefresh: () => void }) {
  if (loading) return <Panel title="Data quality"><div className="empty-state"><RefreshCw size={18} className="spin" /><strong>Analyzing…</strong></div></Panel>;
  if (!data) return <Panel title="Data quality"><EmptyState icon={ChartNoAxesCombined} title="No quality data" note="Click refresh or switch tabs to load quality analysis." /></Panel>;

  return (
    <Panel title={`Quality report: ${symbol} · ${timeframe}`} action={<button className="text-link" onClick={onRefresh}><RefreshCw size={14} /> Refresh</button>}>
      <div className="metric-grid four">
        <Metric label="CANDLES" value={String(data.candle_count || 0)} />
        <Metric label="COMPLETENESS" value={`${data.completeness_pct?.toFixed(2) || "0"}%`} note={data.missing_candles > 0 ? `${data.missing_candles} missing` : "Complete"} />
        <Metric label="OHLC VIOLATIONS" value={String(data.ohlc_violations || 0)} accent={data.ohlc_violations > 0 ? "text-negative" : "text-positive"} />
        <Metric label="ANOMALIES" value={String(data.anomaly_count || 0)} accent={data.anomaly_count > 0 ? "text-negative" : "text-positive"} />
      </div>
      <div className="metric-grid two">
        <Metric label="PRICE RANGE" value={`${money(data.price_range?.[0])} – ${money(data.price_range?.[1])}`} />
        <Metric label="DUPLICATES" value={String(data.duplicate_count || 0)} accent={data.duplicate_count > 0 ? "text-negative" : "text-positive"} />
      </div>
      {data.volume_stats && <div className="metric-grid three"><Metric label="VOLUME COUNT" value={String(data.volume_stats.count || 0)} /><Metric label="VOLUME AVG" value={money(data.volume_stats.avg)} /><Metric label="VOLUME MAX" value={money(data.volume_stats.max)} /></div>}
      {data.anomalies && data.anomalies.length > 0 && <Panel title="Anomalies detected"><div className="table-wrap"><table><thead><tr><th>TYPE</th><th>TIME</th><th>DETAILS</th></tr></thead><tbody>{data.anomalies.slice(0, 10).map((a: Payload, i: number) => <tr key={i}><td>{a.type}</td><td>{stamp(a.timestamp)}</td><td>{JSON.stringify(a)}</td></tr>)}</tbody></table></div></Panel>}
      {data.duplicate_timestamps && data.duplicate_timestamps.length > 0 && <Panel title="Duplicate timestamps"><div className="table-wrap"><table><thead><tr><th>TIMESTAMP</th></tr></thead><tbody>{data.duplicate_timestamps.slice(0, 10).map((ts: string, i: number) => <tr key={i}><td>{ts}</td></tr>)}</tbody></table></div></Panel>}
    </Panel>
  );
}

function GapsView({ data, symbol, timeframe }: { data: Payload | null; symbol: string; timeframe: string }) {
  if (!data) return <Panel title="Gap analysis"><EmptyState icon={ChartNoAxesCombined} title="No gap data" note="Run quality analysis to detect gaps." /></Panel>;

  return (
    <Panel title={`Gap analysis: ${symbol} · ${timeframe}`}>
      <div className="metric-grid two">
        <Metric label="GAPS FOUND" value={String(data.gap_count || 0)} accent={data.gap_count > 0 ? "text-negative" : "text-positive"} />
      </div>
      {data.gaps && data.gaps.length > 0 && <div className="table-wrap"><table><thead><tr><th>AFTER</th><th>BEFORE</th><th>MISSING BARS</th><th>GAP (MIN)</th></tr></thead><tbody>{data.gaps.map((g: Payload, i: number) => <tr key={i}><td>{stamp(g.after)}</td><td>{stamp(g.before)}</td><td>{g.gap_candles}</td><td>{g.gap_minutes}</td></tr>)}</tbody></table></div>}
    </Panel>
  );
}

type ResampleCandle = { timestamp: string; open: number; high: number; low: number; close: number; volume?: number };

function ResampleView({ data, symbol, sourceTimeframe, onResample }: { data: Payload | null; symbol: string; sourceTimeframe: string; onResample: (target: string) => void }) {
  const targets = ["1h", "4h", "1d"];
  const candles = data?.candles as ResampleCandle[] | undefined;
  return (
    <Panel title={`Resample: ${symbol} (${sourceTimeframe} → ?)`} action={<span className="subtle-label">Target timeframe</span>}>
      <div className="button-group">
        {targets.map((tf) => <button key={tf} className="secondary-button" onClick={() => void onResample(tf)}>{tf}</button>)}
      </div>
      {candles && <div className="table-wrap"><table><thead><tr><th>TIME</th><th>OPEN</th><th>HIGH</th><th>LOW</th><th>CLOSE</th><th>VOLUME</th></tr></thead><tbody>{candles.slice(0, 20).map((row, i: number) => <tr key={i}><td>{stamp(row.timestamp)}</td><td>{money(row.open)}</td><td>{money(row.high)}</td><td>{money(row.low)}</td><td>{money(row.close)}</td><td>{row.volume != null ? money(row.volume) : "—"}</td></tr>)}</tbody></table></div>}
      {data && !candles && <EmptyState icon={ChartNoAxesCombined} title="No resampled data" note="Select a target timeframe to generate resampled candles." />}
    </Panel>
  );
}

function AgentsView({ agents, llm }: { agents: Payload; llm?: Payload }) {
  const rows = Object.entries(agents);
  return <div className="agent-grid">{rows.length ? rows.map(([name, output]) => <Panel key={name} title={name.replaceAll("_", " ")} action={<span className="agent-glyph"><Bot size={15} /></span>}><div className="agent-result"><div className="agent-result-top"><Direction value={(output as Payload).direction || (output as Payload).regime} /><span>{(output as Payload).confidence == null ? "Confidence unavailable" : `${Math.round(Number((output as Payload).confidence) * 100)}% confidence`}</span></div><pre>{JSON.stringify(output, null, 2)}</pre></div></Panel>) : <EmptyState icon={Bot} title="No agent output" note="The agent endpoint is unavailable or there is not enough market history." />}{llm?.configured === false && <div className="info-strip"><Cpu size={15} /> AI PROVIDER NOT CONFIGURED. Deterministic agents remain active.</div>}</div>;
}

function SignalsView({ rows, decisions }: { rows: Payload[]; decisions: Payload[] }) {
  return <div className="stack-layout"><Panel title="Agent signals"><div className="table-wrap"><table><thead><tr><th>CREATED</th><th>AGENT</th><th>SYMBOL</th><th>TIMEFRAME</th><th>DIRECTION</th><th>CONFIDENCE</th><th>MODEL</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{stamp(row.created_at)}</td><td>{row.agent_name}</td><td>{row.symbol}</td><td>{row.timeframe}</td><td><Direction value={row.direction} /></td><td>{Math.round(Number(row.confidence || 0) * 100)}%</td><td>{row.llm_used ? "LLM" : "Python"}</td></tr>)}</tbody></table>{!rows.length && <EmptyState icon={Signal} title="No persisted signals" note="Run an analysis cycle to populate this list." />}</div></Panel><Panel title="Decision history"><DecisionTable rows={decisions} /></Panel></div>;
}

function PositionsView({ rows }: { rows: Payload[] }) {
  return <Panel title="Open paper positions" action={<span className="subtle-label">{rows.length} ACTIVE</span>}><div className="table-wrap"><table><thead><tr><th>SYMBOL</th><th>DIRECTION</th><th>SIZE</th><th>ENTRY</th><th>MARK</th><th>STOP</th><th>TARGET</th><th>UNREALIZED</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td className="cell-strong">{row.symbol}</td><td><Direction value={row.direction} /></td><td>{money(row.quantity, 4)}</td><td>{money(row.entry_price)}</td><td>{money(row.current_price)}</td><td>{row.stop_loss == null ? "—" : money(row.stop_loss)}</td><td>{row.take_profit == null ? "—" : money(row.take_profit)}</td><td className={Number(row.unrealized_pnl) >= 0 ? "text-positive" : "text-negative"}>${money(row.unrealized_pnl)}</td></tr>)}</tbody></table>{!rows.length && <EmptyState icon={BriefcaseBusiness} title="No open positions" note="This paper account is flat." />}</div></Panel>;
}

function OrdersView({ rows }: { rows: Payload[] }) {
  return <Panel title="Paper orders"><div className="table-wrap"><table><thead><tr><th>CREATED</th><th>SYMBOL</th><th>SIDE</th><th>TYPE</th><th>QUANTITY</th><th>FILL PRICE</th><th>STATUS</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{stamp(row.created_at)}</td><td>{row.symbol}</td><td><Direction value={row.side} /></td><td>{row.order_type}</td><td>{money(row.quantity, 4)}</td><td>{row.avg_fill_price == null ? "—" : money(row.avg_fill_price)}</td><td><StatusPill value={row.status} /></td></tr>)}</tbody></table>{!rows.length && <EmptyState icon={ListChecks} title="No orders recorded" note="Paper orders and fills will be listed here." />}</div></Panel>;
}

function TradesView({ rows }: { rows: Payload[] }) {
  return <Panel title="Trade audit trail"><div className="table-wrap"><table><thead><tr><th>OPENED</th><th>SYMBOL</th><th>DIRECTION</th><th>ENTRY</th><th>EXIT</th><th>SIZE</th><th>P&L</th><th>R</th><th>STATUS</th><th>REASON</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{stamp(row.entry_time)}</td><td>{row.symbol}</td><td><Direction value={row.direction} /></td><td>{money(row.entry_price)}</td><td>{row.exit_price == null ? "—" : money(row.exit_price)}</td><td>{money(row.quantity, 4)}</td><td className={Number(row.pnl || 0) >= 0 ? "text-positive" : "text-negative"}>{row.pnl == null ? "—" : `$${money(row.pnl)}`}</td><td>{row.r_multiple == null ? "—" : `${money(row.r_multiple)}R`}</td><td><StatusPill value={row.status} /></td><td>{row.reason || "—"}</td></tr>)}</tbody></table>{!rows.length && <EmptyState icon={FileText} title="Journal is empty" note="Closed trades are recorded here with execution details." />}</div></Panel>;
}

function BacktestsView({ rows }: { rows: Payload[] }) {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<Payload | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  async function loadDetail(id: number) {
    if (selectedId === id) {
      setSelectedId(null);
      setDetail(null);
      return;
    }
    setSelectedId(id);
    setLoadingDetail(true);
    try {
      const d = await apiGet(`/backtests/${id}`);
      setDetail(d);
    } catch (error) {
      console.error("Failed to load backtest detail:", error);
      setDetail(null);
    } finally {
      setLoadingDetail(false);
    }
  }

  const btTrades = detail?.trades || [];
  return <div className="stack-layout">
    <div className="info-strip"><Activity size={15} /> Backtests use only candles available at each historical timestamp. Sample data is synthetic and labeled in the data source.</div>
    <Panel title="Saved backtest runs" action={<span className="subtle-label">{rows.length} RUNS</span>}>
      <div className="table-wrap"><table><thead><tr><th>CREATED</th><th>SYMBOL</th><th>TIMEFRAME</th><th>TRADES</th><th>WIN RATE</th><th>NET P&L</th><th>MAX DRAWDOWN</th><th>SHARPE</th><th>STATUS</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id} className={selectedId === row.id ? "row-selected" : ""} onClick={() => void loadDetail(row.id)} style={{ cursor: "pointer" }}><td>{stamp(row.created_at)}</td><td>{row.symbol}</td><td>{row.timeframe}</td><td>{row.trade_count}</td><td>{(Number(row.win_rate || 0) * 100).toFixed(1)}%</td><td className={Number(row.total_pnl || 0) >= 0 ? "text-positive" : "text-negative"}>${money(row.total_pnl)}</td><td>{(Number(row.max_drawdown || 0) * 100).toFixed(2)}%</td><td>{row.sharpe == null ? "—" : money(row.sharpe, 2)}</td><td><StatusPill value={row.status} /></td></tr>)}</tbody></table>{!rows.length && <EmptyState icon={Activity} title="No backtests saved" note="Run a backtest to create the first report." />}</div>
    </Panel>
    {selectedId != null && <Panel title={loadingDetail ? "Loading backtest detail…" : `Backtest #${selectedId} · ${detail?.run?.symbol || ""} ${detail?.run?.timeframe || ""}`} action={detail?.run?.equity_curve?.length ? <span className="subtle-label">EQUITY CURVE</span> : undefined}>
      {loadingDetail && <div className="empty-state"><RefreshCw size={18} className="spin" /><strong>Loading trades…</strong></div>}
      {!loadingDetail && detail && (
        <div>
          {detail.run?.equity_curve?.length ? <MarketChart kind="equity" equity={detail.run.equity_curve} /> : <div className="empty-inline">No equity curve available.</div>}
          <div className="metric-grid four" style={{ marginTop: 12 }}>
            <Metric label="STARTING EQUITY" value={`$${money(detail.run?.starting_equity)}`} />
            <Metric label="ENDING EQUITY" value={`$${money(detail.run?.ending_equity)}`} />
            <Metric label="TOTAL P&L" value={`${Number(detail.run?.total_pnl || 0) >= 0 ? "+" : ""}$${money(detail.run?.total_pnl)}`} accent={Number(detail.run?.total_pnl || 0) >= 0 ? "text-positive" : "text-negative"} />
            <Metric label="TRADES" value={String(detail.run?.trade_count || 0)} />
          </div>
          <div className="table-wrap"><table><thead><tr><th>DIRECTION</th><th>ENTRY TIME</th><th>EXIT TIME</th><th>ENTRY</th><th>EXIT</th><th>SIZE</th><th>P&L</th><th>R</th><th>EXIT REASON</th></tr></thead><tbody>{btTrades.map((t: Payload, i: number) => <tr key={i}><td><Direction value={t.direction} /></td><td>{stamp(t.entry_time)}</td><td>{t.exit_time ? stamp(t.exit_time) : "—"}</td><td>{money(t.entry_price)}</td><td>{t.exit_price == null ? "—" : money(t.exit_price)}</td><td>{money(t.quantity, 4)}</td><td className={Number(t.pnl || 0) >= 0 ? "text-positive" : "text-negative"}>{t.pnl == null ? "—" : `$${money(t.pnl)}`}</td><td>{t.r_multiple == null ? "—" : `${money(t.r_multiple)}R`}</td><td>{t.exit_reason || "—"}</td></tr>)}</tbody></table>{!btTrades.length && <EmptyState icon={FileText} title="No trades in this backtest" note="The strategy did not generate any signals in this window." />}</div>
        </div>
      )}
      {!loadingDetail && !detail && <EmptyState icon={Activity} title="Backtest not found" note="The selected backtest may have been deleted." />}
    </Panel>}
  </div>;
}

function WalkForwardView({ symbol, timeframe }: { symbol: string; timeframe: string }) {
  const [results, setResults] = useState<Payload | null>(null);
  const [loading, setLoading] = useState(false);
  const [params, setParams] = useState({
    train_periods: 4,
    test_size: 50,
    swing_lookback: 10,
    sweep_max_age: 5,
    displacement_required: true,
    displacement_atr: 1.0,
  });

  async function runWalkForward() {
    setLoading(true);
    try {
      const res = await apiPost("/backtests/walk-forward", {
        symbol,
        timeframe,
        train_periods: params.train_periods,
        test_size: params.test_size,
        params: {
          swing_lookback: params.swing_lookback,
          sweep_max_age: params.sweep_max_age,
          displacement_required: params.displacement_required,
          displacement_atr: params.displacement_atr,
        },
      });
      setResults(res);
    } catch (error) {
      console.error("Walk-forward analysis failed:", error);
      setResults(null);
    } finally {
      setLoading(false);
    }
  }

  const summary = results?.summary;
  const periods = results?.results || [];

  return (
    <div className="stack-layout">
      <div className="info-strip">
        <TrendingUp size={15} />
        Walk-forward analysis splits data into sequential train/test windows to validate strategy robustness.
      </div>
      <Panel title="Configuration" action={
        <div className="button-group">
          <button className="primary-button" onClick={runWalkForward} disabled={loading}>
            {loading ? <RefreshCw size={15} className="spin" /> : <TrendingUp size={15} />}
            {loading ? "Running…" : "Run Analysis"}
          </button>
        </div>
      }>
        <div className="config-grid">
          <div>
            <label>Train periods</label>
            <input
              type="number"
              min="2"
              max="20"
              value={params.train_periods}
              onChange={(e) => setParams({ ...params, train_periods: parseInt(e.target.value) })}
            />
          </div>
          <div>
            <label>Test window size</label>
            <input
              type="number"
              min="10"
              max="500"
              value={params.test_size}
              onChange={(e) => setParams({ ...params, test_size: parseInt(e.target.value) })}
            />
          </div>
          <div>
            <label>Swing lookback</label>
            <input
              type="number"
              min="5"
              max="50"
              value={params.swing_lookback}
              onChange={(e) => setParams({ ...params, swing_lookback: parseInt(e.target.value) })}
            />
          </div>
          <div>
            <label>Sweep max age</label>
            <input
              type="number"
              min="1"
              max="20"
              value={params.sweep_max_age}
              onChange={(e) => setParams({ ...params, sweep_max_age: parseInt(e.target.value) })}
            />
          </div>
          <div>
            <label>Displacement ATR</label>
            <input
              type="number"
              step="0.1"
              min="0.1"
              max="5"
              value={params.displacement_atr}
              onChange={(e) => setParams({ ...params, displacement_atr: parseFloat(e.target.value) })}
            />
          </div>
          <div>
            <label>Displacement required</label>
            <select
              value={params.displacement_required.toString()}
              onChange={(e) => setParams({ ...params, displacement_required: e.target.value === "true" })}
            >
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          </div>
        </div>
      </Panel>
      {summary && (
        <Panel title="Summary" className="summary-panel">
          <div className="metric-grid four">
            <Metric label="Periods analyzed" value={String(summary.num_periods)} />
            <Metric label="Total trades" value={String(summary.total_trades)} />
            <Metric label="Avg P&L" value={`${summary.avg_pnl >= 0 ? "+" : ""}$${money(summary.avg_pnl)}`} accent={summary.avg_pnl >= 0 ? "text-positive" : "text-negative"} />
            <Metric label="Avg Win Rate" value={`${(summary.avg_win_rate * 100).toFixed(1)}%`} />
            <Metric label="Avg Profit Factor" value={money(summary.avg_profit_factor, 2)} />
            <Metric label="Avg Sharpe" value={money(summary.avg_sharpe, 2)} />
            <Metric label="Consistency" value={`${(summary.consistency * 100).toFixed(1)}%`} />
            <Metric label="Periods" value={String(summary.num_periods)} />
          </div>
        </Panel>
      )}
      {periods.length > 0 && (
        <Panel title="Period results" action={<span className="subtle-label">{periods.length} TEST WINDOWS</span>}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>PERIOD</th>
                  <th>TRAIN ENDS</th>
                  <th>TEST WINDOW</th>
                  <th>TRADES</th>
                  <th>WIN RATE</th>
                  <th>NET P&L</th>
                  <th>PROFIT FACTOR</th>
                  <th>MAX DD</th>
                  <th>SHARPE</th>
                </tr>
              </thead>
              <tbody>
                {periods.map((p: Payload, i: number) => (
                  <tr key={i}>
                    <td>{p.period}</td>
                    <td>{stamp(p.train_end).split(",")[0]}</td>
                    <td>{stamp(p.test_start).split(",")[0]} → {stamp(p.test_end).split(",")[0]}</td>
                    <td>{p.trade_count}</td>
                    <td>{(Number(p.win_rate || 0) * 100).toFixed(1)}%</td>
                    <td className={Number(p.total_pnl || 0) >= 0 ? "text-positive" : "text-negative"}>
                      ${money(p.total_pnl)}
                    </td>
                    <td>{p.profit_factor == null ? "—" : money(p.profit_factor, 2)}</td>
                    <td>{(Number(p.max_drawdown || 0) * 100).toFixed(2)}%</td>
                    <td>{p.sharpe == null ? "—" : money(p.sharpe, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      {!summary && !loading && (
        <Panel title="Walk-Forward Analysis">
          <EmptyState
            icon={TrendingUp}
            title="No analysis run yet"
            note="Configure parameters and click Run Analysis to start walk-forward validation."
          />
        </Panel>
      )}
    </div>
  );
}

function ExperimentsView() {
  const [experiments, setExperiments] = useState<Payload[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState({
    name: "",
    description: "",
    tags: "",
    config: "{}",
    metrics: "{}",
  });

  async function loadExperiments() {
    try {
      const res = await apiGet("/experiments");
      setExperiments((res?.experiments as Payload[]) || []);
    } catch (error) {
      console.error("Failed to load experiments:", error);
    }
  }

  async function createExperiment() {
    try {
      const tags = formData.tags.split(",").map((t) => t.trim()).filter(Boolean);
      let config, metrics;
      try { config = JSON.parse(formData.config); } catch { config = {}; }
      try { metrics = JSON.parse(formData.metrics); } catch { metrics = {}; }
      
      await apiPost("/experiments", {
        name: formData.name,
        description: formData.description,
        tags,
        config,
        metrics,
      });
      setShowForm(false);
      setFormData({ name: "", description: "", tags: "", config: "{}", metrics: "{}" });
      await loadExperiments();
    } catch (error) {
      console.error("Failed to create experiment:", error);
    }
  }

  async function updateExperimentStatus(id: number, status: string) {
    try {
      await apiPost(`/experiments/${id}`, { status });
      await loadExperiments();
    } catch (error) {
      console.error("Failed to update experiment:", error);
    }
  }

  async function deleteExperiment(id: number) {
    if (!window.confirm("Delete this experiment?")) return;
    try {
      await apiDelete(`/experiments/${id}`);
      await loadExperiments();
    } catch (error) {
      console.error("Failed to delete experiment:", error);
    }
  }

  useEffect(() => { loadExperiments(); }, []);

  return (
    <div className="stack-layout">
      <div className="info-strip">
        <FlaskConical size={15} />
        Track and compare strategy experiments with config snapshots and metrics.
      </div>
      <Panel title="Experiments" action={
        <div className="button-group">
          <button className="secondary-button" onClick={() => setShowForm(!showForm)}>
            {showForm ? <ChevronDown size={15} /> : <ChevronDown size={15} />}
            {showForm ? "Hide Form" : "New Experiment"}
          </button>
        </div>
      }>
        {showForm && (
          <div className="config-grid" style={{ marginBottom: 16 }}>
            <div style={{ gridColumn: "1 / -1" }}>
              <label>Name *</label>
              <input
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                placeholder="e.g., Mean Reversion v2"
              />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <label>Description</label>
              <textarea
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                rows={2}
                placeholder="Experiment notes..."
              />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <label>Tags (comma-separated)</label>
              <input
                type="text"
                value={formData.tags}
                onChange={(e) => setFormData({ ...formData, tags: e.target.value })}
                placeholder="xauusd, mean-reversion, 4h"
              />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <label>Config (JSON)</label>
              <textarea
                value={formData.config}
                onChange={(e) => setFormData({ ...formData, config: e.target.value })}
                rows={4}
                style={{ fontFamily: "monospace", fontSize: 12 }}
                placeholder='{"risk": {"max_risk_per_trade": 0.01}, ...}'
              />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <label>Metrics (JSON)</label>
              <textarea
                value={formData.metrics}
                onChange={(e) => setFormData({ ...formData, metrics: e.target.value })}
                rows={4}
                style={{ fontFamily: "monospace", fontSize: 12 }}
                placeholder='{"sharpe": 1.5, "max_dd": 0.12, ...}'
              />
            </div>
            <div className="button-group" style={{ gridColumn: "1 / -1", marginTop: 8 }}>
              <button className="primary-button" onClick={createExperiment} disabled={!formData.name.trim()}>
                Create Experiment
              </button>
              <button className="secondary-button" onClick={() => setShowForm(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>NAME</th>
                <th>TAGS</th>
                <th>STATUS</th>
                <th>CREATED</th>
                <th>UPDATED</th>
                <th>ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {experiments.length ? experiments.map((exp: Payload) => (
                <tr key={exp.id}>
                  <td><strong>{exp.name}</strong>{exp.description && <span><br /><small>{exp.description}</small></span>}</td>
                  <td>{(exp.tags || []).map((t: string, i: number) => <span key={i} className="tag">{t}</span>)}</td>
                  <td>
                    <select
                      value={exp.status}
                      onChange={(e) => updateExperimentStatus(exp.id, e.target.value)}
                      style={{ padding: 4, fontSize: 12 }}
                    >
                      <option value="running">Running</option>
                      <option value="completed">Completed</option>
                      <option value="failed">Failed</option>
                      <option value="archived">Archived</option>
                    </select>
                  </td>
                  <td>{exp.created_at ? new Date(exp.created_at).toLocaleDateString() : "—"}</td>
                  <td>{exp.updated_at ? new Date(exp.updated_at).toLocaleDateString() : "—"}</td>
                  <td>
                    <button className="secondary-button" onClick={() => deleteExperiment(exp.id)} style={{ padding: "4px 8px", fontSize: 12 }}>
                      Delete
                    </button>
                  </td>
                </tr>
              )) : (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center" }}>
                    <EmptyState
                      icon={FlaskConical}
                      title="No experiments yet"
                      note="Create your first experiment to track strategy iterations."
                    />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function HealthView({ data }: { data: Payload }) {
  const config = data.config || {};
  const health = data.status || {};
  const providerRows = Object.entries(health.providers || {}) as [string, Payload][];
  const components = Object.entries(health.component_health || {}) as [string, Payload][];
  const events = (health.recent_events || []) as Payload[];

  return (
    <div className="stack-layout">
      <div className="info-strip">
        <HeartPulse size={15} />
        System health overview including API status, data providers, and component status.
      </div>
      <Panel title="System Status" action={
        <span className={`subtle-label ${health.status === "healthy" ? "text-positive" : health.status === "degraded" ? "text-warning" : "text-negative"}`}>
          {health.status?.toUpperCase() || "UNKNOWN"}
        </span>
      }>
        <div className="metric-grid four">
          <Metric label="BACKEND API" value={health.status?.toUpperCase() || "UNKNOWN"} note="API server status" />
          <Metric label="DATABASE" value={health.database?.status || "OK"} note={health.database?.message || "SQLite connection"} />
          <Metric label="DATA CACHE" value={health.cache?.status || "OK"} note={`Last refresh: ${health.cache?.last_refresh ? new Date(health.cache.last_refresh).toLocaleTimeString() : "—"}`} />
          <Metric label="LIVE TRADING" value={config.live_trading ? "ENABLED" : "DISABLED"} accent={config.live_trading ? "text-negative" : "text-positive"} note={config.paper_trading ? "Paper trading active" : "Trading disabled"} />
        </div>
      </Panel>

      <Panel title="Data Providers">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>PROVIDER</th>
                <th>STATUS</th>
                <th>RATE LIMITED</th>
                <th>LAST CHECK</th>
                <th>DETAILS</th>
              </tr>
            </thead>
            <tbody>
              {providerRows.length ? providerRows.map(([key, value]) => (
                <tr key={key}>
                  <td>{key.replaceAll("_", " ")}</td>
                  <td><span className={`tag ${value.status === "OK" ? "positive" : value.status === "RATE_LIMITED" ? "warning" : "negative"}`}>{value.status || "OK"}</span></td>
                  <td>{value.rate_limited ? "Yes" : "No"}</td>
                  <td>{value.last_check ? new Date(value.last_check).toLocaleString() : "—"}</td>
                  <td>{value.message || "—"}</td>
                </tr>
              )) : (
                <tr><td colSpan={5} style={{ textAlign: "center" }}><EmptyState icon={Wifi} title="No provider data" note="Provider status will appear here when available." /></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Component Health">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>COMPONENT</th>
                <th>STATUS</th>
                <th>MESSAGE</th>
              </tr>
            </thead>
            <tbody>
              {components.length ? components.map(([key, value]) => (
                <tr key={key}>
                  <td>{key.replaceAll("_", " ")}</td>
                  <td><span className={`tag ${value.status === "OK" ? "positive" : value.status === "WARNING" ? "warning" : "negative"}`}>{value.status || "OK"}</span></td>
                  <td>{value.message || "—"}</td>
                </tr>
              )) : (
                <tr><td colSpan={3} style={{ textAlign: "center" }}><EmptyState icon={Cpu} title="No component data" note="Component health will appear here when available." /></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Recent System Events">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>TIME</th>
                <th>LEVEL</th>
                <th>COMPONENT</th>
                <th>MESSAGE</th>
              </tr>
            </thead>
            <tbody>
              {events.length ? events.map((e: Payload, i: number) => (
                <tr key={i}>
                  <td>{e.created_at ? new Date(e.created_at).toLocaleString() : "—"}</td>
                  <td><span className={`tag ${e.level === "INFO" ? "neutral" : e.level === "WARNING" ? "warning" : "negative"}`}>{e.level}</span></td>
                  <td>{e.component}</td>
                  <td>{e.message}</td>
                </tr>
              )) : (
                <tr><td colSpan={4} style={{ textAlign: "center" }}><EmptyState icon={Activity} title="No events recorded" note="System events will appear here." /></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Configuration Summary">
        <div className="settings-grid">
          <div>
            <strong>Trading Mode:</strong> {config.live_trading ? "LIVE (⚠️ DANGEROUS)" : config.paper_trading ? "Paper Trading" : "Disabled"}
          </div>
          <div>
            <strong>Market:</strong> {config.default_symbol || "—"} · {config.default_timeframe || "—"}
          </div>
          <div>
            <strong>Provider:</strong> {config.market_data_provider || "—"}
          </div>
          <div>
            <strong>AI Model:</strong> {config.llm?.model || "—"} ({config.llm?.configured ? "Configured" : "Not configured"})
          </div>
        </div>
      </Panel>
    </div>
  );
}

function PerformanceView({ data, closedTrades }: { data: Payload; closedTrades: Payload[] }) {
  const performance = data.performance?.trading || {};
  const agentPerformance = data.performance?.agents || [];
  const curve = data.backtests?.backtests?.[0]?.equity_curve || [];
  return <div className="stack-layout"><div className="metric-grid four"><Metric label="CLOSED TRADES" value={String(performance.total_trades || 0)} /><Metric label="WIN RATE" value={performance.win_rate == null ? "—" : `${(Number(performance.win_rate) * 100).toFixed(1)}%`} /><Metric label="PROFIT FACTOR" value={performance.profit_factor == null ? "—" : money(performance.profit_factor, 2)} /><Metric label="EXPECTANCY" value={performance.expectancy_r == null ? "—" : `${money(performance.expectancy_r, 2)}R`} /></div><Panel title="Latest backtest equity curve">{curve.length ? <MarketChart kind="equity" equity={curve} /> : <EmptyState icon={ChartNoAxesCombined} title="No equity curve yet" note="Run a backtest to create a historical equity curve." />}</Panel><Panel title="Agent performance">{agentPerformance.length ? <div className="table-wrap"><table><thead><tr><th>AGENT</th><th>PREDICTIONS</th><th>EVALUATED</th><th>ACCURACY</th><th>FALSE POS</th><th>FALSE NEG</th><th>EXPECTANCY</th><th>RELIABLE</th></tr></thead><tbody>{agentPerformance.map((a: Payload, i: number) => <tr key={i}><td>{a.agent_name}</td><td>{a.predictions}</td><td>{a.evaluated}</td><td>{a.accuracy == null ? "—" : `${(Number(a.accuracy) * 100).toFixed(1)}%`}</td><td>{a.false_positive_rate == null ? "—" : `${(Number(a.false_positive_rate) * 100).toFixed(1)}%`}</td><td>{a.false_negative_rate == null ? "—" : `${(Number(a.false_negative_rate) * 100).toFixed(1)}%`}</td><td>{a.historical_expectancy_r == null ? "—" : `${money(a.historical_expectancy_r, 2)}R`}</td><td>{a.sample_reliable ? "✓" : "✗"}</td></tr>)}</tbody></table></div> : <EmptyState icon={Bot} title="No agent performance data" note="Run analysis cycles to evaluate agent predictions against outcomes." />}</Panel><Panel title="Recent closed trades"><TradesView rows={closedTrades.slice(0, 8)} /></Panel></div>;
}


function DecisionTable({ rows }: { rows: Payload[] }) {
  return <div className="table-wrap"><table><thead><tr><th>TIME</th><th>SYMBOL</th><th>ACTION</th><th>DIRECTION</th><th>CONFIDENCE</th><th>REASON</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td>{stamp(row.created_at)}</td><td>{row.symbol}</td><td><Direction value={row.action} /></td><td>{row.direction}</td><td>{Math.round(Number(row.confidence || 0) * 100)}%</td><td className="reason-cell">{row.decision_reason || "—"}</td></tr>)}</tbody></table>{!rows.length && <p className="empty-inline">No stored decisions.</p>}</div>;
}

function StatusPill({ value }: { value?: string }) {
  const status = String(value || "unknown").toLowerCase();
  return <span className={`status-pill ${status === "filled" || status === "closed" || status === "completed" ? "positive" : status === "rejected" || status === "error" ? "negative" : "neutral"}`}>{status}</span>;
}

function EmptyState({ icon: Icon, title, note }: { icon: typeof Activity; title: string; note: string }) {
  return <div className="empty-state"><span><Icon size={18} /></span><strong>{title}</strong><small>{note}</small></div>;
}

function StrategyConfigView({ config }: { config: Payload }) {
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [formData, setFormData] = useState<Payload>({
    risk: config?.risk || {},
    agent_weights: config?.agent_weights || {},
    strategy: config?.strategy || {},
    execution: config?.execution || {},
    default_symbol: config?.default_symbol || "XAUUSD",
    default_timeframe: config?.default_timeframe || "1h",
  });

  const handleChange = (section: string, key: string, value: string | number) => {
    setFormData(prev => ({
      ...prev,
      [section]: { ...prev[section], [key]: typeof value === "string" ? parseFloat(value) : value }
    }));
  };

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      await apiPost("/config/update", formData);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (error) {
      console.error("Failed to save config:", error);
    } finally {
      setSaving(false);
    }
  };

  const sections: { key: string; label: string; fields: ConfigField[] }[] = [
    { key: "risk", label: "Risk Management", fields: [
      { key: "max_risk_per_trade", label: "Max Risk per Trade (%)", step: 0.1 },
      { key: "max_daily_loss", label: "Max Daily Loss (%)", step: 0.1 },
      { key: "max_consecutive_losses", label: "Max Consecutive Losses", step: 1 },
      { key: "max_open_positions", label: "Max Open Positions", step: 1 },
      { key: "min_risk_reward", label: "Min Risk/Reward", step: 0.1 },
      { key: "max_position_size", label: "Max Position Size", step: 0.01 },
      { key: "max_symbol_exposure", label: "Max Symbol Exposure (%)", step: 0.1 },
      { key: "max_total_exposure", label: "Max Total Exposure (%)", step: 0.1 },
      { key: "risk_percentage", label: "Risk Percentage (%)", step: 0.1 },
    ]},
    { key: "agent_weights", label: "Agent Weights", fields: [
      { key: "technical", label: "Technical Weight", step: 0.1 },
      { key: "quant", label: "Quant Weight", step: 0.1 },
      { key: "strategy", label: "Strategy Weight", step: 0.1 },
      { key: "regime", label: "Regime Weight", step: 0.1 },
      { key: "news", label: "News Weight", step: 0.1 },
      { key: "sentiment", label: "Sentiment Weight", step: 0.1 },
      { key: "decision_threshold", label: "Decision Threshold", step: 0.05 },
    ]},
    { key: "strategy", label: "Strategy Parameters", fields: [
      { key: "swing_lookback", label: "Swing Lookback", step: 1 },
      { key: "sweep_max_age", label: "Sweep Max Age", step: 1 },
      { key: "displacement_required", label: "Displacement Required", step: 0.1 },
      { key: "displacement_atr", label: "Displacement ATR", step: 0.1 },
      { key: "stop_buffer_atr", label: "Stop Buffer ATR", step: 0.1 },
    ]},
    { key: "execution", label: "Execution Settings", fields: [
      { key: "paper_account_balance", label: "Paper Account Balance", step: 100 },
      { key: "paper_commission", label: "Paper Commission (%)", step: 0.001 },
      { key: "paper_slippage", label: "Paper Slippage (%)", step: 0.001 },
      { key: "paper_spread_pct", label: "Paper Spread (%)", step: 0.001 },
    ]},
    { key: "general", label: "General", fields: [
      { key: "default_symbol", label: "Default Symbol" },
      { key: "default_timeframe", label: "Default Timeframe" },
    ]},
  ];

  return (
    <div className="stack-layout">
      <div className="info-strip"><SlidersHorizontal size={15} /> Adjust strategy, risk, and execution parameters. Changes persist to database.</div>
      {saved && <div className="success-banner">Configuration saved successfully.</div>}
      {sections.map((section) => (
        <Panel key={section.key} title={section.label}>
          <div className="config-grid">
            {section.fields.map((field) => (
              <div key={field.key} className="config-field">
                <label>{field.label}</label>
                {section.key === "general" && field.key === "default_symbol" && (
                  <input
                    type="text"
                    value={formData[field.key] || ""}
                    onChange={(e) => { const newData = {...formData}; newData[field.key] = e.target.value; setFormData(newData); }}
                    className="config-input"
                  />
                )}
                {section.key === "general" && field.key === "default_timeframe" && (
                  <select
                    value={formData[field.key] || "1h"}
                    onChange={(e) => { const newData = {...formData}; newData[field.key] = e.target.value; setFormData(newData); }}
                    className="config-select"
                  >
                    <option value="5m">5m</option>
                    <option value="15m">15m</option>
                    <option value="1h">1h</option>
                    <option value="4h">4h</option>
                    <option value="1d">1d</option>
                  </select>
                )}
                {section.key !== "general" && (
                  <input
                    type="number"
                    step={(field as ConfigField).step ?? 0.01}
                    value={formData[section.key]?.[field.key] ?? ""}
                    onChange={(e) => handleChange(section.key, field.key, e.target.value)}
                    className="config-input"
                  />
                )}
              </div>
            ))}
          </div>
        </Panel>
      ))}
      <div className="config-actions">
        <button className="primary-button" onClick={handleSave} disabled={saving}>
          {saving ? "Saving..." : "Save Configuration"}
        </button>
      </div>
    </div>
  );
}

function WatchlistView({ watchlist, onRemove }: { watchlist: Payload; symbol?: string; onRemove: () => void }) {
  const [newSymbol, setNewSymbol] = useState("");
  const [newTimeframe, setNewTimeframe] = useState("1h");
  const items: Payload[] = watchlist?.items || [];

  async function addWatchlist() {
    if (!newSymbol.trim()) return;
    try {
      await apiPost("/watchlist/add", { symbol: newSymbol.toUpperCase(), timeframe: newTimeframe });
      setNewSymbol("");
      onRemove();
    } catch (error) {
      console.error("Failed to add to watchlist:", error);
    }
  }

  async function removeWatchlist(id: number) {
    try {
      await apiDelete(`/watchlist/remove/${id}`);
      onRemove();
    } catch (error) {
      console.error("Failed to remove from watchlist:", error);
    }
  }

  return (
    <div className="stack-layout">
      <div className="info-strip"><Star size={15} /> Track multiple symbols and timeframes for quick reference.</div>
      <Panel title="Add to watchlist">
        <div className="watchlist-add">
          <input
            type="text"
            placeholder="Symbol (e.g. XAUUSD)"
            value={newSymbol}
            onChange={(e) => setNewSymbol(e.target.value)}
            className="watchlist-input"
          />
          <select value={newTimeframe} onChange={(e) => setNewTimeframe(e.target.value)} className="watchlist-select">
            <option value="5m">5m</option>
            <option value="15m">15m</option>
            <option value="1h">1h</option>
            <option value="4h">4h</option>
            <option value="1d">1d</option>
          </select>
          <button className="primary-button" onClick={() => void addWatchlist()}>
            <Star size={14} /> Add
          </button>
        </div>
      </Panel>
      <Panel title="Watched symbols" action={<span className="subtle-label">{items.length} TRACKED</span>}>
        {items.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>SYMBOL</th><th>TIMEFRAME</th><th>ADDED</th><th></th></tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td className="cell-strong">{item.symbol}</td>
                    <td>{item.timeframe}</td>
                    <td>{stamp(item.created_at)}</td>
                    <td><button className="icon-button" onClick={() => void removeWatchlist(item.id)} aria-label="Remove">×</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Star} title="Watchlist empty" note="Add symbols to track their performance." />
        )}
      </Panel>
    </div>
  );
}
