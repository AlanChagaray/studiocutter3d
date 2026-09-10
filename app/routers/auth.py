"""Login, logout y sesion.

Tres decisiones de seguridad que no son negociables y por eso van comentadas:

1. **El mensaje de error es uno solo.** Usuario inexistente y contraseña mala
   devuelven exactamente el mismo texto. Sumado al hash señuelo de
   `seguridad.py`, el login no permite enumerar usuarios ni por respuesta ni
   por tiempo.
2. **La sesion se regenera al entrar.** `session.clear()` antes de escribir el
   usuario descarta cualquier cookie que el visitante trajera de antes, que es
   lo que cierra la fijacion de sesion.
3. **El destino post-login se valida.** Aceptar `?destino=` sin filtrar es un
   open redirect: la pagina de login del sitio real mandando a otro dominio es
   justo lo que necesita un phishing. Solo pasan rutas internas.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..config import ErrorDeConfiguracion
from ..dependencias import CLAVE_USUARIO, AjustesDep, plantillas, usuario_actual
from ..seguridad import verificar_credenciales

router = APIRouter(tags=["auth"])

MENSAJE_CREDENCIALES = "Usuario o contraseña incorrectos."
"""El MISMO mensaje para los dos casos. Ver el punto 1 del docstring."""

MENSAJE_SIN_ALTA = (
    "No hay usuarios dados de alta en el servidor. Crea las credenciales antes de iniciar sesion."
)

RUTA_POR_DEFECTO = "/conversor"


def _destino_seguro(crudo: str | None) -> str:
    """Deja pasar solo rutas internas absolutas.

    Rechaza `//evil.com` (URL protocolo-relativa, que el navegador resuelve
    como externa) y cualquier cosa con `:` antes de la primera `/`, que seria
    un esquema.
    """
    if not crudo or not crudo.startswith("/") or crudo.startswith("//"):
        return RUTA_POR_DEFECTO
    if "\\" in crudo or crudo.startswith("/login"):
        return RUTA_POR_DEFECTO
    return crudo


def _render_login(
    request: Request,
    *,
    destino: str,
    error: str | None = None,
    estado: int = status.HTTP_200_OK,
) -> Response:
    return plantillas.TemplateResponse(
        request,
        "login.html",
        {"destino": destino, "error": error, "usuario_previo": ""},
        status_code=estado,
    )


@router.get("/login", response_class=HTMLResponse)
def pagina_login(request: Request, destino: str = "") -> Response:
    """Formulario de login. Con sesion abierta no tiene sentido: redirige."""
    if usuario_actual(request):
        return RedirectResponse(_destino_seguro(destino), status_code=303)
    return _render_login(request, destino=_destino_seguro(destino))


@router.post("/login")
def iniciar_sesion(
    request: Request,
    a: AjustesDep,
    usuario: Annotated[str, Form()],
    clave: Annotated[str, Form()],
    destino: Annotated[str, Form()] = "",
) -> Response:
    ruta = _destino_seguro(destino)
    try:
        ok = verificar_credenciales(usuario.strip(), clave, a)
    except ErrorDeConfiguracion:
        # Falta `credenciales.json`. Es un problema del servidor, no del que
        # intenta entrar: decirlo evita media hora de probar contraseñas.
        return _render_login(
            request, destino=ruta, error=MENSAJE_SIN_ALTA, estado=status.HTTP_401_UNAUTHORIZED
        )

    if not ok:
        return _render_login(
            request,
            destino=ruta,
            error=MENSAJE_CREDENCIALES,
            estado=status.HTTP_401_UNAUTHORIZED,
        )

    request.session.clear()  # regeneracion: nada de la sesion anterior sobrevive
    request.session[CLAVE_USUARIO] = usuario.strip()
    return RedirectResponse(ruta, status_code=303)


@router.post("/logout")
def cerrar_sesion(request: Request) -> RedirectResponse:
    """POST y no GET: un `<img src="/logout">` ajeno no puede desloguear."""
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
