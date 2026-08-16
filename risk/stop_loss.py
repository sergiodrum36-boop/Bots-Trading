"""Cálculo del precio de stop-loss (02_Especificaciones_Tecnicas.md,
sección 5.2).

Dos métodos, tal como describen las especificaciones:
  - `fixed_percent`: un porcentaje fijo por debajo del precio de entrada.
    Es el método por defecto para esta primera iteración (más simple de
    razonar y de backtestear).
  - `atr`: basado en volatilidad (ATR), se adapta mejor a distintos
    valores pero añade complejidad -- pensado como segunda iteración,
    ya disponible aquí si se quiere usar.

Alcance: la estrategia de este proyecto es LONG-ONLY (solo señales BUY/
SELL, nunca venta en corto -- ver strategy/trend_strategy.py), así que
estas funciones asumen una posición larga: el stop-loss siempre queda POR
DEBAJO del precio de entrada.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class StopLossMethod(str, Enum):
    FIXED_PERCENT = "fixed_percent"
    ATR = "atr"


DEFAULT_FIXED_PERCENT = 0.02  # 2%, valor de partida sugerido por las specs
DEFAULT_ATR_MULTIPLIER = 2.0


def fixed_percent_stop_loss(entry_price: float, percent: float = DEFAULT_FIXED_PERCENT) -> float:
    """Stop-loss porcentual fijo por debajo del precio de entrada.

    Ej.: entry_price=100, percent=0.02 -> stop-loss en 98.0.
    """
    if entry_price <= 0:
        raise ValueError("entry_price debe ser positivo")
    if not (0.0 < percent < 1.0):
        raise ValueError("percent debe estar entre 0 y 1 (ej. 0.02 para 2%)")
    return entry_price * (1.0 - percent)


def atr_stop_loss(
    entry_price: float, atr: float, multiplier: float = DEFAULT_ATR_MULTIPLIER
) -> float:
    """Stop-loss basado en volatilidad: entry_price - (ATR * multiplier).

    `atr` es el valor de ATR (Average True Range) ya calculado sobre el
    símbolo (ver strategy/indicators.py, función `atr()`).
    """
    if entry_price <= 0:
        raise ValueError("entry_price debe ser positivo")
    # math.isnan primero: "atr <= 0" con NaN es False (ninguna comparacion
    # con NaN es verdadera), asi que sin este chequeo explicito un ATR NaN
    # colaria un stop-loss NaN en vez de fallar aqui -- ver
    # strategy/trend_strategy.py sobre cuando puede llegar NaN (no deberia,
    # es una red de seguridad adicional, no el unico guardian).
    if math.isnan(atr):
        raise ValueError("atr no puede ser NaN (indicador sin suficiente historia todavia)")
    if atr <= 0:
        raise ValueError("atr debe ser positivo")
    if multiplier <= 0:
        raise ValueError("multiplier debe ser positivo")

    stop = entry_price - (atr * multiplier)
    if stop <= 0:
        raise ValueError(
            f"El stop-loss calculado ({stop}) no es positivo -- ATR "
            f"({atr}) x multiplier ({multiplier}) es mayor que el precio "
            f"de entrada ({entry_price}). Revisa los parámetros."
        )
    return stop


@dataclass(frozen=True)
class StopLossConfig:
    method: StopLossMethod = StopLossMethod.FIXED_PERCENT
    fixed_percent: float = DEFAULT_FIXED_PERCENT
    atr_multiplier: float = DEFAULT_ATR_MULTIPLIER


def compute_stop_loss(
    config: StopLossConfig, entry_price: float, atr: float | None = None
) -> float:
    """Calcula el stop-loss según el método configurado.

    Con `StopLossMethod.ATR`, quien llame debe pasar el ATR ya calculado
    para el símbolo (ver strategy/indicators.py, función `atr()`, y
    core/bot_engine.py que lo toma de `Signal.indicators.atr`).
    """
    if config.method == StopLossMethod.FIXED_PERCENT:
        return fixed_percent_stop_loss(entry_price, config.fixed_percent)

    if config.method == StopLossMethod.ATR:
        if atr is None:
            raise ValueError(
                "El método ATR requiere pasar el valor de ATR calculado "
                "para el símbolo (parámetro 'atr')."
            )
        return atr_stop_loss(entry_price, atr, config.atr_multiplier)

    raise ValueError(f"Método de stop-loss no soportado: {config.method}")
