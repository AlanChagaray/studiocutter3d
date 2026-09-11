"""Las pantallas. Solo renderizan: todo el trabajo pasa por `/api/`.

Cada una declara `UsuarioRequerido`, que es lo que las protege. Una pantalla
nueva que se olvide de declararlo queda abierta, y eso se ve leyendo la firma
—por eso la autorizacion es una dependencia y no una linea adentro del cuerpo.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..dependencias import UsuarioRequerido, plantillas
from .cortante import CAMPOS, COLORES, LIMITE_MAX_MM

router = APIRouter(tags=["paginas"])


@dataclass(frozen=True)
class Modulo:
    """Una entrada del menu de modulos."""

    clave: str
    """Coincide con la variable `pagina` de la plantilla y con el nombre del
    icono en `macros.html`. Un solo valor para las tres cosas: si se separaran,
    habria tres lugares donde equivocarse."""

    ruta: str
    etiqueta: str


MODULOS: tuple[Modulo, ...] = (
    Modulo("conversor", "/conversor", "Convertir"),
    Modulo("lineas", "/lineas", "Correcto"),
    Modulo("cortante", "/cortante", "Cortante"),
)
"""El menu, escrito UNA sola vez.

Vive aca y no en la plantilla por lo mismo que `CAMPOS` y `COLORES`: hasta el
ciclo anterior la lista estaba duplicada en `base.html` —una vez para el sidebar
y otra para la barra inferior de mobile— con etiquetas distintas en cada copia
("Convertidor" contra "Convertir"). Agregar un modulo eran dos ediciones y nadie
lo recordaba. Un test puede exigir esta lista sin leer HTML.

La `clave` sigue siendo `lineas` aunque el modulo se llame **Correcto**: es la
llave interna —el icono en `macros.html`, la variable `pagina` del template, la
ruta y el prefijo `/api/lineas`—, y renombrarla no cambiaria nada de lo que el
usuario ve. El nombre visible es la `etiqueta`, y vive en esta misma linea.
"""


def _pantalla(request: Request, plantilla: str, usuario: str, **extra: object) -> Response:
    return plantillas.TemplateResponse(
        request, plantilla, {"usuario": usuario, "modulos": MODULOS, **extra}
    )


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
