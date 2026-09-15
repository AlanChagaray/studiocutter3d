"""F1 — el Convertidor. El unico modulo que corre en linea.

Convierte a **JPG o SVG**, y el destino lo elige el usuario por archivo: la
pantalla permite mandar toda la cola al mismo formato o mezclarlos, porque los
dos destinos sirven para cosas distintas. El JPG alimenta la correccion de
lineas; el SVG es lo unico que entra al cortante.

Va sincrono y no por trabajo a proposito: convertir con Pillow tarda
milisegundos, y mandarlo a un proceso hijo que tarda un segundo en arrancar
seria pagar mil veces el costo de lo que se quiere hacer. Vectorizar cuesta
mas que eso, pero sigue siendo mucho menos que el arranque en frio del hijo, y
el handler corre en el threadpool: no bloquea el loop.

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
from cutter3d.errors import Cutter3DError

from ..almacen import EstadoTrabajo, TipoTrabajo
from ..archivos import (
    CLAVE_SALIDA,
    FORMATOS_CONVERSOR,
    NOMBRE_DE,
    Formato,
    FormatoSalida,
    Subida,
    dir_de_trabajo,
    guardar_subida,
)
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import como_dict, traducir

router = APIRouter(prefix="/api/conversor", tags=["conversor"])


def _convertir(subida: Subida, destino: FormatoSalida, salida: Path) -> None:
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
    """png / jfif / webp / jpg / svg a jpg o svg, en el mismo pedido."""
    trabajo = almacen.crear(usuario, TipoTrabajo.CONVERSOR)
    dir_trabajo = dir_de_trabajo(a, trabajo.id, crear=True)

    subida = guardar_subida(
        archivo.file,
        dir_trabajo,
        permitidos=FORMATOS_CONVERSOR,
        limite_bytes=a.tamano_maximo_bytes,
        # El nombre del cliente entra SOLO para que la descarga se llame
        # como el archivo original. `sanear_nombre_base` lo reduce a
        # [A-Za-z0-9._-] y nunca toca una ruta: el archivo en disco sigue
        # siendo `entrada.<ext>`.
        nombre_cliente=archivo.filename,
    )

    clave = CLAVE_SALIDA[formato]
    nombre = NOMBRE_DE[clave]
    try:
        _convertir(subida, formato, dir_trabajo / nombre)
    except Cutter3DError as exc:
        almacen.actualizar(
            trabajo.id, estado=EstadoTrabajo.ERROR, etapa="error", error=como_dict(exc)
        )
        raise traducir(exc) from exc

    almacen.actualizar(
        trabajo.id,
        estado=EstadoTrabajo.LISTO,
        etapa="listo",
        nombre_base=subida.nombre_base,
        archivos={clave.value: nombre},
        reporte={
            "formato_original": subida.formato.value,
            "formato_destino": formato.value,
            "bytes_originales": subida.bytes_escritos,
            **_reduccion(dir_trabajo / nombre, formato),
        },
    )
    return trabajo.como_json()
