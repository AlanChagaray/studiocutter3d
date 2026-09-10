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
        archivos={clave.value: nombre},
        reporte={
            "formato_original": subida.formato.value,
            "formato_destino": formato.value,
            "bytes_originales": subida.bytes_escritos,
        },
    )
    return trabajo.como_json()
