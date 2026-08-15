"""Consultas de SOLO LECTURA sobre la base de datos de persistencia.

Separado deliberadamente de `persistence/store.py` (la interfaz de
ESCRITURA que usa `core/bot_engine.py`): nada de lo que hay aquí se usa
nunca para tomar una decisión de trading, solo para MOSTRAR información a
un humano -- hoy el panel visual (`dashboard/`), en el futuro cualquier
otra herramienta de reporting.
"""

from __future__ import annotations

from persistence.database import session_scope
from persistence.models import EquitySnapshotRecord, ErrorLogRecord, SignalRecord, TradeRecord


def list_trades(session_factory, limit: int = 200) -> list[TradeRecord]:
    """Todas las operaciones (abiertas y cerradas), más recientes primero."""
    with session_scope(session_factory) as session:
        return (
            session.query(TradeRecord)
            .order_by(TradeRecord.entry_time.desc())
            .limit(limit)
            .all()
        )


def list_open_trades(session_factory) -> list[TradeRecord]:
    with session_scope(session_factory) as session:
        return (
            session.query(TradeRecord)
            .filter(TradeRecord.status == "open")
            .order_by(TradeRecord.entry_time.desc())
            .all()
        )


def list_signals(session_factory, limit: int = 300, symbol: str | None = None) -> list[SignalRecord]:
    """Señales más recientes primero, opcionalmente filtradas por símbolo."""
    with session_scope(session_factory) as session:
        query = session.query(SignalRecord).order_by(SignalRecord.timestamp.desc())
        if symbol:
            query = query.filter(SignalRecord.symbol == symbol)
        return query.limit(limit).all()


def list_equity_curve(session_factory, limit: int = 1000) -> list[EquitySnapshotRecord]:
    """Snapshots de capital en orden CRONOLÓGICO ascendente (listos para
    graficar directamente, a diferencia del resto de consultas de este
    módulo que devuelven lo más reciente primero)."""
    with session_scope(session_factory) as session:
        rows = (
            session.query(EquitySnapshotRecord)
            .order_by(EquitySnapshotRecord.timestamp.desc())
            .limit(limit)
            .all()
        )
    return list(reversed(rows))


def list_errors(session_factory, limit: int = 100) -> list[ErrorLogRecord]:
    with session_scope(session_factory) as session:
        return (
            session.query(ErrorLogRecord)
            .order_by(ErrorLogRecord.timestamp.desc())
            .limit(limit)
            .all()
        )


def summarize_closed_trades(session_factory) -> dict:
    """Estadísticas básicas sobre operaciones YA cerradas con P&L conocido:
    nº de operaciones, win rate y P&L acumulado (en % del capital, sumado
    operación a operación -- no es un retorno compuesto, ver el mismo
    aviso que ya se hace en backtesting/backtest_runner.py sobre combinar
    P&L de operaciones secuenciales)."""
    with session_scope(session_factory) as session:
        closed = (
            session.query(TradeRecord)
            .filter(TradeRecord.status == "closed", TradeRecord.pnl_pct_of_capital.isnot(None))
            .all()
        )

    n = len(closed)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "total_pnl_pct": 0.0}

    wins = sum(1 for t in closed if t.pnl_pct_of_capital > 0)
    total_pnl_pct = sum(t.pnl_pct_of_capital for t in closed)
    return {"n_trades": n, "win_rate": wins / n, "total_pnl_pct": total_pnl_pct}
