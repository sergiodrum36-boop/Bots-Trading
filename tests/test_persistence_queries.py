"""Tests de persistence/queries.py: las consultas de solo lectura que usa
el panel visual. SQLite real sobre un archivo temporal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from persistence.database import make_engine, make_session_factory
from persistence.queries import (
    list_equity_curve,
    list_errors,
    list_open_trades,
    list_signals,
    list_trades,
    summarize_closed_trades,
)
from persistence.store import SqlTradeStore
from strategy.signals import IndicatorSnapshot, Signal, SignalAction

FIXED_NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "test.sqlite")
    return make_session_factory(engine)


@pytest.fixture
def store(session_factory) -> SqlTradeStore:
    return SqlTradeStore(session_factory)


def _signal(symbol="SPY", action=SignalAction.BUY, ts=FIXED_NOW) -> Signal:
    return Signal(
        symbol=symbol,
        timestamp=ts,
        action=action,
        price=100.0,
        indicators=IndicatorSnapshot(ema_fast=101.0, ema_slow=99.0, rsi=55.0, macd_histogram=0.3),
        reason="motivo de prueba",
    )


class TestListTrades:
    def test_empty_when_no_trades(self, session_factory):
        assert list_trades(session_factory) == []

    def test_returns_most_recent_first(self, store, session_factory):
        store.open_trade(
            symbol="SPY", side="buy", qty=1.0, entry_price=100.0,
            entry_time=FIXED_NOW - timedelta(days=2), entry_order_id="o1",
            stop_loss_price=98.0, reason="r1",
        )
        store.open_trade(
            symbol="QQQ", side="buy", qty=1.0, entry_price=200.0,
            entry_time=FIXED_NOW, entry_order_id="o2",
            stop_loss_price=196.0, reason="r2",
        )

        rows = list_trades(session_factory)

        assert [r.symbol for r in rows] == ["QQQ", "SPY"]

    def test_respects_limit(self, store, session_factory):
        for i in range(5):
            store.open_trade(
                symbol="SPY", side="buy", qty=1.0, entry_price=100.0,
                entry_time=FIXED_NOW - timedelta(days=i), entry_order_id=f"o{i}",
                stop_loss_price=98.0, reason="r",
            )

        assert len(list_trades(session_factory, limit=2)) == 2


class TestListOpenTrades:
    def test_excludes_closed_trades(self, store, session_factory):
        open_id = store.open_trade(
            symbol="SPY", side="buy", qty=1.0, entry_price=100.0,
            entry_time=FIXED_NOW, entry_order_id="o1", stop_loss_price=98.0, reason="r",
        )
        closed_id = store.open_trade(
            symbol="QQQ", side="buy", qty=1.0, entry_price=200.0,
            entry_time=FIXED_NOW, entry_order_id="o2", stop_loss_price=196.0, reason="r",
        )
        store.close_trade(
            closed_id, exit_price=205.0, exit_time=FIXED_NOW, exit_order_id="o3",
            reason="r", pnl_pct_of_capital=0.01,
        )

        rows = list_open_trades(session_factory)

        assert len(rows) == 1
        assert rows[0].id == open_id


class TestListSignals:
    def test_filters_by_symbol(self, store, session_factory):
        store.record_signal(_signal(symbol="SPY"), executed=True)
        store.record_signal(_signal(symbol="QQQ"), executed=False)

        rows = list_signals(session_factory, symbol="SPY")

        assert len(rows) == 1
        assert rows[0].symbol == "SPY"

    def test_without_filter_returns_all_most_recent_first(self, store, session_factory):
        store.record_signal(_signal(symbol="SPY", ts=FIXED_NOW - timedelta(days=1)), executed=True)
        store.record_signal(_signal(symbol="QQQ", ts=FIXED_NOW), executed=False)

        rows = list_signals(session_factory)

        assert [r.symbol for r in rows] == ["QQQ", "SPY"]


class TestListEquityCurve:
    def test_returns_chronological_ascending_order(self, store, session_factory):
        store.record_equity_snapshot(
            equity=100.0, cash=100.0, buying_power=100.0, timestamp=FIXED_NOW
        )
        store.record_equity_snapshot(
            equity=105.0, cash=105.0, buying_power=105.0, timestamp=FIXED_NOW - timedelta(days=1)
        )

        rows = list_equity_curve(session_factory)

        assert [r.equity for r in rows] == [105.0, 100.0]


class TestListErrors:
    def test_returns_recorded_errors(self, store, session_factory):
        store.record_error(message="fallo", context="conexion", symbol=None, timestamp=FIXED_NOW)

        rows = list_errors(session_factory)

        assert len(rows) == 1
        assert rows[0].message == "fallo"


class TestSummarizeClosedTrades:
    def test_empty_when_no_closed_trades(self, session_factory):
        summary = summarize_closed_trades(session_factory)
        assert summary == {"n_trades": 0, "win_rate": None, "total_pnl_pct": 0.0}

    def test_ignores_open_trades(self, store, session_factory):
        store.open_trade(
            symbol="SPY", side="buy", qty=1.0, entry_price=100.0,
            entry_time=FIXED_NOW, entry_order_id="o1", stop_loss_price=98.0, reason="r",
        )

        summary = summarize_closed_trades(session_factory)

        assert summary["n_trades"] == 0

    def test_computes_win_rate_and_total_pnl(self, store, session_factory):
        win_id = store.open_trade(
            symbol="SPY", side="buy", qty=1.0, entry_price=100.0,
            entry_time=FIXED_NOW, entry_order_id="o1", stop_loss_price=98.0, reason="r",
        )
        store.close_trade(
            win_id, exit_price=110.0, exit_time=FIXED_NOW, exit_order_id="o2",
            reason="r", pnl_pct_of_capital=0.01,
        )
        loss_id = store.open_trade(
            symbol="QQQ", side="buy", qty=1.0, entry_price=200.0,
            entry_time=FIXED_NOW, entry_order_id="o3", stop_loss_price=196.0, reason="r",
        )
        store.close_trade(
            loss_id, exit_price=195.0, exit_time=FIXED_NOW, exit_order_id="o4",
            reason="r", pnl_pct_of_capital=-0.005,
        )

        summary = summarize_closed_trades(session_factory)

        assert summary["n_trades"] == 2
        assert summary["win_rate"] == pytest.approx(0.5)
        assert summary["total_pnl_pct"] == pytest.approx(0.005)
