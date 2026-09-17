"""Subidas: tipo, tamaño y nombres hostiles.

El borde de entrada de la app. Lo que se prueba aca es que **nada de lo que
manda el cliente se usa tal cual**: ni el nombre, ni el `Content-Type`, ni el
tamaño declarado.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image
from test_web_trabajos import sondear_hasta_el_final

from app.almacen import AlmacenEnMemoria, TipoTrabajo
from app.archivos import (
    CLAVE_SALIDA,
    DESTINOS_IMAGEN,
    DESTINOS_MALLA,
    EXTENSION,
    FORMATOS_CONVERSOR,
    FORMATOS_CONVERSOR_IMAGEN,
    FORMATOS_MALLA,
    MAX_DISENOS,
    NOMBRE_DE,
    Formato,
    FormatoSalida,
    destinos_de,
    detectar_formato,
    guardar_subida,
)
from app.config import Ajustes
from app.errores import ErrorApi
from app.routers.post import ROLES as ROLES_DEL_ROUTER
from cutter3d.errors import ImagenInvalida
from cutter3d.malla import ROLES as ROLES_DEL_MOTOR
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
        data={"formato": "obj"},
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
    assert formato in FORMATOS_CONVERSOR_IMAGEN


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


# ── F4: mallas ──────────────────────────────────────────────────────────────


def _malla_3mf() -> bytes:
    escena = trimesh.Scene()
    escena.add_geometry(trimesh.creation.box(extents=(10.0, 10.0, 4.0)), geom_name="cortador")
    return bytes(escena.export(file_type="3mf"))


def _malla_stl() -> bytes:
    return bytes(trimesh.creation.box(extents=(10.0, 10.0, 4.0)).export(file_type="stl"))


def test_una_malla_nunca_puede_terminar_en_pillow() -> None:
    """⚠ El Convertidor acepta mallas, pero las dos mitades no se cruzan NUNCA.

    `FORMATOS_CONVERSOR` era `frozenset(Formato)` y con el enum de puras
    imagenes funcionaba; desde que hay mallas, cada miembro nuevo entraba solo y
    reventaba despues adentro de Pillow — un 500 donde corresponde un 4xx. Hoy
    la malla si entra, pero por su propio camino: lo que sostiene la separacion
    ya no es la lista de formatos sino el par (origen, destino).

    Los dos lados se prueban juntos porque son la misma afirmacion: nada que no
    se pueda abrir con Pillow puede pedir un destino de imagen.
    """
    assert not (FORMATOS_CONVERSOR_IMAGEN & FORMATOS_MALLA)
    for malla in FORMATOS_MALLA:
        assert destinos_de(malla) == DESTINOS_MALLA, malla
    for imagen in FORMATOS_CONVERSOR_IMAGEN:
        assert destinos_de(imagen) == DESTINOS_IMAGEN, imagen


def test_todo_destino_tiene_clave_y_nombre_en_disco() -> None:
    """`CLAVE_SALIDA` es un dict TOTAL: un destino sin fila es un KeyError.

    Explota en el router, con el trabajo ya creado y el archivo ya subido. Es la
    misma red que `EXTENSION` para `Formato`, y hace falta desde que el enum de
    destinos dejo de tener dos miembros.
    """
    assert set(CLAVE_SALIDA) == set(FormatoSalida)
    for clave in CLAVE_SALIDA.values():
        assert clave in NOMBRE_DE


def test_todo_formato_tiene_extension() -> None:
    """`EXTENSION` es un dict TOTAL sin default: un miembro sin fila es un KeyError.

    Explota recien adentro de `guardar_subida`, con el archivo ya a medio
    escribir. Los tres dicts de `ClaveArchivo` ya tenian esta red; este no.
    """
    assert set(EXTENSION) == set(Formato)


@pytest.mark.parametrize("nombre", ["p.3mf", "p.stl"])
def test_post_acepta_mallas(sesion: TestClient, nombre: str) -> None:
    contenido = _malla_3mf() if nombre.endswith(".3mf") else _malla_stl()
    r = sesion.post("/api/post", files={"archivo": (nombre, contenido, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["tipo"] == "post"


@pytest.mark.parametrize(
    ("nombre", "medio"),
    [("d.svg", "image/svg+xml"), ("d.png", "image/png")],
)
def test_post_rechaza_lo_que_no_es_malla(
    sesion: TestClient, png_minimo: bytes, nombre: str, medio: str
) -> None:
    contenido = SVG_MINIMO if nombre.endswith(".svg") else png_minimo
    r = sesion.post("/api/post", files={"archivo": (nombre, contenido, medio)})
    assert r.status_code == 415
    assert r.json()["error"]["mensaje"].endswith("Se aceptan: 3mf, stl.")


def test_post_rechaza_un_zip_que_no_es_3mf(sesion: TestClient) -> None:
    """La firma de un 3MF es la de cualquier ZIP: docx, xlsx, jar y epub la comparten.

    Lo que lo distingue vive en el central directory, al final del archivo, y la
    deteccion por magic bytes solo ve los primeros 64 KB. Por eso el router
    confirma la estructura ANTES de lanzar el proceso hijo — un docx se va con
    un 422 inmediato en vez de consumir uno de los tres slots.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    r = sesion.post(
        "/api/post", files={"archivo": ("t.docx", buffer.getvalue(), "application/octet-stream")}
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "malla_ilegible"


def test_el_stl_binario_se_detecta_antes_que_tga(sesion: TestClient) -> None:
    """⚠ El orden de las ramas sin firma es la correccion misma.

    Los 80 bytes de cabecera de un STL binario son libres y pueden satisfacer de
    casualidad la heuristica de TGA, que solo mira tres campos. La condicion del
    STL es una identidad aritmetica exacta sobre el tamaño del archivo, asi que
    es mucho mas fuerte y tiene que evaluarse primero.
    """
    del sesion
    stl = _malla_stl()
    # Una cabecera que TGA aceptaria: [1] en {0,1}, [2] en {1,2,3,9,10,11}
    # y [16] en {8,15,16,24,32}.
    hostil = bytearray(stl)
    hostil[1], hostil[2], hostil[16] = 1, 2, 24
    assert detectar_formato(bytes(hostil), len(hostil)) is Formato.STL
    # Y sin el tamaño no adivina: prefiere no afirmar nada.
    assert detectar_formato(bytes(hostil)) is not Formato.STL


def test_el_stl_ascii_se_detecta_despues_del_binario() -> None:
    """Un STL binario puede empezar con `solid`: muchos programas escriben ahi
    el nombre del solido. Si el ASCII fuera primero, ese archivo iria al parser
    equivocado."""
    ascii_stl = (
        b"solid cubo\nfacet normal 0 0 1\nouter loop\n"
        b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid cubo\n"
    )
    assert detectar_formato(ascii_stl, len(ascii_stl)) is Formato.STL

    binario_que_dice_solid = bytearray(_malla_stl())
    binario_que_dice_solid[0:5] = b"solid"
    assert (
        detectar_formato(bytes(binario_que_dice_solid), len(binario_que_dice_solid)) is Formato.STL
    )


# ── F4 con varios diseños ───────────────────────────────────────────────────


def _lote(cuantos: int) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("archivo", (f"d{i}.3mf", _malla_3mf(), "application/octet-stream")) for i in range(cuantos)
    ]


def test_post_agrupa_un_cortador_con_su_marcador(sesion: TestClient) -> None:
    """Dos archivos, UN diseño. Es lo que evita fotografiar media pieza."""
    r = sesion.post(
        "/api/post",
        files=[
            ("archivo", ("k_cortador.stl", _malla_stl(), "application/octet-stream")),
            ("archivo", ("k_marcador.stl", _malla_stl(), "application/octet-stream")),
        ],
        data={"agrupacion": "0:cortador,1:marcador"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["disenos"] == 1, "un cortador y su marcador son un solo diseño"


def test_post_sin_agrupacion_hace_un_diseno_por_archivo(sesion: TestClient) -> None:
    """El default honesto: el servidor no ve nombres, no puede emparejar nada."""
    r = sesion.post("/api/post", files=_lote(3))
    assert r.status_code == 200, r.text
    assert r.json()["disenos"] == 3


@pytest.mark.parametrize(
    ("caso", "agrupacion"),
    [
        ("dos cortadores", "0:cortador,1:cortador"),
        ("un archivo en dos grupos", "0:unico;0:unico"),
        ("archivo sin asignar", "0:unico"),
        ("indice que no llego", "0:unico;9:unico"),
        ("rol inventado", "0:pieza;1:unico"),
        ("tres en un diseño", "0:unico,1:unico,0:unico"),
        ("forma rota", "0-unico"),
        ("vacia", ";"),
    ],
)
def test_post_rechaza_una_agrupacion_invalida(
    sesion: TestClient, caso: str, agrupacion: str
) -> None:
    """Cada regla tiene su motivo en `_parsear`; lo que importa es que ninguna
    pase en silencio y arme un diseño con las piezas de otro."""
    r = sesion.post("/api/post", files=_lote(2), data={"agrupacion": agrupacion})
    assert r.status_code == 422, f"{caso}: {r.status_code}"
    assert r.json()["error"]["codigo"] in ("agrupacion_invalida", "peticion_invalida"), caso


def test_post_rechaza_mas_de_veinticinco_disenos(sesion: TestClient) -> None:
    r = sesion.post(
        "/api/post",
        files=_lote(MAX_DISENOS + 1),
        data={"agrupacion": ";".join(f"{i}:unico" for i in range(MAX_DISENOS + 1))},
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "demasiados_disenos"


def test_los_roles_del_router_y_del_motor_son_los_mismos() -> None:
    """`routers/post.py` repite la lista para no importar `malla` —y con el,
    trimesh— en el proceso web. Si se separan, el router acepta un rol que el
    motor no entiende y el diseño falla recien adentro del proceso hijo."""
    assert set(ROLES_DEL_ROUTER) == set(ROLES_DEL_MOTOR)


def test_el_tope_de_disenos_del_front_es_el_del_servidor(sesion: TestClient) -> None:
    """El JS avisa antes de subir 50 MB para que el servidor conteste 422.

    Es un espejo, no una segunda verdad — pero si se desincroniza, el front deja
    pasar un lote que el servidor rechaza, o corta uno que aceptaria."""
    js = sesion.get("/static/js/app.js").text
    assert f"const MAX_DISENOS = {MAX_DISENOS};" in js


# ── F1: mallas, el otro camino del Convertidor ──────────────────────────────


def test_el_conversor_rechaza_una_imagen_pedida_como_3d(
    sesion: TestClient, png_minimo: bytes
) -> None:
    """Construir geometria es F3, con su reporte de fidelidad y sus parametros.

    Un conversor que lo hiciera de callado seria exactamente la perdida de
    fidelidad no elegida que este proyecto no hace. Y el mensaje tiene que
    NOMBRAR la pantalla que si lo hace: un 422 que solo dice "no se puede" deja
    al usuario creyendo que la app no sabe.
    """
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("d.png", png_minimo, "image/png")},
        data={"formato": "3mf"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "conversion_no_aplica"
    assert "Cortante" in r.json()["error"]["mensaje"]


@pytest.mark.parametrize("destino", ["jpg", "svg"])
def test_el_conversor_rechaza_una_malla_pedida_como_imagen(
    sesion: TestClient, destino: str
) -> None:
    """La foto de una pieza es F4, que la rinde con el estudio de luces del cortante.

    Sacarla aca seria una segunda foto que no se parece a la primera, que es
    justo lo que F4 existe para evitar.
    """
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("p.3mf", _malla_3mf(), "application/octet-stream")},
        data={"formato": destino},
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "conversion_no_aplica"
    assert "Post" in r.json()["error"]["mensaje"]


@pytest.mark.parametrize(("nombre", "formato"), [("p.3mf", "3mf"), ("p.stl", "stl")])
def test_una_malla_ya_en_el_formato_pedido_sale_identica(
    sesion: TestClient, nombre: str, formato: str
) -> None:
    """Se copia tal cual: es lo unico honesto y lo unico que no puede alterar nada.

    Pasa mas de lo que parece — el destino lo propone el navegador mirando la
    extension y el formato real lo deciden los bytes, asi que un `.stl` que en
    verdad era un 3MF llega como un par identico. **No gasta un proceso hijo**:
    vuelve `listo` del mismo request.
    """
    contenido = _malla_3mf() if formato == "3mf" else _malla_stl()
    r = sesion.post(
        "/api/conversor",
        files={"archivo": (nombre, contenido, "application/octet-stream")},
        data={"formato": formato},
    )
    assert r.status_code == 200, r.text
    trabajo = r.json()
    assert trabajo["estado"] == "listo"
    assert trabajo["reporte"]["sin_conversion"] is True

    descarga = sesion.get(f"/api/trabajos/{trabajo['id']}/archivo/{formato}")
    assert descarga.content == contenido, "el archivo tiene que salir identico"


def test_el_conversor_rechaza_un_zip_que_no_es_3mf(sesion: TestClient) -> None:
    """Mismo motivo que en `/api/post`: se confirma ANTES de gastar un slot.

    La firma `PK\\x03\\x04` la comparten docx, xlsx, jar y epub, y lo que
    distingue a un 3MF vive en el central directory, al final del archivo, donde
    la deteccion por magic bytes no llega.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("t.docx", buffer.getvalue(), "application/octet-stream")},
        data={"formato": "stl"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "malla_ilegible"


@pytest.mark.lento
def test_la_conversion_de_malla_va_a_un_proceso_hijo_y_conserva_las_medidas(
    sesion: TestClient,
) -> None:
    """De punta a punta: POST, polling, descarga, y la pieza mide lo mismo.

    ⚠ **No vuelve `listo` del request**, y eso es la mitad del test: trimesh no
    puede entrar al proceso web (~1200 modulos, ~89 MB en un proceso que no
    construye un solo poligono), asi que la conversion vive en un hijo. Si algun
    dia alguien la trae al request para "simplificar", este assert se cae.
    """
    r = sesion.post(
        "/api/conversor",
        files={"archivo": ("pieza.3mf", _malla_3mf(), "application/octet-stream")},
        data={"formato": "stl"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["estado"] != "listo", "la malla no puede convertirse en el proceso web"

    final = sondear_hasta_el_final(sesion, r.json()["id"], limite_s=120)
    assert final["estado"] == "listo", final
    assert final["archivos"] == ["stl"]
    assert final["reporte"]["medidas_mm"] == [10.0, 10.0, 4.0]
    assert final["reporte"]["cerrado"] is True

    descarga = sesion.get(f"/api/trabajos/{final['id']}/archivo/stl")
    assert descarga.status_code == 200
    assert descarga.headers["content-type"] == "model/stl"
    malla = trimesh.load(io.BytesIO(descarga.content), file_type="stl")
    assert list(malla.extents) == [10.0, 10.0, 4.0]
