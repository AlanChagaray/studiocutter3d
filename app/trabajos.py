"""Lanzar, vigilar y cancelar trabajos.

**Por que un `Process` por trabajo y no un `ProcessPoolExecutor`:** el pool no
sabe imponer un timeout. `future.result(timeout=N)` corta la *espera* del que
pregunta, no al worker, y `shutdown(cancel_futures=True)` no toca al que ya
esta corriendo. El timeout por trabajo es un requisito duro de seguridad, asi
que gana el `Process` dedicado, que con `join(timeout)` + `terminate()` lo
resuelve con la biblioteca estandar y sin dependencias nuevas.

El precio es el arranque en frio del hijo: con `spawn` (el unico modo en
Windows) el hijo reimporta numpy, scipy y trimesh en cada trabajo. Esta
medido y publicado en el reporte del ciclo.

Cada trabajo tiene un **hilo vigilante** que hace tres cosas: refleja el
progreso que el hijo va dejando en `estado.json`, corta por timeout, y traduce
el final a un cambio de estado en el almacen. Sin ese hilo, un hijo que muere
dejaria el trabajo en `procesando` para siempre.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .almacen import AlmacenTrabajos, EstadoTrabajo, Trabajo
from .archivos import ClaveArchivo, leer_estado
from .config import Ajustes
from .errores import ErrorApi

log = logging.getLogger("studiocutter")

_NOMBRE_PLANO = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,59}\Z")
"""Un nombre de archivo y nada mas, con la MISMA whitelist que el resto de la app.

La version obvia —`^[^\\\\/]+$`, "cualquier cosa sin separadores"— tiene tres
agujeros, los tres verificados y los tres relevantes en Windows, que es donde
esto corre:

- `$` matchea **antes de un salto de linea final**, asi que `"..\\n"` y `".\\n"`
  pasaban y ademas esquivaban el chequeo `texto in (".", "..")`.
- Los espacios finales pasaban, y la Win32 API los descarta: `".. "` resuelve al
  directorio padre.
- Los dos puntos pasaban: `"x:y"` es un flujo alternativo de datos (ADS) y
  `"C:evil"` una ruta relativa a unidad.

`\\A`/`\\Z` con `fullmatch` y una whitelist positiva cierran los tres de una, y de
paso hacen innecesario el caso especial de `"."` y `".."`.
"""

INTERVALO_VIGILANCIA_S = 0.4
"""Cada cuanto el vigilante mira `estado.json`. Suficiente para que la barra
de progreso se mueva sin castigar el disco."""

GRACIA_TERMINATE_S = 5.0

_procesos: dict[str, mp.process.BaseProcess] = {}
_lock = threading.Lock()


def proceso_de(id_: str) -> mp.process.BaseProcess | None:
    """El proceso hijo de un trabajo, si todavia se lo conoce.

    Existe para que los tests puedan afirmar que un trabajo cortado por
    timeout dejo el hijo **muerto** de verdad, no solo marcado como error.
    """
    with _lock:
        return _procesos.get(id_)


def vivos() -> int:
    """Cuantos procesos hijo hay corriendo ahora mismo."""
    with _lock:
        return sum(1 for p in _procesos.values() if p.is_alive())


def lanzar(
    *,
    almacen: AlmacenTrabajos,
    a: Ajustes,
    trabajo: Trabajo,
    objetivo: Callable[..., None],
    argumentos: tuple[Any, ...],
) -> None:
    """Arranca el hijo y su vigilante. Vuelve enseguida: no espera nada.

    Antes de arrancar chequea el tope de trabajos simultaneos: cada hijo carga
    el stack cientifico entero, asi que sin tope una rafaga de pedidos es una
    forma barata de quedarse sin memoria.
    """
    if vivos() >= a.max_trabajos_simultaneos:
        almacen.eliminar(trabajo.id)
        raise ErrorApi(
            "demasiados_trabajos",
            "Hay demasiados trabajos en curso. Espera a que termine alguno.",
            estado=429,
            detalle={"en_curso": a.max_trabajos_simultaneos},
        )

    contexto = mp.get_context("spawn")
    proceso = contexto.Process(target=objetivo, args=argumentos, daemon=True)
    proceso.start()

    with _lock:
        _procesos[trabajo.id] = proceso

    almacen.actualizar(trabajo.id, estado=EstadoTrabajo.PROCESANDO, etapa="arrancando el motor")

    threading.Thread(
        target=_vigilar,
        args=(almacen, a, trabajo.id, proceso),
        name=f"vigia-{trabajo.id[:8]}",
        daemon=True,
    ).start()


def cancelar(almacen: AlmacenTrabajos, id_: str) -> bool:
    """Mata el hijo de un trabajo. True si habia algo vivo que matar."""
    proceso = proceso_de(id_)
    if proceso is None or not proceso.is_alive():
        return False
    _matar(proceso)
    almacen.actualizar(
        id_,
        estado=EstadoTrabajo.ERROR,
        etapa="cancelado",
        error={"codigo": "cancelado", "mensaje": "El trabajo se cancelo.", "detalle": {}},
    )
    return True


def _matar(proceso: mp.process.BaseProcess) -> None:
    """`terminate()` y, si no alcanza, `kill()`. No se deja nada vivo."""
    proceso.terminate()
    proceso.join(GRACIA_TERMINATE_S)
    if proceso.is_alive():
        proceso.kill()
        proceso.join(GRACIA_TERMINATE_S)


def _vigilar(
    almacen: AlmacenTrabajos, a: Ajustes, id_: str, proceso: mp.process.BaseProcess
) -> None:
    dir_trabajo = a.dir_trabajo / id_
    limite = time.monotonic() + a.timeout_trabajo_s

    while proceso.is_alive() and time.monotonic() < limite:
        proceso.join(INTERVALO_VIGILANCIA_S)
        _reflejar_etapa(almacen, id_, dir_trabajo)

    if proceso.is_alive():
        _matar(proceso)
        almacen.actualizar(
            id_,
            estado=EstadoTrabajo.ERROR,
            etapa="cortado por tiempo",
            error={
                "codigo": "tiempo_agotado",
                "mensaje": (
                    f"El trabajo tardo mas de {a.timeout_trabajo_s} segundos y se corto. "
                    "Proba con un dibujo mas simple o un tamaño menor."
                ),
                "detalle": {"timeout_s": a.timeout_trabajo_s},
            },
        )
        log.warning("trabajo %s cortado por timeout", id_)
        return

    _cerrar(almacen, id_, dir_trabajo, proceso.exitcode)


def _reflejar_etapa(almacen: AlmacenTrabajos, id_: str, dir_trabajo: Path) -> None:
    datos = leer_estado(dir_trabajo)
    if datos is None or datos.get("ok") is not None:
        return  # todavia no escribio, o ya es el resultado final
    etapa = datos.get("etapa")
    if isinstance(etapa, str) and etapa:
        almacen.actualizar(id_, etapa=etapa)


def _cerrar(almacen: AlmacenTrabajos, id_: str, dir_trabajo: Path, exitcode: int | None) -> None:
    """Traduce el final del hijo al estado del trabajo.

    La regla del contrato: **`estado.json` ausente o sin `ok` definido
    significa fallo**, nunca "todavia no". El hijo lo escribe siempre, incluso
    cuando el motor levanta una excepcion; si no esta, murio antes de poder.
    """
    datos = leer_estado(dir_trabajo)
    if datos is None or datos.get("ok") is None:
        almacen.actualizar(
            id_,
            estado=EstadoTrabajo.ERROR,
            etapa="error",
            error={
                "codigo": "interno",
                "mensaje": "El procesamiento termino sin dejar resultado.",
                "detalle": {"exitcode": exitcode},
            },
        )
        log.error("trabajo %s termino sin estado.json (exitcode=%s)", id_, exitcode)
        return

    if datos.get("ok") is True:
        almacen.actualizar(
            id_,
            estado=EstadoTrabajo.LISTO,
            etapa="listo",
            archivos=_claves_conocidas(datos.get("archivos")),
            reporte=datos.get("reporte"),
        )
        return

    error = datos.get("error")
    almacen.actualizar(
        id_,
        estado=EstadoTrabajo.ERROR,
        etapa="error",
        error=error
        if isinstance(error, dict)
        else {"codigo": "interno", "mensaje": "El procesamiento fallo.", "detalle": {}},
    )


def _claves_conocidas(crudo: object) -> dict[str, str]:
    """Filtra `archivos` contra el enum antes de que entre al almacen.

    Lo que viene de `estado.json` lo escribio otro proceso. Una clave que no
    este en `ClaveArchivo` reventaria despues, lejos de aca, cuando alguien la
    convierta — y el sintoma seria un 500 en una descarga, no un dato malo en
    el origen. Se descarta en la frontera.
    """
    if not isinstance(crudo, dict):
        return {}
    validas: dict[str, str] = {}
    for clave, nombre in crudo.items():
        try:
            ClaveArchivo(clave)
        except ValueError:
            log.warning("el hijo declaro una clave de archivo desconocida: %r", clave)
            continue
        texto = str(nombre)
        # El VALOR tambien se valida, no solo la clave. Hoy nadie lo usa para
        # armar una ruta —`ruta_de` reconstruye desde `NOMBRE_DE`— asi que un
        # `../../credenciales.json` guardado aca seria inofensivo. Se filtra
        # igual: el dia que alguien decida consumir este valor, la defensa ya
        # va a estar puesta en la frontera y no va a depender de que se acuerde.
        if not _NOMBRE_PLANO.fullmatch(texto):
            log.warning("el hijo declaro un nombre de archivo no plano: %r", texto)
            continue
        validas[str(clave)] = texto
    return validas


def olvidar(id_: str) -> None:
    """Saca el proceso del registro. Lo llama la limpieza por TTL."""
    with _lock:
        _procesos.pop(id_, None)
