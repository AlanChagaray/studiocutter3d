"""Estado de los trabajos.

Hoy es un diccionario en memoria y alcanza: un usuario, un proceso de uvicorn.
Pero el acceso pasa por el `Protocol` `AlmacenTrabajos`, no por el dict, y eso
es deliberado. El dia que haya varios workers —o varios clientes pagando por
el servicio— entra un `AlmacenSQLite` con la misma firma y **no cambia nada
mas**. Por eso `Trabajo` ya lleva `propietario` aunque hoy valga siempre lo
mismo: convertir esto en multiusuario tiene que ser agregar un filtro, no
agregar una columna y revisar cada router.

**`obtener()` exige el propietario.** No hay forma de leer un trabajo sin
decir de quien sos: la autorizacion vive en el almacen y no en cada handler,
asi que un router nuevo no puede olvidarse de chequearla.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class EstadoTrabajo(StrEnum):
    """Los cuatro estados posibles. El front ramifica sobre estos strings."""

    EN_COLA = "en_cola"
    PROCESANDO = "procesando"
    LISTO = "listo"
    ERROR = "error"

    @property
    def terminal(self) -> bool:
        """True si ya no va a cambiar mas: el polling puede parar."""
        return self in (EstadoTrabajo.LISTO, EstadoTrabajo.ERROR)


class TipoTrabajo(StrEnum):
    CONVERSOR = "conversor"
    LINEAS = "lineas"
    CORTANTE = "cortante"


@dataclass
class Trabajo:
    """Un trabajo en curso o terminado. Vive en memoria; los archivos en disco."""

    id: str
    propietario: str
    tipo: TipoTrabajo
    estado: EstadoTrabajo = EstadoTrabajo.EN_COLA
    creado_en: datetime = field(default_factory=lambda: datetime.now(UTC))
    actualizado_en: datetime = field(default_factory=lambda: datetime.now(UTC))
    etapa: str = "en cola"
    archivos: dict[str, str] = field(default_factory=dict)
    """clave del enum -> nombre del archivo dentro del directorio del trabajo."""
    reporte: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def como_json(self) -> dict[str, Any]:
        """La forma que consume el polling del front. Sin rutas del filesystem."""
        return {
            "id": self.id,
            "tipo": self.tipo.value,
            "estado": self.estado.value,
            "etapa": self.etapa,
            "creado_en": self.creado_en.isoformat(),
            "actualizado_en": self.actualizado_en.isoformat(),
            "archivos": sorted(self.archivos),
            "reporte": self.reporte,
            "error": self.error,
        }


class AlmacenTrabajos(Protocol):
    """La costura por donde se corta el dia que esto sea multiusuario."""

    def crear(self, propietario: str, tipo: TipoTrabajo) -> Trabajo: ...

    def obtener(self, id_: str, propietario: str) -> Trabajo | None: ...

    def actualizar(self, id_: str, **cambios: Any) -> Trabajo | None: ...

    def vencidos(self, ttl_s: int) -> list[Trabajo]: ...

    def eliminar(self, id_: str) -> None: ...


class AlmacenEnMemoria:
    """Implementacion de hoy: dict protegido por un lock reentrante.

    El lock hace falta de verdad: los handlers de FastAPI corren en el
    threadpool y el vigilante de cada trabajo es otro hilo mas.
    """

    def __init__(self) -> None:
        self._trabajos: dict[str, Trabajo] = {}
        self._lock = threading.RLock()

    def crear(self, propietario: str, tipo: TipoTrabajo) -> Trabajo:
        trabajo = Trabajo(id=str(uuid.uuid4()), propietario=propietario, tipo=tipo)
        with self._lock:
            self._trabajos[trabajo.id] = trabajo
        return trabajo

    def obtener(self, id_: str, propietario: str) -> Trabajo | None:
        """None tanto si no existe como si es de otro: el que pregunta no
        puede distinguir un id inexistente de uno ajeno."""
        with self._lock:
            trabajo = self._trabajos.get(id_)
        if trabajo is None or trabajo.propietario != propietario:
            return None
        return trabajo

    def actualizar(self, id_: str, **cambios: Any) -> Trabajo | None:
        with self._lock:
            trabajo = self._trabajos.get(id_)
            if trabajo is None:
                return None
            for campo, valor in cambios.items():
                if not hasattr(trabajo, campo):
                    raise AttributeError(f"Trabajo no tiene el campo '{campo}'")
                setattr(trabajo, campo, valor)
            trabajo.actualizado_en = datetime.now(UTC)
            return trabajo

    def vencidos(self, ttl_s: int) -> list[Trabajo]:
        corte = datetime.now(UTC) - timedelta(seconds=ttl_s)
        with self._lock:
            return [t for t in self._trabajos.values() if t.actualizado_en < corte]

    def eliminar(self, id_: str) -> None:
        with self._lock:
            self._trabajos.pop(id_, None)
