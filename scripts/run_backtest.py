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
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtesting.backtest_runner import BacktestResult, run_backtest  # noqa: E402
from connectors.alpaca_connector import DEFAULT_SYMBOLS, AlpacaConnector  # noqa: E402
from connectors.base_connector import ConnectorAuthenticationError, ConnectorError  # noqa: E402


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Fecha invalida '{value}': usa el formato AAAA-MM-DD"
        ) from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest de la estrategia de tendencia (Fase 0) sobre datos historicos de Alpaca."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(DEFAULT_SYMBOLS),
        help=f"Simbolos a testear (por defecto: {', '.join(DEFAULT_SYMBOLS)})",
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
        help="Muestra logs INFO del proceso (por defecto solo WARNING/ERROR)",
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

    try:
        result = run_backtest(
            connector=connector, symbols=args.symbols, start=args.start, end=args.end
        )
    except ConnectorError as exc:
        print(f"ERROR consultando datos de Alpaca: {exc}", file=sys.stderr)
        return 1

    print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
