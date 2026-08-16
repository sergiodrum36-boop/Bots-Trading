"""Tests unitarios de strategy/trend_strategy.py.

`_evaluate` (la logica de decision central: cruces de EMA + filtro RSI +
confirmacion MACD) se testea de forma aislada con valores de indicadores
controlados -- es mas robusto y legible que intentar fabricar una serie de
precios que produzca exactamente esos valores, y el calculo de los propios
indicadores ya tiene su cobertura en test_indicators.py. Ademas se incluyen
tests de integracion de `generate_signals` sobre una serie de precios real.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from connectors.base_connector import OHLCVBar
from strategy.indicators import bars_to_dataframe
from strategy.signals import SignalAction
from strategy.trend_strategy import TrendStrategy, TrendStrategyConfig


def _row(ema_fast, ema_slow, rsi, macd_histogram, close=100.0):
    return pd.Series(
        {
            "close": close,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi,
            "macd_histogram": macd_histogram,
        }
    )


class TestTrendStrategyConfig:
    def test_valid_config(self):
        TrendStrategyConfig(ema_fast=20, ema_slow=50)

    def test_ema_fast_must_be_smaller_than_ema_slow(self):
        with pytest.raises(ValueError):
            TrendStrategyConfig(ema_fast=50, ema_slow=20)

    def test_rsi_buy_range_must_be_valid(self):
        with pytest.raises(ValueError):
            TrendStrategyConfig(rsi_buy_min=70, rsi_buy_max=40)

    def test_rsi_sell_threshold_must_be_in_range(self):
        with pytest.raises(ValueError):
            TrendStrategyConfig(rsi_sell_threshold=0)

    def test_atr_period_must_be_positive(self):
        with pytest.raises(ValueError):
            TrendStrategyConfig(atr_period=0)


class TestEvaluateSignalLogic:
    def setup_method(self):
        self.strategy = TrendStrategy(
            TrendStrategyConfig(rsi_buy_min=40, rsi_buy_max=70, rsi_sell_threshold=80)
        )

    def test_bullish_cross_with_rsi_in_range_and_macd_positive_is_buy(self):
        prev = _row(ema_fast=9.9, ema_slow=10.0, rsi=55.0, macd_histogram=0.1)
        curr = _row(ema_fast=10.1, ema_slow=10.0, rsi=55.0, macd_histogram=0.1)

        action, reason = self.strategy._evaluate(prev, curr)

        assert action == SignalAction.BUY
        assert "cruce alcista" in reason

    def test_bullish_cross_with_rsi_outside_range_is_not_buy(self):
        prev = _row(ema_fast=9.9, ema_slow=10.0, rsi=85.0, macd_histogram=0.1)
        curr = _row(ema_fast=10.1, ema_slow=10.0, rsi=85.0, macd_histogram=0.1)

        action, _ = self.strategy._evaluate(prev, curr)

        # RSI > rsi_sell_threshold (80) domina como señal de sobrecompra.
        assert action == SignalAction.SELL

    def test_bullish_cross_with_negative_macd_is_hold(self):
        prev = _row(ema_fast=9.9, ema_slow=10.0, rsi=55.0, macd_histogram=-0.1)
        curr = _row(ema_fast=10.1, ema_slow=10.0, rsi=55.0, macd_histogram=-0.1)

        action, _ = self.strategy._evaluate(prev, curr)

        assert action == SignalAction.HOLD

    def test_bearish_cross_is_sell(self):
        prev = _row(ema_fast=10.1, ema_slow=10.0, rsi=50.0, macd_histogram=-0.1)
        curr = _row(ema_fast=9.9, ema_slow=10.0, rsi=50.0, macd_histogram=-0.1)

        action, reason = self.strategy._evaluate(prev, curr)

        assert action == SignalAction.SELL
        assert "cruce bajista" in reason

    def test_rsi_overbought_without_cross_is_sell(self):
        prev = _row(ema_fast=11.0, ema_slow=10.0, rsi=75.0, macd_histogram=0.1)
        curr = _row(ema_fast=11.2, ema_slow=10.0, rsi=85.0, macd_histogram=0.1)

        action, reason = self.strategy._evaluate(prev, curr)

        assert action == SignalAction.SELL
        assert "sobrecompra" in reason

    def test_no_cross_no_overbought_is_hold(self):
        prev = _row(ema_fast=11.0, ema_slow=10.0, rsi=55.0, macd_histogram=0.1)
        curr = _row(ema_fast=11.1, ema_slow=10.0, rsi=56.0, macd_histogram=0.1)

        action, _ = self.strategy._evaluate(prev, curr)

        assert action == SignalAction.HOLD


class TestGenerateSignals:
    def test_empty_dataframe_returns_no_signals(self):
        strategy = TrendStrategy()
        assert strategy.generate_signals("SPY", pd.DataFrame()) == []

    def test_warmup_period_is_always_hold(self):
        config = TrendStrategyConfig(ema_fast=3, ema_slow=5, rsi_period=3, macd_fast=3, macd_slow=6, macd_signal=2)
        strategy = TrendStrategy(config)

        closes = [100.0 + i * 0.5 for i in range(15)]
        bars = [
            OHLCVBar(
                symbol="SPY",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1_000.0,
            )
            for i, c in enumerate(closes)
        ]
        df = bars_to_dataframe(bars)

        signals = strategy.generate_signals("SPY", df)

        assert len(signals) == len(closes)
        # Las primeras velas, mientras el indicador mas lento (macd_slow=6)
        # no tiene suficiente historia, deben ser HOLD explicito.
        assert signals[0].action == SignalAction.HOLD
        assert "calentamiento" in signals[0].reason

    def test_atr_is_nan_during_warmup_and_a_real_number_afterwards(self):
        config = TrendStrategyConfig(
            ema_fast=3, ema_slow=5, rsi_period=3, macd_fast=3, macd_slow=6, macd_signal=2, atr_period=3
        )
        strategy = TrendStrategy(config)

        closes = [100.0 + i * 0.5 for i in range(15)]
        bars = [
            OHLCVBar(
                symbol="SPY",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1_000.0,
            )
            for i, c in enumerate(closes)
        ]
        df = bars_to_dataframe(bars)

        signals = strategy.generate_signals("SPY", df)

        assert pd.isna(signals[0].indicators.atr)
        assert pd.notna(signals[-1].indicators.atr)
        assert signals[-1].indicators.atr > 0

    def test_signals_length_matches_input_length_on_realistic_series(self):
        config = TrendStrategyConfig(ema_fast=3, ema_slow=5, rsi_period=3, macd_fast=3, macd_slow=6, macd_signal=2)
        strategy = TrendStrategy(config)

        # Bajada seguida de una subida marcada, para generar variedad de
        # señales (no solo el periodo de calentamiento).
        closes = [100.0 - i * 0.3 for i in range(15)] + [95.5 + i * 1.2 for i in range(1, 16)]
        bars = [
            OHLCVBar(
                symbol="SPY",
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1_000.0,
            )
            for i, c in enumerate(closes)
        ]
        df = bars_to_dataframe(bars)

        signals = strategy.generate_signals("SPY", df)

        assert len(signals) == len(closes)
        actions = {s.action for s in signals}
        # Con una bajada seguida de una subida marcada, esperamos ver algo
        # mas que solo HOLD (al menos una senal de BUY o SELL).
        assert actions - {SignalAction.HOLD}
