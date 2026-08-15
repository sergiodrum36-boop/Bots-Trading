"""Tests de persistence/kill_switch_store.py (SqlKillSwitchStateStore) --
el backend de base de datos del kill switch, pensado para entornos
efímeros (GitHub Actions) donde el archivo JSON local no sobrevive entre
ejecuciones. Usa SQLite real sobre un archivo temporal (tmp_path); el
comportamiento es el mismo con Postgres, solo cambia la URL de conexión
(ver persistence/database.py:is_db_url)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from persistence.database import make_engine, make_session_factory, session_scope
from persistence.kill_switch_store import SqlKillSwitchStateStore, _ROW_ID
from persistence.models import KillSwitchStateRecord
from risk.kill_switch import (
    KillSwitch,
    KillSwitchConfig,
    KillSwitchState,
    KillSwitchStateCorruptError,
)


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(tmp_path / "test.sqlite")
    return make_session_factory(engine)


@pytest.fixture
def store(session_factory) -> SqlKillSwitchStateStore:
    return SqlKillSwitchStateStore(session_factory)


class TestSqlKillSwitchStateStore:
    def test_load_returns_none_when_no_row_yet(self, store):
        assert store.load() is None

    def test_save_then_load_round_trips(self, store):
        state = KillSwitchState(
            date="2026-08-17",
            triggered=True,
            trigger_reason="motivo de prueba",
            triggered_at="2026-08-17T10:00:00+00:00",
            daily_pnl_pct=-0.05,
            consecutive_losses=3,
            trades_today=5,
            connection_lost_at=None,
        )
        store.save(state)

        loaded = store.load()
        assert loaded == state

    def test_save_twice_updates_the_same_row(self, store, session_factory):
        store.save(KillSwitchState(date="2026-08-17", trades_today=1))
        store.save(KillSwitchState(date="2026-08-17", trades_today=2))

        with session_scope(session_factory) as session:
            assert session.query(KillSwitchStateRecord).count() == 1

        assert store.load().trades_today == 2

    def test_corrupt_json_raises_corrupt_error(self, store, session_factory):
        with session_scope(session_factory) as session:
            session.add(
                KillSwitchStateRecord(
                    id=_ROW_ID,
                    state_json="esto no es json valido {{{",
                    updated_at=datetime.now(timezone.utc),
                )
            )

        with pytest.raises(KillSwitchStateCorruptError):
            store.load()


class TestKillSwitchWithSqlBackend:
    """Mismos escenarios de seguridad que tests/test_kill_switch.py
    (backend de archivo), pero con el backend de base de datos -- deben
    comportarse exactamente igual, ver risk/kill_switch.py."""

    def test_state_persists_across_restarts(self, session_factory):
        store1 = SqlKillSwitchStateStore(session_factory)
        ks1 = KillSwitch(config=KillSwitchConfig(), store=store1)
        ks1.check_position_discrepancy({"SPY": 10}, {"SPY": 0})
        assert ks1.is_active() is True

        # Simula un reinicio del proceso: nueva instancia de KillSwitch (y
        # de store), misma base de datos.
        store2 = SqlKillSwitchStateStore(session_factory)
        ks2 = KillSwitch(config=KillSwitchConfig(), store=store2)

        assert ks2.is_active() is True
        assert ks2.can_open_new_position() is False
        assert ks2.state.trigger_reason == ks1.state.trigger_reason

    def test_corrupted_row_starts_paused_for_safety(self, session_factory):
        with session_scope(session_factory) as session:
            session.add(
                KillSwitchStateRecord(
                    id=_ROW_ID,
                    state_json="esto no es json valido {{{",
                    updated_at=datetime.now(timezone.utc),
                )
            )

        ks = KillSwitch(config=KillSwitchConfig(), store=SqlKillSwitchStateStore(session_factory))

        assert ks.is_active() is True
        assert ks.can_open_new_position() is False

    def test_reset_persists_across_restart(self, session_factory):
        ks1 = KillSwitch(config=KillSwitchConfig(), store=SqlKillSwitchStateStore(session_factory))
        ks1.check_position_discrepancy({"SPY": 10}, {"SPY": 0})
        ks1.reset(confirmed_by="sergio")

        ks2 = KillSwitch(config=KillSwitchConfig(), store=SqlKillSwitchStateStore(session_factory))
        assert ks2.is_active() is False
