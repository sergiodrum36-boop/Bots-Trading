"""Motor de backtesting: ejecuta la estrategia de tendencia sobre datos
históricos de uno o varios símbolos y calcula métricas de validación
(ratio de Sharpe, máximo drawdown, win rate, profit factor) -- ver
02_Especificaciones_Tecnicas.md, secciones 4 y 12 (checklist de salida de
Fase 1: "Backtest de la estrategia con métricas aceptadas de antemano por
el usuario").

Decisiones de diseño de esta primera versión (léanse antes de interpretar
las métricas, especialmente antes de usarlas para decidir pasar de fase):

1. Sin motor externo (backtrader/vectorbt, sugeridos como referencia en la
   guía de fuentes): el simulador es deliberadamente simple -- una posición
   larga por símbolo, sin apalancamiento -- para no añadir la complejidad
   de esas librerías antes de validar si la lógica de señales merece la
   pena.

2. Entrada/salida SIN look-ahead bias: la señal se calcula con el CIERRE de
   la vela N, pero la operación se simula ejecutada al PRECIO DE APERTURA
   de la vela N+1. Nunca se opera al precio de la misma vela que generó la
   señal -- eso usaría información que en la realidad todavía no se conoce
   en el momento de decidir.

3. Retornos por operación en TÉRMINOS PORCENTUALES (no en $ con position
   sizing real): risk/position_sizing.py todavía no existe. `win_rate` y
   `profit_factor` son estadísticas estándar a nivel de operación y no se
   ven afectadas por esto.

4. `sharpe_ratio` y `max_drawdown` de ESTA VERSIÓN se calculan sobre la
   SECUENCIA DE RETORNOS POR OPERACIÓN (compuestos en el orden en que se
   cierran), NO sobre una curva de equity diaria account-level. Es una
   aproximación deliberada y HONESTA sobre su alcance:
   - `sharpe_ratio` = (media de retornos por operación / desviación
     estándar de esos retornos), SIN anualizar -- anualizar con sqrt(252)
     asumiendo que cada "operación" ocurre una vez al día sería engañoso
     (una operación puede durar varias sesiones). Sirve para comparar la
     consistencia relativa de la señal, no como un Sharpe anualizado
     "oficial" de cartera.
   - `max_drawdown` asume una única posición de capital compuesta
     secuencialmente trade a trade (ignora los días en que no hay ninguna
     posición abierta, que no aportan ni restan).
   Cuando exista `persistence/` con una tabla `equity_curve` (sección 9 de
   las especificaciones), se podrá calcular un Sharpe/drawdown diario
   estándar sobre la curva de capital real y comparar con estas cifras.

5. Con varios símbolos, `win_rate`, `profit_factor` y `sharpe_ratio`
   globales SÍ se calculan sobre el conjunto combinado de operaciones (son
   estadísticas por operación, válidas independientemente de qué símbolo
   sea cada una). PERO `total_return_pct` y `max_drawdown` GLOBALES se
   dejan en `None` en cuanto hay operaciones de más de un símbolo --
   NO se intenta componer un único capital secuencial con operaciones
   entrelazadas de varios símbolos, porque eso da una cifra irreal (se
   detectó en pruebas reales: con 8 símbolos a la vez, sumar-componer sus
   operaciones como si fueran una sola posición detrás de otra puede
   disparar un "retorno total" de varios cientos por ciento que nadie
   obtendría de verdad, ya que en la práctica el capital estaría repartido
   EN PARALELO entre los símbolos, no todo en una operación cada vez).
   Para esas dos métricas, usa siempre `per_symbol_metrics` (ahí sí
   representan una única posición real para ESE símbolo) -- y si hace
   falta una cifra de cartera con varios símbolos en paralelo, se puede
   promediar el `total_return_pct` de cada símbolo (asumiendo capital
   repartido a partes iguales), pero eso no lo hace este modulo todavia.

No se conecta este módulo a ejecución de órdenes real ni a paper trading:
solo valida la estrategia contra datos históricos.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from connectors.base_connector import BrokerConnector, Timeframe
from strategy.indicators import bars_to_dataframe
from strategy.signals import Signal, SignalAction
from strategy.trend_strategy import TrendStrategy, TrendStrategyConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trade:
    """Una operación simulada (round-trip) sobre un símbolo."""

    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    entry_reason: str
    exit_reason: str

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price) - 1.0


@dataclass(frozen=True)
class BacktestMetrics:
    """Ver docstring del módulo (puntos 4 y 5) para la definición exacta de
    `sharpe_ratio`, `max_drawdown` y `total_return_pct` usada en esta
    version, y para cuándo estas dos ultimas pueden venir a `None`."""

    n_trades: int
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float | None
    total_return_pct: float | None

    def as_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "total_return_pct": self.total_return_pct,
        }


@dataclass(frozen=True)
class BacktestResult:
    trades: list[Trade]
    signals: list[Signal]
    metrics: BacktestMetrics
    per_symbol_metrics: dict[str, BacktestMetrics] = field(default_factory=dict)


def _next_bar_fill(timestamps: list, index_by_ts: dict, df: pd.DataFrame, signal_ts):
    """Devuelve (timestamp, precio de apertura) de la vela SIGUIENTE a
    `signal_ts`, o None si `signal_ts` es la última vela disponible (todavía
    no hay vela siguiente donde ejecutar -- se descarta esa señal, no se
    inventa un precio)."""
    idx = index_by_ts.get(signal_ts)
    if idx is None or idx + 1 >= len(timestamps):
        return None
    next_ts = timestamps[idx + 1]
    return next_ts, float(df.loc[next_ts, "open"])


def _simulate_trades(symbol: str, df: pd.DataFrame, signals: list[Signal]) -> list[Trade]:
    """Simula operaciones long-only, como máximo una posición abierta a la
    vez por símbolo. Entra/sale al precio de APERTURA de la vela SIGUIENTE
    a la señal (ver punto 2 del docstring del módulo: evita look-ahead
    bias)."""
    timestamps = list(df.index)
    index_by_ts = {ts: i for i, ts in enumerate(timestamps)}

    trades: list[Trade] = []
    open_entry: tuple | None = None  # (fill_time, fill_price, entry_reason)

    for signal in signals:
        if signal.action == SignalAction.BUY and open_entry is None:
            fill = _next_bar_fill(timestamps, index_by_ts, df, signal.timestamp)
            if fill is None:
                continue
            fill_time, fill_price = fill
            open_entry = (fill_time, fill_price, signal.reason)

        elif signal.action == SignalAction.SELL and open_entry is not None:
            fill = _next_bar_fill(timestamps, index_by_ts, df, signal.timestamp)
            if fill is None:
                continue
            fill_time, fill_price = fill
            entry_time, entry_price, entry_reason = open_entry
            trades.append(
                Trade(
                    symbol=symbol,
                    entry_time=entry_time,
                    entry_price=entry_price,
                    exit_time=fill_time,
                    exit_price=fill_price,
                    entry_reason=entry_reason,
                    exit_reason=signal.reason,
                )
            )
            open_entry = None

    if open_entry is not None:
        logger.info(
            "%s: posicion abierta al final del periodo de backtest sin señal "
            "de salida; no se cuenta como operacion cerrada en las metricas.",
            symbol,
        )

    return trades


def _compute_metrics(trades: list[Trade], include_equity_curve: bool = True) -> BacktestMetrics:
    """Ver docstring del modulo (puntos 4 y 5) para el significado exacto de
    `sharpe_ratio`, `max_drawdown` y `total_return_pct`.

    `include_equity_curve` controla si se calculan `total_return_pct` y
    `max_drawdown` (que asumen una UNICA linea de capital compuesta
    secuencialmente operacion a operacion). Debe ponerse a False cuando
    `trades` mezcla operaciones de VARIOS SIMBOLOS entrelazadas en el
    tiempo -- componerlas secuencialmente simularia un capital que en la
    realidad estaria repartido en posiciones EN PARALELO, no una detras de
    otra, y da cifras irreales (ver punto 5 del docstring del modulo).
    Cuando es False, ambos campos quedan en `None`.
    """
    if not trades:
        return BacktestMetrics(
            n_trades=0,
            win_rate=0.0,
            profit_factor=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            total_return_pct=0.0,
        )

    returns = [t.return_pct for t in trades]
    n = len(returns)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / n

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss == 0:
        profit_factor = math.inf if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss

    mean_return = sum(returns) / n
    if n > 1:
        variance = sum((r - mean_return) ** 2 for r in returns) / (n - 1)
        std_return = math.sqrt(variance)
    else:
        std_return = 0.0
    sharpe_ratio = 0.0 if std_return == 0 else mean_return / std_return

    max_dd: float | None = None
    total_return_pct: float | None = None
    if include_equity_curve:
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for r in returns:
            equity *= 1.0 + r
            peak = max(peak, equity)
            max_dd = min(max_dd, (equity - peak) / peak)
        total_return_pct = equity - 1.0

    return BacktestMetrics(
        n_trades=n,
        win_rate=win_rate,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe_ratio,
        max_drawdown=max_dd,
        total_return_pct=total_return_pct,
    )


def run_backtest(
    connector: BrokerConnector,
    symbols: list[str],
    start: datetime,
    end: datetime,
    strategy_config: TrendStrategyConfig | None = None,
    timeframe: Timeframe = Timeframe.DAY,
) -> BacktestResult:
    """Ejecuta la estrategia de tendencia sobre datos históricos de varios
    símbolos y devuelve las operaciones simuladas + métricas de validación.

    `connector` es cualquier implementación de `BrokerConnector` (hoy,
    `AlpacaConnector`) -- el backtest depende solo de la interfaz abstracta,
    nunca de un broker concreto, tal como exige la arquitectura desacoplada
    (02_Especificaciones_Tecnicas.md, sección 2).
    """
    strategy = TrendStrategy(strategy_config)
    all_trades: list[Trade] = []
    all_signals: list[Signal] = []
    per_symbol_metrics: dict[str, BacktestMetrics] = {}

    for symbol in symbols:
        bars = connector.get_historical_bars(
            symbols=symbol, timeframe=timeframe, start=start, end=end
        )
        if not bars:
            logger.warning(
                "%s: sin datos historicos en el rango solicitado; se omite.", symbol
            )
            per_symbol_metrics[symbol] = _compute_metrics([])
            continue

        df = bars_to_dataframe(bars)
        df = df[df["symbol"] == symbol]

        signals = strategy.generate_signals(symbol, df)
        trades = _simulate_trades(symbol, df, signals)

        all_signals.extend(signals)
        all_trades.extend(trades)
        per_symbol_metrics[symbol] = _compute_metrics(trades)

        logger.info(
            "%s: %s velas, %s señales, %s operaciones cerradas simuladas.",
            symbol,
            len(bars),
            len(signals),
            len(trades),
        )

    # Se combinan las operaciones de todos los simbolos ordenadas por fecha
    # de cierre. win_rate/profit_factor/sharpe_ratio globales si son
    # validos combinados; total_return_pct/max_drawdown solo si TODAS las
    # operaciones son del mismo simbolo (ver punto 5 del docstring).
    all_trades.sort(key=lambda t: t.exit_time)
    symbols_with_trades = {t.symbol for t in all_trades}
    overall_metrics = _compute_metrics(
        all_trades, include_equity_curve=len(symbols_with_trades) <= 1
    )

    return BacktestResult(
        trades=all_trades,
        signals=all_signals,
        metrics=overall_metrics,
        per_symbol_metrics=per_symbol_metrics,
    )
