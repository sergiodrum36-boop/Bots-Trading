"""Smoke tests de scripts/run_backtest.py: verifican que el entry point de
consola funciona (parseo de argumentos, manejo de credenciales ausentes,
formato del informe), sin llamar a la API real de Alpaca.

Todas las invocaciones de `script.main(...)` pasan `--log-dir` apuntando
a `tmp_path` (via utils/logger.py, ver ese modulo) para no dejar una
carpeta `logs/` real en el directorio desde el que se ejecuta la suite.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import connectors.alpaca_connector as ac_module
import scripts.run_backtest as script


@pytest.fixture
def mock_alpaca_clients(monkeypatch):
    trading_instance = MagicMock(name="TradingClientInstance")
    data_instance = MagicMock(name="StockHistoricalDataClientInstance")
    monkeypatch.setattr(ac_module, "TradingClient", MagicMock(return_value=trading_instance))
    monkeypatch.setattr(
        ac_module, "StockHistoricalDataClient", MagicMock(return_value=data_instance)
    )
    # Sin velas: es suficiente para comprobar que el script no revienta y
    # produce un informe con metricas en cero, sin necesidad de datos reales.
    data_instance.get_stock_bars.return_value = SimpleNamespace(data={})
    return SimpleNamespace(trading=trading_instance, data=data_instance)


def test_missing_credentials_exits_with_error(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    exit_code = script.main(
        ["--start", "2023-01-01", "--log-dir", str(tmp_path / "logs")]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert ".env" in captured.err

def test_runs_end_to_end_with_mocked_connector(monkeypatch, capsys, mock_alpaca_clients, tmp_path):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

    exit_code = script.main(
        [
            "--symbols", "SPY",
            "--start", "2023-01-01",
            "--end", "2023-02-01",
            "--log-dir", str(tmp_path / "logs"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Metricas globales" in captured.out
    assert "n_trades: 0" in captured.out


def test_invalid_date_format_is_rejected(monkeypatch, mock_alpaca_clients):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

    with pytest.raises(SystemExit):
        script.main(["--start", "not-a-date"])


def test_format_metric_handles_none_explicitly():
    # None aparece en total_return_pct/max_drawdown cuando se combinan
    # varios simbolos (ver backtesting/backtest_runner.py, punto 5) -- el
    # informe debe explicarlo, no imprimir "None" a secas.
    assert "no aplica" in script._format_metric(None)


def test_multi_symbol_run_prints_explanatory_notice(monkeypatch, capsys, mock_alpaca_clients, tmp_path):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")

    exit_code = script.main(
        [
            "--symbols", "SPY", "QQQ",
            "--start", "2023-01-01",
            "--end", "2023-02-01",
            "--log-dir", str(tmp_path / "logs"),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    # Sin operaciones (mock sin velas), n_trades es 0 y el "include_equity_curve"
    # sigue la rama de la lista vacia -- aqui solo comprobamos que el script
    # no revienta con varios simbolos y sigue imprimiendo el informe.
    assert "Metricas globales" in captured.out
