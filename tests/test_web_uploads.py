"""Subidas: tipo, tamaño y nombres hostiles.

El borde de entrada de la app. Lo que se prueba aca es que **nada de lo que
manda el cliente se usa tal cual**: ni el nombre, ni el `Content-Type`, ni el
tamaño declarado.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.almacen import AlmacenEnMemoria, TipoTrabajo
from app.archivos import FORMATOS_CONVERSOR, Formato, detectar_formato, guardar_subida
from app.config import Ajustes
from app.errores import ErrorApi
from cutter3d.errors import ImagenInvalida
from cutter3d.raster import MAX_PIXELES, _abrir_raw, convertir_a_jpg
from cutter3d.vector import a_svg

WEBP_MINIMO = (
    b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00\x30\x01\x00\x9d\x01\x2a"
    b"\x01\x00\x01\x00\x02\x00\x34\x25\xa4\x00\x03\x70\x00\xfe\xfb\xfd\x50\x00"
)
SVG_MINIMO = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<rect width="6" height="6"/></svg>'
)


def test_png_se_convierte_a_jpg(sesion: TestClient, png_minimo: bytes) -> None:
    r = sesion.post("/api/conversor", files={"archivo": ("d.png", png_minimo, "image/png")})
    assert r.status_code == 200
    trabajo = r.json()
    assert trabajo["estado"] == "listo"
    descarga = sesion.get(f"/api/trabajos/{trabajo['id']}/archivo/jpg")
    assert descarga.status_code == 200
    assert descarga.content.startswith(b"\xff\xd8\xff"), "no es un JPEG"
    assert descarga.headers["content-type"] == "image/jpeg"


def test_svg_tambien_entra_al_conversor(sesion: TestClient) -> None:
    r = sesion.post("/api/conversor", files={"archivo": ("d.svg", SVG_MINIMO, "image/svg+xml")})
    assert r.status_code == 200
    descarga = sesion.get(f"/api/trabajos/{r.json()['id']}/archivo/jpg")
    assert descarga.content.startswith(b"\xff\xd8\xff")


def test_webp_tambien_entra_al_conversor(sesion: TestClient) -> None:
    r = sesion.post("/api/conversor", files={"archivo": ("d.webp", WEBP_MINIMO, "image/webp")})
    assert r.status_code == 200


def test_el_conversor_tambien_convierte_a_svg(sesion: TestClient, jpg_minimo: bytes) -> None:
    """El destino lo elige el usuario: el mismo endpoint devuelve vectorial."""
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("d.jpg", jpg_minimo, "image/jpeg")},
        data={"formato": "svg"},
    )
    assert r.status_code == 200, r.text
    trabajo = r.json()
    assert trabajo["archivos"] == ["svg"], "no puede quedar tambien el jpg"
    assert trabajo["reporte"]["formato_destino"] == "svg"

    descarga = sesion.get(f"/api/trabajos/{trabajo['id']}/archivo/svg")
    assert descarga.status_code == 200
    assert b"<svg" in descarga.content[:512].lower()
    assert descarga.headers["content-type"] == "image/svg+xml"


def test_un_svg_pedido_como_svg_se_copia_sin_tocarlo(sesion: TestClient) -> None:
    """Revectorizar lo que ya es vectorial solo agregaria error."""
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("d.svg", SVG_MINIMO, "image/svg+xml")},
        data={"formato": "svg"},
    )
    assert r.status_code == 200
    descarga = sesion.get(f"/api/trabajos/{r.json()['id']}/archivo/svg")
    assert descarga.content == SVG_MINIMO, "el archivo tiene que salir identico"


def test_un_formato_de_destino_inventado_se_rechaza(sesion: TestClient, png_minimo: bytes) -> None:
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("d.png", png_minimo, "image/png")},
        data={"formato": "3mf"},
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("nombre", "medio"),
    [("d.png", "image/png"), ("d.jpg", "image/jpeg")],
)
def test_el_cortante_solo_acepta_svg(
    sesion: TestClient, png_minimo: bytes, jpg_minimo: bytes, nombre: str, medio: str
) -> None:
    """Un raster no entra: vectorizar es del Convertidor y de F2, y se ve."""
    contenido = png_minimo if nombre.endswith(".png") else jpg_minimo
    r = sesion.post("/api/cortante", files={"archivo": (nombre, contenido, medio)})
    assert r.status_code == 415
    assert r.json()["error"]["codigo"] == "formato_no_soportado"
    assert r.json()["error"]["mensaje"].endswith("Se aceptan: svg.")


def test_tipo_no_soportado_se_rechaza(sesion: TestClient) -> None:
    r = sesion.post("/api/conversor", files={"archivo": ("x.png", b"MZ\x90\x00" * 50, "image/png")})
    assert r.status_code == 415
    assert r.json()["error"]["codigo"] == "formato_no_soportado"


def test_el_content_type_mentido_no_alcanza(sesion: TestClient, png_minimo: bytes) -> None:
    """El formato sale de los bytes: declarar `image/jpeg` no convierte nada."""
    r = sesion.post("/api/lineas", files={"archivo": ("x.jpg", png_minimo, "image/jpeg")})
    assert r.status_code == 415, "un PNG disfrazado de JPG no puede entrar a F2"


def test_lineas_exige_jpg(sesion: TestClient) -> None:
    r = sesion.post("/api/lineas", files={"archivo": ("d.svg", SVG_MINIMO, "image/svg+xml")})
    assert r.status_code == 415


def test_sin_archivo_ni_origen_es_422(sesion: TestClient) -> None:
    r = sesion.post("/api/lineas", data={"contornear_macizos": "true"})
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "falta_archivo"


def test_archivo_mas_grande_que_el_limite_es_413(sesion: TestClient) -> None:
    """Corta el middleware por `Content-Length`, antes de escribir nada."""
    gordo = b"\x89PNG\r\n\x1a\n" + b"\x00" * (26 * 1024 * 1024)
    r = sesion.post("/api/conversor", files={"archivo": ("g.png", gordo, "image/png")})
    assert r.status_code == 413
    assert r.json()["error"]["codigo"] == "archivo_muy_grande"


def test_el_limite_tambien_se_controla_al_escribir(tmp_path: Path) -> None:
    """Segundo cinturon: un cliente que miente en el header igual no pasa."""
    flujo = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000)
    with pytest.raises(ErrorApi) as caja:
        guardar_subida(flujo, tmp_path, permitidos=FORMATOS_CONVERSOR, limite_bytes=1000)
    assert caja.value.estado == 413
    assert not list(tmp_path.iterdir()), "no puede quedar un archivo a medias en disco"


@pytest.mark.parametrize(
    "hostil",
    ["../../../evil.png", "..\\..\\evil.png", "/etc/passwd.png", "a" * 400 + ".png"],
)
def test_nombre_hostil_queda_dentro_del_directorio_del_trabajo(
    sesion: TestClient, ajustes: Ajustes, png_minimo: bytes, hostil: str
) -> None:
    r = sesion.post("/api/conversor", files={"archivo": (hostil, png_minimo, "image/png")})
    assert r.status_code == 200
    carpeta = ajustes.dir_trabajo / r.json()["id"]
    escritos = sorted(p.name for p in carpeta.iterdir())
    assert escritos == ["entrada.png", "salida.jpg"], escritos
    assert not (ajustes.dir_trabajo / "evil.png").exists()
    assert not (ajustes.dir_trabajo.parent / "evil.png").exists()


def test_no_se_puede_encadenar_desde_un_trabajo_ajeno(
    sesion: TestClient, almacen: AlmacenEnMemoria
) -> None:
    ajeno = almacen.crear("otro_usuario", TipoTrabajo.CONVERSOR)
    r = sesion.post("/api/lineas", data={"origen": ajeno.id})
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "trabajo_inexistente"


@pytest.mark.parametrize(
    ("bytes_", "esperado"),
    [
        (b"\x89PNG\r\n\x1a\n\x00", Formato.PNG),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", Formato.JPEG),
        (WEBP_MINIMO, Formato.WEBP),
        (SVG_MINIMO, Formato.SVG),
        (b"\xef\xbb\xbf  <?xml version='1.0'?><svg/>", Formato.SVG),
        (b"GIF87a", Formato.GIF),
        (b"GIF89a", Formato.GIF),
        (b"BM\x00\x00\x00\x00", Formato.BMP),
        (b"8BPS\x00\x01", Formato.PSD),
        (b"\x00\x00\x01\x00\x01\x00", Formato.ICO),
        # Los tres ISO-BMFF comparten los primeros bytes y solo los separa la
        # marca del offset 8: mirando menos que eso, un CR3 pasa por HEIC.
        (b"\x00\x00\x00\x1cftypheic", Formato.HEIF),
        (b"\x00\x00\x00\x1cftypavif", Formato.AVIF),
        (b"\x00\x00\x00\x18ftypcrx ", Formato.CR3),
        # Los dos ordenes de bytes del contenedor TIFF. Cubre tambien a ARW,
        # CR2, NEF y DNG, que son TIFF por dentro.
        (b"II*\x00\x08\x00", Formato.TIFF),
        (b"MM\x00*\x00\x00", Formato.TIFF),
        (b"", None),
        (b"%PDF-1.7", None),
        (b"MZ\x90\x00" * 8, None),
    ],
)
def test_deteccion_por_contenido(bytes_: bytes, esperado: Formato | None) -> None:
    assert detectar_formato(bytes_) is esperado


# ── Formatos nuevos, y los que quedaron deliberadamente afuera ──────────────


def _heif(ancho: int = 40, alto: int = 30) -> bytes:
    """HEIF real, generado con la misma libreria que despues lo lee."""
    buffer = io.BytesIO()
    Image.new("RGB", (ancho, alto), "red").save(buffer, format="HEIF")
    return buffer.getvalue()


def test_heic_se_acepta_y_se_convierte(sesion: TestClient) -> None:
    """CA-01 — el formato con el que salen las fotos de cualquier iPhone."""
    r = sesion.post("/api/conversor", files={"archivo": ("foto.heic", _heif(), "image/heic")})
    assert r.status_code == 200, r.text
    descarga = sesion.get(f"/api/trabajos/{r.json()['id']}/archivo/jpg")
    assert descarga.content.startswith(b"\xff\xd8\xff"), "no es un JPEG"


@pytest.mark.parametrize(
    "cabecera",
    [b"\x00\x00\x00\x18ftypcrx ", b"II*\x00\x08\x00\x00\x00", b"MM\x00*\x00\x00\x00\x08"],
)
def test_los_raw_de_camara_son_formatos_permitidos(cabecera: bytes) -> None:
    """CA-02 — hasta donde llega la verificacion automatica, y por que.

    Se verifica que los contenedores de RAW se reconocen y estan habilitados en
    el conversor. Que la foto se decodifique de verdad necesita un RAW real de
    camara, de decenas de MB, y el repo no versiona binarios de ese tamaño:
    esa mitad queda como paso manual.
    """
    formato = detectar_formato(cabecera)
    assert formato in (Formato.CR3, Formato.TIFF)
    assert formato in FORMATOS_CONVERSOR


@pytest.mark.parametrize(
    ("etiqueta", "contenido"),
    [
        ("EPS", b"%!PS-Adobe-3.0 EPSF-3.0\n"),
        ("XCF", b"gimp xcf v011\x00"),
        ("EXR", b"\x76\x2f\x31\x01\x02\x00\x00\x00"),
        ("DICOM", b"\x00" * 128 + b"DICM"),
    ],
)
def test_lo_que_queda_afuera_se_rechaza_por_su_nombre(
    sesion: TestClient, etiqueta: str, contenido: bytes
) -> None:
    """CA-03 — "no se puede usar un EPS" es accionable; "desconocido" no.

    Estos cuatro no se abren a proposito (EPS necesita Ghostscript en el
    sistema; DICOM, EXR y XCF son parsers grandes sobre archivos de usuario),
    pero si se reconocen, que cuesta una comparacion de bytes.
    """
    archivo = {"archivo": ("x.bin", contenido, "application/octet-stream")}
    r = sesion.post("/api/conversor", files=archivo)
    assert r.status_code == 415
    error = r.json()["error"]
    assert error["codigo"] == "formato_no_soportado"
    assert error["detalle"]["detectado"] == etiqueta
    assert etiqueta in error["mensaje"], error["mensaje"]


def test_el_tope_de_pixeles_corta_antes_de_decodificar(tmp_path: Path) -> None:
    """CA-04 / RNF-01 — un solo limite para los dos decodificadores.

    Se apaga a proposito el guard propio de Pillow: lo que se prueba es el tope
    del motor, que es el que ademas cubre a `rawpy` — que no pasa por Pillow y
    por lo tanto no tiene ninguna red propia.
    """
    origen = tmp_path / "bomba.png"
    guard_pillow = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        lado = int(MAX_PIXELES**0.5) + 200
        Image.new("L", (lado, lado)).save(origen)
        with pytest.raises(ImagenInvalida, match="megapixeles"):
            convertir_a_jpg(origen, tmp_path / "s.jpg")
    finally:
        Image.MAX_IMAGE_PIXELS = guard_pillow


def test_heic_tambien_llega_a_svg(sesion: TestClient) -> None:
    """El vectorizador tiene su propio decodificador, y no lee HEIC ni RAW.

    Antes de normalizar la entrada, pedir `formato=svg` sobre un HEIC no daba un
    error: vtracer **paniqueaba en Rust**, y `pyo3` levanta esos panics como
    `PanicException`, que hereda de `BaseException` y no de `Exception` — asi
    que el `except` de la skill no lo veia y salia un 500.
    """
    archivo = {"archivo": ("foto.heic", _heif(), "image/heic")}
    r = sesion.post("/api/conversor", files=archivo, data={"formato": "svg"})
    assert r.status_code == 200, r.text
    descarga = sesion.get(f"/api/trabajos/{r.json()['id']}/archivo/svg")
    assert b"<svg" in descarga.content[:200], descarga.content[:80]


def test_el_tope_de_pixeles_tambien_cubre_al_vectorizador(tmp_path: Path) -> None:
    """El camino de vtracer era el unico sin ningun guard de tamaño."""
    origen = tmp_path / "bomba.png"
    guard_pillow = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        lado = int(MAX_PIXELES**0.5) + 200
        Image.new("L", (lado, lado)).save(origen)
        with pytest.raises(ImagenInvalida, match="megapixeles"):
            a_svg(origen, tmp_path / "s.svg")
    finally:
        Image.MAX_IMAGE_PIXELS = guard_pillow


def test_un_tiff_comun_lo_abre_pillow_tras_el_rechazo_de_libraw(tmp_path: Path) -> None:
    """RF-18 — el contenedor TIFF es ambiguo y lo desempata LibRaw.

    Se prueba el MECANISMO, que es lo verificable sin un RAW real: ante un TIFF
    que no es de camara, `_abrir_raw` devuelve `None` porque LibRaw lo rechaza
    limpio, y la apertura cae en Pillow. Al reves —Pillow primero— un `.NEF` se
    "abriria" igual, pero devolviendo el preview JPEG embebido en vez de la
    foto: un resultado incorrecto sin ningun error a la vista.
    """
    origen = tmp_path / "escaneo.tiff"
    Image.new("RGB", (50, 30), "blue").save(origen)
    assert _abrir_raw(origen) is None, "LibRaw no deberia aceptar un TIFF comun"
    with Image.open(convertir_a_jpg(origen, tmp_path / "s.jpg")) as imagen:
        assert imagen.size == (50, 30)
