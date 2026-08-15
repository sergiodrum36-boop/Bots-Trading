"""Tests de dashboard/data.py: funciones puras de acceso a datos y formato
que usa el panel visual, sin dependencias de Streamlit."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from connectors.base_connector import (
    Account,
    BrokerConnector,
    ConnectorError,
    MarketSession,
    Position,
    Timeframe,
)
from dashboard.data import fetch_live_status, format_currency, format_optional, format_pct, summarize_kill_switch
from risk.kill_switch import KillSwitch, KillSwitchConfig

FIXED_NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


class FakeConnector(BrokerConnector):
    def __init__(self):
        self.market_open = True
        self.account = Account(equity=100_000.0, cash=50_000.0, buying_power=100_000.0)
        self.positions = [Position(symbol="SPY", qty=10.0, avg_entry_price=100.0, market_value=1010.0, unrealized_pl=10.0)]
        self.error: Exception | None = None

    def is_market_open(self):
        if self.error:
            raise self.error
        return self.market_open

    def get_account(self):
        if self.error:
            raise self.error
        return self.account

    def get_open_positions(self):
        if self.error:
            raise self.error
        return self.positions

    def get_historical_bars(self, symbols, timeframe, start, end=None, limit=None):
        raise NotImplementedError

    def get_recent_bars(self, symbol, timeframe=Timeframe.DAY, limit=100):
        raise NotImplementedError

    def get_latest_bar(self, symbol):
        raise NotImplementedError

    def get_market_calendar(self, start, end) -> list[MarketSession]:
        raise NotImplementedError

    def is_symbol_tradable(self, symbol: str) -> bool:
        raise NotImplementedError

    def submit_market_order(self, symbol, qty, side, client_order_id):
        raise NotImplementedError

    def get_order_by_client_id(self, client_order_id):
        raise NotImplementedError


class TestFetchLiveStatus:
    def test_returns_data_on_success(self):
        connector = FakeConnector()

        status = fetch_live_status(connector)

        assert status.market_open is True
        assert status.account.equity == 100_000.0
        assert len(status.positions) == 1
        assert status.error is None

    def test_returns_error_instead_of_raising_on_connector_failure(self):
        connector = FakeConnector()
        connector.error = ConnectorError("fallo de red")

        status = fetch_live_status(connector)

        assert status.market_open is None
        assert status.account is None
        assert status.positions == []
        assert status.error == "fallo de red"


class TestSummarizeKillSwitch:
    def test_reflects_inactive_state(self, tmp_path: Path):
        kill_switch = KillSwitch(
            config=KillSwitchConfig(), state_file=tmp_path / "ks.json", clock=lambda: FIXED_NOW
        )

        summary = summarize_kill_switch(kill_switch)

        assert summary.active is False
        assert summary.connection_lost is False
        assert summary.trades_today == 0

    def test_reflects_connection_lost(self, tmp_path: Path):
        kill_switch = KillSwitch(
            config=KillSwitchConfig(), state_file=tmp_path / "ks.json", clock=lambda: FIXED_NOW
        )
        kill_switch.record_connection_lost()

        summary = summarize_kill_switch(kill_switch)

        assert summary.connection_lost is True
        assert summary.active is False

    def test_reflects_triggered_state_with_reason(self, tmp_path: Path):
        kill_switch = KillSwitch(
            config=KillSwitchConfig(max_daily_loss_pct=0.01),
            state_file=tmp_path / "ks.json",
            clock=lambda: FIXED_NOW,
        )
        kill_switch.record_trade_result(-0.02)

        summary = summarize_kill_switch(kill_switch)

        assert summary.active is True
        assert summary.reason is not None
        assert summary.since is not None


class TestFormatters:
    @pytest.mark.parametrize(
        "value,expected",
        [(1234.5, "$1,234.50"), (0.0, "$0.00"), (None, "-"), (-50.0, "$-50.00")],
    )
    def test_format_currency(self, value, expected):
        assert format_currency(value) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [(0.0525, "5.25%"), (0.0, "0.00%"), (None, "-"), (-0.01, "-1.00%")],
    )
    def test_format_pct(self, value, expected):
        assert format_pct(value) == expected

    def test_format_pct_respects_decimals(self):
        assert format_pct(0.05256, decimals=1) == "5.3%"

    @pytest.mark.parametrize("value,expected", [(None, "-"), ("algo", "algo"), (3, "3")])
    def test_format_optional(self, value, expected):
        assert format_optional(value) == expected
