"""Notificaciones de eventos criticos (03_Estructura_Proyecto_Python.md;
02_Especificaciones_Tecnicas.md, seccion 10): activacion del kill switch,
errores no controlados, perdida de conexion, resumen diario opcional.

Canal: Telegram (decision del usuario, 2026-08-16). La seccion 10 de las
especificaciones lo describe como "recomendado, no bloqueante" -- ese
espiritu se refleja en dos decisiones de diseno:

  - `get_notifier()` devuelve `NullNotifier` (no hace nada, nunca falla)
    si `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` no estan configurados --
    el bot sigue funcionando exactamente igual sin notificaciones, tal
    como hasta que existio este modulo. No es obligatorio configurarlo.

  - `TelegramNotifier.notify()` NUNCA lanza una excepcion: un fallo al
    enviar (red caida, token invalido, Telegram caido) se registra en el
    log y devuelve `False`, pero no debe poder tumbar un ciclo de trading
    -- notificar que algo fallo es secundario a que el bot siga
    protegiendo el capital del usuario. Quien llama puede ignorar el
    valor de retorno con seguridad; se devuelve por si quiere hacer algo
    con el (ej. loguear un aviso adicional).

Como crear el bot de Telegram (una sola vez): hablar con @BotFather en
Telegram, `/newbot`, seguir las instrucciones -- da un token con forma
"123456:ABC-DEF...". Para el chat_id, enviar cualquier mensaje al bot
recien creado y luego visitar
`https://api.telegram.org/bot<TOKEN>/getUpdates` en el navegador -- el
chat_id aparece en la respuesta JSON (`result[0].message.chat.id`).
Ambos valores van en `.env` (ver `.env.example`), nunca en el codigo.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Protocol

from dotenv import load_dotenv

from utils.retry import build_retryer

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_WAIT_MIN_SECONDS = 1.0
DEFAULT_WAIT_MAX_SECONDS = 5.0


class Notifier(Protocol):
    """Contrato minimo que usa quien orquesta (scripts/run_paper_trading.py).
    Mismo patron que `KillSwitchStateStore` (risk/kill_switch.py): duck
    typing estructural, sin necesidad de heredar de nada."""

    def notify(self, message: str) -> bool:
        """Envia `message`. Devuelve True si se envio (o si no habia
        canal configurado -- ver NullNotifier); False si fallo el envio.
        NUNCA lanza una excepcion."""
        ...


class NullNotifier:
    """Sin canal configurado: no hace nada, no falla nunca. Mismo patron
    que `persistence.store.NullTradeStore` -- el resto del codigo no
    necesita saber si hay notificaciones reales o no."""

    def notify(self, message: str) -> bool:
        return True


class TelegramAPIError(Exception):
    """La API de Telegram respondio pero indico un fallo (`ok: false`)."""


def _is_retryable_error(exc: BaseException) -> bool:
    """Solo se reintentan fallos transitorios de red -- un token invalido
    o un chat_id inexistente (4xx de Telegram) no se arreglan reintentando.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        # Fallo de resolucion DNS / conexion rechazada / timeout de socket,
        # sin llegar a obtener un status code HTTP.
        return True
    return False


class TelegramNotifier:
    """Notificador via la API de Bots de Telegram (`sendMessage`), sin
    dependencias externas -- usa `urllib` de la biblioteca estandar en vez
    de anadir `requests`/`python-telegram-bot` solo para un POST simple.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token no puede estar vacio")
        if not chat_id:
            raise ValueError("chat_id no puede estar vacio")
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._max_attempts = max_attempts

    def notify(self, message: str) -> bool:
        retryer = build_retryer(
            description="telegram.sendMessage",
            is_retryable=_is_retryable_error,
            max_attempts=self._max_attempts,
            wait_min_seconds=DEFAULT_WAIT_MIN_SECONDS,
            wait_max_seconds=DEFAULT_WAIT_MAX_SECONDS,
        )
        try:
            retryer(self._send, message)
            return True
        except Exception as exc:  # noqa: BLE001 - nunca debe propagarse
            logger.error("Fallo al enviar notificacion por Telegram: %s", exc)
            return False

    def _send(self, message: str) -> None:
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        payload = json.dumps({"chat_id": self._chat_id, "text": message}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = response.read()
        data = json.loads(body)
        if not data.get("ok", False):
            raise TelegramAPIError(f"Telegram devolvio ok=false: {data}")


def get_notifier(env_file: str | None = None) -> Notifier:
    """Construye el notificador segun las variables de entorno
    (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, via `.env`) -- devuelve
    `NullNotifier` si no estan configuradas (caso seguro: el bot sigue
    funcionando igual sin notificaciones, seccion 10 de las
    especificaciones: "recomendado, no bloqueante")."""
    load_dotenv(dotenv_path=env_file, override=False)
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.info(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no configurados; "
            "notificaciones desactivadas (ver .env.example)."
        )
        return NullNotifier()
    return TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
