"""Motor y sesión de SQLAlchemy (02_Especificaciones_Tecnicas.md, sección 9).

Bases de datos SEPARADAS por entorno -- nunca se mezclan dry-run (paper) y
real, para poder comparar resultados de forma limpia (patrón de Freqtrade):
  - `trading_bot.dryrun.sqlite` -- Fase 1 (paper trading), la única
    habilitada por ahora.
  - `trading_bot.live.sqlite` -- Fase 2 (cuenta real). El nombre ya está
    reservado aquí, pero nada en este proyecto escribe en ella todavía --
    ver el checklist de salida de Fase 1 (sección 12 de las
    especificaciones) antes de usarla.

"Migraciones simples": esta primera versión usa
`Base.metadata.create_all()` (crea las tablas si no existen, no toca las
que ya existen) -- suficiente mientras el esquema no cambie de forma
incompatible. Si en el futuro hace falta alterar una tabla existente, este
es el sitio para introducir Alembic; no hace falta antes.

Backend remoto (Postgres/Supabase) para entornos efímeros: además del
SQLite local de siempre, `make_engine` acepta directamente una URL de
conexión de SQLAlchemy (ej. `postgresql://...`) -- pensado para cuando el
ciclo corre en GitHub Actions, que no conserva ningún archivo entre
ejecuciones (ver `resolve_db_path`, que decide cuál usar por defecto según
la variable de entorno `TRADING_BOT_DATABASE_URL`). El esquema es
idéntico en ambos casos (mismos modelos de `persistence/models.py`); lo
único que cambia es dónde vive la base de datos.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from persistence.models import Base

DRYRUN_DB_FILENAME = "trading_bot.dryrun.sqlite"
LIVE_DB_FILENAME = "trading_bot.live.sqlite"
DATABASE_URL_ENV_VAR = "TRADING_BOT_DATABASE_URL"


def default_db_path(paper: bool = True) -> str:
    """Nombre de archivo de base de datos según el entorno. `paper=True`
    (Fase 1) es el único caso soportado activamente por ahora."""
    return DRYRUN_DB_FILENAME if paper else LIVE_DB_FILENAME


def is_db_url(db_path: str | Path) -> bool:
    """True si `db_path` ya es una URL de conexión de SQLAlchemy (ej.
    `postgresql://...`) en vez de una ruta de archivo SQLite local."""
    return "://" in str(db_path)


def resolve_db_path(paper: bool = True) -> str:
    """Ruta/URL de base de datos por defecto: si `TRADING_BOT_DATABASE_URL`
    está definida en el entorno (típico al correr en GitHub Actions, ver
    .github/workflows/ciclo_diario.yml), tiene prioridad sobre el archivo
    SQLite local -- usado como valor por defecto de `--db-path` en
    scripts/run_paper_trading.py y del campo correspondiente en la barra
    lateral de dashboard/app.py."""
    return os.environ.get(DATABASE_URL_ENV_VAR) or default_db_path(paper=paper)


def make_engine(db_path: str | Path = DRYRUN_DB_FILENAME) -> Engine:
    """Crea el engine de SQLAlchemy y asegura que las tablas existan.
    Acepta tanto una ruta de archivo SQLite como una URL completa de
    conexión (ver `is_db_url`)."""
    url = str(db_path) if is_db_url(db_path) else f"sqlite:///{db_path}"
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Contexto transaccional: hace commit si todo va bien, rollback si se
    lanza una excepción dentro del bloque, y siempre cierra la sesión."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
