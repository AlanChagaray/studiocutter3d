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
from .archivos import ClaveArchivo, escribir_estado, leer_estado
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

_series: set[str] = set()
"""Trabajos que estan corriendo una SERIE de pasos, de punta a punta.

Existe para el cupo: entre el final de un paso y el arranque del siguiente no hay
ningun proceso vivo, y sin este registro esos huecos serian una ventana por la
que entra otro trabajo y el tope de `max_trabajos_simultaneos` deja de valer.
Una serie ocupa un lugar desde que se lanza hasta que cierra."""

_canceladas: set[str] = set()
"""Series a las que se les pidio frenar. Se mira entre paso y paso.

Cancelar mata el hijo que esta corriendo —eso lo hace `cancelar` como siempre—
pero sin esta marca el vigilante arrancaria el paso siguiente como si nada: se
mato UN proceso de veinticinco y el trabajo seguiria hasta el final."""


def proceso_de(id_: str) -> mp.process.BaseProcess | None:
    """El proceso hijo de un trabajo, si todavia se lo conoce.

    Existe para que los tests puedan afirmar que un trabajo cortado por
    timeout dejo el hijo **muerto** de verdad, no solo marcado como error.
    """
    with _lock:
        return _procesos.get(id_)


def vivos() -> int:
    """Cuanto del cupo esta ocupado ahora mismo.

    No es "cuantos procesos hay": una serie cuenta como **uno** de punta a punta,
    tambien en los huecos entre paso y paso, cuando no tiene ningun hijo vivo.
    Contar solo procesos dejaria esos huecos como una puerta para pasarse del
    tope, que es justo lo que el tope existe para impedir.
    """
    with _lock:
        sueltos = sum(1 for id_, p in _procesos.items() if p.is_alive() and id_ not in _series)
        return sueltos + len(_series)


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
    """Mata el hijo de un trabajo. True si habia algo vivo que matar.

    En una serie marca ademas la cancelacion, que es lo que impide que el
    vigilante arranque el paso siguiente: matar el proceso de turno sin eso
    cancelaria un diseño y no el trabajo.
    """
    with _lock:
        en_serie = id_ in _series
    if en_serie:
        with _lock:
            _canceladas.add(id_)
    proceso = proceso_de(id_)
    if proceso is None or not proceso.is_alive():
        if not en_serie:
            return False
        almacen.actualizar(
            id_,
            estado=EstadoTrabajo.ERROR,
            etapa="cancelado",
            error={"codigo": "cancelado", "mensaje": "El trabajo se cancelo.", "detalle": {}},
        )
        return True
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


# ── Series: varios pasos, de a UNO por vez ──────────────────────────────────


def lanzar_serie(
    *,
    almacen: AlmacenTrabajos,
    a: Ajustes,
    trabajo: Trabajo,
    objetivo: Callable[..., None],
    pasos: tuple[tuple[Any, ...], ...],
    reporte_base: dict[str, Any] | None = None,
) -> None:
    """Corre `pasos` uno detras del otro. **Nunca hay dos hijos de este trabajo.**

    Es lo que pide un post de 25 diseños: no hace falta paralelismo y si hace
    falta no saturar el servidor. Cada paso es un proceso propio y no una vuelta
    de un solo proceso largo, por tres razones concretas:

    1. **El techo de RAM es por proceso** (`RLIMIT_DATA`, ver `tareas.py`). Un
       unico proceso que encadena 25 mallas acumula fragmentacion y el techo
       deja de significar "lo que cuesta un diseño".
    2. **El timeout es por proceso.** Con uno solo, un diseño patologico se come
       el presupuesto de los otros 24 y no hay forma de decir cual fue.
    3. **Un diseño ilegible no se lleva puestos a los demas.** Muere su proceso,
       se anota el fallo y sigue el siguiente.

    La serie ocupa **un solo lugar** del cupo de trabajos simultaneos de punta a
    punta, incluidos los huecos entre proceso y proceso: ver `vivos()`.

    `reporte_base` es lo que el llamador quiere que sobreviva al cierre —en F4,
    los nombres de descarga de cada diseño—. Viaja **explicito** y no se lee del
    almacen a proposito: `actualizar` reemplaza el campo entero, asi que el
    cierre tiene que saber sobre que escribe, y releer el trabajo para
    averiguarlo dependeria de que el almacen devuelva la instancia viva. Hoy la
    devuelve; el `Protocol` no lo promete.
    """
    if not pasos:
        almacen.eliminar(trabajo.id)
        raise ErrorApi("falta_archivo", "No hay ningun diseño para procesar.", estado=422)

    if vivos() >= a.max_trabajos_simultaneos:
        almacen.eliminar(trabajo.id)
        raise ErrorApi(
            "demasiados_trabajos",
            "Hay demasiados trabajos en curso. Espera a que termine alguno.",
            estado=429,
            detalle={"en_curso": a.max_trabajos_simultaneos},
        )

    with _lock:
        _series.add(trabajo.id)
    almacen.actualizar(
        trabajo.id, estado=EstadoTrabajo.PROCESANDO, etapa=f"diseño 1 de {len(pasos)}"
    )

    threading.Thread(
        target=_vigilar_serie,
        args=(almacen, a, trabajo.id),
        kwargs={"objetivo": objetivo, "pasos": pasos, "reporte_base": reporte_base or {}},
        name=f"serie-{trabajo.id[:8]}",
        daemon=True,
    ).start()


def _vigilar_serie(
    almacen: AlmacenTrabajos,
    a: Ajustes,
    id_: str,
    *,
    objetivo: Callable[..., None],
    pasos: tuple[tuple[Any, ...], ...],
    reporte_base: dict[str, Any],
) -> None:
    """Lanza los pasos de a uno y cierra el trabajo con el resumen de todos."""
    dir_trabajo = a.dir_trabajo / id_
    contexto = mp.get_context("spawn")
    fallidos: list[int] = []
    try:
        for numero, argumentos in enumerate(pasos, start=1):
            if _cancelada(id_):
                log.info("serie %s cancelada en el paso %d", id_, numero)
                return
            almacen.actualizar(id_, etapa=f"diseño {numero} de {len(pasos)}")

            proceso = contexto.Process(target=objetivo, args=argumentos, daemon=True)
            proceso.start()
            with _lock:
                _procesos[id_] = proceso

            limite = time.monotonic() + a.timeout_trabajo_s
            while proceso.is_alive() and time.monotonic() < limite:
                proceso.join(INTERVALO_VIGILANCIA_S)
            if proceso.is_alive():
                _matar(proceso)
                fallidos.append(numero)
                log.warning("diseño %d del trabajo %s cortado por timeout", numero, id_)
                _anotar_timeout(dir_trabajo, numero, a.timeout_trabajo_s)
                continue

            datos = leer_estado(dir_trabajo, numero)
            if datos is None or datos.get("ok") is not True:
                fallidos.append(numero)
                if datos is None:
                    log.error(
                        "diseño %d del trabajo %s termino sin estado (exitcode=%s)",
                        numero,
                        id_,
                        proceso.exitcode,
                    )
        if not _cancelada(id_):
            _cerrar_serie(
                almacen,
                id_,
                dir_trabajo,
                total=len(pasos),
                fallidos=fallidos,
                reporte_base=reporte_base,
            )
    finally:
        with _lock:
            _series.discard(id_)
            _canceladas.discard(id_)
            _procesos.pop(id_, None)


def _anotar_timeout(dir_trabajo: Path, numero: int, timeout_s: int) -> None:
    """Deja el estado del paso que el hijo no llego a escribir.

    Sin esto, un diseño cortado por tiempo es indistinguible de uno que murio
    sin dejar rastro, y el reporte no puede decir que le paso. El padre lo
    escribe porque el hijo ya no existe.
    """
    escribir_estado(
        dir_trabajo,
        {
            "ok": False,
            "etapa": "cortado por tiempo",
            "archivos": {},
            "error": {
                "codigo": "tiempo_agotado",
                "mensaje": (
                    f"El diseño tardo mas de {timeout_s} segundos y se corto. "
                    "Puede ser una malla demasiado pesada."
                ),
                "detalle": {"timeout_s": timeout_s},
            },
        },
        numero,
    )


def _cerrar_serie(
    almacen: AlmacenTrabajos,
    id_: str,
    dir_trabajo: Path,
    *,
    total: int,
    fallidos: list[int],
    reporte_base: dict[str, Any],
) -> None:
    """Cierra el trabajo juntando el estado de cada paso.

    **Alcanza con que UNO salga bien.** Tirar 24 diseños buenos porque el
    veinticinco vino corrupto seria cambiar un problema chico por uno grande, y
    lo que falla se declara: cada fallido va al reporte con su motivo y la
    pantalla lo muestra. Solo si no se salvo ninguno el trabajo queda en ERROR.
    """
    disenos: list[dict[str, Any]] = []
    for numero in range(1, total + 1):
        datos = leer_estado(dir_trabajo, numero) or {}
        entrada: dict[str, Any] = {"indice": numero, "ok": datos.get("ok") is True}
        if entrada["ok"]:
            entrada["reporte"] = datos.get("reporte")
        else:
            entrada["error"] = datos.get("error") or {
                "codigo": "interno",
                "mensaje": "El diseño termino sin dejar resultado.",
                "detalle": {},
            }
        disenos.append(entrada)

    logrados = total - len(fallidos)
    if logrados == 0:
        almacen.actualizar(
            id_,
            estado=EstadoTrabajo.ERROR,
            etapa="error",
            reporte={**reporte_base, "disenos": disenos, "logrados": 0, "total": total},
            error={
                "codigo": "malla_ilegible",
                "mensaje": "No se pudo leer ninguno de los diseños.",
                "detalle": {"total": total},
            },
        )
        return

    almacen.actualizar(
        id_,
        estado=EstadoTrabajo.LISTO,
        etapa="listo",
        reporte={**reporte_base, "disenos": disenos, "logrados": logrados, "total": total},
    )


def _cancelada(id_: str) -> bool:
    with _lock:
        return id_ in _canceladas


def detener_todo() -> None:
    """Mata lo que quede vivo y vacia el registro. **Es para los tests.**

    Existe por lo mismo que `proteccion.reiniciar_frenos`: `_procesos` y
    `_series` son estado de modulo —tienen que serlo, el vigilante es un hilo y
    no recibe fixtures— asi que un test que lanza un trabajo se lo deja al
    siguiente. Con el cupo en 3, tres tests que lanzan y no esperan hacen que el
    cuarto reciba un 429 **segun el orden de ejecucion**: la clase de test
    intermitente que despues nadie puede reproducir.

    No es un `cancelar` por cada uno: no toca el almacen, porque aca los trabajos
    ya no le importan a nadie. Solo suelta los recursos.
    """
    with _lock:
        procesos = list(_procesos.values())
        _canceladas.update(_series)
    for proceso in procesos:
        if proceso.is_alive():
            _matar(proceso)
    with _lock:
        _procesos.clear()
        _series.clear()
        _canceladas.clear()
