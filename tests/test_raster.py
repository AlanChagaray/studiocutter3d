"""F1 (conversion a JPG) y F2 (correccion de lineas), mas la vectorizacion."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from cutter3d.errors import ImagenInvalida
from cutter3d.params import AjustesMotor, CutterParams
from cutter3d.raster import convertir_a_jpg, guardar_binaria, preparar_lineas
from cutter3d.svg_io import cargar_svg
from cutter3d.vector import a_svg

FIXTURES = Path(__file__).parent / "fixtures"
ANCHO, ALTO = 320, 240


def _dibujo_con_macizo_y_trazo() -> np.ndarray:
    """Lienzo blanco con un trazo fino (4 px) y un disco macizo (r=30 px)."""
    lienzo = np.full((ALTO, ANCHO), 255, dtype=np.uint8)
    lienzo[40:44, 30:290] = 0  # trazo horizontal fino
    y, x = np.ogrid[:ALTO, :ANCHO]
    lienzo[((x - 160) ** 2 + (y - 150) ** 2) <= 30**2] = 0  # macizo
    return lienzo


def _con_grises() -> np.ndarray:
    """El mismo dibujo, ensuciado con una rampa de grises y una sombra."""
    lienzo = _dibujo_con_macizo_y_trazo().astype(np.int16)
    rampa = np.linspace(0, 60, ANCHO, dtype=np.int16)[None, :]
    lienzo = np.clip(lienzo - rampa, 0, 255)
    lienzo[200:230, 20:120] = np.clip(lienzo[200:230, 20:120] - 70, 0, 255)
    return lienzo.astype(np.uint8)


@pytest.fixture
def jpg_de_prueba(tmp_path: Path) -> Path:
    origen = tmp_path / "origen.png"
    Image.fromarray(_con_grises(), mode="L").save(origen)
    return convertir_a_jpg(origen, tmp_path / "origen.jpg")


# ── RF-14 · RF-20 · RF-21: conversion a JPG ────────────────────────────────


@pytest.mark.parametrize("formato,extension", [("PNG", ".png"), ("WEBP", ".webp")])
def test_f1_conversion(tmp_path: Path, formato: str, extension: str) -> None:
    """RF-14 / RF-20: la salida es JPEG valido con las mismas dimensiones."""
    origen = tmp_path / f"entrada{extension}"
    Image.fromarray(_dibujo_con_macizo_y_trazo(), mode="L").convert("RGB").save(
        origen, format=formato
    )
    salida = convertir_a_jpg(origen, tmp_path / "salida.jpg")
    with Image.open(salida) as imagen:
        assert imagen.format == "JPEG"
        assert imagen.size == (ANCHO, ALTO)


def test_f1_acepta_jfif(tmp_path: Path) -> None:
    origen = tmp_path / "entrada.jfif"
    Image.fromarray(_dibujo_con_macizo_y_trazo(), mode="L").convert("RGB").save(
        origen, format="JPEG"
    )
    salida = convertir_a_jpg(origen, tmp_path / "salida.jpg")
    with Image.open(salida) as imagen:
        assert imagen.format == "JPEG"
        assert imagen.size == (ANCHO, ALTO)


def test_f1_convierte_svg(tmp_path: Path) -> None:
    """RF-21: el SVG se rasteriza con resvg respetando su tamaño declarado.

    `circulo.svg` declara width/height 600: la salida tiene que medir 600x600,
    igual que en los caminos raster. Comprobar solo que "es un JPEG" dejaria
    pasar un rasterizado a cualquier escala.
    """
    salida = convertir_a_jpg(FIXTURES / "circulo.svg", tmp_path / "circulo.jpg")
    with Image.open(salida) as imagen:
        assert imagen.format == "JPEG"
        assert imagen.size == (600, 600)


def test_f1_rechaza_formato_no_soportado(tmp_path: Path) -> None:
    """El fixture es PDF y no TIFF, y el motivo importa.

    Hasta que F1 acepto RAW y HEIC, el TIFF era el ejemplo obvio de "imagen que
    el motor no abre". Ahora SI lo abre —es el contenedor de ARW, CR2, NEF y
    DNG—, asi que hace falta un formato que siga afuera de verdad. El PDF lo
    esta, y ademas es de los que el conversor reconoce solo para poder nombrarlo
    al rechazarlo.
    """
    origen = tmp_path / "raro.pdf"
    origen.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    with pytest.raises(ImagenInvalida, match="no soportado"):
        convertir_a_jpg(origen, tmp_path / "salida.jpg")


def test_f1_aplana_transparencia_sobre_blanco(tmp_path: Path) -> None:
    origen = tmp_path / "transparente.png"
    Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(origen)
    salida = convertir_a_jpg(origen, tmp_path / "salida.jpg")
    with Image.open(salida) as imagen:
        assert np.asarray(imagen.convert("L")).min() > 240


# ── RF-15 · RF-16 · RF-17: correccion de lineas ────────────────────────────


def test_f2_binario_puro(jpg_de_prueba: Path) -> None:
    """RF-15: la salida contiene unicamente 0 y 255. Cero grises."""
    resultado = preparar_lineas(jpg_de_prueba)
    assert set(np.unique(resultado.imagen).tolist()) <= {0, 255}


def test_f2_contornea_por_default(jpg_de_prueba: Path) -> None:
    """RF-16: sin pasar flags, el macizo queda como anillo y se declara."""
    resultado = preparar_lineas(jpg_de_prueba)
    assert resultado.contorneado_activo is True
    assert resultado.zonas_contorneadas >= 1
    assert resultado.area_contorneada_px > 0
    # El centro del disco tiene que haber quedado en blanco.
    assert resultado.imagen[150, 160] == 255
    # Y su borde, en negro.
    assert resultado.imagen[150, 160 - 29] == 0


def test_f2_respeta_macizos_con_flag(jpg_de_prueba: Path) -> None:
    """RF-17: con el flag apagado el macizo queda intacto y no se declara nada."""
    resultado = preparar_lineas(jpg_de_prueba, contornear_macizos=False)
    assert resultado.contorneado_activo is False
    assert resultado.zonas_contorneadas == 0
    assert resultado.area_contorneada_px == 0
    assert resultado.imagen[150, 160] == 0  # el centro del disco sigue negro


def test_f2_conserva_el_trazo_fino(jpg_de_prueba: Path) -> None:
    """El contorneado solo toca macizos: el trazo fino no se altera."""
    con = preparar_lineas(jpg_de_prueba)
    sin = preparar_lineas(jpg_de_prueba, contornear_macizos=False)
    franja = slice(38, 46)
    assert np.array_equal(con.imagen[franja, 30:280], sin.imagen[franja, 30:280])


def test_f2_exige_jpg(tmp_path: Path) -> None:
    png = tmp_path / "entrada.png"
    Image.fromarray(_dibujo_con_macizo_y_trazo(), mode="L").save(png)
    with pytest.raises(ImagenInvalida, match="JPG"):
        preparar_lineas(png)


def test_f2_no_se_conforma_con_la_extension(tmp_path: Path) -> None:
    """Un PNG renombrado a .jpg tambien se rechaza: se mira el contenido."""
    disfrazado = tmp_path / "mentira.jpg"
    Image.fromarray(_dibujo_con_macizo_y_trazo(), mode="L").save(disfrazado, format="PNG")
    with pytest.raises(ImagenInvalida, match="PNG"):
        preparar_lineas(disfrazado)


def test_f2_umbral_manual(jpg_de_prueba: Path) -> None:
    resultado = preparar_lineas(jpg_de_prueba, umbral=100)
    assert resultado.umbral_usado == 100
    assert set(np.unique(resultado.imagen).tolist()) <= {0, 255}


def test_f2_se_guarda_en_png_no_en_jpg(jpg_de_prueba: Path, tmp_path: Path) -> None:
    """El contrato es explicito: el JPG vuelve a meter grises en el borde."""
    resultado = preparar_lineas(jpg_de_prueba)
    ruta = guardar_binaria(resultado, tmp_path / "lineas.png")
    with Image.open(ruta) as imagen:
        assert imagen.format == "PNG"
        assert set(np.unique(np.asarray(imagen)).tolist()) <= {0, 255}
    with pytest.raises(ImagenInvalida, match="PNG"):
        guardar_binaria(resultado, tmp_path / "lineas.jpg")


# ── vectorizacion ──────────────────────────────────────────────────────────


def test_vectorizacion_produce_svg_parseable(jpg_de_prueba: Path, tmp_path: Path) -> None:
    svg = a_svg(jpg_de_prueba, tmp_path / "trazado.svg")
    assert svg.is_file()
    arte = cargar_svg(svg, CutterParams(), AjustesMotor())
    assert arte.n_contornos >= 1


def test_vectorizacion_rechaza_salida_que_no_sea_svg(jpg_de_prueba: Path, tmp_path: Path) -> None:
    with pytest.raises(ImagenInvalida, match="svg"):
        a_svg(jpg_de_prueba, tmp_path / "salida.png")
