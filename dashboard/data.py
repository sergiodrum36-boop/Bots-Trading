"""Funciones de acceso a datos y formato para el panel visual
(dashboard/app.py).

Deliberadamente SIN dependencias de Streamlit: son funciones Python
normales, testables con pytest sin necesidad de levantar la app ni un
navegador. `dashboard/app.py` es una capa fina encima de este módulo que
solo se encarga de dibujar -- toda la lógica no trivial vive aquí.
"""

from __future__ import annotations

from dataclasses import dataclass

from connectors.base_connector import Account, BrokerConnector, ConnectorError, Position
from risk.kill_switch import KillSwitch


@dataclass(frozen=True)
class LiveStatus:
    """Snapshot en vivo de mercado/cuenta/posiciones, consultado
    directamente al bróker en el momento en que se pide (nunca cacheado
    silenciosamente entre símbolos distintos de estado real)."""

    market_open: bool | None  # None si no se pudo consultar
    account: Account | None
    positions: list[Position]
    error: str | None


def fetch_live_status(connector: BrokerConnector) -> LiveStatus:
    """Consulta al bróker el estado actual. Si falla (red, credenciales,
    etc.), devuelve un `LiveStatus` con `error` relleno en vez de
    propagar la excepción -- el panel debe poder mostrar igualmente los
    datos historicos aunque la conexion en vivo falle en ese instante."""
    try:
        market_open = connector.is_market_open()
        account = connector.get_account()
        positions = connector.get_open_positions()
        return LiveStatus(market_open=market_open, account=account, positions=positions, error=None)
    except ConnectorError as exc:
        return LiveStatus(market_open=None, account=None, positions=[], error=str(exc))


@dataclass(frozen=True)
class KillSwitchSummary:
    active: bool
    reason: str | None
    since: str | None
    daily_pnl_pct: float
    consecutive_losses: int
    trades_today: int
    connection_lost: bool


def summarize_kill_switch(kill_switch: KillSwitch) -> KillSwitchSummary:
    state = kill_switch.state
    return KillSwitchSummary(
        active=state.triggered,
        reason=state.trigger_reason,
        since=state.triggered_at,
        daily_pnl_pct=state.daily_pnl_pct,
        consecutive_losses=state.consecutive_losses,
        trades_today=state.trades_today,
        connection_lost=state.connection_lost_at is not None,
    )


def format_currency(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def format_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{decimals}f}%"


def format_optional(value) -> str:
    return "-" if value is None else str(value)
