"""Risk engine (Milestones 14-15). Deterministic. No LLM. Final veto authority.

Enforces: MAX_RISK_PER_TRADE, MAX_DAILY_LOSS, MAX_CONSECUTIVE_LOSSES,
MAX_OPEN_POSITIONS, MIN_RISK_REWARD, MAX_POSITION_SIZE, MAX_SYMBOL_EXPOSURE,
MAX_TOTAL_EXPOSURE, spread limits, data freshness, market-hours rules,
news-event restrictions. Outputs APPROVED / REJECTED / REDUCED_SIZE / WAIT.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.analysis.schemas import RiskEngineOutput
from app.config import get_settings
from app.models.database import MarketData, NewsEvent, Position, Trade
from app.utils.timeutils import to_utc, utcnow

logger = logging.getLogger("trading.risk")


def _deterministic_position_size(
    account_equity: float,
    risk_percentage: float,
    entry: float,
    stop: float,
    direction: str,
    contract_multiplier: float = 1.0,
) -> dict[str, Optional[float]]:
    """Deterministic sizing. Returns risk_amount, quantity, notional, max_loss.

    Handles invalid inputs (zero/negative stop distance, invalid prices) by
    returning zero size rather than raising.
    """
    out: dict[str, Optional[float]] = {"risk_amount": None, "quantity": None,
                                       "notional": None, "max_loss": None}
    try:
        equity = float(account_equity)
        risk_pct = float(risk_percentage)
        entry_f, stop_f = float(entry), float(stop)
        mult = float(contract_multiplier) if contract_multiplier else 1.0
    except (TypeError, ValueError):
        return out
    if equity <= 0 or risk_pct <= 0 or entry_f <= 0 or stop_f <= 0 or mult <= 0:
        return out
    stop_dist = abs(entry_f - stop_f)
    if stop_dist <= 0:
        return out
    risk_amount = equity * risk_pct
    per_unit_risk = stop_dist * mult
    if per_unit_risk <= 0:
        return out
    quantity = risk_amount / per_unit_risk
    if quantity <= 0:
        return out
    out.update({
        "risk_amount": round(risk_amount, 10),
        "quantity": round(quantity, 10),
        "notional": round(quantity * entry_f * mult, 10),
        "max_loss": round(quantity * per_unit_risk, 10),
    })
    return out


def compute_position_size(account_equity: float, risk_percentage: float, entry: float,
                          stop: float, direction: str,
                          contract_multiplier: float = 1.0) -> dict[str, Optional[float]]:
    """Public wrapper; direction only affects sign conventions, sizing is stop-distance based."""
    return _deterministic_position_size(account_equity, risk_percentage, entry, stop,
                                        direction, contract_multiplier)


class RiskEngine:
    name = "risk"

    def evaluate(
        self,
        db: Session,
        decision: dict[str, Any],
        *,
        account_equity: float,
        current_price: float | None = None,
        spread: float | None = None,
        last_candle_time: datetime | None = None,
        contract_multiplier: float = 1.0,
    ) -> RiskEngineOutput:
        """Evaluate a decision (BUY/SELL with entry/stop/target) against all limits.

        The Risk Engine may override any AI decision. This never calls an LLM.
        """
        s = get_settings()
        action = str(decision.get("action") or "WAIT").upper()
        violations: list[str] = []
        details: dict[str, Any] = {"checks": {}}

        if action not in {"BUY", "SELL"}:
            return RiskEngineOutput(verdict="WAIT", violations=["decision is not actionable"],
                                    details={**details, "action": action})

        direction = "LONG" if action == "BUY" else "SHORT"
        entry = decision.get("entry")
        stop = decision.get("stop_loss")
        target = decision.get("take_profit")
        if entry is None or stop is None or entry <= 0 or stop <= 0:
            return RiskEngineOutput(verdict="REJECTED", violations=["invalid or missing entry/stop"], details=details)
        rr = abs(target - entry) / abs(entry - stop) if (target and abs(entry - stop) > 0) else None

        # --- RR floor -------------------------------------------------------
        if rr is None or rr < s.min_risk_reward:
            violations.append(f"risk_reward {rr if rr is not None else 'n/a'} below minimum {s.min_risk_reward}")
            return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        # --- data freshness ---------------------------------------------------
        if last_candle_time is not None:
            age_min = (utcnow() - to_utc(last_candle_time)).total_seconds() / 60.0
            details["checks"]["data_age_minutes"] = round(age_min, 1)
            if age_min > s.max_data_age_minutes:
                violations.append(f"data stale: {age_min:.0f} min > {s.max_data_age_minutes}")
                return RiskEngineOutput(verdict="WAIT", violations=violations, details=details)

        # --- spread limit ------------------------------------------------------
        if spread is not None and current_price:
            spread_pct = spread / current_price
            details["checks"]["spread_pct"] = round(spread_pct, 6)
            if spread_pct > s.max_spread_pct:
                violations.append(f"spread {spread_pct:.5f} exceeds limit {s.max_spread_pct}")
                return RiskEngineOutput(verdict="WAIT", violations=violations, details=details)

        # --- market hours (optional) --------------------------------------------
        if s.enforce_market_hours:
            now_utc = utcnow()
            weekday = now_utc.weekday()  # 0=Mon
            hour = now_utc.hour
            if weekday >= 5 or (weekday == 4 and hour >= 21) or (weekday == 0 and hour < 22):
                violations.append("outside market hours")
                return RiskEngineOutput(verdict="WAIT", violations=violations, details=details)

        # --- news blackout -------------------------------------------------------
        blackout_cutoff = utcnow() - timedelta(minutes=s.news_blackout_minutes)
        recent_high_impact = (
            db.query(NewsEvent)
            .filter(NewsEvent.impact == "high", NewsEvent.published_at >= blackout_cutoff)
            .count()
        )
        if recent_high_impact:
            violations.append(f"high-impact news within {s.news_blackout_minutes} min blackout")
            return RiskEngineOutput(verdict="WAIT", violations=violations, details=details)

        # --- portfolio state limits -----------------------------------------------
        open_positions: list[Position] = db.query(Position).filter(Position.status == "open").all()
        if len(open_positions) >= s.max_open_positions:
            violations.append(f"open positions {len(open_positions)} >= max {s.max_open_positions}")
            return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        symbol = str(decision.get("symbol") or "")
        same_symbol = [p for p in open_positions if p.symbol == symbol]
        if same_symbol:
            violations.append(f"already exposed to {symbol}")
            return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        # daily loss
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_trades: list[Trade] = (
            db.query(Trade).filter(Trade.exit_time >= day_start, Trade.status == "closed").all()
        )
        realized_today = sum((t.pnl or 0.0) for t in today_trades)
        daily_loss_limit = account_equity * s.max_daily_loss
        details["checks"]["realized_today"] = round(realized_today, 2)
        details["checks"]["daily_loss_limit"] = round(daily_loss_limit, 2)
        if realized_today <= -daily_loss_limit:
            violations.append(f"daily loss {realized_today:.2f} breached limit -{daily_loss_limit:.2f}")
            return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        # consecutive losses
        last_closed = (
            db.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_time.desc()).limit(s.max_consecutive_losses).all()
        )
        if len(last_closed) == s.max_consecutive_losses and all((t.pnl or 0) < 0 for t in last_closed):
            violations.append(f"{s.max_consecutive_losses} consecutive losses - trading halted for now")
            return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        # --- position sizing --------------------------------------------------------
        sizing = compute_position_size(account_equity, s.risk_percentage, entry, stop, direction, contract_multiplier)
        quantity = sizing["quantity"] or 0.0
        risk_amount = sizing["risk_amount"] or 0.0
        notional = sizing["notional"] or 0.0

        # cap risk per trade (hard limit overrides the percentage if it's lower)
        max_risk_amount = account_equity * s.max_risk_per_trade
        if risk_amount > max_risk_amount > 0:
            scale = max_risk_amount / risk_amount
            quantity *= scale
            risk_amount = max_risk_amount
            notional = quantity * entry * contract_multiplier
            violations.append(f"risk reduced to max_risk_per_trade ({max_risk_amount:.2f})")

        # MAX_POSITION_SIZE (notional cap)
        if notional > s.max_position_size > 0:
            scale = s.max_position_size / notional
            quantity *= scale
            notional *= scale
            risk_amount *= scale
            violations.append(f"notional reduced to MAX_POSITION_SIZE ({s.max_position_size:.2f})")

        # exposure caps
        open_notional = sum((p.quantity or 0) * (p.entry_price or 0) for p in open_positions)
        symbol_notional = sum(
            (p.quantity or 0) * (p.entry_price or 0) for p in open_positions if p.symbol == symbol
        )
        symbol_limit = account_equity * s.max_symbol_exposure
        if s.max_symbol_exposure > 0 and symbol_notional + notional > symbol_limit:
            available = symbol_limit - symbol_notional
            if available <= 0:
                violations.append("symbol exposure limit breached")
                return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)
            scale = available / notional
            quantity *= scale
            notional = available
            risk_amount *= scale
            violations.append(f"position reduced to MAX_SYMBOL_EXPOSURE ({symbol_limit:.2f})")

        if account_equity > 0:
            if (symbol_notional + notional) / account_equity > s.max_total_exposure:
                violations.append("total exposure limit breached")
                return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        if quantity <= 0 or risk_amount <= 0:
            violations.append("computed position size is zero")
            return RiskEngineOutput(verdict="REJECTED", violations=violations, details=details)

        reduced = any("reduced" in v for v in violations)
        return RiskEngineOutput(
            verdict="REDUCED_SIZE" if reduced else "APPROVED",
            violations=violations,
            position_size=round(notional, 2),
            risk_amount=round(risk_amount, 2),
            quantity=round(quantity, 10),
            notional=round(notional, 2),
            max_loss=round(risk_amount, 2),
            details=details,
        )


risk_engine = RiskEngine()
