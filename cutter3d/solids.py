"""Extrusion a 3D y booleanas reales.

Sigue "Como construir el marcador" (paso 6) y "Como construir el cortador" de
`prompt_cortante.md`:

- **Marcador**: la silueta maciza extruida a la altura de la base, unida con los
  trazos extruidos desde z=0 hasta la altura total. Sin agujeros pasantes, asi
  que el numero de Euler tiene que dar 2.
- **Cortador**: el filo (`o2 - o1`) extruido 10 mm y el pie (`o3 - o1`) extruido
  2 mm. Es un anillo cerrado: numero de Euler 0.

Las uniones son **booleanas reales** con el engine `manifold`, no mallas
superpuestas: el contrato lo pide explicitamente y es lo unico que garantiza un
solido cerrado que Cura pueda laminar.

Los materiales PBR del `.glb` estan aca y no en `export`: son parte de como se ve
el solido, y asi el `.3mf` y el `.glb` describen exactamente la misma geometria.
"""

from __future__ import annotations

import trimesh
from shapely.geometry import MultiPolygon
from trimesh.visual import TextureVisuals
from trimesh.visual.material import PBRMaterial

from .errors import BooleanaFallida
from .geometry import Cortador2D, Marcador2D
from .params import CutterParams

NOMBRE_MARCADOR = "marcador"
NOMBRE_CORTADOR = "cortador"

# Colores de filamento, para que el preview se lea como plastico impreso y no
# como un gris plano. `roughnessFactor` alto y `metallicFactor` 0: PLA mate.
_COLOR_CORTADOR = (0.31, 0.72, 0.89, 1.0)
_COLOR_MARCADOR = (0.80, 0.84, 0.86, 1.0)
_RUGOSIDAD_PLA = 0.62


def _material(nombre: str, color: tuple[float, float, float, float]) -> PBRMaterial:
    return PBRMaterial(
        name=nombre,
        baseColorFactor=color,
        metallicFactor=0.0,
        roughnessFactor=_RUGOSIDAD_PLA,
    )


def _extruir(geom: MultiPolygon, altura: float) -> list[trimesh.Trimesh]:
    return [trimesh.creation.extrude_polygon(parte, altura) for parte in geom.geoms]


def _unir(mallas: list[trimesh.Trimesh], objeto: str) -> trimesh.Trimesh:
    if not mallas:
        raise BooleanaFallida(objeto, "no hay ninguna malla para unir")
    if len(mallas) == 1:
        unida = mallas[0]
    else:
        try:
            unida = trimesh.boolean.union(mallas, engine="manifold")
        except Exception as exc:
            raise BooleanaFallida(objeto, f"el engine manifold fallo: {exc}") from exc
    if unida is None or len(unida.faces) == 0:
        raise BooleanaFallida(objeto, "la union no produjo geometria")
    return unida


def construir_marcador_3d(m: Marcador2D, p: CutterParams) -> trimesh.Trimesh:
    """Base maciza + trazos, unidos con booleana real."""
    base = _extruir(m.silueta, p.altura_base_mm)
    trazos = _extruir(m.arte_final, p.altura_total_marcador_mm)
    malla = _unir(base + trazos, NOMBRE_MARCADOR)
    malla.visual = TextureVisuals(material=_material("filamento_marcador", _COLOR_MARCADOR))
    return malla


def construir_cortador_3d(c: Cortador2D, p: CutterParams) -> trimesh.Trimesh:
    """Filo alto + pie ancho, unidos con booleana real.

    El pie se extruye como `o3 - o1` y no como `o3 - o2`: abajo de la altura del
    pie se solapa con el filo, asi que la union no depende de dos caras
    coincidentes. La forma final es identica.

    Caso degenerado: la huella del filo (`o2 - o1`) esta **siempre** contenida en
    la del pie (`o3 - o1`), porque `o2` esta dentro de `o3`. Si ademas el filo no
    es mas alto que el pie, el filo no aporta nada y se saltea. Unirlos igual con
    alturas identicas deja las caras superiores coincidentes y la booleana
    devuelve una malla no cerrada — que es exactamente el problema que el truco
    del `o3 - o1` evita en las caras laterales.
    """
    pie = _extruir(c.pie, p.pie_alto_mm)
    piezas = pie if p.filo_alto_mm <= p.pie_alto_mm else _extruir(c.filo, p.filo_alto_mm) + pie
    malla = _unir(piezas, NOMBRE_CORTADOR)
    malla.visual = TextureVisuals(material=_material("filamento_cortador", _COLOR_CORTADOR))
    return malla


def construir_escena(
    marcador: Marcador2D | None, cortador: Cortador2D, p: CutterParams
) -> trimesh.Scene:
    """Escena con los objetos nombrados, los dos apoyados en z=0 y anidados.

    Los dos solidos se construyen en el mismo sistema de coordenadas a partir de
    la misma silueta, asi que ya quedan en su posicion anidada real: no hay que
    moverlos.
    """
    escena = trimesh.Scene()
    if marcador is not None:
        escena.add_geometry(construir_marcador_3d(marcador, p), geom_name=NOMBRE_MARCADOR)
    escena.add_geometry(construir_cortador_3d(cortador, p), geom_name=NOMBRE_CORTADOR)
    return escena
