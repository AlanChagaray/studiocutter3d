"""RF-01 — validacion de rango de los 10 parametros de dimension.

`test_rango_invalido` es exactamente 10 campos x 3 valores = **30 casos**. Ese
numero es el criterio de aceptacion, asi que los demas tests de este archivo van
con otro nombre y no lo alteran.
"""

from __future__ import annotations

import math
from dataclasses import fields

import pytest

from cutter3d.errors import ParametroFueraDeRango
from cutter3d.params import AjustesMotor, CutterParams

CAMPOS = tuple(f.name for f in fields(CutterParams))
VALORES_INVALIDOS = (-1.0, 0.0, 1000.1)


def test_son_diez_campos() -> None:
    assert len(CAMPOS) == 10


@pytest.mark.parametrize("campo", CAMPOS)
@pytest.mark.parametrize("valor", VALORES_INVALIDOS)
def test_rango_invalido(campo: str, valor: float) -> None:
    with pytest.raises(ParametroFueraDeRango) as exc:
        CutterParams(**{campo: valor})
    assert exc.value.parametro == campo
    assert exc.value.valor == valor


def test_defaults_son_los_del_contrato() -> None:
    p = CutterParams()
    assert p.lado_mayor_mm == 90.0
    assert p.altura_base_mm == 1.0
    assert p.altura_trazos_mm == 3.0
    assert p.ancho_trazo_mm == 1.0
    assert p.luz_mm == 0.7
    assert p.filo_ancho_mm == 1.0
    assert p.filo_alto_mm == 10.0
    assert p.pie_ancho_extra_mm == 1.8
    assert p.pie_alto_mm == 2.0
    assert p.distancia_colision_mm == 1.0


def test_offsets_derivados_dan_los_del_contrato() -> None:
    p = CutterParams()
    assert p.offset_o1_mm == pytest.approx(0.7)
    assert p.offset_o2_mm == pytest.approx(1.7)
    assert p.offset_o3_mm == pytest.approx(3.5)
    assert p.altura_total_marcador_mm == pytest.approx(4.0)
    assert p.ancho_total_cortador_mm == pytest.approx(2.8)


def test_offsets_siguen_a_los_parametros() -> None:
    """Los offsets se derivan, no estan hardcodeados."""
    p = CutterParams(luz_mm=1.0, filo_ancho_mm=2.0, pie_ancho_extra_mm=3.0)
    assert p.offset_o1_mm == pytest.approx(1.0)
    assert p.offset_o2_mm == pytest.approx(3.0)
    assert p.offset_o3_mm == pytest.approx(6.0)


def test_el_limite_exacto_de_1000_es_valido() -> None:
    assert CutterParams(lado_mayor_mm=1000.0).lado_mayor_mm == 1000.0


@pytest.mark.parametrize("valor", [math.nan, math.inf, -math.inf])
def test_no_finitos_rechazados(valor: float) -> None:
    with pytest.raises(ParametroFueraDeRango):
        CutterParams(lado_mayor_mm=valor)


def test_booleano_no_cuenta_como_numero() -> None:
    with pytest.raises(ParametroFueraDeRango):
        CutterParams(lado_mayor_mm=True)  # type: ignore[arg-type]


def test_ajustes_motor_rechaza_no_positivos() -> None:
    with pytest.raises(ParametroFueraDeRango):
        AjustesMotor(px_por_mm=0.0)
    with pytest.raises(ParametroFueraDeRango):
        AjustesMotor(poda_esqueleto_mm=-1.0)


def test_ajustes_motor_default() -> None:
    a = AjustesMotor()
    assert a.px_por_mm == 20.0
    assert a.simplify_arte_mm == 0.02
    assert a.simplify_offsets_mm == 0.01
