"""Geometria 2D del marcador y del cortador.

Corazon del contrato de fidelidad. Sigue los pasos 2 a 6 y "Como construir el
cortador" de `prompt_cortante.md`.

Cuatro cosas que el contrato marca explicitamente y que aca se respetan al pie:

1. **La unica modificacion permitida al arte es la dilatacion uniforme** para
   llegar al ancho de trazo objetivo, y solo si el trazo viene mas fino. Nada de
   cierres morfologicos, rellenos entre trazos, macizo a contorno, suavizado ni
   marcos. Si el trazo viene MAS GRUESO que el objetivo, no se adelgaza: se avisa.
2. **La escala se mide sobre la silueta final, despues de engrosar.** Como la
   dilatacion esta en mm y depende de la escala, hay que iterar hasta que
   converja. Si no converge, se levanta `NoConvergeError` con los numeros: nunca
   se cae al ultimo valor en silencio.
3. **El arte se simplifica una sola vez** (0,02 mm) y de ahi salen tanto la
   silueta como los offsets, que se simplifican mas fino (0,01 mm). Simplificar
   la silueta y los trazos por separado mueve cada uno hasta su tolerancia y se
   come la luz de 0,7 mm.
4. **El cortador puentea las colisiones entre extremos.** Una muesca con la boca
   mas angosta que 2*o2 el cortante nunca la pudo reproducir: hoy la resuelve como
   un bolsillo ciego donde la masa se atasca. `distancia_colision_mm` ensancha ese
   umbral a `2*o2 + distancia` (4,4 mm con los defaults), para que dos extremos que
   quedan cerca sin llegar a tocarse tampoco dejen un filo demasiado fino. El
   puenteo cambia COMO se resuelve, no cuanto se reproduce, y toca SOLO la silueta
   que consume el cortador: el arte y el marcador quedan intactos, asi que la regla
   de fidelidad sigue en pie.

En modo `cortante` (sin marcador) **no se mide ancho de trazo ni se dilata**: una
silueta maciza no tiene trazos, y medirla solo produciria una advertencia sin
sentido. Se escala en un paso, que ademas es exacto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from .errors import NoConvergeError
from .measure import MedidaTrazo, medir_ancho_trazo
from .params import AjustesMotor, CutterParams
from .svg_io import Arte, como_multipoligono, contar_contornos_y_huecos

QUAD_SEGS = 32
"""Segmentos por cuarto de circulo en los offsets `round`.

Con 32, el error de cuerda del offset mas grande (3,5 mm) queda en ~0,001 mm,
un orden por debajo de la tolerancia de simplificacion de los offsets.
"""


@dataclass(frozen=True)
class Marcador2D:
    arte_final: MultiPolygon
    """Trazos escalados, dilatados y simplificados. Es lo que se extruye."""

    arte_original_escalado: MultiPolygon
    """El mismo arte a la escala final pero SIN dilatar: la referencia de fidelidad."""

    silueta: MultiPolygon
    """Contornos exteriores unidos con todos los huecos tapados. Es la base maciza."""

    dilatacion_aplicada_mm: float
    iteraciones: int
    escala: float

    trazo: MedidaTrazo
    """Ancho de trazo del arte **final**, ya dilatado.

    Es lo que se imprime y lo que va al reporte."""

    mediana_antes_mm: float
    """Mediana medida ANTES de dilatar: es de donde sale la dilatacion, y la que
    define si el trazo venia mas grueso que el objetivo (en cuyo caso no se
    adelgaza, se avisa)."""

    advertencias: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Cortador2D:
    filo: MultiPolygon
    """o2 - o1, extruido 10 mm."""

    pie: MultiPolygon
    """o3 - o1 (NO o3 - o2), extruido 2 mm.

    Abajo de z = alto del pie se solapa con el filo, asi que la union booleana no
    depende de dos caras coincidentes. La forma final es identica.
    """

    o1: MultiPolygon
    o2: MultiPolygon
    o3: MultiPolygon

    colisiones_puenteadas: int = 0
    """Cuantas ZONAS se puentearon antes de los offsets.

    Con los valores usuales una zona es una colision, pero no es lo mismo y
    conviene saberlo: son componentes conexas, asi que **el conteo no es
    monotono** — al subir `distancia_colision_mm` dos zonas vecinas pueden
    fundirse en una y el numero BAJA mientras el area sube. El area si es
    monotona, y es la que hay que mirar para juzgar cuanto se puenteo.
    """

    area_puenteada_mm2: float = 0.0
    """Area que el puenteo le sumo a la silueta del cortador, en mm2."""


def escalar(geom: MultiPolygon, factor: float) -> MultiPolygon:
    """Escala respecto del origen. El arte ya viene centrado, asi que sigue centrado."""
    return como_multipoligono(affine_transform(geom, [factor, 0.0, 0.0, factor, 0.0, 0.0]))


def tapar_huecos(geom: MultiPolygon) -> MultiPolygon:
    """Union de los contornos exteriores, con todos los huecos tapados."""
    return como_multipoligono(unary_union([Polygon(g.exterior) for g in geom.geoms]))


def lado_mayor(geom: MultiPolygon) -> float:
    minx, miny, maxx, maxy = geom.bounds
    return float(max(maxx - minx, maxy - miny))


def _dilatar(geom: MultiPolygon, d: float) -> MultiPolygon:
    if d <= 0:
        return geom
    return como_multipoligono(geom.buffer(d, quad_segs=QUAD_SEGS, join_style="round"))


def _advertencias_de_trazo(
    mediana_antes: float, trazo: MedidaTrazo, p: CutterParams, a: AjustesMotor
) -> list[str]:
    avisos: list[str] = []
    if mediana_antes - p.ancho_trazo_mm > p.ancho_trazo_mm * 0.1:
        avisos.append(
            f"el trazo viene mas grueso que el objetivo: mediana {mediana_antes:.3f} mm "
            f"contra {p.ancho_trazo_mm:.3f} mm objetivo. No se adelgaza — el contrato pide "
            f"avisar antes de tocarlo."
        )
    if not trazo.poda_completa:
        avisos.append(
            "la poda del esqueleto se freno antes de completar los "
            f"{a.poda_esqueleto_mm:.2f} mm previstos para no vaciarlo: los percentiles bajos "
            "pueden estar contaminados por ramas espurias."
        )
    return avisos


def construir_marcador_2d(arte: Arte, p: CutterParams, a: AjustesMotor) -> Marcador2D:
    """Bucle de convergencia escala <-> dilatacion. Devuelve el marcador en 2D."""
    base = arte.poligonos
    escala = 1.0
    lado = 0.0
    trazo = MedidaTrazo(0.0, 0.0, 0.0, 0.0, a.px_por_mm, True)

    for iteracion in range(1, a.max_iteraciones + 1):
        escalado = escalar(base, escala)
        trazo = medir_ancho_trazo(escalado, a)
        dilatacion = max(0.0, (p.ancho_trazo_mm - trazo.mediana) / 2.0)
        arte_final = como_multipoligono(_dilatar(escalado, dilatacion).simplify(a.simplify_arte_mm))
        silueta = tapar_huecos(arte_final)
        lado = lado_mayor(silueta)

        if abs(lado - p.lado_mayor_mm) <= a.tol_convergencia_mm:
            # El reporte tiene que mostrar el trazo del arte FINAL, no el de la
            # medicion previa que alimento la dilatacion.
            trazo_final = medir_ancho_trazo(arte_final, a) if dilatacion > 0 else trazo
            return Marcador2D(
                arte_final=arte_final,
                arte_original_escalado=escalado,
                silueta=silueta,
                dilatacion_aplicada_mm=dilatacion,
                iteraciones=iteracion,
                escala=escala,
                trazo=trazo_final,
                mediana_antes_mm=trazo.mediana,
                advertencias=tuple(_advertencias_de_trazo(trazo.mediana, trazo_final, p, a)),
            )

        if lado <= 0:
            break
        escala *= p.lado_mayor_mm / lado

    raise NoConvergeError(
        iteraciones=a.max_iteraciones,
        lado_obtenido_mm=lado,
        lado_objetivo_mm=p.lado_mayor_mm,
        mediana_trazo_mm=trazo.mediana,
    )


def construir_silueta_sola(arte: Arte, p: CutterParams, a: AjustesMotor) -> MultiPolygon:
    """Modo `cortante`: solo la silueta, sin medir trazos ni dilatar.

    Sin dilatacion, la escala se resuelve en un paso y de forma exacta: no hay
    nada que iterar.
    """
    silueta_base = tapar_huecos(arte.poligonos)
    lado = lado_mayor(silueta_base)
    if lado <= 0:
        raise NoConvergeError(0, lado, p.lado_mayor_mm, 0.0)
    escala = p.lado_mayor_mm / lado
    return como_multipoligono(escalar(silueta_base, escala).simplify(a.simplify_arte_mm))


def _offset(silueta: MultiPolygon, distancia: float, a: AjustesMotor) -> MultiPolygon:
    inflado = silueta.buffer(distancia, quad_segs=QUAD_SEGS, join_style="round")
    return como_multipoligono(inflado.simplify(a.simplify_offsets_mm))


@dataclass(frozen=True)
class _Puenteo:
    """Lo que el detector le pasa al constructor del cortador.

    Existe para que `_puentear_colisiones` pueda devolver las tres cosas juntas
    sin que `construir_cortador_2d` tenga que volver a deducir ninguna. La
    alternativa —dejar la silueta puenteada en un atributo y los contadores en
    otro— obliga a mantenerlos coherentes entre si a mano, y es justo el tipo de
    estado compartido que despues se desincroniza.

    `silueta` es una COPIA derivada, solo para el cortador: la del marcador no
    se toca nunca.
    """

    silueta: MultiPolygon
    colisiones: int
    area_mm2: float


def _cerrar(geom: MultiPolygon, radio: float) -> MultiPolygon:
    """Cierre morfologico: infla `radio` y desinfla `radio`.

    Puentea las gargantas mas angostas que 2*radio. `join_style` round y
    `QUAD_SEGS`, igual que el resto del modulo.
    """
    inflado = geom.buffer(radio, quad_segs=QUAD_SEGS, join_style="round")
    return como_multipoligono(inflado.buffer(-radio, quad_segs=QUAD_SEGS, join_style="round"))


def _bolsillos_ciegos(filo: MultiPolygon, o1: MultiPolygon) -> list[Polygon]:
    """Huecos del filo que NO son la galletita: los bolsillos ciegos.

    Se llama como lo llama el contrato (`prompt_cortante.md`) y no "celdas
    cerradas": en este modulo `_cerrar` ya significa otra cosa —el cierre
    morfologico— y tener los dos sentidos a diez lineas de distancia se presta a
    leer mal cual es cual.

    Un hueco es la galletita si interseca `o1`. El criterio descansa en que
    entre un bolsillo y `o1` siempre hay una pared de filo completa: dos
    geometrias tangentes en un solo punto lo enganarian, pero eso no puede pasar
    con offsets separados por `filo_ancho_mm`.
    """
    bolsillos: list[Polygon] = []
    for parte in filo.geoms:
        for anillo in parte.interiors:
            hueco = Polygon(anillo)
            if not hueco.intersects(o1):
                bolsillos.append(hueco)
    return bolsillos


def _puentear_colisiones(silueta: MultiPolygon, p: CutterParams, a: AjustesMotor) -> _Puenteo:
    """Puentea en la SILUETA las muescas que el filo no puede reproducir.

    Se hace aguas arriba de los offsets para que o1/o2/o3 salgan todos de la
    misma silueta puenteada: si se corrigiera despues, los tres offsets dejarian
    de ser coherentes entre si.
    """
    o1 = _offset(silueta, p.offset_o1_mm, a)
    o2 = _offset(silueta, p.offset_o2_mm, a)
    filo = como_multipoligono(o2.difference(o1))

    radio = p.distancia_colision_mm / 2.0
    o2_forzado = _cerrar(o2, radio)
    # DOS fuentes de semilla, y las dos hacen falta:
    #  - las celdas ya cerradas NO dependen de la distancia de colision
    #  - el material que agrega forzar la colision SI crece con ella
    # Sin la primera el detector no es monotono: a ciertos valores de la
    # distancia dejaria el bolsillo sin puentear.
    semillas: list[Polygon] = _bolsillos_ciegos(filo, o1)
    semillas.extend(como_multipoligono(o2_forzado.difference(o2)).geoms)
    if not semillas:
        return _Puenteo(silueta, 0, 0.0)

    envolvente = tapar_huecos(o2_forzado)
    base = como_multipoligono(
        envolvente.buffer(-p.offset_o2_mm, quad_segs=QUAD_SEGS, join_style="round")
    )
    sembrado = unary_union(semillas)
    puentes = [
        zona
        for zona in como_multipoligono(base.difference(silueta)).geoms
        if zona.intersects(sembrado)
    ]
    if not puentes:
        return _Puenteo(silueta, 0, 0.0)

    puenteada = tapar_huecos(como_multipoligono(unary_union([silueta, *puentes])))
    return _Puenteo(puenteada, len(puentes), puenteada.area - silueta.area)


def construir_cortador_2d(silueta: MultiPolygon, p: CutterParams, a: AjustesMotor) -> Cortador2D:
    """Tres offsets `round` sobre la silueta; filo y pie salen de restarlos.

    Antes de los offsets se puentean las colisiones entre extremos: una muesca
    con la boca mas angosta que 2*o2 queda como bolsillo ciego donde la masa se
    atasca, asi que se tapa en la silueta y los tres offsets se derivan de esa.
    """
    puenteo = _puentear_colisiones(silueta, p, a)
    base = puenteo.silueta
    # Aceptado y deliberado: cuando hay colision, o1 y o2 se calculan dos veces
    # —una para detectar sobre la silueta original, otra para construir sobre la
    # puenteada—. Es preferible a pasar estado entre el detector y el constructor.
    o1 = _offset(base, p.offset_o1_mm, a)
    o2 = _offset(base, p.offset_o2_mm, a)
    o3 = _offset(base, p.offset_o3_mm, a)
    return Cortador2D(
        filo=como_multipoligono(o2.difference(o1)),
        pie=como_multipoligono(o3.difference(o1)),
        o1=o1,
        o2=o2,
        o3=o3,
        colisiones_puenteadas=puenteo.colisiones,
        area_puenteada_mm2=puenteo.area_mm2,
    )


def partes_de_silueta(silueta: MultiPolygon) -> int:
    """Cuantas piezas disjuntas tiene la silueta.

    Con una sola pieza, el numero de Euler esperado es 2 para el marcador y 0
    para el cortador, que es lo que dice el contrato. Con k piezas es 2k y 0.
    """
    return len(silueta.geoms)


def resumen_contornos(geom: MultiPolygon) -> tuple[int, int]:
    """Contornos exteriores y huecos de una geometria 2D del marcador.

    Existe para que `verify` no tenga que importar de `svg_io`: la capa que le
    entrega los poligonos es esta, y el conteo forma parte de ese contrato.
    """
    return contar_contornos_y_huecos(geom)
