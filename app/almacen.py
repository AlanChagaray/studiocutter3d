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
    POST = "post"


TIPOS_CON_VISTA = frozenset({TipoTrabajo.CORTANTE, TipoTrabajo.POST})
"""Los que dejan un `.glb` y por lo tanto pueden tener foto cenital.

Vive aca y no en el router que lo usa (`PUT /trabajos/{id}/imagen`) para que sea
una propiedad del tipo de trabajo y no una condicion escrita en un `if`: el dia
que aparezca un cuarto tipo con visor, quien lo agregue lo ve en el enum."""


@dataclass
class Trabajo:
    """Un trabajo en curso o terminado. Vive en memoria; los archivos en disco."""

    id: str
    propietario: str
    tipo: TipoTrabajo
    nombre_base: str | None = None
    """Stem saneado del archivo con el que arranco el trabajo, o `None`.

    Solo decide como se VE la descarga (`nombre_de_descarga`). Nunca una ruta:
    los archivos en disco los nombran `EXTENSION` y `NOMBRE_DE`. Vive aca —y no
    en disco— porque la descarga ya exige que el `Trabajo` exista, asi que si se
    perdio el almacen la descarga da 404 igual y el nombre no hace falta."""

    estado: EstadoTrabajo = EstadoTrabajo.EN_COLA
    creado_en: datetime = field(default_factory=lambda: datetime.now(UTC))
    actualizado_en: datetime = field(default_factory=lambda: datetime.now(UTC))
    etapa: str = "en cola"
    archivos: dict[str, str] = field(default_factory=dict)
    """clave del enum -> nombre del archivo dentro del directorio del trabajo."""
    reporte: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    disenos: int = 0
    """Cuantos diseños tiene el trabajo. Solo F4 lo usa; el resto se queda en 0.

    Es un CONTADOR y no una lista, y esa es toda la idea: los archivos por diseño
    no entran en `archivos` —que mapea una clave de enum cerrado a un nombre
    fijo, y no escala a 25 copias de lo mismo— sino que se piden por clave **mas
    indice** (`ClaveDiseno` + `nombre_de_diseno`). Con el contador, el front sabe
    cuantas URLs armar y el servidor cuantas validar; el detalle de cada diseño
    viaja en `reporte`, que ya es libre.

    `archivos` en un post multiple queda con lo que es del TRABAJO y no de un
    diseño: hoy, solo `set`."""

    def como_json(self) -> dict[str, Any]:
        """La forma que consume el polling del front. Sin rutas del filesystem."""
        return {
            "id": self.id,
            "tipo": self.tipo.value,
            "nombre_base": self.nombre_base,
            "estado": self.estado.value,
            "etapa": self.etapa,
            "creado_en": self.creado_en.isoformat(),
            "actualizado_en": self.actualizado_en.isoformat(),
            "archivos": sorted(self.archivos),
            "disenos": self.disenos,
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
