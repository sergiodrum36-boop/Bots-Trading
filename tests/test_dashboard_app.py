"""Smoke test de dashboard/app.py: comprueba que el panel carga sin
excepciones y muestra los elementos esperados, usando
`streamlit.testing.v1.AppTest` (sin navegador real) y sin llamar nunca a
la API real de Alpaca."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest

import connectors.alpaca_connector as ac_module

# Resuelto contra este archivo (tests/), ver AppTest.from_file().
APP_PATH = "../dashboard/app.py"


def _kpi_value(at, label: str) -> str:
    """Busca la tarjeta KPI (dashboard/app.py, render_kpi_card, Etapa 1
    del rediseno "Panel v2") cuyo label coincide y devuelve el texto de su
    valor. Desde el restyling, las metricas de arriba ya no son st.metric
    sino HTML propio renderizado via st.markdown -- por eso ya no se
    pueden buscar con `at.metric`, hay que mirar entre los elementos
    markdown."""
    pattern = re.compile(
        rf'<div class="kpi-label">{re.escape(label)}</div>\s*'
        rf'<div class="kpi-value[^"]*">(.*?)</div>',
        re.DOTALL,
    )
    for md in at.markdown:
        match = pattern.search(md.value)
        if match:
            return match.group(1).strip()
    raise AssertionError(f"No se encontro la tarjeta KPI con label {label!r}")


@pytest.fixture
def mock_alpaca_clients(monkeypatch):
    trading_instance = MagicMock(name="TradingClientInstance")
    data_instance = MagicMock(name="StockHistoricalDataClientInstance")
    monkeypatch.setattr(ac_module, "TradingClient", MagicMock(return_value=trading_instance))
    monkeypatch.setattr(
        ac_module, "StockHistoricalDataClient", MagicMock(return_value=data_instance)
    )
    trading_instance.get_account.return_value = SimpleNamespace(
        equity="100000", cash="50000", buying_power="100000"
    )
    trading_instance.get_all_positions.return_value = []
    trading_instance.get_clock.return_value = SimpleNamespace(is_open=True)
    return SimpleNamespace(trading=trading_instance, data=data_instance)


def _env(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")


def test_app_loads_without_exceptions_with_live_connection(
    monkeypatch, mock_alpaca_clients, tmp_path
):
    _env(monkeypatch)
    # El sidebar usa por defecto una ruta relativa (default_db_path()) --
    # sin esto, la corrida escribiria un .sqlite real en la raiz del repo.
    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()

    assert not at.exception
    assert at.title[0].value == "Estado del bot"
    assert "Bot de Trading" in at.sidebar.title[0].value
    assert [t.label for t in at.tabs] == [
        "Historial de operaciones",
        "Curva de capital",
        "Senales",
        "Configuracion",
        "Errores",
    ]
    # Las metricas KPI (fila en vivo + resumen de "Historial de
    # operaciones") ya no son st.metric sino tarjetas HTML propias (ver
    # render_kpi_card en dashboard/app.py, Etapa 1 del rediseno) -- se
    # comprueban por contenido en vez de por at.metric.
    assert _kpi_value(at, "Equity") == "$100,000.00"
    assert _kpi_value(at, "Mercado") == "Abierto"


def test_app_degrades_gracefully_without_credentials(monkeypatch, tmp_path):
    # Sin APCA_API_KEY_ID/SECRET: el conector no se puede construir, pero el
    # panel debe seguir mostrando el historial (vacio) en vez de romperse.
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()

    assert not at.exception
    assert len(at.error) >= 1
    assert _kpi_value(at, "Mercado") == "-"


def test_app_shows_historical_data_from_the_database(monkeypatch, mock_alpaca_clients, tmp_path):
    _env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "audit.sqlite"

    from persistence.database import make_engine, make_session_factory
    from persistence.store import SqlTradeStore

    store = SqlTradeStore(make_session_factory(make_engine(db_path)))
    store.record_equity_snapshot(
        equity=100_000.0,
        cash=50_000.0,
        buying_power=100_000.0,
        timestamp=datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc),
    )

    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    # El input de base de datos es el primero de la barra lateral.
    at.sidebar.text_input[0].set_value(str(db_path)).run()

    assert not at.exception


def test_app_uses_sql_backed_kill_switch_when_db_path_is_a_url(
    monkeypatch, mock_alpaca_clients, tmp_path
):
    """Si el campo de base de datos es una URL de conexion (Postgres/
    Supabase en produccion; aqui una URL de SQLite prueba el mismo camino
    de codigo sin depender de una base de datos real), el panel debe usar
    esa misma base para el kill switch (persistence/kill_switch_store.py)
    en vez del archivo local -- y desactivar el campo de archivo, que pasa
    a ser irrelevante."""
    _env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    db_url = f"sqlite:///{tmp_path / 'remote.sqlite'}"

    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    at.sidebar.text_input[0].set_value(db_url).run()

    assert not at.exception
    assert at.sidebar.text_input[1].disabled is True
    assert _kpi_value(at, "Kill switch") == "Inactivo"

    from persistence.database import make_engine, make_session_factory
    from persistence.kill_switch_store import SqlKillSwitchStateStore

    session_factory = make_session_factory(make_engine(db_url))
    assert SqlKillSwitchStateStore(session_factory).load() is not None
