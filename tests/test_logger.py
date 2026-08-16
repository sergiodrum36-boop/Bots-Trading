"""Tests unitarios de utils/logger.py.

Cada test guarda y restaura los handlers del logger raiz (fixture
`_clean_root_logger`) para no contaminar el resto de la suite ni la
propia captura de logs de pytest -- `setup_logging()` modifica el logger
raiz global a proposito (ver docstring del modulo).
"""

from __future__ import annotations

import json
import logging

import pytest

from utils.logger import (
    DEFAULT_ERROR_LOG_FILENAME,
    DEFAULT_LOG_FILENAME,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clean_root_logger():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


class TestSetupLogging:
    def test_creates_log_dir_and_both_files(self, tmp_path):
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False)

        logger = logging.getLogger("trading_bot.test")
        logger.info("mensaje de prueba")

        assert (log_dir / DEFAULT_LOG_FILENAME).exists()
        assert (log_dir / DEFAULT_ERROR_LOG_FILENAME).exists()

    def test_info_message_goes_to_main_log_not_error_log(self, tmp_path):
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False)

        logger = logging.getLogger("trading_bot.test")
        logger.info("operativa normal")

        main_content = (log_dir / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8")
        error_content = (log_dir / DEFAULT_ERROR_LOG_FILENAME).read_text(encoding="utf-8")
        assert "operativa normal" in main_content
        assert "operativa normal" not in error_content

    def test_error_message_goes_to_both_logs(self, tmp_path):
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False)

        logger = logging.getLogger("trading_bot.test")
        logger.error("algo fallo")

        main_content = (log_dir / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8")
        error_content = (log_dir / DEFAULT_ERROR_LOG_FILENAME).read_text(encoding="utf-8")
        assert "algo fallo" in main_content
        assert "algo fallo" in error_content

    def test_main_log_captures_info_regardless_of_verbose(self, tmp_path):
        """El archivo de auditoria (INFO+) no depende de --verbose -- solo
        la consola. Ver docstring de setup_logging."""
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False, verbose=False)

        logger = logging.getLogger("trading_bot.test")
        logger.info("deberia quedar registrado igualmente")

        main_content = (log_dir / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8")
        assert "deberia quedar registrado igualmente" in main_content

    def test_exception_traceback_is_captured_in_error_log(self, tmp_path):
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False)

        logger = logging.getLogger("trading_bot.test")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("fallo no controlado")

        error_content = (log_dir / DEFAULT_ERROR_LOG_FILENAME).read_text(encoding="utf-8")
        assert "fallo no controlado" in error_content
        assert "ValueError: boom" in error_content
        assert "Traceback" in error_content

    def test_json_logs_are_valid_json_lines_with_exception_field(self, tmp_path):
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False, json_logs=True)

        logger = logging.getLogger("trading_bot.test")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("fallo no controlado")

        lines = [
            line
            for line in (log_dir / DEFAULT_ERROR_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert lines, "se esperaba al menos una linea en el log de errores"
        record = json.loads(lines[-1])
        assert record["level"] == "ERROR"
        assert record["message"] == "fallo no controlado"
        assert "ValueError: boom" in record["exception"]

    def test_calling_twice_does_not_duplicate_handlers(self, tmp_path):
        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir, console=False)
        setup_logging(log_dir=log_dir, console=False)

        logger = logging.getLogger("trading_bot.test")
        logger.info("una sola vez")

        main_content = (log_dir / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8")
        assert main_content.count("una sola vez") == 1

    def test_returns_resolved_log_dir_path(self, tmp_path):
        log_dir = tmp_path / "logs"
        returned = setup_logging(log_dir=log_dir, console=False)
        assert returned == log_dir
        assert returned.is_dir()
