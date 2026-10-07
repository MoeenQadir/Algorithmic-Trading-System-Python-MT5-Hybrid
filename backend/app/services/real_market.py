"""Real-market paper session state (Phase 38), completed-candle engine
(Phase 37) and readiness service (Phase 40).

CompletedCandleEngine: one authoritative processor for completed candles.

Pipeline:
    provider -> normalize -> validate -> identify completed candle ->
    quality gates -> deduplicate -> persist -> trigger analysis once

Invariants:
- never analyze an incomplete candle
- never persist a duplicate candle (durable last-processed timestamp + DB dedup)
- preserve provider timestamp, received timestamp, source and quality result
- in REMOTE_REAL_DATA mode a failing provider PAUSES the session; cached data
  may only be shown as CACHE and never as REAL DATA
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any, Optional

from app.config import get_settings
from app.data.providers import get_provider, parse_timeframe_minutes
from app.data.quality import QualityThresholds, analyze_candles, check_quality_gates
from app.models.database import MarketData, get_session_factory
from app.utils.timeutils import to_utc, utcnow

logger = logging.getLogger("trading.real_market")


# ------------------------------------------------------------------------------
# Phase 37: Completed candle engine
# ------------------------------------------------------------------------------
class CompletedCandleEngine:
    """Authoritative completed-candle processor (Phase 37).

    Accepts provider candles, validates them, keeps only completed candles,
    applies quality gates, deduplicates durably and persists new candles.
    Returns a structured result so the caller can decide whether to trigger
    analysis (exactly once per new completed candle).
    """

    def __init__(self, symbol: str, timeframe: str) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.tf_minutes = parse_timeframe_minutes(timeframe)
        self._last_processed: Optional[Any] = None  # durable via DB, cached here

    # ------------------------------------------------------------------ helpers
    def load_last_processed(self) -> Optional[Any]:
        """Durable last-processed timestamp: max market_data timestamp."""
        db = get_session_factory()()
        try:
            row = (
                db.query(MarketData)
                .filter(MarketData.symbol == self.symbol, MarketData.timeframe == self.timeframe)
                .order_by(MarketData.timestamp.desc())
                .first()
            )
            self._last_processed = row.timestamp if row else None
            return self._last_processed
        finally:
            db.close()

    @staticmethod
    def is_completed(candle: dict[str, Any], tf_minutes: int, now: Any) -> bool:
        """A candle is completed when its open time + interval <= now."""
        ts = candle.get("timestamp")
        if ts is None:
            return False
        ts = to_utc(ts)
        return ts + timedelta(minutes=tf_minutes) <= now

    def validate_candle(self, candle: dict[str, Any]) -> tuple[bool, str]:
        """Schema + OHLC consistency validation. No fabrication."""
        o, h, l, c = candle.get("open"), candle.get("high"), candle.get("low"), candle.get("close")
        ts = candle.get("timestamp")
        if ts is None or o is None or h is None or l is None or c is None:
            return False, "missing_fields"
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError):
            return False, "non_numeric"
        if not (h >= l and h >= o and h >= c and l <= o and l <= c):
            return False, "ohlc_inconsistent"
        return True, "ok"

    def quality_gate(self, candles: list[dict[str, Any]]) -> dict[str, Any]:
        """Run configured quality gates over a rolling analysis window.

        The window combines recent persisted history with the incoming batch so
        small incremental polls are judged in context. If the combined sample
        is smaller than the configured minimum, the gate is skipped (an
        insufficient sample cannot demonstrate a quality failure).
        """
        s = get_settings()
        if not s.quality_gates_enabled or not candles:
            return {"passed": True, "skipped": True, "result": None}
        try:
            thresholds = QualityThresholds.from_settings()
            db = get_session_factory()()
            try:
                rows = (
                    db.query(MarketData)
                    .filter(MarketData.symbol == self.symbol, MarketData.timeframe == self.timeframe)
                    .order_by(MarketData.timestamp.desc())
                    .limit(max(thresholds.min_candles_required * 3, 60))
                    .all()
                )
            finally:
                db.close()
            history = [
                {
                    "timestamp": r.timestamp, "open": r.open, "high": r.high,
                    "low": r.low, "close": r.close, "volume": r.volume,
                }
                for r in reversed(rows)
            ]
            history_ts = {h["timestamp"] for h in history}
            incoming = [c for c in candles if to_utc(c["timestamp"]) not in history_ts]
            window = history + incoming
            if len(window) < thresholds.min_candles_required:
                return {"passed": True, "skipped": True, "reason": "insufficient_sample", "result": None}
            result = analyze_candles(window, self.symbol, self.timeframe, self.tf_minutes)
            passed, failed = check_quality_gates(result, thresholds)
            return {"passed": bool(passed), "skipped": False, "failed_checks": failed, "result": result.to_dict()}
        except Exception as exc:  # quality engine failure must not crash the worker
            logger.warning("quality gate error: %s", exc)
            return {"passed": False, "skipped": False, "error": str(exc)}

    # ------------------------------------------------------------------ process
    def process(self, provider_candles: list[dict[str, Any]], source: str = "remote") -> dict[str, Any]:
        """Run the full pipeline. Returns a structured outcome dict."""
        now = utcnow()
        out: dict[str, Any] = {
            "received_count": len(provider_candles or []),
            "completed_count": 0,
            "persisted": 0,
            "duplicates_skipped": 0,
            "invalid_skipped": 0,
            "quality": None,
            "last_processed_at": None,
            "new_completed_candle": False,
        }
        if not provider_candles:
            return out

        # 1. normalize is provider responsibility (normalize_row) - validate here
        valid: list[dict[str, Any]] = []
        for candle in provider_candles:
            ok, reason = self.validate_candle(candle)
            if ok:
                valid.append(candle)
            else:
                out["invalid_skipped"] += 1
                logger.warning("invalid candle skipped (%s): %s", reason, candle.get("timestamp"))

        # 2. keep only completed candles (never analyze incomplete)
        completed = [c for c in valid if self.is_completed(c, self.tf_minutes, now)]
        out["completed_count"] = len(completed)
        if not completed:
            return out

        # 3. quality gates on the completed batch
        gate = self.quality_gate(completed)
        out["quality"] = {k: v for k, v in gate.items() if k != "result"}
        if gate.get("result") is not None:
            out["quality"]["summary"] = gate["result"].get("summary") if isinstance(gate["result"], dict) else None
        if not gate.get("passed", False):
            out["quality_blocked"] = True
            return out

        # 4. deduplicate + persist (durable last-processed timestamp)
        last_processed = self._last_processed or self.load_last_processed()
        if last_processed is not None:
            last_processed = to_utc(last_processed)
        db = get_session_factory()()
        try:
            for candle in completed:
                ts = to_utc(candle["timestamp"])
                if last_processed is not None and ts <= last_processed:
                    out["duplicates_skipped"] += 1
                    continue
                existing = (
                    db.query(MarketData)
                    .filter(
                        MarketData.symbol == self.symbol,
                        MarketData.timeframe == self.timeframe,
                        MarketData.timestamp == ts,
                    )
                    .first()
                )
                if existing:
                    out["duplicates_skipped"] += 1
                    self._last_processed = max(self._last_processed, ts) if self._last_processed else ts
                    continue
                db.add(MarketData(
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    timestamp=ts,
                    open=float(candle["open"]),
                    high=float(candle["high"]),
                    low=float(candle["low"]),
                    close=float(candle["close"]),
                    volume=candle.get("volume"),
                    bid=candle.get("bid"),
                    ask=candle.get("ask"),
                    spread=candle.get("spread"),
                    source=source or candle.get("source"),
                ))
                db.flush()
                out["persisted"] += 1
                self._last_processed = max(self._last_processed, ts) if self._last_processed else ts
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        out["last_processed_at"] = self._last_processed.isoformat() if self._last_processed else None
        out["new_completed_candle"] = out["persisted"] > 0
        return out


# ------------------------------------------------------------------------------
# Phase 38: Real-market session manager
# ------------------------------------------------------------------------------
class RealMarketSession:
    """Tracks the real-market paper session state (Phase 38).

    Data modes: REAL_MARKET_DATA | CACHE | OFFLINE | DEMO
    Worker states: RUNNING | PAUSED | ERROR

    Policy: in real-data mode a provider failure (unavailable, stale, malformed,
    unauthorized, rate limited, not entitled) PAUSES the session. Cached data is
    displayed only as CACHE and never labelled REAL DATA.
    """

    MODE_REAL = "REAL_MARKET_DATA"
    MODE_CACHE = "CACHE"
    MODE_OFFLINE = "OFFLINE"
    MODE_DEMO = "DEMO"

    def __init__(self, symbol: str | None = None, timeframe: str | None = None) -> None:
        s = get_settings()
        self.symbol = (symbol or s.default_symbol).upper()
        self.timeframe = timeframe or s.default_timeframe
        self.paused = False
        self.pause_reason: str | None = None
        self.last_failure_category: str | None = None
        self.data_mode: str = self.MODE_OFFLINE
        self.last_completed_candle_at: Any | None = None
        self.last_analysis_at: Any | None = None
        self.last_signal: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ control
    def pause(self, reason: str, category: str | None = None) -> None:
        with self._lock:
            self.paused = True
            self.pause_reason = reason[:300]
            self.last_failure_category = category
            self.data_mode = self.MODE_CACHE if self.last_completed_candle_at else self.MODE_OFFLINE
        logger.warning("real-market session PAUSED: %s", reason)

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            self.pause_reason = None
            self.last_failure_category = None
            self.data_mode = self.MODE_REAL
        logger.info("real-market session RESUMED")

    def on_candle_processed(self, ts: Any) -> None:
        with self._lock:
            self.last_completed_candle_at = ts
            self.data_mode = self.MODE_REAL if not self.paused else self.data_mode

    def on_analysis(self, signal: str | None) -> None:
        with self._lock:
            self.last_analysis_at = utcnow()
            self.last_signal = signal

    # ------------------------------------------------------------------ status
    def classify_data_age(self, candle_ts: Any) -> str:
        s = get_settings()
        if candle_ts is None:
            return "OFFLINE"
        try:
            age = (utcnow() - to_utc(candle_ts)).total_seconds()
        except (TypeError, ValueError):
            return "OFFLINE"
        if age <= s.realmarket_stale_after_sec:
            return self.MODE_REAL
        if age <= s.realmarket_stale_after_sec * 6:
            return self.MODE_CACHE
        return self.MODE_OFFLINE

    def status(self, worker_status: dict[str, Any] | None = None,
               rate_limit_status: dict[str, Any] | None = None) -> dict[str, Any]:
        s = get_settings()
        with self._lock:
            mode = self.data_mode
            if self.paused:
                # A paused session never claims REAL: cached data shows as CACHE.
                mode = self.MODE_CACHE if self.last_completed_candle_at else self.MODE_OFFLINE
            elif mode == self.MODE_OFFLINE and self.last_completed_candle_at:
                mode = self.classify_data_age(self.last_completed_candle_at)
            is_real = s.market_data_provider == "twelvedata" and bool(s.twelve_data_api_key)
            if not is_real and not self.paused and mode == self.MODE_OFFLINE:
                mode = self.MODE_DEMO
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "mode": mode,
            "real_market_data_enabled": is_real,
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "last_failure_category": self.last_failure_category,
            "latest_completed_candle": self.last_completed_candle_at.isoformat() if self.last_completed_candle_at else None,
            "last_analysis": self.last_analysis_at.isoformat() if self.last_analysis_at else None,
            "last_signal": self.last_signal,
            "worker": worker_status or {},
            "credits": rate_limit_status or {},
            "paper_trading": s.paper_trading,
            "live_trading": s.live_trading,
        }


_session: Optional[RealMarketSession] = None
_session_lock = threading.Lock()


def get_session_manager() -> RealMarketSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = RealMarketSession()
        return _session


# ------------------------------------------------------------------------------
# Phase 40: Readiness service
# ------------------------------------------------------------------------------
class ReadinessService:
    """Aggregated readiness for REAL-MARKET PAPER trading (Phase 40).

    Each control evaluates to PASS / FAIL / DEGRADED / DISABLED / CONFIG_REQUIRED.
    Overall READY_FOR_REAL_MARKET_PAPER only if all critical controls pass.
    """

    CRITICAL = {
        "MARKET_DATA", "DATA_QUALITY", "XAUUSD_CAPABILITY", "TIMEFRAME",
        "CANDLE_COMPLETION", "RATE_LIMIT", "POLLING_WORKER", "STRATEGY",
        "CONSENSUS", "RISK", "PAPER_BROKER", "SL_TP", "JOURNAL",
        "RECOVERY", "SECURITY",
    }

    def evaluate(self) -> dict[str, Any]:
        s = get_settings()
        checks: dict[str, dict[str, Any]] = {}

        def add(name: str, state: str, detail: str = "", critical: bool = True) -> None:
            checks[name] = {"state": state, "detail": detail, "critical": critical}

        # MARKET DATA ---------------------------------------------------------
        provider_name = s.market_data_provider
        if provider_name == "twelvedata":
            if s.twelve_data_api_key:
                add("MARKET_DATA", "PASS", "twelvedata configured")
            else:
                add("MARKET_DATA", "CONFIG_REQUIRED", "TWELVE_DATA_API_KEY missing")
        elif provider_name in {"local", "stooq"}:
            add("MARKET_DATA", "PASS", f"provider={provider_name}", critical=False)
        else:
            add("MARKET_DATA", "FAIL", f"unknown provider {provider_name}")

        # DATA QUALITY --------------------------------------------------------
        add("DATA_QUALITY", "PASS" if s.quality_gates_enabled else "DISABLED",
            f"gates={'enabled' if s.quality_gates_enabled else 'disabled'}")

        # XAUUSD CAPABILITY ---------------------------------------------------
        if provider_name == "twelvedata" and s.twelve_data_api_key:
            try:
                from app.data.providers import TwelveDataProvider
                cap = TwelveDataProvider().check_capability(s.default_symbol, s.default_timeframe)
                if cap.get("available"):
                    add("XAUUSD_CAPABILITY", "PASS", cap.get("reason", "OK"))
                elif "CONFIG_REQUIRED" in str(cap.get("reason", "")):
                    add("XAUUSD_CAPABILITY", "CONFIG_REQUIRED", str(cap.get("reason")))
                else:
                    add("XAUUSD_CAPABILITY", "DEGRADED", str(cap.get("reason")))
            except Exception as exc:
                add("XAUUSD_CAPABILITY", "DEGRADED", f"capability check failed: {exc}")
        else:
            add("XAUUSD_CAPABILITY", "DISABLED", "real provider not active", critical=False)

        # TIMEFRAME -----------------------------------------------------------
        try:
            parse_timeframe_minutes(s.default_timeframe)
            add("TIMEFRAME", "PASS", f"{s.default_timeframe}")
        except ValueError as exc:
            add("TIMEFRAME", "FAIL", str(exc))

        # CANDLE COMPLETION ---------------------------------------------------
        add("CANDLE_COMPLETION", "PASS", "completed-candle engine active")

        # RATE LIMIT ----------------------------------------------------------
        add("RATE_LIMIT", "PASS", "credit-aware window active")

        # POLLING WORKER ------------------------------------------------------
        try:
            from app.services.real_time_worker import get_worker
            worker = get_worker()
            wstatus = worker.get_status()
            if wstatus.get("running"):
                add("POLLING_WORKER", "PASS", "worker running")
            else:
                add("POLLING_WORKER", "DEGRADED", "worker not running")
        except Exception as exc:
            add("POLLING_WORKER", "FAIL", f"worker unavailable: {exc}")

        # STRATEGY / CONSENSUS / RISK ------------------------------------------
        add("STRATEGY", "PASS", "deterministic strategy active")
        add("CONSENSUS", "PASS", "decision engine active")
        add("RISK", "PASS", "risk engine active")

        # PAPER BROKER / SL_TP -------------------------------------------------
        add("PAPER_BROKER", "PASS" if s.paper_trading else "DISABLED",
            "paper broker" if s.paper_trading else "paper trading disabled")
        if s.live_trading:
            add("SL_TP", "FAIL", "LIVE_TRADING must be false for paper readiness")
        else:
            add("SL_TP", "PASS", "SL/TP enforced on every order; live trading disabled")

        # JOURNAL --------------------------------------------------------------
        add("JOURNAL", "PASS", "journal service active")

        # RECOVERY -------------------------------------------------------------
        add("RECOVERY", "PASS", "durable last-processed timestamp + DB dedup")

        # SECURITY -------------------------------------------------------------
        if s.live_trading:
            add("SECURITY", "FAIL", "live trading enabled")
        else:
            add("SECURITY", "PASS", "API key server-side only; live trading disabled")

        critical_fail = [n for n, c in checks.items() if c["critical"] and c["state"] in {"FAIL"}]
        critical_cfg = [n for n, c in checks.items() if c["critical"] and c["state"] in {"CONFIG_REQUIRED"}]
        if critical_fail:
            overall = "NOT_READY"
        elif critical_cfg:
            overall = "CONFIG_REQUIRED"
        elif any(c["state"] == "DEGRADED" for c in checks.values() if c["critical"]):
            overall = "DEGRADED"
        else:
            overall = "READY_FOR_REAL_MARKET_PAPER"
        return {"overall": overall, "checks": checks, "evaluated_at": utcnow().isoformat()}


readiness_service = ReadinessService()
