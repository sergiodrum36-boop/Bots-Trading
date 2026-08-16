"""Entry point de paper trading (Fase 1 obligatoria):
ejecuta UN ciclo completo del bot (datos -> estrategia -> riesgo ->
ejecucion) contra el sandbox de PAPER TRADING de Alpaca, con dinero
simulado (02_Especificaciones_Tecnicas.md, secciones 1 y 2).

IMPORTANTE -- este script SI puede enviar ordenes reales de paper trading
(no reales de mercado, pero si reales dentro de tu cuenta sandbox de
Alpaca) si la senal del dia asi lo indica para alguno de los simbolos. No
es una simulacion de lectura solamente, a diferencia de
`probar_conexion.py` (solo lee datos) y `scripts/run_backtest.py` (solo
lee datos historicos). Siempre usa `paper=True` -- pasar a cuenta real
(Fase 2) no esta soportado por este script.

Pensado para invocarse UNA VEZ por ejecucion (no tiene bucle propio) desde
un programador de tareas externo (Task Scheduler en Windows, cron en
Linux/Mac), tipicamente una vez al dia poco despues de la apertura del
mercado -- ver el docstring de core/bot_engine.py para la justificacion de
este modelo de invocacion.

Requiere APCA_API_KEY_ID y APCA_API_SECRET_KEY en un archivo .env en la
raiz del proyecto (copia .env.example y rellena tus claves de PAPER
TRADING -- nunca las de cuenta real).

El estado del kill switch se persiste de forma duradera: por defecto en
disco (`kill_switch_state.json` en el directorio desde el que se ejecute
el script); si `--db-path`/`TRADING_BOT_DATABASE_URL` apunta a una base de
datos (Postgres/Supabase) en vez de a un archivo SQLite local, el estado
del kill switch pasa a guardarse ahi tambien (tabla `kill_switch_state`,
ver persistence/kill_switch_store.py) -- necesario para poder correr este
script desde un entorno efimero como GitHub Actions, que no conserva
ningun archivo entre ejecuciones. En cualquier caso, si el kill switch ya
esta activo de una ejecucion anterior, este script lo respeta (no reanuda
solo -- ver risk/kill_switch.py).

Cada ciclo tambien queda registrado en una base de datos de auditoria (por
defecto SQLite local, `trading_bot.dryrun.sqlite`; o la URL de
`TRADING_BOT_DATABASE_URL`/`--db-path` si se indica, ver
persistence/database.py): todas las senales generadas, las operaciones
abiertas/cerradas y los errores. Es solo auditoria -- no cambia ninguna
decision del bot (ver el punto 2 del docstring de core/bot_engine.py).

Uso:
    python scripts/run_paper_trading.py
    python scripts/run_paper_trading.py --symbols SPY QQQ
    python scripts/run_paper_trading.py --verbose
    # Contra una base de datos remota (Postgres/Supabase) en vez de SQLite
    # local -- tipico al correr desde GitHub Actions, ver
    # .github/workflows/ciclo_diario.yml:
    python scripts/run_paper_trading.py --db-path "$TRADING_BOT_DATABASE_URL"

Los parametros de estrategia, riesgo, stop-loss y kill switch se leen de
`config/config.yaml` (ver `config/settings.py`) -- si no existe, se usan
los mismos valores por defecto de siempre. `--symbols` en la linea de
comandos, si se indica, tiene prioridad sobre el archivo.

NOTIFICACIONES (notifications/notifier.py, seccion 10 de las
especificaciones): si `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` estan
configurados en `.env`, este script avisa por Telegram cuando: (a) el
ciclo se aborta por un fallo de conexion, (b) el kill switch se activa
DURANTE este ciclo (no se reavisa cada dia mientras siga activo -- solo
la primera vez), o (c) algun simbolo tuvo un error no controlado. El
resumen diario de operativa es opcional, vease `--notify-summary`. Sin
esas variables configuradas, el comportamiento es exactamente el mismo
que antes de que existiera este modulo (no bloqueante, ver
notifications/notifier.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigValidationError, load_settings  # noqa: E402
from connectors.alpaca_connector import AlpacaConnector  # noqa: E402
from connectors.base_connector import ConnectorAuthenticationError, ConnectorError  # noqa: E402
from core.bot_engine import BotEngine, BotEngineConfig, CycleResult  # noqa: E402
from execution.order_manager import OrderManager  # noqa: E402
from persistence.database import (  # noqa: E402
    is_db_url,
    make_engine,
    make_session_factory,
    resolve_db_path,
)
from notifications.notifier import Notifier, get_notifier  # noqa: E402
from persistence.kill_switch_store import SqlKillSwitchStateStore  # noqa: E402
from persistence.store import SqlTradeStore  # noqa: E402
from risk.kill_switch import DEFAULT_STATE_FILE, KillSwitch  # noqa: E402
from strategy.trend_strategy import TrendStrategy  # noqa: E402
from utils.logger import DEFAULT_LOG_DIR, setup_logging  # noqa: E402


def _parse_args(
    argv: list[str] | None, default_symbols: list[str]
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ejecuta UN ciclo de paper trading (Fase 1) contra el sandbox de "
            "Alpaca. Puede enviar ordenes reales de PAPER TRADING (dinero "
            "simulado) si la senal del dia lo indica."
        )
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=default_symbols,
        help=f"Simbolos a operar (por defecto, de config/config.yaml: {', '.join(default_symbols)})",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help=f"Ruta del archivo de estado del kill switch (por defecto: {DEFAULT_STATE_FILE})",
    )
    parser.add_argument(
        "--db-path",
        default=resolve_db_path(paper=True),
        help=(
            "Ruta de archivo SQLite o URL de conexion (ej. postgresql://...) "
            "de la base de datos de auditoria. Por defecto usa la variable de "
            "entorno TRADING_BOT_DATABASE_URL si esta definida, si no un "
            "archivo SQLite local."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Muestra logs INFO por consola (por defecto solo WARNING/ERROR); "
        "el archivo de log en disco siempre registra INFO, ver --log-dir.",
    )
    parser.add_argument(
        "--log-dir",
        default=DEFAULT_LOG_DIR,
        help=f"Carpeta donde escribir los logs rotados (por defecto: {DEFAULT_LOG_DIR}/)",
    )
    parser.add_argument(
        "--notify-summary",
        action="store_true",
        help=(
            "Ademas de los avisos criticos (kill switch, errores, conexion "
            "caida), envia tambien un resumen del ciclo por Telegram aunque "
            "no haya pasado nada destacable -- por defecto solo se avisa de "
            "lo critico, para no generar un mensaje diario de puro tramite."
        ),
    )
    return parser.parse_args(argv)


def _format_symbol_line(symbol_result) -> str:
    line = f"{symbol_result.symbol}: {symbol_result.action.value}"
    if symbol_result.executed:
        line += " (ejecutado)"
    return line


def send_notifications(
    notifier: Notifier,
    result: CycleResult,
    kill_switch: KillSwitch,
    kill_switch_was_active_before: bool,
    notify_summary: bool,
) -> None:
    """Envia los avisos criticos que correspondan segun `result`
    (ver el docstring del modulo). Un fallo de `notifier.notify(...)`
    nunca llega aqui como excepcion (ver notifications/notifier.py) --
    esta funcion tampoco lanza nada por su cuenta, para no poder tumbar
    el ciclo de trading por un problema de notificaciones."""
    if result.aborted:
        notifier.notify(
            f"⚠️ Bot de Trading: ciclo abortado -- {result.abort_reason}"
        )

    # Solo se avisa la PRIMERA vez que se dispara (no cada dia que sigue
    # activo esperando un reset() manual) -- comparar el estado ANTES vs
    # DESPUES de este ciclo evita repetir el mismo aviso indefinidamente.
    if not kill_switch_was_active_before and result.kill_switch_active:
        notifier.notify(
            f"🛑 Bot de Trading: KILL SWITCH ACTIVADO -- "
            f"{kill_switch.state.trigger_reason}. Requiere reset() manual "
            f"para reanudar."
        )

    symbol_errors = [sr for sr in result.symbol_results if sr.error is not None]
    if symbol_errors:
        detalle = "; ".join(f"{sr.symbol}: {sr.error}" for sr in symbol_errors)
        notifier.notify(f"⚠️ Bot de Trading: errores en el ciclo -- {detalle}")

    if notify_summary:
        if result.aborted:
            resumen = f"ciclo abortado ({result.abort_reason})"
        elif not result.symbol_results:
            resumen = (
                "kill switch activo, ningun simbolo evaluado"
                if result.kill_switch_active
                else "mercado cerrado, ningun simbolo evaluado"
            )
        else:
            resumen = "; ".join(_format_symbol_line(sr) for sr in result.symbol_results)
        notifier.notify(f"📋 Bot de Trading: resumen del ciclo -- {resumen}")


def print_report(result: CycleResult) -> None:
    print(f"\n=== Ciclo de paper trading -- {result.started_at.isoformat()} ===")
    print(f"Mercado abierto: {result.market_open}")
    print(f"Kill switch activo: {result.kill_switch_active}")

    if result.aborted:
        print(f"\nCICLO ABORTADO: {result.abort_reason}")
        print(
            "No se evaluo ningun simbolo. Revisa la conexion/credenciales y "
            "vuelve a intentarlo; si esto persiste, el kill switch puede "
            "acabar disparandose por perdida de conexion prolongada (ver "
            "risk/kill_switch.py)."
        )
        return

    if not result.symbol_results:
        if result.kill_switch_active:
            print(
                "\nNingun simbolo evaluado: el kill switch esta activo. "
                "Requiere un reset() manual explicito para reanudar "
                "(revisa risk/kill_switch.py antes de resetear)."
            )
        else:
            print("\nNingun simbolo evaluado: el mercado esta cerrado ahora mismo.")
        return

    print("\n=== Resultado por simbolo ===")
    for symbol_result in result.symbol_results:
        print(f"\n  {symbol_result.symbol}: senal={symbol_result.action.value}  "
              f"ejecutado={symbol_result.executed}")
        print(f"    motivo: {symbol_result.reason}")
        if symbol_result.order is not None:
            order = symbol_result.order
            print(
                f"    orden: id={order.broker_order_id} estado={order.status.value} "
                f"qty_pedida={order.qty} qty_ejecutada={order.filled_qty} "
                f"precio_medio={order.filled_avg_price}"
            )
        if symbol_result.error is not None:
            print(f"    ERROR: {symbol_result.error}")


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
    except ConfigValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Corrige config/config.yaml (o borralo para volver a los "
            "valores por defecto) antes de reintentar -- ver config/settings.py.",
            file=sys.stderr,
        )
        return 1

    args = _parse_args(argv, default_symbols=list(settings.symbols))
    setup_logging(verbose=args.verbose, log_dir=args.log_dir)

    try:
        connector = AlpacaConnector(paper=True)
    except ConnectorAuthenticationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Copia .env.example a .env y rellena tus claves de PAPER TRADING "
            "(https://app.alpaca.markets/paper/dashboard/overview).",
            file=sys.stderr,
        )
        return 1

    session_factory = make_session_factory(make_engine(args.db_path))
    kill_switch_config = settings.to_kill_switch_config()

    if is_db_url(args.db_path):
        # Base de datos remota (Postgres/Supabase): el estado del kill
        # switch tambien se guarda ahi (no en --state-file), porque un
        # entorno como GitHub Actions no conserva ningun archivo local
        # entre ejecuciones -- ver persistence/kill_switch_store.py.
        kill_switch = KillSwitch(
            config=kill_switch_config, store=SqlKillSwitchStateStore(session_factory)
        )
        kill_switch_backend_msg = "base de datos remota (tabla kill_switch_state)"
    else:
        kill_switch = KillSwitch(config=kill_switch_config, state_file=args.state_file)
        kill_switch_backend_msg = f"archivo local ({args.state_file})"

    # Estado ANTES del ciclo, para poder distinguir "se acaba de disparar
    # ahora" de "sigue disparado desde hace dias" -- ver send_notifications().
    kill_switch_was_active_before = kill_switch.is_active()
    notifier = get_notifier()

    strategy = TrendStrategy(settings.to_trend_strategy_config())
    order_manager = OrderManager(connector=connector, kill_switch=kill_switch)
    config = BotEngineConfig(
        symbols=tuple(args.symbols),
        timeframe=settings.timeframe,
        bars_limit=settings.bars_limit,
        strategy_config=settings.to_trend_strategy_config(),
        risk_pct=settings.risk.risk_pct,
        max_position_pct=settings.risk.max_position_pct,
        stop_loss_config=settings.to_stop_loss_config(),
    )
    store = SqlTradeStore(session_factory)
    engine = BotEngine(
        connector=connector,
        strategy=strategy,
        kill_switch=kill_switch,
        order_manager=order_manager,
        config=config,
        store=store,
    )

    print(
        "Modo: PAPER TRADING (dinero simulado). Este ciclo puede enviar "
        "ordenes de mercado dentro de tu cuenta sandbox de Alpaca si la "
        "senal del dia lo indica."
    )
    print(f"Base de datos de auditoria: {args.db_path}")
    print(f"Estado del kill switch: {kill_switch_backend_msg}")

    try:
        result = engine.run_cycle()
    except ConnectorError as exc:
        notifier.notify(f"⚠️ Bot de Trading: fallo no controlado consultando Alpaca -- {exc}")
        print(f"ERROR consultando Alpaca: {exc}", file=sys.stderr)
        return 1

    send_notifications(
        notifier, result, kill_switch, kill_switch_was_active_before, args.notify_summary
    )
    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
