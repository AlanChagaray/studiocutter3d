"""Bateria de verificacion de fidelidad.

Implementa la seccion "Verificacion antes de entregar" de `prompt_cortante.md`.

**Como se evita la verificacion circular.** Cada numero se obtiene por un camino
distinto del que lo genero:

| Que se verifica | Camino de generacion | Camino de verificacion |
|---|---|---|
| watertight, Euler, nombres, z=0 | `trimesh.Scene` en memoria | se **relee el .3mf del disco** |
| secciones a z=1/5/9,5 | poligonos shapely `o1/o2/o3` | `mesh.section(...)` **desde la malla** |
| contornos, huecos, contencion, desvio | operaciones de buffer/union |
  comparacion shapely entre el arte original escalado y el final |
| luz minima | offset `o1 = silueta + 0,7` | distancia medida **despues** de simplificar |

**Que NO prueba este reporte**, y queda declarado en el propio texto: la luz se
mide en 2D sobre los poligonos que efectivamente se extruyeron. Eso demuestra que
la simplificacion no se comio la separacion, que es el riesgo real, pero no es
distancia superficie-a-superficie entre las dos mallas 3D.

**Interpretacion del criterio de muescas.** El contrato pide "cuanto contorno del
original quedo a mas de 0,15 mm del final". Con una dilatacion uniforme de `d`,
TODO punto del contorno original queda exactamente a `d` del contorno final, asi
que el umbral se aplica sobre el exceso: se cuenta el contorno cuya distancia
supera `d + 0,15 mm`. Con `d = 0` el criterio se reduce al literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely
import trimesh
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from .errors import Cutter3DError, MallaNoManifold
from .geometry import Cortador2D, Marcador2D, lado_mayor, partes_de_silueta, resumen_contornos
from .measure import MedidaHuecos, MedidaTrazo
from .params import AjustesMotor, CutterParams
from .solids import NOMBRE_CORTADOR, NOMBRE_MARCADOR

PASO_MUESTREO_MM = 0.1
"""Espaciado del muestreo de contornos. Cada muestra representa ~este arco."""

TOL_Z_MM = 1e-6
TOL_SECCION = 0.01
"""1 % de desvio admitido entre el area de la seccion y la esperada."""

TOL_AREA_MM2 = 1e-6
"""Piso de area para considerar que dos geometrias son la misma."""

TOL_SIMPLIFICACION_MM = 0.02
"""Lo que la simplificacion del arte puede mover un contorno, por encima de la dilatacion."""

MIN_PUNTOS_ANILLO = 4
"""Menos de 4 puntos no cierran un anillo con area."""


@dataclass(frozen=True)
class Seccion:
    z_mm: float
    area_medida_mm2: float
    area_esperada_mm2: float

    @property
    def desvio_relativo(self) -> float:
        if self.area_esperada_mm2 <= 0:
            return 0.0
        return abs(self.area_medida_mm2 - self.area_esperada_mm2) / self.area_esperada_mm2

    @property
    def ok(self) -> bool:
        return self.desvio_relativo <= TOL_SECCION


@dataclass(frozen=True)
class Topologia:
    objeto: str
    watertight: bool
    euler: int
    euler_esperado: int
    z_min_mm: float
    z_max_mm: float

    @property
    def ok(self) -> bool:
        return (
            self.watertight and self.euler == self.euler_esperado and abs(self.z_min_mm) <= TOL_Z_MM
        )


@dataclass(frozen=True)
class ReporteFidelidad:
    """Todo lo que el contrato pide mostrar antes de entregar."""

    objetos: tuple[str, ...]
    topologias: tuple[Topologia, ...]
    secciones: tuple[Seccion, ...]
    lado_mayor_final_mm: float
    luz_minima_real_mm: float
    luz_nominal_mm: float

    contornos_original: int | None
    contornos_final: int | None
    huecos_original: int | None
    huecos_final: int | None
    area_diferencia_mm2: float | None

    desvio_p50_mm: float | None
    desvio_p99_mm: float | None
    desvio_max_mm: float | None
    dilatacion_aplicada_mm: float
    mediana_trazo_antes_mm: float | None
    """Mediana del trazo ANTES de dilatar. Junto con `trazo.mediana` deja ver de
    donde salio la dilatacion y hasta donde llego."""

    muescas_selladas_mm: float
    muescas_selladas_pct: float

    trazo: MedidaTrazo | None
    huecos: MedidaHuecos | None

    zonas_contorneadas: int
    area_contorneada_mm2: float

    advertencias: tuple[str, ...]

    @property
    def conteos_coinciden(self) -> bool:
        return (
            self.contornos_original == self.contornos_final
            and self.huecos_original == self.huecos_final
        )

    @property
    def desvio_acotado(self) -> bool:
        """El contorno no se movio mas de lo que la dilatacion justifica.

        Es una de las comprobaciones que pide el contrato, asi que tiene que
        pesar en el veredicto y no solo en el test: quien corre el CLI sobre su
        propio arte necesita que un contorno corrido le baje el veredicto.
        """
        if self.desvio_max_mm is None:
            return True
        return self.desvio_max_mm <= self.dilatacion_aplicada_mm + TOL_SIMPLIFICACION_MM

    @property
    def todo_ok(self) -> bool:
        return (
            all(t.ok for t in self.topologias)
            and all(s.ok for s in self.secciones)
            and self.conteos_coinciden
            and (self.area_diferencia_mm2 or 0.0) < TOL_AREA_MM2
            and self.desvio_acotado
            and self.luz_minima_real_mm >= self.luz_nominal_mm - TOL_SIMPLIFICACION_MM
        )


def _muestrear_contorno(geom: BaseGeometry, paso: float) -> shapely.GeometryType | None:
    borde = geom.boundary
    if borde.is_empty:
        return None
    return shapely.segmentize(borde, paso)


def _distancias_de_contorno(origen: BaseGeometry, destino: BaseGeometry, paso: float) -> np.ndarray:
    borde = _muestrear_contorno(origen, paso)
    if borde is None:
        return np.empty(0, dtype=np.float64)
    coords = shapely.get_coordinates(borde)
    if coords.size == 0:
        return np.empty(0, dtype=np.float64)
    puntos = shapely.points(coords)
    return np.asarray(shapely.distance(puntos, destino.boundary), dtype=np.float64)


def area_de_seccion(malla: trimesh.Trimesh, z: float) -> float:
    """Area de la seccion horizontal, calculada **desde la malla**.

    Se arman los anillos a mano con shapely en vez de usar `Path3D.to_planar()`,
    que exige `rtree`. Los anillos se combinan con XOR (even-odd), igual que los
    subpaths del SVG: en un anillo cortante eso da exactamente la corona.
    """
    seccion = malla.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
    if seccion is None:
        return 0.0
    acumulado: BaseGeometry | None = None
    for polilinea in seccion.discrete:
        if len(polilinea) < MIN_PUNTOS_ANILLO:
            continue
        anillo: BaseGeometry = Polygon(polilinea[:, :2])
        if not anillo.is_valid:
            anillo = anillo.buffer(0)
        acumulado = anillo if acumulado is None else acumulado.symmetric_difference(anillo)
    return float(acumulado.area) if acumulado is not None else 0.0


def _alturas_de_muestreo(p: CutterParams, cortador: Cortador2D) -> tuple[tuple[float, float], ...]:
    """Las tres alturas de seccion con su area esperada, **derivadas de los parametros**.

    Con los defaults del contrato (pie 2 mm, filo 10 mm) dan exactamente z=1, z=5
    y z=9,5. No se hardcodean esos numeros porque la expectativa de area depende
    de en que zona cae cada altura: por debajo del pie conviven filo y pie
    (`o3 - o1`), por encima queda solo el filo (`o2 - o1`). Con un filo de 3 mm,
    una altura fija de 5 mm caeria fuera de la malla y `verify` reportaria un
    fallo sobre geometria perfectamente correcta.
    """
    con_pie = cortador.o3.area - cortador.o1.area
    solo_filo = cortador.o2.area - cortador.o1.area
    z_en_pie = min(1.0, p.pie_alto_mm * 0.5)
    tramo_solo_filo = p.filo_alto_mm - p.pie_alto_mm
    if tramo_solo_filo <= 0:
        # El pie llega tan alto como el filo: no existe la zona de "solo filo".
        return ((z_en_pie, con_pie),)
    return (
        (z_en_pie, con_pie),
        (p.pie_alto_mm + tramo_solo_filo * 3.0 / 8.0, solo_filo),
        (p.pie_alto_mm + tramo_solo_filo * 15.0 / 16.0, solo_filo),
    )


def _topologia(nombre: str, malla: trimesh.Trimesh, euler_esperado: int) -> Topologia:
    return Topologia(
        objeto=nombre,
        watertight=bool(malla.is_watertight),
        euler=int(malla.euler_number),
        euler_esperado=euler_esperado,
        z_min_mm=float(malla.bounds[0][2]),
        z_max_mm=float(malla.bounds[1][2]),
    )


def _fidelidad_del_arte(
    m: Marcador2D, a: AjustesMotor
) -> tuple[int, int, int, int, float, float, float, float, float, float]:
    original: MultiPolygon = m.arte_original_escalado
    final: MultiPolygon = m.arte_final
    contornos_o, huecos_o = resumen_contornos(original)
    contornos_f, huecos_f = resumen_contornos(final)
    area_diferencia = float(original.difference(final).area)

    desvios = _distancias_de_contorno(final, original, PASO_MUESTREO_MM)
    if desvios.size:
        p50, p99 = (float(v) for v in np.percentile(desvios, [50, 99]))
        dmax = float(desvios.max())
    else:
        p50 = p99 = dmax = 0.0

    inverso = _distancias_de_contorno(original, final, PASO_MUESTREO_MM)
    umbral = m.dilatacion_aplicada_mm + a.umbral_muesca_mm
    if inverso.size:
        fraccion = float((inverso > umbral).mean())
        selladas_mm = fraccion * float(original.boundary.length)
    else:
        fraccion = 0.0
        selladas_mm = 0.0

    return (
        contornos_o,
        contornos_f,
        huecos_o,
        huecos_f,
        area_diferencia,
        p50,
        p99,
        dmax,
        selladas_mm,
        fraccion * 100.0,
    )


def verificar(
    ruta_3mf: Path,
    marcador: Marcador2D | None,
    cortador: Cortador2D,
    silueta: MultiPolygon,
    *,
    p: CutterParams,
    a: AjustesMotor,
    huecos: MedidaHuecos | None = None,
    zonas_contorneadas: int = 0,
    area_contorneada_mm2: float = 0.0,
) -> ReporteFidelidad:
    """Verifica el .3mf ya escrito y devuelve el reporte tipado."""
    # Import local a proposito: `export` importa de `solids` y `verify` importa
    # de ambos; subirlo al tope crearia un ciclo export <-> verify.
    from .export import releer_3mf  # noqa: PLC0415

    mallas = releer_3mf(ruta_3mf)
    # Sin fallbacks silenciosos: si el archivo no trae lo que se le pidio
    # construir, se dice, en vez de saltear la verificacion de ese objeto.
    faltantes = [NOMBRE_CORTADOR] if NOMBRE_CORTADOR not in mallas else []
    if marcador is not None and NOMBRE_MARCADOR not in mallas:
        faltantes.append(NOMBRE_MARCADOR)
    if faltantes:
        raise Cutter3DError(
            f"el .3mf releido de {ruta_3mf} no contiene {', '.join(faltantes)}; "
            f"trae {sorted(mallas)}"
        )

    partes = partes_de_silueta(silueta)
    topologias: list[Topologia] = []
    if NOMBRE_MARCADOR in mallas:
        topologias.append(_topologia(NOMBRE_MARCADOR, mallas[NOMBRE_MARCADOR], 2 * partes))
    topologias.append(_topologia(NOMBRE_CORTADOR, mallas[NOMBRE_CORTADOR], 0))

    malla_cortador = mallas[NOMBRE_CORTADOR]
    secciones = tuple(
        Seccion(z, area_de_seccion(malla_cortador, z), esperada)
        for z, esperada in _alturas_de_muestreo(p, cortador)
    )

    luz = float(silueta.distance(cortador.o1.boundary))

    advertencias: list[str] = []
    # En modo `cortante` no hay marcador y por lo tanto no hay arte que comparar:
    # esas metricas quedan en None, que es distinto de cero. Se declaran con su
    # tipo aca en vez de dejar que la primera rama lo fije.
    c_o: int | None = None
    c_f: int | None = None
    h_o: int | None = None
    h_f: int | None = None
    area_dif: float | None = None
    p50: float | None = None
    p99: float | None = None
    dmax: float | None = None
    selladas_mm = 0.0
    selladas_pct = 0.0

    if marcador is not None:
        (
            c_o,
            c_f,
            h_o,
            h_f,
            area_dif,
            p50,
            p99,
            dmax,
            selladas_mm,
            selladas_pct,
        ) = _fidelidad_del_arte(marcador, a)
        advertencias.extend(marcador.advertencias)
        if (c_o, h_o) != (c_f, h_f):
            advertencias.append(
                f"el conteo de formas cambio con la dilatacion: contornos {c_o} -> {c_f}, "
                f"huecos {h_o} -> {h_f}. El contrato pide que sean iguales."
            )
        if selladas_pct > 0:
            advertencias.append(
                f"la dilatacion sello muescas: {selladas_mm:.2f} mm de contorno "
                f"({selladas_pct:.1f} % del original). Se reporta, no se compensa."
            )

    if huecos is not None and huecos.fraccion_bajo_umbral > 0:
        advertencias.append(
            f"huecos entre trazos por debajo de {huecos.umbral_mm:.1f} mm: "
            f"{huecos.fraccion_bajo_umbral * 100:.1f} % del esqueleto. "
            "Una boquilla de 0,4 mm probablemente los cierre al imprimir."
        )
    if zonas_contorneadas:
        advertencias.append(
            f"la etapa de correccion de lineas contorneo {zonas_contorneadas} zona(s) maciza(s) "
            f"({area_contorneada_mm2:.1f} mm2). El motor no altera el arte: "
            "el cambio se hizo antes."
        )
    if luz < p.luz_mm - 0.02:
        advertencias.append(
            f"la luz minima real ({luz:.3f} mm) quedo por debajo de la nominal "
            f"({p.luz_mm:.3f} mm) mas la tolerancia de simplificacion."
        )

    return ReporteFidelidad(
        objetos=tuple(sorted(mallas)),
        topologias=tuple(topologias),
        secciones=secciones,
        lado_mayor_final_mm=lado_mayor(silueta),
        luz_minima_real_mm=luz,
        luz_nominal_mm=p.luz_mm,
        contornos_original=c_o,
        contornos_final=c_f,
        huecos_original=h_o,
        huecos_final=h_f,
        area_diferencia_mm2=area_dif,
        desvio_p50_mm=p50,
        desvio_p99_mm=p99,
        desvio_max_mm=dmax,
        dilatacion_aplicada_mm=marcador.dilatacion_aplicada_mm if marcador else 0.0,
        mediana_trazo_antes_mm=marcador.mediana_antes_mm if marcador else None,
        muescas_selladas_mm=selladas_mm,
        muescas_selladas_pct=selladas_pct,
        trazo=marcador.trazo if marcador else None,
        huecos=huecos,
        zonas_contorneadas=zonas_contorneadas,
        area_contorneada_mm2=area_contorneada_mm2,
        advertencias=tuple(advertencias),
    )


def exigir_manifold(r: ReporteFidelidad) -> None:
    """Falla duro si algun objeto releido del .3mf no es un solido cerrado."""
    for t in r.topologias:
        if not t.watertight or t.euler != t.euler_esperado:
            raise MallaNoManifold(t.objeto, t.watertight, t.euler, t.euler_esperado)


def _marca(ok: bool) -> str:
    return "OK" if ok else "!!"


def veredicto_de(r: ReporteFidelidad) -> str:
    """Tres estados, no dos.

    Un archivo que pasa todas las comprobaciones pero tiene advertencias de
    imprimibilidad NO es lo mismo que uno limpio, y el contrato visual del
    proyecto distingue los dos casos.
    """
    if not r.todo_ok:
        return "NO VERIFICADO"
    return "VERIFICADO CON ADVERTENCIAS" if r.advertencias else "VERIFICADO"


def _lineas_de_fidelidad(r: ReporteFidelidad) -> list[str]:
    """Conteos, contencion y desvio. Vacio en modo `cortante` (no hay arte)."""
    if r.contornos_original is None:
        return []
    return [
        "-- contornos y huecos (arte original escalado -> arte final) --",
        f"  contornos exteriores : {r.contornos_original} -> {r.contornos_final}  "
        f"{_marca(r.contornos_original == r.contornos_final)}",
        f"  huecos interiores    : {r.huecos_original} -> {r.huecos_final}  "
        f"{_marca(r.huecos_original == r.huecos_final)}",
        f"  area de la diferencia: {r.area_diferencia_mm2:.6f} mm2  "
        f"{_marca((r.area_diferencia_mm2 or 0.0) < TOL_AREA_MM2)}",
        "",
        "-- desvio del contorno final respecto del original --",
        f"  dilatacion aplicada  : {r.dilatacion_aplicada_mm:.4f} mm",
        f"  p50 / p99 / maximo   : {r.desvio_p50_mm:.4f} / "
        f"{r.desvio_p99_mm:.4f} / {r.desvio_max_mm:.4f} mm  {_marca(r.desvio_acotado)}",
        f"  muescas selladas     : {r.muescas_selladas_mm:.2f} mm ({r.muescas_selladas_pct:.1f} %)",
        "",
    ]


def render_texto(r: ReporteFidelidad) -> str:
    """Reporte legible para el CLI. La web del ciclo 2 consume la dataclass."""
    lineas: list[str] = []
    ap = lineas.append
    ap(f"=== Fidelidad: {veredicto_de(r)} ===")
    ap(f"objetos en el .3mf: {', '.join(r.objetos)}")
    ap("")
    lineas.extend(_lineas_de_fidelidad(r))

    if r.trazo is not None:
        t = r.trazo
        ap("-- ancho de trazo del arte final (mm) --")
        ap(f"  p1 {t.p1:.3f} | p5 {t.p5:.3f} | mediana {t.mediana:.3f} | p95 {t.p95:.3f}")
        if r.mediana_trazo_antes_mm is not None:
            ap(
                f"  mediana antes de dilatar: {r.mediana_trazo_antes_mm:.3f} mm "
                f"-> despues: {t.mediana:.3f} mm"
            )
        ap("")
    if r.huecos is not None:
        h = r.huecos
        ap("-- huecos entre trazos (mm) --")
        ap(f"  minimo {h.minimo:.3f} | p1 {h.p1:.3f} | p5 {h.p5:.3f} | mediana {h.mediana:.3f}")
        ap(
            f"  bajo {h.umbral_mm:.1f} mm: {h.fraccion_bajo_umbral * 100:.1f} %  |  "
            f"bajo 1,0 mm: {h.fraccion_bajo_1mm * 100:.1f} %"
        )
        ap("")

    ap("-- medidas finales --")
    ap(f"  lado mayor           : {r.lado_mayor_final_mm:.3f} mm")
    ap(
        f"  luz minima real      : {r.luz_minima_real_mm:.3f} mm (nominal {r.luz_nominal_mm:.3f})  "
        f"{_marca(r.luz_minima_real_mm >= r.luz_nominal_mm - 0.02)}"
    )
    ap("")
    ap("-- secciones del cortador (medidas sobre la malla exportada) --")
    for s in r.secciones:
        ap(
            f"  z = {s.z_mm:5.2f} mm : {s.area_medida_mm2:9.3f} mm2  "
            f"esperada {s.area_esperada_mm2:9.3f}  "
            f"desvio {s.desvio_relativo * 100:.2f} %  {_marca(s.ok)}"
        )
    ap("")
    ap("-- topologia, releida del .3mf del disco --")
    for t3 in r.topologias:
        ap(
            f"  {t3.objeto:10s} watertight={t3.watertight!s:5s} euler={t3.euler:3d} "
            f"(esperado {t3.euler_esperado})  "
            f"z=[{t3.z_min_mm:.6f}, {t3.z_max_mm:.3f}]  {_marca(t3.ok)}"
        )

    if r.advertencias:
        ap("")
        ap("-- advertencias (el archivo es valido; mira esto antes de imprimir) --")
        for i, aviso in enumerate(r.advertencias, 1):
            ap(f"  {i}. {aviso}")

    ap("")
    ap("-- que NO prueba este reporte --")
    ap("  La luz minima se mide en 2D sobre los poligonos que efectivamente se extruyeron:")
    ap("  demuestra que la simplificacion no se comio la separacion, no la distancia")
    ap("  superficie-a-superficie entre las dos mallas. Los conteos y el desvio se comparan")
    ap("  contra el arte original escalado, no contra el SVG en disco.")
    return "\n".join(lineas)
