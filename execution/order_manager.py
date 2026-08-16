"""Módulo de Ejecución: traduce una señal + tamaño de posición ya
calculados en una orden concreta contra el conector
(02_Especificaciones_Tecnicas.md, sección 6).

Responsabilidades (y límites) de este módulo:
  - Construye y envía órdenes de MERCADO (LimitOrderRequest queda para una
    iteración futura si hace falta).
  - IDEMPOTENCIA: genera un `client_order_id` determinista a partir de una
    clave que elige quien llama (`idempotency_key`); antes de reenviar,
    consulta primero si esa orden ya existe en el broker, para no
    duplicar. Si no se puede determinar con certeza si la orden ya existe
    (fallo al consultar, no un "no existe" limpio), NO se envía una orden
    nueva -- la opción segura es abortar y dejar que quien orquesta decida
    (reintentar más tarde, alertar), nunca arriesgarse a duplicar una
    orden real con dinero (aunque sea simulado) de por medio.
  - Confirma el estado final de la orden (filled/partially_filled/
    rejected/canceled) antes de dar la operación por completada, con un
    número ACOTADO de consultas de seguimiento (nunca espera indefinidamente).
  - Registra (log) cualquier discrepancia entre lo pedido y lo ejecutado
    (ej. fill parcial).

Lo que este módulo NO hace:
  - No calcula tamaño de posición (risk/position_sizing.py) ni stop-loss
    (risk/stop_loss.py) -- los recibe ya calculados.
  - No decide SI operar por cuenta propia; solo consulta al kill switch
    como gate antes de ABRIR una posición nueva.
  - No reconcilia el estado completo de todas las posiciones contra el
    broker de forma periódica -- eso es responsabilidad de
    core/bot_engine.py (el orquestador), que llama a
    `KillSwitch.check_position_discrepancy()` una vez por ciclo, antes de
    evaluar ningún símbolo (ver `BotEngine._check_position_discrepancy`).
  - No registra el resultado (P&L) en el kill switch -- `record_trade_result`
    se llama cuando se conoce el resultado final de una posición CERRADA
    (responsabilidad de quien orquesta, que es quien sabe a qué precio se
    abrió originalmente), no en este módulo operación a operación.

Decisión de diseño importante: el kill switch bloquea ABRIR posiciones
nuevas (BUY que abre), pero NO bloquea CERRAR posiciones existentes (SELL
que cierra) -- si el kill switch se disparó, seguir permitiendo salir de
una posición abierta es más seguro que dejar al usuario atrapado sin poder
reducir riesgo. Por eso `ExecutionRequest` lleva un flag explícito
`is_opening_position` en vez de derivarlo solo de `side`.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from connectors.base_connector import (
    TERMINAL_ORDER_STATUSES,
    BrokerConnector,
    ConnectorError,
    OrderResult,
    OrderSide,
    OrderStatus,
)
from risk.kill_switch import KillSwitch

logger = logging.getLogger(__name__)

DEFAULT_FILL_POLL_ATTEMPTS = 5
DEFAULT_FILL_POLL_WAIT_SECONDS = 2.0


class OrderRejectedError(Exception):
    """La orden fue rechazada por el broker."""


class KillSwitchActiveError(Exception):
    """No se puede abrir una posición nueva: el kill switch está activo, se
    alcanzó el límite de operaciones del día, o la conexión está caída."""


class DuplicateOrderPreventedError(Exception):
    """Se evitó reenviar una orden porque no se pudo confirmar con certeza
    si ya existía en el broker (fallo al consultar su estado)."""


@dataclass(frozen=True)
class ExecutionRequest:
    """Todo lo que `OrderManager` necesita para ejecutar UNA operación. El
    tamaño de posición y el motivo ya vienen calculados por `risk/` y
    `strategy/` respectivamente -- este módulo no los recalcula.
    """

    symbol: str
    side: OrderSide
    qty: float
    is_opening_position: bool
    idempotency_key: str  # ej. f"{symbol}-{signal.timestamp.isoformat()}-{side.value}"


def make_client_order_id(idempotency_key: str) -> str:
    """`client_order_id` determinista a partir de `idempotency_key`: la
    MISMA clave siempre produce el MISMO client_order_id, para que
    reintentar la misma petición (tras un timeout, un reinicio, etc.)
    permita detectar que ya se envió antes. Alpaca limita
    `client_order_id` a 48 caracteres, así que se usa un hash corto en vez
    de la clave en crudo.
    """
    digest = uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex
    return f"bot-{digest[:40]}"


class OrderManager:
    def __init__(
        self,
        connector: BrokerConnector,
        kill_switch: KillSwitch,
        fill_poll_attempts: int = DEFAULT_FILL_POLL_ATTEMPTS,
        fill_poll_wait_seconds: float = DEFAULT_FILL_POLL_WAIT_SECONDS,
    ) -> None:
        if fill_poll_attempts < 1:
            raise ValueError("fill_poll_attempts debe ser >= 1")
        if fill_poll_wait_seconds < 0:
            raise ValueError("fill_poll_wait_seconds no puede ser negativo")

        self._connector = connector
        self._kill_switch = kill_switch
        self._fill_poll_attempts = fill_poll_attempts
        self._fill_poll_wait_seconds = fill_poll_wait_seconds

    def execute(self, request: ExecutionRequest) -> OrderResult:
        if request.qty <= 0:
            raise ValueError(f"qty debe ser positivo (recibido: {request.qty})")

        if request.is_opening_position and not self._kill_switch.can_open_new_position():
            raise KillSwitchActiveError(
                f"No se abre posicion nueva en {request.symbol}: kill switch "
                f"activo, limite diario de operaciones alcanzado, o conexion caida."
            )

        client_order_id = make_client_order_id(request.idempotency_key)

        existing = self._find_existing_order(client_order_id)
        if existing is not None:
            logger.info(
                "%s: ya existe una orden con client_order_id=%s "
                "(idempotency_key=%s); no se reenvia, se usa la existente.",
                request.symbol,
                client_order_id,
                request.idempotency_key,
            )
            order = existing
        else:
            order = self._submit(request, client_order_id)

        final_order = self._await_fill(order)
        self._log_outcome(request, final_order)
        return final_order

    def _find_existing_order(self, client_order_id: str) -> OrderResult | None:
        try:
            return self._connector.get_order_by_client_id(client_order_id)
        except ConnectorError as exc:
            # No se pudo determinar con certeza si la orden ya existia. La
            # opcion segura es NO enviar una nueva (podria duplicarla) --
            # se propaga como excepcion propia para que quien orquesta
            # decida el siguiente paso, en vez de asumir en silencio que
            # no existia.
            raise DuplicateOrderPreventedError(
                f"No se pudo confirmar si ya existia una orden con "
                f"client_order_id={client_order_id}: {exc}. Por seguridad, "
                f"no se envia una orden nueva."
            ) from exc

    def _submit(self, request: ExecutionRequest, client_order_id: str) -> OrderResult:
        logger.info(
            "Enviando orden de mercado: %s %s x%s (client_order_id=%s)",
            request.side.value,
            request.symbol,
            request.qty,
            client_order_id,
        )
        try:
            return self._connector.submit_market_order(
                symbol=request.symbol,
                qty=request.qty,
                side=request.side,
                client_order_id=client_order_id,
            )
        except ConnectorError as exc:
            logger.error("Fallo al enviar la orden de %s: %s", request.symbol, exc)
            raise

    def _await_fill(self, order: OrderResult) -> OrderResult:
        current = order
        for attempt in range(1, self._fill_poll_attempts + 1):
            if current.status in TERMINAL_ORDER_STATUSES:
                break
            logger.info(
                "Orden %s (%s) sin estado final todavia (%s); consulta %s/%s...",
                current.broker_order_id,
                current.symbol,
                current.status.value,
                attempt,
                self._fill_poll_attempts,
            )
            if self._fill_poll_wait_seconds > 0:
                time.sleep(self._fill_poll_wait_seconds)
            refreshed = self._connector.get_order_by_client_id(current.client_order_id)
            if refreshed is not None:
                current = refreshed

        if current.status not in TERMINAL_ORDER_STATUSES:
            logger.warning(
                "Orden %s (%s) sigue sin estado final tras %s consultas "
                "(estado: %s); se devuelve tal cual -- requiere seguimiento "
                "en el proximo ciclo del orquestador.",
                current.broker_order_id,
                current.symbol,
                self._fill_poll_attempts,
                current.status.value,
            )

        if current.status == OrderStatus.REJECTED:
            raise OrderRejectedError(
                f"Orden {current.broker_order_id} ({current.symbol}) fue "
                f"rechazada por el broker."
            )

        return current

    @staticmethod
    def _log_outcome(request: ExecutionRequest, order: OrderResult) -> None:
        if order.status == OrderStatus.FILLED and order.filled_qty != request.qty:
            logger.warning(
                "%s: discrepancia entre cantidad pedida (%s) y ejecutada "
                "(%s) en la orden %s.",
                request.symbol,
                request.qty,
                order.filled_qty,
                order.broker_order_id,
            )
        elif order.status == OrderStatus.PARTIALLY_FILLED:
            logger.warning(
                "%s: la orden %s quedo PARCIALMENTE ejecutada (%s de %s).",
                request.symbol,
                order.broker_order_id,
                order.filled_qty,
                request.qty,
            )
        elif order.status == OrderStatus.CANCELED:
            logger.warning(
                "%s: la orden %s fue CANCELADA (por el broker o por "
                "vencimiento del time_in_force).",
                request.symbol,
                order.broker_order_id,
            )
