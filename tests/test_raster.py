"""F1 (conversion a JPG) y F2 (correccion de lineas), mas la vectorizacion."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scipy.ndimage import convolve, label
from skimage.morphology import disk, erosion, medial_axis, opening

from cutter3d.errors import ImagenInvalida, ParametroFueraDeRango
from cutter3d.params import AjustesMotor, CutterParams
from cutter3d.raster import (
    ANCHO_MINIMO_NORMALIZACION_PX,
    FACTOR_MACIZO,
    MAX_PIXELES_TRABAJO,
    SEMILLA_EJE,
    VECINDAD,
    _contornear_macizos,
    _macizos,
    _normalizar_trazo,
    convertir_a_jpg,
    escala_px_por_mm,
    factor_de_ampliacion,
    guardar_binaria,
    guardar_editable,
    preparar_lineas,
)
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


def test_el_contorneado_es_morfologia_por_disco_exacta_en_tiempo_lineal() -> None:
    """`_macizos` y el anillo dan EXACTAMENTE lo que `opening`/`erosion` con `disk(r)`.

    Se calculan con transformadas de distancia y no con el footprint porque el
    radio crece con la grilla: normalizando buzz a x5 el disco de apertura es de
    radio 14 sobre 2,6 MP, y con footprint el contorneado tardaba **4,1 s de
    los 5,1** de toda la etapa (medido). La transformada es O(pixeles) sin
    importar el radio. Pero es una optimizacion y no un cambio de criterio:
    erosion por disco es "ningun fondo a distancia <= r" y dilatacion es "algun
    pixel a distancia <= r", que es literalmente lo que la transformada
    responde — por eso el assert es igualdad pixel a pixel y no aproximada.
    """
    generador = np.random.default_rng(3)
    y, x = np.ogrid[:120, :160]
    tinta = np.zeros((120, 160), dtype=bool)
    for cx, cy, r in ((40, 60, 22), (110, 50, 9), (120, 90, 15)):
        tinta |= (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    tinta[20:24, 10:150] = True  # un trazo
    tinta |= generador.random(tinta.shape) < 0.01  # motas, para los bordes raros
    for ancho in (2.0, 4.5, 7.0):
        radio_apertura = max(1, round(FACTOR_MACIZO * ancho))
        esperado = np.asarray(opening(tinta, disk(radio_apertura)), dtype=bool)
        assert np.array_equal(_macizos(tinta, ancho), esperado), ancho

        nucleo = np.asarray(erosion(esperado, disk(max(1, round(ancho)))), dtype=bool)
        anillo_esperado = (tinta & ~esperado) | (esperado & ~nucleo)
        contorneada, _, area = _contornear_macizos(tinta, ancho)
        assert np.array_equal(contorneada, anillo_esperado), ancho
        assert area == int(esperado.sum())

    # Los dos casos degenerados, que son los que la transformada de distancia
    # NO sabe resolver sola: `distance_transform_edt` sin un solo cero devuelve
    # la distancia a un punto fantasma pegado a la esquina superior izquierda
    # en vez de fallar (medido: 1, 1,41, 2,24... desde (0, 0)). Un dibujo sin manchas
    # —la erosion no deja nada y hay que dilatar una mascara vacia— pintaba un
    # cuarto de disco de "macizo" en la esquina, y de ahi un arquito de tinta
    # que no estaba en el dibujo. Ya paso, y se llevo la escala con el.
    solo_trazos = np.zeros((60, 80), dtype=bool)
    solo_trazos[10:13, 5:75] = True
    solo_trazos[30:50, 40:43] = True
    assert not _macizos(solo_trazos, 4.0).any()
    intacta, zonas, area = _contornear_macizos(solo_trazos, 4.0)
    assert np.array_equal(intacta, solo_trazos) and zonas == 0 and area == 0

    todo = np.ones((30, 40), dtype=bool)
    assert np.array_equal(_macizos(todo, 4.0), np.asarray(opening(todo, disk(3)), dtype=bool))


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


def test_la_copia_editable_es_jpg_y_vuelve_a_dar_la_misma_mascara(
    jpg_de_prueba: Path, tmp_path: Path
) -> None:
    """El JPG que se baja para retocar a mano no es la salida, pero tiene que ser fiel.

    Es el archivo que el usuario abre en Paint y vuelve a subir a Correcto, y
    Correcto lo va a umbralizar de nuevo: si la compresion moviera un solo
    pixel del lado equivocado del umbral, la segunda correccion arrancaria de
    un dibujo distinto al que se le mostro. Con dos valores puros y calidad 95
    sin submuestreo, el ruido queda a decenas de niveles del medio y la mascara
    vuelve exacta.
    """
    resultado = preparar_lineas(jpg_de_prueba)
    ruta = guardar_editable(resultado, tmp_path / "editable.jpg")
    with Image.open(ruta) as imagen:
        assert imagen.format == "JPEG"
        assert imagen.size == resultado.tamano_usado
        releida = np.asarray(imagen.convert("L"))
    assert np.array_equal(releida <= 128, resultado.imagen == 0)
    with pytest.raises(ImagenInvalida, match="JPG"):
        guardar_editable(resultado, tmp_path / "editable.png")


# ── Normalizacion del ancho de trazo ───────────────────────────────────────
#
# El fixture son tres lineas de 2, 4 y 6 px sobre un ancho de tinta de 260 px.
# Los numeros no son arbitrarios:
#
# - La mediana del ancho da 4 px, asi que el detector de macizos abre con un
#   disco de radio 3 (diametro 7) y **ninguna de las tres califica**: el caso
#   queda limpio de contorneado y mide solo lo que se quiere medir.
# - Con `lado_mayor_mm=52` la escala es 260/52 = 5 px/mm, asi que el objetivo de
#   1 mm cae en 5 px — justo en el medio de las tres. La de 2 px tiene que
#   engordar y la de 6 px tiene que afinarse: las dos direcciones en un dibujo.

FILAS_FINA = slice(25, 60)
FILAS_MEDIA = slice(85, 120)
FILAS_GRUESA = slice(145, 185)
LADO_MAYOR_DEL_FIXTURE_MM = 52.0
"""Deja el objetivo de 1 mm en exactamente 5 px. Ver el comentario de arriba."""


def _tres_anchos() -> np.ndarray:
    """Tres lineas horizontales de 2, 4 y 6 px, todas de 260 px de largo."""
    lienzo = np.full((ALTO, ANCHO), 255, dtype=np.uint8)
    lienzo[40:42, 30:290] = 0
    lienzo[100:104, 30:290] = 0
    lienzo[160:166, 30:290] = 0
    return lienzo


@pytest.fixture
def jpg_tres_anchos(tmp_path: Path) -> Path:
    origen = tmp_path / "tres.png"
    Image.fromarray(_tres_anchos(), mode="L").save(origen)
    return convertir_a_jpg(origen, tmp_path / "tres.jpg")


def _ancho_de_linea(imagen: np.ndarray, filas: slice, columna: int = 160, factor: int = 1) -> float:
    """Tinta en una columna, dentro de la franja de una linea, en px ORIGINALES.

    `factor` es el `factor_ampliacion` del resultado: la normalizacion trabaja
    y entrega en una grilla mas fina, asi que la franja y la columna se escalan
    y la cuenta se divide, para que los asserts sigan hablando en los pixeles
    con los que se dibujo el fixture.
    """
    franja = slice(filas.start * factor, filas.stop * factor)
    return np.count_nonzero(imagen[franja, columna * factor] == 0) / factor


def test_f2_no_normaliza_por_default(jpg_tres_anchos: Path) -> None:
    """Apagada, la etapa se comporta exactamente como antes y no declara nada.

    Es la mitad del contrato de la opcion: quien no la pide no paga el segundo
    eje medial ni se lleva el dibujo modificado.
    """
    resultado = preparar_lineas(jpg_tres_anchos)
    assert resultado.normalizacion_activa is False
    assert resultado.ancho_objetivo_px == 0.0
    assert resultado.ancho_logrado_px == 0
    assert resultado.area_engrosada_px == 0
    assert resultado.area_afinada_px == 0
    imagen = resultado.imagen
    assert _ancho_de_linea(imagen, FILAS_FINA) == 2
    assert _ancho_de_linea(imagen, FILAS_GRUESA) == 6


def test_f2_normalizar_deja_las_tres_lineas_al_mismo_ancho(jpg_tres_anchos: Path) -> None:
    """Lo que la opcion promete: 2, 4 y 6 px entran y salen los tres iguales.

    Con un objetivo de 5 px la etapa amplia x4 (16 / 5 → 4) y entrega a
    1280x960, asi que las tres se miden en la grilla ampliada y se vuelven a
    pixeles del fixture. El ancho logrado es impar en la grilla de trabajo —21
    px, 5,25 originales— y ese es el numero que se declara.
    """
    resultado = preparar_lineas(
        jpg_tres_anchos, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DEL_FIXTURE_MM
    )
    k = resultado.factor_ampliacion
    assert k == 4
    anchos = {
        _ancho_de_linea(resultado.imagen, franja, factor=k)
        for franja in (FILAS_FINA, FILAS_MEDIA, FILAS_GRUESA)
    }
    assert len(anchos) == 1
    (unico,) = anchos
    assert unico * k == resultado.ancho_logrado_px
    assert unico == pytest.approx(5.0, abs=0.5)


def test_f2_normalizar_engorda_y_tambien_afina(jpg_tres_anchos: Path) -> None:
    """Las dos direcciones se aplican y se declaran por separado.

    Afinar es lo que F3 **no** puede hacer —su dilatacion solo engorda— y es la
    unica razon por la que esta etapa existe. Un cambio que dejara `afinada` en
    cero pasaria igual mirando solo la mediana, y seria justo el que rompe el
    caso del contorno grueso.
    """
    resultado = preparar_lineas(
        jpg_tres_anchos, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DEL_FIXTURE_MM
    )
    assert resultado.area_engrosada_px > 0
    assert resultado.area_afinada_px > 0


def test_f2_el_objetivo_sale_de_los_dos_milimetros(jpg_tres_anchos: Path) -> None:
    """1 mm sobre una pieza de 52 mm, con 260 px de tinta, son 5 px."""
    resultado = preparar_lineas(
        jpg_tres_anchos,
        normalizar_trazo=True,
        ancho_trazo_mm=1.0,
        lado_mayor_mm=LADO_MAYOR_DEL_FIXTURE_MM,
    )
    # En pixeles de la grilla de trabajo, que es donde estan todas las medidas
    # del resultado; dividido por el factor vuelve a los 5 px del fixture.
    k = resultado.factor_ampliacion
    assert resultado.ancho_objetivo_px / k == pytest.approx(5.0, abs=0.1)
    assert resultado.ancho_objetivo_mm == 1.0
    assert resultado.lado_mayor_supuesto_mm == LADO_MAYOR_DEL_FIXTURE_MM
    assert resultado.px_por_mm / k == pytest.approx(5.0, abs=0.1)


def test_f2_una_pieza_mas_grande_pide_un_trazo_mas_fino(jpg_tres_anchos: Path) -> None:
    """El objetivo en pixeles es inverso al tamaño de la pieza, no una constante.

    La misma imagen para una pieza del doble de lado tiene que dar la mitad de
    pixeles por milimetro: es toda la traduccion que hace esta etapa.
    """
    chica = preparar_lineas(jpg_tres_anchos, normalizar_trazo=True, lado_mayor_mm=52.0)
    grande = preparar_lineas(jpg_tres_anchos, normalizar_trazo=True, lado_mayor_mm=104.0)
    # Cada una elige su propio factor de ampliacion (la grande necesita mas),
    # asi que la proporcion se compara en pixeles originales.
    objetivo_chica = chica.ancho_objetivo_px / chica.factor_ampliacion
    objetivo_grande = grande.ancho_objetivo_px / grande.factor_ampliacion
    assert objetivo_grande == pytest.approx(objetivo_chica / 2, abs=0.1)


def test_f2_la_escala_sale_de_la_tinta_y_no_del_lienzo(tmp_path: Path) -> None:
    """El margen blanco alrededor del dibujo no viaja a la pieza.

    Medir sobre el lienzo haria que la misma figura pidiera un trazo mas fino
    solo por estar escaneada con mas borde, y el resultado dependeria del
    encuadre en vez del dibujo. Es la razon de `_lado_mayor_tinta_px`.
    """
    apretado = _tres_anchos()
    holgado = np.full((ALTO * 2, ANCHO * 2), 255, dtype=np.uint8)
    holgado[100 : 100 + ALTO, 100 : 100 + ANCHO] = apretado

    objetivos = []
    for nombre, lienzo in (("apretado", apretado), ("holgado", holgado)):
        origen = tmp_path / f"{nombre}.png"
        Image.fromarray(lienzo, mode="L").save(origen)
        jpg = convertir_a_jpg(origen, tmp_path / f"{nombre}.jpg")
        resultado = preparar_lineas(
            jpg, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DEL_FIXTURE_MM
        )
        # El lienzo holgado tiene 4 veces mas pixeles y le toca otro factor de
        # ampliacion: se compara en pixeles originales, que es lo que la caja
        # de la tinta tiene que dejar igual.
        objetivos.append(resultado.ancho_objetivo_px / resultado.factor_ampliacion)

    assert objetivos[0] == pytest.approx(objetivos[1], abs=0.1)


def test_escala_px_por_mm_es_cero_sin_tinta() -> None:
    """Un lienzo en blanco no tiene escala, y la cuenta no puede dividir por cero."""
    assert escala_px_por_mm(np.zeros((10, 10), dtype=bool), 90.0) == 0.0


def test_f2_un_dibujo_chico_se_amplia_hasta_que_el_trazo_tenga_cuerpo(
    jpg_de_prueba: Path,
) -> None:
    """Un dibujo de 260 px para una pieza de 90 mm trae el objetivo en 2,9 px.

    Reconstruir eso desde un eje de un pixel deja la linea temblorosa —medio
    pixel de error sobre 3 es un 17%—, asi que la etapa amplia hasta que el
    objetivo llegue a `ANCHO_MINIMO_NORMALIZACION_PX`, trabaja y entrega a esa
    escala, y lo declara. Es lo unico de F2 que agranda una imagen.
    """
    resultado = preparar_lineas(jpg_de_prueba, normalizar_trazo=True)
    k = resultado.factor_ampliacion
    assert k > 1
    assert resultado.fue_ampliada and not resultado.fue_reducida
    assert resultado.ancho_objetivo_px >= ANCHO_MINIMO_NORMALIZACION_PX
    assert 2.0 < resultado.ancho_objetivo_px / k < 3.0
    ancho, alto = resultado.tamano_original
    assert resultado.tamano_usado == (ancho * k, alto * k)
    assert resultado.imagen.shape == (alto * k, ancho * k)


def test_f2_sin_normalizar_no_amplia(jpg_de_prueba: Path) -> None:
    """La ampliacion es de la normalizacion y de nadie mas.

    Quien no la pide se lleva el dibujo en la escala en que lo trajo — es la
    mitad del contrato de la opcion, igual que no pagar el segundo eje medial.
    """
    resultado = preparar_lineas(jpg_de_prueba)
    assert resultado.factor_ampliacion == 1
    assert not resultado.fue_ampliada
    assert resultado.tamano_usado == resultado.tamano_original


def test_factor_de_ampliacion_llega_al_minimo_sin_pasar_el_presupuesto() -> None:
    """Se amplia lo justo para llegar al minimo, y nunca por encima de los 3 MP.

    Los tres casos son los tres dibujos de referencia: buzz (339x307, objetivo
    3,5 px) entra entero a x5; el murcielago (740x740, 7,3 px) pediria x3 pero
    el presupuesto solo deja x2; una foto que ya esta en el presupuesto no se
    toca aunque el trazo venga fino. Y sin tinta no hay escala: factor 1.
    """
    buzz = np.zeros((307, 339), dtype=bool)
    k = factor_de_ampliacion(buzz, 3.5)
    assert k == 5
    assert 3.5 * k >= ANCHO_MINIMO_NORMALIZACION_PX
    assert k * k * buzz.size <= MAX_PIXELES_TRABAJO

    murcielago = np.zeros((740, 740), dtype=bool)
    assert factor_de_ampliacion(murcielago, 7.26) == 2  # x3 se pasaria de 3 MP

    foto = np.zeros((1500, 2000), dtype=bool)
    assert factor_de_ampliacion(foto, 3.5) == 1

    assert factor_de_ampliacion(buzz, 0.0) == 1
    assert factor_de_ampliacion(buzz, 20.0) == 1  # ya tiene cuerpo


def test_normalizar_trazo_redondea_el_medio_ancho_en_vez_de_truncarlo() -> None:
    """Objetivo 2,89 px → ancho 3, no 1.

    Es el caso degradado de cuando el presupuesto no deja ampliar —una foto de
    3 MP con un trazo finisimo— y por eso se prueba sobre la funcion directa:
    con la ampliacion, un dibujo chico ya no llega aca con ese objetivo.
    Truncar `(2,89 - 1) / 2 = 0,94` a 0 dejaba todo el arte en el esqueleto
    pelado, de un pixel.
    """
    tinta = np.zeros((40, 60), dtype=bool)
    tinta[18:22, 5:55] = True
    assert _normalizar_trazo(tinta, 2.89, None).ancho_logrado_px == 3


def test_f2_normalizar_respeta_los_macizos_con_el_contorneado_apagado(
    jpg_de_prueba: Path,
) -> None:
    """Sin contorneado, una mancha se deja como esta y se declara cuanta.

    El eje medial de un disco macizo es un arbolito: reconstruirlo con radio
    fijo lo convertiria en una figura de palitos, que es exactamente lo que el
    usuario evita cuando apaga el contorneado.
    """
    resultado = preparar_lineas(jpg_de_prueba, contornear_macizos=False, normalizar_trazo=True)
    k = resultado.factor_ampliacion
    assert resultado.imagen[150 * k, 160 * k] == 0  # el centro del disco sigue lleno
    assert resultado.area_protegida_px > 0


def test_f2_normalizar_afina_el_anillo_que_dejo_el_contorneado(jpg_de_prueba: Path) -> None:
    """Con contorneado encendido no hay nada protegido: el anillo tambien se empareja.

    Fija el orden de las dos etapas. Si la normalizacion corriera primero, la
    mancha llegaria al contorneado ya convertida en palitos; y si los macizos se
    protegieran igual, el anillo quedaria con el ancho viejo, que es el caso del
    contorno grueso que la opcion vino a resolver.
    """
    resultado = preparar_lineas(jpg_de_prueba, normalizar_trazo=True)
    assert resultado.zonas_contorneadas >= 1
    assert resultado.area_protegida_px == 0
    assert resultado.area_afinada_px > 0


def test_f2_normalizar_sigue_siendo_binario_puro(jpg_tres_anchos: Path) -> None:
    resultado = preparar_lineas(
        jpg_tres_anchos, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DEL_FIXTURE_MM
    )
    assert set(np.unique(resultado.imagen).tolist()) <= {0, 255}


# ── El ruido de la reconstruccion ──────────────────────────────────────────
#
# El fixture de las tres lineas no sirve para esto: son rectas horizontales, y
# el ruido de la reconstruccion aparece justo donde ellas no tienen nada —en las
# curvas, donde el eje medial avanza en escalera y el disco discreto que lo
# redibuja deja el borde dentado—. Esta onda de grosor variable trae las tres
# cosas que faltaban, y ninguna es decorativa:
#
# - **La curva**, que produce el dentado.
# - **La espiga**: una tilde de 7x2 px pegada al costado de la onda. Sin podar,
#   la reconstruccion la convierte en un bulto del ancho entero del trazo.
# - **El puntito suelto** de 3x3, mas chico que la poda, que sin el rescate de
#   `_podar_espigas` desapareceria del dibujo sin que nadie lo declare.
#
# Y trae ademas, de arriba, el desempate que hace falta para el test de
# repetibilidad: el grosor que cambia de a poco deja al eje medial con empates,
# que es lo que `medial_axis` resuelve al azar cuando no se le fija `rng`. Una
# recta o una curva de grosor constante no los tiene y el test pasaria solo.
#
# Los numeros: 263 px de tinta sobre 38 mm dan un objetivo de 6,9 px, o sea un
# radio de reconstruccion de 3 —el dentado necesita radio >= 2 para verse—, y el
# grosor de 3 a 5 px deja la mediana en 4,47, con lo que el detector de macizos
# abre con un disco de diametro 7 y no califica ninguna parte de la onda: el
# caso mide normalizacion y nada mas (`zonas_contorneadas` da 0).

LADO_MAYOR_DE_LA_ONDA_MM = 38.0
"""Deja el objetivo en 6,9 px (radio 3). Ver el comentario de arriba."""

VENTANA_DE_LA_ESPIGA = (slice(60, 72), slice(146, 158))
"""El rectangulo que hay encima de la onda, donde asoma la espiga.

La onda pasa por la fila 76 a la altura de la columna 150, asi que esta ventana
cae **arriba** de ella: lo unico que puede entrar es la espiga. Con la tinta
original hay 14 px ahi dentro y engordar la onda a 7 px suma uno; convertir la
espiga en un bulto sube a 32."""


def _onda_con_ruido() -> np.ndarray:
    """Onda de grosor variable, con una espiga pegada y un puntito suelto."""
    lienzo = np.full((ALTO, ANCHO), 255, dtype=np.uint8)
    for t in range(260):
        x = 30 + t
        y = int(110 + 45 * np.sin(t / 22))
        grosor = 4 + int(np.sin(t / 7))
        lienzo[y : y + grosor, x : x + grosor] = 0
    lienzo[70:77, 150:152] = 0
    lienzo[215:218, 40:43] = 0
    return lienzo


@pytest.fixture
def jpg_onda(tmp_path: Path) -> Path:
    origen = tmp_path / "onda.png"
    Image.fromarray(_onda_con_ruido(), mode="L").save(origen)
    return convertir_a_jpg(origen, tmp_path / "onda.jpg")


def _dientes(tinta: np.ndarray) -> int:
    """Pixeles de tinta con 5 o mas vecinos de fondo: puntas, bultos y dentado.

    Es un proxy del ruido que se mide sobre el raster y no sobre el SVG, para no
    atar la suite a la version de vtracer. Correlaciona con lo que importa: en
    los tres dibujos de referencia, la version sin podar ni limar subia este
    numero y subia en la misma direccion los nodos del path trazado.
    """
    fondo = convolve((~tinta).astype(np.uint8), VECINDAD, mode="constant")
    return int(np.count_nonzero(tinta & (fondo >= 5)))


def _anchos_del_trazo(tinta: np.ndarray) -> np.ndarray:
    ejes, distancia = medial_axis(tinta, return_distance=True, rng=SEMILLA_EJE)
    return 2.0 * np.asarray(distancia)[np.asarray(ejes, dtype=bool)]


def test_f2_normalizar_no_deja_el_trazo_mas_dentado_que_el_dibujo(jpg_onda: Path) -> None:
    """El invariante que faltaba: emparejar el ancho no puede ensuciar la linea.

    Reconstruir a secas sale ruidoso —espigas convertidas en bultos y el borde
    dentado por el disco discreto— y eso viaja hasta el marcador impreso como
    vibracion del cabezal. Medido en este fixture: la tinta original tiene 8
    dientes, la reconstruccion sin limar **16**, y completa quedan 4.

    Se compara el conteo absoluto aunque el resultado este en la grilla
    ampliada (x3), y eso es a proposito conservador: el borde ampliado es tres
    veces mas largo, asi que tener MENOS dientes en total es una vara mas alta
    que tener menos por unidad de largo.

    El assert se compara contra la **propia entrada** y no contra una constante
    a proposito: lo que hay que sostener es que la etapa no agrega ruido, y eso
    no depende de cuanto ruido traiga el dibujo.
    """
    original = preparar_lineas(jpg_onda).imagen == 0
    normalizada = (
        preparar_lineas(
            jpg_onda, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DE_LA_ONDA_MM
        ).imagen
        == 0
    )
    assert _dientes(normalizada) <= _dientes(original)


def test_f2_normalizar_no_deja_tramos_mas_finos_que_los_que_recibio(jpg_onda: Path) -> None:
    """Emparejar hacia arriba no puede dejar tramos por debajo de donde arranco.

    Es la otra cara del dentado y el que delata el caso que motivo todo esto: la
    version sin limar llevaba la mediana a 6,3 px con un objetivo de 6,9 pero
    **dejaba el percentil 5 en 2,0** —mas fino que los 4,0 de la entrada—,
    porque en las diagonales el disco de radio 3 no llega a cerrar la escalera.
    Un marcador con esos tramos se dobla, que es exactamente lo que la opcion
    vino a evitar.
    """
    original = preparar_lineas(jpg_onda).imagen == 0
    resultado = preparar_lineas(
        jpg_onda, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DE_LA_ONDA_MM
    )
    # A x3 el objetivo de 6,9 px son 20,7, y el impar mas cercano es 21.
    k = resultado.factor_ampliacion
    assert k == 3
    assert resultado.ancho_logrado_px == 21
    fino_antes = float(np.percentile(_anchos_del_trazo(original), 5))
    fino_despues = float(np.percentile(_anchos_del_trazo(resultado.imagen == 0), 5)) / k
    assert fino_despues >= fino_antes


def test_f2_normalizar_deja_el_ancho_parejo_a_lo_largo_del_trazo(jpg_onda: Path) -> None:
    """Lo que se ve como "linea temblorosa" es el ancho variando a lo largo.

    Se mide como `(p95 - p5) / p50` del ancho local sobre el resultado. En la
    onda: la tinta original da 0,37; reconstruir en la grilla nativa —con poda
    y limada— deja 0,14; en la grilla ampliada x3 queda **0,056**. La
    ampliacion es lo que baja el ultimo escalon y por eso el tope va en 0,10:
    sin ampliar no pasa, y sin limar da 0,71. Es el test que fija que la
    resolucion de trabajo forma parte del arreglo y no es una optimizacion.
    """
    resultado = preparar_lineas(
        jpg_onda, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DE_LA_ONDA_MM
    )
    p5, p50, p95 = np.percentile(_anchos_del_trazo(resultado.imagen == 0), [5, 50, 95])
    assert (p95 - p5) / p50 <= 0.10


def test_f2_normalizar_no_se_come_las_piezas_mas_chicas_que_la_poda(jpg_onda: Path) -> None:
    """El puntito de 3x3 sobrevive a una poda de 3 pasos.

    La poda saca las puntas del eje `medio_ancho` veces, y una pieza cuyo eje es
    mas corto que eso se queda sin un solo pixel: se evapora. Perder arte en
    silencio es lo unico que esta etapa no puede hacer —todo lo que toca lo
    declara—, asi que las piezas que quedarian vacias se restauran enteras.
    """
    original = preparar_lineas(jpg_onda).imagen == 0
    resultado = preparar_lineas(
        jpg_onda, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DE_LA_ONDA_MM
    )
    normalizada = resultado.imagen == 0
    etiquetas, cuantas = label(original, structure=VECINDAD)
    # Las piezas se etiquetan en la grilla original y se llevan a la de trabajo
    # repitiendo cada pixel k x k, que es exactamente lo que la ampliacion hace
    # con la posicion de las cosas.
    k = resultado.factor_ampliacion
    etiquetas_k = np.kron(etiquetas, np.ones((k, k), dtype=etiquetas.dtype))
    vacias = [n for n in range(1, cuantas + 1) if not (normalizada & (etiquetas_k == n)).any()]
    assert cuantas == 2  # la onda con su espiga, y el puntito suelto
    assert vacias == []


def test_f2_normalizar_no_convierte_una_espiga_en_un_bulto(jpg_onda: Path) -> None:
    """Una tilde de 2 px de ancho no puede salir con el ancho entero del trazo.

    Es lo que hace la poda y no lo tapa la limada: cada irregularidad del borde
    le cuelga una ramita al eje medial, y reconstruir esa ramita con el radio
    fijo la infla al ancho del trazo. Aca la espiga es explicita para poder
    medirla, pero en un line art real salen de la compresion del JPG y son
    docenas —172 bifurcaciones en `tests/buzz-lightyear.jpg`—.

    Escala con el radio, asi que este fixture muestra la version chica del
    problema: con radio 3 la poda saca 58 px de bulto, y a 3 MP —donde el radio
    da 8— saca 10.076 px y baja los nodos del SVG trazado un 15%.
    """
    original = preparar_lineas(jpg_onda).imagen == 0
    resultado = preparar_lineas(
        jpg_onda, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DE_LA_ONDA_MM
    )
    normalizada = resultado.imagen == 0
    # La ventana se lleva a la grilla de trabajo y la cuenta se vuelve a
    # pixeles originales dividiendo por k^2: sin eso el rectangulo cae en otra
    # parte del dibujo y el test pasa mirando cualquier cosa.
    k = resultado.factor_ampliacion
    filas, columnas = VENTANA_DE_LA_ESPIGA
    ventana_k = (
        slice(filas.start * k, filas.stop * k),
        slice(columnas.start * k, columnas.stop * k),
    )
    antes = int(original[VENTANA_DE_LA_ESPIGA].sum())
    despues = int(normalizada[ventana_k].sum()) / (k * k)
    # Engordar la onda de 4 a 7 px suma unos 3 px aca; el bulto suma 18.
    assert despues <= antes + 6


def test_f2_normalizar_da_lo_mismo_en_dos_corridas(jpg_onda: Path) -> None:
    """`medial_axis` desempata al azar si no se le pasa `rng`, y eso se declaraba.

    Sin la semilla, el eje de este fixture salio de 386 o 387 pixeles segun la
    corrida, y sobre `tests/buzz-lightyear.jpg` el `area_engrosada_px` declarado
    dio 227, 224 y 224 en tres corridas del mismo archivo. Un reporte que afirma
    cuanto se modifico el dibujo y cambia solo no afirma nada, asi que la
    repetibilidad es parte del contrato de la etapa y no una casualidad.
    """
    corridas = [
        preparar_lineas(jpg_onda, normalizar_trazo=True, lado_mayor_mm=LADO_MAYOR_DE_LA_ONDA_MM)
        for _ in range(3)
    ]
    for otra in corridas[1:]:
        assert np.array_equal(corridas[0].imagen, otra.imagen)
        assert otra.area_engrosada_px == corridas[0].area_engrosada_px
        assert otra.area_afinada_px == corridas[0].area_afinada_px


@pytest.mark.parametrize(
    "kwargs,campo",
    [
        ({"ancho_trazo_mm": 0.0}, "ancho_trazo_mm"),
        ({"ancho_trazo_mm": -1.0}, "ancho_trazo_mm"),
        ({"lado_mayor_mm": 0.0}, "lado_mayor_mm"),
        ({"lado_mayor_mm": 5000.0}, "lado_mayor_mm"),
    ],
)
def test_f2_los_limites_los_pone_cutterparams(
    jpg_tres_anchos: Path, kwargs: dict[str, float], campo: str
) -> None:
    """Los dos milimetros se validan con las mismas reglas que en el cortante.

    No hay una segunda tabla de limites en F2: `preparar_lineas` construye un
    `CutterParams` y deja que levante el error, con el nombre del campo adentro
    —que es lo que la web convierte en un 422 y pinta en rojo.
    """
    with pytest.raises(ParametroFueraDeRango) as excepcion:
        preparar_lineas(jpg_tres_anchos, **kwargs)
    assert excepcion.value.parametro == campo


# ── vectorizacion ──────────────────────────────────────────────────────────


def test_vectorizacion_produce_svg_parseable(jpg_de_prueba: Path, tmp_path: Path) -> None:
    svg = a_svg(jpg_de_prueba, tmp_path / "trazado.svg")
    assert svg.is_file()
    arte = cargar_svg(svg, CutterParams(), AjustesMotor())
    assert arte.n_contornos >= 1


def test_vectorizacion_rechaza_salida_que_no_sea_svg(jpg_de_prueba: Path, tmp_path: Path) -> None:
    with pytest.raises(ImagenInvalida, match="svg"):
        a_svg(jpg_de_prueba, tmp_path / "salida.png")
