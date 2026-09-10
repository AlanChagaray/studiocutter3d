"""Vectorizacion jpg -> svg con vtracer.

Se usa vtracer y no potrace: `pypotrace` **no tiene wheel de Windows** (se
verifico contra PyPI) y compilarlo exige el binding en C. vtracer es un binding
de Rust con wheel `cp313` publicado.

⚠ `hierarchical="cutout"` es **obligatorio**. El default de vtracer es
`"stacked"`, que apila formas en vez de generar huecos: un marcador trazado asi
saldria con todos los huecos internos rellenos, y el motor lo reproduciria fiel
— fiel a un dibujo equivocado.

`filter_speckle` va bajo (2) a proposito: filtrar mas limpia el ruido de
compresion pero tambien se come detalle fino del trazo, que es justo lo que el
contrato de fidelidad no quiere perder. Lo mejor sigue siendo entrar con PNG
binario a esta etapa; el JPG es la concesion de comodidad.
"""

from __future__ import annotations

from pathlib import Path

import vtracer

from .errors import ImagenInvalida

MODO_COLOR = "binary"
JERARQUIA = "cutout"
MODO_CURVA = "spline"
FILTRO_MOTAS = 2
PRECISION_PATH = 8


def a_svg(entrada: Path, salida: Path) -> Path:
    """Vectoriza una imagen raster a SVG de paths rellenos, sin stroke."""
    if not entrada.is_file():
        raise ImagenInvalida(str(entrada), "el archivo no existe")
    if salida.suffix.lower() != ".svg":
        raise ImagenInvalida(str(salida), "la salida de la vectorizacion va en .svg")
    salida.parent.mkdir(parents=True, exist_ok=True)
    try:
        vtracer.convert_image_to_svg_py(
            str(entrada),
            str(salida),
            colormode=MODO_COLOR,
            hierarchical=JERARQUIA,
            mode=MODO_CURVA,
            filter_speckle=FILTRO_MOTAS,
            path_precision=PRECISION_PATH,
        )
    except Exception as exc:
        raise ImagenInvalida(str(entrada), f"no se pudo vectorizar: {exc}") from exc
    if not salida.is_file():
        raise ImagenInvalida(str(entrada), "la vectorizacion no produjo ningun archivo")
    return salida
