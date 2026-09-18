"""studioCutter3D — motor de geometria.

Convierte un SVG de line art en un cortante de galletitas (y opcionalmente su
marcador) listo para imprimir, y **prueba numericamente** que no altero el dibujo.

El contrato de geometria y la regla de fidelidad viven en `prompt_cortante.md`,
en la raiz del repo. Los numeros de este modulo salen de ahi: no se reinterpretan.

`generar()` es la entrada publica — la que va a llamar la capa web del ciclo 2
desde un `ProcessPoolExecutor`. Es una funcion pura respecto del proceso: recibe
rutas y parametros, escribe archivos y devuelve un resultado tipado. No sabe nada
de HTTP.

## Por que los imports del motor estan adentro de `generar()`

Importar este paquete **por cualquier motivo** ejecuta su `__init__`, y hasta el
ciclo 6 eso cargaba trimesh, manifold3d, scipy, skimage y shapely enteros. El
costo no lo pagaba quien usa la geometria sino todo el mundo: `app/errores.py`
hace `from cutter3d.errors import ...` —un modulo que importa **solo**
`__future__`— y con eso arrastraba **1200 modulos y ~89 MB** a cada proceso de
la capa web, que no construye un solo poligono. Medido: el proceso de uvicorn
quedaba en 128,7 MB y `app.tareas` en 106,7 MB *antes de ejecutar una linea*.

Con `spawn` (que `app/trabajos.py` fuerza) no hay copy-on-write, asi que padre e
hijo **suman**: en una instancia de 512 MB ese costo fijo es lo que no deja lugar
al pico de la geometria. Por eso ahora solo son eager los dos modulos que no
cuestan nada —`errors` (stdlib pura) y `params` (`math` + `dataclasses`)— y el
resto entra cuando `generar()` corre de verdad.

Los nombres re-exportados que viven en los modulos caros (`Salidas`,
`ReporteFidelidad`, `render_texto`) siguen disponibles como
`cutter3d.<nombre>` gracias al `__getattr__` de modulo (PEP 562): quien los pide
paga el import, quien no, no.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import (
    BooleanaFallida,
    Cutter3DError,
    ImagenInvalida,
    MallaNoManifold,
    NoConvergeError,
    ParametroFueraDeRango,
    SvgInvalido,
)
from .params import AjustesMotor, CutterParams

if TYPE_CHECKING:  # para mypy y para las anotaciones; en runtime no se importa
    from .export import Salidas
    from .verify import ReporteFidelidad, render_texto

__version__ = "1.0.1"

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


_PEREZOSOS = {
    "ReporteFidelidad": "verify",
    "Salidas": "export",
    "render_texto": "verify",
}
"""Nombre re-exportado -> submodulo caro donde vive. Ver el docstring del modulo."""


def __getattr__(nombre: str) -> Any:
    """Resuelve los re-exportados de los modulos caros la primera vez que se piden.

    Tiene que levantar `AttributeError` —y no otra cosa— ante un nombre
    desconocido: es lo que deja que `from cutter3d import raster` siga
    encontrando el submodulo por el camino normal del sistema de imports.
    """
    modulo = _PEREZOSOS.get(nombre)
    if modulo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
    valor = getattr(importlib.import_module(f".{modulo}", __name__), nombre)
    globals()[nombre] = valor  # queda cacheado: el __getattr__ no vuelve a correr
    return valor


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

    @property
    def rutas_3mf_objeto(self) -> tuple[Path, ...]:
        return self.salidas.rutas_3mf_objeto


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
    # Los imports del motor van aca y no arriba: ver el docstring del modulo.
    from .export import exportar  # noqa: PLC0415
    from .geometry import (  # noqa: PLC0415
        construir_cortador_2d,
        construir_marcador_2d,
        construir_silueta_sola,
    )
    from .measure import medir_huecos  # noqa: PLC0415
    from .solids import construir_escena  # noqa: PLC0415
    from .svg_io import cargar_svg  # noqa: PLC0415
    from .verify import exigir_manifold, verificar  # noqa: PLC0415

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

    # `arte` esta muerto desde aca: lo que sigue consume `silueta` y `marcador`,
    # que ya tienen lo suyo. Soltarlo deja que el recolector se lleve el
    # MultiPolygon del dibujo entero antes del pico de las mallas.
    del arte

    cortador = construir_cortador_2d(silueta, p, a)
    escena = construir_escena(marcador, cortador, p)
    salidas = exportar(escena, salida, con_stl=con_stl)

    # `verificar` NO recibe la escena: relee el .3mf del disco a proposito (es su
    # regla anti-circularidad, ver `verify.py`). Soltar la original antes de esa
    # lectura no toca la garantia y evita el unico momento del pipeline en el que
    # habria dos juegos completos de mallas vivos a la vez.
    del escena

    reporte = verificar(salidas.ruta_3mf, marcador, cortador, silueta, p=p, a=a, huecos=huecos)
    if exigir_solido:
        exigir_manifold(reporte)
    return Resultado(salidas=salidas, reporte=reporte)
