"""Tests unitarios de execution/order_manager.py.

Ninguno de estos tests llama a ninguna API real ni espera tiempo real: se
usa un `FakeOrderConnector` (doble de pruebas de `BrokerConnector`) cuyo
comportamiento se controla explicitamente en cada test, y
`fill_poll_wait_seconds=0` para no ralentizar la suite.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from connectors.base_connector import (
    BrokerConnector,
    ConnectorError,
    MarketSession,
    OrderResult,
    OrderSide,
    OrderStatus,
    Timeframe,
)
from execution.order_manager import (
    DuplicateOrderPreventedError,
    ExecutionRequest,
    KillSwitchActiveError,
    OrderManager,
    OrderRejectedError,
    make_client_order_id,
)
from risk.kill_switch import KillSwitch, KillSwitchConfig


# ----------------------------------------------------------------------
# Doble de pruebas de BrokerConnector
# ----------------------------------------------------------------------
class FakeOrderConnector(BrokerConnector):
    """Conector de pruebas centrado en ordenes: el comportamiento de
    `get_order_by_client_id` se controla encolando respuestas (consumidas
    en orden en cada llamada, tanto para la consulta inicial de
    idempotencia como para el polling posterior de `_await_fill`), y el de
    `submit_market_order` fijando un resultado o un error."""

    def __init__(self):
        self.submitted: list = []
        self._get_order_responses: list = []
        self._submit_result: OrderResult | None = None
        self._submit_error: Exception | None = None

    def queue_get_order_response(self, response) -> None:
        self._get_order_responses.append(response)

    def set_submit_result(self, result: OrderResult) -> None:
        self._submit_result = result

    def set_submit_error(self, exc: Exception) -> None:
        self._submit_error = exc

    def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        if not self._get_order_responses:
            return None
        response = self._get_order_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def submit_market_order(self, symbol, qty, side, client_order_id) -> OrderResult:
        self.submitted.append(
            {"symbol": symbol, "qty": qty, "side": side, "client_order_id": client_order_id}
        )
        if self._submit_error is not None:
            raise self._submit_error
        assert self._submit_result is not None, "test no configuro submit_result"
        return self._submit_result

    # --- resto de la interfaz: no se usa en estos tests ---
    def get_historical_bars(self, symbols, timeframe, start, end=None, limit=None):
        raise NotImplementedError

    def get_recent_bars(self, symbol, timeframe=Timeframe.DAY, limit=100):
        raise NotImplementedError

    def get_latest_bar(self, symbol):
        raise NotImplementedError

    def get_market_calendar(self, start: date, end: date) -> list[MarketSession]:
        raise NotImplementedError

    def is_market_open(self) -> bool:
        raise NotImplementedError

    def is_symbol_tradable(self, symbol: str) -> bool:
        raise NotImplementedError

    def get_account(self):
        raise NotImplementedError

    def get_open_positions(self):
        raise NotImplementedError


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _order(
    status: OrderStatus,
    symbol="SPY",
    side=OrderSide.BUY,
    qty=10.0,
    filled_qty=None,
    client_order_id="bot-test",
    broker_order_id="broker-1",
) -> OrderResult:
    return OrderResult(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        status=status,
        filled_qty=qty if filled_qty is None else filled_qty,
        filled_avg_price=100.0 if status == OrderStatus.FILLED else None,
        submitted_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )


def _request(
    symbol="SPY",
    side=OrderSide.BUY,
    qty=10.0,
    is_opening_position=True,
    idempotency_key="SPY-2026-08-14-buy",
) -> ExecutionRequest:
    return ExecutionRequest(
        symbol=symbol,
        side=side,
        qty=qty,
        is_opening_position=is_opening_position,
        idempotency_key=idempotency_key,
    )


@pytest.fixture
def connector() -> FakeOrderConnector:
    return FakeOrderConnector()


@pytest.fixture
def kill_switch(tmp_path) -> KillSwitch:
    """Kill switch fresco, sin disparar, sin bloquear aperturas."""
    return KillSwitch(
        config=KillSwitchConfig(),
        state_file=tmp_path / "kill_switch_state.json",
        clock=lambda: datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def blocked_kill_switch(tmp_path) -> KillSwitch:
    """Kill switch que bloquea APERTURAS (conexion caida) sin estar
    formalmente 'disparado' -- suficiente para probar el gate de
    can_open_new_position() sin arrastrar el umbral completo de perdidas."""
    ks = KillSwitch(
        config=KillSwitchConfig(),
        state_file=tmp_path / "kill_switch_state.json",
        clock=lambda: datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    )
    ks.record_connection_lost()
    return ks


@pytest.fixture
def manager(connector, kill_switch) -> OrderManager:
    return OrderManager(
        connector=connector,
        kill_switch=kill_switch,
        fill_poll_attempts=5,
        fill_poll_wait_seconds=0,
    )


# ----------------------------------------------------------------------
# Validacion basica
# ----------------------------------------------------------------------
class TestValidation:
    def test_constructor_rejects_non_positive_fill_poll_attempts(
        self, connector, kill_switch
    ):
        with pytest.raises(ValueError):
            OrderManager(connector, kill_switch, fill_poll_attempts=0)

    def test_constructor_rejects_negative_wait_seconds(self, connector, kill_switch):
        with pytest.raises(ValueError):
            OrderManager(connector, kill_switch, fill_poll_wait_seconds=-1)

    def test_non_positive_qty_raises_value_error(self, manager, connector):
        with pytest.raises(ValueError):
            manager.execute(_request(qty=0))
        with pytest.raises(ValueError):
            manager.execute(_request(qty=-5))
        assert connector.submitted == []


# ----------------------------------------------------------------------
# Kill switch: bloquea aperturas, nunca cierres
# ----------------------------------------------------------------------
class TestKillSwitchGate:
    def test_blocks_opening_new_position(self, connector, blocked_kill_switch):
        manager = OrderManager(
            connector, blocked_kill_switch, fill_poll_attempts=3, fill_poll_wait_seconds=0
        )

        with pytest.raises(KillSwitchActiveError):
            manager.execute(_request(is_opening_position=True))

        assert connector.submitted == []

    def test_does_not_block_closing_existing_position(self, connector, blocked_kill_switch):
        manager = OrderManager(
            connector, blocked_kill_switch, fill_poll_attempts=3, fill_poll_wait_seconds=0
        )
        connector.queue_get_order_response(None)  # sin orden previa
        connector.set_submit_result(_order(OrderStatus.FILLED, side=OrderSide.SELL))

        result = manager.execute(
            _request(side=OrderSide.SELL, is_opening_position=False)
        )

        assert result.status == OrderStatus.FILLED
        assert len(connector.submitted) == 1


# ----------------------------------------------------------------------
# Idempotencia
# ----------------------------------------------------------------------
class TestIdempotency:
    def test_deterministic_client_order_id(self):
        first = make_client_order_id("SPY-2026-08-14-buy")
        second = make_client_order_id("SPY-2026-08-14-buy")
        different = make_client_order_id("SPY-2026-08-15-buy")

        assert first == second
        assert first != different
        assert first.startswith("bot-")
        assert len(first) <= 48

    def test_reuses_existing_order_instead_of_resubmitting(self, manager, connector):
        existing = _order(OrderStatus.FILLED, client_order_id="bot-existing")
        connector.queue_get_order_response(existing)

        result = manager.execute(_request())

        assert result == existing
        assert connector.submitted == []

    def test_duplicate_prevented_when_existence_cannot_be_confirmed(
        self, manager, connector
    ):
        connector.queue_get_order_response(ConnectorError("fallo de red al consultar"))

        with pytest.raises(DuplicateOrderPreventedError):
            manager.execute(_request())

        assert connector.submitted == []


# ----------------------------------------------------------------------
# Envio y seguimiento hasta estado final
# ----------------------------------------------------------------------
class TestFillPolling:
    def test_submits_when_no_existing_order_found(self, manager, connector):
        connector.queue_get_order_response(None)
        connector.set_submit_result(_order(OrderStatus.FILLED))

        result = manager.execute(_request())

        assert result.status == OrderStatus.FILLED
        assert len(connector.submitted) == 1
        assert connector.submitted[0]["symbol"] == "SPY"
        assert connector.submitted[0]["client_order_id"] == make_client_order_id(
            "SPY-2026-08-14-buy"
        )

    def test_polls_through_intermediate_statuses_until_filled(self, manager, connector):
        connector.queue_get_order_response(None)  # sin orden previa
        connector.set_submit_result(_order(OrderStatus.NEW))
        connector.queue_get_order_response(_order(OrderStatus.PARTIALLY_FILLED))
        connector.queue_get_order_response(_order(OrderStatus.FILLED))

        result = manager.execute(_request())

        assert result.status == OrderStatus.FILLED
        assert len(connector.submitted) == 1

    def test_rejected_order_raises_and_stops_polling(self, manager, connector):
        connector.queue_get_order_response(None)
        connector.set_submit_result(_order(OrderStatus.REJECTED))

        with pytest.raises(OrderRejectedError):
            manager.execute(_request())

    def test_gives_up_after_bounded_attempts_without_infinite_loop(self, connector, kill_switch):
        manager = OrderManager(
            connector, kill_switch, fill_poll_attempts=3, fill_poll_wait_seconds=0
        )
        connector.queue_get_order_response(None)  # sin orden previa
        connector.set_submit_result(_order(OrderStatus.NEW))
        # Nunca llega a un estado final: 3 respuestas de poll, todas NEW.
        connector.queue_get_order_response(_order(OrderStatus.NEW))
        connector.queue_get_order_response(_order(OrderStatus.NEW))
        connector.queue_get_order_response(_order(OrderStatus.NEW))

        result = manager.execute(_request())

        # No lanza excepcion: se devuelve el ultimo estado conocido para
        # que el orquestador le haga seguimiento en el proximo ciclo.
        assert result.status == OrderStatus.NEW
        assert len(connector.submitted) == 1
