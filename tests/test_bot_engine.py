"""Tests unitarios de core/bot_engine.py (el orquestador).

No se prueba aqui la logica de calculo de senales/indicadores (ya cubierta
en test_trend_strategy.py e test_indicators.py) ni la logica interna de
OrderManager (test_order_manager.py) o KillSwitch (test_kill_switch.py) --
se usa un `FakeStrategy` controlable para aislar la orquestacion: el gate
del kill switch, el sizing de la orden, el manejo de perdida de conexion,
el aislamiento de errores por simbolo y el registro de P&L al cerrar.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from connectors.base_connector import (
    Account,
    BrokerConnector,
    ConnectorError,
    MarketSession,
    OHLCVBar,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    Timeframe,
)
from core.bot_engine import BotEngine, BotEngineConfig
from execution.order_manager import OrderManager, make_client_order_id
from persistence.store import TradeStore
from risk.kill_switch import KillSwitch, KillSwitchConfig
from risk.stop_loss import StopLossConfig
from strategy.signals import IndicatorSnapshot, Signal, SignalAction


# ----------------------------------------------------------------------
# Dobles de pruebas
# ----------------------------------------------------------------------
class FakeStrategy:
    """Doble de TrendStrategy: devuelve senales preconfiguradas por simbolo
    en vez de calcularlas de verdad, para aislar la orquestacion de la
    logica de indicadores."""

    def __init__(self, signals_by_symbol=None, error_symbols=None):
        self._signals_by_symbol = signals_by_symbol or {}
        self._error_symbols = error_symbols or set()
        self.calls: list[str] = []

    def generate_signals(self, symbol, df):
        self.calls.append(symbol)
        if symbol in self._error_symbols:
            raise RuntimeError(f"fallo simulado generando senales para {symbol}")
        return self._signals_by_symbol.get(symbol, [])


class FakeEngineConnector(BrokerConnector):
    """Doble de BrokerConnector centrado en lo que usa BotEngine: estado de
    mercado, cuenta/posiciones, negociabilidad, velas recientes y envio/
    consulta de ordenes. Sin llamadas reales."""

    def __init__(self):
        self.market_open = True
        self.market_open_error: Exception | None = None
        self.account = Account(equity=100_000.0, cash=50_000.0, buying_power=100_000.0)
        self.account_error: Exception | None = None
        self.positions: dict[str, Position] = {}
        self.tradable: dict[str, bool] = {}
        self.bars_by_symbol: dict[str, list[OHLCVBar]] = {}
        self.submitted: list[dict] = []
        self._get_order_responses: list = []
        self._submit_result: OrderResult | None = None
        self._submit_error: Exception | None = None

    def queue_get_order_response(self, response) -> None:
        self._get_order_responses.append(response)

    def set_submit_result(self, result: OrderResult) -> None:
        self._submit_result = result

    def is_market_open(self) -> bool:
        if self.market_open_error is not None:
            raise self.market_open_error
        return self.market_open

    def get_account(self) -> Account:
        if self.account_error is not None:
            raise self.account_error
        return self.account

    def get_open_positions(self) -> list[Position]:
        return list(self.positions.values())

    def is_symbol_tradable(self, symbol: str) -> bool:
        return self.tradable.get(symbol, True)

    def get_recent_bars(self, symbol, timeframe=Timeframe.DAY, limit=100):
        return list(self.bars_by_symbol.get(symbol, []))

    def submit_market_order(self, symbol, qty, side, client_order_id) -> OrderResult:
        self.submitted.append(
            {"symbol": symbol, "qty": qty, "side": side, "client_order_id": client_order_id}
        )
        if self._submit_error is not None:
            raise self._submit_error
        assert self._submit_result is not None, "test no configuro submit_result"
        return self._submit_result

    def get_order_by_client_id(self, client_order_id):
        if not self._get_order_responses:
            return None
        response = self._get_order_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    # --- resto de la interfaz: no se usa en estos tests ---
    def get_historical_bars(self, symbols, timeframe, start, end=None, limit=None):
        raise NotImplementedError

    def get_latest_bar(self, symbol):
        raise NotImplementedError

    def get_market_calendar(self, start: date, end: date) -> list[MarketSession]:
        raise NotImplementedError


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
FIXED_NOW = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


def _bar(symbol="SPY", price=100.0) -> OHLCVBar:
    return OHLCVBar(
        symbol=symbol,
        timestamp=FIXED_NOW,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1_000.0,
    )


def _signal(symbol, action, price=100.0, reason="motivo de prueba") -> Signal:
    return Signal(
        symbol=symbol,
        timestamp=FIXED_NOW,
        action=action,
        price=price,
        indicators=IndicatorSnapshot(ema_fast=1.0, ema_slow=1.0, rsi=50.0, macd_histogram=0.1),
        reason=reason,
    )


def _order(
    status=OrderStatus.FILLED,
    symbol="SPY",
    side=OrderSide.BUY,
    qty=10.0,
    filled_qty=None,
    filled_avg_price=95.0,
) -> OrderResult:
    return OrderResult(
        broker_order_id="broker-1",
        client_order_id="bot-test",
        symbol=symbol,
        side=side,
        qty=qty,
        status=status,
        filled_qty=qty if filled_qty is None else filled_qty,
        filled_avg_price=filled_avg_price,
        submitted_at=FIXED_NOW,
    )


@pytest.fixture
def connector() -> FakeEngineConnector:
    return FakeEngineConnector()


@pytest.fixture
def kill_switch(tmp_path) -> KillSwitch:
    return KillSwitch(
        config=KillSwitchConfig(),
        state_file=tmp_path / "kill_switch_state.json",
        clock=lambda: FIXED_NOW,
    )


class FakeTradeStore(TradeStore):
    """Doble de pruebas de TradeStore: guarda todo en memoria en listas
    simples para poder inspeccionar qué llamó BotEngine, sin tocar disco."""

    def __init__(self):
        self.signals: list[tuple[Signal, bool]] = []
        self.equity_snapshots: list[dict] = []
        self.open_trades: list[dict] = []
        self.closed_trades: list[tuple[int, dict]] = []
        self.errors: list[dict] = []
        self._next_id = 1
        self._open_trade_id_by_symbol: dict[str, int] = {}

    def record_signal(self, signal, executed):
        self.signals.append((signal, executed))

    def record_equity_snapshot(self, equity, cash, buying_power, timestamp):
        self.equity_snapshots.append(
            {"equity": equity, "cash": cash, "buying_power": buying_power, "timestamp": timestamp}
        )

    def open_trade(self, **kwargs):
        trade_id = self._next_id
        self._next_id += 1
        self.open_trades.append({"id": trade_id, **kwargs})
        self._open_trade_id_by_symbol[kwargs["symbol"]] = trade_id
        return trade_id

    def get_open_trade_id(self, symbol):
        return self._open_trade_id_by_symbol.get(symbol)

    def close_trade(self, trade_id, **kwargs):
        self.closed_trades.append((trade_id, kwargs))
        for symbol, tid in list(self._open_trade_id_by_symbol.items()):
            if tid == trade_id:
                del self._open_trade_id_by_symbol[symbol]

    def record_error(self, **kwargs):
        self.errors.append(kwargs)


def _make_engine(connector, kill_switch, strategy, symbols=("SPY",), store=None) -> BotEngine:
    order_manager = OrderManager(
        connector=connector, kill_switch=kill_switch, fill_poll_attempts=2, fill_poll_wait_seconds=0
    )
    config = BotEngineConfig(symbols=symbols, stop_loss_config=StopLossConfig())
    return BotEngine(
        connector=connector,
        strategy=strategy,
        kill_switch=kill_switch,
        order_manager=order_manager,
        config=config,
        clock=lambda: FIXED_NOW,
        store=store,
    )


# ----------------------------------------------------------------------
# Validacion de configuracion
# ----------------------------------------------------------------------
class TestBotEngineConfig:
    def test_rejects_empty_symbols(self):
        with pytest.raises(ValueError):
            BotEngineConfig(symbols=())

    def test_rejects_non_positive_bars_limit(self):
        with pytest.raises(ValueError):
            BotEngineConfig(symbols=("SPY",), bars_limit=0)

    def test_rejects_out_of_range_risk_pct(self):
        with pytest.raises(ValueError):
            BotEngineConfig(symbols=("SPY",), risk_pct=0.0)

    def test_rejects_out_of_range_max_position_pct(self):
        with pytest.raises(ValueError):
            BotEngineConfig(symbols=("SPY",), max_position_pct=1.5)


# ----------------------------------------------------------------------
# Gates de nivel de ciclo: mercado cerrado / kill switch / conexion
# ----------------------------------------------------------------------
class TestCycleGates:
    def test_market_closed_skips_every_symbol(self, connector, kill_switch):
        connector.market_open = False
        strategy = FakeStrategy()
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.market_open is False
        assert result.aborted is False
        assert result.symbol_results == ()
        assert strategy.calls == []

    def test_kill_switch_active_skips_cycle(self, connector, kill_switch):
        # Umbral de perdida diaria muy bajo para dispararlo con una sola llamada.
        kill_switch.config = KillSwitchConfig(max_daily_loss_pct=0.01)
        kill_switch.record_trade_result(-0.02)
        assert kill_switch.is_active() is True

        strategy = FakeStrategy()
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.kill_switch_active is True
        assert result.symbol_results == ()
        assert strategy.calls == []

    def test_market_status_connection_failure_aborts_and_marks_kill_switch(
        self, connector, kill_switch
    ):
        connector.market_open_error = ConnectorError("red caida")
        strategy = FakeStrategy()
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.aborted is True
        assert "red caida" in result.abort_reason
        assert kill_switch.state.connection_lost_at is not None
        assert strategy.calls == []

    def test_account_fetch_failure_aborts_and_marks_kill_switch(self, connector, kill_switch):
        connector.account_error = ConnectorError("timeout")
        strategy = FakeStrategy()
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.aborted is True
        assert kill_switch.state.connection_lost_at is not None
        assert strategy.calls == []

    def test_successful_cycle_clears_previously_marked_connection_loss(
        self, connector, kill_switch
    ):
        kill_switch.record_connection_lost()
        assert kill_switch.state.connection_lost_at is not None

        strategy = FakeStrategy()  # sin senales para SPY -> HOLD
        engine = _make_engine(connector, kill_switch, strategy)

        engine.run_cycle()

        assert kill_switch.state.connection_lost_at is None


# ----------------------------------------------------------------------
# Simbolo individual: no negociable / sin datos / HOLD
# ----------------------------------------------------------------------
class TestSymbolPreconditions:
    def test_non_tradable_symbol_is_skipped_before_calling_strategy(self, connector, kill_switch):
        connector.tradable["SPY"] = False
        strategy = FakeStrategy()
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.symbol_results[0].action == SignalAction.HOLD
        assert result.symbol_results[0].executed is False
        assert "no negociable" in result.symbol_results[0].reason
        assert strategy.calls == []

    def test_no_bars_available_is_hold_without_calling_strategy(self, connector, kill_switch):
        strategy = FakeStrategy()  # connector.bars_by_symbol vacio para SPY
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.symbol_results[0].reason == "sin velas disponibles"
        assert strategy.calls == []

    def test_hold_signal_does_not_execute_anything(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY")]
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.HOLD, reason="sin condiciones")]})
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.symbol_results[0].executed is False
        assert result.symbol_results[0].action == SignalAction.HOLD
        assert connector.submitted == []


# ----------------------------------------------------------------------
# Senal de compra
# ----------------------------------------------------------------------
class TestBuySignal:
    def test_executes_market_buy_sized_by_risk(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY", price=100.0)]
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.BUY, price=100.0)]})
        connector.queue_get_order_response(None)  # sin orden previa
        connector.set_submit_result(_order(status=OrderStatus.FILLED, symbol="SPY", qty=200.0))

        engine = _make_engine(connector, kill_switch, strategy)
        result = engine.run_cycle()

        symbol_result = result.symbol_results[0]
        assert symbol_result.executed is True
        assert symbol_result.action == SignalAction.BUY
        assert symbol_result.order.status == OrderStatus.FILLED

        # stop 2% -> 98.0, distancia=2.0, riesgo 1% de 100000=1000 -> 500
        # acciones por riesgo, pero limitado al 20% del capital / 100 = 200.
        assert connector.submitted[0]["qty"] == 200.0
        assert connector.submitted[0]["side"] == OrderSide.BUY
        expected_key = make_client_order_id("SPY-2026-08-15-buy")
        assert connector.submitted[0]["client_order_id"] == expected_key

    def test_skips_buy_when_position_already_open(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY")]
        connector.positions["SPY"] = Position(
            symbol="SPY", qty=10.0, avg_entry_price=90.0, market_value=1000.0, unrealized_pl=100.0
        )
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.BUY)]})
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.symbol_results[0].executed is False
        assert "ya hay una posicion abierta" in result.symbol_results[0].reason
        assert connector.submitted == []

    def test_zero_sizing_skips_without_calling_connector_submit(self, connector, kill_switch):
        # Capital muy pequeno -> el presupuesto de riesgo no da ni para 1 accion.
        connector.account = Account(equity=1.0, cash=1.0, buying_power=1.0)
        connector.bars_by_symbol["SPY"] = [_bar("SPY", price=100.0)]
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.BUY, price=100.0)]})
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.symbol_results[0].executed is False
        assert "0 acciones" in result.symbol_results[0].reason
        assert connector.submitted == []


# ----------------------------------------------------------------------
# Senal de venta
# ----------------------------------------------------------------------
class TestSellSignal:
    def test_closes_position_and_records_pnl_in_kill_switch(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY", price=100.0)]
        connector.positions["SPY"] = Position(
            symbol="SPY", qty=50.0, avg_entry_price=90.0, market_value=5000.0, unrealized_pl=500.0
        )
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.SELL, price=100.0)]})
        connector.queue_get_order_response(None)
        connector.set_submit_result(
            _order(status=OrderStatus.FILLED, symbol="SPY", side=OrderSide.SELL, qty=50.0, filled_avg_price=95.0)
        )

        engine = _make_engine(connector, kill_switch, strategy)
        result = engine.run_cycle()

        symbol_result = result.symbol_results[0]
        assert symbol_result.executed is True
        assert symbol_result.action == SignalAction.SELL
        assert connector.submitted[0]["qty"] == 50.0
        assert connector.submitted[0]["side"] == OrderSide.SELL

        # pnl = (95 - 90) * 50 = 250; 250 / 100000 capital = 0.0025
        assert kill_switch.state.trades_today == 1
        assert kill_switch.state.daily_pnl_pct == pytest.approx(0.0025)

    def test_skips_sell_when_no_position_open(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY")]
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.SELL)]})
        engine = _make_engine(connector, kill_switch, strategy)

        result = engine.run_cycle()

        assert result.symbol_results[0].executed is False
        assert "no hay posicion abierta" in result.symbol_results[0].reason
        assert connector.submitted == []

    def test_non_filled_close_does_not_record_pnl(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY")]
        connector.positions["SPY"] = Position(
            symbol="SPY", qty=50.0, avg_entry_price=90.0, market_value=5000.0, unrealized_pl=500.0
        )
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.SELL)]})
        connector.queue_get_order_response(None)
        connector.set_submit_result(
            _order(status=OrderStatus.NEW, symbol="SPY", side=OrderSide.SELL, qty=50.0, filled_qty=0.0, filled_avg_price=None)
        )

        engine = _make_engine(connector, kill_switch, strategy)
        engine.run_cycle()

        assert kill_switch.state.trades_today == 0


# ----------------------------------------------------------------------
# Kill switch: bloquea apertura, nunca cierre
# ----------------------------------------------------------------------
class TestKillSwitchBlocksOpensOnly:
    def test_trade_limit_blocks_buy_but_not_sell(self, connector, kill_switch):
        kill_switch.config = KillSwitchConfig(max_trades_per_day=1)
        kill_switch.record_trade_result(0.001)  # trades_today=1, sin disparar (ganancia)
        assert kill_switch.can_open_new_position() is False
        assert kill_switch.is_active() is False  # bloqueo de limite, no "disparado"

        connector.bars_by_symbol["AAA"] = [_bar("AAA", price=100.0)]
        connector.bars_by_symbol["BBB"] = [_bar("BBB", price=100.0)]
        connector.positions["BBB"] = Position(
            symbol="BBB", qty=10.0, avg_entry_price=90.0, market_value=1000.0, unrealized_pl=100.0
        )
        strategy = FakeStrategy(
            {
                "AAA": [_signal("AAA", SignalAction.BUY, price=100.0)],
                "BBB": [_signal("BBB", SignalAction.SELL, price=100.0)],
            }
        )
        connector.queue_get_order_response(None)  # consumido por el cierre de BBB
        connector.set_submit_result(
            _order(status=OrderStatus.FILLED, symbol="BBB", side=OrderSide.SELL, qty=10.0)
        )

        engine = _make_engine(connector, kill_switch, strategy, symbols=("AAA", "BBB"))
        result = engine.run_cycle()

        by_symbol = {r.symbol: r for r in result.symbol_results}
        assert by_symbol["AAA"].executed is False
        assert "kill switch" in by_symbol["AAA"].reason.lower()
        assert by_symbol["BBB"].executed is True
        assert len(connector.submitted) == 1
        assert connector.submitted[0]["symbol"] == "BBB"


# ----------------------------------------------------------------------
# Aislamiento de errores por simbolo
# ----------------------------------------------------------------------
class TestSymbolIsolation:
    def test_error_in_one_symbol_does_not_abort_the_rest(self, connector, kill_switch):
        connector.bars_by_symbol["GOOD"] = [_bar("GOOD")]
        connector.bars_by_symbol["BAD"] = [_bar("BAD")]
        strategy = FakeStrategy(
            signals_by_symbol={"GOOD": []},
            error_symbols={"BAD"},
        )
        engine = _make_engine(connector, kill_switch, strategy, symbols=("BAD", "GOOD"))

        result = engine.run_cycle()

        by_symbol = {r.symbol: r for r in result.symbol_results}
        assert by_symbol["BAD"].error is not None
        assert by_symbol["BAD"].executed is False
        assert by_symbol["GOOD"].error is None


# ----------------------------------------------------------------------
# Integracion con persistence/ (TradeStore) -- BotEngine con store=None
# (el resto de tests de este archivo) sigue funcionando exactamente igual
# que antes: NullTradeStore no cambia ningun comportamiento observable.
# ----------------------------------------------------------------------
class TestPersistenceIntegration:
    def test_every_signal_is_recorded_even_when_not_executed(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY")]
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.HOLD, reason="motivo hold")]})
        store = FakeTradeStore()
        engine = _make_engine(connector, kill_switch, strategy, store=store)

        engine.run_cycle()

        assert len(store.signals) == 1
        signal, executed = store.signals[0]
        assert signal.action == SignalAction.HOLD
        assert executed is False

    def test_equity_snapshot_recorded_on_successful_cycle(self, connector, kill_switch):
        store = FakeTradeStore()
        engine = _make_engine(connector, kill_switch, FakeStrategy(), store=store)

        engine.run_cycle()

        assert len(store.equity_snapshots) == 1
        assert store.equity_snapshots[0]["equity"] == 100_000.0

    def test_no_equity_snapshot_when_market_closed(self, connector, kill_switch):
        connector.market_open = False
        store = FakeTradeStore()
        engine = _make_engine(connector, kill_switch, FakeStrategy(), store=store)

        engine.run_cycle()

        assert store.equity_snapshots == []

    def test_buy_execution_opens_trade_with_real_fill_price(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY", price=100.0)]
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.BUY, price=100.0)]})
        connector.queue_get_order_response(None)
        connector.set_submit_result(
            _order(status=OrderStatus.FILLED, symbol="SPY", qty=200.0, filled_avg_price=101.0)
        )
        store = FakeTradeStore()
        engine = _make_engine(connector, kill_switch, strategy, store=store)

        engine.run_cycle()

        assert len(store.open_trades) == 1
        opened = store.open_trades[0]
        assert opened["symbol"] == "SPY"
        assert opened["qty"] == 200.0
        # precio real de ejecucion, no el precio de la senal que la disparo
        assert opened["entry_price"] == 101.0
        assert opened["entry_order_id"] == "broker-1"

    def test_sell_execution_closes_the_matching_open_trade(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY", price=100.0)]
        connector.positions["SPY"] = Position(
            symbol="SPY", qty=50.0, avg_entry_price=90.0, market_value=5000.0, unrealized_pl=500.0
        )
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.SELL, price=100.0)]})
        connector.queue_get_order_response(None)
        connector.set_submit_result(
            _order(status=OrderStatus.FILLED, symbol="SPY", side=OrderSide.SELL, qty=50.0, filled_avg_price=95.0)
        )
        store = FakeTradeStore()
        store.open_trade(
            symbol="SPY",
            side="buy",
            qty=50.0,
            entry_price=90.0,
            entry_time=FIXED_NOW,
            entry_order_id="prev-entry-1",
            stop_loss_price=88.0,
            reason="apertura previa",
        )

        engine = _make_engine(connector, kill_switch, strategy, store=store)
        engine.run_cycle()

        assert len(store.closed_trades) == 1
        trade_id, kwargs = store.closed_trades[0]
        assert trade_id == 1
        assert kwargs["exit_price"] == 95.0
        assert kwargs["pnl_pct_of_capital"] == pytest.approx(0.0025)

    def test_close_without_matching_open_trade_does_not_crash(self, connector, kill_switch):
        # Normal si la posicion se abrio antes de tener persistencia --
        # no debe fallar el ciclo.
        connector.bars_by_symbol["SPY"] = [_bar("SPY", price=100.0)]
        connector.positions["SPY"] = Position(
            symbol="SPY", qty=50.0, avg_entry_price=90.0, market_value=5000.0, unrealized_pl=500.0
        )
        strategy = FakeStrategy({"SPY": [_signal("SPY", SignalAction.SELL, price=100.0)]})
        connector.queue_get_order_response(None)
        connector.set_submit_result(
            _order(status=OrderStatus.FILLED, symbol="SPY", side=OrderSide.SELL, qty=50.0, filled_avg_price=95.0)
        )
        store = FakeTradeStore()  # sin ningun open_trade previo

        engine = _make_engine(connector, kill_switch, strategy, store=store)
        result = engine.run_cycle()

        assert result.symbol_results[0].executed is True
        assert store.closed_trades == []

    def test_symbol_error_is_recorded_in_store(self, connector, kill_switch):
        connector.bars_by_symbol["SPY"] = [_bar("SPY")]
        strategy = FakeStrategy(error_symbols={"SPY"})
        store = FakeTradeStore()
        engine = _make_engine(connector, kill_switch, strategy, store=store)

        engine.run_cycle()

        assert len(store.errors) == 1
        assert store.errors[0]["symbol"] == "SPY"

    def test_connection_failure_is_recorded_in_store(self, connector, kill_switch):
        connector.market_open_error = ConnectorError("boom")
        store = FakeTradeStore()
        engine = _make_engine(connector, kill_switch, FakeStrategy(), store=store)

        engine.run_cycle()

        assert len(store.errors) == 1
        assert store.errors[0]["symbol"] is None
        assert "boom" in store.errors[0]["message"]
