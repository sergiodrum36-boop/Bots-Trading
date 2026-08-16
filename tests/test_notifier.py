"""Tests unitarios de notifications/notifier.py.

Ninguno de estos tests llama a la API real de Telegram: `urllib.request.urlopen`
se sustituye por un doble de pruebas en cada test que lo necesita.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import notifications.notifier as notifier_module
from notifications.notifier import (
    NullNotifier,
    TelegramAPIError,
    TelegramNotifier,
    get_notifier,
)


# ----------------------------------------------------------------------
# NullNotifier
# ----------------------------------------------------------------------
class TestNullNotifier:
    def test_notify_always_returns_true_and_never_raises(self):
        assert NullNotifier().notify("cualquier cosa") is True


# ----------------------------------------------------------------------
# TelegramNotifier -- validacion / construccion
# ----------------------------------------------------------------------
class TestTelegramNotifierConstruction:
    def test_rejects_empty_bot_token(self):
        with pytest.raises(ValueError):
            TelegramNotifier(bot_token="", chat_id="123")

    def test_rejects_empty_chat_id(self):
        with pytest.raises(ValueError):
            TelegramNotifier(bot_token="abc:def", chat_id="")


# ----------------------------------------------------------------------
# TelegramNotifier -- envio (urlopen mockeado)
# ----------------------------------------------------------------------
class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestTelegramNotifierSend:
    def test_sends_successfully_and_returns_true(self, monkeypatch):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append({"url": request.full_url, "timeout": timeout, "data": json.loads(request.data)})
            return _FakeHTTPResponse(json.dumps({"ok": True}).encode("utf-8"))

        monkeypatch.setattr(notifier_module.urllib.request, "urlopen", fake_urlopen)
        notifier = TelegramNotifier(bot_token="test-token", chat_id="42")

        result = notifier.notify("hola")

        assert result is True
        assert len(calls) == 1
        assert calls[0]["url"] == "https://api.telegram.org/bottest-token/sendMessage"
        assert calls[0]["data"] == {"chat_id": "42", "text": "hola"}

    def test_retries_transient_error_then_succeeds(self, monkeypatch):
        attempts = {"n": 0}

        def fake_urlopen(request, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise urllib.error.URLError("temporary failure in name resolution")
            return _FakeHTTPResponse(json.dumps({"ok": True}).encode("utf-8"))

        monkeypatch.setattr(notifier_module.urllib.request, "urlopen", fake_urlopen)
        notifier = TelegramNotifier(bot_token="test-token", chat_id="42", max_attempts=3)

        result = notifier.notify("hola")

        assert result is True
        assert attempts["n"] == 2

    def test_returns_false_without_raising_after_exhausting_retries(self, monkeypatch):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("caido de verdad")

        monkeypatch.setattr(notifier_module.urllib.request, "urlopen", fake_urlopen)
        notifier = TelegramNotifier(bot_token="test-token", chat_id="42", max_attempts=2)

        result = notifier.notify("hola")  # no debe lanzar

        assert result is False

    def test_returns_false_when_telegram_reports_ok_false(self, monkeypatch):
        def fake_urlopen(request, timeout=None):
            return _FakeHTTPResponse(
                json.dumps({"ok": False, "description": "chat not found"}).encode("utf-8")
            )

        monkeypatch.setattr(notifier_module.urllib.request, "urlopen", fake_urlopen)
        notifier = TelegramNotifier(bot_token="test-token", chat_id="42", max_attempts=1)

        result = notifier.notify("hola")

        assert result is False

    def test_does_not_retry_non_transient_http_error(self, monkeypatch):
        attempts = {"n": 0}

        def fake_urlopen(request, timeout=None):
            attempts["n"] += 1
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(notifier_module.urllib.request, "urlopen", fake_urlopen)
        notifier = TelegramNotifier(bot_token="bad-token", chat_id="42", max_attempts=3)

        result = notifier.notify("hola")

        assert result is False
        assert attempts["n"] == 1  # 401 no es transitorio, no se reintenta


# ----------------------------------------------------------------------
# get_notifier
# ----------------------------------------------------------------------
class TestGetNotifier:
    def test_returns_null_notifier_when_env_not_set(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setattr(notifier_module, "load_dotenv", lambda *a, **kw: None)

        notifier = get_notifier()

        assert isinstance(notifier, NullNotifier)

    def test_returns_null_notifier_when_only_token_set(self, monkeypatch):
        monkeypatch.setattr(notifier_module, "load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        notifier = get_notifier()

        assert isinstance(notifier, NullNotifier)

    def test_returns_telegram_notifier_when_both_set(self, monkeypatch):
        monkeypatch.setattr(notifier_module, "load_dotenv", lambda *a, **kw: None)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

        notifier = get_notifier()

        assert isinstance(notifier, TelegramNotifier)


def test_telegram_api_error_is_an_exception():
    assert issubclass(TelegramAPIError, Exception)
