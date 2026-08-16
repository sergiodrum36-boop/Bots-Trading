"""Orquestador / bucle principal (02_Especificaciones_Tecnicas.md, sección 2
y 03_Estructura_Proyecto_Python.md). Ata `connectors/` -> `strategy/` ->
`risk/` -> `execution/`, y da entrada a `risk/kill_switch.py` para las
comprobaciones de seguridad, tal como describe el diagrama de arquitectura
por capas de las especificaciones.

Decisiones de diseño de esta primera versión (léase antes de tocar este
módulo):

  1. MODELO DE INVOCACIÓN: `BotEngine.run_cycle()` procesa UN ciclo completo
     (todos los símbolos configurados) y termina -- no lleva un bucle
     interno con `sleep()`. Encaja con el timeframe diario de la estrategia
     (sección 4 de las especificaciones): la idea es que un programador de
     tareas EXTERNO (Task Scheduler en Windows, cron en Linux/Mac) invoque
     `run_cycle()` una vez al día, poco después de la apertura del mercado
     (ver `scripts/run_paper_trading.py`, todavía no construido). Esto evita
     tener que mantener un proceso Python corriendo 24/7 y gestionar su
     apagado ordenado -- mucho más simple y seguro para una primera versión,
     sobre todo en el entorno de un usuario sin experiencia manteniendo
     procesos en segundo plano. Si en el futuro se baja a un timeframe
     intradía, este método puede reutilizarse dentro de un bucle con
     `sleep()` sin cambiar su lógica interna.

  2. ESTADO DE POSICIONES: este ciclo NO usa la base de datos para decidir
     si abrir o cerrar una posición -- en cada ciclo se pregunta a
     `connector.get_open_positions()` y esa respuesta se trata como la
     verdad, aunque ya exista `persistence/` (ver más abajo). Es la opción
     más segura: evita decidir a partir de un estado local que podría
     haberse quedado desincronizado. El `TradeStore` de `persistence/` es
     SOLO un registro de auditoría de lo que el bot HIZO, nunca una entrada
     para lo que el bot DEBE hacer.

     COMPROBACIÓN DE DISCREPANCIA (sección 5.4, `KillSwitch.
     check_position_discrepancy`): SÍ se llama desde aquí, una vez por
     ciclo, justo después de obtener cuenta/posiciones del bróker y antes
     de evaluar ningún símbolo (ver `_check_position_discrepancy`).
     Compara lo que el bot CREE tener abierto según `self._store.
     get_expected_open_positions()` (su propio registro de auditoría)
     contra lo que reporta el bróker -- si no coinciden, dispara el kill
     switch y el ciclo se aborta ENTERO (igual que una pérdida de conexión;
     ver el punto 4 más abajo), sin evaluar ningún símbolo.

     Dos decisiones deliberadas para acotar los falsos positivos que
     motivaron dejar esto sin activar en versiones anteriores:
       - Si `get_expected_open_positions()` devuelve `None` (backend sin
         registro de auditoría fiable, ej. `NullTradeStore` -- el valor por
         defecto de `BotEngine`), la comprobación se OMITE por completo.
         Comparar contra un diccionario vacío por no tener persistencia
         real dispararía el kill switch en cuanto hubiera cualquier
         posición abierta -- un falso positivo garantizado, no una
         protección real.
       - La comparación se acota a `self._config.symbols` (los símbolos que
         el bot gestiona) en ambos lados, tanto en lo esperado como en lo
         real -- para no disparar el kill switch por una posición que el
         usuario abrió manualmente desde el panel de Alpaca en un símbolo
         que el bot ni siquiera vigila.

     Dos fuentes de falso positivo que SIGUEN sin mitigar a propósito,
     porque hacerlo requeriría inventar datos que no existen: una posición
     de un símbolo vigilado que se abrió antes de tener persistencia
     activada, o un registro de apertura que falló en guardarse después de
     un envío exitoso a Alpaca. En ambos casos el resultado es el mismo:
     el kill switch se dispara (opción segura, coherente con el resto de
     este módulo) y requiere revisión manual + `reset()` explícito -- ver
     el docstring de `KillSwitch.check_position_discrepancy` y `reset()`.

  3. IDEMPOTENCIA DIARIA: la clave de idempotencia de cada orden es
     determinista a partir de símbolo + fecha (UTC) + lado
     (`f"{symbol}-{fecha_iso}-{side.value}"`). Con una estrategia de vela
     diaria esto significa como mucho una orden de apertura y una de cierre
     por símbolo y día -- si el programador de tareas se disparase dos veces
     el mismo día por error, `execution/order_manager.py` detecta la orden
     ya existente y no la duplica.

  4. PÉRDIDA DE CONEXIÓN: si falla la consulta de `is_market_open()` o de
     `get_account()`/`get_open_positions()`, el ciclo se aborda ENTERO (no
     se evalúa ningún símbolo) y se marca `kill_switch.record_connection_lost()`
     -- coherente con la sección 8 de las especificaciones ("pausar la
     generación de nuevas señales" ante pérdida de conexión). Si en un ciclo
     posterior esas mismas consultas funcionan de nuevo, se llama a
     `record_connection_restored()`, que a su vez dispara el kill switch si
     la caída fue más larga que el umbral configurado (ver
     `risk/kill_switch.py`).

  5. AISLAMIENTO POR SÍMBOLO: un fallo inesperado procesando UN símbolo
     (por ejemplo, datos corruptos o un error de programación) se registra
     en el log y se refleja en `SymbolCycleResult.error`, pero nunca aborta
     el resto del ciclo -- el resto de símbolos se siguen evaluando.

Persistencia (`persistence/`, sección 9 de las especificaciones): opcional
vía el parámetro `store` (un `TradeStore`, ver persistence/store.py) -- si
no se pasa ninguno, se usa `NullTradeStore` (no persiste nada, mismo
comportamiento que antes de que existiera `persistence/`). Cuando hay un
`TradeStore` real, cada ciclo guarda: TODA señal generada (se ejecute o
no, con sus indicadores y motivo), un snapshot de la cuenta, la apertura y
el cierre de cada operación, y cualquier error relevante. Es auditoría
pura -- ver el punto 2 más arriba sobre por qué nunca se usa para decidir.

Lo que este módulo NO hace todavía:
  - No implementa rate limiting local propio (sección 8) -- se apoya en
    los reintentos acotados y el circuit breaker que ya tiene
    `connectors/alpaca_connector.py` (ver el docstring de ese módulo:
    protegen fallos transitorios y fallos sostenidos respectivamente,
    ambos por debajo de esta capa, de forma transparente para
    `BotEngine` -- aquí solo se ven como `ConnectorError`/
    `ConnectorUnavailableError`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from connectors.base_connector import (
    Account,
    BrokerConnector,
    ConnectorError,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    Timeframe,
)
from execution.order_manager import (
    DuplicateOrderPreventedError,
    ExecutionRequest,
    KillSwitchActiveError,
    OrderManager,
    OrderRejectedError,
)
from persistence.store import NullTradeStore, TradeStore
from risk.kill_switch import KillSwitch
from risk.position_sizing import (
    DEFAULT_MAX_POSITION_PCT,
    DEFAULT_RISK_PCT,
    calculate_position_size,
)
from risk.stop_loss import StopLossConfig, StopLossMethod, compute_stop_loss
from strategy.indicators import bars_to_dataframe
from strategy.signals import Signal, SignalAction
from strategy.trend_strategy import TrendStrategy, TrendStrategyConfig

logger = logging.getLogger(__name__)

DEFAULT_BARS_LIMIT = 100  # margen suficiente para el calentamiento de EMA/RSI/MACD


@dataclass(frozen=True)
class BotEngineConfig:
    """Parámetros de un `BotEngine`. Nada de esto vive hardcodeado en la
    lógica -- cuando exista `config/config.yaml` alimentará estos campos
    directamente."""

    symbols: tuple[str, ...]
    timeframe: Timeframe = Timeframe.DAY
    bars_limit: int = DEFAULT_BARS_LIMIT
    strategy_config: TrendStrategyConfig = field(default_factory=TrendStrategyConfig)
    risk_pct: float = DEFAULT_RISK_PCT
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT
    stop_loss_config: StopLossConfig = field(default_factory=StopLossConfig)

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols no puede estar vacio")
        if self.bars_limit <= 0:
            raise ValueError("bars_limit debe ser positivo")
        if not (0.0 < self.risk_pct <= 1.0):
            raise ValueError("risk_pct debe estar entre 0 (exclusivo) y 1")
        if not (0.0 < self.max_position_pct <= 1.0):
            raise ValueError("max_position_pct debe estar entre 0 (exclusivo) y 1")


@dataclass(frozen=True)
class SymbolCycleResult:
    """Qué paso con UN símbolo en un ciclo, para poder auditar y para que
    quien invoque `run_cycle()` sepa qué se hizo (o por qué no se hizo
    nada)."""

    symbol: str
    action: SignalAction
    executed: bool
    reason: str
    order: OrderResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class CycleResult:
    """Resultado completo de una llamada a `run_cycle()`."""

    started_at: datetime
    market_open: bool
    kill_switch_active: bool
    aborted: bool
    abort_reason: str | None
    symbol_results: tuple[SymbolCycleResult, ...]


class BotEngine:
    """Orquesta un ciclo completo: datos -> estrategia -> riesgo ->
    ejecución, con el kill switch como gate de seguridad en todo momento.
    """

    def __init__(
        self,
        connector: BrokerConnector,
        strategy: TrendStrategy,
        kill_switch: KillSwitch,
        order_manager: OrderManager,
        config: BotEngineConfig,
        clock: Callable[[], datetime] | None = None,
        store: TradeStore | None = None,
    ) -> None:
        self._connector = connector
        self._strategy = strategy
        self._kill_switch = kill_switch
        self._order_manager = order_manager
        self._config = config
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._store = store or NullTradeStore()

    # ------------------------------------------------------------------
    # Ciclo completo
    # ------------------------------------------------------------------
    def run_cycle(self) -> CycleResult:
        started_at = self._now()

        if self._kill_switch.is_active():
            logger.warning(
                "Kill switch activo; ciclo omitido, no se evalua ningun simbolo "
                "hasta un reset() manual."
            )
            return CycleResult(
                started_at=started_at,
                market_open=False,
                kill_switch_active=True,
                aborted=False,
                abort_reason=None,
                symbol_results=(),
            )

        try:
            market_open = self._connector.is_market_open()
        except ConnectorError as exc:
            return self._abort_for_connection_failure(
                started_at, f"fallo al consultar si el mercado esta abierto: {exc}"
            )

        if not market_open:
            logger.info("Mercado cerrado; ciclo omitido.")
            return CycleResult(
                started_at=started_at,
                market_open=False,
                kill_switch_active=False,
                aborted=False,
                abort_reason=None,
                symbol_results=(),
            )

        try:
            account = self._connector.get_account()
            open_positions = {p.symbol: p for p in self._connector.get_open_positions()}
        except ConnectorError as exc:
            return self._abort_for_connection_failure(
                started_at, f"fallo al consultar cuenta/posiciones: {exc}"
            )

        if self._kill_switch.state.connection_lost_at is not None:
            self._kill_switch.record_connection_restored()

        self._store.record_equity_snapshot(
            equity=account.equity,
            cash=account.cash,
            buying_power=account.buying_power,
            timestamp=started_at,
        )

        discrepancy_abort = self._check_position_discrepancy(open_positions, started_at)
        if discrepancy_abort is not None:
            return discrepancy_abort

        results: list[SymbolCycleResult] = []
        for symbol in self._config.symbols:
            try:
                result = self._process_symbol(symbol, account, open_positions.get(symbol))
            except Exception as exc:  # noqa: BLE001 - aislar fallos por simbolo
                logger.exception(
                    "%s: error inesperado durante el ciclo; se continua con el "
                    "resto de simbolos.",
                    symbol,
                )
                result = SymbolCycleResult(
                    symbol=symbol,
                    action=SignalAction.HOLD,
                    executed=False,
                    reason="error inesperado procesando el simbolo",
                    error=str(exc),
                )
                self._store.record_error(
                    message=str(exc), context="ciclo por simbolo", symbol=symbol, timestamp=self._now()
                )
            results.append(result)

        return CycleResult(
            started_at=started_at,
            market_open=True,
            kill_switch_active=self._kill_switch.is_active(),
            aborted=False,
            abort_reason=None,
            symbol_results=tuple(results),
        )

    def _check_position_discrepancy(
        self, open_positions: dict[str, Position], started_at: datetime
    ) -> CycleResult | None:
        """Compara lo que el bot cree tener abierto (`self._store.
        get_expected_open_positions()`) contra lo que reporta el bróker,
        acotado a `self._config.symbols` -- ver el punto 2 del docstring
        del módulo para el razonamiento completo. Devuelve un `CycleResult`
        de aborto si hay discrepancia (kill switch ya disparado dentro de
        `KillSwitch.check_position_discrepancy`), o `None` si todo sigue
        normal (coincide, o no hay auditoría fiable con la que comparar).
        """
        expected = self._store.get_expected_open_positions()
        if expected is None:
            return None

        tracked_symbols = set(self._config.symbols)
        expected_tracked = {
            symbol: qty for symbol, qty in expected.items() if symbol in tracked_symbols and qty != 0
        }
        actual_tracked = {
            symbol: position.qty
            for symbol, position in open_positions.items()
            if symbol in tracked_symbols and position.qty != 0
        }

        if not self._kill_switch.check_position_discrepancy(expected_tracked, actual_tracked):
            return None

        reason = (
            "discrepancia entre las posiciones que el bot esperaba tener "
            f"({expected_tracked}) y las que reporta el broker "
            f"({actual_tracked}); kill switch activado, ciclo abortado sin "
            f"evaluar ningun simbolo"
        )
        logger.error(reason)
        self._store.record_error(
            message=reason, context="discrepancia posiciones", symbol=None, timestamp=started_at
        )
        return CycleResult(
            started_at=started_at,
            market_open=True,
            kill_switch_active=True,
            aborted=True,
            abort_reason=reason,
            symbol_results=(),
        )

    def _abort_for_connection_failure(self, started_at: datetime, reason: str) -> CycleResult:
        self._kill_switch.record_connection_lost()
        logger.error("Ciclo abortado: %s", reason)
        self._store.record_error(message=reason, context="conexion", symbol=None, timestamp=started_at)
        return CycleResult(
            started_at=started_at,
            market_open=False,
            kill_switch_active=self._kill_switch.is_active(),
            aborted=True,
            abort_reason=reason,
            symbol_results=(),
        )

    # ------------------------------------------------------------------
    # Un simbolo
    # ------------------------------------------------------------------
    def _process_symbol(
        self, symbol: str, account: Account, position: Position | None
    ) -> SymbolCycleResult:
        if not self._connector.is_symbol_tradable(symbol):
            logger.info("%s: no negociable ahora mismo (halt/inactivo); se omite.", symbol)
            return SymbolCycleResult(
                symbol=symbol,
                action=SignalAction.HOLD,
                executed=False,
                reason="no negociable ahora mismo (halt/inactivo)",
            )

        bars = self._connector.get_recent_bars(
            symbol, timeframe=self._config.timeframe, limit=self._config.bars_limit
        )
        if not bars:
            return SymbolCycleResult(
                symbol=symbol,
                action=SignalAction.HOLD,
                executed=False,
                reason="sin velas disponibles",
            )

        df = bars_to_dataframe(bars)
        signals = self._strategy.generate_signals(symbol, df)
        if not signals:
            return SymbolCycleResult(
                symbol=symbol,
                action=SignalAction.HOLD,
                executed=False,
                reason="sin senales generadas",
            )

        last_signal = signals[-1]

        if last_signal.action == SignalAction.BUY:
            result = self._handle_buy_signal(symbol, last_signal, account, position)
        elif last_signal.action == SignalAction.SELL:
            result = self._handle_sell_signal(symbol, last_signal, account, position)
        else:
            result = SymbolCycleResult(
                symbol=symbol, action=SignalAction.HOLD, executed=False, reason=last_signal.reason
            )

        self._store.record_signal(last_signal, executed=result.executed)
        return result

    def _handle_buy_signal(
        self, symbol: str, signal: Signal, account: Account, position: Position | None
    ) -> SymbolCycleResult:
        if position is not None and position.qty > 0:
            return SymbolCycleResult(
                symbol=symbol,
                action=SignalAction.BUY,
                executed=False,
                reason=f"{signal.reason} -- pero ya hay una posicion abierta; se omite",
            )

        stop_loss_price = compute_stop_loss(
            self._config.stop_loss_config,
            entry_price=signal.price,
            atr=(
                signal.indicators.atr
                if self._config.stop_loss_config.method == StopLossMethod.ATR
                else None
            ),
        )
        sizing = calculate_position_size(
            capital=account.equity,
            entry_price=signal.price,
            stop_loss_price=stop_loss_price,
            risk_pct=self._config.risk_pct,
            max_position_pct=self._config.max_position_pct,
        )
        if sizing.shares <= 0:
            return SymbolCycleResult(
                symbol=symbol,
                action=SignalAction.BUY,
                executed=False,
                reason=f"{signal.reason} -- position sizing dio 0 acciones; se omite",
            )

        request = ExecutionRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=float(sizing.shares),
            is_opening_position=True,
            idempotency_key=self._idempotency_key(symbol, OrderSide.BUY),
        )
        result = self._execute(request, SignalAction.BUY, signal.reason)
        if result.executed and result.order is not None:
            entry_price = (
                result.order.filled_avg_price
                if result.order.filled_avg_price is not None
                else signal.price
            )
            self._store.open_trade(
                symbol=symbol,
                side=OrderSide.BUY.value,
                qty=result.order.qty,
                entry_price=entry_price,
                entry_time=result.order.submitted_at,
                entry_order_id=result.order.broker_order_id,
                stop_loss_price=stop_loss_price,
                reason=signal.reason,
            )
        return result

    def _handle_sell_signal(
        self, symbol: str, signal: Signal, account: Account, position: Position | None
    ) -> SymbolCycleResult:
        if position is None or position.qty <= 0:
            return SymbolCycleResult(
                symbol=symbol,
                action=SignalAction.SELL,
                executed=False,
                reason=f"{signal.reason} -- pero no hay posicion abierta que cerrar; se omite",
            )

        request = ExecutionRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=position.qty,
            is_opening_position=False,
            idempotency_key=self._idempotency_key(symbol, OrderSide.SELL),
        )
        result = self._execute(request, SignalAction.SELL, signal.reason)
        if result.executed and result.order is not None:
            self._finalize_closed_trade(result.order, position, account, signal.reason)
        return result

    def _execute(
        self, request: ExecutionRequest, action: SignalAction, reason: str
    ) -> SymbolCycleResult:
        try:
            order = self._order_manager.execute(request)
        except KillSwitchActiveError as exc:
            logger.warning(
                "%s: apertura bloqueada por el kill switch: %s", request.symbol, exc
            )
            return SymbolCycleResult(
                symbol=request.symbol,
                action=action,
                executed=False,
                reason=f"{reason} -- bloqueado por el kill switch",
                error=str(exc),
            )
        except (OrderRejectedError, DuplicateOrderPreventedError, ConnectorError) as exc:
            logger.error("%s: fallo al ejecutar la orden: %s", request.symbol, exc)
            return SymbolCycleResult(
                symbol=request.symbol,
                action=action,
                executed=False,
                reason=reason,
                error=str(exc),
            )

        return SymbolCycleResult(
            symbol=request.symbol, action=action, executed=True, reason=reason, order=order
        )

    def _finalize_closed_trade(
        self, order: OrderResult, position: Position, account: Account, reason: str
    ) -> None:
        trade_id = self._store.get_open_trade_id(order.symbol)

        if order.status != OrderStatus.FILLED or order.filled_avg_price is None or order.filled_qty <= 0:
            logger.warning(
                "%s: la orden de cierre no quedo FILLED (estado=%s); no se "
                "registra P&L en el kill switch todavia -- requiere "
                "seguimiento en el proximo ciclo. Tampoco se cierra el "
                "registro de persistencia todavia por el mismo motivo.",
                order.symbol,
                order.status.value,
            )
            return

        pnl_money = (order.filled_avg_price - position.avg_entry_price) * order.filled_qty
        pnl_pct_of_capital = pnl_money / account.equity if account.equity else 0.0
        self._kill_switch.record_trade_result(pnl_pct_of_capital)
        logger.info(
            "%s: P&L de la operacion cerrada registrado en el kill switch (%.2f%% del capital).",
            order.symbol,
            pnl_pct_of_capital * 100,
        )

        if trade_id is not None:
            self._store.close_trade(
                trade_id,
                exit_price=order.filled_avg_price,
                exit_time=order.submitted_at,
                exit_order_id=order.broker_order_id,
                reason=reason,
                pnl_pct_of_capital=pnl_pct_of_capital,
            )
        else:
            logger.info(
                "%s: no habia un registro de apertura en persistencia para "
                "cerrar (normal sin TradeStore real, o si la posicion se "
                "abrio antes de tener persistencia activada).",
                order.symbol,
            )

    def _idempotency_key(self, symbol: str, side: OrderSide) -> str:
        today = self._now().date().isoformat()
        return f"{symbol}-{today}-{side.value}"
