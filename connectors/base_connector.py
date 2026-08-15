"""Interfaz abstracta para conectores de bróker/fuente de datos.

Principio de diseño (ver 02_Especificaciones_Tecnicas.md, seccion 2):
la capa de Estrategia y el resto del bot NUNCA hablan directamente con la
API de un broker concreto. Solo conocen esta interfaz. La implementacion
concreta (por ejemplo `AlpacaConnector`) es la unica que sabe traducir estos
metodos a llamadas reales contra Alpaca. Esto permite anadir en el futuro
un `CCXTConnector` (cripto) o un conector de forex implementando la misma
interfaz, sin tocar `strategy/`, `risk/` ni `execution/`.

Alcance del contrato: autenticacion, velas historicas (backtesting), datos
recientes/en tiempo real (paper trading), calendario de mercado y estado
de negociabilidad de un simbolo (halts) -- ver seccion 3 de las
especificaciones tecnicas -- mas cuenta, posiciones y ordenes de mercado
(seccion 6, "Modulo de Ejecucion"), anadidas al construir
`execution/order_manager.py`. Solo ordenes de MERCADO por ahora
(LimitOrderRequest queda para una iteracion futura si hace falta).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Sequence


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    """Estados normalizados de una orden, independientes del broker.

    `PENDING` es un cajon de sastre para cualquier estado intermedio del
    broker que no se haya mapeado explicitamente (ej. "accepted",
    "pending_new") -- se trata como "todavia no es un estado final", nunca
    como si ya estuviera ejecutada.
    """

    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    PENDING = "pending"


TERMINAL_ORDER_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}
)


class Timeframe(str, Enum):
    """Timeframes soportados, independientes del broker concreto.

    La estrategia de la Fase 1 usa velas diarias (ver especificaciones
    tecnicas, seccion 4), pero el conector no debe hardcodear ese supuesto:
    acepta el timeframe como parametro para poder bajar a intradia mas
    adelante sin cambiar la interfaz.
    """

    MINUTE = "1Min"
    FIVE_MINUTE = "5Min"
    FIFTEEN_MINUTE = "15Min"
    HOUR = "1Hour"
    DAY = "1Day"


@dataclass(frozen=True)
class OHLCVBar:
    """Vela normalizada, en un formato interno comun independiente del broker."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: float | None = None
    vwap: float | None = None


@dataclass(frozen=True)
class MarketSession:
    """Un dia de sesion del calendario de mercado (NYSE/NASDAQ)."""

    session_date: date
    market_open: datetime
    market_close: datetime


@dataclass(frozen=True)
class Account:
    """Estado de la cuenta, normalizado."""

    equity: float
    cash: float
    buying_power: float


@dataclass(frozen=True)
class Position:
    """Una posicion abierta, normalizada."""

    symbol: str
    qty: float  # positivo = posicion larga
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class OrderResult:
    """Resultado normalizado de una orden (recien enviada o consultada)."""

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: float
    status: OrderStatus
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: datetime


class ConnectorError(Exception):
    """Error base de cualquier conector de datos/broker."""


class ConnectorAuthenticationError(ConnectorError):
    """Credenciales ausentes, invalidas o rechazadas por el broker."""


class ConnectorUnavailableError(ConnectorError):
    """La fuente de datos/broker no respondio tras agotar los reintentos.

    Se distingue deliberadamente de errores de programacion o de datos
    invalidos: esta excepcion significa "lo hemos intentado varias veces
    de forma controlada y sigue fallando", nunca "hay que reintentar para
    siempre". Quien la capture (el orquestador) debe tratarlo como una
    senal para pausar operativa, no para reintentar en bucle.
    """


class SymbolNotFoundError(ConnectorError):
    """El simbolo solicitado no existe o no esta disponible en el broker."""


class BrokerConnector(ABC):
    """Contrato que debe implementar cualquier conector de datos de mercado."""

    # ------------------------------------------------------------------
    # Datos historicos (backtesting)
    # ------------------------------------------------------------------
    @abstractmethod
    def get_historical_bars(
        self,
        symbols: str | Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[OHLCVBar]:
        """Devuelve velas OHLCV historicas para uno o varios simbolos."""

    # ------------------------------------------------------------------
    # Datos recientes / "tiempo real" via polling (paper trading)
    # ------------------------------------------------------------------
    @abstractmethod
    def get_recent_bars(
        self,
        symbol: str,
        timeframe: Timeframe = Timeframe.DAY,
        limit: int = 100,
    ) -> list[OHLCVBar]:
        """Devuelve las ultimas `limit` velas cerradas de un simbolo."""

    @abstractmethod
    def get_latest_bar(self, symbol: str) -> OHLCVBar:
        """Devuelve la ultima vela disponible (la mas reciente) de un simbolo."""

    # ------------------------------------------------------------------
    # Calendario de mercado y estado de negociabilidad
    # ------------------------------------------------------------------
    @abstractmethod
    def get_market_calendar(
        self, start: date, end: date
    ) -> list[MarketSession]:
        """Devuelve las sesiones de mercado (apertura/cierre) entre dos fechas."""

    @abstractmethod
    def is_market_open(self) -> bool:
        """Indica si el mercado esta abierto en este momento."""

    @abstractmethod
    def is_symbol_tradable(self, symbol: str) -> bool:
        """Indica si el simbolo se puede negociar ahora mismo (activo y no halted).

        Nota: la deteccion de halts intradia por codigos de condicion de
        trade/quote no esta cubierta por esta version (requiere datos de
        streaming). Esta comprobacion se basa en el estado del activo que
        reporta el broker; ante duda, debe devolver False (opcion segura)
        en vez de asumir que se puede operar.
        """

    # ------------------------------------------------------------------
    # Cuenta, posiciones y ordenes (execution/order_manager.py)
    # ------------------------------------------------------------------
    @abstractmethod
    def get_account(self) -> Account:
        """Devuelve el estado actual de la cuenta (equity, cash, buying power)."""

    @abstractmethod
    def get_open_positions(self) -> list[Position]:
        """Devuelve todas las posiciones abiertas segun el broker (la fuente
        de verdad para reconciliar contra lo que el bot cree tener)."""

    @abstractmethod
    def submit_market_order(
        self, symbol: str, qty: float, side: OrderSide, client_order_id: str
    ) -> OrderResult:
        """Envia una orden de mercado. `client_order_id` es responsabilidad
        de quien llama (ver execution/order_manager.py para la generacion
        idempotente) -- este metodo no genera uno por su cuenta."""

    @abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        """Busca una orden por su client_order_id.

        Devuelve `None` UNICAMENTE cuando el broker confirma que esa orden
        no existe (ej. 404 limpio) -- NUNCA como resultado de un error de
        red/autenticacion/timeout, que debe propagarse como excepcion. Esta
        distincion es la base de la idempotencia en execution/
        order_manager.py: "no existe" y "no se pudo comprobar" no son lo
        mismo, y tratarlos igual podria acabar duplicando una orden real.
        """
