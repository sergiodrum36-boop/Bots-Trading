"""Tests de persistence/: database.py (engine/sesion) y store.py
(TradeStore). Usa SQLite real sobre un archivo temporal (tmp_path) -- es
suficientemente rapido y evita depender de mocks para verificar que el
esquema y las consultas funcionan de verdad."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from persistence.database import make_engine, make_session_factory
from persistence.store import NullTradeStore, SqlTradeStore
from strategy.signals import IndicatorSnapshot, Signal, SignalAction

FIXED_NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


def _signal(action=SignalAction.BUY, reason="motivo de prueba") -> Signal:
    return Signal(
        symbol="SPY",
        timestamp=FIXED_NOW,
        action=action,
        price=100.0,
        indicators=IndicatorSnapshot(ema_fast=101.0, ema_slow=99.0, rsi=55.0, macd_histogram=0.3),
        reason=reason,
    )


@pytest.fixture
def store(tmp_path) -> SqlTradeStore:
    engine = make_engine(tmp_path / "test.sqlite")
    session_factory = make_session_factory(engine)
    return SqlTradeStore(session_factory)


class TestSignals:
    def test_record_signal_persists_all_fields(self, store, tmp_path):
        signal = _signal(action=SignalAction.BUY, reason="cruce alcista")

        store.record_signal(signal, executed=True)

        rows = _all_signal_rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row.symbol == "SPY"
        assert row.action == "BUY"
        assert row.price == 100.0
        assert row.rsi == 55.0
        assert row.reason == "cruce alcista"
        assert row.executed is True

    def test_nan_indicators_are_stored_as_null(self, store):
        signal = _signal()
        signal = Signal(
            symbol=signal.symbol,
            timestamp=signal.timestamp,
            action=signal.action,
            price=signal.price,
            indicators=IndicatorSnapshot(
                ema_fast=float("nan"), ema_slow=float("nan"), rsi=float("nan"), macd_histogram=float("nan")
            ),
            reason="periodo de calentamiento",
        )

        store.record_signal(signal, executed=False)

        rows = _all_signal_rows(store)
        assert rows[0].ema_fast is None
        assert rows[0].rsi is None


class TestEquitySnapshots:
    def test_record_equity_snapshot_persists(self, store):
        store.record_equity_snapshot(equity=100_000.0, cash=50_000.0, buying_power=100_000.0, timestamp=FIXED_NOW)

        rows = _all_equity_rows(store)
        assert len(rows) == 1
        assert rows[0].equity == 100_000.0
        assert rows[0].cash == 50_000.0


class TestTrades:
    def test_open_trade_returns_id_and_get_open_trade_id_finds_it(self, store):
        trade_id = store.open_trade(
            symbol="SPY",
            side="buy",
            qty=10.0,
            entry_price=100.0,
            entry_time=FIXED_NOW,
            entry_order_id="order-1",
            stop_loss_price=98.0,
            reason="cruce alcista",
        )

        assert trade_id is not None
        assert store.get_open_trade_id("SPY") == trade_id

    def test_get_open_trade_id_returns_none_when_no_open_trade(self, store):
        assert store.get_open_trade_id("SPY") is None

    def test_get_open_trade_id_is_scoped_by_symbol(self, store):
        store.open_trade(
            symbol="SPY", side="buy", qty=10.0, entry_price=100.0, entry_time=FIXED_NOW,
            entry_order_id="o1", stop_loss_price=98.0, reason="r",
        )

        assert store.get_open_trade_id("QQQ") is None

    def test_close_trade_updates_record(self, store):
        trade_id = store.open_trade(
            symbol="SPY", side="buy", qty=10.0, entry_price=100.0, entry_time=FIXED_NOW,
            entry_order_id="order-1", stop_loss_price=98.0, reason="cruce alcista",
        )

        store.close_trade(
            trade_id,
            exit_price=105.0,
            exit_time=FIXED_NOW,
            exit_order_id="order-2",
            reason="cruce bajista",
            pnl_pct_of_capital=0.01,
        )

        rows = _all_trade_rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "closed"
        assert row.exit_price == 105.0
        assert row.pnl_pct_of_capital == 0.01
        # Ya no debe contar como abierta.
        assert store.get_open_trade_id("SPY") is None

    def test_close_trade_with_unknown_id_does_not_raise(self, store):
        store.close_trade(
            9999, exit_price=1.0, exit_time=FIXED_NOW, exit_order_id="x", reason="r", pnl_pct_of_capital=0.0
        )
        # No debe lanzar excepcion; simplemente no hay nada que actualizar.

    def test_only_the_most_recent_open_trade_is_returned(self, store):
        first_id = store.open_trade(
            symbol="SPY", side="buy", qty=10.0, entry_price=100.0,
            entry_time=datetime(2026, 8, 10, tzinfo=timezone.utc),
            entry_order_id="o1", stop_loss_price=98.0, reason="r1",
        )
        store.close_trade(
            first_id, exit_price=101.0, exit_time=datetime(2026, 8, 11, tzinfo=timezone.utc),
            exit_order_id="o2", reason="r", pnl_pct_of_capital=0.001,
        )
        second_id = store.open_trade(
            symbol="SPY", side="buy", qty=5.0, entry_price=102.0,
            entry_time=datetime(2026, 8, 12, tzinfo=timezone.utc),
            entry_order_id="o3", stop_loss_price=99.0, reason="r2",
        )

        assert store.get_open_trade_id("SPY") == second_id
        assert second_id != first_id


class TestErrors:
    def test_record_error_persists(self, store):
        store.record_error(message="fallo de red", context="conexion", symbol=None, timestamp=FIXED_NOW)

        rows = _all_error_rows(store)
        assert len(rows) == 1
        assert rows[0].message == "fallo de red"
        assert rows[0].symbol is None


class TestNullTradeStore:
    def test_all_methods_are_safe_no_ops(self):
        store = NullTradeStore()

        store.record_signal(_signal(), executed=True)
        store.record_equity_snapshot(equity=1.0, cash=1.0, buying_power=1.0, timestamp=FIXED_NOW)
        assert store.open_trade(
            symbol="SPY", side="buy", qty=1.0, entry_price=1.0, entry_time=FIXED_NOW,
            entry_order_id="o", stop_loss_price=1.0, reason="r",
        ) is None
        assert store.get_open_trade_id("SPY") is None
        store.close_trade(1, exit_price=1.0, exit_time=FIXED_NOW, exit_order_id="o", reason="r", pnl_pct_of_capital=None)
        store.record_error(message="m", context="c", symbol=None, timestamp=FIXED_NOW)
        # Ninguna llamada anterior debe lanzar excepcion.


# ----------------------------------------------------------------------
# Helpers de lectura directa (para no depender solo de los metodos del
# propio store al verificar lo que quedo escrito en disco)
# ----------------------------------------------------------------------
def _all_signal_rows(store: SqlTradeStore):
    from persistence.database import session_scope
    from persistence.models import SignalRecord

    with session_scope(store._session_factory) as session:
        return session.query(SignalRecord).all()


def _all_equity_rows(store: SqlTradeStore):
    from persistence.database import session_scope
    from persistence.models import EquitySnapshotRecord

    with session_scope(store._session_factory) as session:
        return session.query(EquitySnapshotRecord).all()


def _all_trade_rows(store: SqlTradeStore):
    from persistence.database import session_scope
    from persistence.models import TradeRecord

    with session_scope(store._session_factory) as session:
        return session.query(TradeRecord).all()


def _all_error_rows(store: SqlTradeStore):
    from persistence.database import session_scope
    from persistence.models import ErrorLogRecord

    with session_scope(store._session_factory) as session:
        return session.query(ErrorLogRecord).all()
