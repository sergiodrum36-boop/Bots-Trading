"""Panel visual del bot de trading.

Panel de SOLO CONSULTA (más un único control: reset manual del kill
switch, con confirmación en dos pasos) sobre el estado del bot: cuenta y
posiciones en vivo (vía Alpaca), estado del kill switch, historial de
operaciones, curva de capital y registro de señales -- con el motivo de
cada una, para poder auditar "por qué hizo esto" (persistence/, sección 9
de las especificaciones).

Cómo lanzarlo:
    streamlit run dashboard/app.py
(o haciendo doble clic en `iniciar_panel_app.vbs`, que lo abre en su
propia ventana de escritorio sin depender del navegador ni mostrar una
ventana de cmd -- ver desktop_app.py. `iniciar_panel.bat` se conserva como
modo de respaldo/depuración: abre el panel en el navegador de siempre).

Decisiones de diseño:
  - Solo lectura + un único control (reset del kill switch). Deliberadamente
    NO incluye botones para forzar el cierre de una posición ni para
    enviar órdenes manuales -- añaden superficie real de error con dinero
    (simulado) de por medio; se puede añadir en una segunda iteración si
    hace falta (ver core/bot_engine.py y execution/order_manager.py para
    lo que ya existe si se quisiera exponer).
  - Los datos "EN VIVO" (cuenta, posiciones, mercado abierto) se consultan
    directamente a Alpaca en cada recarga de la página. Los datos
    HISTÓRICOS (operaciones, señales, curva de capital) vienen de la base
    de datos de auditoría de persistence/ -- si está vacía (todavía no se
    ha ejecutado ningún ciclo con persistencia activada, ver
    scripts/run_paper_trading.py), las tablas y gráficos aparecen vacíos;
    no es un error.
  - Si falla la conexión en vivo con Alpaca (credenciales, red), el panel
    lo indica con un aviso pero sigue mostrando el historial guardado --
    nunca se cae entero solo porque el bróker no responda en ese instante.
  - Toda la lógica no trivial (acceso a datos, formato) vive en
    dashboard/data.py y persistence/queries.py, testables con pytest sin
    Streamlit -- este archivo es una capa fina que solo dibuja.
  - TEMA VISUAL (Etapa 1 del rediseño "Panel v2", 2026-08-16): paleta base
    en .streamlit/config.toml (fondo oscuro, acento #5B8DEF), y aquí se
    inyecta CSS propio (_PANEL_CSS más abajo) para la tipografía (Inter),
    las tarjetas KPI y el verde/rojo suavizado del P&L -- ver
    render_kpi_card(). No se tocó nada de la lógica de arriba, solo cómo
    se dibuja.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from config.settings import BotSettings, ConfigValidationError, load_settings  # noqa: E402
from connectors.alpaca_connector import AlpacaConnector  # noqa: E402
from connectors.base_connector import ConnectorAuthenticationError  # noqa: E402
from dashboard.data import (  # noqa: E402
    fetch_live_status,
    format_currency,
    format_optional,
    format_pct,
    pnl_tone,
    summarize_kill_switch,
)
from persistence.database import is_db_url, make_engine, make_session_factory, resolve_db_path  # noqa: E402
from persistence.kill_switch_store import SqlKillSwitchStateStore  # noqa: E402
from persistence.queries import (  # noqa: E402
    list_equity_curve,
    list_errors,
    list_signals,
    list_trades,
    summarize_closed_trades,
)
from risk.kill_switch import DEFAULT_STATE_FILE, KillSwitch  # noqa: E402
from risk.stop_loss import StopLossMethod  # noqa: E402

st.set_page_config(page_title="Bot de Trading -- Panel", page_icon="\U0001F4C8", layout="wide")

# ----------------------------------------------------------------------
# Tema visual (Etapa 1 del rediseno "Panel v2"): tipografia Inter,
# tarjetas KPI con estilo propio (para poder colorear el valor del P&L
# sin depender de la estructura HTML interna de st.metric, que puede
# cambiar entre versiones de Streamlit), verde/rojo suavizados, y ocultar
# el menu hamburguesa/barra de herramientas (incluye el boton "Deploy")/
# pie de pagina de Streamlit -- no aportan nada en un panel de uso
# personal, y menos aun envuelto en su propia ventana de escritorio (ver
# desktop_app.py). La paleta base (fondo, acento) viene de
# .streamlit/config.toml.
_PANEL_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}

#MainMenu, footer, [data-testid="stToolbar"] { visibility: hidden; }

.block-container { padding-top: 2rem; }

.kpi-card {
    background-color: #161B2E;
    border: 1px solid #232A44;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
}

.kpi-label {
    color: #9CA3AF;
    font-weight: 500;
    letter-spacing: 0.03em;
    text-transform: uppercase;
    font-size: 0.72rem;
    margin-bottom: 6px;
}

.kpi-value {
    font-weight: 700;
    font-size: 1.6rem;
    color: #E5E7EB;
    line-height: 1.3;
}

.kpi-positive { color: #3FB68B; }
.kpi-negative { color: #E0556B; }
.kpi-neutral  { color: #E5E7EB; }

[data-testid="stDataFrame"] {
    border: 1px solid #232A44;
    border-radius: 8px;
    overflow: hidden;
}
</style>
"""
st.markdown(_PANEL_CSS, unsafe_allow_html=True)


def render_kpi_card(container, label: str, value: str, tone: str = "neutral") -> None:
    """Dibuja una tarjeta KPI (etiqueta + valor grande) con estilo propio
    en vez de st.metric -- da control total sobre el color del valor
    (verde/rojo suavizado para P&L y kill switch) sin depender de la
    estructura HTML interna de st.metric. `container` es una columna o el
    propio `st` (cualquier objeto con `.markdown()`)."""
    tone_class = {"positive": "kpi-positive", "negative": "kpi-negative"}.get(tone, "kpi-neutral")
    container.markdown(
        f"""
        <div class="kpi-card">
            <div class="kpi-label">{html.escape(label)}</div>
            <div class="kpi-value {tone_class}">{html.escape(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Config/config.yaml (o los valores por defecto de siempre si no existe o
# no se puede leer -- ver config/settings.py) alimenta tanto el kill
# switch como la pestana "Configuracion" mas abajo. Un config.yaml
# invalido no debe tumbar el panel de consulta entero -- se avisa y se
# sigue con los valores por defecto, a diferencia de los scripts de
# ejecucion (run_paper_trading.py/run_backtest.py), que si abortan.
try:
    settings = load_settings()
    settings_error: str | None = None
except ConfigValidationError as exc:
    settings = BotSettings()
    settings_error = str(exc)

# ----------------------------------------------------------------------
# Barra lateral: de donde leer los datos
# ----------------------------------------------------------------------
st.sidebar.title("\U0001F4C8 Bot de Trading")
st.sidebar.caption("Panel de solo consulta -- Fase 1, paper trading")

db_path = st.sidebar.text_input(
    "Base de datos de auditoria",
    value=resolve_db_path(paper=True),
    help=(
        "Ruta de archivo SQLite local, o una URL de conexion "
        "(ej. postgresql://...) si el bot esta corriendo en GitHub Actions "
        "contra Supabase. Si es una URL, tambien se usa para el estado del "
        "kill switch (el campo de abajo se ignora en ese caso)."
    ),
)
state_file = st.sidebar.text_input(
    "Archivo de estado del kill switch",
    value=DEFAULT_STATE_FILE,
    disabled=is_db_url(db_path),
)

if st.sidebar.button("\U0001F504 Actualizar ahora", use_container_width=True):
    st.rerun()

st.sidebar.caption(f"Actualizado: {pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M:%S UTC')}")

if settings_error:
    st.sidebar.error(
        f"config/config.yaml tiene un problema, se estan usando los "
        f"valores por defecto: {settings_error}"
    )

# ----------------------------------------------------------------------
# Conexion en vivo con Alpaca (paper trading) + estado del kill switch
# ----------------------------------------------------------------------
connector = None
connector_error = None
try:
    connector = AlpacaConnector(paper=True)
except ConnectorAuthenticationError as exc:
    connector_error = str(exc)

session_factory = make_session_factory(make_engine(db_path))

kill_switch_config = settings.to_kill_switch_config()

if is_db_url(db_path):
    # Misma base de datos remota para el historial y el kill switch -- ver
    # persistence/kill_switch_store.py. El campo "Archivo de estado del
    # kill switch" de la barra lateral se ignora en este caso.
    kill_switch = KillSwitch(config=kill_switch_config, store=SqlKillSwitchStateStore(session_factory))
else:
    kill_switch = KillSwitch(config=kill_switch_config, state_file=state_file)
kill_switch_summary = summarize_kill_switch(kill_switch)

st.title("Estado del bot")

live = None
if connector_error:
    st.error(
        f"No se pudo conectar con Alpaca: {connector_error}. Revisa tu "
        f"archivo .env (ver .env.example). Mientras tanto, el panel sigue "
        f"mostrando el historial guardado."
    )
else:
    live = fetch_live_status(connector)
    if live.error:
        st.warning(
            f"No se pudo consultar el estado en vivo: {live.error}. "
            f"Mostrando solo el historial guardado."
        )

col1, col2, col3, col4, col5 = st.columns(5)
render_kpi_card(
    col1, "Equity", format_currency(live.account.equity) if live and live.account else "-"
)
render_kpi_card(col2, "Cash", format_currency(live.account.cash) if live and live.account else "-")
render_kpi_card(
    col3,
    "Buying power",
    format_currency(live.account.buying_power) if live and live.account else "-",
)
if live is not None and live.market_open is not None:
    mercado_label = "Abierto" if live.market_open else "Cerrado"
else:
    mercado_label = "-"
render_kpi_card(col4, "Mercado", mercado_label)
render_kpi_card(
    col5,
    "Kill switch",
    "Activo" if kill_switch_summary.active else "Inactivo",
    tone="negative" if kill_switch_summary.active else "positive",
)

# ----------------------------------------------------------------------
# Kill switch: detalle + reset manual (confirmacion en dos pasos)
# ----------------------------------------------------------------------
with st.expander("Detalle del kill switch", expanded=kill_switch_summary.active):
    daily_pnl_tone = pnl_tone(kill_switch_summary.daily_pnl_pct)
    daily_pnl_class = {"positive": "kpi-positive", "negative": "kpi-negative"}.get(
        daily_pnl_tone, "kpi-neutral"
    )
    st.markdown(
        f"**P&L acumulado hoy:** "
        f"<span class='{daily_pnl_class}'>"
        f"{html.escape(format_pct(kill_switch_summary.daily_pnl_pct))}</span>",
        unsafe_allow_html=True,
    )
    st.write(f"**Perdidas consecutivas:** {kill_switch_summary.consecutive_losses}")
    st.write(f"**Operaciones hoy:** {kill_switch_summary.trades_today}")
    st.write(f"**Conexion caida ahora mismo:** {'Si' if kill_switch_summary.connection_lost else 'No'}")

    if kill_switch_summary.active:
        st.error(
            f"Motivo del disparo: {format_optional(kill_switch_summary.reason)} "
            f"(desde {format_optional(kill_switch_summary.since)})"
        )

        if "confirm_reset" not in st.session_state:
            st.session_state.confirm_reset = False

        if not st.session_state.confirm_reset:
            if st.button("Resetear kill switch"):
                st.session_state.confirm_reset = True
                st.rerun()
        else:
            st.warning(
                "Esto reanuda la operativa del bot inmediatamente. Solo "
                "hazlo despues de revisar por que se disparo -- ver el "
                "motivo de arriba."
            )
            confirmed_by = st.text_input(
                "Tu nombre o iniciales (queda registrado)", value="", key="confirm_by"
            )
            reset_col1, reset_col2 = st.columns(2)
            with reset_col1:
                if st.button("Si, confirmar reset", disabled=not confirmed_by.strip()):
                    kill_switch.reset(confirmed_by=confirmed_by.strip())
                    st.session_state.confirm_reset = False
                    st.success("Kill switch reseteado.")
                    st.rerun()
            with reset_col2:
                if st.button("Cancelar"):
                    st.session_state.confirm_reset = False
                    st.rerun()
    else:
        st.session_state.confirm_reset = False

# ----------------------------------------------------------------------
# Posiciones abiertas (en vivo)
# ----------------------------------------------------------------------
st.subheader("Posiciones abiertas")
if live is not None and live.positions:
    positions_df = pd.DataFrame(
        [
            {
                "Simbolo": p.symbol,
                "Cantidad": p.qty,
                "Precio medio entrada": p.avg_entry_price,
                "Valor de mercado": p.market_value,
                "P&L no realizado": p.unrealized_pl,
            }
            for p in live.positions
        ]
    )
    st.dataframe(positions_df, use_container_width=True, hide_index=True)
elif live is not None:
    st.caption("Sin posiciones abiertas ahora mismo.")
else:
    st.caption("No se pudo consultar (ver aviso de arriba).")

# ----------------------------------------------------------------------
# Pestanas: historial / curva de capital / senales / configuracion / errores
# ----------------------------------------------------------------------
tab_trades, tab_equity, tab_signals, tab_config, tab_errors = st.tabs(
    ["Historial de operaciones", "Curva de capital", "Senales", "Configuracion", "Errores"]
)

with tab_trades:
    summary = summarize_closed_trades(session_factory)
    sum_col1, sum_col2, sum_col3 = st.columns(3)
    render_kpi_card(sum_col1, "Operaciones cerradas", str(summary["n_trades"]))
    render_kpi_card(
        sum_col2,
        "Win rate",
        format_pct(summary["win_rate"]) if summary["win_rate"] is not None else "-",
    )
    render_kpi_card(
        sum_col3,
        "P&L acumulado",
        format_pct(summary["total_pnl_pct"]),
        tone=pnl_tone(summary["total_pnl_pct"]),
    )

    trades = list_trades(session_factory)
    if trades:
        trades_df = pd.DataFrame(
            [
                {
                    "Simbolo": t.symbol,
                    "Estado": t.status,
                    "Cantidad": t.qty,
                    "Entrada": t.entry_price,
                    "Fecha entrada": t.entry_time,
                    "Salida": t.exit_price,
                    "Fecha salida": t.exit_time,
                    "Stop-loss": t.stop_loss_price,
                    "P&L (% capital)": t.pnl_pct_of_capital,
                    "Motivo entrada": t.entry_reason,
                    "Motivo salida": t.exit_reason,
                }
                for t in trades
            ]
        )
        st.dataframe(trades_df, use_container_width=True, hide_index=True)
    else:
        st.caption("Todavia no hay ninguna operacion registrada.")

with tab_equity:
    equity_rows = list_equity_curve(session_factory)
    if equity_rows:
        equity_df = pd.DataFrame(
            {
                "timestamp": [e.timestamp for e in equity_rows],
                "equity": [e.equity for e in equity_rows],
            }
        ).set_index("timestamp")
        st.line_chart(equity_df)
    else:
        st.caption("Todavia no hay snapshots de capital guardados.")

with tab_signals:
    symbol_filter = st.selectbox("Filtrar por simbolo", options=["(todos)"] + list(settings.symbols))
    signals = list_signals(
        session_factory, symbol=None if symbol_filter == "(todos)" else symbol_filter
    )
    if signals:
        signals_df = pd.DataFrame(
            [
                {
                    "Fecha": s.timestamp,
                    "Simbolo": s.symbol,
                    "Senal": s.action,
                    "Ejecutada": s.executed,
                    "Precio": s.price,
                    "RSI": s.rsi,
                    "EMA rapida": s.ema_fast,
                    "EMA lenta": s.ema_slow,
                    "MACD hist.": s.macd_histogram,
                    "Motivo": s.reason,
                }
                for s in signals
            ]
        )
        st.dataframe(signals_df, use_container_width=True, hide_index=True)
    else:
        st.caption("Todavia no hay senales registradas.")

with tab_config:
    st.caption(
        "Valores cargados desde config/config.yaml -- si el archivo no "
        "existe, se usan los mismos valores por defecto que ya traia "
        "cada modulo (ver config/settings.py)."
    )
    st.write(f"**Simbolos por defecto:** {', '.join(settings.symbols)}")
    st.write(f"**Timeframe:** {settings.timeframe.value}")
    st.write(f"**Riesgo por operacion:** {format_pct(settings.risk.risk_pct)} del capital")
    st.write(f"**Maximo por posicion:** {format_pct(settings.risk.max_position_pct)} del capital")
    if settings.stop_loss.method == StopLossMethod.FIXED_PERCENT:
        st.write(
            f"**Stop-loss fijo:** {format_pct(settings.stop_loss.fixed_percent)} "
            f"por debajo del precio de entrada"
        )
    else:
        st.write(f"**Stop-loss ATR:** multiplicador {settings.stop_loss.atr_multiplier}")
    st.write(
        f"**Kill switch -- perdida diaria maxima:** "
        f"{format_pct(settings.kill_switch.max_daily_loss_pct)} del capital"
    )
    st.write(
        f"**Kill switch -- perdidas consecutivas maximas:** "
        f"{settings.kill_switch.max_consecutive_losses}"
    )
    st.write(
        f"**Kill switch -- operaciones maximas por dia:** "
        f"{settings.kill_switch.max_trades_per_day}"
    )
    st.write(f"**Base de datos de auditoria:** `{db_path}`")
    if is_db_url(db_path):
        st.write("**Estado del kill switch:** base de datos remota (tabla `kill_switch_state`)")
    else:
        st.write(f"**Archivo de estado del kill switch:** `{state_file}`")

with tab_errors:
    errors = list_errors(session_factory)
    if errors:
        errors_df = pd.DataFrame(
            [
                {
                    "Fecha": e.timestamp,
                    "Simbolo": e.symbol or "(general)",
                    "Contexto": e.context,
                    "Mensaje": e.message,
                }
                for e in errors
            ]
        )
        st.dataframe(errors_df, use_container_width=True, hide_index=True)
    else:
        st.caption("Sin errores registrados.")
