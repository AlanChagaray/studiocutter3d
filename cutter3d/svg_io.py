"""Lectura del SVG de line art y conversion a poligonos de shapely.

Sigue el paso 1 de `prompt_cortante.md`:

    "Parquea el SVG a poligonos, aplicando las transformaciones del archivo.
     Combina los subpaths con XOR (even-odd) para que los contornos internos
     queden como huecos. Aplana las curvas fino (~0,05 mm en escala final)."

Dos decisiones que no son obvias:

1. **XOR dentro de cada elemento, union entre elementos.** El contrato habla de
   combinar los *subpaths* con even-odd: eso es lo que convierte el contorno
   interior de una forma en un hueco. Entre elementos distintos del SVG la
   semantica de pintado es union, no XOR — si no, dos formas superpuestas se
   cancelarian.
2. **El aplanado tiene que dar 0,05 mm en la escala FINAL**, pero la escala final
   todavia no se conoce al parsear. Se resuelve con una pasada preliminar: bbox
   crudo -> escala aproximada -> aplanar con `flatten_mm / escala` en unidades
   SVG. Despues de aplanar, escalar es una transformacion afin **exacta** sobre
   poligonos, asi que el bucle de convergencia de `geometry` puede reescalar sin
   volver a aplanar y sin perder precision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path as RutaFs
from typing import Any

from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from svgelements import SVG, Close, Move, Path, Shape

from .errors import SvgInvalido
from .params import AjustesMotor, CutterParams

MAX_MUESTRAS_POR_SEGMENTO = 2000
MIN_PUNTOS_POR_ANILLO = 4


@dataclass(frozen=True)
class Arte:
    """El dibujo ya en milimetros, con el eje Y hacia arriba y centrado en el origen.

    `n_contornos` y `n_huecos` son el conteo **tal como se parseo**, util para
    diagnostico y para el reporte de entrada. La comparacion de fidelidad de
    RF-02 NO se hace contra estos numeros sino en `verify`, entre el arte
    original escalado y el arte final: los dos a la escala definitiva y por el
    mismo camino de calculo. Escalar es una transformacion afin que no cambia la
    topologia, asi que los conteos coinciden — pero la referencia que manda es la
    de `verify`, no esta.
    """

    poligonos: MultiPolygon
    n_contornos: int
    n_huecos: int
    escala_preliminar: float


def contar_contornos_y_huecos(geom: MultiPolygon) -> tuple[int, int]:
    """Contornos exteriores y huecos interiores de una geometria."""
    return len(geom.geoms), sum(len(g.interiors) for g in geom.geoms)


def como_multipoligono(geom: BaseGeometry) -> MultiPolygon:
    """Normaliza a MultiPolygon, descartando restos de dimension menor."""
    if geom.is_empty:
        return MultiPolygon()
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    partes = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(partes)


def _largo_segmento(segmento: Any) -> float:
    try:
        return float(segmento.length())
    except (TypeError, ValueError, ZeroDivisionError):
        inicio, fin = segmento.start, segmento.end
        return float(math.hypot(fin.x - inicio.x, fin.y - inicio.y))


def _muestrear_segmento(segmento: Any, tolerancia: float) -> list[tuple[float, float]]:
    """Puntos del segmento SIN incluir el inicio (lo aporta el segmento anterior)."""
    if isinstance(segmento, (Move, Close)):
        return []
    largo = _largo_segmento(segmento)
    if largo <= tolerancia:
        fin = segmento.end
        return [(float(fin.x), float(fin.y))]
    n = min(MAX_MUESTRAS_POR_SEGMENTO, max(2, math.ceil(largo / tolerancia)))
    puntos: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        punto = segmento.point(i / n)
        puntos.append((float(punto.x), float(punto.y)))
    return puntos


def _anillos_de_path(path: Path, tolerancia: float) -> list[list[tuple[float, float]]]:
    anillos: list[list[tuple[float, float]]] = []
    for subpath in path.as_subpaths():
        segmentos = list(subpath)
        if not segmentos:
            continue
        inicio = segmentos[0].end if isinstance(segmentos[0], Move) else segmentos[0].start
        puntos: list[tuple[float, float]] = [(float(inicio.x), float(inicio.y))]
        for segmento in segmentos:
            puntos.extend(_muestrear_segmento(segmento, tolerancia))
        if len(puntos) >= MIN_PUNTOS_POR_ANILLO:
            anillos.append(puntos)
    return anillos


def _xor_de_anillos(anillos: list[list[tuple[float, float]]]) -> BaseGeometry | None:
    """Even-odd: los contornos internos quedan como huecos."""
    acumulado: BaseGeometry | None = None
    for anillo in anillos:
        poligono: BaseGeometry = Polygon(anillo)
        if not poligono.is_valid:
            poligono = poligono.buffer(0)
        if poligono.is_empty:
            continue
        acumulado = poligono if acumulado is None else acumulado.symmetric_difference(poligono)
    return acumulado


def _formas_rellenas(svg: SVG, ruta: str) -> list[Shape]:
    formas: list[Shape] = []
    solo_trazo = 0
    for elemento in svg.elements():
        if not isinstance(elemento, Shape):
            continue
        relleno = getattr(elemento, "fill", None)
        trazo = getattr(elemento, "stroke", None)
        tiene_relleno = relleno is not None and getattr(relleno, "value", None) is not None
        tiene_trazo = trazo is not None and getattr(trazo, "value", None) is not None
        if not tiene_relleno:
            if tiene_trazo:
                solo_trazo += 1
            continue
        formas.append(elemento)
    if not formas:
        motivo = (
            f"no hay ninguna forma rellena ({solo_trazo} con stroke y sin fill). "
            "El contrato asume line art vectorizado: paths rellenos en negro, sin stroke."
            if solo_trazo
            else "no hay ninguna forma dibujable"
        )
        raise SvgInvalido(ruta, motivo)
    return formas


def cargar_svg(ruta: RutaFs, p: CutterParams, a: AjustesMotor) -> Arte:
    """Lee el SVG y devuelve el arte en mm, eje Y arriba, centrado en el origen."""
    texto_ruta = str(ruta)
    if not ruta.is_file():
        raise SvgInvalido(texto_ruta, "el archivo no existe")
    try:
        svg = SVG.parse(texto_ruta, reify=True)
    except Exception as exc:
        raise SvgInvalido(texto_ruta, f"no se pudo parsear: {exc}") from exc

    formas = _formas_rellenas(svg, texto_ruta)

    # Escala preliminar, para que el aplanado de 0,05 mm sea en la escala final.
    caja = _bbox_de_formas(formas, texto_ruta)
    lado_svg = max(caja[2] - caja[0], caja[3] - caja[1])
    if lado_svg <= 0:
        raise SvgInvalido(texto_ruta, "el dibujo no tiene extension: bbox degenerado")
    escala = p.lado_mayor_mm / lado_svg
    tolerancia = a.flatten_mm / escala

    piezas: list[BaseGeometry] = []
    for forma in formas:
        anillos = _anillos_de_path(Path(forma), tolerancia)
        pieza = _xor_de_anillos(anillos)
        if pieza is not None and not pieza.is_empty:
            piezas.append(pieza)
    if not piezas:
        raise SvgInvalido(texto_ruta, "las formas no produjeron ningun area cerrada")

    unido = unary_union(piezas)

    # SVG es y-abajo: se espeja el eje Y y se escala en un solo paso.
    espejado = affine_transform(unido, [escala, 0.0, 0.0, -escala, 0.0, 0.0])
    minx, miny, maxx, maxy = espejado.bounds
    centrado = affine_transform(
        espejado, [1.0, 0.0, 0.0, 1.0, -(minx + maxx) / 2.0, -(miny + maxy) / 2.0]
    )

    poligonos = como_multipoligono(centrado)
    if poligonos.is_empty:
        raise SvgInvalido(texto_ruta, "el resultado no contiene poligonos")
    n_contornos, n_huecos = contar_contornos_y_huecos(poligonos)
    return Arte(
        poligonos=poligonos,
        n_contornos=n_contornos,
        n_huecos=n_huecos,
        escala_preliminar=escala,
    )


def _bbox_de_formas(formas: list[Shape], ruta: str) -> tuple[float, float, float, float]:
    cajas = [f.bbox() for f in formas]
    validas = [c for c in cajas if c is not None]
    if not validas:
        raise SvgInvalido(ruta, "las formas no tienen bounding box")
    return (
        min(float(c[0]) for c in validas),
        min(float(c[1]) for c in validas),
        max(float(c[2]) for c in validas),
        max(float(c[3]) for c in validas),
    )
