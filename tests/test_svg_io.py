"""Parseo del SVG: transformaciones, even-odd, conteo de contornos y bordes.

Los numeros de conteo y aspecto son **golden master**: se capturaron de los
fixtures fijos y cualquier cambio en el parseo los mueve. Esa es la idea.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from cutter3d.errors import SvgInvalido
from cutter3d.params import AjustesMotor, CutterParams
from cutter3d.svg_io import Arte, cargar_svg

FIXTURES = Path(__file__).parent / "fixtures"
TOL = 1e-6


def cargar(nombre: str) -> Arte:
    return cargar_svg(FIXTURES / f"{nombre}.svg", CutterParams(), AjustesMotor())


def test_circulo_es_una_forma_sin_huecos() -> None:
    arte = cargar("circulo")
    assert arte.n_contornos == 1
    assert arte.n_huecos == 0


def test_estrella_es_una_forma_sin_huecos() -> None:
    arte = cargar("estrella")
    assert arte.n_contornos == 1
    assert arte.n_huecos == 0


def test_evenodd_convierte_el_contorno_interno_en_hueco() -> None:
    """La cabeza del line art es un anillo: exterior + interior con even-odd."""
    arte = cargar("lineart_ojos_llenos")
    assert arte.n_huecos == 1


def test_conteo_de_contornos_del_lineart() -> None:
    """9 y no 10: los dos trazos de la boca se tocan y la union los fusiona."""
    arte = cargar("lineart_ojos_llenos")
    assert arte.n_contornos == 9


@pytest.mark.parametrize("nombre", ["circulo", "estrella", "lineart_ojos_llenos"])
def test_lado_mayor_queda_en_el_objetivo(nombre: str) -> None:
    arte = cargar(nombre)
    minx, miny, maxx, maxy = arte.poligonos.bounds
    assert max(maxx - minx, maxy - miny) == pytest.approx(90.0, abs=1e-6)


@pytest.mark.parametrize("nombre", ["circulo", "estrella", "lineart_ojos_llenos"])
def test_arte_centrado_en_el_origen(nombre: str) -> None:
    arte = cargar(nombre)
    minx, miny, maxx, maxy = arte.poligonos.bounds
    assert (minx + maxx) / 2 == pytest.approx(0.0, abs=1e-6)
    assert (miny + maxy) / 2 == pytest.approx(0.0, abs=1e-6)


def test_transform_del_svg_se_aplica() -> None:
    """La estrella lleva `translate(300,300) rotate(15)`.

    Sin la rotacion el bbox de la estrella daria 90 x 85,59 (aspecto 0,951).
    Con la rotacion aplicada da 87,05 x 90 (aspecto 1,034). El aspecto es lo
    unico observable, porque el recentrado borra el efecto del translate.
    """
    arte = cargar("estrella")
    minx, miny, maxx, maxy = arte.poligonos.bounds
    aspecto = (maxy - miny) / (maxx - minx)
    assert aspecto == pytest.approx(1.0339, abs=0.002)
    assert aspecto != pytest.approx(0.951, abs=0.002)


def test_eje_y_espejado() -> None:
    """El SVG es y-abajo; el arte tiene que quedar y-arriba.

    Lo observable es la forma de los extremos: arriba estan las dos orejas (dos
    piezas separadas) y abajo el arco inferior de la cabeza (una sola pieza). Si
    no se espejara el eje Y, la relacion se daria al reves.
    """
    arte = cargar("lineart_ojos_llenos")
    minx, miny, maxx, maxy = arte.poligonos.bounds
    alto = maxy - miny
    franja_sup = box(minx, maxy - 0.02 * alto, maxx, maxy)
    franja_inf = box(minx, miny, maxx, miny + 0.02 * alto)
    assert _piezas(arte, franja_sup) == 2, "arriba tienen que estar las dos orejas"
    assert _piezas(arte, franja_inf) == 1, "abajo tiene que estar el arco de la cabeza"


def _piezas(arte: Arte, caja: Polygon) -> int:
    recorte = arte.poligonos.intersection(caja)
    if recorte.is_empty:
        return 0
    return len(recorte.geoms) if hasattr(recorte, "geoms") else 1


def test_svg_inexistente(tmp_path: Path) -> None:
    with pytest.raises(SvgInvalido, match="no existe"):
        cargar_svg(tmp_path / "no_esta.svg", CutterParams(), AjustesMotor())


def test_svg_solo_con_stroke_es_invalido(tmp_path: Path) -> None:
    """El contrato asume line art vectorizado: paths rellenos, sin stroke."""
    ruta = tmp_path / "trazado.svg"
    ruta.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        'viewBox="0 0 100 100">'
        '<path fill="none" stroke="#000" stroke-width="2" d="M10,10 L90,90"/>'
        "</svg>",
        encoding="utf-8",
    )
    with pytest.raises(SvgInvalido, match="stroke"):
        cargar_svg(ruta, CutterParams(), AjustesMotor())


def test_svg_sin_formas_es_invalido(tmp_path: Path) -> None:
    ruta = tmp_path / "vacio.svg"
    ruta.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        'viewBox="0 0 100 100"></svg>',
        encoding="utf-8",
    )
    with pytest.raises(SvgInvalido):
        cargar_svg(ruta, CutterParams(), AjustesMotor())
