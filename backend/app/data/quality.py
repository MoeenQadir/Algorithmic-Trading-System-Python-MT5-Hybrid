"""Historical data quality analysis (Milestone 1).

Provides data quality reports for candle datasets, detecting:
- Missing candles / gaps
- Duplicate timestamps
- Price anomalies (zero, negative, extreme jumps)
- Volume anomalies
- OHLC consistency violations
- Data completeness metrics

Quality Gates: configurable thresholds that must be met for data to be accepted.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from app.utils.timeutils import to_utc
from app.config import get_settings


@dataclass
class QualityThresholds:
    """Configurable quality thresholds for data acceptance."""
    min_completeness_pct: float = 80.0
    max_ohlc_violations_pct: float = 5.0
    max_anomaly_pct: float = 5.0
    max_price_jump_pct: float = 50.0
    min_candles_required: int = 10

    @classmethod
    def from_settings(cls) -> "QualityThresholds":
        """Load thresholds from settings or use defaults."""
        settings = get_settings()
        return cls(
            min_completeness_pct=getattr(settings, "quality_min_completeness_pct", 80.0),
            max_ohlc_violations_pct=getattr(settings, "quality_max_ohlc_violations_pct", 5.0),
            max_anomaly_pct=getattr(settings, "quality_max_anomaly_pct", 5.0),
            max_price_jump_pct=getattr(settings, "quality_max_price_jump_pct", 50.0),
            min_candles_required=getattr(settings, "quality_min_candles_required", 10),
        )


class QualityGateError(Exception):
    """Raised when data fails quality gate validation."""
    def __init__(self, message: str, report: QualityReport):
        super().__init__(message)
        self.report = report


def check_quality_gates(
    report: QualityReport,
    thresholds: Optional[QualityThresholds] = None,
) -> tuple[bool, list[str]]:
    """Check if a quality report passes all quality gates.

    Returns:
        Tuple of (passed: bool, failed_checks: list[str])
    """
    if thresholds is None:
        thresholds = QualityThresholds.from_settings()

    failed = []

    # Check minimum candles
    if report.candle_count < thresholds.min_candles_required:
        failed.append(f"insufficient_candles: {report.candle_count} < {thresholds.min_candles_required}")

    # Check completeness
    if report.completeness_pct < thresholds.min_completeness_pct:
        failed.append(f"low_completeness: {report.completeness_pct}% < {thresholds.min_completeness_pct}%")

    # Check OHLC violations
    ohlc_pct = (report.ohlc_violations / report.candle_count * 100) if report.candle_count > 0 else 100.0
    if ohlc_pct > thresholds.max_ohlc_violations_pct:
        failed.append(f"high_ohlc_violations: {ohlc_pct:.1f}% > {thresholds.max_ohlc_violations_pct}%")

    # Check anomaly rate
    anomaly_pct = (len(report.anomalies) / report.candle_count * 100) if report.candle_count > 0 else 100.0
    if anomaly_pct > thresholds.max_anomaly_pct:
        failed.append(f"high_anomaly_rate: {anomaly_pct:.1f}% > {thresholds.max_anomaly_pct}%")

    # Check extreme price jumps specifically
    extreme_jumps = [a for a in report.anomalies if a.get("type") == "extreme_price_jump"]
    jump_pct = (len(extreme_jumps) / report.candle_count * 100) if report.candle_count > 0 else 0.0
    if jump_pct > thresholds.max_price_jump_pct:
        failed.append(f"excessive_price_jumps: {jump_pct:.1f}% > {thresholds.max_price_jump_pct}%")

    return len(failed) == 0, failed


def validate_quality_or_raise(
    candles: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    expected_interval_minutes: int,
    thresholds: Optional[QualityThresholds] = None,
) -> QualityReport:
    """Analyze candles and raise QualityGateError if they fail quality gates.

    Args:
        candles: List of normalized candles
        symbol: Ticker symbol
        timeframe: Timeframe string
        expected_interval_minutes: Expected interval in minutes
        thresholds: Optional custom thresholds

    Returns:
        QualityReport if validation passes

    Raises:
        QualityGateError: If data fails quality gates
    """
    report = analyze_candles(candles, symbol, timeframe, expected_interval_minutes)
    passed, failed = check_quality_gates(report, thresholds)
    if not passed:
        raise QualityGateError(f"Quality gate failed: {', '.join(failed)}", report)
    return report


@dataclass
class QualityReport:
    """Data quality report for a candle dataset."""

    symbol: str
    timeframe: str
    candle_count: int
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    completeness_pct: float = 0.0
    missing_candles: int = 0
    duplicate_timestamps: list[datetime] = field(default_factory=list)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    ohlc_violations: int = 0
    price_range: tuple[float, float] = (0.0, 0.0)
    volume_stats: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "candle_count": self.candle_count,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "completeness_pct": self.completeness_pct,
            "missing_candles": self.missing_candles,
            "duplicate_timestamps": [ts.isoformat() for ts in self.duplicate_timestamps[:10]],
            "duplicate_count": len(self.duplicate_timestamps),
            "anomalies": self.anomalies[:20],
            "anomaly_count": len(self.anomalies),
            "ohlc_violations": self.ohlc_violations,
            "price_range": list(self.price_range),
            "volume_stats": self.volume_stats,
            "source": self.source,
        }


def analyze_candles(
    candles: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    expected_interval_minutes: int,
) -> QualityReport:
    """Analyze a list of normalized candles and produce a quality report."""
    if not candles:
        return QualityReport(symbol=symbol, timeframe=timeframe, candle_count=0)

    timestamps = [c["timestamp"] for c in candles]
    closes = [c["close"] for c in candles if c.get("close") is not None]

    # Detect duplicates
    ts_counter = Counter(timestamps)
    duplicates = [ts for ts, count in ts_counter.items() if count > 1]

    # Detect OHLC violations
    ohlc_violations = 0
    for c in candles:
        o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
        if None in (o, h, l, cl):
            ohlc_violations += 1
            continue
        if not (h >= l and h >= o and h >= cl and l <= o and l <= cl):
            ohlc_violations += 1

    # Detect anomalies (extreme price jumps > 50%)
    anomalies: list[dict[str, Any]] = []
    prev_close: Optional[float] = None
    for c in candles:
        close = c.get("close")
        if close is None:
            continue
        if prev_close is not None and prev_close > 0:
            change_pct = abs(close - prev_close) / prev_close
            if change_pct > 0.50:
                anomalies.append({
                    "type": "extreme_price_jump",
                    "timestamp": c["timestamp"].isoformat(),
                    "previous_close": prev_close,
                    "close": close,
                    "change_pct": round(change_pct * 100, 2),
                })
        prev_close = close

    # Zero or negative prices
    for c in candles:
        for field_name in ("open", "high", "low", "close"):
            val = c.get(field_name)
            if val is not None and val <= 0:
                anomalies.append({
                    "type": "invalid_price",
                    "timestamp": c["timestamp"].isoformat(),
                    "field": field_name,
                    "value": val,
                })

    # Volume stats
    volumes = [c["volume"] for c in candles if c.get("volume") is not None and c["volume"] > 0]
    volume_stats: dict[str, Any] = {}
    if volumes:
        volume_stats = {
            "count": len(volumes),
            "min": min(volumes),
            "max": max(volumes),
            "avg": round(sum(volumes) / len(volumes), 2),
        }

    # Completeness: expected candles vs actual
    if len(timestamps) >= 2:
        start = min(timestamps)
        end = max(timestamps)
        span_minutes = (end - start).total_seconds() / 60
        expected = int(span_minutes / expected_interval_minutes) + 1
        completeness = min(100.0, (len(candles) / expected) * 100) if expected > 0 else 100.0
        missing = max(0, expected - len(candles))
    else:
        start = timestamps[0] if timestamps else None
        end = timestamps[0] if timestamps else None
        completeness = 100.0
        missing = 0

    # Price range
    all_prices = [p for p in closes if p is not None and p > 0]
    price_range = (min(all_prices), max(all_prices)) if all_prices else (0.0, 0.0)

    return QualityReport(
        symbol=symbol,
        timeframe=timeframe,
        candle_count=len(candles),
        start=start,
        end=end,
        completeness_pct=round(completeness, 2),
        missing_candles=missing,
        duplicate_timestamps=duplicates,
        anomalies=anomalies,
        ohlc_violations=ohlc_violations,
        price_range=price_range,
        volume_stats=volume_stats,
        source=candles[0].get("source", "unknown") if candles else "unknown",
    )


def detect_gaps(
    candles: list[dict[str, Any]],
    expected_interval_minutes: int,
    max_gap_candles: int = 3,
) -> list[dict[str, Any]]:
    """Detect gaps larger than max_gap_candles in the candle sequence."""
    gaps: list[dict[str, Any]] = []
    if len(candles) < 2:
        return gaps

    expected_delta = timedelta(minutes=expected_interval_minutes)
    for i in range(1, len(candles)):
        delta = candles[i]["timestamp"] - candles[i - 1]["timestamp"]
        if delta > expected_delta:
            gap_candles = int(delta.total_seconds() / 60 / expected_interval_minutes) - 1
            if gap_candles > max_gap_candles:
                gaps.append({
                    "after": candles[i - 1]["timestamp"].isoformat(),
                    "before": candles[i]["timestamp"].isoformat(),
                    "gap_candles": gap_candles,
                    "gap_minutes": int(delta.total_seconds() / 60),
                })
    return gaps


def validate_candle_schema(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate that all candles have the required schema fields."""
    errors: list[dict[str, Any]] = []
    required_fields = ("timestamp", "symbol", "timeframe", "open", "high", "low", "close")
    for i, c in enumerate(candles):
        missing = [f for f in required_fields if f not in c or c.get(f) is None]
        if missing:
            errors.append({
                "index": i,
                "timestamp": c.get("timestamp", None),
                "missing_fields": missing,
            })
    return errors
