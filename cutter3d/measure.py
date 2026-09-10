"""Medicion del ancho de trazo y de los huecos entre trazos.

Sigue el paso 4 de `prompt_cortante.md`:

    "Medi el ancho real del trazo con transformada de distancia sobre el
     esqueleto, podando las puntas espurias del esqueleto (~0,5 mm) para que las
     esquinas no ensucien los percentiles bajos."

El procedimiento es el mismo para trazos y para huecos, cambiando la mascara:

    rasterizar -> medial_axis (eje + distancia) -> podar -> percentiles

En cada pixel del eje medial, la transformada de distancia vale el radio
inscrito, asi que el ancho local es `2 * distancia`. `medial_axis` devuelve las
dos cosas en una sola pasada; el porque de no usar `skeletonize` esta explicado
en `_anchos_mm`. La poda saca las ramas espurias que el eje genera en las
esquinas y que, sin podar, meten anchos falsamente chicos en los percentiles
bajos.

Los huecos se miden sobre el **complemento del arte dentro de la silueta**: es la
separacion entre trazos, que es lo que decide si la masa se despega del marcador.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageDraw
from scipy.ndimage import convolve
from shapely.geometry import MultiPolygon, Polygon
from skimage.morphology import medial_axis

from .params import AjustesMotor

Mascara = npt.NDArray[np.bool_]

MARGEN_PX = 6
_KERNEL_VECINOS = np.ones((3, 3), dtype=np.uint8)
_VECINOS_DE_EXTREMO = 2
"""Suma del kernel 3x3 en un extremo del eje: el propio pixel + 1 vecino."""


@dataclass(frozen=True)
class Grilla:
    """Mapeo entre milimetros y pixeles. Compartida por todas las mascaras de una medicion."""

    ppm: float
    minx: float
    maxy: float
    ancho_px: int
    alto_px: int

    def a_pixeles(
        self, xs: npt.NDArray[np.float64], ys: npt.NDArray[np.float64]
    ) -> list[tuple[float, float]]:
        px = (xs - self.minx) * self.ppm + MARGEN_PX
        py = (self.maxy - ys) * self.ppm + MARGEN_PX
        return list(zip(px.tolist(), py.tolist(), strict=True))


@dataclass(frozen=True)
class MedidaTrazo:
    p1: float
    p5: float
    mediana: float
    p95: float
    px_por_mm_efectivo: float
    poda_completa: bool
    """False cuando la poda habria vaciado el esqueleto y se freno antes."""


@dataclass(frozen=True)
class MedidaHuecos:
    minimo: float
    p1: float
    p5: float
    mediana: float
    fraccion_bajo_umbral: float
    fraccion_bajo_1mm: float
    umbral_mm: float
    px_por_mm_efectivo: float


def crear_grilla(bounds: tuple[float, float, float, float], a: AjustesMotor) -> Grilla:
    """Grilla para las mascaras, clampeando la resolucion al tope de memoria."""
    minx, miny, maxx, maxy = bounds
    ancho_mm = max(maxx - minx, 1e-9)
    alto_mm = max(maxy - miny, 1e-9)
    ppm = a.px_por_mm
    lado_mm = max(ancho_mm, alto_mm)
    max_util = a.max_px_lado - 2 * MARGEN_PX
    if lado_mm * ppm > max_util:
        ppm = max_util / lado_mm
    return Grilla(
        ppm=ppm,
        minx=minx,
        maxy=maxy,
        ancho_px=int(np.ceil(ancho_mm * ppm)) + 2 * MARGEN_PX,
        alto_px=int(np.ceil(alto_mm * ppm)) + 2 * MARGEN_PX,
    )


def rasterizar(geom: MultiPolygon | Polygon, grilla: Grilla) -> Mascara:
    """Poligonos a mascara booleana. Los huecos se restan pintandolos de fondo."""
    imagen = Image.new("1", (grilla.ancho_px, grilla.alto_px), 0)
    dibujo = ImageDraw.Draw(imagen)
    partes = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    for poligono in partes:
        xs, ys = np.asarray(poligono.exterior.coords).T
        dibujo.polygon(grilla.a_pixeles(xs, ys), fill=1)
    for poligono in partes:
        for hueco in poligono.interiors:
            xs, ys = np.asarray(hueco.coords).T
            dibujo.polygon(grilla.a_pixeles(xs, ys), fill=0)
    # `np.array` y no `np.asarray`: el buffer que devuelve PIL es de solo
    # lectura y las rutinas de morfologia de skimage necesitan el suyo escribible.
    return np.array(imagen, dtype=bool)


def _podar(esqueleto: Mascara, iteraciones: int) -> tuple[Mascara, bool]:
    """Saca `iteraciones` capas de extremos. Frena antes de vaciar el esqueleto."""
    esq = esqueleto
    for _ in range(iteraciones):
        if not esq.any():
            return esq, False
        vecinos = convolve(esq.astype(np.uint8), _KERNEL_VECINOS, mode="constant", cval=0)
        extremos = esq & (vecinos <= _VECINOS_DE_EXTREMO)
        restante = esq & ~extremos
        if not restante.any():
            return esq, False
        esq = restante
    return esq, True


def _anchos_mm(
    mascara: Mascara, a: AjustesMotor, ppm: float
) -> tuple[npt.NDArray[np.float64], bool]:
    """Anchos locales en mm sobre el eje medial podado de la mascara.

    Se usa `medial_axis` y no `skeletonize`. Motivo verificado en esta maquina
    (scikit-image 0.26.0, Windows, Python 3.13.7): `skeletonize` **segfaultea**
    con la mascara rasterizada del fixture `circulo` (1812x1812, area maciza),
    y lo hace tambien sin ninguna llamada previa a la transformada de distancia.
    No es un problema de tamaño: un disco sintetico de identico tamaño, dtype,
    flags y area pasa sin problema, asi que el disparador esta en la
    configuracion concreta del borde rasterizado, no en las dimensiones.

    `medial_axis` ademas devuelve el eje y la transformada de distancia en una
    sola pasada, que es justo lo que pide el contrato: el ancho local es
    `2 * distancia` sobre el eje.
    """
    if not mascara.any():
        return np.empty(0, dtype=np.float64), True
    ejes_raw, distancia_raw = medial_axis(mascara, return_distance=True)
    ejes = np.asarray(ejes_raw, dtype=bool)
    distancia = np.asarray(distancia_raw, dtype=np.float64)
    pasos = max(1, round(a.poda_esqueleto_mm * ppm))
    podado, completa = _podar(ejes, pasos)
    if not podado.any():
        podado, completa = ejes, False
    return 2.0 * distancia[podado] / ppm, completa


def medir_ancho_trazo(geom: MultiPolygon, a: AjustesMotor) -> MedidaTrazo:
    """Percentiles del ancho de trazo del arte, en mm."""
    grilla = crear_grilla(geom.bounds, a)
    anchos, completa = _anchos_mm(rasterizar(geom, grilla), a, grilla.ppm)
    if anchos.size == 0:
        return MedidaTrazo(0.0, 0.0, 0.0, 0.0, grilla.ppm, completa)
    p1, p5, mediana, p95 = (float(v) for v in np.percentile(anchos, [1, 5, 50, 95]))
    return MedidaTrazo(p1, p5, mediana, p95, grilla.ppm, completa)


def medir_huecos(arte: MultiPolygon, silueta: Polygon, a: AjustesMotor) -> MedidaHuecos:
    """Percentiles de la separacion entre trazos, dentro de la silueta."""
    grilla = crear_grilla(silueta.bounds, a)
    mascara_hueco = rasterizar(silueta, grilla) & ~rasterizar(arte, grilla)
    anchos, _ = _anchos_mm(mascara_hueco, a, grilla.ppm)
    if anchos.size == 0:
        return MedidaHuecos(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, a.umbral_hueco_imprimible_mm, grilla.ppm)
    minimo = float(anchos.min())
    p1, p5, mediana = (float(v) for v in np.percentile(anchos, [1, 5, 50]))
    return MedidaHuecos(
        minimo=minimo,
        p1=p1,
        p5=p5,
        mediana=mediana,
        fraccion_bajo_umbral=float((anchos < a.umbral_hueco_imprimible_mm).mean()),
        fraccion_bajo_1mm=float((anchos < 1.0).mean()),
        umbral_mm=a.umbral_hueco_imprimible_mm,
        px_por_mm_efectivo=grilla.ppm,
    )
