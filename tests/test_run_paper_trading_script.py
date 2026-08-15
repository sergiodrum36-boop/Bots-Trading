"""Smoke tests de scripts/run_paper_trading.py: verifican que el entry
point de consola funciona (credenciales ausentes, mercado cerrado, ciclo
sin datos, kill switch activo), sin llamar a la API real de Alpaca ni
enviar ninguna orden real."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import connectors.alpaca_connector as ac_module
import scripts.run_paper_trading as script


@pytest.fixture
def mock_alpaca_clients(monkeypatch):
    trading_instance = MagicMock(name="TradingClientInstance")
    data_instance = MagicMock(name="StockHistoricalDataClientInstance")
    monkeypatch.setattr(ac_module, "TradingClient", MagicMock(return_value=trading_instance))
    monkeypatch.setattr(
        ac_module, "StockHistoricalDataClient", MagicMock(return_value=data_instance)
    )
    trading_instance.get_account.return_value = SimpleNamespace(
        equity="100000", cash="100000", buying_power="100000"
    )
    trading_instance.get_all_positions.return_value = []
    trading_instance.get_asset.return_value = SimpleNamespace(tradable=True, status="active")
    # Sin velas: suficiente para comprobar que el ciclo no revienta y llega
    # a "sin velas disponibles" (HOLD) sin necesidad de datos reales.
    data_instance.get_stock_bars.return_value = SimpleNamespace(data={})
    return SimpleNamespace(trading=trading_instance, data=data_instance)


def _env(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def test_missing_credentials_exits_with_error(monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    exit_code = script.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert ".env" in captured.err


def test_market_closed_reports_no_symbols_evaluated(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path
):
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=False)

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(tmp_path / "audit.sqlite"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Mercado abierto: False" in captured.out
    assert "mercado esta cerrado" in captured.out
    # No debe haberse consultado la cuenta si el mercado esta cerrado.
    mock_alpaca_clients.trading.get_account.assert_not_called()


def test_market_open_no_data_reports_hold_per_symbol(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path
):
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=True)

    exit_code = script.main(
        [
            "--symbols", "SPY", "QQQ",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(tmp_path / "audit.sqlite"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Mercado abierto: True" in captured.out
    assert "SPY: senal=HOLD" in captured.out
    assert "QQQ: senal=HOLD" in captured.out
    assert "sin velas disponibles" in captured.out
    # Ninguna orden debe haberse enviado sin datos que generen una senal.
    mock_alpaca_clients.trading.submit_order.assert_not_called()


def test_kill_switch_state_file_argument_is_used(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path
):
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=True)
    state_file = tmp_path / "custom_state.json"

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(state_file),
            "--db-path", str(tmp_path / "audit.sqlite"),
        ]
    )

    assert exit_code == 0
    assert state_file.exists()


def test_already_active_kill_switch_blocks_the_whole_cycle(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path
):
    _env(monkeypatch)
    state_file = tmp_path / "ks.json"
    state_file.write_text(
        '{"date": "2020-01-01", "triggered": true, "trigger_reason": "prueba", '
        '"triggered_at": "2020-01-01T00:00:00+00:00", "daily_pnl_pct": 0.0, '
        '"consecutive_losses": 0, "trades_today": 0, "connection_lost_at": null}'
    )

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(state_file),
            "--db-path", str(tmp_path / "audit.sqlite"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Kill switch activo: True" in captured.out
    assert "reset() manual" in captured.out
    mock_alpaca_clients.trading.get_account.assert_not_called()


def test_db_path_argument_creates_and_records_to_the_database(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path
):
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=True)
    db_path = tmp_path / "custom_audit.sqlite"

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(db_path),
        ]
    )

    assert exit_code == 0
    assert db_path.exists()

    from persistence.database import make_engine, make_session_factory, session_scope
    from persistence.models import EquitySnapshotRecord, SignalRecord

    session_factory = make_session_factory(make_engine(db_path))
    with session_scope(session_factory) as session:
        assert session.query(EquitySnapshotRecord).count() == 1
        # Sin velas disponibles, el ciclo ni siquiera llega a generar una
        # senal para SPY (ver core/bot_engine.py._process_symbol).
        assert session.query(SignalRecord).count() == 0


def test_db_path_as_url_uses_the_sql_backed_kill_switch(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path
):
    """Cuando --db-path es una URL de conexion (Postgres/Supabase en
    produccion; aqui una URL de SQLite sirve igual para probar el mismo
    camino de codigo sin depender de una base de datos real) el estado del
    kill switch se guarda en esa base de datos, no en --state-file -- ver
    persistence/kill_switch_store.py. Es el modo pensado para GitHub
    Actions, que no conserva ningun archivo entre ejecuciones."""
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=True)
    db_url = f"sqlite:///{tmp_path / 'remote_audit.sqlite'}"
    unused_state_file = tmp_path / "unused_ks.json"

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(unused_state_file),
            "--db-path", db_url,
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "base de datos remota" in captured.out
    # El backend de base de datos se usa en vez del archivo -- no debe
    # haberse creado.
    assert not unused_state_file.exists()

    from persistence.database import make_engine, make_session_factory
    from persistence.kill_switch_store import SqlKillSwitchStateStore

    session_factory = make_session_factory(make_engine(db_url))
    state = SqlKillSwitchStateStore(session_factory).load()
    assert state is not None
    assert state.triggered is False
