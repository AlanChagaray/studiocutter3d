"""F2 — correccion de lineas, por trabajo.

Va a un proceso hijo y no en linea porque el eje medial sobre una imagen
grande no es instantaneo y porque asi hereda el timeout: una imagen patologica
se corta sola en vez de dejar colgado al servidor.

**El contorneado de macizos viene encendido**, por decision explicita del
usuario, y el resultado siempre declara cuantas zonas se tocaron y que area.
Esa declaracion no es decorativa: es la unica modificacion del arte que el
producto permite, asi que tiene que quedar a la vista de quien la pidio.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from ..almacen import TipoTrabajo
from ..archivos import FORMATOS_LINEAS, dir_de_trabajo, resolver_entrada
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..tareas import ejecutar_lineas
from ..trabajos import lanzar

router = APIRouter(prefix="/api/lineas", tags=["lineas"])


@router.post("")
def corregir(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    *,
    archivo: Annotated[UploadFile | None, File()] = None,
    origen: Annotated[str | None, Form()] = None,
    contornear_macizos: Annotated[bool, Form()] = True,
) -> dict[str, object]:
    trabajo = almacen.crear(usuario, TipoTrabajo.LINEAS)
    destino = dir_de_trabajo(a, trabajo.id, crear=True)

    subida = resolver_entrada(
        a,
        almacen,
        flujo=archivo.file if archivo is not None else None,
        origen_id=origen,
        usuario=usuario,
        destino_dir=destino,
        permitidos=FORMATOS_LINEAS,
    )

    lanzar(
        almacen=almacen,
        a=a,
        trabajo=trabajo,
        objetivo=ejecutar_lineas,
        argumentos=(str(destino), str(subida.ruta), contornear_macizos),
    )
    return trabajo.como_json()
