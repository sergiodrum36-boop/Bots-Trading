"""Configuracion centralizada de logging (03_Estructura_Proyecto_Python.md;
02_Especificaciones_Tecnicas.md, seccion 9: "log de texto rotado" con
RotatingFileHandler, nivel INFO para operativa normal y ERROR para
incidencias; y seccion 8: "todas las excepciones no controladas deben
quedar registradas con traceback completo en el log").

Principio de diseno: el resto del codigo NUNCA necesita saber que existe
este modulo -- cada archivo sigue haciendo `logging.getLogger(__name__)`
como siempre (ver risk/kill_switch.py, connectors/alpaca_connector.py,
execution/order_manager.py, etc.). `setup_logging()` es lo UNICO que
cambia: se llama UNA VEZ, al arrancar un entry point
(scripts/run_paper_trading.py, scripts/run_backtest.py, o cualquier
futuro entry point), y configura los handlers del logger raiz para que
todos los `getLogger(__name__)` de cualquier modulo hereden el mismo
comportamiento (consola + archivos rotados), sin que cada modulo tenga
que configurarse a si mismo.

Por que dos archivos de log separados (`trading_bot.log` y
`trading_bot_errors.log`) en vez de uno solo: separar "todo lo que ha
pasado" (INFO+) de "lo que hay que revisar" (ERROR+, siempre con
traceback completo cuando el registro se hizo con `logger.exception(...)`
o `exc_info=True`) hace mucho mas rapido localizar incidencias sin tener
que revisar un log enorme -- mismo espiritu que la tabla `errors_log` de
`persistence/` (auditoria complementaria, no sustituta, del log de
texto).

Por que el archivo INFO no depende de `--verbose`: `verbose` solo
controla cuanto se imprime por CONSOLA (para no saturar la pantalla en
uso normal); el archivo de auditoria en disco debe conservar el nivel
INFO siempre, tal como pide la seccion 9, para no perder historial solo
porque alguien olvido pasar `--verbose` una ejecucion concreta. El
archivo de errores, a su vez, SIEMPRE registra ERROR+ pase lo que pase
con `verbose` o `console`.

Idempotencia: `setup_logging()` puede llamarse mas de una vez sin
duplicar handlers (por ejemplo, si un test o un script la invoca dos
veces) -- limpia los handlers existentes del logger raiz antes de anadir
los suyos.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any

DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILENAME = "trading_bot.log"
DEFAULT_ERROR_LOG_FILENAME = "trading_bot_errors.log"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB por archivo antes de rotar
DEFAULT_BACKUP_COUNT = 5  # + el activo, tope aprox. 30 MB por log

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _JsonFormatter(logging.Formatter):
    """Formatea cada registro como una linea JSON (seccion 9: "opcionalmente,
    tambien en JSON para facilitar analisis posterior"). Incluye el
    traceback completo cuando el registro lleva una excepcion adjunta.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _build_formatter(json_logs: bool) -> logging.Formatter:
    return _JsonFormatter() if json_logs else logging.Formatter(_TEXT_FORMAT)


def setup_logging(
    *,
    verbose: bool = False,
    log_dir: str | Path = DEFAULT_LOG_DIR,
    json_logs: bool = False,
    console: bool = True,
) -> Path:
    """Configura el logger raiz: consola (opcional) + dos archivos
    rotados en `log_dir` (se crea si no existe):

      - `trading_bot.log`: nivel INFO siempre (independiente de
        `verbose`), es el log de auditoria de operativa normal.
      - `trading_bot_errors.log`: nivel ERROR siempre, con traceback
        completo de cualquier excepcion registrada con
        `logger.exception(...)` / `exc_info=True`.

    `verbose` solo controla el nivel de la CONSOLA (WARNING por defecto,
    INFO si `verbose=True`) -- mismo flag `--verbose` que ya exponian
    los scripts antes de que existiera este modulo.

    Devuelve la ruta resuelta de `log_dir` (util para tests y para
    mensajes informativos al usuario).

    Idempotente: llamar varias veces no duplica handlers.
    """
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    console_level = logging.INFO if verbose else logging.WARNING
    formatter = _build_formatter(json_logs)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # los HANDLERS filtran, no el logger raiz

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir_path / DEFAULT_LOG_FILENAME,
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        log_dir_path / DEFAULT_ERROR_LOG_FILENAME,
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    logging.getLogger(__name__).info(
        "Logging configurado (verbose=%s, log_dir=%s, json_logs=%s)",
        verbose,
        log_dir_path,
        json_logs,
    )
    return log_dir_path
