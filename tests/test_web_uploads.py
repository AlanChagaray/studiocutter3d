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

from app.almacen import AlmacenEnMemoria, TipoTrabajo
from app.archivos import FORMATOS_CONVERSOR, Formato, detectar_formato, guardar_subida
from app.config import Ajustes
from app.errores import ErrorApi

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
        (b"GIF89a", None),
        (b"", None),
        (b"%PDF-1.7", None),
    ],
)
def test_deteccion_por_contenido(bytes_: bytes, esperado: Formato | None) -> None:
    assert detectar_formato(bytes_) is esperado
