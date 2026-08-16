"""Entry point de backtesting (Fase 0): ejecuta la estrategia de tendencia
sobre datos historicos REALES de Alpaca y muestra las metricas de
validacion (02_Especificaciones_Tecnicas.md, secciones 4 y 12).

No ejecuta ninguna orden ni de paper trading ni real: solo lee datos
historicos de mercado.

Requiere APCA_API_KEY_ID y APCA_API_SECRET_KEY en un archivo .env en la
raiz del proyecto (copia .env.example y rellena tus claves de PAPER
TRADING -- nunca las de cuenta real).

Uso:
    python scripts/run_backtest.py --start 2023-01-01
    python scripts/run_backtest.py --symbols SPY QQQ --start 2023-01-01 --end 2026-01-01
    python scripts/run_backtest.py --start 2023-01-01 --verbose

Los simbolos por defecto y los parametros de la estrategia (EMA/RSI/MACD)
se leen de `config/config.yaml` (ver `config/settings.py`) -- asi el
backtest valida la MISMA configuracion que despues usa
scripts/run_paper_trading.py, no una distinta. `--symbols` en la linea de
comandos, si se indica, tiene prioridad sobre el archivo.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtesting.backtest_runner import BacktestResult, run_backtest  # noqa: E402
from config.settings import ConfigValidationError, load_settings  # noqa: E402
from connectors.alpaca_connector import AlpacaConnector  # noqa: E402
from connectors.base_connector import ConnectorAuthenticationError, ConnectorError  # noqa: E402
from utils.logger import DEFAULT_LOG_DIR, setup_logging  # noqa: E402


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Fecha invalida '{value}': usa el formato AAAA-MM-DD"
        ) from exc


def _parse_args(argv: list[str] | None, default_symbols: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest de la estrategia de tendencia (Fase 0) sobre datos historicos de Alpaca."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=default_symbols,
        help=f"Simbolos a testear (por defecto, de config/config.yaml: {', '.join(default_symbols)})",
    )
    parser.add_argument(
        "--start", type=_parse_date, required=True, help="Fecha de inicio, formato AAAA-MM-DD"
    )
    parser.add_argument(
        "--end",
        type=_parse_date,
        default=datetime.now(timezone.utc),
        help="Fecha de fin, formato AAAA-MM-DD (por defecto: hoy)",
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
    return parser.parse_args(argv)


def _format_metric(value) -> str:
    if value is None:
        return "no aplica combinando varios simbolos (ver aviso mas abajo)"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def print_report(result: BacktestResult) -> None:
    print("\n=== Metricas globales (todos los simbolos combinados) ===")
    for key, value in result.metrics.as_dict().items():
        print(f"  {key}: {_format_metric(value)}")

    print("\n=== Desglose por simbolo ===")
    for symbol, metrics in result.per_symbol_metrics.items():
        print(f"\n  {symbol}:")
        for key, value in metrics.as_dict().items():
            print(f"    {key}: {_format_metric(value)}")

    print(
        "\nAVISO: sharpe_ratio y max_drawdown de esta version se calculan "
        "sobre la secuencia de retornos POR OPERACION (no sobre una curva "
        "de equity diaria) y NO estan anualizados -- son una medida de "
        "consistencia relativa de la senal, no un Sharpe de cartera "
        "'oficial'. win_rate y profit_factor si son estandar. Ver el "
        "docstring de backtesting/backtest_runner.py para el detalle "
        "completo antes de usar estas cifras para decidir nada."
    )

    if result.metrics.total_return_pct is None:
        print(
            "\nAVISO: con varios simbolos combinados, total_return_pct y "
            "max_drawdown GLOBALES no se muestran (salen como 'None') "
            "porque componerlos secuencialmente simularia un unico capital "
            "operacion tras operacion, cuando en la practica estaria "
            "repartido EN PARALELO entre los simbolos -- daria una cifra "
            "irreal (por ejemplo, un retorno absurdamente alto). Para esas "
            "dos metricas, mira el desglose por simbolo: ahi si representan "
            "una posicion real de ESE simbolo."
        )


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

    try:
        result = run_backtest(
            connector=connector,
            symbols=args.symbols,
            start=args.start,
            end=args.end,
            strategy_config=settings.to_trend_strategy_config(),
        )
    except ConnectorError as exc:
        print(f"ERROR consultando datos de Alpaca: {exc}", file=sys.stderr)
        return 1

    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
