"""Dependencias de FastAPI: ajustes, almacen, plantillas y autorizacion.

`exigir_login` es la unica puerta de entrada a todo lo protegido. Que la
autorizacion viva en una sola dependencia —y no repartida en cada handler— es
lo que hace que agregar una ruta sin protegerla sea visible: si no la declara,
no la tiene.

Las paginas HTML redirigen a `/login`; los endpoints de la API responden 401
con la forma de error uniforme. La distincion se hace por el prefijo de la
ruta, no por el header `Accept`, que el navegador manda de forma inconsistente.

El objeto `plantillas` vive aca y no en `main.py` a proposito: los routers lo
necesitan y `main` los importa a ellos, asi que ponerlo alla cerraria el ciclo
de imports. Este modulo es el registro de objetos compartidos de la app.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from .almacen import AlmacenEnMemoria, AlmacenTrabajos
from .config import Ajustes, cargar_ajustes
from .errores import ErrorApi, RedireccionALogin

_ajustes = cargar_ajustes()
_almacen: AlmacenTrabajos = AlmacenEnMemoria()

DIR_PLANTILLAS = _ajustes.raiz / "app" / "templates"
plantillas = Jinja2Templates(directory=str(DIR_PLANTILLAS))

plantillas.env.globals["version"] = __version__
"""La version desplegada, visible en toda plantilla sin pasarla por cada handler.

Es la misma `__version__` que `main.py` le da a FastAPI y la que el workflow
`ci-release.yml` bumpea en cada merge a `main` (`scripts/version.py`): una sola
fuente. Va como global de Jinja y no en el contexto de `_pantalla` por lo mismo
que `modulos` vive en una constante: una pantalla nueva no tiene que acordarse
de pasarla. Se muestra debajo del logo (`macros.marca`) y SOLO con sesion — el
login no la lleva, por la misma razon que `/salud` no dice la version: a quien
no entro no se le cuenta que corre."""

CLAVE_USUARIO = "usuario"
"""Lo unico que se guarda en la sesion.

El encadenado entre pantallas NO va aca: viaja como `?origen=<id>` en la URL.
Guardarlo en la sesion haria que dos pestañas trabajando sobre archivos
distintos se pisen entre si, y no aporta seguridad — el id igual se valida
contra el propietario en el almacen. El tema claro/oscuro tampoco: eso es
`localStorage`, que no viaja en cada pedido."""


def obtener_ajustes() -> Ajustes:
    return _ajustes


def obtener_almacen() -> AlmacenTrabajos:
    return _almacen


def usuario_actual(request: Request) -> str | None:
    """El usuario de la sesion, o None. No exige nada: solo informa."""
    valor = request.session.get(CLAVE_USUARIO)
    return str(valor) if valor else None


def exigir_login(request: Request) -> str:
    """Devuelve el usuario o corta el pedido. Es la autorizacion de la app."""
    usuario = usuario_actual(request)
    if usuario:
        return usuario
    if request.url.path.startswith("/api/"):
        raise ErrorApi("no_autenticado", "Hay que iniciar sesion.", estado=401)
    raise RedireccionALogin(str(request.url.path))


def redirigir_a_login(destino: str | None = None) -> RedirectResponse:
    ruta = "/login" if not destino else f"/login?destino={destino}"
    return RedirectResponse(ruta, status_code=303)


UsuarioRequerido = Annotated[str, Depends(exigir_login)]
AjustesDep = Annotated[Ajustes, Depends(obtener_ajustes)]
AlmacenDep = Annotated[AlmacenTrabajos, Depends(obtener_almacen)]
