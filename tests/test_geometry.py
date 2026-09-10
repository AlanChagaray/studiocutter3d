"""Geometria 2D: convergencia de escala, dilatacion y offsets del cortador."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cutter3d.errors import NoConvergeError
from cutter3d.geometry import (
    construir_cortador_2d,
    construir_marcador_2d,
    construir_silueta_sola,
    lado_mayor,
    partes_de_silueta,
    tapar_huecos,
)
from cutter3d.params import AjustesMotor, CutterParams
from cutter3d.svg_io import Arte, cargar_svg

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
