"""Carga y valida `config/config.yaml`: simbolos, timeframe y parametros
de riesgo/estrategia (03_Estructura_Proyecto_Python.md;
02_Especificaciones_Tecnicas.md, secciones 5.3 y 13).

Principio de diseno -- NUNCA secretos aqui: las credenciales de Alpaca
siguen viviendo solo en variables de entorno via `.env` (ver
`connectors/alpaca_connector.py`, que las lee directamente con
python-dotenv, sin pasar por este modulo). `config/config.yaml` no
contiene secretos y esta pensado para versionarse en git, tal como pide
la seccion 7 de las especificaciones.

Comportamiento ante errores (mismo espiritu que `risk/kill_switch.py`:
"no existe" es un caso seguro, "existe pero esta mal" no lo es):
  - Si `config/config.yaml` NO EXISTE, es un caso seguro: se usan los
    mismos valores por defecto que ya tenia cada modulo antes de que
    existiera este archivo (ver las constantes `DEFAULT_*` de
    `strategy/`, `risk/`). Esto permite que el proyecto siga funcionando
    igual que siempre si alguien borra el archivo sin querer.
  - Si `config/config.yaml` EXISTE pero su contenido no es valido (YAML
    mal formado, o un valor fuera de rango -- ej. `risk_pct` negativo,
    `ema_fast >= ema_slow`), se lanza `ConfigValidationError` de forma
    inmediata (en `load_settings`, sin esperar a que el valor invalido se
    use mas adelante) -- quien llama debe fallar el arranque, nunca
    seguir en silencio con parametros de riesgo a medias o incorrectos.

Los valores cargados alimentan directamente los dataclasses que ya usaba
el resto del proyecto (`TrendStrategyConfig`, `StopLossConfig`,
`KillSwitchConfig`) via los metodos `to_*()` de `BotSettings` -- el resto
del codigo no necesita saber que existe pydantic-settings, solo recibe
los mismos dataclasses de siempre.

Variable de entorno opcional `TRADING_BOT_CONFIG_PATH` (via el entorno o
`.env`): apunta a un `config.yaml` distinto del que viene por defecto en
esta misma carpeta -- pensado sobre todo para tests o para una segunda
configuracion alternativa; el uso normal no necesita definirla.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from connectors.alpaca_connector import DEFAULT_SYMBOLS
from connectors.base_connector import Timeframe
from risk.kill_switch import (
    DEFAULT_MAX_CONNECTION_LOSS_MINUTES,
    DEFAULT_MAX_CONSECUTIVE_LOSSES,
    DEFAULT_MAX_DAILY_LOSS_PCT,
    DEFAULT_MAX_TRADES_PER_DAY,
    KillSwitchConfig,
)
from risk.position_sizing import DEFAULT_MAX_POSITION_PCT, DEFAULT_RISK_PCT
from risk.stop_loss import (
    DEFAULT_ATR_MULTIPLIER,
    DEFAULT_FIXED_PERCENT,
    StopLossConfig,
    StopLossMethod,
)
from strategy.indicators import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_EMA_FAST,
    DEFAULT_EMA_SLOW,
    DEFAULT_MACD_FAST,
    DEFAULT_MACD_SIGNAL,
    DEFAULT_MACD_SLOW,
    DEFAULT_RSI_PERIOD,
)
from strategy.trend_strategy import TrendStrategyConfig

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
DEFAULT_BARS_LIMIT = 100  # igual que core/bot_engine.py -- ver su docstring


class ConfigValidationError(Exception):
    """`config/config.yaml` existe pero su contenido no es valido. Nunca
    se debe seguir arrancando con parametros de riesgo a medias o
    incorrectos -- ver el docstring del modulo."""


class StrategySettings(BaseModel):
    """Espejo de `strategy.trend_strategy.TrendStrategyConfig` como
    entrada de config.yaml -- la validacion de fondo (ej. ema_fast debe
    ser menor que ema_slow) la hace ese mismo dataclass en
    `to_trend_strategy_config()`, no se duplica aqui."""

    ema_fast: int = Field(default=DEFAULT_EMA_FAST, gt=0)
    ema_slow: int = Field(default=DEFAULT_EMA_SLOW, gt=0)
    rsi_period: int = Field(default=DEFAULT_RSI_PERIOD, gt=0)
    rsi_buy_min: float = Field(default=40.0, ge=0.0, le=100.0)
    rsi_buy_max: float = Field(default=70.0, ge=0.0, le=100.0)
    rsi_sell_threshold: float = Field(default=80.0, gt=0.0, le=100.0)
    macd_fast: int = Field(default=DEFAULT_MACD_FAST, gt=0)
    macd_slow: int = Field(default=DEFAULT_MACD_SLOW, gt=0)
    macd_signal: int = Field(default=DEFAULT_MACD_SIGNAL, gt=0)
    atr_period: int = Field(default=DEFAULT_ATR_PERIOD, gt=0)

    def to_trend_strategy_config(self) -> TrendStrategyConfig:
        return TrendStrategyConfig(
            ema_fast=self.ema_fast,
            ema_slow=self.ema_slow,
            rsi_period=self.rsi_period,
            rsi_buy_min=self.rsi_buy_min,
            rsi_buy_max=self.rsi_buy_max,
            rsi_sell_threshold=self.rsi_sell_threshold,
            macd_fast=self.macd_fast,
            macd_slow=self.macd_slow,
            macd_signal=self.macd_signal,
            atr_period=self.atr_period,
        )


class RiskSettings(BaseModel):
    """Espejo de los parametros de `risk/position_sizing.py`. Sin
    dataclass propio en ese modulo (son argumentos sueltos de
    `calculate_position_size`), asi que los limites de la seccion 5.1/5.3
    de las especificaciones se validan aqui mismo."""

    risk_pct: float = Field(default=DEFAULT_RISK_PCT, gt=0.0, le=1.0)
    max_position_pct: float = Field(default=DEFAULT_MAX_POSITION_PCT, gt=0.0, le=1.0)


class StopLossSettings(BaseModel):
    """Espejo de `risk.stop_loss.StopLossConfig`. Ese dataclass no valida
    nada en su `__init__` (la validacion vive en las funciones
    `fixed_percent_stop_loss`/`atr_stop_loss`, que se llaman mas tarde en
    cada ciclo) -- para poder fallar aqui, en el arranque, se replican
    esos mismos limites como restricciones de pydantic."""

    method: StopLossMethod = StopLossMethod.FIXED_PERCENT
    fixed_percent: float = Field(default=DEFAULT_FIXED_PERCENT, gt=0.0, lt=1.0)
    atr_multiplier: float = Field(default=DEFAULT_ATR_MULTIPLIER, gt=0.0)

    def to_stop_loss_config(self) -> StopLossConfig:
        return StopLossConfig(
            method=self.method,
            fixed_percent=self.fixed_percent,
            atr_multiplier=self.atr_multiplier,
        )


class KillSwitchSettings(BaseModel):
    """Espejo de `risk.kill_switch.KillSwitchConfig` -- la validacion de
    fondo la hace ese mismo dataclass en `to_kill_switch_config()`."""

    max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT
    max_consecutive_losses: int = DEFAULT_MAX_CONSECUTIVE_LOSSES
    max_connection_loss_minutes: float = DEFAULT_MAX_CONNECTION_LOSS_MINUTES
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY

    def to_kill_switch_config(self) -> KillSwitchConfig:
        return KillSwitchConfig(
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_consecutive_losses=self.max_consecutive_losses,
            max_connection_loss_minutes=self.max_connection_loss_minutes,
            max_trades_per_day=self.max_trades_per_day,
        )


class BotSettings(BaseModel):
    """Configuracion completa del bot, validada a partir de
    `config/config.yaml` (o de los valores por defecto de siempre si el
    archivo no existe -- ver `load_settings`)."""

    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    timeframe: Timeframe = Timeframe.DAY
    bars_limit: int = Field(default=DEFAULT_BARS_LIMIT, gt=0)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    stop_loss: StopLossSettings = Field(default_factory=StopLossSettings)
    kill_switch: KillSwitchSettings = Field(default_factory=KillSwitchSettings)

    @field_validator("symbols", mode="before")
    @classmethod
    def _symbols_not_empty(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)) and not value:
            raise ValueError("symbols no puede estar vacio")
        return value

    def to_trend_strategy_config(self) -> TrendStrategyConfig:
        return self.strategy.to_trend_strategy_config()

    def to_stop_loss_config(self) -> StopLossConfig:
        return self.stop_loss.to_stop_loss_config()

    def to_kill_switch_config(self) -> KillSwitchConfig:
        return self.kill_switch.to_kill_switch_config()


class _ConfigPathSettings(BaseSettings):
    """Resuelve DONDE esta el `config.yaml` a cargar, via
    `TRADING_BOT_CONFIG_PATH` en el entorno o en `.env` (ver
    `.env.example`) -- es la unica parte de este modulo que usa
    pydantic-settings para leer variables de entorno; el resto de la
    validacion (`BotSettings`) es un `BaseModel` normal sobre el
    contenido ya leido del YAML."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    trading_bot_config_path: Path | None = None


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Decide que archivo de configuracion usar, en orden de prioridad:
    `path` explicito > `TRADING_BOT_CONFIG_PATH` (entorno/.env) >
    `config/config.yaml` junto a este modulo."""
    if path is not None:
        return Path(path)
    env_path = _ConfigPathSettings().trading_bot_config_path
    return env_path if env_path is not None else DEFAULT_CONFIG_PATH


def load_settings(path: str | Path | None = None) -> BotSettings:
    """Carga y valida la configuracion del bot desde `config/config.yaml`
    (ver `resolve_config_path` para la prioridad de rutas).

    Si el archivo no existe, devuelve `BotSettings()` con los valores por
    defecto de siempre (caso seguro). Si existe pero no es un YAML valido
    o tiene algun parametro fuera de rango, lanza `ConfigValidationError`
    de inmediato -- nunca deja pasar una configuracion a medio validar.
    """
    config_path = resolve_config_path(path)

    if not config_path.exists():
        return BotSettings()

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"{config_path} no es un YAML valido: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigValidationError(
            f"{config_path} debe contener un mapa de parametros (YAML tipo "
            f"diccionario) en la raiz, no {type(raw).__name__}."
        )

    try:
        settings = BotSettings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigValidationError(f"{config_path} tiene parametros invalidos:\n{exc}") from exc

    # Validacion final: construir los dataclasses de destino dispara la
    # validacion propia de cada uno (ej. TrendStrategyConfig exige
    # ema_fast < ema_slow, KillSwitchConfig exige max_trades_per_day >= 1)
    # -- mismo principio de "fallar en el arranque, no a medio ciclo".
    try:
        settings.to_trend_strategy_config()
        settings.to_kill_switch_config()
    except ValueError as exc:
        raise ConfigValidationError(f"{config_path} tiene parametros invalidos: {exc}") from exc

    return settings
