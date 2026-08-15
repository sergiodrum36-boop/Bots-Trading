"""Kill switch: parada de emergencia global (02_Especificaciones_Tecnicas.md,
sección 5.4) y límite de operaciones por día (sección 5.5).

PRINCIPIO NO NEGOCIABLE (coincide con las instrucciones del Project): el
kill switch persiste su estado EN DISCO. Si el proceso se reinicia después
de que el kill switch se disparase, debe arrancar en modo PAUSADO -- nunca
reanudar operativa automáticamente. Solo una llamada EXPLÍCITA a
`reset()`, pensada para ejecutarse manualmente tras revisar lo ocurrido,
puede volver a permitir operar.

Condiciones que disparan el kill switch (sección 5.4):
  1. Pérdida acumulada del día >= umbral configurado.
  2. Número de pérdidas consecutivas >= umbral configurado.
  3. Pérdida de conexión con la API durante más de N minutos -- aquí se
     aplica de forma conservadora: mientras la conexión esté caída, NO se
     abren posiciones nuevas (independientemente de si hay posiciones
     abiertas), y si la caída supera el umbral configurado al reconectar,
     se dispara el kill switch para forzar una revisión manual del estado
     real de la cuenta antes de seguir.
  4. Discrepancia entre las posiciones que el bot cree tener y las que
     reporta el bróker.

Importante sobre unidades: `record_trade_result` espera el P&L de la
operación como PORCENTAJE DEL CAPITAL TOTAL de la cuenta (no como % de
variación del precio de entrada de esa operación) -- son cosas distintas.
Una posición dimensionada al 1% de riesgo (ver risk/position_sizing.py)
que toca su stop-loss solo le cuesta ~1% al capital total, aunque el
precio en sí haya caído mucho más. Pasar aquí el % de variación del precio
en vez del % sobre capital haría el kill switch mucho menos sensible de lo
previsto -- justo el tipo de fallo silencioso que este módulo existe para
evitar.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

DEFAULT_MAX_DAILY_LOSS_PCT = 0.05  # 5% del capital, ver seccion 5.4
DEFAULT_MAX_CONSECUTIVE_LOSSES = 3
DEFAULT_MAX_CONNECTION_LOSS_MINUTES = 15.0
DEFAULT_MAX_TRADES_PER_DAY = 10
DEFAULT_STATE_FILE = "kill_switch_state.json"


@dataclass(frozen=True)
class KillSwitchConfig:
    max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT
    max_consecutive_losses: int = DEFAULT_MAX_CONSECUTIVE_LOSSES
    max_connection_loss_minutes: float = DEFAULT_MAX_CONNECTION_LOSS_MINUTES
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY

    def __post_init__(self) -> None:
        if not (0.0 < self.max_daily_loss_pct <= 1.0):
            raise ValueError("max_daily_loss_pct debe estar entre 0 (exclusivo) y 1")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses debe ser >= 1")
        if self.max_connection_loss_minutes <= 0:
            raise ValueError("max_connection_loss_minutes debe ser positivo")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day debe ser >= 1")


@dataclass
class KillSwitchState:
    """Estado persistido en disco. Todos los campos son tipos serializables
    a JSON directamente (fechas/horas como texto ISO 8601)."""

    date: str  # fecha (ISO) a la que corresponden los contadores diarios
    triggered: bool = False
    trigger_reason: str | None = None
    triggered_at: str | None = None
    daily_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    trades_today: int = 0
    connection_lost_at: str | None = None  # None = conectado

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "KillSwitchState":
        return cls(
            date=str(data["date"]),
            triggered=bool(data.get("triggered", False)),
            trigger_reason=data.get("trigger_reason"),
            triggered_at=data.get("triggered_at"),
            daily_pnl_pct=float(data.get("daily_pnl_pct", 0.0)),
            consecutive_losses=int(data.get("consecutive_losses", 0)),
            trades_today=int(data.get("trades_today", 0)),
            connection_lost_at=data.get("connection_lost_at"),
        )


class KillSwitchStateCorruptError(Exception):
    """El backend de estado tiene una fila/archivo, pero su contenido no se
    pudo interpretar como un `KillSwitchState` válido. `KillSwitch` trata
    esto como "no se puede confirmar que no está disparado" y arranca en
    modo PAUSADO -- ver `KillSwitch._load_state`. Distinto de "no existe
    ningún estado todavía" (primer arranque, `load()` devuelve `None`),
    que sí es un caso seguro de estado limpio."""


class KillSwitchStateStore(Protocol):
    """Backend de persistencia del estado del kill switch. El backend por
    defecto es `FileKillSwitchStateStore` (archivo JSON en disco, como
    exige la sección 5.4 de las especificaciones). `persistence.kill_switch_store.SqlKillSwitchStateStore`
    es la alternativa para entornos efímeros (ej. GitHub Actions) donde un
    archivo local no sobrevive entre ejecuciones -- el requisito de fondo
    ("el estado debe sobrevivir a un reinicio del proceso") es el mismo en
    ambos casos, solo cambia el medio."""

    def load(self) -> "KillSwitchState | None":
        """Devuelve el estado guardado, o `None` si todavía no hay
        ninguno (primer arranque -- caso seguro, no es un error). Debe
        lanzar `KillSwitchStateCorruptError` si hay algo guardado pero no
        se puede interpretar; cualquier otra excepción (ej. fallo de
        conexión) se propaga tal cual y hace fallar el arranque."""
        ...

    def save(self, state: "KillSwitchState") -> None:
        ...


class FileKillSwitchStateStore:
    """Backend por defecto: un archivo JSON en disco (comportamiento
    histórico de este módulo, sin cambios de fondo)."""

    def __init__(self, state_file: str | Path = DEFAULT_STATE_FILE) -> None:
        self._state_file = Path(state_file)

    def load(self) -> "KillSwitchState | None":
        if not self._state_file.exists():
            return None
        try:
            data = json.loads(self._state_file.read_text())
            return KillSwitchState.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise KillSwitchStateCorruptError(
                f"estado de kill switch en {self._state_file} corrupto o ilegible: {exc}"
            ) from exc

    def save(self, state: "KillSwitchState") -> None:
        self._state_file.write_text(json.dumps(state.to_dict(), indent=2))


class KillSwitch:
    """Parada de emergencia global, con estado persistido de forma
    duradera (ver `KillSwitchStateStore`; por defecto, un archivo en disco).

    `clock` es inyectable (por defecto `datetime.now(timezone.utc)`) para
    poder testear de forma determinista la lógica de días/duraciones sin
    depender del reloj real.
    """

    def __init__(
        self,
        config: KillSwitchConfig | None = None,
        state_file: str | Path = DEFAULT_STATE_FILE,
        store: KillSwitchStateStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or KillSwitchConfig()
        # `store`, si se pasa, tiene prioridad sobre `state_file` (que solo
        # sirve para construir el backend de archivo por defecto) -- ver
        # persistence/kill_switch_store.py para el backend de base de datos.
        self._store = store or FileKillSwitchStateStore(state_file)
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._state = self._load_state()

        if self._state.triggered:
            logger.warning(
                "Kill switch ACTIVO al arrancar (motivo: %s, desde %s). El "
                "bot arranca en modo PAUSADO; requiere reset() manual "
                "explicito para reanudar.",
                self._state.trigger_reason,
                self._state.triggered_at,
            )
        self._save()

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------
    def _load_state(self) -> KillSwitchState:
        today = self._now().date().isoformat()
        try:
            state = self._store.load()
        except KillSwitchStateCorruptError as exc:
            # Ante un estado guardado pero corrupto/ilegible, la opcion
            # segura es asumir que el kill switch esta activo -- nunca lo
            # contrario. Un estado que no se puede leer no debe
            # traducirse en "arrancar como si nada hubiera pasado".
            # Cualquier OTRO fallo (ej. no se puede conectar al backend)
            # se propaga tal cual y hace fallar el arranque -- ver
            # KillSwitchStateStore.load().
            logger.error(
                "%s; se arranca en modo PAUSADO por seguridad.", exc
            )
            state = KillSwitchState(date=today)
            state.triggered = True
            state.trigger_reason = str(exc)
            state.triggered_at = self._now().isoformat()
            return state

        return state if state is not None else KillSwitchState(date=today)

    def _save(self) -> None:
        self._store.save(self._state)

    def _roll_day_if_needed(self) -> None:
        today = self._now().date().isoformat()
        if self._state.date == today or self._state.triggered:
            # Un kill switch ya disparado NO se limpia solo porque cambio
            # el dia -- requiere reset() manual (ver docstring del modulo).
            return
        self._state.date = today
        self._state.daily_pnl_pct = 0.0
        self._state.consecutive_losses = 0
        self._state.trades_today = 0
        # connection_lost_at se preserva a proposito: una perdida de
        # conexion en curso no se "cura" porque cambie el dia.
        self._save()

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------
    def is_active(self) -> bool:
        """True si el kill switch esta disparado (bloquea toda operativa nueva)."""
        return self._state.triggered

    def can_open_new_position(self) -> bool:
        """False si: el kill switch esta activo, la conexion esta caida
        ahora mismo, o ya se alcanzo el limite de operaciones del dia."""
        self._roll_day_if_needed()
        if self._state.triggered:
            return False
        if self._state.connection_lost_at is not None:
            return False
        if self._state.trades_today >= self.config.max_trades_per_day:
            return False
        return True

    @property
    def state(self) -> KillSwitchState:
        return self._state

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------
    def _trigger(self, reason: str) -> None:
        if self._state.triggered:
            return  # ya estaba activo; no pisar el motivo original
        self._state.triggered = True
        self._state.trigger_reason = reason
        self._state.triggered_at = self._now().isoformat()
        logger.error(
            "KILL SWITCH ACTIVADO: %s. Se bloquean nuevas operaciones hasta "
            "un reset() manual explicito.",
            reason,
        )
        self._save()

    def record_trade_result(self, pnl_pct_of_capital: float) -> None:
        """Registra el resultado de una operacion cerrada.

        `pnl_pct_of_capital`: P&L como fraccion del CAPITAL TOTAL de la
        cuenta (ej. -0.008 = -0.8% del capital), NO como variacion del
        precio de la operacion -- ver el aviso de unidades en el docstring
        del modulo.
        """
        self._roll_day_if_needed()
        self._state.trades_today += 1
        self._state.daily_pnl_pct += pnl_pct_of_capital
        if pnl_pct_of_capital <= 0:
            self._state.consecutive_losses += 1
        else:
            self._state.consecutive_losses = 0
        self._save()

        if self._state.daily_pnl_pct <= -abs(self.config.max_daily_loss_pct):
            self._trigger(
                f"perdida acumulada del dia ({self._state.daily_pnl_pct:.2%}) "
                f"alcanzo el umbral (-{self.config.max_daily_loss_pct:.2%})"
            )
        elif self._state.consecutive_losses >= self.config.max_consecutive_losses:
            self._trigger(
                f"{self._state.consecutive_losses} perdidas consecutivas "
                f"alcanzaron el umbral ({self.config.max_consecutive_losses})"
            )

    def record_connection_lost(self) -> None:
        """Marca la conexion como caida. Mientras lo este, no se abriran
        posiciones nuevas (ver can_open_new_position)."""
        self._roll_day_if_needed()
        if self._state.connection_lost_at is not None:
            return  # ya estaba marcada como caida
        self._state.connection_lost_at = self._now().isoformat()
        self._save()
        logger.warning(
            "Conexion perdida; no se abriran posiciones nuevas hasta "
            "reconectar y confirmar el estado real de la cuenta."
        )

    def record_connection_restored(self) -> None:
        """Marca la conexion como restablecida. Si la caida supero el
        umbral configurado, dispara el kill switch (fuerza revision manual
        del estado real de la cuenta antes de seguir operando)."""
        if self._state.connection_lost_at is None:
            return

        lost_since = datetime.fromisoformat(self._state.connection_lost_at)
        downtime_minutes = (self._now() - lost_since).total_seconds() / 60.0
        self._state.connection_lost_at = None
        self._save()

        if downtime_minutes > self.config.max_connection_loss_minutes:
            self._trigger(
                f"perdida de conexion de {downtime_minutes:.1f} minutos "
                f"supero el umbral ({self.config.max_connection_loss_minutes} min)"
            )
        else:
            logger.info("Conexion restablecida tras %.1f minutos.", downtime_minutes)

    def check_position_discrepancy(
        self, expected: dict[str, float], actual: dict[str, float]
    ) -> bool:
        """Compara las posiciones que el bot cree tener (`expected`) contra
        las que reporta el broker (`actual`) -- ambas como
        {simbolo: cantidad de acciones}. Si no coinciden, dispara el kill
        switch y devuelve True; si coinciden, devuelve False.
        """
        if expected != actual:
            self._trigger(
                f"discrepancia entre posiciones esperadas por el bot "
                f"({expected}) y las reportadas por el broker ({actual})"
            )
            return True
        return False

    def reset(self, confirmed_by: str) -> None:
        """Reanuda la operativa tras un kill switch activado.

        Debe llamarse SOLO manualmente (ej. desde un script de
        confirmacion explicito), tras revisar lo ocurrido -- nunca de
        forma automatica. `confirmed_by` es obligatorio y queda registrado
        en el log, como rastro de auditoria de quien autorizo reanudar.
        """
        if not confirmed_by:
            raise ValueError(
                "reset() requiere indicar quien confirma la reanudacion "
                "(parametro confirmed_by) -- no se puede reanudar de forma anonima."
            )
        logger.warning(
            "Kill switch reseteado manualmente por '%s'. Motivo anterior: %s",
            confirmed_by,
            self._state.trigger_reason,
        )
        self._state = KillSwitchState(date=self._now().date().isoformat())
        self._save()
