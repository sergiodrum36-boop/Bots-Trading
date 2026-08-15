"""Tests unitarios de risk/kill_switch.py -- la pieza de seguridad mas
critica del proyecto, asi que se testea con cuidado especial: persistencia
en disco, arranque en modo pausado tras un reinicio, no reanudacion
automatica, y cada condicion de disparo por separado.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from risk.kill_switch import KillSwitch, KillSwitchConfig, KillSwitchState


class FakeClock:
    """Reloj controlable manualmente, para testear de forma determinista
    la logica de dias/duraciones sin depender del reloj real."""

    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "kill_switch_state.json"


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc))


@pytest.fixture
def config():
    return KillSwitchConfig(
        max_daily_loss_pct=0.05,
        max_consecutive_losses=3,
        max_connection_loss_minutes=15.0,
        max_trades_per_day=10,
    )


class TestKillSwitchConfig:
    def test_invalid_max_daily_loss_pct_raises(self):
        with pytest.raises(ValueError):
            KillSwitchConfig(max_daily_loss_pct=0.0)

    def test_invalid_max_consecutive_losses_raises(self):
        with pytest.raises(ValueError):
            KillSwitchConfig(max_consecutive_losses=0)

    def test_invalid_max_trades_per_day_raises(self):
        with pytest.raises(ValueError):
            KillSwitchConfig(max_trades_per_day=0)


class TestFreshState:
    def test_starts_inactive_and_allows_trading(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        assert ks.is_active() is False
        assert ks.can_open_new_position() is True

    def test_state_file_is_created(self, state_path, config, clock):
        KillSwitch(config=config, state_file=state_path, clock=clock)
        assert state_path.exists()


class TestDailyLossThreshold:
    def test_triggers_when_daily_loss_reaches_threshold(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)

        ks.record_trade_result(-0.02)
        assert ks.is_active() is False
        ks.record_trade_result(-0.02)
        assert ks.is_active() is False
        ks.record_trade_result(-0.02)  # acumulado: -6% > umbral 5%

        assert ks.is_active() is True
        assert ks.can_open_new_position() is False
        assert "perdida acumulada del dia" in ks.state.trigger_reason

    def test_does_not_trigger_below_threshold(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_trade_result(-0.01)
        ks.record_trade_result(-0.01)
        assert ks.is_active() is False

    def test_winning_trades_offset_losses(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_trade_result(-0.03)
        ks.record_trade_result(0.02)  # neto -1%, lejos del umbral 5%
        assert ks.is_active() is False


class TestConsecutiveLosses:
    def test_triggers_after_n_consecutive_losses(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_trade_result(-0.001)
        ks.record_trade_result(-0.001)
        assert ks.is_active() is False
        ks.record_trade_result(-0.001)  # 3ª perdida consecutiva -> umbral

        assert ks.is_active() is True
        assert "perdidas consecutivas" in ks.state.trigger_reason

    def test_winning_trade_resets_the_streak(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_trade_result(-0.001)
        ks.record_trade_result(-0.001)
        ks.record_trade_result(0.001)  # gana -> resetea la racha
        ks.record_trade_result(-0.001)
        ks.record_trade_result(-0.001)

        assert ks.is_active() is False  # solo 2 perdidas consecutivas tras el reset


class TestConnectionLoss:
    def test_blocks_new_positions_immediately_while_down(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_connection_lost()

        assert ks.can_open_new_position() is False
        assert ks.is_active() is False  # bloquea, pero no es un "kill switch" todavia

    def test_short_outage_does_not_trigger_kill_switch(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_connection_lost()
        clock.advance(minutes=5)
        ks.record_connection_restored()

        assert ks.is_active() is False
        assert ks.can_open_new_position() is True

    def test_long_outage_triggers_kill_switch(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_connection_lost()
        clock.advance(minutes=20)  # supera el umbral de 15 min
        ks.record_connection_restored()

        assert ks.is_active() is True
        assert "perdida de conexion" in ks.state.trigger_reason


class TestPositionDiscrepancy:
    def test_matching_positions_do_not_trigger(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        discrepancy = ks.check_position_discrepancy({"SPY": 10}, {"SPY": 10})
        assert discrepancy is False
        assert ks.is_active() is False

    def test_mismatched_positions_trigger(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        discrepancy = ks.check_position_discrepancy({"SPY": 10}, {"SPY": 7})
        assert discrepancy is True
        assert ks.is_active() is True
        assert "discrepancia" in ks.state.trigger_reason


class TestMaxTradesPerDay:
    def test_blocks_after_reaching_daily_limit(self, state_path, clock):
        config = KillSwitchConfig(max_trades_per_day=2)
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)

        ks.record_trade_result(0.001)
        assert ks.can_open_new_position() is True
        ks.record_trade_result(0.001)
        assert ks.can_open_new_position() is False  # ya van 2 operaciones hoy


class TestPersistenceAcrossRestarts:
    def test_triggered_state_persists_when_reloaded(self, state_path, config, clock):
        ks1 = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks1.check_position_discrepancy({"SPY": 10}, {"SPY": 0})
        assert ks1.is_active() is True

        # Simula un reinicio del proceso: nueva instancia, mismo fichero de estado.
        ks2 = KillSwitch(config=config, state_file=state_path, clock=clock)

        assert ks2.is_active() is True
        assert ks2.can_open_new_position() is False
        assert ks2.state.trigger_reason == ks1.state.trigger_reason

    def test_corrupted_state_file_starts_paused_for_safety(self, state_path, config, clock):
        state_path.write_text("esto no es json valido {{{")

        ks = KillSwitch(config=config, state_file=state_path, clock=clock)

        assert ks.is_active() is True
        assert ks.can_open_new_position() is False


class TestReset:
    def test_reset_requires_confirmed_by(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.check_position_discrepancy({"SPY": 10}, {"SPY": 0})

        with pytest.raises(ValueError):
            ks.reset(confirmed_by="")

    def test_reset_clears_trigger_and_allows_trading_again(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.check_position_discrepancy({"SPY": 10}, {"SPY": 0})
        assert ks.is_active() is True

        ks.reset(confirmed_by="sergio")

        assert ks.is_active() is False
        assert ks.can_open_new_position() is True

    def test_reset_persists_across_restart(self, state_path, config, clock):
        ks1 = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks1.check_position_discrepancy({"SPY": 10}, {"SPY": 0})
        ks1.reset(confirmed_by="sergio")

        ks2 = KillSwitch(config=config, state_file=state_path, clock=clock)
        assert ks2.is_active() is False


class TestDayRollover:
    def test_daily_counters_reset_on_new_day_when_not_triggered(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_trade_result(-0.02)
        assert ks.state.daily_pnl_pct == pytest.approx(-0.02)

        clock.advance(days=1)
        ks.record_trade_result(-0.01)  # si no se reseteara, acumularia -0.03

        assert ks.state.daily_pnl_pct == pytest.approx(-0.01)
        assert ks.state.consecutive_losses == 1  # tambien se resetea la racha

    def test_triggered_state_does_not_clear_on_new_day(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.check_position_discrepancy({"SPY": 10}, {"SPY": 0})
        assert ks.is_active() is True

        clock.advance(days=1)

        assert ks.is_active() is True  # sigue pausado: hace falta reset() manual
        assert ks.can_open_new_position() is False

    def test_ongoing_connection_outage_survives_day_rollover(self, state_path, config, clock):
        ks = KillSwitch(config=config, state_file=state_path, clock=clock)
        ks.record_connection_lost()

        clock.advance(days=1)
        ks.record_trade_result(0.001)  # dispara un roll de dia

        # La caida de conexion sigue activa: no se "cura" por cambiar el dia.
        assert ks.can_open_new_position() is False


class TestKillSwitchStateSerialization:
    def test_round_trip(self):
        state = KillSwitchState(
            date="2026-08-15",
            triggered=True,
            trigger_reason="motivo de prueba",
            triggered_at="2026-08-15T10:00:00+00:00",
            daily_pnl_pct=-0.05,
            consecutive_losses=3,
            trades_today=5,
            connection_lost_at=None,
        )
        restored = KillSwitchState.from_dict(state.to_dict())
        assert restored == state
