"""Modelos SQLAlchemy de persistencia (02_Especificaciones_Tecnicas.md,
sección 9). Inspirado en el patrón de Freqtrade: tablas mínimas para poder
auditar qué hizo el bot y por qué, y reconstruir después el drawdown real
a partir de una curva de capital guardada de verdad (no recalculada a
posteriori).

Tablas:
  - `signals`: TODA señal generada por la estrategia, se haya ejecutado o
    no, con los valores de los indicadores que la originaron -- permite
    auditar por qué se tomó o se descartó una decisión.
  - `trades`: una fila por operación (apertura + cierre eventual), con
    precio de entrada/salida, tamaño, stop-loss, resultado y los ids de
    orden del bróker.
  - `equity_curve`: snapshot periódico del valor de la cuenta.
  - `errors_log`: errores relevantes con contexto, complementario al log
    de texto (todavía no existe utils/logger.py con rotación -- por ahora
    esta tabla es el único registro persistente de errores).
  - `kill_switch_state`: fila única (id=1) con el estado del kill switch
    serializado en JSON -- backend alternativo al archivo local
    (risk/kill_switch.py, persistence/kill_switch_store.py) para cuando el
    bot corre en un entorno efímero (ej. GitHub Actions) donde un archivo
    en disco no sobrevive entre ejecuciones. Mismo principio no
    negociable de la sección 5.4: el estado debe sobrevivir a un reinicio
    del proceso -- "disco" pasa a ser "una fila en la base de datos
    compartida", no cambia el requisito.

Nota: estas tablas son SOLO de auditoría. `core/bot_engine.py` sigue
usando al bróker (`get_open_positions()`) como la única fuente de verdad
para decidir si abrir/cerrar una posición -- ver `persistence/store.py`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, SmallInteger, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY / SELL / HOLD
    price: Mapped[float] = mapped_column(Float, nullable=False)
    ema_fast: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema_slow: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_histogram: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY -- la estrategia es long-only
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stop_loss_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open", index=True)  # open / closed
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pnl_pct_of_capital: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EquitySnapshotRecord(Base):
    __tablename__ = "equity_curve"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    buying_power: Mapped[float] = mapped_column(Float, nullable=False)


class ErrorLogRecord(Base):
    __tablename__ = "errors_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    context: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class KillSwitchStateRecord(Base):
    """Fila única (id siempre 1) con el estado completo del kill switch
    serializado en JSON -- ver persistence/kill_switch_store.py, que es
    quien lee/escribe esta tabla. El propio JSON es la misma
    representación que ya usa risk.kill_switch.KillSwitchState.to_dict(),
    para no duplicar la definición de campos en dos sitios."""

    __tablename__ = "kill_switch_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
