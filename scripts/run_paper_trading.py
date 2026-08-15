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
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.alpaca_connector import DEFAULT_SYMBOLS, AlpacaConnector  # noqa: E402
from connectors.base_connector import ConnectorAuthenticationError, ConnectorError  # noqa: E402
from core.bot_engine import BotEngine, BotEngineConfig, CycleResult  # noqa: E402
from execution.order_manager import OrderManager  # noqa: E402
from persistence.database import (  # noqa: E402
    is_db_url,
    make_engine,
    make_session_factory,
    resolve_db_path,
)
from persistence.kill_switch_store import SqlKillSwitchStateStore  # noqa: E402
from persistence.store import SqlTradeStore  # noqa: E402
from risk.kill_switch import DEFAULT_STATE_FILE, KillSwitch  # noqa: E402
from strategy.trend_strategy import TrendStrategy, TrendStrategyConfig  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        default=list(DEFAULT_SYMBOLS),
        help=f"Simbolos a operar (por defecto: {', '.join(DEFAULT_SYMBOLS)})",
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
        help="Muestra logs INFO del proceso (por defecto solo WARNING/ERROR)",
    )
    return parser.parse_args(argv)


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
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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

    if is_db_url(args.db_path):
        # Base de datos remota (Postgres/Supabase): el estado del kill
        # switch tambien se guarda ahi (no en --state-file), porque un
        # entorno como GitHub Actions no conserva ningun archivo local
        # entre ejecuciones -- ver persistence/kill_switch_store.py.
        kill_switch = KillSwitch(store=SqlKillSwitchStateStore(session_factory))
        kill_switch_backend_msg = "base de datos remota (tabla kill_switch_state)"
    else:
        kill_switch = KillSwitch(state_file=args.state_file)
        kill_switch_backend_msg = f"archivo local ({args.state_file})"

    strategy = TrendStrategy(TrendStrategyConfig())
    order_manager = OrderManager(connector=connector, kill_switch=kill_switch)
    config = BotEngineConfig(symbols=tuple(args.symbols))
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
        print(f"ERROR consultando Alpaca: {exc}", file=sys.stderr)
        return 1

    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
