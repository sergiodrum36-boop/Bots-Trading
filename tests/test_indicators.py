"""Tests unitarios de strategy/indicators.py (calculo puro sobre pandas,
sin dependencias externas ni red)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from connectors.base_connector import OHLCVBar
from strategy.indicators import add_indicators, bars_to_dataframe, ema, macd, rsi


def _make_bars(symbol: str, closes: list[float]) -> list[OHLCVBar]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        OHLCVBar(
            symbol=symbol,
            timestamp=start + timedelta(days=i),
            open=c,
            high=c + 1,
            low=c - 1,
            close=c,
            volume=1_000.0,
        )
        for i, c in enumerate(closes)
    ]


class TestEma:
    def test_ema_of_constant_series_equals_the_constant(self):
        series = pd.Series([100.0] * 30)
        result = ema(series, period=10)
        assert result.iloc[-1] == pytest.approx(100.0)

    def test_ema_before_min_periods_is_nan(self):
        series = pd.Series([100.0] * 30)
        result = ema(series, period=10)
        assert result.iloc[0:9].isna().all()
        assert pd.notna(result.iloc[9])

    def test_ema_invalid_period_raises(self):
        with pytest.raises(ValueError):
            ema(pd.Series([1.0, 2.0]), period=0)


class TestRsi:
    def test_rsi_all_gains_is_100(self):
        series = pd.Series([100.0 + i for i in range(20)])  # siempre sube
        result = rsi(series, period=14)
        assert result.iloc[-1] == pytest.approx(100.0)

    def test_rsi_all_losses_is_0(self):
        series = pd.Series([100.0 - i for i in range(20)])  # siempre baja
        result = rsi(series, period=14)
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_rsi_invalid_period_raises(self):
        with pytest.raises(ValueError):
            rsi(pd.Series([1.0, 2.0]), period=0)


class TestMacd:
    def test_macd_histogram_is_zero_for_constant_series(self):
        series = pd.Series([50.0] * 40)
        result = macd(series, fast=12, slow=26, signal=9)
        assert result["macd_histogram"].iloc[-1] == pytest.approx(0.0)

    def test_macd_fast_must_be_smaller_than_slow(self):
        with pytest.raises(ValueError):
            macd(pd.Series([1.0, 2.0, 3.0]), fast=26, slow=12)


class TestBarsToDataframe:
    def test_empty_list_returns_empty_dataframe_with_expected_columns(self):
        df = bars_to_dataframe([])
        assert df.empty
        assert "close" in df.columns

    def test_sorts_by_timestamp(self):
        bars = _make_bars("SPY", [1.0, 2.0, 3.0])
        # Desordenamos a proposito.
        shuffled = [bars[2], bars[0], bars[1]]
        df = bars_to_dataframe(shuffled)
        assert list(df["close"]) == [1.0, 2.0, 3.0]
        assert df.index.name == "timestamp"


class TestAddIndicators:
    def test_adds_expected_columns(self):
        bars = _make_bars("SPY", [100.0 + i for i in range(60)])
        df = bars_to_dataframe(bars)
        enriched = add_indicators(df, ema_fast=5, ema_slow=10, rsi_period=5)
        for col in ["ema_fast", "ema_slow", "rsi", "macd", "macd_signal", "macd_histogram"]:
            assert col in enriched.columns

    def test_missing_close_column_raises(self):
        df = pd.DataFrame({"open": [1.0, 2.0]})
        with pytest.raises(ValueError):
            add_indicators(df)

    def test_multiple_symbols_raises(self):
        bars = _make_bars("SPY", [100.0, 101.0]) + _make_bars("QQQ", [200.0, 201.0])
        df = bars_to_dataframe(bars)
        with pytest.raises(ValueError):
            add_indicators(df)
