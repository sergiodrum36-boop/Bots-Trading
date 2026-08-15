"""Cálculo del tamaño de posición (02_Especificaciones_Tecnicas.md,
sección 5.1).

Fórmula base (arriesgar un % fijo del capital por operación):

    riesgo_dinero = capital_total * porcentaje_riesgo_por_operacion
    distancia_stop = precio_entrada - precio_stop_loss
    n_acciones = floor(riesgo_dinero / distancia_stop)

Con un límite adicional: nunca comprometer más de un `% máximo del capital
por posición individual` (ej. 20%), aunque el cálculo anterior diera un
número mayor de acciones.

Este módulo SOLO calcula -- no decide si operar (eso es la señal de
estrategia) ni ejecuta nada (eso es execution/). Recibe el stop-loss ya
calculado (ver risk/stop_loss.py); no lo calcula él mismo.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_RISK_PCT = 0.01  # 1% del capital por operación (rango sugerido: 1-2%)
DEFAULT_MAX_POSITION_PCT = 0.20  # nunca más del 20% del capital en una sola posición


@dataclass(frozen=True)
class PositionSizeResult:
    """Resultado del cálculo, con el desglose completo para poder auditar
    *por qué* se llegó a ese número de acciones (mismo espíritu que
    `strategy.signals.Signal.reason`)."""

    shares: int
    risk_amount: float  # dinero que se arriesga si salta el stop-loss (shares * distancia_stop)
    position_value: float  # capital comprometido en la posición (shares * entry_price)
    position_pct_of_capital: float  # position_value / capital_total
    capped_by_max_position: bool  # True si el límite del 5.1 (% máx. por posición) fue el que decidió, no el riesgo


def calculate_position_size(
    capital: float,
    entry_price: float,
    stop_loss_price: float,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
) -> PositionSizeResult:
    """Calcula cuántas acciones comprar dado el capital disponible, el
    riesgo que se está dispuesto a asumir por operación y el stop-loss ya
    decidido.

    Devuelve `shares=0` (sin lanzar excepción) cuando el presupuesto de
    riesgo no alcanza ni para una sola acción -- es un resultado legítimo
    ("no se puede entrar en esta operación con este tamaño de riesgo"), no
    un error.
    """
    if capital <= 0:
        raise ValueError("capital debe ser positivo")
    if entry_price <= 0:
        raise ValueError("entry_price debe ser positivo")
    if not (0.0 < risk_pct <= 1.0):
        raise ValueError("risk_pct debe estar entre 0 (exclusivo) y 1 (ej. 0.01 para 1%)")
    if not (0.0 < max_position_pct <= 1.0):
        raise ValueError("max_position_pct debe estar entre 0 (exclusivo) y 1")
    if stop_loss_price >= entry_price:
        raise ValueError(
            "stop_loss_price debe ser menor que entry_price (posición larga: "
            f"entry_price={entry_price}, stop_loss_price={stop_loss_price})"
        )

    distancia_stop = entry_price - stop_loss_price
    riesgo_dinero = capital * risk_pct
    shares_por_riesgo = math.floor(riesgo_dinero / distancia_stop)

    max_position_value = capital * max_position_pct
    shares_por_limite_posicion = math.floor(max_position_value / entry_price)

    shares = min(shares_por_riesgo, shares_por_limite_posicion)
    capped = shares_por_limite_posicion < shares_por_riesgo

    if shares <= 0:
        logger.warning(
            "Position sizing: 0 acciones (riesgo=%.2f, distancia_stop=%.4f, "
            "shares_por_riesgo=%s, shares_por_limite_posicion=%s) -- el "
            "presupuesto de riesgo no alcanza para ni una accion con este "
            "stop-loss. No se entra en la operacion.",
            riesgo_dinero,
            distancia_stop,
            shares_por_riesgo,
            shares_por_limite_posicion,
        )
        return PositionSizeResult(
            shares=0,
            risk_amount=0.0,
            position_value=0.0,
            position_pct_of_capital=0.0,
            capped_by_max_position=False,
        )

    position_value = shares * entry_price
    risk_amount = shares * distancia_stop

    if capped:
        logger.info(
            "Position sizing: limitado por el %% maximo por posicion "
            "(%.0f%%): %s acciones en vez de las %s que pedia el riesgo puro.",
            max_position_pct * 100,
            shares,
            shares_por_riesgo,
        )

    return PositionSizeResult(
        shares=shares,
        risk_amount=risk_amount,
        position_value=position_value,
        position_pct_of_capital=position_value / capital,
        capped_by_max_position=capped,
    )
