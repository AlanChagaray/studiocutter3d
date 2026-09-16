"""Consulta de trabajos, descarga de archivos y subida de la foto del cortante.

Es el unico lugar de la app que mueve bytes del disco de un trabajo —en las dos
direcciones—, asi que concentra las defensas y no hay ninguna escondida en otro
router:

1. **El propietario se chequea en el almacen.** `obtener()` devuelve `None`
   tanto para un id inexistente como para uno ajeno: desde afuera son
   indistinguibles, asi que no se puede sondear que ids existen. Es lo primero
   que corre en los cinco handlers.
2. **La clave viene de un enum cerrado.** FastAPI rechaza con 422 todo lo que
   no este en `ClaveArchivo`, antes de entrar al handler. El cliente nombra
   una clave, nunca un archivo.
3. **El id se valida como UUID**, dentro de `dir_de_trabajo`: cualquier cosa
   con separadores de ruta no llega a tocar el filesystem.

⚠ El orden de arriba es el orden real, y no es el que uno supondria. La
validacion del UUID **no** es lo primero: un id malformado muere antes, en el
punto 1, porque no esta en el almacen —cuyas claves las pone `uuid4`—, no
porque se lo haya parseado. Son dos defensas distintas y conviene no
confundirlas; `subir_imagen` lo repite en su propio docstring porque es el
unico handler donde la diferencia se puede llegar a notar.

**La foto del cortante entra por aca y no por `cortante.py`.** La rinde el
navegador con WebGL —el servidor no tiene con que— y despues se sube, para que
sea un archivo del trabajo como cualquier otro: se baja por el mismo endpoint,
con el mismo criterio de nombre, y entra en el ZIP. Que el unico camino de
escritura viva junto a los de lectura es el punto: las defensas se leen juntas.
Sobre las tres de arriba suma dos propias —solo trabajos de tipo cortante y ya
terminados— y delega el resto en `guardar_subida`, que decide por los BYTES y
no por el `Content-Type` ni el `filename`, igual que con el arte que sube el
usuario.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from cutter3d import lamina
from cutter3d.errors import Cutter3DError

from ..almacen import TIPOS_CON_VISTA, EstadoTrabajo, TipoTrabajo, Trabajo
from ..archivos import (
    FORMATOS_VISTA,
    LIMITE_VISTA_BYTES,
    MEDIO_DE,
    MEDIO_DISENO,
    NOMBRE_DE,
    ClaveArchivo,
    ClaveDiseno,
    claves_descargables,
    dir_de_trabajo,
    guardar_subida,
    nombre_de_descarga,
    nombre_de_descarga_de_diseno,
    nombre_de_diseno,
    nombre_de_zip,
    ruta_de,
    ruta_de_diseno,
)
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import ErrorApi, traducir
from ..trabajos import cancelar

router = APIRouter(prefix="/api/trabajos", tags=["trabajos"])

log = logging.getLogger(__name__)

MEDIO_ZIP = "application/zip"

TROZO_ZIP = 64 * 1024

COMPRESION_DE: dict[ClaveArchivo, int] = {
    ClaveArchivo.TRES_MF: zipfile.ZIP_STORED,
    ClaveArchivo.TRES_MF_MARCADOR: zipfile.ZIP_STORED,
    ClaveArchivo.TRES_MF_CORTADOR: zipfile.ZIP_STORED,
    ClaveArchivo.JPG_VISTA: zipfile.ZIP_STORED,
}
"""Lo que NO se vuelve a comprimir al armar el ZIP. El resto va con deflate.

**Un `.3mf` ES un ZIP** —deflate adentro de deflate— y un JPEG ya esta
comprimido: pasarlos por zlib quema CPU completa para ganar cerca de nada. En
un cortante con marcador son cuatro de los seis miembros, asi que no es una
micro-optimizacion: es la diferencia entre que este endpoint cueste ~1,5 s de
CPU por pedido o una fraccion de eso. Importa porque el freno por IP cuenta
PEDIDOS y no costo, y esta ruta es de lejos la mas cara de la app.

Los `.stl` son texto/binario plano y si comprimen — esos se quedan con deflate."""

PREFIJO_TEMPORAL = ".todo-"
SUFIJO_TEMPORAL = ".zip.tmp"
"""Como se llama el ZIP mientras se arma.

Arranca con punto para que quede oculto por convencion —igual que el
`.estado-*.tmp` de `escribir_estado`— y no termina en `.zip` a secas para que
un listado no lo confunda con una salida del trabajo. Los dos juntos son
ademas lo que hace reconocible al temporal para `_limpiar_temporales`."""


def _borrar(ruta: Path) -> None:
    """Borra sin hacer ruido, y dejando rastro si no se puede.

    `missing_ok` no alcanza: en Windows un antivirus o el indexador con el
    handle abierto levanta `PermissionError`, no `FileNotFoundError`. Lo que
    quede sin borrar lo junta `_limpiar_temporales` en el pedido siguiente, y
    en ultima instancia el barrido por TTL del directorio.
    """
    try:
        ruta.unlink(missing_ok=True)
    except OSError:
        log.warning("no se pudo borrar el temporal %s", ruta.name)


EDAD_TEMPORAL_S = 15 * 60
"""Desde cuando un temporal del ZIP se considera abandonado.

⚠ **El barrido va por EDAD y no "todos", y no es una optimizacion.** Dos
pedidos al mismo trabajo corren en paralelo —los handlers son `def`, o sea que
FastAPI los manda al threadpool—, asi que borrar todos los temporales del
directorio significaba que el segundo pedido le volaba el archivo al primero
mientras lo estaba armando. En POSIX `unlink` no falla por tener el handle
abierto, asi que ni siquiera daba error: daba un 500 o un ZIP de cero bytes con
un `content-length` que prometia otra cosa.

Quince minutos es mas que cualquier descarga viva y mucho menos que el TTL de
6 h del directorio, que es el ultimo cinturon."""


def _limpiar_temporales(directorio: Path) -> None:
    """Junta los temporales que algun pedido anterior no llego a borrar.

    Es el segundo cinturon del `finally` del generador: si el proceso murio a
    mitad de una descarga, ese ZIP queda. Recogerlo al empezar el pedido
    siguiente mantiene el costo acotado en vez de acumular uno por pedido.
    """
    corte = time.time() - EDAD_TEMPORAL_S
    for viejo in directorio.glob(f"{PREFIJO_TEMPORAL}*{SUFIJO_TEMPORAL}"):
        try:
            if viejo.stat().st_mtime < corte:
                _borrar(viejo)
        except OSError:
            continue


def _exigir_trabajo(almacen: AlmacenDep, id_: str, usuario: str) -> Trabajo:
    trabajo = almacen.obtener(id_, usuario)
    if trabajo is None:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    return trabajo


def _sin_descargables() -> ErrorApi:
    """El 404 del ZIP cuando no hay nada que empaquetar.

    Sale de dos puntos del mismo handler —antes de armar y despues, si ningun
    miembro entro— y el mensaje tiene que ser el mismo en los dos: son la misma
    situacion vista en dos momentos."""
    return ErrorApi(
        "archivo_inexistente",
        "Este trabajo todavia no tiene archivos para descargar.",
        estado=404,
    )


@router.get("/{id_}")
def estado_del_trabajo(
    id_: str, usuario: UsuarioRequerido, almacen: AlmacenDep
) -> dict[str, object]:
    """Lo que consulta el polling del front."""
    return _exigir_trabajo(almacen, id_, usuario).como_json()


@router.get("/{id_}/archivo/{clave}")
def descargar(
    id_: str,
    clave: ClaveArchivo,
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
) -> FileResponse:
    """Sirve un archivo del trabajo. El nombre lo decide el servidor."""
    trabajo = _exigir_trabajo(almacen, id_, usuario)
    ruta = ruta_de(a, id_, clave)
    if ruta is None:
        raise ErrorApi(
            "archivo_inexistente",
            "Ese archivo no esta disponible para este trabajo.",
            estado=404,
            detalle={"clave": clave.value},
        )
    return FileResponse(
        ruta,
        media_type=MEDIO_DE[clave],
        # Starlette percent-encodea lo que haga falta (`filename*=utf-8''...`),
        # asi que el header no se arma a mano: el saneo de `nombre_de_descarga`
        # es por como se LEE el nombre, no por seguridad del header.
        filename=nombre_de_descarga(id_, clave, trabajo.nombre_base),
    )


@router.get("/{id_}/zip")
def descargar_todo(
    id_: str,
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
) -> StreamingResponse:
    """Un ZIP con todos los archivos descargables del trabajo.

    Los miembros se llaman **igual que la descarga suelta** (`buddy.3mf`,
    `buddy-cortador.stl`, `buddy-vista.jpg`): un solo criterio de nombre en
    toda la app, no uno para el boton y otro para el ZIP.

    Se arma sobre un temporal en el directorio del trabajo y no en memoria: un
    cortante con marcador son seis archivos de varios MB cada uno, y tenerlos
    todos en RAM se multiplica por pedido concurrente.

    ⚠ **Se sirve con `StreamingResponse` y no con `FileResponse`, y el motivo
    es el borrado.** `FileResponse` soporta `Range`, y ante un header de rango
    invalido o insatisfacible **retorna antes** de correr su `background`: un
    `Range: bytes=abc` dejaba el temporal en disco, y como cada pedido escribe
    un ZIP completo de todas las salidas, repetirlo llenaba el directorio del
    trabajo hasta el barrido por TTL. El `Content-Length` se manda igual, asi
    que la descarga sigue mostrando progreso; lo unico que se pierde es poder
    reanudarla, que para un archivo que se arma al vuelo no significa nada.

    ⚠ **El borrado NO se apoya en el `finally` del generador**, aunque lo
    tenga. Ante una desconexion, Starlette **cancela** la tarea del stream y el
    generador queda suspendido en su `yield` sin que nadie le llame `close()`:
    ese `finally` termina corriendo por refcount del recolector, que en CPython
    es lo habitual pero **no es una garantia del flujo de control**. Lo que
    realmente sostiene esto es el `unlink` con el descriptor ya abierto, unas
    lineas mas abajo — ahi esta explicado.
    """
    trabajo = _exigir_trabajo(almacen, id_, usuario)
    claves = claves_descargables(trabajo.archivos)
    # En un post con varios diseños la mayor parte de lo que se baja NO esta en
    # `archivos`: las fotos por diseño se piden por clave mas indice. El ZIP las
    # junta igual —bajar 25 fotos de a una es justo lo que este boton evita— y
    # ademas el `set`, que si es una clave de trabajo comun y ya viene en `claves`.
    por_diseno = _fotos_de_disenos(a, trabajo)
    destino = dir_de_trabajo(a, id_)
    # El directorio se chequea ANTES de crear el temporal: `limpiar_vencidos`
    # borra el directorio y recien despues saca el trabajo del almacen, asi que
    # hay una ventana con trabajo vivo y carpeta muerta. Crear el temporal ahi
    # adentro tiraba `FileNotFoundError` y salia un 500 donde corresponde 404.
    if not (claves or por_diseno) or not destino.is_dir():
        raise _sin_descargables()

    _limpiar_temporales(destino)
    # `delete=False` porque el handle de escritura se cierra antes de servir, y
    # en Windows un temporal abierto dos veces falla.
    with tempfile.NamedTemporaryFile(
        dir=destino, prefix=PREFIJO_TEMPORAL, suffix=SUFIJO_TEMPORAL, delete=False
    ) as tmp:
        temporal = Path(tmp.name)
    try:
        with zipfile.ZipFile(temporal, "w", zipfile.ZIP_DEFLATED) as zip_:
            _llenar_zip(zip_, a, trabajo, claves, por_diseno)
            vacio = not zip_.namelist()
        if vacio:
            raise _sin_descargables()
        # ⚠ Se abre el handle ACA, antes de devolver la respuesta, y el tamaño
        # sale de ESE descriptor. No es lo mismo que medir con `stat()` y abrir
        # despues, dentro del generador: entre las dos cosas hay un hueco real
        # —el barrido por TTL corre en un hilo de fondo y `rmtree` el directorio
        # entero— y caer ahi significaba mandar un `content-length` de N bytes y
        # emitir cero, con los headers ya enviados y sin forma de responder un
        # error. Con el descriptor tomado antes, el archivo sobrevive a que lo
        # borren mientras se lo esta sirviendo.
        archivo = temporal.open("rb")
        tamano = os.fstat(archivo.fileno()).st_size
        # ⚠ Se borra el NOMBRE ahora, con el descriptor ya abierto.
        #
        # En POSIX —que es donde esto se despliega— `unlink` sobre un archivo
        # abierto saca la entrada del directorio pero conserva los bloques
        # hasta que se cierra el ultimo descriptor. O sea: el ZIP se sigue
        # sirviendo perfecto y el sistema recupera el espacio **solo**, aunque
        # el `finally` de abajo no llegue a correr nunca.
        #
        # Y puede no correr: con `StreamingResponse`, una desconexion del
        # cliente CANCELA la tarea del stream, y el generador queda suspendido
        # en su `yield` sin que nadie le llame `close()`. El `finally` termina
        # ejecutandose por refcount del recolector, que es lo normal en CPython
        # pero no es una garantia del flujo de control. Esto lo vuelve una
        # optimizacion en vez de la unica red: un cliente que abre conexiones y
        # no lee ya no puede acumular ZIPs en disco.
        #
        # En Windows `unlink` sobre un archivo abierto tira `PermissionError`,
        # `_borrar` lo registra y sigue: ahi la red son el `finally` y el
        # barrido por edad, como hasta recien.
        _borrar(temporal)
    except BaseException:
        _borrar(temporal)
        raise

    def leer() -> Iterator[bytes]:
        try:
            while trozo := archivo.read(TROZO_ZIP):
                yield trozo
        finally:
            archivo.close()
            _borrar(temporal)

    # El header se arma a mano —`StreamingResponse` no tiene `filename=`— y es
    # seguro por una razon concreta: `nombre_de_zip` solo devuelve nombres que
    # pasan `base_es_segura`, o sea la whitelist `[A-Za-z0-9._-]`, o el
    # fallback `studiocutter-<8 hex>`. No hay comillas, espacios ni no-ASCII
    # que percent-encodear. Si algun dia esa whitelist se afloja, esto hay que
    # volver a mirarlo.
    nombre = nombre_de_zip(id_, trabajo.nombre_base)
    return StreamingResponse(
        leer(),
        media_type=MEDIO_ZIP,
        headers={
            "content-length": str(tamano),
            "content-disposition": f'attachment; filename="{nombre}"',
        },
    )


@router.put("/{id_}/imagen")
def subir_imagen(
    id_: str,
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    archivo: Annotated[UploadFile, File()],
) -> dict[str, object]:
    """Recibe la foto cenital que rindio el navegador y la suma al trabajo.

    Es el unico camino por el que un cliente escribe en el directorio de un
    trabajo, asi que las defensas se declaran en orden y ninguna se da por
    supuesta:

    1. `_exigir_trabajo` → el trabajo existe y es del que pide. Ojo con lo que
       esto NO hace: **no valida el UUID**. Un id con separadores de ruta muere
       igual aca, pero porque no esta en el almacen —donde los ids los pone
       `uuid4`—, no porque se lo haya parseado. La validacion de forma llega en
       el paso 4, dentro de `dir_de_trabajo`. Son dos defensas distintas y
       conviene no confundirlas.
    2. **Solo trabajos que dejan un `.glb`** (`TIPOS_CON_VISTA`: el cortante y
       el post). El Convertidor y Correcto no tienen vista 3D: una foto ahi no
       significa nada. Responde 404 y no 403 a proposito — el mismo cuerpo que un
       trabajo inexistente, para no filtrar que el id existe.
    3. **Solo trabajos terminados.** Una foto de una geometria que todavia no
       existe seria una foto de otra cosa. Ademas es lo que hace segura la
       actualizacion de `archivos` de abajo: `LISTO` es terminal, o sea que el
       vigilante ya no escribe mas este trabajo y no hay dos escritores.
    4. **El contenido decide.** `guardar_subida` mira los BYTES (no el
       `Content-Type` ni el `filename`, que los elige quien sube), corta por
       tamaño **mientras copia** y borra lo escrito si se pasa. El nombre en
       disco sale de `NOMBRE_DE`: el cliente sigue sin nombrar nada.

    Devuelve el trabajo entero —y no un 204— por lo mismo que `POST
    /api/cortante`: el front repinta el grupo de descargas leyendo la
    respuesta, en vez de suponer que la clave aparecio.
    """
    trabajo = _exigir_trabajo(almacen, id_, usuario)
    if trabajo.tipo not in TIPOS_CON_VISTA:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    if trabajo.estado is not EstadoTrabajo.LISTO:
        raise ErrorApi(
            "trabajo_no_terminado",
            "El trabajo todavia no esta listo.",
            estado=409,
            detalle={"estado": trabajo.estado.value},
        )

    # `crear=False`: el paso 3 ya garantiza que el trabajo termino, o sea que
    # el motor escribio sus salidas ahi. Si el directorio no esta es porque lo
    # barrio el TTL, y en ese caso recrearlo dejaria un trabajo "listo" cuyo
    # unico archivo es una foto de algo que ya no se puede bajar.
    destino = dir_de_trabajo(a, id_)
    if not destino.is_dir():
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)

    clave = ClaveArchivo.JPG_VISTA
    guardar_subida(
        archivo.file,
        destino,
        permitidos=FORMATOS_VISTA,
        limite_bytes=LIMITE_VISTA_BYTES,
        destino_nombre=NOMBRE_DE[clave],
    )
    # ⚠ `actualizar` fija `actualizado_en = now()` SIEMPRE, y de ese campo
    # depende `vencidos()`, o sea el TTL de 6 h — que es la unica cota de disco
    # total que tiene el diseño. Sin restaurarlo, este endpoint seria el primer
    # camino por el que un cliente refresca el TTL de un trabajo YA TERMINADO:
    # un PUT de tres bytes cada cinco horas, invisible frente al freno de 240
    # pedidos por minuto, deja un directorio con todas las salidas vivo para
    # siempre. Subir la foto describe el trabajo; no es usarlo.
    #
    # El arreglo de fondo es que el almacen sepa distinguir "escribi un campo"
    # de "toca el TTL" (un `tocar=False` en `actualizar`), pero eso es cambiar
    # `app/almacen.py`, que esta fuera del mapa aprobado de este ciclo. Queda
    # como recomendacion en el reporte de seguridad.
    visto_en = trabajo.actualizado_en
    actualizado = almacen.actualizar(
        id_, archivos={**trabajo.archivos, clave.value: NOMBRE_DE[clave]}
    )
    if actualizado is None:
        # El trabajo se fue entre el chequeo y esto. Devolver 200 con el
        # `archivos` viejo —sin `jpg_vista`— seria contestar que salio bien con
        # datos que ya no son ciertos, y el front repintaria sobre eso.
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    # ⚠ Esto funciona porque `AlmacenEnMemoria.actualizar` devuelve la INSTANCIA
    # viva, no una copia. El `Protocol` `AlmacenTrabajos` no lo promete, asi que
    # el dia que entre un `AlmacenSQLite` —la costura que el almacen declara
    # tener preparada— esta linea se vuelve un no-op **y el test que la cubre
    # sigue en verde**, porque lee de la misma instancia. Es el motivo por el
    # que el arreglo de fondo es `tocar=False` en el almacen y no esto.
    actualizado.actualizado_en = visto_en
    return actualizado.como_json()


@router.delete("/{id_}")
def cancelar_trabajo(id_: str, usuario: UsuarioRequerido, almacen: AlmacenDep) -> JSONResponse:
    """Mata el proceso hijo de un trabajo en curso."""
    _exigir_trabajo(almacen, id_, usuario)
    return JSONResponse({"cancelado": cancelar(almacen, id_)})


# ── Archivos por diseño (F4 con varios diseños) ─────────────────────────────


def _exigir_post(almacen: AlmacenDep, id_: str, usuario: str) -> Trabajo:
    """El trabajo, y que sea un post. 404 —no 403— si no lo es.

    El mismo cuerpo que un trabajo inexistente, por lo mismo que en `subir_imagen`:
    responder distinto filtraria que el id existe.
    """
    trabajo = _exigir_trabajo(almacen, id_, usuario)
    if trabajo.tipo is not TipoTrabajo.POST or trabajo.disenos < 1:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    return trabajo


def _exigir_diseno(trabajo: Trabajo, indice: int) -> int:
    """Que el indice exista EN ESTE trabajo, no solo que sea un entero valido.

    `validar_indice` acota al rango del producto (1..25); esto lo acota a los
    diseños que este trabajo tiene de verdad. Sin lo segundo, pedir el diseño 20
    de un trabajo de 3 daria 404 igual pero recien al no encontrar el archivo, y
    eso es depender de un accidente del disco en vez de una regla.
    """
    if not 1 <= indice <= trabajo.disenos:
        raise ErrorApi(
            "diseno_inexistente",
            "No existe ese diseño en el trabajo.",
            estado=404,
            detalle={"indice": indice, "disenos": trabajo.disenos},
        )
    return indice


def _nombre_de_diseno_para_bajar(trabajo: Trabajo, clave: ClaveDiseno, indice: int) -> str:
    """El `filename=` de la descarga, con el nombre propio del diseño si lo hay."""
    nombres = (trabajo.reporte or {}).get("nombres")
    base = None
    if isinstance(nombres, list) and 1 <= indice <= len(nombres):
        crudo = nombres[indice - 1]
        base = crudo if isinstance(crudo, str) else None
    return nombre_de_descarga_de_diseno(
        trabajo.id, clave, indice, base, con_indice=trabajo.disenos > 1
    )


@router.get("/{id_}/diseno/{indice}/archivo/{clave}")
def descargar_de_diseno(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    *,
    id_: str,
    indice: int,
    clave: ClaveDiseno,
) -> FileResponse:
    """El `.glb` o la foto de UN diseño.

    `clave` la valida FastAPI contra el enum antes de entrar, e `indice` lo
    validan `_exigir_diseno` y despues `nombre_de_diseno`. El cliente manda una
    palabra de una lista cerrada y un entero acotado: sigue sin nombrar nada.
    """
    trabajo = _exigir_post(almacen, id_, usuario)
    _exigir_diseno(trabajo, indice)
    ruta = ruta_de_diseno(a, id_, clave, indice)
    if ruta is None:
        raise ErrorApi("archivo_inexistente", "Ese archivo no existe.", estado=404)
    return FileResponse(
        ruta,
        media_type=MEDIO_DISENO[clave],
        filename=_nombre_de_diseno_para_bajar(trabajo, clave, indice),
    )


@router.put("/{id_}/diseno/{indice}/imagen")
def subir_imagen_de_diseno(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    *,
    id_: str,
    indice: int,
    archivo: Annotated[UploadFile, File()],
) -> dict[str, object]:
    """La foto cenital de UN diseño, rendida por el navegador.

    Es el gemelo por indice de `subir_imagen`, con las mismas cuatro defensas en
    el mismo orden. Lo que **no** hace es tocar `archivos` del trabajo: los
    archivos por diseño no viven ahi (ver `Trabajo.disenos`), asi que no hay nada
    que actualizar y el TTL no se toca por construccion en vez de a mano.
    """
    trabajo = _exigir_post(almacen, id_, usuario)
    _exigir_diseno(trabajo, indice)
    if trabajo.estado is not EstadoTrabajo.LISTO:
        raise ErrorApi(
            "trabajo_no_terminado",
            "El trabajo todavia no esta listo.",
            estado=409,
            detalle={"estado": trabajo.estado.value},
        )
    destino = dir_de_trabajo(a, id_)
    if not destino.is_dir():
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)

    guardar_subida(
        archivo.file,
        destino,
        permitidos=FORMATOS_VISTA,
        limite_bytes=LIMITE_VISTA_BYTES,
        destino_nombre=nombre_de_diseno(ClaveDiseno.JPG_VISTA, indice),
    )
    return {"indice": indice, "ok": True}


@router.post("/{id_}/set")
def componer_set(
    id_: str, usuario: UsuarioRequerido, almacen: AlmacenDep, a: AjustesDep
) -> dict[str, object]:
    """Pega las fotos de todos los diseños en una sola imagen.

    Se pide **despues** de que el navegador subio cada foto, y se compone del
    lado del servidor a proposito: asi la grilla, la separacion y el fondo de los
    huecos son cosas que la suite puede medir. Ver `cutter3d/lamina.py`.

    Corre **en el proceso web** y no en un hijo, como F1: el costo real es la
    lamina mas una celda viva a la vez —del orden de 13 MB— porque `Image.draft`
    baja cada foto al tamaño de su celda al abrirla. Mandarlo a un proceso nuevo
    costaria mas el `spawn` que la composicion.

    Solo entran los diseños que **tienen** foto: si uno fallo al convertirse, el
    set sale con los que si salieron y se declara cuantos entraron. Negarse a
    armar el set por un diseño de veinticinco seria cambiar un problema chico por
    uno grande.
    """
    trabajo = _exigir_post(almacen, id_, usuario)
    if trabajo.estado is not EstadoTrabajo.LISTO:
        raise ErrorApi(
            "trabajo_no_terminado",
            "El trabajo todavia no esta listo.",
            estado=409,
            detalle={"estado": trabajo.estado.value},
        )

    fotos = [
        ruta
        for indice in range(1, trabajo.disenos + 1)
        if (ruta := ruta_de_diseno(a, id_, ClaveDiseno.JPG_VISTA, indice)) is not None
    ]
    if not fotos:
        raise ErrorApi(
            "sin_fotos",
            "Todavia no hay ninguna foto para armar el set.",
            estado=409,
            detalle={"disenos": trabajo.disenos},
        )

    clave = ClaveArchivo.SET
    try:
        reporte = lamina.componer(fotos, dir_de_trabajo(a, id_) / NOMBRE_DE[clave])
    except Cutter3DError as exc:
        raise traducir(exc) from exc

    # ⚠ El TTL no se refresca, por lo mismo que en `subir_imagen`: armar el set
    # describe el trabajo, no es usarlo. Ver la nota larga de ahi.
    visto_en = trabajo.actualizado_en
    actualizado = almacen.actualizar(
        id_, archivos={**trabajo.archivos, clave.value: NOMBRE_DE[clave]}
    )
    if actualizado is None:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    actualizado.actualizado_en = visto_en

    return {
        "celdas": reporte.celdas,
        # El reparto entero y no solo "columnas x filas": con filas desparejas
        # ese par no describe el armado — un set de 7 y uno de 9 dan los dos 3x3.
        "distribucion": list(reporte.distribucion),
        "tamano_px": list(reporte.tamano_px),
        "trabajo": actualizado.como_json(),
    }


def _fotos_de_disenos(a: AjustesDep, trabajo: Trabajo) -> list[tuple[int, Path]]:
    """Las fotos por diseño que estan en disco, con su indice. Vacia si no es post.

    Se listan mirando el disco y no `trabajo.disenos` a secas porque un diseño
    puede haber fallado, o su foto todavia no haberse subido: el ZIP lleva lo que
    hay, y lo que no hay se nota al abrirlo.
    """
    if trabajo.tipo is not TipoTrabajo.POST:
        return []
    fotos: list[tuple[int, Path]] = []
    for indice in range(1, trabajo.disenos + 1):
        ruta = ruta_de_diseno(a, trabajo.id, ClaveDiseno.JPG_VISTA, indice)
        if ruta is not None:
            fotos.append((indice, ruta))
    return fotos


def _llenar_zip(
    zip_: zipfile.ZipFile,
    a: AjustesDep,
    trabajo: Trabajo,
    claves: list[ClaveArchivo],
    por_diseno: list[tuple[int, Path]],
) -> None:
    """Mete en el ZIP las claves del trabajo y las fotos por diseño.

    Un archivo declarado que ya no esta en disco **no es un 500**: el trabajo
    pudo vencer entre el listado y esto. Se omite, y si al final no entro
    ninguno, quien llama responde 404.
    """
    for clave in claves:
        ruta = ruta_de(a, trabajo.id, clave)
        if ruta is None:
            continue
        try:
            zip_.write(
                ruta,
                nombre_de_descarga(trabajo.id, clave, trabajo.nombre_base),
                compress_type=COMPRESION_DE.get(clave, zipfile.ZIP_DEFLATED),
            )
        except OSError:
            log.warning("el archivo %s del trabajo %s no se pudo leer", clave.value, trabajo.id)

    for indice, ruta in por_diseno:
        try:
            zip_.write(
                ruta,
                _nombre_de_diseno_para_bajar(trabajo, ClaveDiseno.JPG_VISTA, indice),
                # Ya es un JPEG: comprimirlo otra vez cuesta CPU y no baja nada.
                # Mismo criterio que `COMPRESION_DE`.
                compress_type=zipfile.ZIP_STORED,
            )
        except OSError:
            log.warning("la foto del diseño %d del trabajo %s no se leyo", indice, trabajo.id)
