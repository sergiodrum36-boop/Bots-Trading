"""Tests de config/settings.py: carga y validacion de config/config.yaml.

Cubre el mismo principio que risk/kill_switch.py -- "no existe" es un
caso seguro (valores por defecto), "existe pero esta mal" no lo es
(ConfigValidationError inmediato, nunca un arranque a medias)."""

from __future__ import annotations

import pytest

from config.settings import (
    BotSettings,
    ConfigValidationError,
    load_settings,
    resolve_config_path,
)
from connectors.alpaca_connector import DEFAULT_SYMBOLS
from connectors.base_connector import Timeframe
from risk.kill_switch import DEFAULT_MAX_DAILY_LOSS_PCT
from risk.position_sizing import DEFAULT_MAX_POSITION_PCT, DEFAULT_RISK_PCT
from risk.stop_loss import DEFAULT_FIXED_PERCENT, StopLossMethod
from strategy.indicators import DEFAULT_ATR_PERIOD, DEFAULT_EMA_FAST, DEFAULT_EMA_SLOW


def test_missing_file_returns_the_same_defaults_as_before(tmp_path):
    settings = load_settings(tmp_path / "no_existe.yaml")

    assert settings == BotSettings()
    assert settings.symbols == DEFAULT_SYMBOLS
    assert settings.risk.risk_pct == DEFAULT_RISK_PCT
    assert settings.risk.max_position_pct == DEFAULT_MAX_POSITION_PCT
    assert settings.stop_loss.fixed_percent == DEFAULT_FIXED_PERCENT
    assert settings.kill_switch.max_daily_loss_pct == DEFAULT_MAX_DAILY_LOSS_PCT
    assert settings.strategy.ema_fast == DEFAULT_EMA_FAST
    assert settings.strategy.ema_slow == DEFAULT_EMA_SLOW
    assert settings.strategy.atr_period == DEFAULT_ATR_PERIOD
    assert settings.timeframe == Timeframe.DAY


def test_valid_yaml_overrides_the_defaults(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
symbols: [DIA, IWM]
timeframe: "1Hour"
bars_limit: 50
strategy:
  ema_fast: 10
  ema_slow: 30
  atr_period: 7
risk:
  risk_pct: 0.02
  max_position_pct: 0.10
stop_loss:
  method: fixed_percent
  fixed_percent: 0.03
kill_switch:
  max_daily_loss_pct: 0.03
  max_trades_per_day: 5
"""
    )

    settings = load_settings(config_file)

    assert settings.symbols == ("DIA", "IWM")
    assert settings.timeframe == Timeframe.HOUR
    assert settings.bars_limit == 50
    assert settings.strategy.ema_fast == 10
    assert settings.strategy.ema_slow == 30
    assert settings.strategy.atr_period == 7
    assert settings.risk.risk_pct == 0.02
    assert settings.risk.max_position_pct == 0.10
    assert settings.stop_loss.fixed_percent == 0.03
    assert settings.kill_switch.max_daily_loss_pct == 0.03
    assert settings.kill_switch.max_trades_per_day == 5


def test_to_dataclass_helpers_build_matching_objects(tmp_path):
    settings = load_settings(tmp_path / "no_existe.yaml")

    trend_config = settings.to_trend_strategy_config()
    assert trend_config.ema_fast == settings.strategy.ema_fast
    assert trend_config.ema_slow == settings.strategy.ema_slow
    assert trend_config.atr_period == settings.strategy.atr_period

    stop_loss_config = settings.to_stop_loss_config()
    assert stop_loss_config.fixed_percent == settings.stop_loss.fixed_percent
    assert stop_loss_config.method == StopLossMethod.FIXED_PERCENT

    kill_switch_config = settings.to_kill_switch_config()
    assert kill_switch_config.max_daily_loss_pct == settings.kill_switch.max_daily_loss_pct


def test_malformed_yaml_raises_config_validation_error(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("symbols: [SPY, QQQ\nrisk: {risk_pct: 0.01}")  # yaml roto a proposito

    with pytest.raises(ConfigValidationError):
        load_settings(config_file)


def test_yaml_that_is_not_a_mapping_raises_config_validation_error(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("- SPY\n- QQQ\n")  # una lista, no un diccionario

    with pytest.raises(ConfigValidationError):
        load_settings(config_file)


def test_out_of_range_risk_pct_raises_config_validation_error(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("risk:\n  risk_pct: 1.5\n")  # >100% del capital, invalido

    with pytest.raises(ConfigValidationError):
        load_settings(config_file)


def test_empty_symbols_raises_config_validation_error(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("symbols: []\n")

    with pytest.raises(ConfigValidationError):
        load_settings(config_file)


def test_ema_fast_not_smaller_than_ema_slow_raises_config_validation_error(tmp_path):
    """Este limite lo valida TrendStrategyConfig, no un Field de pydantic
    -- load_settings debe capturarlo igual, en el arranque (ver
    docstring del modulo)."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("strategy:\n  ema_fast: 50\n  ema_slow: 20\n")

    with pytest.raises(ConfigValidationError):
        load_settings(config_file)


def test_unsupported_stop_loss_method_raises_config_validation_error(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("stop_loss:\n  method: trailing\n")  # no soportado

    with pytest.raises(ConfigValidationError):
        load_settings(config_file)


def test_resolve_config_path_prefers_explicit_path_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_BOT_CONFIG_PATH", str(tmp_path / "otro.yaml"))
    explicit = tmp_path / "explicito.yaml"

    assert resolve_config_path(explicit) == explicit


def test_resolve_config_path_uses_env_var_when_no_explicit_path(monkeypatch, tmp_path):
    env_path = tmp_path / "desde_entorno.yaml"
    monkeypatch.setenv("TRADING_BOT_CONFIG_PATH", str(env_path))

    assert resolve_config_path() == env_path
