"""Tipos de datos para las señales generadas por la capa de Estrategia.

La capa de Estrategia SOLO genera señales (BUY/SELL/HOLD); nunca decide el
tamaño de la posición ni ejecuta la orden -- eso es responsabilidad del
módulo de Riesgo y del módulo de Ejecución respectivamente (ver
02_Especificaciones_Tecnicas.md, sección 4).

`Signal` guarda también los valores de los indicadores que la originaron,
para poder auditar despues *por qué* se tomó o descartó una decisión (esto
es justo lo que pide la tabla `signals` de persistence/, sección 9 de las
especificaciones -- ese módulo aún no existe, pero esta forma ya queda
lista para volcarse ahí sin cambios).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Valores de los indicadores en el momento de generar la señal."""

    ema_fast: float
    ema_slow: float
    rsi: float
    macd_histogram: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    timestamp: datetime
    action: SignalAction
    price: float  # precio de cierre de la vela que originó la señal
    indicators: IndicatorSnapshot
    reason: str  # explicación legible, ej. "cruce alcista EMA + RSI 55.2 + MACD hist positivo"
