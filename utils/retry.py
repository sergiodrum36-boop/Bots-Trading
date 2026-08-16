"""Helpers reutilizables de reintentos y circuit breaker
(03_Estructura_Proyecto_Python.md; 02_Especificaciones_Tecnicas.md,
seccion 8: "Manejo de errores y resiliencia").

Dos utilidades independientes, pensadas para usarse juntas o por
separado:

  - `build_retryer()`: fabrica un `tenacity.Retrying` con la MISMA
    politica que ya usa `connectors/alpaca_connector.py` dentro de su
    propio metodo `_call_with_retry` (backoff exponencial acotado,
    numero maximo de intentos, solo reintenta errores transitorios,
    cada intento fallido queda registrado en el log antes de
    reintentar). Se extrae aqui para que un futuro conector (CCXT,
    forex -- ver 02_Especificaciones_Tecnicas.md, seccion 1) no tenga
    que reimplementar la misma logica desde cero.

    NOTA DELIBERADA (sigue vigente): `AlpacaConnector._call_with_retry`
    sigue sin usar este helper -- construye su propio `tenacity.Retrying`
    inline, con la misma politica. Es codigo ya probado en produccion
    (ver GitHub Actions) y migrarlo a `build_retryer()` es una decision
    aparte que conviene confirmar explicitamente antes de tocar un
    modulo estable, no colar de paso. Queda para una sesion futura.

  - `CircuitBreaker`: cubre la parte de la seccion 8 que NINGUN modulo
    implementaba todavia: "si varias llamadas consecutivas a la API
    fallan, dejar de intentar operar durante un periodo de enfriamiento
    y notificar, en lugar de seguir golpeando una API caida". Es un
    concepto distinto de los reintentos: los reintentos absorben un
    fallo TRANSITORIO dentro de una sola llamada; el circuit breaker
    protege contra fallos SOSTENIDOS a lo largo de varias llamadas
    (incluso ya con reintentos agotados en cada una), evitando que el
    bot siga golpeando una API caida en cada ciclo del orquestador.

    CONECTADO (actualizado): `connectors/alpaca_connector.py` ya lo usa
    dentro de `_call_with_retry` -- cada llamada a la API de Alpaca (tras
    agotar sus reintentos internos) pasa por
    `self._circuit_breaker.call(...)`; ver el docstring de ese modulo,
    seccion "Resiliencia". `core/bot_engine.py` no necesita saber nada de
    esto: un circuito abierto se le presenta igual que cualquier otro
    `ConnectorUnavailableError`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WAIT_MIN_SECONDS = 1.0
DEFAULT_WAIT_MAX_SECONDS = 10.0


def build_retryer(
    *,
    description: str,
    is_retryable: Callable[[BaseException], bool],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    wait_min_seconds: float = DEFAULT_WAIT_MIN_SECONDS,
    wait_max_seconds: float = DEFAULT_WAIT_MAX_SECONDS,
    logger_override: logging.Logger | None = None,
) -> Retrying:
    """Fabrica un `tenacity.Retrying` con la politica estandar del
    proyecto: backoff exponencial acotado entre `wait_min_seconds` y
    `wait_max_seconds`, maximo `max_attempts` intentos EN TOTAL (nunca
    reintento indefinido), solo reintenta si `is_retryable(exc)` devuelve
    True, y registra (WARNING) cada intento fallido antes de reintentar.

    `reraise=True`: al agotar los intentos, se relanza la ULTIMA
    excepcion tal cual (nunca un `RetryError` opaco que oculte la causa
    real) -- quien llama decide como reclasificarla, igual que hace
    `connectors/alpaca_connector.py._call_with_retry` al envolverla en
    `ConnectorError`/`ConnectorUnavailableError`.

    Uso:

        retryer = build_retryer(
            description="get_account",
            is_retryable=_is_retryable_error,
        )
        result = retryer(some_function, arg1, arg2)
    """
    if max_attempts < 1:
        raise ValueError("max_attempts debe ser >= 1")
    if wait_min_seconds < 0:
        raise ValueError("wait_min_seconds no puede ser negativo")
    if wait_max_seconds < wait_min_seconds:
        raise ValueError("wait_max_seconds debe ser >= wait_min_seconds")

    log = logger_override or logger

    def _log_before_sleep(retry_state) -> None:
        exc = retry_state.outcome.exception()
        log.warning(
            "%s: intento %s/%s fallido (%s: %s). Reintentando...",
            description,
            retry_state.attempt_number,
            max_attempts,
            type(exc).__name__,
            exc,
        )

    return Retrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(
            multiplier=wait_min_seconds, min=wait_min_seconds, max=wait_max_seconds
        ),
        retry=retry_if_exception(is_retryable),
        reraise=True,
        before_sleep=_log_before_sleep,
    )


class CircuitState(str, Enum):
    CLOSED = "closed"  # normal: las llamadas pasan
    OPEN = "open"  # fallos sostenidos: se rechazan llamadas sin intentarlas
    HALF_OPEN = "half_open"  # cooldown cumplido: se permite UNA llamada de prueba


class CircuitBreakerOpenError(Exception):
    """El circuit breaker esta abierto: la llamada se rechaza localmente
    (sin golpear la API) porque hubo `failure_threshold` fallos
    consecutivos y el periodo de enfriamiento (`cooldown_seconds`)
    todavia no ha pasado."""


@dataclass
class CircuitBreaker:
    """Protege contra fallos SOSTENIDOS a lo largo de varias llamadas
    (02_Especificaciones_Tecnicas.md, seccion 8). No sustituye a los
    reintentos de `build_retryer` -- se complementan: los reintentos
    absorben un fallo puntual DENTRO de una llamada, el circuit breaker
    evita repetir llamadas cuando ya se ha demostrado que la API esta
    caida de forma sostenida.

    Estados (ver `CircuitState`): CLOSED (normal) -> tras
    `failure_threshold` fallos CONSECUTIVOS -> OPEN (rechaza todo sin
    intentarlo) -> tras `cooldown_seconds` -> HALF_OPEN (permite UNA
    llamada de prueba) -> si tiene exito, vuelve a CLOSED y se reinicia
    el contador; si falla, vuelve a OPEN y se reinicia el cooldown.

    Uso tipico:

        breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=60)
        try:
            result = breaker.call(connector.get_account)
        except CircuitBreakerOpenError:
            # No se intento la llamada: la API se sigue considerando caida.
            ...

    No es seguro para hilos concurrentes (no hace falta: el orquestador
    de este proyecto es de un solo hilo, ver core/bot_engine.py).
    `clock` es inyectable para tests deterministas (por defecto,
    `time.monotonic`, inmune a cambios de la hora del sistema).
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    name: str = "default"
    clock: Callable[[], float] = field(default=time.monotonic)

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _opened_at: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold debe ser >= 1")
        if self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds debe ser > 0")

    @property
    def state(self) -> CircuitState:
        """Estado EXTERNO visible: si el estado interno es OPEN pero ya
        paso el cooldown, se reporta HALF_OPEN (aunque internamente no se
        muta nada hasta que se intenta de verdad una llamada via `call`)."""
        if self._state == CircuitState.OPEN and self._cooldown_elapsed():
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _cooldown_elapsed(self) -> bool:
        return self._opened_at is not None and (
            self.clock() - self._opened_at >= self.cooldown_seconds
        )

    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Ejecuta `func(*args, **kwargs)` si el circuito lo permite.

        Lanza `CircuitBreakerOpenError` SIN llamar a `func` si el
        circuito esta abierto y el cooldown no ha pasado todavia."""
        current_state = self.state

        if current_state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker '{self.name}' abierto tras "
                f"{self._consecutive_failures} fallo(s) consecutivo(s); "
                f"en cooldown ({self.cooldown_seconds}s)."
            )

        try:
            result = func(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            self._record_success()
            return result

    def _record_success(self) -> None:
        if self._state != CircuitState.CLOSED:
            logger.info("Circuit breaker '%s': recuperado, vuelve a CLOSED.", self.name)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        # Si `self._state` sigue siendo OPEN en este punto, significa que
        # la llamada que acaba de fallar era la de prueba de HALF_OPEN
        # (una llamada OPEN "de verdad", sin cooldown cumplido, nunca
        # llega aqui porque `call()` la rechaza antes de invocar `func`).
        was_half_open_trial = self._state == CircuitState.OPEN
        if was_half_open_trial or self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self.clock()
            logger.error(
                "Circuit breaker '%s': ABIERTO tras %s fallo(s) consecutivo(s). "
                "Se rechazaran llamadas durante %ss.",
                self.name,
                self._consecutive_failures,
                self.cooldown_seconds,
            )

    def reset(self) -> None:
        """Reinicio manual explicito (ej. tras confirmar que la API ya
        responde con normalidad) -- vuelve a CLOSED y pone a cero el
        contador de fallos consecutivos."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
