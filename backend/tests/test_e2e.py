"""End-to-end test (Milestone 28): full workflow through the API.

1. Load sample historical data. 2. Calculate indicators. 3. Detect setup.
4. Run agents. 5. Generate decision. 6. RiskEngine. 7. Paper execute.
8. Close trade. 9. Journal. 10. Performance. 11-12. Verify via API.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(test_db, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("NEWS_RSS_URLS", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from main import app

    # avoid re-seeding on startup event firing during TestClient context
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    get_settings.cache_clear()


def test_health_and_status(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["paper_trading"] is True
    assert body["live_trading"] is False

    r2 = client.get("/status")
    assert r2.status_code == 200
    assert "providers" in r2.json()

    r3 = client.get("/health/detailed")
    assert r3.status_code == 200
    b3 = r3.json()
    assert b3["status"] in {"HEALTHY", "DEGRADED", "DOWN"}
    assert "backend" in b3["components"]
    assert "database" in b3["components"]
    assert "risk_engine" in b3["components"]

    r4 = client.get("/risk")
    assert r4.status_code == 200
    b4 = r4.json()
    assert b4["status"] == "active"
    assert "limits" in b4


def test_loopback_cors_allows_fallback_dashboard_port(client):
    response = client.get("/health", headers={"Origin": "http://127.0.0.1:3001"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3001"


def test_config_endpoint_no_secrets(client):
    r = client.get("/config")
    assert r.status_code == 200
    text = str(r.json())
    assert "OPENROUTER_API_KEY" not in text
    assert "sk-" not in text


def test_market_endpoint_after_seed(client):
    r = client.get("/market/US100?timeframe=15m&limit=100")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] > 0
    assert body["candles"][0]["timestamp"]
    assert body["is_sample"] is True
    assert "SAMPLE DATA" in body["source"]


def test_full_pipeline_cycle(client):
    """agents -> decision -> risk -> (execution if actionable) -> journal."""
    r = client.post("/cycle")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"ok", "WAIT"}
    assert "decision" in body
    assert body["decision"]["action"] in {"BUY", "SELL", "WAIT", "REJECT"}
    # agents section present even when waiting
    assert "agents" in body
    assert body["broker_stops_checked"] is True


def test_live_collector_runs_signal_only_for_completed_bars(monkeypatch):
    import main as app_main
    import app.data.providers as providers

    current = datetime(2026, 9, 29, 12, 31, tzinfo=timezone.utc)
    candles = [
        {"timestamp": datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)},
        {"timestamp": datetime(2026, 9, 29, 12, 15, tzinfo=timezone.utc)},
        {"timestamp": datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc)},
    ]
    persisted = []
    cycles = []
    monkeypatch.setattr(app_main, "settings", SimpleNamespace(
        default_symbol="XAUUSD", default_timeframe="15m",
    ))
    monkeypatch.setattr(app_main, "utcnow", lambda: current)
    monkeypatch.setattr(providers, "get_provider", lambda name: SimpleNamespace(
        get_ohlcv=lambda symbol, timeframe, limit: candles,
    ))
    monkeypatch.setattr(app_main.pipeline, "persist_candles", lambda rows: persisted.extend(rows) or len(rows))
    monkeypatch.setattr(app_main.pipeline, "run_cycle", lambda symbol, timeframe, execute: (
        cycles.append((symbol, timeframe, execute)) or {"decision": {"action": "BUY"}}
    ))

    assert app_main._collect_completed_bars() == 2
    assert [bar["timestamp"] for bar in persisted] == [bar["timestamp"] for bar in candles[:2]]
    assert cycles == [("XAUUSD", "15m", False)]


def test_agents_endpoint(client):
    r = client.get("/agents")
    assert r.status_code == 200
    body = r.json()
    if body["status"] == "ok":
        assert set(body["agents"]) == {"technical", "quant", "regime", "news", "sentiment", "strategy"}


def test_paper_order_and_close(client):
    r = client.post("/paper/orders", json={"symbol": "US100", "side": "BUY", "quantity": 5})
    assert r.status_code == 200
    order = r.json()["order"]
    assert order["status"] == "filled"

    positions = client.get("/positions").json()
    assert positions["count"] >= 1
    pos_id = positions["positions"][0]["id"]

    closed = client.post(f"/paper/close/{pos_id}")
    assert closed.status_code == 200
    assert "pnl" in closed.json()


def test_trade_journal_records_audit(client):
    client.post("/paper/orders", json={"symbol": "US100", "side": "SELL", "quantity": 3})
    positions = client.get("/positions").json()["positions"]
    pos_id = positions[0]["id"]
    client.post(f"/paper/close/{pos_id}")

    trades = client.get("/trades?status=closed").json()["trades"]
    assert trades
    t = trades[0]
    assert t["symbol"] == "US100"
    assert t["result"] in {"win", "loss", "breakeven"}


def test_backtest_via_api(client):
    r = client.post("/backtests/run", json={
        "symbol": "US100", "timeframe": "15m",
        "params": {"max_atr_pct": 0.05, "starting_equity": 10000},
    })
    assert r.status_code == 200
    body = r.json()
    assert "run_id" in body
    run = body["run"]
    assert run["trade_count"] == len(body["trades"])
    assert run["ending_equity"] > 0

    listed = client.get("/backtests").json()
    assert listed["count"] >= 1
    detail = client.get(f"/backtests/{body['run_id']}")
    assert detail.status_code == 200
    assert "equity_curve" in detail.json()["run"]


def test_performance_endpoint(client):
    client.post("/paper/orders", json={"symbol": "US100", "side": "BUY", "quantity": 2})
    positions = client.get("/positions").json()["positions"]
    client.post(f"/paper/close/{positions[0]['id']}")
    r = client.get("/performance")
    assert r.status_code == 200
    body = r.json()
    assert "trading" in body and "agents" in body
    assert body["trading"]["total_trades"] >= 1


def test_paper_reset(client):
    client.post("/paper/orders", json={"symbol": "US100", "side": "BUY", "quantity": 1})
    r = client.post("/paper/reset?confirm=true")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.get("/positions").json()["count"] == 0


def test_invalid_requests_rejected(client):
    assert client.get("/market/US100?timeframe=bananas").status_code == 400
    assert client.post("/paper/orders", json={"symbol": "US100", "side": "UP", "quantity": 5}).status_code == 422
    assert client.post("/paper/reset").status_code == 400
    assert client.get("/backtests/999999").status_code == 404


def test_decisions_and_signals_persist(client):
    client.post("/cycle")
    r = client.get("/decisions")
    assert r.status_code == 200
    decisions = r.json()["decisions"]
    assert decisions  # cycle stored at least one decision
    d = decisions[0]
    assert d["action"] in {"BUY", "SELL", "WAIT", "REJECT"}
    sigs = client.get("/signals").json()["signals"]
    assert sigs  # agent signals stored


def test_e2e_full_workflow_sl_tp_preserved(client, test_db, long_setup_candles):
    """Deterministic E2E: historical candle → strategy → signal → entry/SL/TP
    → risk engine → paper broker → stored position → exit → P&L → journal → dashboard.

    Verifies actual values at every stage. Uses engineered setup for deterministic results.
    """
    from app.analysis.strategy import evaluate_setup, DEFAULT_PARAMS
    from app.decision.engine import DecisionEngine
    from app.risk.engine import risk_engine
    from app.execution.broker import PaperBroker
    from app.services.journal import journal_service
    from unittest.mock import patch
    candles = long_setup_candles
    sig = evaluate_setup(candles, {"rr_target": 2.5}, "XAUUSD", "15m")
    assert sig.setup_valid is True, f"Strategy setup invalid: {sig.reasons}"
    assert sig.entry is not None and sig.stop_loss is not None and sig.take_profit is not None
    assert sig.direction in {"LONG", "SHORT"}
    print(f"1. Strategy: direction={sig.direction}, entry={sig.entry}, SL={sig.stop_loss}, TP={sig.take_profit}")

    # 2. Strategy → signal (already in sig)
    # 3. Signal → decision
    from app.decision.engine import DecisionEngine
    from app.decision.engine import decision_engine
    db = test_db()
    decision = decision_engine.decide(
        {"strategy": {"candidate_trade": True, "direction": sig.direction, "confidence": 0.8,
                       "entry": sig.entry, "stop": sig.stop_loss, "target": sig.take_profit,
                       "reasons": sig.reasons, "conflicts": []},
         "technical": {"direction": sig.direction, "confidence": 0.7},
         "quant": {"direction": sig.direction, "confidence": 0.6}},
        "XAUUSD", "15m",
    )
    assert decision.action in {"BUY", "SELL"}, f"Expected BUY/SELL, got {decision.action}"
    assert decision.stop_loss == sig.stop_loss
    assert decision.take_profit == sig.take_profit
    print(f"2. Decision: action={decision.action}, SL={decision.stop_loss}, TP={decision.take_profit}")

    # 4. Decision → risk engine
    from app.risk.engine import risk_engine
    from app.utils.timeutils import utcnow
    from app.models.database import Trade, MarketData
    risk_out = risk_engine.evaluate(
        db, {"action": decision.action, "symbol": "XAUUSD", "entry": decision.entry,
             "stop_loss": decision.stop_loss, "take_profit": decision.take_profit},
        account_equity=10000.0, current_price=decision.entry,
    )
    assert risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}, f"Risk rejected: {risk_out.violations}"
    print(f"3. Risk engine: verdict={risk_out.verdict}, qty={risk_out.quantity}")

    # Seed market data so broker can fill
    db.add(MarketData(
        symbol="XAUUSD", timeframe="15m", timestamp=utcnow(),
        open=decision.entry, high=decision.entry + 1, low=decision.entry - 1,
        close=decision.entry, volume=1000, source="test",
    ))
    db.commit()

    # 5. Risk engine → paper broker
    from app.execution.broker import PaperBroker
    broker = PaperBroker(db, starting_balance=10000.0)
    side = "BUY" if decision.action == "BUY" else "SELL"
    direction = "LONG" if decision.action == "BUY" else "SHORT"
    exec_result = broker.place_order(
        "XAUUSD", side, risk_out.quantity or 0.0, "market",
        stop_price=decision.stop_loss, take_profit=decision.take_profit,
    )
    assert exec_result["ok"], f"Broker order failed: {exec_result.get('error')}"
    print(f"4. Broker order: ok={exec_result['ok']}, position SL={broker.get_positions()[0]['stop_loss']}, "
          f"TP={broker.get_positions()[0]['take_profit']}")

    # 6. Stored position → journal → dashboard
    # Verify position has SL/TP
    pos = broker.get_positions()[0]
    assert pos["stop_loss"] == decision.stop_loss
    assert pos["take_profit"] == decision.take_profit
    assert pos["direction"] == direction
    assert pos["quantity"] > 0
    print(f"5. Stored position: dir={pos['direction']}, qty={pos['quantity']}, SL={pos['stop_loss']}, TP={pos['take_profit']}")

    # 7. Close position and verify P&L
    # For LONG: move price above TP
    target_price = pos["take_profit"] + 1.0 if pos["direction"] == "LONG" else pos["stop_loss"] - 1.0
    close_result = broker.close_position(pos["id"], reason="tp_hit_e2e", exit_price=target_price)
    assert close_result["ok"]
    trade = db.query(Trade).filter(Trade.status == "closed").first()
    assert trade is not None
    assert trade.stop_loss == decision.stop_loss
    assert trade.take_profit == decision.take_profit
    assert trade.pnl is not None
    r_multiple = trade.r_multiple
    print(f"6. Exit: reason={trade.result}, P&L={trade.pnl}, r_multiple={r_multiple}")

    # 8. Dashboard displays SL/TP
    from fastapi.testclient import TestClient
    pos_api = client.get("/positions")
    assert pos_api.status_code == 200
    positions = pos_api.json()["positions"]
    assert len(positions) == 0  # closed
    # Check last trade has SL/TP
    trades_api = client.get("/trades?status=closed")
    assert trades_api.status_code == 200
    closed_trades = trades_api.json()["trades"]
    assert len(closed_trades) >= 1
    t = closed_trades[0]
    assert t["stop_loss"] == decision.stop_loss, f"Dashboard SL: {t['stop_loss']} != {decision.stop_loss}"
    assert t["take_profit"] == decision.take_profit, f"Dashboard TP: {t['take_profit']} != {decision.take_profit}"
    print(f"7. Dashboard: trade SL={t['stop_loss']}, TP={t['take_profit']} match expected")

    # 9. Journal records
    from app.models.database import TradingDecision
    all_decisions = db.query(TradingDecision).all()
    # The decision was recorded; verify it has SL/TP
    if all_decisions:
        dc = all_decisions[0]
        assert dc.stop_loss == decision.stop_loss, f"Journal decision SL: {dc.stop_loss} != {decision.stop_loss}"
        assert dc.take_profit == decision.take_profit, f"Journal decision TP: {dc.take_profit} != {decision.take_profit}"
        print(f"8. Journal: decision SL={dc.stop_loss}, TP={dc.take_profit}")
    else:
        print("8. Journal: no decisions recorded (pipeline not called)")

    print("\n=== E2E workflow: ALL CHECKS PASSED ===")
