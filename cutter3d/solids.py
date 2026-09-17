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

# ⚠ 0,78 y no el 0,62 de antes: una pieza recien salida de una FDM no brilla.
#
# Con 0,62 el lobulo especular del panel cenital se concentra bastante como
# para dejar una mancha clara sobre la cara de arriba, que es justo donde vive
# el grabado del marcador — o sea que el brillo tapaba el detalle que la foto
# existe para mostrar. Subir la rugosidad reparte ese mismo brillo sobre toda
# la cara: el nivel medio casi no se mueve y el contraste local del relieve
# deja de competir contra un reflejo.
#
# Es ademas lo fisicamente cierto para el PLA mate con lineas de capa: la
# microgeometria de la superficie impresa dispersa mucho mas que un plastico
# inyectado. El front la complementa con la textura de capas de `preview3d.js`,
# que es la otra mitad de lo mismo — una dispersa, la otra dibuja.
_RUGOSIDAD_PLA = 0.78


def _material(nombre: str, color: tuple[float, float, float, float]) -> PBRMaterial:
    return PBRMaterial(
        name=nombre,
        baseColorFactor=color,
        metallicFactor=0.0,
        roughnessFactor=_RUGOSIDAD_PLA,
    )


def aplicar_acabado(malla: trimesh.Trimesh, objeto: str | None = None) -> trimesh.Trimesh:
    """Le pone a una malla el acabado PLA mate de este motor, y devuelve la malla.

    **Es publica porque tiene un segundo consumidor: `cutter3d/malla.py`**, que
    convierte a `.glb` un `.3mf`/`.stl` que este motor no construyo. Ese es el
    unico camino por el que la foto de un archivo viejo puede salir igual a la de
    un cortante recien generado, y el motivo es concreto: un `.3mf` **no
    transporta materiales**, asi que el `.glb` derivado sale sin array
    `materials` y el visor le aplica el default de la spec de glTF
    (`metallicFactor` 1.0, `roughnessFactor` 1.0) — o sea metal rugoso en vez de
    plastico mate. Y `pintarPieza` del front pisa el color, nunca el acabado.

    Que viva aca y no alla es lo que evita dos verdades sobre el mismo acabado.

    `objeto` es el nombre de la geometria en la escena. Un `.stl` no tiene
    nombres de objeto —el formato no los soporta— y una malla anonima es, en el
    caso normal, un cortador: ese es el default. El color igual lo pisa la paleta
    de la pantalla; lo que no se puede recuperar despues es el metallic y el
    roughness.
    """
    color = _COLOR_MARCADOR if objeto == NOMBRE_MARCADOR else _COLOR_CORTADOR
    nombre = NOMBRE_MARCADOR if objeto == NOMBRE_MARCADOR else NOMBRE_CORTADOR
    malla.visual = TextureVisuals(material=_material(f"filamento_{nombre}", color))
    return malla


def _extruir(geom: MultiPolygon, altura: float) -> list[trimesh.Trimesh]:
    """Extruye cada parte con el triangulador de `manifold`, nunca el default.

    El default de trimesh es `earcut`, y `earcut` no cierra los poligonos con
    vertices colineales: al llegar a una oreja de area cero emite un triangulo
    degenerado y se come otro, asi que la tapa queda con una arista sin par y la
    extrusion sale NO watertight. Con line art vectorizado pasa seguido —
    VTracer deja tiradas de puntos colineales sobre los tramos rectos, y
    `simplify` no los borra porque no desvian nada.

    El sintoma es enganoso: el poligono 2D es valido (`is_valid`), la malla
    tiene el area correcta y hasta el volumen correcto; lo unico que delata el
    agujero es el numero de Euler, que da uno de mas. Despues explota lejos de
    la causa, en la booleana, con "Not all meshes are volumes!".

    `manifold` triangula los mismos anillos sin insertar vertices, asi que la
    geometria es identica — cambia quien la corta en triangulos, no la forma.
    Es ademas el mismo engine que ya usan las booleanas, y `manifold3d` ya es
    dependencia dura del proyecto: el fix no agrega nada que instalar.
    """
    return [
        trimesh.creation.extrude_polygon(parte, altura, engine="manifold") for parte in geom.geoms
    ]


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
    return aplicar_acabado(malla, NOMBRE_MARCADOR)


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
    return aplicar_acabado(malla, NOMBRE_CORTADOR)


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
