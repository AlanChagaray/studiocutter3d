"""F1 (conversion a JPG) y F2 (correccion de lineas).

**F1** — png, jfif, webp y svg a jpg. El SVG se rasteriza con `resvg-py` (Rust,
sin dependencias del sistema; `cairosvg` necesitaria la DLL de Cairo, que en
Windows no viene). Ojo: pasar un SVG por aca **pierde fidelidad** — si ya tenes
el vector, va derecho al modulo de cortante y no por esta etapa.

**F2** — jpg a blanco y negro puro de lineas. Dos reglas que vienen del pedido:

- La salida contiene **solo 0 y 255**. Cero grises, cero sombreados.
- El **contorneado de zonas macizas esta ENCENDIDO por default**. Es la unica
  etapa del flujo que modifica el arte, y por eso **siempre declara cuantas zonas
  toco y que area**: la modificacion no puede ser invisible. El modulo de cortante
  (F3) nunca hace esto — reproduce fiel lo que reciba.

El umbral de "macizo" se autocalibra con el propio dibujo: se mide su ancho de
trazo mediano `w` y se considera macizo toda region donde entre un disco de radio
`0,75 w`. Un trazo de ancho `w` tiene medio ancho `0,5 w` y no entra; una mancha
si. Asi no hace falta conocer la escala fisica de la imagen.

La salida de F2 se guarda en **PNG y nunca en JPG**: el contrato es explicito en
que el ruido de compresion ensucia el borde y mete dientes en el filo.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
import numpy.typing as npt
import resvg_py
from PIL import Image
from scipy.ndimage import label
from skimage.filters import threshold_otsu
from skimage.morphology import disk, erosion, medial_axis, opening

from .errors import ImagenInvalida

Mascara = npt.NDArray[np.bool_]

EXTENSIONES_RASTER = frozenset({".png", ".jpg", ".jpeg", ".jfif", ".webp"})
EXTENSIONES_VECTOR = frozenset({".svg"})
EXTENSIONES_ENTRADA = EXTENSIONES_RASTER | EXTENSIONES_VECTOR
CALIDAD_JPG = 95
FACTOR_MACIZO = 0.75
"""Radio del disco de apertura, en unidades de ancho de trazo mediano."""


@dataclass(frozen=True)
class ResultadoF2:
    imagen: npt.NDArray[np.uint8]
    """uint8 con exactamente dos valores: 0 (tinta) y 255 (fondo)."""

    umbral_usado: int
    ancho_trazo_px: float
    zonas_contorneadas: int
    area_contorneada_px: int
    contorneado_activo: bool


def _abrir_como_pil(entrada: Path) -> Image.Image:
    sufijo = entrada.suffix.lower()
    if not entrada.is_file():
        raise ImagenInvalida(str(entrada), "el archivo no existe")
    if sufijo not in EXTENSIONES_ENTRADA:
        soportadas = ", ".join(sorted(EXTENSIONES_ENTRADA))
        raise ImagenInvalida(str(entrada), f"formato no soportado. Se aceptan: {soportadas}")
    if sufijo in EXTENSIONES_VECTOR:
        try:
            png = resvg_py.svg_to_bytes(svg_path=str(entrada), background="#ffffff")
        except Exception as exc:
            raise ImagenInvalida(str(entrada), f"no se pudo rasterizar el SVG: {exc}") from exc
        return Image.open(BytesIO(bytes(png)))
    try:
        return Image.open(entrada)
    except Exception as exc:
        raise ImagenInvalida(str(entrada), f"no se pudo abrir: {exc}") from exc


def _aplanar_sobre_blanco(imagen: Image.Image) -> Image.Image:
    """JPEG no tiene canal alfa: lo transparente se compone sobre blanco."""
    if imagen.mode in ("RGBA", "LA") or (imagen.mode == "P" and "transparency" in imagen.info):
        rgba = imagen.convert("RGBA")
        fondo = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(fondo, rgba).convert("RGB")
    return imagen.convert("RGB")


def convertir_a_jpg(entrada: Path, salida: Path, calidad: int = CALIDAD_JPG) -> Path:
    """F1 — png / jfif / webp / svg a jpg."""
    with _abrir_como_pil(entrada) as imagen:
        rgb = _aplanar_sobre_blanco(imagen)
        salida.parent.mkdir(parents=True, exist_ok=True)
        rgb.save(salida, format="JPEG", quality=calidad, subsampling=0)
    return salida


def _ancho_trazo_px(tinta: Mascara) -> float:
    """Ancho mediano del trazo, en pixeles, medido sobre el propio dibujo."""
    ejes, distancia = medial_axis(tinta, return_distance=True)
    valores = 2.0 * np.asarray(distancia)[np.asarray(ejes, dtype=bool)]
    return float(np.median(valores)) if valores.size else 1.0


def _contornear_macizos(tinta: Mascara, ancho_px: float) -> tuple[Mascara, int, int]:
    """Reemplaza cada zona maciza por un anillo del ancho de trazo del dibujo."""
    # `opening` / `erosion` y no sus variantes `binary_*`: estan deprecadas
    # desde skimage 0.26. Sobre entrada booleana hacen morfologia binaria igual.
    radio_apertura = max(1, round(FACTOR_MACIZO * ancho_px))
    macizos = np.asarray(opening(tinta, disk(radio_apertura)), dtype=bool)
    if not macizos.any():
        return tinta, 0, 0
    radio_anillo = max(1, round(ancho_px))
    nucleo = np.asarray(erosion(macizos, disk(radio_anillo)), dtype=bool)
    anillo = macizos & ~nucleo
    _, cantidad = label(macizos)
    resultado = (tinta & ~macizos) | anillo
    return resultado, int(cantidad), int(macizos.sum())


def preparar_lineas(
    jpg: Path, contornear_macizos: bool = True, umbral: int | None = None
) -> ResultadoF2:
    """F2 — jpg a blanco y negro puro de lineas.

    `contornear_macizos` viene en True por decision explicita del usuario. El
    resultado siempre declara cuantas zonas se tocaron y que area, para que la
    modificacion del arte quede a la vista.
    """
    if jpg.suffix.lower() not in (".jpg", ".jpeg", ".jfif"):
        raise ImagenInvalida(str(jpg), "esta etapa recibe JPG si o si")
    with _abrir_como_pil(jpg) as imagen:
        if imagen.format != "JPEG":
            raise ImagenInvalida(str(jpg), f"esta etapa recibe JPG si o si (es {imagen.format})")
        grises = np.asarray(imagen.convert("L"), dtype=np.uint8)

    corte = int(threshold_otsu(grises)) if umbral is None else int(umbral)
    tinta: Mascara = grises <= corte

    ancho_px = _ancho_trazo_px(tinta) if tinta.any() else 1.0
    zonas = 0
    area = 0
    if contornear_macizos and tinta.any():
        tinta, zonas, area = _contornear_macizos(tinta, ancho_px)

    salida = np.where(tinta, np.uint8(0), np.uint8(255)).astype(np.uint8)
    return ResultadoF2(
        imagen=salida,
        umbral_usado=corte,
        ancho_trazo_px=ancho_px,
        zonas_contorneadas=zonas,
        area_contorneada_px=area,
        contorneado_activo=contornear_macizos,
    )


def guardar_binaria(resultado: ResultadoF2, salida: Path) -> Path:
    """Guarda en PNG. Nunca en JPG: la compresion volveria a meter grises."""
    if salida.suffix.lower() != ".png":
        raise ImagenInvalida(str(salida), "la salida de la correccion de lineas va en PNG")
    salida.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(resultado.imagen, mode="L").save(salida, format="PNG", optimize=True)
    return salida
