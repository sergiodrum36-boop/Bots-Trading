"""`TradeStore`: el punto de contacto entre `core/bot_engine.py` y la
persistencia. Igual que `BrokerConnector` (connectors/base_connector.py)
desacopla la estrategia del bróker concreto, `TradeStore` desacopla el
orquestador del motor de almacenamiento concreto (aquí, SQLAlchemy sobre
SQLite -- ver persistence/database.py y persistence/models.py). Esto
permite testear `BotEngine` con un `TradeStore` falso, sin tocar disco.

DECISIÓN DE DISEÑO IMPORTANTE: este almacén es SOLO de auditoría.
`core/bot_engine.py` sigue usando `connector.get_open_positions()` (el
bróker) como la ÚNICA fuente de verdad para decidir si abrir o cerrar una
posición (ver el punto 2 del docstring de core/bot_engine.py) --
`TradeStore` nunca se consulta para tomar esa decisión, solo para
registrar lo que pasó. `get_open_trade_id()` es una excepción parcial:
se usa únicamente para saber QUÉ registro de auditoría cerrar cuando el
bróker confirma que una posición se cerró, nunca para decidir si cerrarla.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from persistence.database import session_scope
from persistence.models import EquitySnapshotRecord, ErrorLogRecord, SignalRecord, TradeRecord
from strategy.signals import Signal

logger = logging.getLogger(__name__)


class TradeStore(ABC):
    """Contrato de persistencia que usa `BotEngine`."""

    @abstractmethod
    def record_signal(self, signal: Signal, executed: bool) -> None:
        """Guarda TODA señal generada, se haya ejecutado o no."""

    @abstractmethod
    def record_equity_snapshot(
        self, equity: float, cash: float, buying_power: float, timestamp: datetime
    ) -> None:
        """Guarda un snapshot periódico del valor de la cuenta."""

    @abstractmethod
    def open_trade(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        entry_time: datetime,
        entry_order_id: str,
        stop_loss_price: float,
        reason: str,
    ) -> int | None:
        """Registra la apertura de una operación. Devuelve el id interno
        del registro (para poder cerrarlo después con `close_trade`), o
        `None` si esta implementación no persiste nada de verdad."""

    @abstractmethod
    def get_open_trade_id(self, symbol: str) -> int | None:
        """Id del registro de la operación abierta más reciente de ese
        símbolo, o `None` si no hay ninguna abierta registrada."""

    @abstractmethod
    def close_trade(
        self,
        trade_id: int,
        *,
        exit_price: float,
        exit_time: datetime,
        exit_order_id: str,
        reason: str,
        pnl_pct_of_capital: float | None,
    ) -> None:
        """Marca un registro de operación como cerrado, con su resultado."""

    @abstractmethod
    def record_error(
        self, *, message: str, context: str, symbol: str | None, timestamp: datetime
    ) -> None:
        """Guarda un error relevante con contexto, complementario al log."""


class NullTradeStore(TradeStore):
    """Implementación no-operativa: no persiste nada en ningún lado. Valor
    por defecto de `BotEngine` para no obligar a nadie (tests incluidos) a
    montar una base de datos si no la necesita."""

    def record_signal(self, signal: Signal, executed: bool) -> None:
        return None

    def record_equity_snapshot(self, equity, cash, buying_power, timestamp) -> None:
        return None

    def open_trade(self, **kwargs) -> int | None:
        return None

    def get_open_trade_id(self, symbol: str) -> int | None:
        return None

    def close_trade(self, trade_id, **kwargs) -> None:
        return None

    def record_error(self, **kwargs) -> None:
        return None


def _clean_float(value: float) -> float | None:
    """NaN (típico del periodo de calentamiento de los indicadores) no es
    representable de forma portable en SQLite -- se guarda como NULL."""
    return None if value != value else value  # value != value <=> NaN


class SqlTradeStore(TradeStore):
    """Implementación real, respaldada por SQLAlchemy/SQLite (ver
    persistence/database.py para el engine/sesión y persistence/models.py
    para el esquema)."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def record_signal(self, signal: Signal, executed: bool) -> None:
        with session_scope(self._session_factory) as session:
            session.add(
                SignalRecord(
                    symbol=signal.symbol,
                    timestamp=signal.timestamp,
                    action=signal.action.value,
                    price=signal.price,
                    ema_fast=_clean_float(signal.indicators.ema_fast),
                    ema_slow=_clean_float(signal.indicators.ema_slow),
                    rsi=_clean_float(signal.indicators.rsi),
                    macd_histogram=_clean_float(signal.indicators.macd_histogram),
                    reason=signal.reason,
                    executed=executed,
                    created_at=signal.timestamp,
                )
            )

    def record_equity_snapshot(
        self, equity: float, cash: float, buying_power: float, timestamp: datetime
    ) -> None:
        with session_scope(self._session_factory) as session:
            session.add(
                EquitySnapshotRecord(
                    timestamp=timestamp, equity=equity, cash=cash, buying_power=buying_power
                )
            )

    def open_trade(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        entry_time: datetime,
        entry_order_id: str,
        stop_loss_price: float,
        reason: str,
    ) -> int:
        with session_scope(self._session_factory) as session:
            record = TradeRecord(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                entry_time=entry_time,
                entry_order_id=entry_order_id,
                stop_loss_price=stop_loss_price,
                entry_reason=reason,
                status="open",
                created_at=entry_time,
            )
            session.add(record)
            session.flush()  # asigna el id autoincremental antes del commit
            return record.id

    def get_open_trade_id(self, symbol: str) -> int | None:
        with session_scope(self._session_factory) as session:
            record = (
                session.query(TradeRecord)
                .filter(TradeRecord.symbol == symbol, TradeRecord.status == "open")
                .order_by(TradeRecord.entry_time.desc())
                .first()
            )
            return record.id if record is not None else None

    def close_trade(
        self,
        trade_id: int,
        *,
        exit_price: float,
        exit_time: datetime,
        exit_order_id: str,
        reason: str,
        pnl_pct_of_capital: float | None,
    ) -> None:
        with session_scope(self._session_factory) as session:
            record = session.get(TradeRecord, trade_id)
            if record is None:
                logger.warning(
                    "close_trade: no existe ningun registro de operacion con "
                    "id=%s; no se actualiza nada.",
                    trade_id,
                )
                return
            record.status = "closed"
            record.exit_price = exit_price
            record.exit_time = exit_time
            record.exit_order_id = exit_order_id
            record.exit_reason = reason
            record.pnl_pct_of_capital = pnl_pct_of_capital

    def record_error(
        self, *, message: str, context: str, symbol: str | None, timestamp: datetime
    ) -> None:
        with session_scope(self._session_factory) as session:
            session.add(
                ErrorLogRecord(timestamp=timestamp, symbol=symbol, context=context, message=message)
            )
