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
   mas angosta que 2*o2 el cortante nunca la pudo reproducir: sin puentear queda
   como un bolsillo ciego donde la masa se atasca, o peor. `distancia_colision_mm`
   ensancha ese umbral a `2*o2 + distancia` (4,4 mm con los defaults), para que dos
   extremos que quedan cerca sin llegar a tocarse tampoco dejen un filo demasiado
   fino. El puenteo cambia COMO se resuelve, no cuanto se reproduce, y toca SOLO la
   silueta que consume el cortador: el arte y el marcador quedan intactos, asi que
   la regla de fidelidad sigue en pie.

**La garantia que da el puenteo, y por que es una sola regla.** El filo es
`o2 - o1`, o sea la banda que queda entre dos dilataciones que se llevan
`filo_ancho_mm`. Esa banda mide exactamente lo pedido mientras las dos paredes
tengan lugar; en cuanto el complemento de la silueta se angosta, se degrada, y
degrada de tres maneras que son la misma:

| Boca de la muesca (`w`) | Que queda del filo ahi |
|---|---|
| `w > 2*o2 + distancia` | dos paredes enteras de `filo_ancho_mm`: sano |
| `2*luz < w <= 2*o2` | un alma fusionada de `w - 2*luz`: **mas fina o mas gruesa** |
| `w <= 2*luz` | `o1` cierra la boca; la camara de adentro queda **suelta** |

Por eso se puentea **toda zona del cierre morfologico que entra en la banda del
filo**, y no solo las que disparaba tal o cual sintoma. La contrapartida esta
declarada: esa muesca deja de cortarse y queda como luz mas grande. Es el orden
de prioridades que fija el contrato — antes un cortante funcional que una muesca
reproducida con un filo que no se puede imprimir.

En modo `cortante` (sin marcador) **no se mide ancho de trazo ni se dilata**: una
silueta maciza no tiene trazos, y medirla solo produciria una advertencia sin
sentido. Se escala en un paso, que ademas es exacto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import shapely
from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from .errors import CuerpoSueltoEnCortador, NoConvergeError
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

    ventanas_del_pie: int = 0
    """Huecos del pie que NO son la galletita: ventanas que cerro el offset `o3`.

    Aparecen cuando dos tramos del contorno quedan mas lejos que la boca que
    puentea `distancia_colision_mm` (`2*o2 + distancia`, 4,4 mm con los
    defaults) pero mas cerca que `2*o3` (7 mm): el filo sigue abierto ahi —o
    sea, **corta bien**— y el pie, que es mas ancho, se cierra sobre el hueco y
    queda calado.

    No es un bolsillo ciego y no se puentea: el bolsillo ciego es del FILO y
    atasca masa; esto es un calado en el ala de apoyo de 2 mm, que es un solido
    valido e imprimible. Por eso solo se cuenta y se advierte.
    """

    area_ventanas_pie_mm2: float = 0.0
    """Area total de esas ventanas, en mm2."""


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


def _huecos_ajenos_a_la_galletita(geom: MultiPolygon, o1: MultiPolygon) -> list[Polygon]:
    """Huecos de `geom` que NO son la galletita.

    Un hueco es la galletita si interseca `o1`. El criterio descansa en que
    entre un hueco ajeno y `o1` siempre hay una pared completa: dos geometrias
    tangentes en un solo punto lo enganarian, pero eso no puede pasar con
    offsets separados por `filo_ancho_mm`.

    Se le pasa el PIE: son las ventanas del ala de apoyo, que solo se advierten
    (ver `Cortador2D.ventanas_del_pie`). Sobre el filo ya no hace falta — con el
    puenteo actual no quedan bolsillos ciegos que detectar, porque la garganta
    que los produce se rellena antes de los offsets.
    """
    ajenos: list[Polygon] = []
    for parte in geom.geoms:
        for anillo in parte.interiors:
            hueco = Polygon(anillo)
            if not hueco.intersects(o1):
                ajenos.append(hueco)
    return ajenos


def cuerpos_sueltos(anillo: MultiPolygon, o1: MultiPolygon) -> list[Polygon]:
    """Piezas de `anillo` que no sujetan galletita: los cuerpos sueltos.

    Una pieza legitima del cortador es un ANILLO — tiene al menos un hueco que
    interseca `o1`, y ese hueco es la galletita que esa pieza rodea y corta. Una
    pieza sin ningun hueco asi no rodea nada: sale de la impresora como un pedazo
    de filo flotando adentro del cortante, sin nada que lo sostenga.

    **De donde salen.** Son exactamente los huecos de `o1`. Cuando una muesca
    tiene la boca mas angosta que `2*luz` pero adentro se ensancha, `o1` se cierra
    sobre la boca y esa camara queda como hueco de `o1`; ahi `o1` no existe, `o2`
    y `o3` si, y `o2 - o1` vale toda la camara — una isla, desconectada del anillo
    de afuera. `_puentear_colisiones` las elimina rellenando la muesca en la
    silueta, que es la unica forma de sacarlas sin romper la coherencia de los
    tres offsets.

    Se mide sobre el PIE y no sobre el filo por lo mismo que el numero de Euler:
    la huella del filo esta contenida en la del pie, asi que el solido se retrae
    sobre el pie y es su conteo de piezas el que manda.
    """
    return [
        parte
        for parte in anillo.geoms
        if not any(Polygon(hueco).intersects(o1) for hueco in parte.interiors)
    ]


def _puentear_colisiones(silueta: MultiPolygon, p: CutterParams, a: AjustesMotor) -> _Puenteo:
    """Puentea en la SILUETA toda garganta que el filo no puede recorrer entera.

    Se hace aguas arriba de los offsets para que o1/o2/o3 salgan todos de la
    misma silueta puenteada: si se corrigiera despues, los tres offsets dejarian
    de ser coherentes entre si.

    **La regla, en una linea:** `base` es el cierre morfologico de la silueta con
    radio `o2 + distancia/2`, o sea *todo* lo que el complemento tiene mas
    angosto que `2*o2 + distancia`; de ese material se puentea el que **entra en
    la banda del filo**, y se deja el que no.

    Las dos mitades importan:

    - **Cerrar con ese radio no es una heuristica.** El cierre por disco de radio
      `r` deja intacto exactamente lo que un disco de radio `r` puede recorrer,
      asi que rellena todas las gargantas mas angostas que `2r` y **ninguna otra**.
      Con `r = o2 + distancia/2` eso es, literalmente, el umbral del contrato
      (4,4 mm con los defaults). Se calcula como `cerrar(o2, distancia/2)`
      encogido `o2` —que da lo mismo, porque las erosiones se componen— para poder
      tapar los huecos en el medio: una camara que quedo encerrada detras de una
      boca angosta tiene que entrar al puente ENTERA, y es `tapar_huecos` el que
      la mete. Sin ese paso la camara sobrevive como hueco de `o1` y vuelve el
      cuerpo suelto que el puenteo existe para evitar.
    - **El filtro es `o1` y no una semilla.** Una zona que queda contenida en `o1`
      cae entera adentro de la luz: el filo nunca la pisa, asi que rellenarla no
      cambiaria una linea del cortador y solo agrandaria el area que el reporte
      declara como no cortada. Es lo que pasa en cada rincon concavo del dibujo,
      que son cientos. Las que se salen de `o1` son las otras: ahi el filo entra,
      y entra fusionado —mas fino o mas grueso que `filo_ancho_mm`— o rodeando una
      camara que va a quedar suelta.

    Antes esto se resolvia sembrando: bolsillos ya cerrados del filo mas el
    material que agregaba forzar la colision. Las dos semillas juntas **no cubren
    el caso mas comun**, que es el de dos paredes de `o2` que ya se tocan sin
    encerrar nada —un brazo que roza el cuerpo, una mano contra una pierna—:
    forzar la colision no agrega material donde ya estaba fusionado, y sin camara
    cerrada no hay bolsillo que sembrar. El filo bajaba igual a la muesca, con un
    alma de menos de 1 mm, y si `o1` alcanzaba a cerrar la boca dejaba ademas la
    isla suelta. Medido sobre `sr-cara-papa`: 2 cuerpos sueltos y 9,2 mm2 de filo
    mas fino que el ancho pedido, contra 0 y 0,2 mm2 con esta regla.
    """
    o1 = _offset(silueta, p.offset_o1_mm, a)
    o2 = _offset(silueta, p.offset_o2_mm, a)
    envolvente = tapar_huecos(_cerrar(o2, p.distancia_colision_mm / 2.0))
    base = como_multipoligono(
        envolvente.buffer(-p.offset_o2_mm, quad_segs=QUAD_SEGS, join_style="round")
    )
    # `prepare` construye el indice espacial de `o1` una sola vez: sin el, cada
    # `covers` lo rearma, y en un line art vectorizado las zonas son cientos.
    shapely.prepare(o1)
    puentes = [
        zona for zona in como_multipoligono(base.difference(silueta)).geoms if not o1.covers(zona)
    ]
    if not puentes:
        return _Puenteo(silueta, 0, 0.0)

    puenteada = tapar_huecos(como_multipoligono(unary_union([silueta, *puentes])))
    return _Puenteo(puenteada, len(puentes), puenteada.area - silueta.area)


def construir_cortador_2d(silueta: MultiPolygon, p: CutterParams, a: AjustesMotor) -> Cortador2D:
    """Tres offsets `round` sobre la silueta; filo y pie salen de restarlos.

    Antes de los offsets se puentean las colisiones entre extremos: una garganta
    mas angosta que `2*o2 + distancia_colision_mm` no le deja lugar a las dos
    paredes del filo, asi que se rellena en la silueta y los tres offsets se
    derivan de esa.
    """
    puenteo = _puentear_colisiones(silueta, p, a)
    base = puenteo.silueta
    # Aceptado y deliberado: cuando hay colision, o1 y o2 se calculan dos veces
    # —una para detectar sobre la silueta original, otra para construir sobre la
    # puenteada—. Es preferible a pasar estado entre el detector y el constructor.
    o1 = _offset(base, p.offset_o1_mm, a)
    o2 = _offset(base, p.offset_o2_mm, a)
    o3 = _offset(base, p.offset_o3_mm, a)
    pie = como_multipoligono(o3.difference(o1))
    # Guarda, no rama esperada: con la silueta puenteada no queda ningun hueco de
    # `o1`, que es lo unico que produce cuerpos sueltos. Esta aca por lo mismo que
    # `exigir_manifold` — un pedazo de filo que no sujeta nada no se puede
    # imprimir, y el contrato manda fallar duro antes que entregarlo.
    sueltos = cuerpos_sueltos(pie, o1)
    if sueltos:
        raise CuerpoSueltoEnCortador(len(sueltos), float(sum(s.area for s in sueltos)))
    # Se miden DESPUES del puenteo y sobre el pie ya construido: lo que sobrevive
    # aca es exactamente lo que se va a extruir, que es lo que define la
    # topologia del solido (ver `euler_esperado_de`).
    ventanas = _huecos_ajenos_a_la_galletita(pie, o1)
    return Cortador2D(
        filo=como_multipoligono(o2.difference(o1)),
        pie=pie,
        o1=o1,
        o2=o2,
        o3=o3,
        colisiones_puenteadas=puenteo.colisiones,
        area_puenteada_mm2=puenteo.area_mm2,
        ventanas_del_pie=len(ventanas),
        area_ventanas_pie_mm2=float(sum(v.area for v in ventanas)),
    )


def partes_de_silueta(silueta: MultiPolygon) -> int:
    """Cuantas piezas disjuntas tiene la silueta.

    Para el numero de Euler esperado usar `euler_esperado_de`, que contempla
    ademas los huecos: contar solo las piezas alcanza para el marcador —su
    huella no tiene huecos, los tapa `tapar_huecos`— pero no para el cortador.
    """
    return len(silueta.geoms)


def euler_esperado_de(huella: MultiPolygon) -> int:
    """Numero de Euler de la superficie del solido que se extruye de `huella`.

    Un prisma sobre una region plana de `c` componentes y `h` huecos tiene por
    borde una superficie cerrada de caracteristica `2*(c - h)`: cada componente
    aporta 2 y cada hueco es un agujero pasante que resta 2. Una pieza maciza da
    2 (el marcador) y un anillo simple da 0 (el cortador), que son los dos
    numeros que fija el contrato para el caso canonico.

    **Por que se calcula y no alcanza con esos dos literales.** El contrato los
    enuncia para la forma tipica; la geometria real los generaliza en los DOS
    ejes, no solo en el de las piezas:

    - `c > 1`: una silueta en varias piezas da `2c` para el marcador.
    - `h > 1` en el pie: cuando dos tramos del contorno quedan mas lejos que la
      boca que puentea `distancia_colision_mm` pero mas cerca que `2*o3`, el
      filo sigue abierto ahi —corta bien— y el pie se cierra sobre el hueco
      dejando una ventana. El solido es valido, cerrado e imprimible; lo unico
      que no es, es un anillo simple. Fijar el 0 a mano convertia ese caso en un
      `MallaNoManifold` que decia "no cerro" sobre una malla con
      `watertight=True`.

    Para el cortador se le pasa el PIE: la huella del filo esta siempre
    contenida en la del pie (`o2` dentro de `o3`), asi que el solido se retrae
    sobre el pie y es su topologia la que manda. Vale igual en el caso
    degenerado en que el filo no se extruye por no ser mas alto que el pie.

    ⚠ **Calcularlo sobre la huella real tiene un punto ciego, y no se tapa
    aca.** Un cortador partido en islas cumple su propio numero de Euler: tres
    piezas y un hueco dan 4, y la malla mide 4. La comprobacion pasa y el
    archivo sale VERIFICADO con pedazos sueltos adentro. Lo que lo evita es
    `cuerpos_sueltos`, que se exige en `construir_cortador_2d` antes de extruir
    nada — este numero dice si el solido CERRO, no si es utilizable.
    """
    componentes = len(huella.geoms)
    huecos = sum(len(parte.interiors) for parte in huella.geoms)
    return 2 * (componentes - huecos)


def resumen_contornos(geom: MultiPolygon) -> tuple[int, int]:
    """Contornos exteriores y huecos de una geometria 2D del marcador.

    Existe para que `verify` no tenga que importar de `svg_io`: la capa que le
    entrega los poligonos es esta, y el conteo forma parte de ese contrato.
    """
    return contar_contornos_y_huecos(geom)
