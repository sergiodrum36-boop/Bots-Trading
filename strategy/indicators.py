"""Cálculo de indicadores técnicos sobre velas OHLCV.

Ver 02_Especificaciones_Tecnicas.md, sección 4, y 04_Guia_Referencias_
Fuentes.md, sección 5 (pandas-ta). Esta primera versión implementa EMA/RSI/
MACD a mano con pandas puro (son pocas líneas y evitamos una dependencia
extra); si en el futuro hacen falta más indicadores, pandas-ta es la
referencia sugerida por las especificaciones.
"""

from __future__ import annotations

import pandas as pd

from connectors.base_connector import OHLCVBar

DEFAULT_EMA_FAST = 20
DEFAULT_EMA_SLOW = 50
DEFAULT_RSI_PERIOD = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9

_BAR_COLUMNS = ["symbol", "open", "high", "low", "close", "volume", "trade_count", "vwap"]


def bars_to_dataframe(bars: list[OHLCVBar]) -> pd.DataFrame:
    """Convierte una lista de `OHLCVBar` (formato interno del conector) en un
    DataFrame de pandas ordenado cronológicamente e indexado por timestamp.

    Nota: si `bars` mezcla varios símbolos, el DataFrame resultante también
    los mezcla (columna 'symbol'); quien llame debe filtrar/agrupar por
    símbolo antes de pasarlo a `add_indicators` (que espera un único
    símbolo). Este es un punto de extensión natural para el futuro
    `data/data_normalizer.py` de la estructura del proyecto.
    """
    if not bars:
        return pd.DataFrame(columns=_BAR_COLUMNS).set_index(
            pd.DatetimeIndex([], name="timestamp")
        )

    df = pd.DataFrame(
        {
            "symbol": [b.symbol for b in bars],
            "timestamp": [b.timestamp for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
            "trade_count": [b.trade_count for b in bars],
            "vwap": [b.vwap for b in bars],
        }
    )
    return df.sort_values(["symbol", "timestamp"]).set_index("timestamp")


def ema(series: pd.Series, period: int) -> pd.Series:
    """Media móvil exponencial (EMA)."""
    if period <= 0:
        raise ValueError("El periodo de la EMA debe ser mayor que 0")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = DEFAULT_RSI_PERIOD) -> pd.Series:
    """RSI (Relative Strength Index) con suavizado de Wilder (EWM con
    alpha = 1/period, la forma estándar de calcularlo)."""
    if period <= 0:
        raise ValueError("El periodo del RSI debe ser mayor que 0")

    delta = series.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    rsi_values = 100.0 - (100.0 / (1.0 + rs))
    # Cuando no ha habido ninguna pérdida en la ventana (avg_loss == 0), el
    # RSI es 100 por definición (evita division por cero -> NaN engañoso).
    rsi_values = rsi_values.where(avg_loss != 0.0, 100.0)
    return rsi_values.astype(float)


def macd(
    series: pd.Series,
    fast: int = DEFAULT_MACD_FAST,
    slow: int = DEFAULT_MACD_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> pd.DataFrame:
    """MACD estándar. Devuelve un DataFrame con columnas 'macd',
    'macd_signal' y 'macd_histogram'."""
    if fast >= slow:
        raise ValueError("El periodo 'fast' del MACD debe ser menor que 'slow'")

    ema_fast_series = ema(series, fast)
    ema_slow_series = ema(series, slow)
    macd_line = ema_fast_series - ema_slow_series
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line

    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_histogram": histogram}
    )


def add_indicators(
    df: pd.DataFrame,
    ema_fast: int = DEFAULT_EMA_FAST,
    ema_slow: int = DEFAULT_EMA_SLOW,
    rsi_period: int = DEFAULT_RSI_PERIOD,
    macd_fast: int = DEFAULT_MACD_FAST,
    macd_slow: int = DEFAULT_MACD_SLOW,
    macd_signal: int = DEFAULT_MACD_SIGNAL,
) -> pd.DataFrame:
    """Añade columnas de indicadores (ema_fast, ema_slow, rsi, macd,
    macd_signal, macd_histogram) a un DataFrame OHLCV de UN SOLO símbolo,
    ordenado cronológicamente. No mezclar símbolos: llamar una vez por
    símbolo (ver `bars_to_dataframe` para separar por símbolo primero).
    """
    if "close" not in df.columns:
        raise ValueError("El DataFrame debe tener una columna 'close'")
    if "symbol" in df.columns and df["symbol"].nunique() > 1:
        raise ValueError(
            "add_indicators espera un DataFrame de un solo simbolo; agrupa "
            "por simbolo antes de llamar (ver bars_to_dataframe)."
        )

    out = df.copy()
    out["ema_fast"] = ema(out["close"], ema_fast)
    out["ema_slow"] = ema(out["close"], ema_slow)
    out["rsi"] = rsi(out["close"], rsi_period)
    out = out.join(macd(out["close"], macd_fast, macd_slow, macd_signal))
    return out
