"""Lógica de señal BUY/SELL/HOLD de la estrategia de tendencia (Fase 1).

Reglas (ver 02_Especificaciones_Tecnicas.md, sección 4):

  Entrada (BUY): cruce alcista de EMA rápida sobre EMA lenta + RSI entre
  `rsi_buy_min` y `rsi_buy_max` (evita comprar en sobrecompra extrema) +
  histograma MACD positivo (confirmación de momentum).

  Salida (SELL): cruce bajista de EMAs, o RSI > `rsi_sell_threshold`.

  El stop-loss/take-profit que también menciona la sección 4 como señal de
  salida es responsabilidad de `risk/stop_loss.py` (módulo futuro, todavía
  no construido) -- se aplicará como una capa adicional sobre estas señales
  de estrategia, no lo calcula esta clase. Esta clase SOLO genera señales:
  nunca decide tamaño de posición ni ejecuta órdenes.

Todos los parámetros son configurables (nunca hardcodeados sin más),
agrupados en `TrendStrategyConfig` -- cuando exista config/config.yaml, sus
valores alimentarán directamente esta clase.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from strategy.indicators import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_EMA_FAST,
    DEFAULT_EMA_SLOW,
    DEFAULT_MACD_FAST,
    DEFAULT_MACD_SLOW,
    DEFAULT_MACD_SIGNAL,
    DEFAULT_RSI_PERIOD,
    add_indicators,
)
from strategy.signals import IndicatorSnapshot, Signal, SignalAction

# Incluye 'atr' aunque _evaluate() no lo use para decidir BUY/SELL/HOLD:
# esta lista tambien gobierna el "periodo de calentamiento" (mas abajo), y
# si el ATR todavia no tiene suficiente historia no debe poder salir una
# señal BUY -- risk/stop_loss.py con StopLossMethod.ATR necesita un ATR
# valido (no NaN) para calcular el stop-loss de esa operacion.
_INDICATOR_COLUMNS = ["ema_fast", "ema_slow", "rsi", "macd_histogram", "atr"]


@dataclass(frozen=True)
class TrendStrategyConfig:
    """Parámetros configurables de la estrategia."""

    ema_fast: int = DEFAULT_EMA_FAST
    ema_slow: int = DEFAULT_EMA_SLOW
    rsi_period: int = DEFAULT_RSI_PERIOD
    rsi_buy_min: float = 40.0
    rsi_buy_max: float = 70.0
    rsi_sell_threshold: float = 80.0
    macd_fast: int = DEFAULT_MACD_FAST
    macd_slow: int = DEFAULT_MACD_SLOW
    macd_signal: int = DEFAULT_MACD_SIGNAL
    atr_period: int = DEFAULT_ATR_PERIOD

    def __post_init__(self) -> None:
        if self.ema_fast <= 0 or self.ema_slow <= 0:
            raise ValueError("Los periodos de EMA deben ser positivos")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast debe ser menor que ema_slow")
        if not (0.0 <= self.rsi_buy_min < self.rsi_buy_max <= 100.0):
            raise ValueError("Debe cumplirse 0 <= rsi_buy_min < rsi_buy_max <= 100")
        if not (0.0 < self.rsi_sell_threshold <= 100.0):
            raise ValueError("rsi_sell_threshold debe estar en (0, 100]")
        if self.atr_period <= 0:
            raise ValueError("atr_period debe ser positivo")


class TrendStrategy:
    """Genera señales BUY/SELL/HOLD para UN símbolo a partir de sus velas.

    Esta clase SOLO genera señales -- nunca decide tamaño de posición ni
    ejecuta órdenes (eso es risk/ y execution/ respectivamente).
    """

    def __init__(self, config: TrendStrategyConfig | None = None) -> None:
        self.config = config or TrendStrategyConfig()

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Añade las columnas de indicadores a un DataFrame OHLCV de un
        único símbolo, ordenado cronológicamente."""
        return add_indicators(
            df,
            ema_fast=self.config.ema_fast,
            ema_slow=self.config.ema_slow,
            rsi_period=self.config.rsi_period,
            macd_fast=self.config.macd_fast,
            macd_slow=self.config.macd_slow,
            macd_signal=self.config.macd_signal,
            atr_period=self.config.atr_period,
        )

    def generate_signals(self, symbol: str, df: pd.DataFrame) -> list[Signal]:
        """Genera una señal por cada vela del DataFrame, comparando la vela
        actual con la anterior (necesario para detectar cruces de EMA).

        Mientras los indicadores no tengan suficiente historia (NaN, al
        principio de la serie -- "periodo de calentamiento"), la señal es
        HOLD con el motivo explícito 'indicadores insuficientes'; nunca se
        genera BUY/SELL con datos incompletos.
        """
        if df.empty:
            return []

        enriched = self.compute_indicators(df)
        signals: list[Signal] = []
        prev_row: pd.Series | None = None

        for timestamp, row in enriched.iterrows():
            current_incomplete = bool(row[_INDICATOR_COLUMNS].isna().any())
            prev_incomplete = prev_row is None or bool(
                prev_row[_INDICATOR_COLUMNS].isna().any()
            )

            if current_incomplete or prev_incomplete:
                signals.append(
                    self._make_signal(
                        symbol,
                        timestamp,
                        row,
                        SignalAction.HOLD,
                        "indicadores insuficientes (periodo de calentamiento)",
                    )
                )
                prev_row = row
                continue

            action, reason = self._evaluate(prev_row, row)
            signals.append(self._make_signal(symbol, timestamp, row, action, reason))
            prev_row = row

        return signals

    def _evaluate(self, prev_row: pd.Series, row: pd.Series) -> tuple[SignalAction, str]:
        bullish_cross = (
            prev_row["ema_fast"] <= prev_row["ema_slow"] and row["ema_fast"] > row["ema_slow"]
        )
        bearish_cross = (
            prev_row["ema_fast"] >= prev_row["ema_slow"] and row["ema_fast"] < row["ema_slow"]
        )
        rsi_in_buy_range = self.config.rsi_buy_min <= row["rsi"] <= self.config.rsi_buy_max
        rsi_overbought = row["rsi"] > self.config.rsi_sell_threshold
        macd_positive = row["macd_histogram"] > 0

        if bullish_cross and rsi_in_buy_range and macd_positive:
            return (
                SignalAction.BUY,
                f"cruce alcista EMA{self.config.ema_fast}/EMA{self.config.ema_slow} "
                f"+ RSI {row['rsi']:.1f} en [{self.config.rsi_buy_min:.0f},"
                f"{self.config.rsi_buy_max:.0f}] + histograma MACD positivo "
                f"({row['macd_histogram']:.4f})",
            )

        if bearish_cross:
            return (
                SignalAction.SELL,
                f"cruce bajista EMA{self.config.ema_fast}/EMA{self.config.ema_slow}",
            )

        if rsi_overbought:
            return (
                SignalAction.SELL,
                f"RSI en sobrecompra ({row['rsi']:.1f} > {self.config.rsi_sell_threshold:.0f})",
            )

        return SignalAction.HOLD, "sin condiciones de entrada ni salida"

    @staticmethod
    def _make_signal(
        symbol: str, timestamp, row: pd.Series, action: SignalAction, reason: str
    ) -> Signal:
        def _safe_float(value) -> float:
            return float(value) if pd.notna(value) else float("nan")

        return Signal(
            symbol=symbol,
            timestamp=timestamp,
            action=action,
            price=float(row["close"]),
            indicators=IndicatorSnapshot(
                ema_fast=_safe_float(row.get("ema_fast")),
                ema_slow=_safe_float(row.get("ema_slow")),
                rsi=_safe_float(row.get("rsi")),
                macd_histogram=_safe_float(row.get("macd_histogram")),
                atr=_safe_float(row.get("atr")),
            ),
            reason=reason,
        )
