"""Conector de datos de mercado para Alpaca (acciones/ETFs de EE. UU.).

Implementa `BrokerConnector` (ver `base_connector.py`) usando el SDK oficial
`alpaca-py`. Referencia: 04_Guia_Referencias_Fuentes.md, seccion 1.

Decisiones de diseno de esta version (ver conversacion de arranque del
modulo, 2026-08-15):
  - Simbolos de ejemplo/pruebas: SPY, QQQ, AAPL, MSFT -- alta liquidez,
    spreads minimos, coherente con lo que ya usan las propias guias de
    Alpaca y con lo mencionado en 02_Especificaciones_Tecnicas.md (seccion 1).
    El conector en si es agnostico al simbolo: acepta cualquiera.
  - Datos "en tiempo real / recientes" (punto 3 del encargo): via POLLING
    REST (`get_recent_bars` / `get_latest_bar`), no streaming por
    WebSocket. Es la opcion mas simple y encaja con un orquestador tipo
    scheduler y una estrategia de timeframe diario (seccion 4 de las
    especificaciones). La interfaz (`BrokerConnector`) no impide anadir mas
    adelante un metodo de streaming si se baja a intradia.
  - Feed de datos: IEX por defecto (gratuito, disponible en cuentas de
    paper trading; ver 00_INDICE.md y la documentacion oficial de Alpaca).

Resiliencia (02_Especificaciones_Tecnicas.md, seccion 8): dos mecanismos
distintos y complementarios, ambos dentro de `_call_with_retry` (el punto
unico por el que pasa cualquier llamada a la API de Alpaca):
  - Reintentos acotados (tenacity, como antes): absorben un fallo
    TRANSITORIO dentro de UNA llamada (red, timeout, 5xx, 429). Backoff
    exponencial acotado, nunca reintento indefinido.
  - `CircuitBreaker` (utils/retry.py, `self._circuit_breaker`): protege
    contra fallos SOSTENIDOS a lo largo de VARIAS llamadas, incluso ya con
    reintentos agotados en cada una -- si `circuit_breaker_failure_
    threshold` llamadas seguidas fallan, deja de intentarlo durante
    `circuit_breaker_cooldown_seconds` (rechaza localmente con
    `CircuitBreakerOpenError`, reclasificado a `ConnectorUnavailableError`,
    sin golpear la API) en vez de seguir goteando peticiones contra un
    servicio caido. Una llamada con exito reinicia el contador. Es
    deliberadamente un mecanismo EN MEMORIA, sin persistir a disco (a
    diferencia del kill switch): no hace falta que sobreviva a un reinicio
    del proceso, solo evitar seguir golpeando la API DENTRO del mismo
    proceso mientras dura la caida.

Seguridad: las credenciales SIEMPRE se leen de variables de entorno
(`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`) via `.env` (python-dotenv).
Nunca se aceptan como literales en el codigo ni se loguean.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone

from dotenv import load_dotenv
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

try:
    from alpaca.common.exceptions import APIError
except ImportError:  # pragma: no cover - alpaca-py siempre debe estar instalado
    class APIError(Exception):  # type: ignore[no-redef]
        """Fallback si alpaca-py no expone APIError en esta version del SDK."""


from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame as AlpacaTimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest

from connectors.base_connector import (
    Account,
    BrokerConnector,
    ConnectorAuthenticationError,
    ConnectorError,
    ConnectorUnavailableError,
    MarketSession,
    OHLCVBar,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    SymbolNotFoundError,
    Timeframe,
)
from utils.retry import CircuitBreaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

# Simbolos de referencia para configuracion/tests de esta primera iteracion
# (ver docstring del modulo). No es una lista cerrada: cualquier metodo de
# este conector acepta cualquier simbolo valido de Alpaca.
DEFAULT_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "AAPL", "MSFT")

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_WAIT_MIN = 1.0  # segundos
DEFAULT_RETRY_WAIT_MAX = 10.0  # segundos

DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 60.0

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_TIMEFRAME_MAP: dict[Timeframe, AlpacaTimeFrame] = {
    Timeframe.MINUTE: AlpacaTimeFrame.Minute,
    Timeframe.FIVE_MINUTE: AlpacaTimeFrame(5, TimeFrameUnit.Minute),
    Timeframe.FIFTEEN_MINUTE: AlpacaTimeFrame(15, TimeFrameUnit.Minute),
    Timeframe.HOUR: AlpacaTimeFrame.Hour,
    Timeframe.DAY: AlpacaTimeFrame.Day,
}

# Alpaca puede devolver el status como Enum o como texto plano segun la
# version del SDK; se normaliza con .lower() antes de mirar este mapa.
_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "accepted_for_bidding": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "done_for_day": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}

# Margen (calendario) para pedir suficientes velas historicas y quedarnos
# con las ultimas `limit` tras filtrar fines de semana/festivos.
_LOOKBACK_PER_TIMEFRAME: dict[Timeframe, timedelta] = {
    Timeframe.MINUTE: timedelta(minutes=3),
    Timeframe.FIVE_MINUTE: timedelta(minutes=15),
    Timeframe.FIFTEEN_MINUTE: timedelta(minutes=45),
    Timeframe.HOUR: timedelta(hours=3),
    Timeframe.DAY: timedelta(days=2),
}


def _is_retryable_error(exc: BaseException) -> bool:
    """Solo son transitorios: red/timeout, 5xx y rate limit (429).

    Errores de autenticacion/permisos o peticiones invalidas (4xx salvo 429)
    NO se reintentan -- reintentar no los arregla y puede ocultar un bug real.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, APIError):
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        return status_code in _RETRYABLE_STATUS_CODES
    return False


def _lookback_window(timeframe: Timeframe, limit: int) -> timedelta:
    unit = _LOOKBACK_PER_TIMEFRAME[timeframe]
    # x1.6 de margen para cubrir fines de semana/festivos/horas sin sesion,
    # mas un colchon fijo pequeno.
    return unit * max(1, int(limit * 1.6)) + unit


def _combine_date_and_time(day: date, value: datetime | dt_time) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(day, value)


class AlpacaConnector(BrokerConnector):
    """Implementacion de `BrokerConnector` para Alpaca (paper o real).

    Args:
        paper: si True (por defecto), opera contra el sandbox de paper
            trading de Alpaca. Fase 1 de este proyecto SIEMPRE debe usar
            paper=True; pasar a False es una decision de Fase 2 que requiere
            el checklist de salida de fase (ver especificaciones tecnicas).
        feed: feed de datos de mercado ("iex" por defecto, gratuito). Se
            puede sobrescribir via variable de entorno APCA_DATA_FEED.
        max_retries / retry_wait_min / retry_wait_max: politica de
            reintentos ante fallos transitorios. Nunca hay reintento
            indefinido: como mucho `max_retries` intentos, con backoff
            exponencial acotado entre `retry_wait_min` y `retry_wait_max`
            segundos.
        circuit_breaker: instancia de `utils.retry.CircuitBreaker` a usar.
            Si no se indica, se crea una con `circuit_breaker_failure_
            threshold`/`circuit_breaker_cooldown_seconds` (o sus valores
            por defecto). Permite inyectar una instancia propia en tests
            (reloj falso) o compartir una entre varios conectores.
        circuit_breaker_failure_threshold / circuit_breaker_cooldown_seconds:
            solo se usan para construir el `CircuitBreaker` por defecto;
            se ignoran si se pasa `circuit_breaker` explicitamente.
        env_file: ruta opcional a un archivo .env distinto del por defecto
            (util en tests). Si no se indica, se usa el comportamiento
            estandar de python-dotenv (busca `.env` en el directorio actual).
    """

    def __init__(
        self,
        *,
        paper: bool = True,
        feed: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_wait_min: float = DEFAULT_RETRY_WAIT_MIN,
        retry_wait_max: float = DEFAULT_RETRY_WAIT_MAX,
        circuit_breaker: CircuitBreaker | None = None,
        circuit_breaker_failure_threshold: int = DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        circuit_breaker_cooldown_seconds: float = DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        env_file: str | None = None,
    ) -> None:
        load_dotenv(dotenv_path=env_file, override=False)

        api_key = os.getenv("APCA_API_KEY_ID")
        secret_key = os.getenv("APCA_API_SECRET_KEY")
        if not api_key or not secret_key:
            logger.error(
                "Faltan credenciales de Alpaca en el entorno "
                "(APCA_API_KEY_ID / APCA_API_SECRET_KEY)."
            )
            raise ConnectorAuthenticationError(
                "APCA_API_KEY_ID y APCA_API_SECRET_KEY deben estar definidas "
                "como variables de entorno (ver .env.example). Nunca deben "
                "escribirse en el codigo fuente."
            )

        self._feed = feed or os.getenv("APCA_DATA_FEED", "iex")
        self._paper = paper
        self._max_retries = max(1, max_retries)
        self._retry_wait_min = retry_wait_min
        self._retry_wait_max = retry_wait_max
        self._circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=circuit_breaker_failure_threshold,
            cooldown_seconds=circuit_breaker_cooldown_seconds,
            name="alpaca",
        )

        self._trading_client = TradingClient(api_key, secret_key, paper=paper)
        self._data_client = StockHistoricalDataClient(api_key, secret_key)

        logger.info(
            "AlpacaConnector inicializado (paper=%s, feed=%s, max_retries=%s, "
            "circuit_breaker_failure_threshold=%s, circuit_breaker_cooldown_seconds=%s)",
            paper,
            self._feed,
            self._max_retries,
            self._circuit_breaker.failure_threshold,
            self._circuit_breaker.cooldown_seconds,
        )

    # ------------------------------------------------------------------
    # Infraestructura de reintentos / logging
    # ------------------------------------------------------------------
    def _call_with_retry(self, description: str, func, *args, **kwargs):
        """Ejecuta `func(*args, **kwargs)` con reintentos acotados (via
        tenacity) y protegido por el circuit breaker de la instancia (ver
        docstring del modulo, seccion "Resiliencia").

        - Reintenta solo errores transitorios (`_is_retryable_error`): red,
          timeouts, 5xx y rate limit (429).
        - Backoff exponencial entre `retry_wait_min` y `retry_wait_max`
          segundos.
        - Nunca reintenta mas de `self._max_retries` veces en total (sin
          bucles indefinidos): `reraise=True` hace que, al agotar los
          intentos (o ante un error no transitorio), se relance la ultima
          excepcion tal cual en vez de envolverla en un `RetryError` opaco.
        - Cada intento fallido se registra en el log (WARNING) antes de
          reintentar; el fallo definitivo se registra como ERROR.
        - Toda la secuencia de reintentos (una llamada "logica") pasa por
          `self._circuit_breaker.call(...)`: si el circuito ya esta
          abierto (demasiadas llamadas seguidas fallidas), esta llamada se
          rechaza SIN intentar nada (ni siquiera el primer intento) --
          se reclasifica igual que agotar los reintentos
          (`ConnectorUnavailableError`), para que quien llama (`core/
          bot_engine.py`) no tenga que distinguir entre ambos casos.
        """
        attempt_counter = {"n": 0}

        def _log_before_sleep(retry_state) -> None:
            attempt_counter["n"] = retry_state.attempt_number
            exc = retry_state.outcome.exception()
            logger.warning(
                "%s: intento %s/%s fallido (%s: %s). Reintentando...",
                description,
                retry_state.attempt_number,
                self._max_retries,
                type(exc).__name__,
                exc,
            )

        retryer = Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=self._retry_wait_min,
                min=self._retry_wait_min,
                max=self._retry_wait_max,
            ),
            retry=retry_if_exception(_is_retryable_error),
            reraise=True,
            before_sleep=_log_before_sleep,
        )

        def _run_with_retries():
            return retryer(func, *args, **kwargs)

        try:
            return self._circuit_breaker.call(_run_with_retries)
        except CircuitBreakerOpenError as exc:
            logger.error(
                "%s: circuit breaker '%s' abierto tras %s fallo(s) seguido(s); "
                "llamada rechazada sin golpear la API: %s",
                description,
                self._circuit_breaker.name,
                self._circuit_breaker.consecutive_failures,
                exc,
            )
            raise ConnectorUnavailableError(f"{description}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - reclasificado a continuacion
            if not _is_retryable_error(exc):
                logger.error(
                    "%s: error no recuperable (%s: %s)",
                    description,
                    type(exc).__name__,
                    exc,
                )
                raise ConnectorError(f"{description}: {exc}") from exc

            logger.error(
                "%s: agotados %s intento(s), ultimo error: %s",
                description,
                self._max_retries,
                exc,
            )
            raise ConnectorUnavailableError(
                f"{description}: sin respuesta tras {self._max_retries} "
                f"intento(s): {exc}"
            ) from exc

    @staticmethod
    def _bar_to_ohlcv(symbol: str, bar) -> OHLCVBar:
        return OHLCVBar(
            symbol=symbol,
            timestamp=bar.timestamp,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
            trade_count=getattr(bar, "trade_count", None),
            vwap=getattr(bar, "vwap", None),
        )

    def _normalize_bar_set(
        self, bar_set, symbols: str | list[str]
    ) -> list[OHLCVBar]:
        # BarSet.data es un dict {simbolo: [Bar, ...]} en alpaca-py.
        data = getattr(bar_set, "data", bar_set)
        symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)

        bars: list[OHLCVBar] = []
        for symbol in symbol_list:
            raw_bars = data.get(symbol) if hasattr(data, "get") else None
            if not raw_bars:
                logger.warning(
                    "Sin velas para '%s' en el rango solicitado.", symbol
                )
                continue
            bars.extend(self._bar_to_ohlcv(symbol, bar) for bar in raw_bars)

        bars.sort(key=lambda b: (b.symbol, b.timestamp))
        return bars

    # ------------------------------------------------------------------
    # Datos historicos (backtesting)
    # ------------------------------------------------------------------
    def get_historical_bars(
        self,
        symbols: str | list[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[OHLCVBar]:
        alpaca_timeframe = _TIMEFRAME_MAP[timeframe]
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=alpaca_timeframe,
            start=start,
            end=end,
            limit=limit,
            feed=self._feed,
        )
        bar_set = self._call_with_retry(
            f"get_historical_bars(symbols={symbols}, timeframe={timeframe.value})",
            self._data_client.get_stock_bars,
            request,
        )
        return self._normalize_bar_set(bar_set, symbols)

    # ------------------------------------------------------------------
    # Datos recientes / "tiempo real" via polling REST (paper trading)
    # ------------------------------------------------------------------
    def get_recent_bars(
        self,
        symbol: str,
        timeframe: Timeframe = Timeframe.DAY,
        limit: int = 100,
    ) -> list[OHLCVBar]:
        end = datetime.now(timezone.utc)
        start = end - _lookback_window(timeframe, limit)
        bars = self.get_historical_bars(
            symbols=symbol, timeframe=timeframe, start=start, end=end, limit=limit
        )
        return bars[-limit:]

    def get_latest_bar(self, symbol: str) -> OHLCVBar:
        request = StockLatestBarRequest(symbol_or_symbols=symbol, feed=self._feed)
        response = self._call_with_retry(
            f"get_latest_bar({symbol})",
            self._data_client.get_stock_latest_bar,
            request,
        )
        bar = response.get(symbol) if hasattr(response, "get") else None
        if bar is None:
            raise SymbolNotFoundError(
                f"Sin datos recientes disponibles para el simbolo '{symbol}'."
            )
        return self._bar_to_ohlcv(symbol, bar)

    # ------------------------------------------------------------------
    # Calendario de mercado y estado de negociabilidad
    # ------------------------------------------------------------------
    def get_market_calendar(self, start: date, end: date) -> list[MarketSession]:
        request = GetCalendarRequest(start=start, end=end)
        calendar_days = self._call_with_retry(
            f"get_market_calendar({start}..{end})",
            self._trading_client.get_calendar,
            request,
        )
        sessions = [
            MarketSession(
                session_date=day.date,
                market_open=_combine_date_and_time(day.date, day.open),
                market_close=_combine_date_and_time(day.date, day.close),
            )
            for day in calendar_days
        ]
        return sessions

    def is_market_open(self) -> bool:
        clock = self._call_with_retry(
            "is_market_open", self._trading_client.get_clock
        )
        return bool(clock.is_open)

    def is_symbol_tradable(self, symbol: str) -> bool:
        try:
            asset = self._call_with_retry(
                f"is_symbol_tradable({symbol})",
                self._trading_client.get_asset,
                symbol,
            )
        except ConnectorError:
            # Ante cualquier duda (incluido "no encontrado" o fallo de red
            # tras agotar reintentos), la opcion segura es asumir que NO se
            # puede operar -- nunca lo contrario. La deteccion de halts
            # intradia por codigos de condicion de trade queda fuera de
            # esta version (requeriria datos de streaming).
            logger.warning(
                "No se pudo verificar el estado de '%s'; se asume no "
                "negociable por seguridad.",
                symbol,
            )
            return False

        status = getattr(asset, "status", None)
        status_ok = status is None or status == AssetStatus.ACTIVE
        tradable = bool(getattr(asset, "tradable", False))
        return tradable and status_ok

    # ------------------------------------------------------------------
    # Cuenta, posiciones y ordenes (execution/order_manager.py)
    # ------------------------------------------------------------------
    def get_account(self) -> Account:
        account = self._call_with_retry("get_account", self._trading_client.get_account)
        return Account(
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
        )

    def get_open_positions(self) -> list[Position]:
        positions = self._call_with_retry(
            "get_open_positions", self._trading_client.get_all_positions
        )
        return [
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
            )
            for p in positions
        ]

    def submit_market_order(
        self, symbol: str, qty: float, side: OrderSide, client_order_id: str
    ) -> OrderResult:
        alpaca_side = AlpacaOrderSide.BUY if side == OrderSide.BUY else AlpacaOrderSide.SELL
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        order = self._call_with_retry(
            f"submit_market_order({side.value} {qty} {symbol})",
            self._trading_client.submit_order,
            request,
        )
        return self._order_to_result(order)

    def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        try:
            order = self._call_with_retry(
                f"get_order_by_client_id({client_order_id})",
                self._trading_client.get_order_by_client_id,
                client_order_id,
            )
        except ConnectorError as exc:
            if self._is_not_found_error(exc):
                return None
            # Cualquier otro fallo (red, autenticacion, timeout...) se
            # propaga tal cual: NO se puede confirmar si la orden existe o
            # no, y devolver None aqui podria hacer que execution/
            # order_manager.py reenviara una orden que en realidad ya
            # existe (ver docstring de este metodo en base_connector.py).
            raise
        return self._order_to_result(order)

    @staticmethod
    def _is_not_found_error(exc: ConnectorError) -> bool:
        cause = exc.__cause__
        status_code = getattr(cause, "status_code", None) or getattr(cause, "code", None)
        return status_code == 404

    @staticmethod
    def _order_to_result(order) -> OrderResult:
        raw_status = str(getattr(order, "status", "")).lower().rsplit(".", maxsplit=1)[-1]
        status = _ORDER_STATUS_MAP.get(raw_status, OrderStatus.PENDING)
        raw_side = str(getattr(order, "side", "")).lower()
        side = OrderSide.BUY if raw_side.endswith("buy") else OrderSide.SELL

        filled_avg_price = getattr(order, "filled_avg_price", None)
        qty = getattr(order, "qty", None)
        filled_qty = getattr(order, "filled_qty", None)

        return OrderResult(
            broker_order_id=str(order.id),
            client_order_id=str(order.client_order_id),
            symbol=order.symbol,
            side=side,
            qty=float(qty) if qty is not None else 0.0,
            status=status,
            filled_qty=float(filled_qty) if filled_qty is not None else 0.0,
            filled_avg_price=float(filled_avg_price) if filled_avg_price is not None else None,
            submitted_at=order.submitted_at,
        )
