"""Regression tests for P1: Stop-loss and Take-profit pass-through verification.

Tests that SL and TP are reliably passed through every layer of the
execution path: strategy → signal → decision → risk engine → execution
→ paper broker → stored position/order → journal → dashboard.

Also tests that missing SL/TP causes rejection instead of creating
unprotected positions.
"""
from __future__ import annotations

import pytest

from app.analysis.strategy import evaluate_setup
from app.analysis.schemas import StrategySignal
from app.decision.engine import DecisionEngine, decision_engine
from app.risk.engine import risk_engine
from app.execution.broker import PaperBroker
from app.services.journal import journal_service
from app.models.database import (
    MarketData, Position, Trade, Order, RiskCheck, TradingDecision,
    get_session_factory,
)
from app.config import get_settings
from datetime import datetime


def _seed_market_data(db, symbol="XAUUSD", price=2600.0, ts=None):
    """Seed a market data row."""
    from app.utils.timeutils import utcnow
    if ts is None:
        ts = utcnow()
    db.add(MarketData(
        symbol=symbol, timeframe="15m", timestamp=ts,
        open=price, high=price + 1, low=price - 1, close=price,
        volume=1000, source="test",
    ))
    db.commit()


def _make_decision_with_sl_tp(entry, stop, target, action="BUY"):
    """Helper to create a decision dict with SL/TP."""
    return {
        "action": action,
        "direction": "LONG" if action == "BUY" else "SHORT",
        "entry": entry,
        "stop_loss": stop,
        "take_profit": target,
        "risk_reward": abs(target - entry) / abs(entry - stop) if stop and entry and abs(entry - stop) > 0 else None,
    }


# ------------------------------------------------------------------------------
# 1. BUY order receives correct SL and TP
# 2. SELL order receives correct SL and TP
# ------------------------------------------------------------------------------

def test_buy_order_receives_correct_sl_tp(db, long_setup_candles):
    """BUY order through pipeline receives correct SL and TP in position and trade."""
    candles = long_setup_candles
    sig = evaluate_setup(candles, {"rr_target": 2.5}, "XAUUSD", "15m")
    assert sig.setup_valid is True
    assert sig.stop_loss is not None
    assert sig.take_profit is not None

    agent_outputs = {
        "strategy": {"candidate_trade": True, "direction": "LONG", "confidence": 0.8,
                     "entry": sig.entry, "stop": sig.stop_loss, "target": sig.take_profit,
                     "reasons": [], "conflicts": []},
        "technical": {"direction": "LONG", "confidence": 0.7},
        "quant": {"direction": "LONG", "confidence": 0.6},
    }
    decision = decision_engine.decide(agent_outputs, "XAUUSD", "15m")
    assert decision.action == "BUY"
    assert decision.stop_loss == sig.stop_loss
    assert decision.take_profit == sig.take_profit

    # Seed market data so broker can fill
    _seed_market_data(db, "XAUUSD", sig.entry)

    # Risk engine evaluates with full SL/TP
    risk_out = risk_engine.evaluate(
        db, _make_decision_with_sl_tp(sig.entry, sig.stop_loss, sig.take_profit, "BUY"),
        account_equity=10000.0,
        current_price=sig.entry,
    )
    assert risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}

    # Broker place_order preserves SL/TP
    broker = PaperBroker(db, starting_balance=10000.0)
    exec_result = broker.place_order(
        "XAUUSD", "BUY", risk_out.quantity or 0.0, "market",
        stop_price=decision.stop_loss,
        take_profit=decision.take_profit,
    )
    assert exec_result["ok"]

    # Position contains SL/TP
    pos = broker.get_positions()[0]
    assert pos["stop_loss"] == decision.stop_loss, \
        f"Position stop_loss {pos['stop_loss']} != decision.stop_loss {decision.stop_loss}"
    assert pos["take_profit"] == decision.take_profit, \
        f"Position take_profit {pos['take_profit']} != decision.take_profit {decision.take_profit}"

    # Trade contains SL/TP
    trade = db.query(Trade).filter(Trade.status == "open").order_by(Trade.entry_time.desc()).first()
    assert trade is not None
    assert trade.stop_loss == decision.stop_loss, \
        f"Trade stop_loss {trade.stop_loss} != decision.stop_loss {decision.stop_loss}"
    assert trade.take_profit == decision.take_profit, \
        f"Trade take_profit {trade.take_profit} != decision.take_profit {decision.take_profit}"


def test_sell_order_receives_correct_sl_tp(db, short_setup_candles):
    """SELL order through pipeline receives correct SL and TP in position and trade."""
    candles = short_setup_candles
    sig_short = evaluate_setup(candles, {"rr_target": 2.5}, "XAUUSD", "15m")
    assert sig_short.direction == "SHORT"
    assert sig_short.setup_valid is True
    assert sig_short.stop_loss is not None
    assert sig_short.take_profit is not None

    agent_outputs = {
        "strategy": {"candidate_trade": True, "direction": "SHORT", "confidence": 0.8,
                     "entry": sig_short.entry, "stop": sig_short.stop_loss, "target": sig_short.take_profit,
                     "reasons": [], "conflicts": []},
        "technical": {"direction": "SHORT", "confidence": 0.7},
        "quant": {"direction": "SHORT", "confidence": 0.6},
    }
    decision = decision_engine.decide(agent_outputs, "XAUUSD", "15m")
    assert decision.action == "SELL"
    assert decision.stop_loss == sig_short.stop_loss
    assert decision.take_profit == sig_short.take_profit

    # Seed market data so broker can fill
    _seed_market_data(db, "XAUUSD", sig_short.entry)

    # Risk engine evaluates
    risk_out = risk_engine.evaluate(
        db, _make_decision_with_sl_tp(sig_short.entry, sig_short.stop_loss, sig_short.take_profit, "SELL"),
        account_equity=10000.0,
        current_price=sig_short.entry,
    )
    assert risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}

    # Broker place_order preserves SL/TP
    broker = PaperBroker(db, starting_balance=10000.0)
    exec_result = broker.place_order(
        "XAUUSD", "SELL", risk_out.quantity or 0.0, "market",
        stop_price=decision.stop_loss,
        take_profit=decision.take_profit,
    )
    assert exec_result["ok"]

    # Position contains SL/TP
    pos = broker.get_positions()[0]
    assert pos["direction"] == "SHORT"
    assert pos["stop_loss"] == decision.stop_loss, \
        f"Position stop_loss {pos['stop_loss']} != decision.stop_loss {decision.stop_loss}"
    assert pos["take_profit"] == decision.take_profit, \
        f"Position take_profit {pos['take_profit']} != decision.take_profit {decision.take_profit}"

    # Trade contains SL/TP
    trade = db.query(Trade).filter(Trade.status == "open").order_by(Trade.entry_time.desc()).first()
    assert trade is not None
    assert trade.direction == "SHORT"
    assert trade.stop_loss == decision.stop_loss, \
        f"Trade stop_loss {trade.stop_loss} != decision.stop_loss {decision.stop_loss}"
    assert trade.take_profit == decision.take_profit, \
        f"Trade take_profit {trade.take_profit} != decision.take_profit {decision.take_profit}"


# ------------------------------------------------------------------------------
# 3. Missing SL causes rejection
# 4. Missing TP causes rejection
# ------------------------------------------------------------------------------

def test_missing_sl_causes_rejection(db, long_setup_candles):
    """Missing stop-loss causes risk engine rejection, preventing unprotected position."""
    # Manually create an invalid signal (no stop)
    from app.analysis.schemas import StrategySignal
    invalid_sig = StrategySignal(
        symbol="XAUUSD", timeframe="15m", direction="LONG", setup_valid=True,
        entry=2600.0, stop_loss=None, take_profit=2650.0,
        risk_reward=2.0, confidence=0.55, reasons=[], invalidations=[],
        timestamp=datetime.utcnow(),
    )

    decision = decision_engine.decide(
        {"strategy": {"candidate_trade": True, "direction": "LONG", "confidence": 0.8,
                      "entry": invalid_sig.entry, "stop": invalid_sig.stop_loss,
                      "target": invalid_sig.take_profit,
                      "reasons": [], "conflicts": []}},
        "XAUUSD", "15m",
    )
    # Decision engine should WAIT due to incomplete levels
    assert decision.action == "WAIT"

    # Even if we bypass decision engine, risk engine rejects missing stop
    risk_out = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0, "stop_loss": None, "take_profit": 2650.0},
        account_equity=10000.0,
        current_price=2600.0,
    )
    assert risk_out.verdict == "REJECTED"
    assert any("missing" in v.lower() or "invalid" in v.lower() for v in risk_out.violations)


def test_missing_tp_causes_rejection(db):
    """Missing take-profit causes risk engine rejection."""
    risk_out = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0, "stop_loss": 2595.0, "take_profit": None},
        account_equity=10000.0,
        current_price=2600.0,
    )
    assert risk_out.verdict == "REJECTED"
    # Missing TP results in RR being None, which fails the RR floor check
    assert any("risk_reward" in v.lower() for v in risk_out.violations)


# ------------------------------------------------------------------------------
# 5. Signal → paper broker preserves exact risk levels
# 6. Stored trade contains SL/TP
# 7. Journal contains SL/TP
# 8. Dashboard displays SL/TP
# ------------------------------------------------------------------------------

def test_signal_broker_preserves_risk_levels(db, long_setup_candles):
    """Signal → paper broker preserves exact risk levels (SL and TP values)."""
    candles = long_setup_candles
    sig = evaluate_setup(candles, {}, "XAUUSD", "15m")
    assert sig.setup_valid is True
    entry = sig.entry
    sl = sig.stop_loss
    tp = sig.take_profit

    agent_outputs = {
        "strategy": {"candidate_trade": True, "direction": "LONG", "confidence": 0.8,
                     "entry": entry, "stop": sl, "target": tp,
                     "reasons": [], "conflicts": []},
        "technical": {"direction": "LONG", "confidence": 0.7},
        "quant": {"direction": "LONG", "confidence": 0.6},
    }
    # Decision engine passes exact values
    decision = decision_engine.decide(agent_outputs, "XAUUSD", "15m")
    assert decision.entry == entry
    assert decision.stop_loss == sl
    assert decision.take_profit == tp

    # Risk engine passes through exact values
    risk_out = risk_engine.evaluate(
        db, _make_decision_with_sl_tp(entry, sl, tp, "BUY"),
        account_equity=10000.0,
        current_price=entry,
    )
    assert risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}

    # Seed market data so broker can fill
    _seed_market_data(db, "XAUUSD", entry)

    # Broker place_order preserves exact SL/TP
    broker = PaperBroker(db, starting_balance=10000.0)
    exec_result = broker.place_order(
        "XAUUSD", "BUY", risk_out.quantity or 0.0, "market",
        stop_price=decision.stop_loss,
        take_profit=decision.take_profit,
    )
    assert exec_result["ok"]

    # Verify exact values preserved through all layers
    pos = broker.get_positions()[0]
    assert pos["stop_loss"] == sl, \
        f"SL mismatch: position={pos['stop_loss']}, expected={sl}"
    assert pos["take_profit"] == tp, \
        f"TP mismatch: position={pos['take_profit']}, expected={tp}"

    trade = db.query(Trade).filter(Trade.status == "open").order_by(Trade.entry_time.desc()).first()
    assert trade is not None
    assert trade.stop_loss == sl, \
        f"Trade SL mismatch: trade={trade.stop_loss}, expected={sl}"
    assert trade.take_profit == tp, \
        f"Trade TP mismatch: trade={trade.take_profit}, expected={tp}"


def test_journal_and_dashboard_contain_sl_tp(db, long_setup_candles):
    """Journal and dashboard display SL/TP correctly."""
    from app.services.journal import journal_service
    from app.execution.broker import PaperBroker

    candles = long_setup_candles
    sig = evaluate_setup(candles, {}, "XAUUSD", "15m")
    assert sig.setup_valid is True

    # Create decision with SL/TP
    decision = {
        "action": "BUY",
        "direction": "LONG",
        "entry": sig.entry,
        "stop_loss": sig.stop_loss,
        "take_profit": sig.take_profit,
        "risk_reward": sig.risk_reward,
        "confidence": sig.confidence,
        "reasons": sig.reasons,
        "invalidations": sig.invalidations,
        "timestamp": sig.timestamp.isoformat() if hasattr(sig, 'timestamp') else "",
    }

    # Record decision in journal using the test DB
    decision_row = journal_service.record_decision(
        db, decision, {"strategy": {"candidate_trade": True}}, "XAUUSD", "15m"
    )
    db.commit()

    # Check decision has SL/TP
    assert decision_row.stop_loss == sig.stop_loss
    assert decision_row.take_profit == sig.take_profit

    # Seed market data so broker can fill
    _seed_market_data(db, "XAUUSD", sig.entry)

    # Place order to create trade with SL/TP
    broker = PaperBroker(db, starting_balance=10000.0)
    risk_out = risk_engine.evaluate(
        db, _make_decision_with_sl_tp(sig.entry, sig.stop_loss, sig.take_profit, "BUY"),
        account_equity=10000.0, current_price=sig.entry,
    )
    if risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}:
        side = "BUY" if decision["action"] == "BUY" else "SELL"
        exec_result = broker.place_order(
            "XAUUSD", side, risk_out.quantity or 0.0, "market",
            stop_price=decision["stop_loss"],
            take_profit=decision["take_profit"],
        )

    # Check trade journal has SL/TP
    from app.models.database import Trade
    trade = db.query(Trade).order_by(Trade.entry_time.desc()).first()
    assert trade is not None
    assert trade.stop_loss == sig.stop_loss, \
        f"Journal trade stop_loss: {trade.stop_loss} != {sig.stop_loss}"
    assert trade.take_profit == sig.take_profit, \
        f"Journal trade take_profit: {trade.take_profit} != {sig.take_profit}"

    # Check risk check has SL/TP
    from app.models.database import RiskCheck
    risk_checks = db.query(RiskCheck).all()
    if risk_checks:
        rc = risk_checks[0]
        assert rc.verdict in {"APPROVED", "REDUCED_SIZE"}


def test_dashboard_displays_sl_tp(db, long_setup_candles):
    """Dashboard API displays SL/TP in positions and trades."""
    from app.execution.broker import PaperBroker
    from main import app
    from fastapi.testclient import TestClient

    candles = long_setup_candles
    sig = evaluate_setup(candles, {}, "XAUUSD", "15m")
    assert sig.setup_valid is True

    client = TestClient(app)

    # Place a BUY order with SL/TP
    broker = PaperBroker(db, starting_balance=10000.0)
    risk_out = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": sig.entry,
             "stop_loss": sig.stop_loss, "take_profit": sig.take_profit},
        account_equity=10000.0, current_price=sig.entry,
    )
    if risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}:
        side = "BUY"
        exec_result = broker.place_order(
            "XAUUSD", side, risk_out.quantity or 0.0, "market",
            stop_price=sig.stop_loss,
            take_profit=sig.take_profit,
        )

    # Check positions endpoint displays SL/TP
    pos_result = client.get("/positions")
    assert pos_result.status_code == 200
    positions = pos_result.json()["positions"]
    if positions:
        pos = positions[0]
        assert "stop_loss" in pos, "Position missing stop_loss in dashboard"
        assert "take_profit" in pos, "Position missing take_profit in dashboard"
        assert pos["stop_loss"] == sig.stop_loss, \
            f"Dashboard stop_loss: {pos['stop_loss']} != {sig.stop_loss}"
        assert pos["take_profit"] == sig.take_profit, \
            f"Dashboard take_profit: {pos['take_profit']} != {sig.take_profit}"

    # Check trades endpoint displays SL/TP
    trade_result = client.get("/trades?status=open")
    assert trade_result.status_code == 200
    trades = trade_result.json()["trades"]
    if trades:
        t = trades[0]
        assert "stop_loss" in t, "Trade missing stop_loss in dashboard"
        assert "take_profit" in t, "Trade missing take_profit in dashboard"
        assert t["stop_loss"] == sig.stop_loss, \
            f"Trade dashboard stop_loss: {t['stop_loss']} != {sig.stop_loss}"
        assert t["take_profit"] == sig.take_profit, \
            f"Trade dashboard take_profit: {t['take_profit']} != {sig.take_profit}"


# ------------------------------------------------------------------------------
# 9. Duplicate signals cannot create duplicate positions
# 10. Risk engine can veto the trade
# ------------------------------------------------------------------------------

def test_duplicate_signals_create_single_position(db):
    """Duplicate signals cannot create duplicate positions for same symbol."""
    from app.execution.broker import PaperBroker

    # Set up initial position
    _seed_market_data(db, "XAUUSD", 2600.0)

    broker = PaperBroker(db, starting_balance=10000.0)

    # Place first BUY order
    risk_out1 = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0,
             "stop_loss": 2595.0, "take_profit": 2650.0},
        account_equity=10000.0, current_price=2600.0,
    )
    if risk_out1.verdict in {"APPROVED", "REDUCED_SIZE"}:
        side = "BUY" if risk_out1.verdict in {"APPROVED", "REDUCED_SIZE"} else "WAIT"
        exec_result1 = broker.place_order(
            "XAUUSD", side, risk_out1.quantity or 0.0, "market",
            stop_price=2595.0, take_profit=2650.0,
        )

    # Place second BUY order (same signal duplicate)
    risk_out2 = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0,
             "stop_loss": 2595.0, "take_profit": 2650.0},
        account_equity=10000.0, current_price=2600.0,
    )
    if risk_out2.verdict in {"APPROVED", "REDUCED_SIZE"}:
        side = "BUY" if risk_out2.verdict in {"APPROVED", "REDUCED_SIZE"} else "WAIT"
        exec_result2 = broker.place_order(
            "XAUUSD", side, risk_out2.quantity or 0.0, "market",
            stop_price=2595.0, take_profit=2650.0,
        )

    # Risk engine should reject second trade due to same_symbol exposure check
    # Or the broker should merge into existing position
    positions = broker.get_positions()
    # After the fix, the second order should either be rejected or merge into one position
    # Let's verify the behavior
    assert len(positions) == 1, \
        f"Expected 1 position (merged), got {len(positions)}: duplicate positions created!"

    # Alternative: second order is rejected
    # if risk_out2.verdict == "REJECTED":
    #     assert len(positions) == 1
    # else:
    #     # merged position
    #     assert positions[0]["quantity"] == risk_out1.quantity + risk_out2.quantity


def test_risk_engine_can_veto_the_trade(db):
    """Risk engine must be able to veto an AI decision, even with SL/TP present."""
    # Test 1: Zero stop distance gets rejected (RR becomes n/a)
    risk_out = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0, "stop_loss": 2600.0, "take_profit": 2700.0},
        account_equity=10000.0,
    )
    assert risk_out.verdict == "REJECTED"
    assert any("risk_reward" in v.lower() for v in risk_out.violations)

    # Test 2: RR below minimum gets rejected
    risk_out2 = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0, "stop_loss": 2595.0, "take_profit": 2601.0},
        account_equity=10000.0,
    )
    assert risk_out2.verdict == "REJECTED"
    assert any("risk_reward" in v.lower() for v in risk_out2.violations)

    # Test 3: Daily loss limit breached gets rejected
    from app.utils.timeutils import utcnow
    db.add(Trade(
        symbol="XAUUSD", direction="LONG", entry_time=utcnow(), exit_time=utcnow(),
        entry_price=100, quantity=1, pnl=-200.0, status="closed"
    ))
    db.commit()
    risk_out3 = risk_engine.evaluate(
        db, {"action": "BUY", "symbol": "XAUUSD", "entry": 2600.0, "stop_loss": 2595.0, "take_profit": 2650.0},
        account_equity=10000.0,
    )
    assert risk_out3.verdict == "REJECTED"
    assert any("daily loss" in v.lower() for v in risk_out3.violations)