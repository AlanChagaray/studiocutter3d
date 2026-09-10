"""studioCutter3D — motor de geometria.

Convierte un SVG de line art en un cortante de galletitas (y opcionalmente su
marcador) listo para imprimir, y **prueba numericamente** que no altero el dibujo.

El contrato de geometria y la regla de fidelidad viven en `prompt_cortante.md`,
en la raiz del repo. Los numeros de este modulo salen de ahi: no se reinterpretan.

`generar()` es la entrada publica — la que va a llamar la capa web del ciclo 2
desde un `ProcessPoolExecutor`. Es una funcion pura respecto del proceso: recibe
rutas y parametros, escribe archivos y devuelve un resultado tipado. No sabe nada
de HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import (
    BooleanaFallida,
    Cutter3DError,
    ImagenInvalida,
    MallaNoManifold,
    NoConvergeError,
    ParametroFueraDeRango,
    SvgInvalido,
)
from .export import Salidas, exportar
from .geometry import construir_cortador_2d, construir_marcador_2d, construir_silueta_sola
from .measure import medir_huecos
from .params import AjustesMotor, CutterParams
from .solids import construir_escena
from .svg_io import cargar_svg
from .verify import ReporteFidelidad, exigir_manifold, render_texto, verificar

__version__ = "0.1.0"

__all__ = [
    "AjustesMotor",
    "BooleanaFallida",
    "Cutter3DError",
    "CutterParams",
    "ImagenInvalida",
    "MallaNoManifold",
    "Modo",
    "NoConvergeError",
    "ParametroFueraDeRango",
    "ReporteFidelidad",
    "Resultado",
    "Salidas",
    "SvgInvalido",
    "generar",
    "render_texto",
]


class Modo(StrEnum):
    CORTANTE = "cortante"
    CORTANTE_MARCADOR = "cortante+marcador"


@dataclass(frozen=True)
class Resultado:
    salidas: Salidas
    reporte: ReporteFidelidad

    @property
    def ruta_3mf(self) -> Path:
        return self.salidas.ruta_3mf

    @property
    def ruta_glb(self) -> Path:
        return self.salidas.ruta_glb

    @property
    def rutas_stl(self) -> tuple[Path, ...]:
        return self.salidas.rutas_stl


def generar(
    svg: Path,
    modo: Modo,
    salida: Path,
    *,
    params: CutterParams | None = None,
    ajustes: AjustesMotor | None = None,
    con_stl: bool = False,
    exigir_solido: bool = True,
) -> Resultado:
    """Corre el pipeline completo y devuelve las rutas escritas mas el reporte.

    Con `exigir_solido=True` (default) una malla que no cierre levanta
    `MallaNoManifold` **despues** de escribir el archivo, para que el archivo
    quede disponible para inspeccion pero nadie lo tome por valido.
    """
    p = params or CutterParams()
    a = ajustes or AjustesMotor()

    arte = cargar_svg(svg, p, a)
    if modo is Modo.CORTANTE_MARCADOR:
        marcador = construir_marcador_2d(arte, p, a)
        silueta = marcador.silueta
        huecos = medir_huecos(marcador.arte_final, silueta, a)
    else:
        marcador = None
        silueta = construir_silueta_sola(arte, p, a)
        huecos = None

    cortador = construir_cortador_2d(silueta, p, a)
    escena = construir_escena(marcador, cortador, p)
    salidas = exportar(escena, salida, con_stl=con_stl)
    reporte = verificar(salidas.ruta_3mf, marcador, cortador, silueta, p=p, a=a, huecos=huecos)
    if exigir_solido:
        exigir_manifold(reporte)
    return Resultado(salidas=salidas, reporte=reporte)
