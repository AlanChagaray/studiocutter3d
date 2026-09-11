"""Geometria 2D: convergencia de escala, dilatacion y offsets del cortador."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cutter3d.errors import NoConvergeError
from cutter3d.geometry import (
    _offset,
    construir_cortador_2d,
    construir_marcador_2d,
    construir_silueta_sola,
    lado_mayor,
    partes_de_silueta,
    tapar_huecos,
)
from cutter3d.params import AjustesMotor, CutterParams
from cutter3d.svg_io import Arte, cargar_svg, como_multipoligono

FIXTURES = Path(__file__).parent / "fixtures"


def arte(nombre: str, p: CutterParams | None = None) -> Arte:
    return cargar_svg(FIXTURES / f"{nombre}.svg", p or CutterParams(), AjustesMotor())


def test_convergencia_lado_mayor() -> None:
    """RF-11: el lado mayor de la silueta final mide 90 mm +- 0,1."""
    p = CutterParams()
    m = construir_marcador_2d(arte("lineart_ojos_llenos"), p, AjustesMotor())
    assert lado_mayor(m.silueta) == pytest.approx(90.0, abs=0.1)


def test_la_dilatacion_lleva_el_trazo_al_objetivo() -> None:
    """El fixture viene con el trazo a ~0,54 mm; tiene que terminar en 1,0 mm."""
    p = CutterParams()
    m = construir_marcador_2d(arte("lineart_ojos_llenos"), p, AjustesMotor())
    assert m.mediana_antes_mm < p.ancho_trazo_mm
    assert m.dilatacion_aplicada_mm > 0
    assert m.trazo.mediana == pytest.approx(p.ancho_trazo_mm, abs=0.05)


def test_no_se_adelgaza_un_trazo_mas_grueso_que_el_objetivo() -> None:
    """El contrato pide avisar antes de adelgazar, nunca adelgazar solo."""
    p = CutterParams()
    m = construir_marcador_2d(arte("circulo"), p, AjustesMotor())
    assert m.mediana_antes_mm > p.ancho_trazo_mm
    assert m.dilatacion_aplicada_mm == 0.0
    assert any("mas grueso" in aviso for aviso in m.advertencias)


def test_no_converge_falla_duro() -> None:
    """RF-18: sin iteraciones suficientes se levanta la excepcion, no se aproxima."""
    p = CutterParams()
    with pytest.raises(NoConvergeError) as exc:
        construir_marcador_2d(arte("lineart_ojos_llenos"), p, AjustesMotor(max_iteraciones=1))
    assert exc.value.lado_objetivo_mm == p.lado_mayor_mm
    assert exc.value.iteraciones == 1


def test_la_silueta_tapa_todos_los_huecos() -> None:
    m = construir_marcador_2d(arte("lineart_ojos_llenos"), CutterParams(), AjustesMotor())
    assert sum(len(g.interiors) for g in m.silueta.geoms) == 0
    assert partes_de_silueta(m.silueta) == 1


def test_la_silueta_contiene_al_arte() -> None:
    m = construir_marcador_2d(arte("lineart_ojos_llenos"), CutterParams(), AjustesMotor())
    assert m.arte_final.difference(m.silueta).area == pytest.approx(0.0, abs=1e-9)


def test_offsets_del_cortador() -> None:
    """El filo es o2-o1 y el pie es o3-o1, no o3-o2."""
    p, a = CutterParams(), AjustesMotor()
    silueta = construir_silueta_sola(arte("circulo"), p, a)
    c = construir_cortador_2d(silueta, p, a)
    assert c.filo.area == pytest.approx(c.o2.area - c.o1.area, rel=1e-6)
    assert c.pie.area == pytest.approx(c.o3.area - c.o1.area, rel=1e-6)
    assert c.pie.area != pytest.approx(c.o3.area - c.o2.area, rel=1e-3)


def test_areas_del_cortador_contra_el_calculo_analitico() -> None:
    """El circulo permite comprobar los offsets contra la formula, no contra si mismos."""
    p, a = CutterParams(), AjustesMotor()
    silueta = construir_silueta_sola(arte("circulo"), p, a)
    c = construir_cortador_2d(silueta, p, a)
    r = p.lado_mayor_mm / 2
    r1, r2, r3 = r + p.offset_o1_mm, r + p.offset_o2_mm, r + p.offset_o3_mm
    assert c.filo.area == pytest.approx(math.pi * (r2**2 - r1**2), rel=0.01)
    assert c.pie.area == pytest.approx(math.pi * (r3**2 - r1**2), rel=0.01)


def test_luz_entre_marcador_y_cortador() -> None:
    """RF-09: la simplificacion no puede comerse la luz de 0,7 mm."""
    p, a = CutterParams(), AjustesMotor()
    m = construir_marcador_2d(arte("lineart_ojos_llenos"), p, a)
    c = construir_cortador_2d(m.silueta, p, a)
    assert float(m.silueta.distance(c.o1.boundary)) >= p.luz_mm - 0.02


def test_modo_cortante_no_itera_ni_dilata() -> None:
    """Sin marcador no hay trazos que medir: la escala se resuelve exacta."""
    p, a = CutterParams(), AjustesMotor()
    silueta = construir_silueta_sola(arte("estrella"), p, a)
    assert lado_mayor(silueta) == pytest.approx(90.0, abs=1e-6)
    assert sum(len(g.interiors) for g in silueta.geoms) == 0


def test_tapar_huecos_elimina_los_interiores() -> None:
    a_lineart = arte("lineart_ojos_llenos")
    assert a_lineart.n_huecos == 1
    tapada = tapar_huecos(a_lineart.poligonos)
    assert sum(len(g.interiors) for g in tapada.geoms) == 0


def test_parametros_distintos_cambian_los_offsets() -> None:
    """Los offsets se derivan de los parametros: no estan hardcodeados."""
    a = AjustesMotor()
    p_default = CutterParams()
    p_ancho = CutterParams(luz_mm=2.0)
    silueta = construir_silueta_sola(arte("circulo"), p_default, a)
    c1 = construir_cortador_2d(silueta, p_default, a)
    c2 = construir_cortador_2d(silueta, p_ancho, a)
    assert c2.o1.area > c1.o1.area
    assert float(silueta.distance(c2.o1.boundary)) == pytest.approx(2.0, abs=0.02)


def test_una_camara_tras_un_cuello_angosto_genera_bolsillo_sin_puentear() -> None:
    """El defecto que motivo el puenteo, fijado a mano para que no vuelva callado.

    Los offsets se arman aca mismo sobre la silueta CRUDA, sin pasar por
    `construir_cortador_2d`, que es el unico que puentea: asi queda documentado
    lo que pasa cuando no se puentea. La camara de 14,4 mm esta detras de un
    cuello de 2,52 mm, mas angosto que 2*o2, asi que o2 lo pellizca y la camara
    queda como celda cerrada por filo — un bolsillo ciego donde la masa se atasca
    y el cortador sale con euler -2. El filo termina con DOS anillos: el legitimo
    de la galletita, mas el del bolsillo.
    """
    p, a = CutterParams(), AjustesMotor()
    silueta = construir_silueta_sola(arte("dos_lobulos"), p, a)
    o1 = _offset(silueta, p.offset_o1_mm, a)
    o2 = _offset(silueta, p.offset_o2_mm, a)
    filo = como_multipoligono(o2.difference(o1))
    assert sum(len(g.interiors) for g in o2.geoms) == 1, "o2 se cierra sobre el cuello"
    assert sum(len(g.interiors) for g in filo.geoms) == 2, "el anillo legitimo mas el bolsillo"


def test_el_puenteo_deja_el_filo_con_un_solo_anillo() -> None:
    """La misma silueta, ahora por `construir_cortador_2d`: la muesca se rellena antes."""
    p, a = CutterParams(), AjustesMotor()
    silueta = construir_silueta_sola(arte("dos_lobulos"), p, a)
    c = construir_cortador_2d(silueta, p, a)
    assert sum(len(g.interiors) for g in c.filo.geoms) == 1
    assert c.colisiones_puenteadas == 1
    # Rango y no `> 0`: un puenteo degenerado de 0,001 mm2 tambien seria `> 0`
    # y dejaria el bolsillo practicamente igual. Medido: 252,03 mm2.
    assert 200.0 < c.area_puenteada_mm2 < 300.0


def test_el_area_puenteada_no_decrece_con_la_distancia_de_colision() -> None:
    """La trampa del detector: con una sola fuente de semilla no era monotono.

    El material que agrega forzar la colision crece con la distancia, pero las
    celdas ya cerradas no dependen de ella. Sembrando solo con lo primero, a
    ciertos valores el bolsillo quedaba sin puentear. Por eso no alcanza con
    probar el default: el filo tiene que cerrar en un anillo para los tres, y el
    area puenteada tiene que ser no decreciente, nunca achicarse al subir la luz.

    Corre sobre el murcielago y no sobre `dos_lobulos` a proposito. En
    `dos_lobulos` la camara ya es celda cerrada de `o2`, asi que la semilla que
    NO depende de la distancia alcanza sola y las tres areas quedan a 0,25 % una
    de otra: el `<=` pasaria igual si el parametro no hiciera nada. En el
    murcielago la dispersion es 13 %, y por eso se puede exigir ademas un `<`
    estricto entre los extremos — que es el assert que falla si el parametro
    deja de tener efecto. Cuesta 1,2 s contra 0,07 s; el poder de deteccion los
    vale.
    """
    a = AjustesMotor()
    areas: list[float] = []
    for distancia in (0.05, 1.0, 5.0):
        p = CutterParams(distancia_colision_mm=distancia)
        silueta = construir_silueta_sola(arte("murcielago", p), p, a)
        c = construir_cortador_2d(silueta, p, a)
        assert sum(len(g.interiors) for g in c.filo.geoms) == 1, f"distancia {distancia} mm"
        areas.append(c.area_puenteada_mm2)
    a005, a1, a5 = areas
    assert a005 <= a1 <= a5, f"el area puenteada decrece: {areas}"
    assert a005 < a5, f"la distancia de colision no tuvo ningun efecto: {areas}"


@pytest.mark.parametrize("fixture", ["circulo", "estrella", "lineart_ojos_llenos"])
def test_sin_colisiones_no_se_puentea_nada(fixture: str) -> None:
    """Guardia contra falsos positivos: sin muescas, la silueta no se toca.

    Los tres fixtures que existian antes del ciclo: ninguno pellizca ni con 6 mm
    de offset, asi que los tres tienen que dar cero exacto. Si alguno empieza a
    puentear, el detector se volvio sensible de mas.
    """
    p, a = CutterParams(), AjustesMotor()
    silueta = construir_silueta_sola(arte(fixture), p, a)
    c = construir_cortador_2d(silueta, p, a)
    assert c.colisiones_puenteadas == 0
    assert c.area_puenteada_mm2 == 0.0


def test_la_distancia_de_colision_no_toca_el_marcador() -> None:
    """El parametro es de la pieza cortador: el marcador sale identico con cualquier valor."""
    a = AjustesMotor()
    p_corta = CutterParams(distancia_colision_mm=0.5)
    p_larga = CutterParams(distancia_colision_mm=5.0)
    m_corta = construir_marcador_2d(arte("lineart_ojos_llenos", p_corta), p_corta, a)
    m_larga = construir_marcador_2d(arte("lineart_ojos_llenos", p_larga), p_larga, a)
    assert m_corta.silueta.equals(m_larga.silueta)
    assert m_corta.arte_final.equals(m_larga.arte_final)
