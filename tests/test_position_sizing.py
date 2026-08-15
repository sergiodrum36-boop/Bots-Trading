"""Tests unitarios de risk/position_sizing.py."""

from __future__ import annotations

import pytest

from risk.position_sizing import calculate_position_size


class TestCalculatePositionSize:
    def test_basic_calculation_not_capped(self):
        # capital=100_000, riesgo 1% -> 1_000 $ de riesgo.
        # entry=50, stop=45 -> distancia_stop=5 -> 200 acciones "por riesgo".
        # valor de posicion = 200*50 = 10_000 = 10% del capital, por debajo
        # del limite del 20% -> no deberia estar capado.
        result = calculate_position_size(
            capital=100_000.0,
            entry_price=50.0,
            stop_loss_price=45.0,
            risk_pct=0.01,
            max_position_pct=0.20,
        )

        assert result.shares == 200
        assert result.risk_amount == pytest.approx(1_000.0)
        assert result.position_value == pytest.approx(10_000.0)
        assert result.position_pct_of_capital == pytest.approx(0.10)
        assert result.capped_by_max_position is False

    def test_capped_by_max_position_pct(self):
        # capital=100_000, riesgo 1% -> 1_000 $ de riesgo.
        # entry=50, stop=49 -> distancia_stop=1 -> 1_000 acciones "por riesgo"
        # (valdrian 50_000 = 50% del capital), pero el limite es 20% ->
        # maximo 400 acciones (400*50=20_000).
        result = calculate_position_size(
            capital=100_000.0,
            entry_price=50.0,
            stop_loss_price=49.0,
            risk_pct=0.01,
            max_position_pct=0.20,
        )

        assert result.shares == 400
        assert result.position_value == pytest.approx(20_000.0)
        assert result.position_pct_of_capital == pytest.approx(0.20)
        assert result.capped_by_max_position is True

    def test_zero_shares_when_risk_budget_too_small(self):
        # riesgo_dinero = 1000*0.001 = 1$, distancia_stop=50 -> floor(1/50)=0.
        result = calculate_position_size(
            capital=1_000.0,
            entry_price=100.0,
            stop_loss_price=50.0,
            risk_pct=0.001,
        )

        assert result.shares == 0
        assert result.risk_amount == 0.0
        assert result.position_value == 0.0
        assert result.capped_by_max_position is False

    def test_stop_loss_must_be_below_entry_price(self):
        with pytest.raises(ValueError):
            calculate_position_size(
                capital=100_000.0, entry_price=50.0, stop_loss_price=50.0
            )
        with pytest.raises(ValueError):
            calculate_position_size(
                capital=100_000.0, entry_price=50.0, stop_loss_price=55.0
            )

    def test_invalid_capital_raises(self):
        with pytest.raises(ValueError):
            calculate_position_size(capital=0.0, entry_price=50.0, stop_loss_price=45.0)

    def test_invalid_entry_price_raises(self):
        with pytest.raises(ValueError):
            calculate_position_size(capital=100_000.0, entry_price=0.0, stop_loss_price=-1.0)

    def test_invalid_risk_pct_raises(self):
        with pytest.raises(ValueError):
            calculate_position_size(
                capital=100_000.0, entry_price=50.0, stop_loss_price=45.0, risk_pct=0.0
            )
        with pytest.raises(ValueError):
            calculate_position_size(
                capital=100_000.0, entry_price=50.0, stop_loss_price=45.0, risk_pct=1.5
            )

    def test_invalid_max_position_pct_raises(self):
        with pytest.raises(ValueError):
            calculate_position_size(
                capital=100_000.0,
                entry_price=50.0,
                stop_loss_price=45.0,
                max_position_pct=0.0,
            )
