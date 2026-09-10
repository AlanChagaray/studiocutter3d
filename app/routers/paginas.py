"""Las pantallas. Solo renderizan: todo el trabajo pasa por `/api/`.

Cada una declara `UsuarioRequerido`, que es lo que las protege. Una pantalla
nueva que se olvide de declararlo queda abierta, y eso se ve leyendo la firma
—por eso la autorizacion es una dependencia y no una linea adentro del cuerpo.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..dependencias import UsuarioRequerido, plantillas
from .cortante import CAMPOS, COLORES, LIMITE_MAX_MM

router = APIRouter(tags=["paginas"])


def _pantalla(request: Request, plantilla: str, usuario: str, **extra: object) -> Response:
    return plantillas.TemplateResponse(request, plantilla, {"usuario": usuario, **extra})


@router.get("/")
def inicio(usuario: UsuarioRequerido) -> RedirectResponse:
    del usuario  # solo se pide para exigir la sesion
    return RedirectResponse("/conversor", status_code=307)


@router.get("/conversor", response_class=HTMLResponse)
def pagina_conversor(request: Request, usuario: UsuarioRequerido) -> Response:
    return _pantalla(request, "conversor.html", usuario, pagina="conversor")


@router.get("/lineas", response_class=HTMLResponse)
def pagina_lineas(request: Request, usuario: UsuarioRequerido) -> Response:
    return _pantalla(request, "lineas.html", usuario, pagina="lineas")


@router.get("/cortante", response_class=HTMLResponse)
def pagina_cortante(request: Request, usuario: UsuarioRequerido) -> Response:
    return _pantalla(
        request,
        "cortante.html",
        usuario,
        pagina="cortante",
        campos=CAMPOS,
        colores=COLORES,
        limite_max_mm=LIMITE_MAX_MM,
    )
