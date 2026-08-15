"""Backend de persistencia del kill switch sobre la misma base de datos de
auditoría (SQLAlchemy) -- pensado para entornos efímeros como GitHub
Actions, donde el archivo JSON local
(`risk.kill_switch.FileKillSwitchStateStore`) no sobrevive entre
ejecuciones.

Ver el docstring de `risk/kill_switch.py` para el principio no negociable
de que el estado debe sobrevivir a un reinicio del proceso: aquí "disco"
se traduce en "una fila persistida en la base de datos compartida", no
cambia el requisito, solo el medio. Reutiliza la misma serialización JSON
que ya usa el backend de archivo (`KillSwitchState.to_dict/from_dict`),
para no duplicar la definición de los campos en dos sitios.

Este módulo vive en `persistence/` (no en `risk/`) porque depende de
SQLAlchemy y de `persistence/models.py` -- `risk/kill_switch.py` no
depende de `persistence/` en ningún sentido, solo define el `Protocol`
(`KillSwitchStateStore`) que esta clase implementa.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from persistence.models import KillSwitchStateRecord
from risk.kill_switch import KillSwitchState, KillSwitchStateCorruptError

_ROW_ID = 1  # fila unica: el estado del kill switch no tiene historial, solo el actual


class SqlKillSwitchStateStore:
    """Guarda el estado del kill switch como una fila única (id=1) en la
    tabla `kill_switch_state`, usando la misma base de datos/engine que la
    auditoría de operaciones (ver `persistence/database.py`)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load(self) -> KillSwitchState | None:
        with self._session_factory() as session:
            record = session.get(KillSwitchStateRecord, _ROW_ID)
            if record is None:
                return None
            try:
                data = json.loads(record.state_json)
                return KillSwitchState.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise KillSwitchStateCorruptError(
                    f"estado de kill switch en la fila id={_ROW_ID} de "
                    f"kill_switch_state corrupto o ilegible: {exc}"
                ) from exc

    def save(self, state: KillSwitchState) -> None:
        payload = json.dumps(state.to_dict())
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            record = session.get(KillSwitchStateRecord, _ROW_ID)
            if record is None:
                session.add(
                    KillSwitchStateRecord(id=_ROW_ID, state_json=payload, updated_at=now)
                )
            else:
                record.state_json = payload
                record.updated_at = now
            session.commit()
