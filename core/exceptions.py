"""Excepciones propias del dominio del orquestador
(03_Estructura_Proyecto_Python.md).

Nota de alcance de esta primera versión: `core/bot_engine.py` NO deja que un
fallo al procesar un símbolo, o incluso al consultar cuenta/posiciones,
tumbe el proceso -- todo se traduce en el valor de retorno de `run_cycle()`
(`CycleResult` / `SymbolCycleResult`), visible a quien lo invoque (por
ejemplo `scripts/run_paper_trading.py`, todavía no construido) sin
excepciones sin capturar. `BotEngineError` queda reservada para fallos de
orquestación genuinamente irrecuperables en iteraciones futuras (por
ejemplo, cuando `core/bot_engine.py` crezca un bucle continuo propio con
apagado ordenado, o cuando `config/config.yaml` se cargue y pueda estar mal
formado de una manera específica del dominio).
"""

from __future__ import annotations


class BotEngineError(Exception):
    """Error base de core/bot_engine.py."""
