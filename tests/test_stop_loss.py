"""Tests unitarios de risk/stop_loss.py."""

from __future__ import annotations

import pytest

from risk.stop_loss import (
    StopLossConfig,
    StopLossMethod,
    atr_stop_loss,
    compute_stop_loss,
    fixed_percent_stop_loss,
)


class TestFixedPercentStopLoss:
    def test_basic_calculation(self):
        assert fixed_percent_stop_loss(100.0, percent=0.02) == pytest.approx(98.0)

    def test_default_percent_is_2_percent(self):
        assert fixed_percent_stop_loss(100.0) == pytest.approx(98.0)

    def test_invalid_entry_price_raises(self):
        with pytest.raises(ValueError):
            fixed_percent_stop_loss(0.0, percent=0.02)

    def test_invalid_percent_raises(self):
        with pytest.raises(ValueError):
            fixed_percent_stop_loss(100.0, percent=0.0)
        with pytest.raises(ValueError):
            fixed_percent_stop_loss(100.0, percent=1.0)


class TestAtrStopLoss:
    def test_basic_calculation(self):
        assert atr_stop_loss(100.0, atr=2.0, multiplier=2.0) == pytest.approx(96.0)

    def test_invalid_atr_raises(self):
        with pytest.raises(ValueError):
            atr_stop_loss(100.0, atr=0.0)
        with pytest.raises(ValueError):
            atr_stop_loss(100.0, atr=-1.0)

    def test_invalid_multiplier_raises(self):
        with pytest.raises(ValueError):
            atr_stop_loss(100.0, atr=2.0, multiplier=0.0)

    def test_atr_too_large_raises_instead_of_negative_stop(self):
        # ATR * multiplier > entry_price -> stop <= 0, no tiene sentido.
        with pytest.raises(ValueError):
            atr_stop_loss(10.0, atr=6.0, multiplier=2.0)


class TestComputeStopLoss:
    def test_dispatches_to_fixed_percent(self):
        config = StopLossConfig(method=StopLossMethod.FIXED_PERCENT, fixed_percent=0.05)
        assert compute_stop_loss(config, entry_price=100.0) == pytest.approx(95.0)

    def test_dispatches_to_atr(self):
        config = StopLossConfig(method=StopLossMethod.ATR, atr_multiplier=1.5)
        assert compute_stop_loss(config, entry_price=100.0, atr=4.0) == pytest.approx(94.0)

    def test_atr_method_without_atr_value_raises(self):
        config = StopLossConfig(method=StopLossMethod.ATR)
        with pytest.raises(ValueError):
            compute_stop_loss(config, entry_price=100.0)
