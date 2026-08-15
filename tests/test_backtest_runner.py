"""Tests de integracion de backtesting/backtest_runner.py: conector (fake,
sin red) -> estrategia -> simulacion de operaciones -> metricas.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from backtesting.backtest_runner import run_backtest
from connectors.base_connector import (
    BrokerConnector,
    MarketSession,
    OHLCVBar,
    Timeframe,
)
from strategy.trend_strategy import TrendStrategyConfig


class FakeConnector(BrokerConnector):
    """Conector de pruebas: devuelve velas precargadas en memoria, sin
    llamar a ninguna API real. Solo implementa lo que usa el backtest
    (get_historical_bars); el resto no se necesita para este test."""

    def __init__(self, bars_by_symbol: dict[str, list[OHLCVBar]]):
        self._bars_by_symbol = bars_by_symbol

    def get_historical_bars(self, symbols, timeframe, start, end=None, limit=None):
        return list(self._bars_by_symbol.get(symbols, []))

    def get_recent_bars(self, symbol, timeframe=Timeframe.DAY, limit=100):
        raise NotImplementedError

    def get_latest_bar(self, symbol):
        raise NotImplementedError

    def get_market_calendar(self, start: date, end: date) -> list[MarketSession]:
        raise NotImplementedError

    def is_market_open(self) -> bool:
        raise NotImplementedError

    def is_symbol_tradable(self, symbol: str) -> bool:
        raise NotImplementedError

    def get_account(self):
        raise NotImplementedError

    def get_open_positions(self):
        raise NotImplementedError

    def submit_market_order(self, symbol, qty, side, client_order_id):
        raise NotImplementedError

    def get_order_by_client_id(self, client_order_id):
        raise NotImplementedError


def _bars(symbol: str, closes: list[float]) -> list[OHLCVBar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, c in enumerate(closes):
        bars.append(
            OHLCVBar(
                symbol=symbol,
                timestamp=start + timedelta(days=i),
                open=c,
                high=c + 0.5,
                low=c - 0.5,
                close=c,
                volume=1_000.0,
            )
        )
    return bars


def _uptrend_then_downtrend_closes() -> list[float]:
    # Bajada suave (calentamiento de indicadores) + repunte marcado (deberia
    # disparar BUY, con pendiente moderada para que el RSI todavia este en
    # rango de compra en el momento exacto del cruce alcista) + caida
    # marcada (deberia disparar SELL).
    decline = [100.0 - i * 0.4 for i in range(20)]
    rally = [decline[-1] + i * 1.0 for i in range(1, 20)]
    drop = [rally[-1] - i * 1.0 for i in range(1, 20)]
    return decline + rally + drop


TEST_CONFIG = TrendStrategyConfig(
    ema_fast=3, ema_slow=5, rsi_period=5, macd_fast=4, macd_slow=9, macd_signal=3
)


class TestRunBacktest:
    def test_produces_at_least_one_round_trip_trade_without_lookahead(self):
        closes = _uptrend_then_downtrend_closes()
        connector = FakeConnector({"SPY": _bars("SPY", closes)})

        result = run_backtest(
            connector=connector,
            symbols=["SPY"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        assert result.metrics.n_trades >= 1
        for trade in result.trades:
            # Nunca se opera en el mismo instante que genero la senal: la
            # entrada siempre es estrictamente posterior a la primera vela
            # de la serie, y la salida siempre es posterior a la entrada
            # (sin look-ahead bias, ver backtest_runner.py punto 2).
            assert trade.exit_time > trade.entry_time
            assert trade.entry_price > 0
            assert trade.exit_price > 0

    def test_metrics_dict_has_expected_keys(self):
        closes = _uptrend_then_downtrend_closes()
        connector = FakeConnector({"SPY": _bars("SPY", closes)})

        result = run_backtest(
            connector=connector,
            symbols=["SPY"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        expected_keys = {
            "n_trades",
            "win_rate",
            "profit_factor",
            "sharpe_ratio",
            "max_drawdown",
            "total_return_pct",
        }
        assert expected_keys == set(result.metrics.as_dict().keys())

    def test_symbol_without_data_does_not_crash_and_reports_zero_trades(self):
        connector = FakeConnector({})  # ningun simbolo tiene datos

        result = run_backtest(
            connector=connector,
            symbols=["QQQ"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        assert result.metrics.n_trades == 0
        assert result.per_symbol_metrics["QQQ"].n_trades == 0

    def test_multiple_symbols_are_combined_in_overall_metrics(self):
        closes = _uptrend_then_downtrend_closes()
        connector = FakeConnector(
            {"SPY": _bars("SPY", closes), "QQQ": _bars("QQQ", closes)}
        )

        result = run_backtest(
            connector=connector,
            symbols=["SPY", "QQQ"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        assert set(result.per_symbol_metrics.keys()) == {"SPY", "QQQ"}
        assert result.metrics.n_trades == (
            result.per_symbol_metrics["SPY"].n_trades
            + result.per_symbol_metrics["QQQ"].n_trades
        )
        # Las operaciones combinadas deben quedar ordenadas por fecha de cierre.
        exit_times = [t.exit_time for t in result.trades]
        assert exit_times == sorted(exit_times)

    def test_overall_return_and_drawdown_are_none_when_multiple_symbols_mix(self):
        # Con mas de un simbolo, total_return_pct/max_drawdown GLOBALES no
        # deben calcularse (compondrian de forma irreal un capital unico
        # secuencial con operaciones de varios simbolos entrelazadas) --
        # ver docstring de backtest_runner.py, punto 5.
        closes = _uptrend_then_downtrend_closes()
        connector = FakeConnector(
            {"SPY": _bars("SPY", closes), "QQQ": _bars("QQQ", closes)}
        )

        result = run_backtest(
            connector=connector,
            symbols=["SPY", "QQQ"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        assert result.metrics.total_return_pct is None
        assert result.metrics.max_drawdown is None
        # win_rate/profit_factor/sharpe_ratio SI son validos combinados.
        assert result.metrics.n_trades > 0

        # Por simbolo, en cambio, si representan una posicion real de ese
        # simbolo y deben venir como numero.
        assert result.per_symbol_metrics["SPY"].total_return_pct is not None
        assert result.per_symbol_metrics["QQQ"].total_return_pct is not None

    def test_overall_return_and_drawdown_are_numbers_for_a_single_symbol(self):
        closes = _uptrend_then_downtrend_closes()
        connector = FakeConnector({"SPY": _bars("SPY", closes)})

        result = run_backtest(
            connector=connector,
            symbols=["SPY"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        assert isinstance(result.metrics.total_return_pct, float)
        assert isinstance(result.metrics.max_drawdown, float)

    def test_no_trades_returns_zeroed_metrics_without_error(self):
        # Serie plana: no hay cruces de EMA ni sobrecompra -> sin operaciones.
        closes = [100.0] * 40
        connector = FakeConnector({"SPY": _bars("SPY", closes)})

        result = run_backtest(
            connector=connector,
            symbols=["SPY"],
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 1, tzinfo=timezone.utc),
            strategy_config=TEST_CONFIG,
        )

        assert result.metrics.n_trades == 0
        assert result.metrics.sharpe_ratio == 0.0
        assert result.metrics.max_drawdown == 0.0
