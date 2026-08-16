"""Smoke tests de scripts/run_paper_trading.py: verifican que el entry
point de consola funciona (credenciales ausentes, mercado cerrado, ciclo
sin datos, kill switch activo), sin llamar a la API real de Alpaca ni
enviar ninguna orden real.

Todas las invocaciones de `script.main(...)` pasan `--log-dir` apuntando
a `tmp_path` (via utils/logger.py, ver ese modulo) para no dejar una
carpeta `logs/` real en el directorio desde el que se ejecuta la suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import connectors.alpaca_connector as ac_module
import scripts.run_paper_trading as script
from connectors.base_connector import ConnectorError
from core.bot_engine import CycleResult, SymbolCycleResult
from risk.kill_switch import KillSwitch
from scripts.run_paper_trading import send_notifications
from strategy.signals import SignalAction


class _RecordingNotifier:
    """Doble de pruebas de `Notifier` (notifications/notifier.py): nunca
    toca la red, solo guarda lo que se le habria enviado -- asi los tests
    pueden comprobar QUE se notifico sin depender de Telegram real."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def notify(self, message: str) -> bool:
        self.messages.append(message)
        return True


@pytest.fixture(autouse=True)
def fake_notifier(monkeypatch):
    """Sustituye script.get_notifier por un doble en TODOS los tests de
    este archivo (autouse) -- sin esto, si el usuario tiene
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configurados en un .env real junto
    a trading_bot/ (mismo riesgo de fuga que load_dotenv(), ver
    connectors/alpaca_connector.py y notifications/notifier.py), correr
    esta suite podria disparar mensajes reales de Telegram. Los tests que
    quieran inspeccionar los mensajes enviados pueden pedir este fixture
    por nombre y leer su atributo `.messages`."""
    recorder = _RecordingNotifier()
    monkeypatch.setattr(script, "get_notifier", lambda *args, **kwargs: recorder)
    return recorder


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


def test_missing_credentials_exits_with_error(monkeypatch, capsys, tmp_path):
    # Sin esto, en un checkout real con un `.env` de verdad junto a
    # trading_bot/, `AlpacaConnector.__init__` -> `load_dotenv()` (sin ruta
    # explicita, busca subiendo desde connectors/alpaca_connector.py, no
    # desde el cwd) volveria a rellenar las credenciales que delenv() acaba
    # de borrar -- visto fallar en la practica ("assert 0 == 1").
    monkeypatch.setattr(ac_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    exit_code = script.main(["--log-dir", str(tmp_path / "logs")])

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
            "--log-dir", str(tmp_path / "logs"),
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
            "--log-dir", str(tmp_path / "logs"),
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
            "--log-dir", str(tmp_path / "logs"),
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
            "--log-dir", str(tmp_path / "logs"),
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
            "--log-dir", str(tmp_path / "logs"),
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
            "--log-dir", str(tmp_path / "logs"),
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


# ----------------------------------------------------------------------
# Notificaciones (notifications/notifier.py) -- via script.main()
# ----------------------------------------------------------------------
def test_market_closed_without_notify_summary_sends_no_notification(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path, fake_notifier
):
    """Por defecto (sin --notify-summary) solo se avisa de lo critico -- un
    ciclo sin incidentes no debe generar ningun mensaje de Telegram."""
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=False)

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(tmp_path / "audit.sqlite"),
            "--log-dir", str(tmp_path / "logs"),
        ]
    )

    assert exit_code == 0
    assert fake_notifier.messages == []


def test_notify_summary_flag_sends_summary_when_market_closed(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path, fake_notifier
):
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=False)

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(tmp_path / "audit.sqlite"),
            "--log-dir", str(tmp_path / "logs"),
            "--notify-summary",
        ]
    )

    assert exit_code == 0
    assert len(fake_notifier.messages) == 1
    assert "resumen del ciclo" in fake_notifier.messages[0]
    assert "mercado cerrado" in fake_notifier.messages[0]


def test_already_active_kill_switch_sends_no_notification(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path, fake_notifier
):
    """Un kill switch que YA estaba activo de una ejecucion anterior no
    debe reavisarse en cada ciclo posterior -- solo la primera vez que se
    dispara (ver send_notifications() en scripts/run_paper_trading.py)."""
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
            "--log-dir", str(tmp_path / "logs"),
        ]
    )

    assert exit_code == 0
    assert fake_notifier.messages == []


def test_connection_abort_sends_notification(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path, fake_notifier
):
    """Un fallo (no transitorio, para no reintentar) consultando si el
    mercado esta abierto aborta el ciclo entero (core/bot_engine.py) -- eso
    debe avisarse por Telegram."""
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.side_effect = ValueError("fallo simulado")

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(tmp_path / "audit.sqlite"),
            "--log-dir", str(tmp_path / "logs"),
        ]
    )

    assert exit_code == 0
    assert len(fake_notifier.messages) == 1
    assert "ciclo abortado" in fake_notifier.messages[0]


def test_run_cycle_connector_error_sends_notification_and_exits_with_error(
    monkeypatch, capsys, mock_alpaca_clients, tmp_path, fake_notifier
):
    """Cubre el `except ConnectorError` que envuelve `engine.run_cycle()`
    en `main()`. En la practica `BotEngine.run_cycle()` ya captura los
    fallos de conexion internamente y devuelve un `CycleResult` abortado
    (ver el test anterior) -- este bloque exterior es una ultima red de
    seguridad por si algo se escapase; se fuerza aqui para comprobar que
    tambien notifica y sale con codigo de error."""
    _env(monkeypatch)
    mock_alpaca_clients.trading.get_clock.return_value = SimpleNamespace(is_open=True)
    monkeypatch.setattr(
        script.BotEngine,
        "run_cycle",
        lambda self: (_ for _ in ()).throw(ConnectorError("caida simulada")),
    )

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--state-file", str(tmp_path / "ks.json"),
            "--db-path", str(tmp_path / "audit.sqlite"),
            "--log-dir", str(tmp_path / "logs"),
        ]
    )

    assert exit_code == 1
    assert len(fake_notifier.messages) == 1
    assert "fallo no controlado consultando Alpaca" in fake_notifier.messages[0]


# ----------------------------------------------------------------------
# Notificaciones -- tests directos de send_notifications() (sin pasar por
# script.main()), para controlar con precision el estado del kill switch y
# de los resultados del ciclo sin tener que orquestar un BotEngine entero.
# ----------------------------------------------------------------------
class TestSendNotifications:
    @staticmethod
    def _cycle_result(**overrides):
        defaults = dict(
            started_at=datetime.now(timezone.utc),
            market_open=True,
            kill_switch_active=False,
            aborted=False,
            abort_reason=None,
            symbol_results=(),
        )
        defaults.update(overrides)
        return CycleResult(**defaults)

    def test_kill_switch_newly_triggered_sends_exactly_one_notification(self, tmp_path):
        notifier = _RecordingNotifier()
        kill_switch = KillSwitch(state_file=str(tmp_path / "ks.json"))
        kill_switch_was_active_before = kill_switch.is_active()
        assert kill_switch_was_active_before is False

        # Se dispara AHORA, durante este ciclo (perdidas consecutivas).
        for _ in range(kill_switch.config.max_consecutive_losses):
            kill_switch.record_trade_result(-0.01)
        assert kill_switch.is_active() is True

        result = self._cycle_result(kill_switch_active=True)
        send_notifications(
            notifier, result, kill_switch, kill_switch_was_active_before, notify_summary=False
        )

        assert len(notifier.messages) == 1
        assert "KILL SWITCH ACTIVADO" in notifier.messages[0]

    def test_kill_switch_already_active_before_does_not_resend(self, tmp_path):
        notifier = _RecordingNotifier()
        kill_switch = KillSwitch(state_file=str(tmp_path / "ks.json"))
        for _ in range(kill_switch.config.max_consecutive_losses):
            kill_switch.record_trade_result(-0.01)
        assert kill_switch.is_active() is True

        result = self._cycle_result(kill_switch_active=True)
        send_notifications(notifier, result, kill_switch, True, notify_summary=False)

        assert notifier.messages == []

    def test_symbol_error_sends_notification_with_detail(self, tmp_path):
        notifier = _RecordingNotifier()
        kill_switch = KillSwitch(state_file=str(tmp_path / "ks.json"))
        symbol_result = SymbolCycleResult(
            symbol="SPY",
            action=SignalAction.HOLD,
            executed=False,
            reason="error inesperado procesando el simbolo",
            error="boom",
        )
        result = self._cycle_result(symbol_results=(symbol_result,))
        send_notifications(notifier, result, kill_switch, False, notify_summary=False)

        assert len(notifier.messages) == 1
        assert "SPY: boom" in notifier.messages[0]

    def test_notify_summary_lists_each_symbol(self, tmp_path):
        notifier = _RecordingNotifier()
        kill_switch = KillSwitch(state_file=str(tmp_path / "ks.json"))
        symbol_result = SymbolCycleResult(
            symbol="SPY", action=SignalAction.BUY, executed=True, reason="cruce alcista"
        )
        result = self._cycle_result(symbol_results=(symbol_result,))
        send_notifications(notifier, result, kill_switch, False, notify_summary=True)

        assert len(notifier.messages) == 1
        assert "SPY: BUY (ejecutado)" in notifier.messages[0]

    def test_nothing_notable_sends_no_notification_by_default(self, tmp_path):
        notifier = _RecordingNotifier()
        kill_switch = KillSwitch(state_file=str(tmp_path / "ks.json"))
        result = self._cycle_result()
        send_notifications(notifier, result, kill_switch, False, notify_summary=False)

        assert notifier.messages == []
