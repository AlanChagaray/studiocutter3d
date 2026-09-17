"""F1 — el Convertidor. Dos puertas de entrada que no se cruzan.

Convierte **imagenes** a JPG o SVG, y **mallas** entre `.3mf` y `.stl`. El
destino lo elige el usuario por archivo: la pantalla permite mandar toda la cola
al mismo formato o mezclarlos, porque cada destino sirve para algo distinto. El
JPG alimenta la correccion de lineas; el SVG es lo unico que entra al cortante;
y el par 3MF/STL existe porque cada slicer y cada impresora pide el suyo.

## Las dos mitades no se cruzan, y esa es la regla

Una imagen sale como imagen y una malla sale como malla. No hay pasarela: una
malla a JPG seria una foto de la pieza —eso es F4, `/post`, que la rinde con el
mismo estudio de luces que el cortante— y una imagen a 3MF seria construir
geometria, que es F3, con sus parametros y su reporte de fidelidad. El par se
valida contra `destinos_de` y lo que no corresponde se rechaza nombrando la
pantalla que si lo hace. Ver el docstring de `archivos.destinos_de`.

## Lo de imagen va en linea; lo de malla, a un proceso hijo

Convertir con Pillow tarda milisegundos y mandarlo a un hijo que tarda un
segundo en arrancar seria pagar mil veces el costo de lo que se quiere hacer.
Vectorizar cuesta mas, pero sigue siendo mucho menos que el arranque en frio, y
el handler corre en el threadpool: no bloquea el loop.

⚠ **La malla no puede seguir ese camino, y no porque tarde** — tarda menos que
vectorizar. `cutter3d.malla` importa trimesh, y trimesh adentro de uvicorn son
~1200 modulos y ~89 MB en un proceso que no construye un solo poligono:
exactamente lo que el ciclo 6 saco de ahi. Va a un hijo, y de paso hereda el
techo de RAM y el timeout, que es lo que un STL de 20 MB de un desconocido
justifica por si solo. Lo unico que este modulo importa de mallas es
`paquete3mf`, que cuesta `zipfile` — la misma nota que en `routers/post.py`.

Se sube un archivo por pedido: asi cada uno tiene su propio resultado, su
propio formato de destino y su propio error, y uno que falla no arrastra a los
demas.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, UploadFile

from cutter3d import raster, vector
from cutter3d.errors import Cutter3DError, MallaIlegible

# ⚠ `paquete3mf` y no `cutter3d.malla`: ver la nota del docstring del modulo.
from cutter3d.paquete3mf import confirmar_3mf

from ..almacen import AlmacenTrabajos, EstadoTrabajo, TipoTrabajo, Trabajo
from ..archivos import (
    CLAVE_SALIDA,
    DESTINOS_MALLA,
    FORMATO_DE_MALLA,
    FORMATOS_CONVERSOR,
    FORMATOS_MALLA,
    LIMITE_MALLA_BYTES,
    NOMBRE_DE,
    Formato,
    FormatoSalida,
    Subida,
    destinos_de,
    dir_de_trabajo,
    guardar_subida,
)
from ..config import Ajustes
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import ErrorApi, como_dict, traducir
from ..tareas import ejecutar_malla
from ..trabajos import lanzar

router = APIRouter(prefix="/api/conversor", tags=["conversor"])

#: A donde mandar a quien pidio un cruce imposible. El mensaje nombra la
#: pantalla que SI hace eso: un 422 que solo dice "no se puede" deja al usuario
#: creyendo que la app no sabe hacerlo, cuando sabe y es otro boton.
_FUERA_DE_ALCANCE = {
    True: (
        "Un archivo 3D no se convierte a imagen aca. Para sacarle la foto, usa la pantalla Post."
    ),
    False: (
        "Una imagen no se convierte a un archivo 3D aca. Para construir la pieza, "
        "usa la pantalla Cortante."
    ),
}


def _convertir_imagen(subida: Subida, destino: FormatoSalida, salida: Path) -> None:
    """Aplica la conversion que corresponda al par (origen, destino)."""
    if destino is FormatoSalida.JPG:
        raster.convertir_a_jpg(subida.ruta, salida)
        return
    if subida.formato is Formato.SVG:
        # Ya es vectorial: revectorizarlo solo agregaria error. Se copia tal
        # cual, que ademas es lo unico honesto — el archivo no se toco.
        shutil.copyfile(subida.ruta, salida)
        return
    vector.a_svg(subida.ruta, salida)


def _reduccion(salida: Path, formato: FormatoSalida) -> dict[str, object]:
    """Declara a que tamaño quedo el JPG, si el presupuesto de memoria lo redujo.

    El conversor puede recibir una foto de 61 MP y devolver un JPG de 16: sin
    esto, la reduccion seria silenciosa, que es justo lo que este proyecto no
    hace con ninguna modificacion de la imagen. Se lee del archivo ya escrito
    —`Image.open` es lazy, asi que es leer el header y nada mas— en vez de
    cambiar la firma del motor.

    Solo aplica al JPG: un SVG no tiene un tamaño en pixeles que declarar.
    """
    if formato is not FormatoSalida.JPG:
        return {}
    tamano = raster.tamano_de(salida)
    if tamano is None:  # pragma: no cover — el archivo lo acabamos de escribir
        return {}
    return {"tamano_salida": list(tamano), "presupuesto_px": raster.MAX_PIXELES_TRABAJO}


@router.post("")
def convertir(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    archivo: UploadFile,
    formato: Annotated[FormatoSalida, Form()] = FormatoSalida.JPG,
) -> dict[str, object]:
    """Imagen a jpg o svg, o malla a 3mf o stl, segun lo que entre y lo que pidan."""
    trabajo = almacen.crear(usuario, TipoTrabajo.CONVERSOR)
    dir_trabajo = dir_de_trabajo(a, trabajo.id, crear=True)

    subida = guardar_subida(
        archivo.file,
        dir_trabajo,
        permitidos=FORMATOS_CONVERSOR,
        # Las mallas tienen su propio tope, mas ceñido que el general y el mismo
        # de F4: 20 MB de STL binario son 400.000 triangulos, dos ordenes de
        # magnitud por encima de cualquier cortante real.
        limite_bytes=LIMITE_MALLA_BYTES if formato in DESTINOS_MALLA else a.tamano_maximo_bytes,
        # El nombre del cliente entra SOLO para que la descarga se llame
        # como el archivo original. `sanear_nombre_base` lo reduce a
        # [A-Za-z0-9._-] y nunca toca una ruta: el archivo en disco sigue
        # siendo `entrada.<ext>`.
        nombre_cliente=archivo.filename,
    )
    if formato not in destinos_de(subida.formato):
        raise ErrorApi(
            "conversion_no_aplica",
            _FUERA_DE_ALCANCE[subida.formato in FORMATOS_MALLA],
            estado=422,
            detalle={"origen": subida.formato.value, "destino": formato.value},
        )

    clave = CLAVE_SALIDA[formato]
    nombre = NOMBRE_DE[clave]
    almacen.actualizar(trabajo.id, nombre_base=subida.nombre_base)

    if subida.formato in FORMATOS_MALLA:
        return _malla(
            almacen=almacen,
            a=a,
            trabajo=trabajo,
            subida=subida,
            formato=formato,
            salida=dir_trabajo / nombre,
        )

    try:
        _convertir_imagen(subida, formato, dir_trabajo / nombre)
    except Cutter3DError as exc:
        almacen.actualizar(
            trabajo.id, estado=EstadoTrabajo.ERROR, etapa="error", error=como_dict(exc)
        )
        raise traducir(exc) from exc

    almacen.actualizar(
        trabajo.id,
        estado=EstadoTrabajo.LISTO,
        etapa="listo",
        archivos={clave.value: nombre},
        reporte={
            "formato_original": subida.formato.value,
            "formato_destino": formato.value,
            "bytes_originales": subida.bytes_escritos,
            **_reduccion(dir_trabajo / nombre, formato),
        },
    )
    return trabajo.como_json()


def _malla(
    *,
    almacen: AlmacenTrabajos,
    a: Ajustes,
    trabajo: Trabajo,
    subida: Subida,
    formato: FormatoSalida,
    salida: Path,
) -> dict[str, object]:
    """El camino de las mallas: confirmar, y copiar o lanzar el hijo.

    La confirmacion del 3MF va **antes** de decidir nada: su firma `PK\\x03\\x04`
    la comparten docx, xlsx, jar y epub, y lo que lo distingue vive en el central
    directory, al final del archivo, donde la deteccion por magic bytes no llega.
    Chequearlo aca hace que un docx se vaya con un 422 inmediato en vez de gastar
    uno de los tres slots de proceso — igual que en `routers/post.py`.
    """
    if subida.formato is Formato.TRES_MF:
        try:
            confirmar_3mf(subida.ruta)
        except MallaIlegible as exc:
            almacen.actualizar(
                trabajo.id, estado=EstadoTrabajo.ERROR, etapa="error", error=como_dict(exc)
            )
            raise traducir(exc) from exc

    clave = CLAVE_SALIDA[formato]
    if FORMATO_DE_MALLA[formato] is subida.formato:
        # Ya esta en el formato que pidieron. Se copia tal cual, que es lo unico
        # honesto —el archivo no se toco— y ademas lo unico que no puede alterar
        # ni el diseño ni las medidas. Es la misma regla del SVG pedido como SVG.
        #
        # Pasa mas de lo que parece: el destino lo propone el navegador mirando
        # la extension y el formato real lo deciden los bytes, asi que un `.stl`
        # que en verdad era un 3MF llega aca como un par identico.
        shutil.copyfile(subida.ruta, salida)
        almacen.actualizar(
            trabajo.id,
            estado=EstadoTrabajo.LISTO,
            etapa="listo",
            archivos={clave.value: salida.name},
            reporte={
                "formato_original": subida.formato.value,
                "formato_destino": formato.value,
                "bytes_originales": subida.bytes_escritos,
                "sin_conversion": True,
            },
        )
        return trabajo.como_json()

    lanzar(
        almacen=almacen,
        a=a,
        trabajo=trabajo,
        objetivo=ejecutar_malla,
        # `salida.name` y no la ruta: sale de `NOMBRE_DE`, y el hijo lo pega al
        # directorio de trabajo que ya recibe. El cliente sigue sin nombrar nada.
        argumentos=(str(salida.parent), str(subida.ruta), salida.name, clave.value),
    )
    return trabajo.como_json()
