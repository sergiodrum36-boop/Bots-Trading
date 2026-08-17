"""Tests unitarios de AlpacaConnector.

Importante: NINGUNO de estos tests llama a la API real de Alpaca. Los
clientes de alpaca-py (`TradingClient`, `StockHistoricalDataClient`) se
sustituyen por mocks en cada test.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import AssetStatus

from connectors import alpaca_connector as ac_module
from connectors.alpaca_connector import AlpacaConnector, DEFAULT_SYMBOLS
from connectors.base_connector import (
    ConnectorAuthenticationError,
    ConnectorError,
    ConnectorUnavailableError,
    OrderSide,
    OrderStatus,
    SymbolNotFoundError,
    Timeframe,
)
from utils.retry import CircuitBreaker, CircuitState


# ----------------------------------------------------------------------
# Helpers / fixtures
# ----------------------------------------------------------------------
def _make_bar(
    timestamp: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1_000.0,
    trade_count: float = 10.0,
    vwap: float = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        trade_count=trade_count,
        vwap=vwap if vwap is not None else close,
    )


@pytest.fixture
def env_credentials(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret-key")


@pytest.fixture
def mock_clients(monkeypatch):
    """Sustituye TradingClient y StockHistoricalDataClient por mocks y
    devuelve las instancias mockeadas para configurarlas en cada test."""
    trading_client_instance = MagicMock(name="TradingClientInstance")
    data_client_instance = MagicMock(name="StockHistoricalDataClientInstance")

    trading_client_cls = MagicMock(return_value=trading_client_instance)
    data_client_cls = MagicMock(return_value=data_client_instance)

    monkeypatch.setattr(ac_module, "TradingClient", trading_client_cls)
    monkeypatch.setattr(ac_module, "StockHistoricalDataClient", data_client_cls)

    return SimpleNamespace(trading=trading_client_instance, data=data_client_instance)


@pytest.fixture
def connector(env_credentials, mock_clients):
    """Conector listo para usar, con reintentos rapidos para no ralentizar
    los tests (sigue respetando la logica real de reintentos/backoff)."""
    return AlpacaConnector(
        paper=True,
        max_retries=3,
        retry_wait_min=0.01,
        retry_wait_max=0.02,
    )


# ----------------------------------------------------------------------
# Autenticacion
# ----------------------------------------------------------------------
class TestAuthentication:
    def test_missing_credentials_raises_authentication_error(
        self, monkeypatch, mock_clients
    ):
        # `AlpacaConnector.__init__` llama a `load_dotenv(dotenv_path=None, ...)`
        # sin ruta explicita -- python-dotenv busca el `.env` real subiendo
        # desde la ubicacion de connectors/alpaca_connector.py (NO desde el
        # cwd del test), asi que en un checkout real con un `.env` de verdad
        # junto a trading_bot/, delenv() por si solo no basta: dotenv lo
        # volveria a rellenar. Se anula la llamada a load_dotenv() para que
        # el test sea fiable tanto en un sandbox limpio como en el checkout
        # real del usuario (visto fallar en la practica: "DID NOT RAISE").
        monkeypatch.setattr(ac_module, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

        with pytest.raises(ConnectorAuthenticationError):
            AlpacaConnector(paper=True)

    def test_missing_secret_key_only_raises_authentication_error(
        self, monkeypatch, mock_clients
    ):
        monkeypatch.setattr(ac_module, "load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setenv("APCA_API_KEY_ID", "some-key")
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

        with pytest.raises(ConnectorAuthenticationError):
            AlpacaConnector(paper=True)

    def test_init_success_reads_credentials_from_env(self, connector, mock_clients):
        # Si el fixture `connector` se construyo sin lanzar excepcion,
        # la autenticacion via variables de entorno funciono correctamente.
        assert connector is not None
        # Los clientes de alpaca-py se han instanciado (una vez cada uno).
        assert ac_module.TradingClient.call_count == 1
        assert ac_module.StockHistoricalDataClient.call_count == 1

    def test_default_symbols_are_liquid_examples(self):
        assert DEFAULT_SYMBOLS == ("SPY", "QQQ", "AAPL", "MSFT")


# ----------------------------------------------------------------------
# Velas historicas / recientes
# ----------------------------------------------------------------------
class TestBars:
    def test_get_historical_bars_normalizes_data(self, connector, mock_clients):
        bar1 = _make_bar(datetime(2026, 8, 10, tzinfo=timezone.utc), 100, 105, 99, 104)
        bar2 = _make_bar(datetime(2026, 8, 11, tzinfo=timezone.utc), 104, 108, 103, 107)
        bar_set = SimpleNamespace(data={"SPY": [bar1, bar2]})
        mock_clients.data.get_stock_bars.return_value = bar_set

        bars = connector.get_historical_bars(
            symbols="SPY",
            timeframe=Timeframe.DAY,
            start=datetime(2026, 8, 1, tzinfo=timezone.utc),
            end=datetime(2026, 8, 12, tzinfo=timezone.utc),
        )

        assert len(bars) == 2
        assert bars[0].symbol == "SPY"
        assert bars[0].close == 104.0
        assert bars[1].close == 107.0
        # Orden cronologico
        assert bars[0].timestamp < bars[1].timestamp

    def test_get_historical_bars_missing_symbol_logs_and_skips(
        self, connector, mock_clients
    ):
        bar_set = SimpleNamespace(data={})
        mock_clients.data.get_stock_bars.return_value = bar_set

        bars = connector.get_historical_bars(
            symbols="QQQ",
            timeframe=Timeframe.DAY,
            start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

        assert bars == []

    def test_get_recent_bars_returns_last_n_bars(self, connector, mock_clients):
        bars_raw = [
            _make_bar(datetime(2026, 8, day, tzinfo=timezone.utc), 100 + day, 101 + day, 99 + day, 100.5 + day)
            for day in range(1, 6)
        ]
        bar_set = SimpleNamespace(data={"AAPL": bars_raw})
        mock_clients.data.get_stock_bars.return_value = bar_set

        bars = connector.get_recent_bars("AAPL", timeframe=Timeframe.DAY, limit=3)

        assert len(bars) == 3
        assert bars[-1].timestamp == datetime(2026, 8, 5, tzinfo=timezone.utc)

    def test_get_recent_bars_does_not_pass_limit_to_alpaca(self, connector, mock_clients):
        """Regresion del bug detectado el 17/08/2026: la API de bars de
        Alpaca devuelve las velas en orden ASCENDENTE dentro de
        [start, end] y trunca en cuanto alcanza `limit` -- con la ventana
        amplia que usa `get_recent_bars` (necesaria para cubrir fines de
        semana/festivos), pasar `limit` a Alpaca hacia que devolviera las
        velas MAS ANTIGUAS del rango, no las mas recientes. El ciclo de
        paper trading en vivo llego a operar con velas de hace ~6 meses
        sin ningun error visible. `get_recent_bars` debe pedir el rango
        COMPLETO a Alpaca (limit=None en la peticion) y recortar el mismo,
        en Python, las ultimas N velas -- ver connectors/alpaca_connector.py.
        """
        bars_raw = [
            _make_bar(
                datetime(2026, 1, day, tzinfo=timezone.utc),
                100 + day, 101 + day, 99 + day, 100.5 + day,
            )
            for day in range(1, 32)  # 31 velas "disponibles" en el rango simulado
        ]
        bar_set = SimpleNamespace(data={"AAPL": bars_raw})
        mock_clients.data.get_stock_bars.return_value = bar_set

        bars = connector.get_recent_bars("AAPL", timeframe=Timeframe.DAY, limit=5)

        # La peticion real enviada a Alpaca no debe llevar un `limit` que
        # trunque la respuesta desde el principio del rango.
        request_sent = mock_clients.data.get_stock_bars.call_args[0][0]
        assert request_sent.limit is None

        # Y las 5 velas devueltas deben ser las MAS RECIENTES del rango
        # completo (27-31 de enero), no las 5 primeras (1-5 de enero) --
        # el recorte real ocurre en Python, sobre los datos completos.
        assert len(bars) == 5
        assert bars[0].timestamp == datetime(2026, 1, 27, tzinfo=timezone.utc)
        assert bars[-1].timestamp == datetime(2026, 1, 31, tzinfo=timezone.utc)

    def test_get_latest_bar_returns_single_normalized_bar(
        self, connector, mock_clients
    ):
        bar = _make_bar(datetime(2026, 8, 14, tzinfo=timezone.utc), 200, 202, 198, 201)
        mock_clients.data.get_stock_latest_bar.return_value = {"MSFT": bar}

        latest = connector.get_latest_bar("MSFT")

        assert latest.symbol == "MSFT"
        assert latest.close == 201.0

    def test_get_latest_bar_symbol_not_found_raises(self, connector, mock_clients):
        mock_clients.data.get_stock_latest_bar.return_value = {}

        with pytest.raises(SymbolNotFoundError):
            connector.get_latest_bar("NOPE")


# ----------------------------------------------------------------------
# Calendario de mercado / estado del mercado
# ----------------------------------------------------------------------
class TestMarketCalendar:
    def test_get_market_calendar_normalizes_sessions(self, connector, mock_clients):
        mock_clients.trading.get_calendar.return_value = [
            SimpleNamespace(
                date=date(2026, 8, 17),
                open=dt_time(9, 30),
                close=dt_time(16, 0),
            )
        ]

        sessions = connector.get_market_calendar(date(2026, 8, 17), date(2026, 8, 17))

        assert len(sessions) == 1
        assert sessions[0].session_date == date(2026, 8, 17)
        assert sessions[0].market_open == datetime(2026, 8, 17, 9, 30)
        assert sessions[0].market_close == datetime(2026, 8, 17, 16, 0)

    def test_is_market_open_true(self, connector, mock_clients):
        mock_clients.trading.get_clock.return_value = SimpleNamespace(is_open=True)
        assert connector.is_market_open() is True

    def test_is_market_open_false(self, connector, mock_clients):
        mock_clients.trading.get_clock.return_value = SimpleNamespace(is_open=False)
        assert connector.is_market_open() is False


# ----------------------------------------------------------------------
# Negociabilidad / halts
# ----------------------------------------------------------------------
class TestTradability:
    def test_is_symbol_tradable_true_for_active_tradable_asset(
        self, connector, mock_clients
    ):
        mock_clients.trading.get_asset.return_value = SimpleNamespace(
            tradable=True, status=AssetStatus.ACTIVE
        )
        assert connector.is_symbol_tradable("SPY") is True

    def test_is_symbol_tradable_false_for_inactive_asset(
        self, connector, mock_clients
    ):
        mock_clients.trading.get_asset.return_value = SimpleNamespace(
            tradable=True, status=AssetStatus.INACTIVE
        )
        assert connector.is_symbol_tradable("SPY") is False

    def test_is_symbol_tradable_defaults_to_false_when_lookup_fails(
        self, connector, mock_clients
    ):
        # Fallo no transitorio (p.ej. simbolo inexistente) -> no reintenta,
        # pero is_symbol_tradable no debe propagar la excepcion: la opcion
        # segura es devolver False.
        mock_clients.trading.get_asset.side_effect = ValueError("404 not found")
        assert connector.is_symbol_tradable("DOESNOTEXIST") is False


# ----------------------------------------------------------------------
# Reintentos / manejo de errores (sin llamadas reales, sin bucles infinitos)
# ----------------------------------------------------------------------
class TestRetryBehavior:
    def test_retries_on_transient_error_then_succeeds(self, connector, mock_clients):
        bar = _make_bar(datetime(2026, 8, 14, tzinfo=timezone.utc), 10, 11, 9, 10.5)
        mock_clients.data.get_stock_latest_bar.side_effect = [
            ConnectionError("timeout de red"),
            ConnectionError("timeout de red otra vez"),
            {"SPY": bar},
        ]

        latest = connector.get_latest_bar("SPY")

        assert latest.close == 10.5
        assert mock_clients.data.get_stock_latest_bar.call_count == 3

    def test_retry_exhausted_raises_unavailable_and_stops(
        self, connector, mock_clients
    ):
        mock_clients.data.get_stock_latest_bar.side_effect = ConnectionError(
            "red caida"
        )

        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")

        # Nunca reintenta mas alla de max_retries (3 en el fixture): no hay
        # bucle indefinido.
        assert mock_clients.data.get_stock_latest_bar.call_count == 3

    def test_non_retryable_error_fails_fast_without_retrying(
        self, connector, mock_clients
    ):
        mock_clients.data.get_stock_latest_bar.side_effect = ValueError(
            "parametros invalidos"
        )

        with pytest.raises(ConnectorError):
            connector.get_latest_bar("SPY")

        assert mock_clients.data.get_stock_latest_bar.call_count == 1

    def test_api_error_with_retryable_status_code_is_retried(
        self, connector, mock_clients, monkeypatch
    ):
        class FakeAPIError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        monkeypatch.setattr(ac_module, "APIError", FakeAPIError)

        bar = _make_bar(datetime(2026, 8, 14, tzinfo=timezone.utc), 10, 11, 9, 10.5)
        mock_clients.data.get_stock_latest_bar.side_effect = [
            FakeAPIError("rate limited", status_code=429),
            {"SPY": bar},
        ]

        latest = connector.get_latest_bar("SPY")

        assert latest.close == 10.5
        assert mock_clients.data.get_stock_latest_bar.call_count == 2

    def test_api_error_with_non_retryable_status_code_fails_fast(
        self, connector, mock_clients, monkeypatch
    ):
        class FakeAPIError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        monkeypatch.setattr(ac_module, "APIError", FakeAPIError)
        mock_clients.data.get_stock_latest_bar.side_effect = FakeAPIError(
            "no autorizado", status_code=401
        )

        with pytest.raises(ConnectorError):
            connector.get_latest_bar("SPY")

        assert mock_clients.data.get_stock_latest_bar.call_count == 1


# ----------------------------------------------------------------------
# Circuit breaker (fallos sostenidos a lo largo de varias llamadas,
# distinto de los reintentos de UNA llamada que cubre TestRetryBehavior)
# ----------------------------------------------------------------------
class _FakeClock:
    """Reloj controlable a mano, mismo patron que tests/test_retry.py."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestCircuitBreakerIntegration:
    def _make_connector(self, env_credentials, mock_clients, breaker):
        return AlpacaConnector(
            paper=True,
            max_retries=1,  # sin reintentos internos: cada llamada falla a la primera
            retry_wait_min=0.01,
            retry_wait_max=0.02,
            circuit_breaker=breaker,
        )

    def test_uses_a_default_circuit_breaker_when_none_is_injected(self, connector):
        assert connector._circuit_breaker.state == CircuitState.CLOSED

    def test_opens_after_consecutive_call_level_failures_and_rejects_without_hitting_api(
        self, env_credentials, mock_clients
    ):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60, clock=clock, name="test")
        connector = self._make_connector(env_credentials, mock_clients, breaker)
        mock_clients.data.get_stock_latest_bar.side_effect = ConnectionError("red caida")

        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")
        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")
        assert breaker.state == CircuitState.OPEN
        assert mock_clients.data.get_stock_latest_bar.call_count == 2

        # Tercera llamada: el circuito ya esta abierto, se rechaza SIN
        # llegar a invocar al cliente de Alpaca.
        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")
        assert mock_clients.data.get_stock_latest_bar.call_count == 2

    def test_non_retryable_errors_also_count_towards_the_circuit_breaker(
        self, env_credentials, mock_clients
    ):
        # Un error no transitorio (ej. autenticacion) no se reintenta, pero
        # SIGUE siendo un fallo de la llamada a efectos del circuit breaker.
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=clock, name="test")
        connector = self._make_connector(env_credentials, mock_clients, breaker)
        mock_clients.data.get_stock_latest_bar.side_effect = ValueError("parametros invalidos")

        with pytest.raises(ConnectorError):
            connector.get_latest_bar("SPY")
        assert breaker.state == CircuitState.OPEN

        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")
        assert mock_clients.data.get_stock_latest_bar.call_count == 1  # la 2a ni se intento

    def test_successful_call_resets_the_failure_count(self, env_credentials, mock_clients):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60, clock=clock, name="test")
        connector = self._make_connector(env_credentials, mock_clients, breaker)
        bar = _make_bar(datetime(2026, 8, 14, tzinfo=timezone.utc), 10, 11, 9, 10.5)
        mock_clients.data.get_stock_latest_bar.side_effect = [
            ConnectionError("red caida"),
            {"SPY": bar},
        ]

        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")
        assert breaker.consecutive_failures == 1

        latest = connector.get_latest_bar("SPY")
        assert latest.close == 10.5
        assert breaker.consecutive_failures == 0
        assert breaker.state == CircuitState.CLOSED

    def test_recovers_after_cooldown_via_half_open(self, env_credentials, mock_clients):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=clock, name="test")
        connector = self._make_connector(env_credentials, mock_clients, breaker)
        bar = _make_bar(datetime(2026, 8, 14, tzinfo=timezone.utc), 10, 11, 9, 10.5)
        mock_clients.data.get_stock_latest_bar.side_effect = [
            ConnectionError("red caida"),
            {"SPY": bar},
        ]

        with pytest.raises(ConnectorUnavailableError):
            connector.get_latest_bar("SPY")
        assert breaker.state == CircuitState.OPEN

        clock.advance(60)  # cooldown cumplido -> HALF_OPEN, se permite una llamada de prueba
        latest = connector.get_latest_bar("SPY")

        assert latest.close == 10.5
        assert breaker.state == CircuitState.CLOSED


# ----------------------------------------------------------------------
# Cuenta / posiciones
# ----------------------------------------------------------------------
def _make_order(
    order_id="order-1",
    client_order_id="bot-abc123",
    symbol="SPY",
    side="buy",
    status="filled",
    qty="10",
    filled_qty="10",
    filled_avg_price="123.45",
    submitted_at=None,
):
    return SimpleNamespace(
        id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        status=status,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        submitted_at=submitted_at or datetime(2026, 8, 14, tzinfo=timezone.utc),
    )


class TestAccountAndPositions:
    def test_get_account_normalizes_fields(self, connector, mock_clients):
        mock_clients.trading.get_account.return_value = SimpleNamespace(
            equity="10000.50", cash="5000.25", buying_power="20000.00"
        )

        account = connector.get_account()

        assert account.equity == 10000.50
        assert account.cash == 5000.25
        assert account.buying_power == 20000.00

    def test_get_open_positions_normalizes_list(self, connector, mock_clients):
        mock_clients.trading.get_all_positions.return_value = [
            SimpleNamespace(
                symbol="SPY",
                qty="10",
                avg_entry_price="400.0",
                market_value="4100.0",
                unrealized_pl="100.0",
            ),
            SimpleNamespace(
                symbol="AAPL",
                qty="5",
                avg_entry_price="150.0",
                market_value="740.0",
                unrealized_pl="-10.0",
            ),
        ]

        positions = connector.get_open_positions()

        assert len(positions) == 2
        assert positions[0].symbol == "SPY"
        assert positions[0].qty == 10.0
        assert positions[0].unrealized_pl == 100.0
        assert positions[1].symbol == "AAPL"
        assert positions[1].unrealized_pl == -10.0

    def test_get_open_positions_empty_list(self, connector, mock_clients):
        mock_clients.trading.get_all_positions.return_value = []
        assert connector.get_open_positions() == []


# ----------------------------------------------------------------------
# Envio de ordenes de mercado
# ----------------------------------------------------------------------
class TestSubmitMarketOrder:
    def test_submits_and_normalizes_result(self, connector, mock_clients):
        mock_clients.trading.submit_order.return_value = _make_order(
            order_id="broker-order-1",
            client_order_id="bot-xyz",
            symbol="SPY",
            side="buy",
            status="new",
            qty="10",
            filled_qty="0",
            filled_avg_price=None,
        )

        result = connector.submit_market_order(
            symbol="SPY", qty=10, side=OrderSide.BUY, client_order_id="bot-xyz"
        )

        assert result.broker_order_id == "broker-order-1"
        assert result.client_order_id == "bot-xyz"
        assert result.symbol == "SPY"
        assert result.side == OrderSide.BUY
        assert result.qty == 10.0
        assert result.status == OrderStatus.NEW
        assert result.filled_qty == 0.0
        assert result.filled_avg_price is None

        # La orden enviada al broker debe llevar el client_order_id exacto
        # que se le paso (idempotencia responsabilidad de execution/).
        sent_request = mock_clients.trading.submit_order.call_args.args[0]
        assert sent_request.client_order_id == "bot-xyz"

    def test_filled_order_status_and_sell_side_parsed(self, connector, mock_clients):
        mock_clients.trading.submit_order.return_value = _make_order(
            side="sell", status="filled", qty="5", filled_qty="5", filled_avg_price="99.9"
        )

        result = connector.submit_market_order(
            symbol="SPY", qty=5, side=OrderSide.SELL, client_order_id="bot-sell"
        )

        assert result.side == OrderSide.SELL
        assert result.status == OrderStatus.FILLED
        assert result.filled_avg_price == 99.9

    def test_status_parsed_defensively_from_enum_repr_string(
        self, connector, mock_clients
    ):
        # Segun la version del SDK, `order.status` puede llegar como texto
        # plano ("filled") o como la representacion de un Enum
        # ("OrderStatus.FILLED"); ambos deben normalizarse igual.
        mock_clients.trading.submit_order.return_value = _make_order(
            status="OrderStatus.FILLED"
        )

        result = connector.submit_market_order(
            symbol="SPY", qty=1, side=OrderSide.BUY, client_order_id="bot-1"
        )

        assert result.status == OrderStatus.FILLED

    def test_unmapped_status_defaults_to_pending(self, connector, mock_clients):
        mock_clients.trading.submit_order.return_value = _make_order(
            status="some_unexpected_status"
        )

        result = connector.submit_market_order(
            symbol="SPY", qty=1, side=OrderSide.BUY, client_order_id="bot-1"
        )

        assert result.status == OrderStatus.PENDING


# ----------------------------------------------------------------------
# Consulta de ordenes por client_order_id (idempotencia)
# ----------------------------------------------------------------------
class TestGetOrderByClientId:
    def test_returns_normalized_order_when_found(self, connector, mock_clients):
        mock_clients.trading.get_order_by_client_id.return_value = _make_order(
            client_order_id="bot-found"
        )

        result = connector.get_order_by_client_id("bot-found")

        assert result is not None
        assert result.client_order_id == "bot-found"

    def test_returns_none_when_not_found_404(self, connector, mock_clients):
        class FakeAPIError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        mock_clients.trading.get_order_by_client_id.side_effect = FakeAPIError(
            "order not found", status_code=404
        )

        result = connector.get_order_by_client_id("bot-missing")

        assert result is None

    def test_propagates_error_when_not_a_clean_404(self, connector, mock_clients):
        class FakeAPIError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        mock_clients.trading.get_order_by_client_id.side_effect = FakeAPIError(
            "no autorizado", status_code=401
        )

        # Un fallo distinto a "no existe" (ej. autenticacion) NUNCA debe
        # traducirse en None: hacerlo podria hacer que execution/
        # order_manager.py reenviara una orden que en realidad ya existe.
        with pytest.raises(ConnectorError):
            connector.get_order_by_client_id("bot-1")

    def test_propagates_error_on_persistent_network_failure(
        self, connector, mock_clients
    ):
        mock_clients.trading.get_order_by_client_id.side_effect = ConnectionError(
            "red caida"
        )

        with pytest.raises(ConnectorUnavailableError):
            connector.get_order_by_client_id("bot-1")
