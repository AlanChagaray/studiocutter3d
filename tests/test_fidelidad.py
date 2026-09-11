"""Red de regresion — la bateria de fidelidad de `prompt_cortante.md`.

Estos asserts SON el golden master del proyecto. No se versionan `.3mf` binarios:
cambiarian con cada version de manifold3d sin que cambie nada real. Lo que se fija
son los invariantes numericos del contrato.

El pipeline completo tarda unos segundos, asi que se construye una sola vez por
modulo y todos los tests leen del mismo resultado.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import trimesh

from cutter3d import Modo, generar
from cutter3d.export import Salidas, exportar, releer_3mf
from cutter3d.geometry import (
    Cortador2D,
    Marcador2D,
    construir_cortador_2d,
    construir_marcador_2d,
    construir_silueta_sola,
)
from cutter3d.measure import MedidaHuecos, medir_huecos
from cutter3d.params import AjustesMotor, CutterParams
from cutter3d.solids import NOMBRE_CORTADOR, NOMBRE_MARCADOR, construir_escena
from cutter3d.svg_io import cargar_svg
from cutter3d.verify import ReporteFidelidad, area_de_seccion, render_texto, verificar

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class Ciclo:
    p: CutterParams
    a: AjustesMotor
    marcador: Marcador2D
    cortador: Cortador2D
    huecos: MedidaHuecos
    salidas: Salidas
    reporte: ReporteFidelidad


def _correr(destino: Path, p: CutterParams, a: AjustesMotor, fixture: str) -> Ciclo:
    arte = cargar_svg(FIXTURES / f"{fixture}.svg", p, a)
    marcador = construir_marcador_2d(arte, p, a)
    cortador = construir_cortador_2d(marcador.silueta, p, a)
    huecos = medir_huecos(marcador.arte_final, marcador.silueta, a)
    escena = construir_escena(marcador, cortador, p)
    salidas = exportar(escena, destino, con_stl=True)
    reporte = verificar(
        salidas.ruta_3mf, marcador, cortador, marcador.silueta, p=p, a=a, huecos=huecos
    )
    return Ciclo(p, a, marcador, cortador, huecos, salidas, reporte)


@pytest.fixture(scope="module")
def ciclo(tmp_path_factory: pytest.TempPathFactory) -> Ciclo:
    destino = tmp_path_factory.mktemp("fidelidad") / "lineart.3mf"
    return _correr(destino, CutterParams(), AjustesMotor(), "lineart_ojos_llenos")


# ── RF-02 · RF-03 · RF-04: fidelidad del arte ──────────────────────────────


def test_conteo_contornos_y_huecos(ciclo: Ciclo) -> None:
    r = ciclo.reporte
    assert r.contornos_original == r.contornos_final
    assert r.huecos_original == r.huecos_final
    assert r.huecos_original == 1


def test_contencion_integra(ciclo: Ciclo) -> None:
    """El arte original queda contenido entero en el final: no se perdio nada."""
    assert ciclo.reporte.area_diferencia_mm2 == pytest.approx(0.0, abs=1e-6)


def test_desvio_acotado(ciclo: Ciclo) -> None:
    """El contorno no se movio mas de lo que la dilatacion justifica."""
    r = ciclo.reporte
    tope = r.dilatacion_aplicada_mm + 0.02
    assert r.desvio_p50_mm is not None and r.desvio_p50_mm <= tope
    assert r.desvio_p99_mm is not None and r.desvio_p99_mm <= tope
    assert r.desvio_max_mm is not None and r.desvio_max_mm <= tope


def test_muescas_selladas_se_reportan(ciclo: Ciclo) -> None:
    """Se miden y se declaran; no se compensan inventando geometria."""
    r = ciclo.reporte
    assert r.muescas_selladas_mm >= 0.0
    assert 0.0 <= r.muescas_selladas_pct <= 100.0
    if r.muescas_selladas_pct > 0:
        assert any("sello muescas" in aviso for aviso in r.advertencias)


# ── RF-05 · RF-06 · RF-07: topologia releida del archivo ───────────────────


def test_watertight_desde_archivo(ciclo: Ciclo) -> None:
    mallas = releer_3mf(ciclo.salidas.ruta_3mf)
    assert set(mallas) == {NOMBRE_MARCADOR, NOMBRE_CORTADOR}
    for malla in mallas.values():
        assert malla.is_watertight


def test_euler(ciclo: Ciclo) -> None:
    mallas = releer_3mf(ciclo.salidas.ruta_3mf)
    assert mallas[NOMBRE_MARCADOR].euler_number == 2
    assert mallas[NOMBRE_CORTADOR].euler_number == 0


def test_dos_objetos_nombrados_apoyados_en_cero(ciclo: Ciclo) -> None:
    mallas = releer_3mf(ciclo.salidas.ruta_3mf)
    assert sorted(mallas) == [NOMBRE_CORTADOR, NOMBRE_MARCADOR]
    for malla in mallas.values():
        assert float(malla.bounds[0][2]) == pytest.approx(0.0, abs=1e-6)


def test_alturas_del_contrato(ciclo: Ciclo) -> None:
    mallas = releer_3mf(ciclo.salidas.ruta_3mf)
    assert float(mallas[NOMBRE_MARCADOR].bounds[1][2]) == pytest.approx(4.0, abs=1e-3)
    assert float(mallas[NOMBRE_CORTADOR].bounds[1][2]) == pytest.approx(10.0, abs=1e-3)


# ── RF-09 · RF-10: luz y secciones ─────────────────────────────────────────


def test_luz_minima(ciclo: Ciclo) -> None:
    r = ciclo.reporte
    assert r.luz_minima_real_mm >= r.luz_nominal_mm - 0.02


def test_secciones(ciclo: Ciclo) -> None:
    """Areas medidas sobre la MALLA contra las calculadas desde los poligonos 2D."""
    for seccion in ciclo.reporte.secciones:
        assert seccion.ok, (
            f"z={seccion.z_mm}: medida {seccion.area_medida_mm2:.3f} vs "
            f"esperada {seccion.area_esperada_mm2:.3f}"
        )


def test_seccion_por_encima_del_pie_es_solo_filo(ciclo: Ciclo) -> None:
    mallas = releer_3mf(ciclo.salidas.ruta_3mf)
    area_z5 = area_de_seccion(mallas[NOMBRE_CORTADOR], 5.0)
    area_z1 = area_de_seccion(mallas[NOMBRE_CORTADOR], 1.0)
    assert area_z1 > area_z5, "a z=1 tienen que estar filo y pie; a z=5 solo el filo"


# ── RF-08: modo cortante ───────────────────────────────────────────────────


def test_modo_cortante_produce_solo_el_cortador(tmp_path: Path) -> None:
    p, a = CutterParams(), AjustesMotor()
    arte = cargar_svg(FIXTURES / "circulo.svg", p, a)
    silueta = construir_silueta_sola(arte, p, a)
    cortador = construir_cortador_2d(silueta, p, a)
    escena = construir_escena(None, cortador, p)
    salidas = exportar(escena, tmp_path / "circulo.3mf")
    mallas = releer_3mf(salidas.ruta_3mf)
    assert sorted(mallas) == [NOMBRE_CORTADOR]
    assert mallas[NOMBRE_CORTADOR].is_watertight
    assert mallas[NOMBRE_CORTADOR].euler_number == 0


# ── puenteo de colisiones entre extremos ───────────────────────────────────


@pytest.mark.lento
def test_el_murcielago_cierra_en_anillo(tmp_path: Path) -> None:
    """Line art real vectorizado, con dos camaras detras de cuellos angostos.

    Antes del puenteo este mismo comando terminaba en `MallaNoManifold`: el
    cortador salia con euler -2, dos bolsillos ciegos cerrados por filo. Por eso
    NO se pasa `exigir_solido=False` — que `generar` no aborte es el punto del
    test, y la topologia se relee del archivo como en el resto de la bateria.
    """
    resultado = generar(FIXTURES / "murcielago.svg", Modo.CORTANTE, tmp_path / "murcielago.3mf")
    cortador = releer_3mf(resultado.ruta_3mf)[NOMBRE_CORTADOR]
    assert cortador.is_watertight
    assert cortador.euler_number == 0

    r = resultado.reporte
    assert r.colisiones_puenteadas == 2
    assert 150 < r.area_puenteada_mm2 < 250, r.area_puenteada_mm2
    assert any("puenteo" in aviso for aviso in r.advertencias), r.advertencias


# ── RF-19: lo poco imprimible advierte, no bloquea ─────────────────────────


def test_huecos_finos_advierten_no_bloquean(tmp_path: Path) -> None:
    """Con el umbral en 1 mm, el fixture tiene huecos por debajo.

    El archivo se escribe igual y el reporte lleva la advertencia con su numero.
    """
    p = CutterParams()
    a = AjustesMotor(umbral_hueco_imprimible_mm=1.0)
    ciclo = _correr(tmp_path / "finos.3mf", p, a, "lineart_ojos_llenos")
    assert ciclo.huecos.fraccion_bajo_umbral > 0
    assert ciclo.salidas.ruta_3mf.is_file()
    assert any("huecos entre trazos" in aviso for aviso in ciclo.reporte.advertencias)


# ── RF-22 · RF-23: STL y GLB ───────────────────────────────────────────────


def test_stl_describe_la_misma_geometria_que_el_3mf(ciclo: Ciclo) -> None:
    assert len(ciclo.salidas.rutas_stl) == 2
    mallas = releer_3mf(ciclo.salidas.ruta_3mf)
    for ruta in ciclo.salidas.rutas_stl:
        nombre = ruta.stem.rsplit("_", 1)[1]
        desde_stl = trimesh.load(str(ruta), file_type="stl")
        assert desde_stl.volume == pytest.approx(mallas[nombre].volume, rel=1e-6)


def test_cada_3mf_suelto_es_el_mismo_cuerpo_en_el_mismo_lugar(ciclo: Ciclo) -> None:
    """Separar los objetos no puede moverlos ni cambiarles la malla.

    Si un suelto viniera recentrado, abrir los dos en el slicer los dejaria
    superpuestos en vez de anidados — y no habria forma de darse cuenta hasta
    imprimir.
    """
    sueltos = ciclo.salidas.rutas_3mf_objeto
    assert len(sueltos) == 2, sueltos
    juntas = releer_3mf(ciclo.salidas.ruta_3mf)
    for ruta in sueltos:
        mallas = releer_3mf(ruta)
        assert len(mallas) == 1, f"{ruta.name} tiene que traer un solo cuerpo"
        nombre, malla = next(iter(mallas.items()))
        assert nombre == ruta.stem.rsplit("_", 1)[1], "el 3MF perdio el nombre del objeto"
        original = juntas[nombre]
        assert len(malla.faces) == len(original.faces)
        assert malla.is_watertight
        assert malla.bounds == pytest.approx(original.bounds, abs=1e-9), "se movio de lugar"


def test_sin_marcador_no_hay_3mf_sueltos(tmp_path: Path) -> None:
    """Con un solo cuerpo, el suelto seria una copia del combinado."""
    r = generar(FIXTURES / "estrella.svg", Modo.CORTANTE, tmp_path / "solo.3mf")
    assert r.rutas_3mf_objeto == ()


def test_glb_trae_material_pbr(ciclo: Ciclo) -> None:
    """El preview 3D tiene que verse como plastico impreso, no como un gris plano."""
    escena = trimesh.load(str(ciclo.salidas.ruta_glb), file_type="glb", force="scene")
    assert sorted(escena.geometry) == [NOMBRE_CORTADOR, NOMBRE_MARCADOR]
    for malla in escena.geometry.values():
        material = malla.visual.material
        assert float(material.roughnessFactor) > 0.4
        assert float(material.metallicFactor) == pytest.approx(0.0, abs=1e-6)


# ── robustez ante parametros no-default ────────────────────────────────────


@pytest.mark.parametrize(
    "pie_alto,filo_alto,secciones_esperadas",
    [
        (2.0, 10.0, 3),  # defaults: z = 1,00 / 5,00 / 9,50, los del contrato
        (1.0, 3.0, 3),  # filo corto: con alturas fijas, z=5 caeria fuera de la malla
        (2.0, 2.0, 1),  # pie tan alto como el filo: no hay zona de "solo filo"
        (4.0, 2.0, 1),  # pie mas alto que el filo: el filo queda contenido
    ],
)
def test_secciones_se_derivan_de_los_parametros(
    tmp_path: Path, pie_alto: float, filo_alto: float, secciones_esperadas: int
) -> None:
    """Las alturas de muestreo no pueden estar hardcodeadas.

    La expectativa de area depende de en que zona cae cada altura, y eso lo
    define la relacion entre `pie_alto_mm` y `filo_alto_mm`, que el usuario edita.
    """
    p = CutterParams(pie_alto_mm=pie_alto, filo_alto_mm=filo_alto)
    resultado = generar(FIXTURES / "circulo.svg", Modo.CORTANTE, tmp_path / "c.3mf", params=p)
    assert len(resultado.reporte.secciones) == secciones_esperadas
    for seccion in resultado.reporte.secciones:
        assert seccion.ok, f"z={seccion.z_mm}: desvio {seccion.desvio_relativo * 100:.2f} %"
    assert all(t.ok for t in resultado.reporte.topologias)


def test_alturas_con_defaults_son_las_del_contrato(tmp_path: Path) -> None:
    resultado = generar(
        FIXTURES / "circulo.svg", Modo.CORTANTE, tmp_path / "c.3mf", params=CutterParams()
    )
    assert [round(s.z_mm, 3) for s in resultado.reporte.secciones] == [1.0, 5.0, 9.5]


# ── el reporte en si ───────────────────────────────────────────────────────


def test_el_reporte_declara_lo_que_no_prueba(ciclo: Ciclo) -> None:
    texto = render_texto(ciclo.reporte)
    assert "que NO prueba este reporte" in texto
    assert "superficie-a-superficie" in texto


def test_el_reporte_pasa_limpio(ciclo: Ciclo) -> None:
    assert ciclo.reporte.conteos_coinciden
    assert ciclo.reporte.desvio_acotado
    assert ciclo.reporte.todo_ok


def test_el_veredicto_distingue_advertencias_de_limpio(ciclo: Ciclo) -> None:
    """Tres estados, no dos: pasar con advertencias no es pasar limpio."""
    texto = render_texto(ciclo.reporte)
    if not ciclo.reporte.todo_ok:
        assert "NO VERIFICADO" in texto
    elif ciclo.reporte.advertencias:
        assert "VERIFICADO CON ADVERTENCIAS" in texto
    else:
        assert "=== Fidelidad: VERIFICADO ===" in texto


def test_el_desvio_pesa_en_el_veredicto(ciclo: Ciclo) -> None:
    """Un contorno corrido de mas tiene que bajar el veredicto, no solo el test."""
    corrido = replace(
        ciclo.reporte,
        desvio_max_mm=ciclo.reporte.dilatacion_aplicada_mm + 1.0,
    )
    assert not corrido.desvio_acotado
    assert not corrido.todo_ok
    assert "NO VERIFICADO" in render_texto(corrido)
