"""Script de comprobación manual del indicador ATR (strategy/indicators.py)
con datos reales de mercado, en el mismo espíritu que `probar_conexion.py`.

QUÉ HACE: pide velas recientes reales a Alpaca (paper trading, feed IEX) para
cada símbolo de config/config.yaml, calcula el ATR con los mismos parámetros
que usaría un ciclo real (config.strategy.atr_period), y muestra qué stop-loss
saldría con CADA método (fixed_percent y atr) para que se puedan comparar
uno al lado del otro con precios de verdad.

QUÉ NO HACE (a propósito, cero riesgo): no envía ninguna orden, no toca el
kill switch, no escribe en la base de datos ni en persistence/ -- solo hace
peticiones de LECTURA (velas históricas) a Alpaca y las imprime en pantalla.
Se puede correr tantas veces como se quiera, cualquier día (con mercado
abierto o cerrado), sin ningún efecto en la cuenta de paper trading.

Uso:
    python probar_atr.py                  # todos los simbolos de config.yaml
    python probar_atr.py --symbols SPY QQQ # solo estos simbolos
"""

from __future__ import annotations

import argparse
import sys

from config.settings import load_settings
from connectors.alpaca_connector import AlpacaConnector
from connectors.base_connector import ConnectorError
from risk.stop_loss import atr_stop_loss, fixed_percent_stop_loss
from strategy.indicators import add_indicators, bars_to_dataframe


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Simbolos a comprobar (por defecto, los de config/config.yaml)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - error de config, mensaje claro y salir
        print(f"config/config.yaml invalido: {exc}", file=sys.stderr)
        return 1

    symbols = args.symbols or list(settings.symbols)

    try:
        connector = AlpacaConnector(paper=True)
    except ConnectorError as exc:
        print(f"No se pudo conectar con Alpaca: {exc}", file=sys.stderr)
        return 1

    print(
        f"Comprobando ATR (periodo={settings.strategy.atr_period}) sobre "
        f"{len(symbols)} simbolo(s), timeframe {settings.timeframe.value}...\n"
    )

    for symbol in symbols:
        try:
            bars = connector.get_recent_bars(
                symbol, timeframe=settings.timeframe, limit=settings.bars_limit
            )
        except ConnectorError as exc:
            print(f"{symbol}: error pidiendo velas ({exc})")
            continue

        if not bars:
            print(f"{symbol}: sin velas disponibles")
            continue

        df = bars_to_dataframe(bars)
        enriched = add_indicators(
            df,
            ema_fast=settings.strategy.ema_fast,
            ema_slow=settings.strategy.ema_slow,
            rsi_period=settings.strategy.rsi_period,
            macd_fast=settings.strategy.macd_fast,
            macd_slow=settings.strategy.macd_slow,
            macd_signal=settings.strategy.macd_signal,
            atr_period=settings.strategy.atr_period,
        )
        last = enriched.iloc[-1]
        close = float(last["close"])
        atr_value = float(last["atr"])

        print(f"{symbol}: ultimo cierre={close:.2f}")
        stop_fixed = fixed_percent_stop_loss(close, settings.stop_loss.fixed_percent)
        print(
            f"  stop-loss fixed_percent ({settings.stop_loss.fixed_percent:.0%}): "
            f"{stop_fixed:.2f}  (distancia {close - stop_fixed:.2f})"
        )
        if atr_value != atr_value:  # NaN -- todavia en periodo de calentamiento
            print(
                f"  ATR: todavia no disponible (calentamiento, hacen falta al "
                f"menos {settings.strategy.atr_period} velas con historia)"
            )
        else:
            stop_atr = atr_stop_loss(close, atr_value, settings.stop_loss.atr_multiplier)
            print(
                f"  ATR: {atr_value:.4f}  ->  stop-loss atr "
                f"(multiplicador {settings.stop_loss.atr_multiplier}): {stop_atr:.2f}  "
                f"(distancia {close - stop_atr:.2f})"
            )
        print()

    print(
        f"Metodo activo ahora mismo en config.yaml: '{settings.stop_loss.method.value}'. "
        "Nada de esto ha enviado ninguna orden ni ha tocado el kill switch -- "
        "solo lectura."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
