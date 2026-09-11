"""Consulta de trabajos y descarga de archivos.

Es el unico lugar de la app que devuelve bytes del disco, asi que concentra
las tres defensas y no hay una cuarta escondida en otro router:

1. **El id se valida como UUID.** Cualquier cosa con separadores de ruta ni
   siquiera llega a tocar el filesystem.
2. **La clave viene de un enum cerrado.** FastAPI rechaza con 422 todo lo que
   no este en `ClaveArchivo`, antes de entrar al handler. El cliente nombra
   una clave, nunca un archivo.
3. **El propietario se chequea en el almacen.** `obtener()` devuelve `None`
   tanto para un id inexistente como para uno ajeno: desde afuera son
   indistinguibles, asi que no se puede sondear que ids existen.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from ..almacen import Trabajo
from ..archivos import MEDIO_DE, ClaveArchivo, nombre_de_descarga, ruta_de
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import ErrorApi
from ..trabajos import cancelar

router = APIRouter(prefix="/api/trabajos", tags=["trabajos"])


def _exigir_trabajo(almacen: AlmacenDep, id_: str, usuario: str) -> Trabajo:
    trabajo = almacen.obtener(id_, usuario)
    if trabajo is None:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    return trabajo


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


@router.delete("/{id_}")
def cancelar_trabajo(id_: str, usuario: UsuarioRequerido, almacen: AlmacenDep) -> JSONResponse:
    """Mata el proceso hijo de un trabajo en curso."""
    _exigir_trabajo(almacen, id_, usuario)
    return JSONResponse({"cancelado": cancelar(almacen, id_)})
