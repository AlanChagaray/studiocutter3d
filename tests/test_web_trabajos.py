"""Ciclo de vida de un trabajo: polling, timeout, errores y descargas.

Casi todos los tests usan **tareas falsas** en vez del motor: un trabajo real
cuesta segundos, y lo que se esta probando aca es el andamiaje (el vigilante,
el timeout, la traduccion de `estado.json`), no la geometria — que ya tiene
sus propios 105 tests.

Las tareas falsas estan a nivel de modulo porque en Windows el arranque es
`spawn`: el proceso hijo importa este archivo y busca la funcion por nombre.
Una funcion anidada o un lambda no se podrian pasar.

El unico test que corre el motor de verdad esta marcado `lento` y es el que
cierra el ciclo de punta a punta.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.almacen import (
    TIPOS_CON_VISTA,
    AlmacenEnMemoria,
    EstadoTrabajo,
    TipoTrabajo,
    Trabajo,
)
from app.archivos import (
    LIMITE_VISTA_BYTES,
    MEDIO_DE,
    NOMBRE_DE,
    SUFIJO_DESCARGA,
    ClaveArchivo,
    claves_descargables,
    escribir_estado,
    nombre_de_zip,
)
from app.config import Ajustes
from app.errores import ErrorApi
from app.routers.cortante import COLORES
from app.trabajos import cancelar, lanzar, lanzar_serie, proceso_de, vivos

RAIZ_PROYECTO = Path(__file__).resolve().parent.parent
FIXTURES = RAIZ_PROYECTO / "tests" / "fixtures"
SVG_VACIO = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>'


# ── Tareas falsas (tienen que ser importables por el hijo) ──────────────────


def tarea_que_termina_bien(dir_trabajo: str) -> None:
    destino = Path(dir_trabajo)
    (destino / "salida.png").write_bytes(b"\x89PNG\r\n\x1a\ncontenido de prueba")
    escribir_estado(
        destino,
        {
            "ok": True,
            "etapa": "listo",
            "archivos": {"png": "salida.png"},
            "reporte": {"zonas_contorneadas": 3},
        },
    )


def tarea_que_falla(dir_trabajo: str) -> None:
    escribir_estado(
        Path(dir_trabajo),
        {
            "ok": False,
            "etapa": "error",
            "archivos": {},
            "error": {"codigo": "svg_invalido", "mensaje": "El dibujo no sirve.", "detalle": {}},
        },
    )


def tarea_con_clave_inventada(dir_trabajo: str) -> None:
    destino = Path(dir_trabajo)
    (destino / "salida.png").write_bytes(b"\x89PNG")
    escribir_estado(
        destino,
        {
            "ok": True,
            "etapa": "listo",
            "archivos": {"png": "salida.png", "credenciales": "../../credenciales.json"},
            "reporte": None,
        },
    )


def tarea_que_no_termina(dir_trabajo: str) -> None:
    del dir_trabajo
    time.sleep(120)


def tarea_que_muere_sin_avisar(dir_trabajo: str) -> None:
    del dir_trabajo
    os._exit(7)


# ── Ayudantes ────────────────────────────────────────────────────────────────


def esperar_estado(almacen: AlmacenEnMemoria, id_: str, usuario: str, limite_s: float = 40.0):
    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        trabajo = almacen.obtener(id_, usuario)
        assert trabajo is not None
        if trabajo.estado.terminal:
            return trabajo
        time.sleep(0.15)
    raise AssertionError(f"el trabajo {id_} no termino en {limite_s} s")


def sondear_hasta_el_final(cliente: TestClient, id_: str, limite_s: float = 60.0):
    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        respuesta = cliente.get(f"/api/trabajos/{id_}")
        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        if cuerpo["estado"] in ("listo", "error"):
            return cuerpo
        time.sleep(0.2)
    raise AssertionError("el polling nunca vio un estado terminal")


def _lanzar(almacen, ajustes, tarea, usuario="tester"):
    trabajo = almacen.crear(usuario, TipoTrabajo.LINEAS)
    destino = ajustes.dir_trabajo / trabajo.id
    destino.mkdir(parents=True, exist_ok=True)
    lanzar(almacen=almacen, a=ajustes, trabajo=trabajo, objetivo=tarea, argumentos=(str(destino),))
    return trabajo


# ── El andamiaje ─────────────────────────────────────────────────────────────


def test_ciclo_completo_con_polling_y_descarga(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    trabajo = _lanzar(almacen, ajustes, tarea_que_termina_bien)
    final = sondear_hasta_el_final(sesion, trabajo.id)

    assert final["estado"] == "listo"
    assert final["archivos"] == ["png"]
    assert final["reporte"]["zonas_contorneadas"] == 3
    assert final["error"] is None

    descarga = sesion.get(f"/api/trabajos/{trabajo.id}/archivo/png")
    assert descarga.status_code == 200
    assert descarga.content == b"\x89PNG\r\n\x1a\ncontenido de prueba"
    # CA-07: este trabajo lo arma una tarea falsa que nunca vio un nombre de
    # cliente, asi que cae al id. Es el fallback, y tiene que seguir existiendo.
    assert "studiocutter-" in descarga.headers["content-disposition"]


# ── El nombre del archivo sobrevive el pipeline ─────────────────────────────


def test_la_descarga_conserva_el_nombre_original(sesion: TestClient, png_minimo: bytes) -> None:
    """CA-05 — `buddy.png` baja como `buddy.jpg`, no como `studiocutter-a75088.jpg`."""
    r = sesion.post("/api/conversor", files={"archivo": ("buddy.png", png_minimo, "image/png")})
    assert r.status_code == 200
    assert r.json()["nombre_base"] == "buddy"

    descarga = sesion.get(f"/api/trabajos/{r.json()['id']}/archivo/jpg")
    assert 'filename="buddy.jpg"' in descarga.headers["content-disposition"]


@pytest.mark.parametrize(
    "hostil",
    [
        "../../../evil.png",
        "..\\..\\evil.png",
        "C:\\fotos\\buddy.png",
        "a" * 400 + ".png",
        'bu"ddy.png',
        "mi dibujo.png",
        "...",
    ],
)
def test_el_nombre_de_descarga_sale_saneado(
    sesion: TestClient, ajustes: Ajustes, png_minimo: bytes, hostil: str
) -> None:
    """CA-06 / RNF-02 / RNF-03 — el nombre se ve, pero no manda.

    Dos afirmaciones, y la segunda es la que sostiene todo el diseño: el nombre
    del cliente llega al header y **el archivo en disco sigue siendo
    `entrada.png`**. Mientras esas dos cosas valgan, conservar el nombre no
    reabre el path traversal.
    """
    r = sesion.post("/api/conversor", files={"archivo": (hostil, png_minimo, "image/png")})
    assert r.status_code == 200
    trabajo = r.json()

    escritos = sorted(p.name for p in (ajustes.dir_trabajo / trabajo["id"]).iterdir())
    assert escritos == ["entrada.png", "salida.jpg"], escritos

    base = trabajo["nombre_base"]
    if base is not None:
        assert len(base) <= 60
        assert re.fullmatch(r"[A-Za-z0-9._-]+", base), base

    disposicion = sesion.get(f"/api/trabajos/{trabajo['id']}/archivo/jpg").headers[
        "content-disposition"
    ]
    for prohibido in ("/", "\\", "\r", "\n"):
        assert prohibido not in disposicion, disposicion


def test_el_nombre_se_hereda_al_encadenar(
    sesion: TestClient, almacen: AlmacenEnMemoria, png_minimo: bytes
) -> None:
    """CA-08 — `buddy.png` convertido y encadenado sigue siendo `buddy`.

    Es lo que hace que tres pantallas despues el `.3mf` se llame `buddy.3mf`.
    El trabajo encadenado lanza el motor de verdad, asi que se cancela apenas
    se comprueba lo unico que importa aca, que es el nombre.
    """
    primero = sesion.post(
        "/api/conversor", files={"archivo": ("buddy.png", png_minimo, "image/png")}
    )
    assert primero.json()["nombre_base"] == "buddy"

    segundo = sesion.post("/api/lineas", data={"origen": primero.json()["id"]})
    assert segundo.status_code == 200, segundo.text
    try:
        assert segundo.json()["nombre_base"] == "buddy"
    finally:
        cancelar(almacen, segundo.json()["id"])


def test_un_trabajo_fallido_llega_como_error(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    trabajo = _lanzar(almacen, ajustes, tarea_que_falla)
    final = sondear_hasta_el_final(sesion, trabajo.id)
    assert final["estado"] == "error"
    assert final["error"]["codigo"] == "svg_invalido"


def test_el_timeout_mata_el_proceso_hijo(almacen: AlmacenEnMemoria, ajustes: Ajustes) -> None:
    """No alcanza con marcarlo error: el hijo tiene que estar muerto."""
    impaciente = replace(ajustes, timeout_trabajo_s=2)
    trabajo = _lanzar(almacen, impaciente, tarea_que_no_termina)
    final = esperar_estado(almacen, trabajo.id, "tester", limite_s=30)

    assert final.estado is EstadoTrabajo.ERROR
    assert final.error is not None
    assert final.error["codigo"] == "tiempo_agotado"

    hijo = proceso_de(trabajo.id)
    assert hijo is not None
    assert not hijo.is_alive(), "el proceso quedo vivo despues del timeout"


def test_un_hijo_que_muere_sin_escribir_es_error_y_no_queda_colgado(
    almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """`estado.json` ausente significa fallo, nunca 'todavia no'."""
    trabajo = _lanzar(almacen, ajustes, tarea_que_muere_sin_avisar)
    final = esperar_estado(almacen, trabajo.id, "tester", limite_s=30)
    assert final.estado is EstadoTrabajo.ERROR
    assert final.error is not None
    assert final.error["codigo"] == "interno"
    assert final.error["detalle"]["exitcode"] == 7


def test_hay_un_tope_de_trabajos_simultaneos(almacen: AlmacenEnMemoria, ajustes: Ajustes) -> None:
    """Cada hijo carga numpy y trimesh: sin tope, una rafaga tumba la maquina."""
    de_a_uno = replace(ajustes, max_trabajos_simultaneos=1, timeout_trabajo_s=8)
    primero = _lanzar(almacen, de_a_uno, tarea_que_no_termina)

    with pytest.raises(ErrorApi) as caja:
        _lanzar(almacen, de_a_uno, tarea_que_no_termina)
    assert caja.value.estado == 429
    assert caja.value.codigo == "demasiados_trabajos"

    cancelar(almacen, primero.id)


def test_una_clave_de_archivo_desconocida_no_entra_al_almacen(
    almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Lo que declara el hijo se filtra contra el enum en la frontera."""
    trabajo = _lanzar(almacen, ajustes, tarea_con_clave_inventada)
    final = esperar_estado(almacen, trabajo.id, "tester", limite_s=30)
    assert final.estado is EstadoTrabajo.LISTO
    assert final.archivos == {"png": "salida.png"}, final.archivos


# ── Autorizacion y superficie de la API ─────────────────────────────────────


def test_uuid_inexistente_es_404(sesion: TestClient) -> None:
    r = sesion.get("/api/trabajos/2f1b3e0c-1111-4000-8000-000000000000")
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "trabajo_inexistente"


def test_id_que_no_es_uuid_es_404_y_no_toca_el_disco(sesion: TestClient) -> None:
    for id_ in ("..", "../../etc", "a/b", "%2e%2e", "no-uuid"):
        r = sesion.get(f"/api/trabajos/{id_}")
        assert r.status_code == 404, id_


def test_un_trabajo_de_otro_propietario_es_404(
    sesion: TestClient, almacen: AlmacenEnMemoria
) -> None:
    """Hoy hay un solo usuario, pero la autorizacion ya esta puesta."""
    ajeno = almacen.crear("otra_persona", TipoTrabajo.CORTANTE)
    assert sesion.get(f"/api/trabajos/{ajeno.id}").status_code == 404
    assert sesion.get(f"/api/trabajos/{ajeno.id}/archivo/3mf").status_code == 404


def test_clave_de_archivo_fuera_del_enum_es_4xx(
    sesion: TestClient, almacen: AlmacenEnMemoria
) -> None:
    propio = almacen.crear("tester", TipoTrabajo.CORTANTE)
    for clave in ("passwd", "../../secreto", "estado.json", "entrada"):
        r = sesion.get(f"/api/trabajos/{propio.id}/archivo/{clave}")
        assert 400 <= r.status_code < 500, clave


def test_archivo_valido_pero_inexistente_es_404(
    sesion: TestClient, almacen: AlmacenEnMemoria
) -> None:
    propio = almacen.crear("tester", TipoTrabajo.CORTANTE)
    r = sesion.get(f"/api/trabajos/{propio.id}/archivo/3mf")
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] in ("archivo_inexistente", "trabajo_inexistente")


def cancelar_trabajo(sesion: TestClient, id_: str) -> None:
    """Corta el hijo: al test le alcanza con que el pedido haya sido aceptado."""
    sesion.delete(f"/api/trabajos/{id_}")


# ── Parametros del cortante ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("lado_mayor_mm", 1500),
        ("lado_mayor_mm", 0),
        ("lado_mayor_mm", -3),
        ("luz_mm", 1000.5),
        ("distancia_colision_mm", 1000.5),
        ("pie_alto_mm", 0),
        ("filo_alto_mm", 99999),
    ],
)
def test_parametro_fuera_de_rango_es_422_y_nombra_el_campo(
    sesion: TestClient, campo: str, valor: float
) -> None:
    r = sesion.post("/api/cortante", data={campo: valor, "origen": "x"}, files={})
    assert r.status_code == 422, r.text
    cuerpo = r.json()["error"]
    assert cuerpo["codigo"] == "parametro_invalido"
    assert cuerpo["detalle"]["parametro"] == campo
    assert campo in cuerpo["mensaje"]


def test_los_diez_parametros_por_defecto_se_aceptan(sesion: TestClient) -> None:
    """Con los defaults, el rechazo tiene que ser por el archivo y no por un numero."""
    r = sesion.post("/api/cortante", data={})
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "falta_archivo"


# ── Parametros de la normalizacion de trazo (F2) ─────────────────────────────


@pytest.mark.parametrize(
    ("campo", "valor"),
    [("ancho_trazo_mm", 0), ("ancho_trazo_mm", -1), ("lado_mayor_mm", 0), ("lado_mayor_mm", 1500)],
)
def test_las_medidas_de_la_normalizacion_se_validan_como_las_del_cortante(
    sesion: TestClient, campo: str, valor: float
) -> None:
    """Mismos limites, mismo 422, mismo nombre de campo — y en el mismo momento.

    Se valida ANTES de crear el trabajo: con la validacion del lado del motor
    nada mas, un 0 salia como un trabajo que arranca y falla adentro del proceso
    hijo, donde el usuario ve "error" y no cual de los dos numeros estaba mal.
    """
    r = sesion.post("/api/lineas", data={campo: valor, "normalizar_trazo": "true", "origen": "x"})
    assert r.status_code == 422, r.text
    cuerpo = r.json()["error"]
    assert cuerpo["codigo"] == "parametro_invalido"
    assert cuerpo["detalle"]["parametro"] == campo


def test_las_medidas_se_validan_aunque_la_normalizacion_este_apagada(sesion: TestClient) -> None:
    """Un numero invalido es invalido, lo mire o no esta corrida.

    La pantalla manda los dos campos siempre, encendido o apagado el switch, y
    aceptarlos en silencio cuando esta apagado dejaria pasar un valor que rompe
    recien la proxima vez que alguien lo enciende.
    """
    r = sesion.post("/api/lineas", data={"lado_mayor_mm": 0, "normalizar_trazo": "false"})
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "parametro_invalido"


def test_en_modo_cortante_los_parametros_del_marcador_son_opcionales(
    sesion: TestClient,
) -> None:
    """La pantalla no los manda porque el motor los ignora: no pueden ser obligatorios."""
    estrella = (FIXTURES / "estrella.svg").read_bytes()
    r = sesion.post(
        "/api/cortante",
        files={"archivo": ("estrella.svg", estrella, "image/svg+xml")},
        data={"modo": "cortante", "lado_mayor_mm": "70"},
    )
    assert r.status_code == 200, r.text
    cancelar_trabajo(sesion, r.json()["id"])


# ── Fuga de informacion ──────────────────────────────────────────────────────


def test_un_error_del_motor_no_devuelve_rutas_del_servidor(
    sesion: TestClient, ajustes: Ajustes
) -> None:
    """`str(exc)` del motor empieza con la ruta: no puede llegar al cliente."""
    r = sesion.post("/api/cortante", files={"archivo": ("vacio.svg", SVG_VACIO, "image/svg+xml")})
    assert r.status_code == 200
    final = sondear_hasta_el_final(sesion, r.json()["id"])
    assert final["estado"] == "error"

    # Se inspecciona SOLO lo que arma el servidor. `nombre_base` queda afuera a
    # proposito: es texto que eligio el cliente, y desde que viaja en el JSON
    # dos de los centinelas son alcanzables con un nombre de archivo legitimo
    # —`Temp.png` dispara "Temp" y `proyecto.venv.png` dispara ".venv"—. Un
    # fallo asi no seria una fuga del servidor, que es lo unico que este test
    # existe para detectar.
    del final["nombre_base"]
    cuerpo = str(final)
    for filtracion in ("C:\\", "/Users/", ".venv", str(ajustes.dir_trabajo), "Temp"):
        assert filtracion not in cuerpo, f"la respuesta filtro {filtracion!r}: {cuerpo}"


# ── Motor real, de punta a punta ─────────────────────────────────────────────


@pytest.mark.lento
def test_cortante_real_de_punta_a_punta(sesion: TestClient, ajustes: Ajustes) -> None:
    """El unico test que corre el motor: POST, polling, y los archivos abren.

    El pedido **no pide los STL** y los dos tienen que estar igual: dejaron de
    ser una opcion de la pantalla y salen siempre.
    """
    svg = (FIXTURES / "estrella.svg").read_bytes()
    r = sesion.post(
        "/api/cortante",
        files={"archivo": ("estrella.svg", svg, "image/svg+xml")},
        data={"modo": "cortante+marcador"},
    )
    assert r.status_code == 200

    final = sondear_hasta_el_final(sesion, r.json()["id"], limite_s=180)
    assert final["estado"] == "listo", final
    assert set(final["archivos"]) >= {
        "3mf",
        "3mf_marcador",
        "3mf_cortador",
        "glb",
        "stl_marcador",
        "stl_cortador",
    }

    reporte = final["reporte"]
    assert reporte["todo_ok"] is True
    assert {t["objeto"] for t in reporte["topologias"]} == {"marcador", "cortador"}
    assert all(t["watertight"] for t in reporte["topologias"])

    tresmf = sesion.get(f"/api/trabajos/{final['id']}/archivo/3mf")
    assert tresmf.status_code == 200
    assert tresmf.content[:2] == b"PK", "un .3mf es un zip"
    glb = sesion.get(f"/api/trabajos/{final['id']}/archivo/glb")
    assert glb.content[:4] == b"glTF"
    stl = sesion.get(f"/api/trabajos/{final['id']}/archivo/stl_cortador")
    assert stl.status_code == 200 and len(stl.content) > 1000


@pytest.mark.lento
def test_la_cadena_lineas_a_cortante_pasa_por_svg(sesion: TestClient, jpg_minimo: bytes) -> None:
    """F2 deja PNG, SVG y la copia JPG; el SVG es lo que acepta el cortante.

    Es el unico camino que queda desde una imagen: el cortante solo toma SVG,
    asi que si F2 no lo produjera, el boton de seguir estaria roto. El JPG es
    lo que el usuario se baja para retocar a mano —y lo unico que Correcto
    acepta de vuelta—, asi que tiene que ser un JPEG de verdad y bajar como tal.
    """
    r = sesion.post("/api/lineas", files={"archivo": ("d.jpg", jpg_minimo, "image/jpeg")})
    assert r.status_code == 200
    lineas = sondear_hasta_el_final(sesion, r.json()["id"], limite_s=180)
    assert lineas["estado"] == "listo", lineas
    assert set(lineas["archivos"]) == {"png", "svg", "jpg_editable"}, lineas["archivos"]

    svg = sesion.get(f"/api/trabajos/{lineas['id']}/archivo/svg")
    assert svg.status_code == 200
    assert b"<svg" in svg.content[:512].lower()

    jpg = sesion.get(f"/api/trabajos/{lineas['id']}/archivo/jpg_editable")
    assert jpg.status_code == 200
    assert jpg.headers["content-type"].startswith("image/jpeg")
    assert jpg.content[:3] == b"\xff\xd8\xff"  # SOI de JPEG
    assert 'filename="d.jpg"' in jpg.headers["content-disposition"]

    encadenado = sesion.post("/api/cortante", data={"origen": lineas["id"]})
    assert encadenado.status_code == 200, encadenado.text
    cancelar_trabajo(sesion, encadenado.json()["id"])


@pytest.mark.lento
def test_la_normalizacion_de_trazo_cruza_la_frontera_de_procesos(
    sesion: TestClient, jpg_minimo: bytes
) -> None:
    """El flag y los dos milimetros tienen que llegar al hijo y volver declarados.

    Es lo unico que prueba el tramo `router -> lanzar -> spawn -> tarea`: en el
    medio los argumentos viajan **posicionales** dentro de una tupla, asi que
    agregar un parametro en el router sin tocar `ejecutar_lineas` no rompe
    ningun tipo — corre, y silenciosamente normaliza con el numero equivocado o
    no normaliza.
    """
    r = sesion.post(
        "/api/lineas",
        files={"archivo": ("d.jpg", jpg_minimo, "image/jpeg")},
        data={"normalizar_trazo": "true", "ancho_trazo_mm": "1.0", "lado_mayor_mm": "40"},
    )
    assert r.status_code == 200

    final = sondear_hasta_el_final(sesion, r.json()["id"], limite_s=180)
    assert final["estado"] == "listo", final
    reporte = final["reporte"]
    assert reporte["normalizacion_activa"] is True
    assert reporte["ancho_objetivo_mm"] == 1.0
    assert reporte["lado_mayor_supuesto_mm"] == 40.0
    # 80 px de tinta sobre una pieza de 40 mm son 2 px/mm. Con eso el trazo no
    # tiene cuerpo, asi que el hijo amplia la imagen y todas sus medidas vienen
    # en la grilla ampliada: el factor tiene que cruzar la frontera junto con
    # ellas, o el cliente no puede volverlas a la escala original.
    k = reporte["factor_ampliacion"]
    assert reporte["fue_ampliada"] is True and k > 1
    assert reporte["tamano_usado"] == [120 * k, 80 * k]
    assert reporte["ancho_objetivo_px"] / k == pytest.approx(2.0, abs=0.2)
    # El ancho logrado es impar por construccion: es el numero que devuelve la
    # reconstruccion, no el que se pidio.
    assert reporte["ancho_logrado_px"] % 2 == 1


# ── Descargar todo (ZIP) y la foto de la vista ───────────────────────────────
#
# Estos tests NO pasan por `lanzar`: el andamiaje del vigilante ya tiene sus
# propios tests arriba, y un proceso hijo por caso costaria segundos para
# llegar al mismo estado que dos escrituras y un `actualizar`. Lo que se prueba
# aca es el ZIP y el PUT, no como llega el trabajo a `listo`.

SALIDAS: dict[str, tuple[str, bytes]] = {
    "3mf": ("salida.3mf", b"PK\x03\x04 combinado"),
    "3mf_cortador": ("salida_cortador.3mf", b"PK\x03\x04 cortador"),
    "3mf_marcador": ("salida_marcador.3mf", b"PK\x03\x04 marcador"),
    "stl_cortador": ("salida_cortador.stl", b"solid cortador\nendsolid\n"),
    "stl_marcador": ("salida_marcador.stl", b"solid marcador\nendsolid\n"),
    "glb": ("salida.glb", b"glTF\x02\x00\x00\x00"),
    "jpg_vista": ("vista.jpg", b"\xff\xd8\xff\xe0 foto de prueba"),
}

#: Lo que deja el motor en modo `cortante+marcador`. La foto NO esta: la sube
#: el navegador despues, y varios tests dependen de esa diferencia.
DEL_MOTOR = ("3mf", "3mf_cortador", "3mf_marcador", "stl_cortador", "stl_marcador", "glb")


def _cortante_listo(
    almacen: AlmacenEnMemoria,
    ajustes: Ajustes,
    *,
    usuario: str = "tester",
    nombre_base: str | None = None,
    claves: tuple[str, ...] = DEL_MOTOR,
    estado: EstadoTrabajo = EstadoTrabajo.LISTO,
) -> Trabajo:
    """Un trabajo cortante con sus salidas ya en disco."""
    trabajo = almacen.crear(usuario, TipoTrabajo.CORTANTE)
    destino = ajustes.dir_trabajo / trabajo.id
    destino.mkdir(parents=True, exist_ok=True)
    archivos: dict[str, str] = {}
    for clave in claves:
        nombre, contenido = SALIDAS[clave]
        (destino / nombre).write_bytes(contenido)
        archivos[clave] = nombre
    almacen.actualizar(trabajo.id, estado=estado, archivos=archivos, nombre_base=nombre_base)
    return trabajo


@pytest.mark.parametrize("clave", list(ClaveArchivo))
def test_los_mapas_de_archivo_estan_completos(clave: ClaveArchivo) -> None:
    """Toda clave del enum tiene nombre, media type y sufijo.

    Son tres dicts escritos a mano y desacoplados del enum: olvidarse de uno
    solo se nota cuando alguien intenta bajar ese archivo, con un `KeyError`
    que sale como 500 en una descarga. Esto lo mueve a la suite.
    """
    assert clave in NOMBRE_DE, "falta en NOMBRE_DE"
    assert clave in MEDIO_DE, "falta en MEDIO_DE"
    assert clave in SUFIJO_DESCARGA, "falta en SUFIJO_DESCARGA"


def test_el_zip_trae_los_descargables_y_no_el_glb(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """El ZIP es "todo lo que el usuario puede bajar", no "todo lo que hay".

    El `.glb` existe solo para el visor y no esta en el grupo de descargas de
    la pantalla; meterlo en el ZIP seria entregar un archivo que la app nunca
    ofrecio.
    """
    trabajo = _cortante_listo(
        almacen, ajustes, nombre_base="buddy", claves=(*DEL_MOTOR, "jpg_vista")
    )
    r = sesion.get(f"/api/trabajos/{trabajo.id}/zip")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(BytesIO(r.content)) as z:
        miembros = z.namelist()
        assert z.testzip() is None, "el zip salio corrupto"
    assert len(miembros) == 6
    assert not [n for n in miembros if n.endswith(".glb")]


def test_el_zip_usa_los_nombres_de_descarga(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Adentro del ZIP los archivos se llaman igual que bajados de a uno.

    Un solo criterio de nombre en toda la app: si el boton suelto entrega
    `buddy-cortador.stl`, el ZIP no puede entregar `salida_cortador.stl`.
    """
    trabajo = _cortante_listo(
        almacen, ajustes, nombre_base="buddy", claves=(*DEL_MOTOR, "jpg_vista")
    )
    with zipfile.ZipFile(BytesIO(sesion.get(f"/api/trabajos/{trabajo.id}/zip").content)) as z:
        miembros = sorted(z.namelist())

    assert miembros == sorted(
        [
            "buddy.3mf",
            "buddy-cortador.3mf",
            "buddy-marcador.3mf",
            "buddy-cortador.stl",
            "buddy-marcador.stl",
            "buddy-vista.jpg",
        ]
    )


def test_el_zip_en_modo_cortante_trae_solo_lo_que_hay(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Sin marcador no hay `*_marcador`, y el ZIP no los inventa ni falla."""
    trabajo = _cortante_listo(
        almacen, ajustes, nombre_base="buddy", claves=("3mf", "stl_cortador", "glb", "jpg_vista")
    )
    with zipfile.ZipFile(BytesIO(sesion.get(f"/api/trabajos/{trabajo.id}/zip").content)) as z:
        assert sorted(z.namelist()) == sorted(
            ["buddy.3mf", "buddy-cortador.stl", "buddy-vista.jpg"]
        )


def test_el_zip_nombre_de_descarga(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Con nombre del usuario lo respeta; sin el cae al id, como las sueltas."""
    con_nombre = _cortante_listo(almacen, ajustes, nombre_base="buddy")
    r = sesion.get(f"/api/trabajos/{con_nombre.id}/zip")
    assert 'filename="buddy.zip"' in r.headers["content-disposition"]

    sin_nombre = _cortante_listo(almacen, ajustes)
    r = sesion.get(f"/api/trabajos/{sin_nombre.id}/zip")
    esperado = f'filename="studiocutter-{sin_nombre.id[:8]}.zip"'
    assert esperado in r.headers["content-disposition"]


def test_el_zip_sin_archivos_es_404(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Un ZIP vacio seria peor que un error: parece que la descarga funciono."""
    trabajo = _cortante_listo(almacen, ajustes, claves=())
    r = sesion.get(f"/api/trabajos/{trabajo.id}/zip")

    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "archivo_inexistente"


def test_el_zip_con_solo_el_glb_es_404(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Tener archivos no alcanza: tienen que ser DESCARGABLES."""
    trabajo = _cortante_listo(almacen, ajustes, claves=("glb",))
    r = sesion.get(f"/api/trabajos/{trabajo.id}/zip")

    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "archivo_inexistente"


def test_el_zip_ajeno_es_404(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Las mismas tres defensas que la descarga suelta, sin atajos."""
    ajeno = _cortante_listo(almacen, ajustes, usuario="otra_persona")
    assert sesion.get(f"/api/trabajos/{ajeno.id}/zip").status_code == 404

    for id_ in ("..", "../../etc", "a/b", "%2e%2e", "no-uuid"):
        assert sesion.get(f"/api/trabajos/{id_}/zip").status_code == 404, id_


def test_el_zip_no_deja_temporales(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """El temporal del armado se borra al terminar la respuesta."""
    trabajo = _cortante_listo(almacen, ajustes, nombre_base="buddy")
    directorio = ajustes.dir_trabajo / trabajo.id
    antes = sorted(p.name for p in directorio.iterdir())

    assert sesion.get(f"/api/trabajos/{trabajo.id}/zip").status_code == 200

    assert sorted(p.name for p in directorio.iterdir()) == antes


@pytest.mark.parametrize("rango", ["bytes=abc", "bytes=99999999-", "bytes=0-1"])
def test_el_zip_no_deja_temporales_ni_con_un_range_hostil(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, rango: str
) -> None:
    """El borrado no puede depender de que la respuesta salga bien.

    `FileResponse` maneja `Range` y ante uno invalido o insatisfacible retorna
    **antes** de correr su `background`, asi que el temporal quedaba en disco.
    Como cada pedido escribe un ZIP completo de todas las salidas, repetirlo
    llenaba el directorio del trabajo: es una amplificacion de disco que dispara
    el cliente con un header. Por eso el ZIP se sirve con un generador y el
    borrado vive en un `finally`.
    """
    trabajo = _cortante_listo(almacen, ajustes, nombre_base="buddy")
    directorio = ajustes.dir_trabajo / trabajo.id
    antes = sorted(p.name for p in directorio.iterdir())

    for _ in range(3):
        sesion.get(f"/api/trabajos/{trabajo.id}/zip", headers={"Range": rango})

    assert sorted(p.name for p in directorio.iterdir()) == antes


def test_el_archivo_con_id_que_no_es_uuid_es_404(sesion: TestClient) -> None:
    """La defensa del UUID en la ruta que SIRVE BYTES, no solo en la de estado.

    El test de mas arriba cubre `/api/trabajos/{id}`; esta es la ruta que
    termina en un `open()`, asi que es la que mas importa cubrir.
    """
    for id_ in ("..", "../../etc", "a/b", "%2e%2e", "no-uuid"):
        assert sesion.get(f"/api/trabajos/{id_}/archivo/3mf").status_code == 404, id_


def test_la_imagen_se_sube_y_queda_descargable(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """El recorrido entero: subir la foto y bajarla como un archivo mas."""
    trabajo = _cortante_listo(almacen, ajustes, nombre_base="buddy")

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("loquesea.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    assert "jpg_vista" in r.json()["archivos"]

    bajada = sesion.get(f"/api/trabajos/{trabajo.id}/archivo/jpg_vista")
    assert bajada.status_code == 200
    assert bajada.headers["content-type"] == "image/jpeg"
    assert 'filename="buddy-vista.jpg"' in bajada.headers["content-disposition"]
    assert bajada.content == jpg_minimo


def test_la_imagen_se_puede_reemplazar(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Subir de nuevo pisa la anterior: la foto es la de la ultima vista.

    Es lo que sostiene la frescura del lado del servidor — el front sube antes
    de cada descarga, y eso solo sirve si el PUT reemplaza.
    """
    trabajo = _cortante_listo(almacen, ajustes)
    ruta = f"/api/trabajos/{trabajo.id}/imagen"
    descarga = f"/api/trabajos/{trabajo.id}/archivo/jpg_vista"

    # La primera subida se afirma: sin esto el test pasaria igual aunque la
    # primera fallara, y entonces estaria probando "subir anda" en vez de
    # "reemplazar anda", que es lo que dice su nombre.
    assert (
        sesion.put(ruta, files={"archivo": ("a.jpg", jpg_minimo, "image/jpeg")}).status_code == 200
    )
    assert sesion.get(descarga).content == jpg_minimo

    otra = jpg_minimo + b"\x00" * 16
    assert sesion.put(ruta, files={"archivo": ("b.jpg", otra, "image/jpeg")}).status_code == 200
    assert sesion.get(descarga).content == otra


def test_la_imagen_que_no_es_jpeg_se_rechaza(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, png_minimo: bytes
) -> None:
    """Decide el CONTENIDO, no el `Content-Type` ni el nombre.

    Los dos los elige quien sube, asi que se le miente a proposito a los dos y
    se comprueba que igual no entra — y que no deja residuo en disco.
    """
    trabajo = _cortante_listo(almacen, ajustes)

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("foto.jpg", png_minimo, "image/jpeg")},
    )

    assert r.status_code == 415
    assert r.json()["error"]["codigo"] == "formato_no_soportado"
    assert not (ajustes.dir_trabajo / trabajo.id / "vista.jpg").exists()
    assert "jpg_vista" not in (almacen.obtener(trabajo.id, "tester") or trabajo).archivos


def test_la_imagen_demasiado_grande_se_rechaza(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """El tope propio de la foto corta MIENTRAS copia y no deja basura."""
    trabajo = _cortante_listo(almacen, ajustes)
    gigante = jpg_minimo + b"\x00" * (LIMITE_VISTA_BYTES + 1)

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("foto.jpg", gigante, "image/jpeg")},
    )

    assert r.status_code == 413
    # Y que lo corte el tope PROPIO de la foto, no el general de 25 MB del
    # middleware: si no, el limite de 4 MB no estaria haciendo nada y nadie se
    # enteraria. Los dos dan 413, asi que el codigo es lo que los distingue.
    assert r.json()["error"]["codigo"] == "archivo_muy_grande"
    assert len(gigante) < ajustes.tamano_maximo_bytes, (
        "el caso tiene que estar bajo el tope general"
    )
    assert not (ajustes.dir_trabajo / trabajo.id / "vista.jpg").exists()


def test_la_imagen_rechaza_un_trabajo_ajeno(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    ajeno = _cortante_listo(almacen, ajustes, usuario="otra_persona")
    r = sesion.put(
        f"/api/trabajos/{ajeno.id}/imagen",
        files={"archivo": ("foto.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "trabajo_inexistente"


def test_la_imagen_rechaza_un_id_que_no_es_uuid(sesion: TestClient, jpg_minimo: bytes) -> None:
    for id_ in ("..", "../../etc", "a/b", "no-uuid"):
        r = sesion.put(
            f"/api/trabajos/{id_}/imagen",
            files={"archivo": ("foto.jpg", jpg_minimo, "image/jpeg")},
        )
        assert r.status_code == 404, id_


def _post_listo(
    almacen: AlmacenEnMemoria,
    ajustes: Ajustes,
    *,
    usuario: str = "tester",
    estado: EstadoTrabajo = EstadoTrabajo.LISTO,
) -> Trabajo:
    """El gemelo de `_cortante_listo` para F4: un trabajo post con su `.glb`."""
    trabajo = almacen.crear(usuario, TipoTrabajo.POST)
    destino = ajustes.dir_trabajo / trabajo.id
    destino.mkdir(parents=True, exist_ok=True)
    nombre, contenido = SALIDAS["glb"]
    (destino / nombre).write_bytes(contenido)
    almacen.actualizar(trabajo.id, estado=estado, archivos={"glb": nombre})
    return trabajo


def test_la_imagen_se_acepta_en_un_trabajo_post(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """F4 existe justamente para producir esta foto: tiene que poder subirla.

    Es lo que `TIPOS_CON_VISTA` habilita. Sin esto el PUT responde 404 y la
    pantalla entera no sirve para nada — el sintoma seria "el servidor rechazo
    la imagen" en la pista, con el visor andando perfecto.
    """
    trabajo = _post_listo(almacen, ajustes)

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("vista.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    assert "jpg_vista" in r.json()["archivos"]
    assert (ajustes.dir_trabajo / trabajo.id / "vista.jpg").is_file()


def test_los_tipos_con_vista_son_los_que_dejan_glb() -> None:
    """`TIPOS_CON_VISTA` no es una lista suelta: es "los que dejan un .glb"."""
    assert TIPOS_CON_VISTA == {TipoTrabajo.CORTANTE, TipoTrabajo.POST}
    assert TipoTrabajo.CONVERSOR not in TIPOS_CON_VISTA
    assert TipoTrabajo.LINEAS not in TIPOS_CON_VISTA


def test_la_imagen_rechaza_un_trabajo_sin_vista_3d(
    sesion: TestClient, almacen: AlmacenEnMemoria, jpg_minimo: bytes
) -> None:
    """El Convertidor y Correcto no dejan `.glb`: una foto ahi no significa nada.

    Responde 404 y no 403 a proposito: el mismo cuerpo que un trabajo
    inexistente, para no filtrar que el id existe.
    """
    otro = almacen.crear("tester", TipoTrabajo.CONVERSOR)
    almacen.actualizar(otro.id, estado=EstadoTrabajo.LISTO, archivos={"jpg": "salida.jpg"})

    r = sesion.put(
        f"/api/trabajos/{otro.id}/imagen",
        files={"archivo": ("foto.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "trabajo_inexistente"


def test_la_imagen_rechaza_un_trabajo_que_no_termino(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Una foto de una geometria que todavia no existe seria de otra cosa."""
    en_curso = _cortante_listo(almacen, ajustes, claves=(), estado=EstadoTrabajo.PROCESANDO)

    r = sesion.put(
        f"/api/trabajos/{en_curso.id}/imagen",
        files={"archivo": ("foto.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "trabajo_no_terminado"


def test_subir_la_imagen_no_refresca_el_ttl_del_trabajo(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Subir la foto describe el trabajo; no cuenta como usarlo.

    `almacen.actualizar` fija `actualizado_en = now()` siempre, y de ese campo
    depende `vencidos()`, o sea el TTL de 6 h — la unica cota de disco total
    que tiene el diseño. Este es el primer camino por el que un cliente escribe
    sobre un trabajo YA TERMINADO, asi que sin esto un PUT de tres bytes cada
    cinco horas —invisible frente al freno de 240 pedidos por minuto— deja un
    directorio con todas las salidas vivo para siempre.
    """
    trabajo = _cortante_listo(almacen, ajustes)
    antes = (almacen.obtener(trabajo.id, "tester") or trabajo).actualizado_en

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("foto.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 200, r.text

    despues = (almacen.obtener(trabajo.id, "tester") or trabajo).actualizado_en
    assert despues == antes, "el PUT no puede empujar el vencimiento del trabajo"


def test_una_subida_fallida_no_destruye_la_imagen_anterior(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """La escritura es atomica: o entra la nueva, o queda la vieja intacta.

    Desde que `guardar_subida` acepta `destino_nombre`, el destino puede ser un
    archivo VIVO en vez de un `entrada.<ext>` nuevo. Abrirlo en `"wb"` lo
    truncaba antes de saber si la subida entraba, asi que un segundo PUT
    demasiado grande borraba una foto valida y dejaba a `trabajo.archivos`
    afirmando que seguia ahi: la descarga pasaba a 404 y el ZIP perdia el
    miembro con un warning.
    """
    trabajo = _cortante_listo(almacen, ajustes, nombre_base="buddy")
    ruta = f"/api/trabajos/{trabajo.id}/imagen"
    assert (
        sesion.put(ruta, files={"archivo": ("a.jpg", jpg_minimo, "image/jpeg")}).status_code == 200
    )

    gigante = jpg_minimo + b"\x00" * (LIMITE_VISTA_BYTES + 1)
    assert sesion.put(ruta, files={"archivo": ("b.jpg", gigante, "image/jpeg")}).status_code == 413

    bajada = sesion.get(f"/api/trabajos/{trabajo.id}/archivo/jpg_vista")
    assert bajada.status_code == 200, "la foto valida anterior tiene que sobrevivir"
    assert bajada.content == jpg_minimo
    # Y el ZIP la sigue trayendo: `archivos` no puede quedar prometiendo un
    # archivo que ya no esta.
    with zipfile.ZipFile(BytesIO(sesion.get(f"/api/trabajos/{trabajo.id}/zip").content)) as z:
        assert "buddy-vista.jpg" in z.namelist()


def test_la_subida_no_deja_temporales(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Ni cuando entra ni cuando se rechaza por tamaño."""
    trabajo = _cortante_listo(almacen, ajustes)
    directorio = ajustes.dir_trabajo / trabajo.id
    ruta = f"/api/trabajos/{trabajo.id}/imagen"

    sesion.put(ruta, files={"archivo": ("a.jpg", jpg_minimo, "image/jpeg")})
    sesion.put(
        ruta,
        files={"archivo": ("b.jpg", jpg_minimo + b"\x00" * (LIMITE_VISTA_BYTES + 1), "image/jpeg")},
    )

    assert not list(directorio.glob(".subida-*.tmp")), "quedo un temporal de subida"


def test_el_tipo_se_chequea_antes_que_el_estado(
    sesion: TestClient, almacen: AlmacenEnMemoria, jpg_minimo: bytes
) -> None:
    """Un trabajo ajeno al cortante Y sin terminar tiene que dar 404, no 409.

    El orden de las defensas importa y ningun otro test lo fija: los casos de
    "no es cortante" y "no termino" usan trabajos que cumplen la otra
    condicion, asi que los dos chequeos se podrian intercambiar sin que nada se
    ponga en rojo. Este es el caso discriminante — y el orden correcto es el que
    NO filtra informacion: 404 (no existe para vos) le gana a 409 (existe pero
    todavia no).
    """
    otro = almacen.crear("tester", TipoTrabajo.CONVERSOR)
    almacen.actualizar(otro.id, estado=EstadoTrabajo.PROCESANDO)

    r = sesion.put(
        f"/api/trabajos/{otro.id}/imagen",
        files={"archivo": ("foto.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 404, "el tipo se tiene que chequear antes que el estado"


def test_claves_descargables_filtra_lo_que_no_es_del_enum() -> None:
    """Lo que el hijo declaro y no esta en `ClaveArchivo` no entra al ZIP.

    Hoy `_claves_conocidas` ya lo descarta en la frontera con el proceso hijo,
    pero esta funcion no depende de eso —y no deberia—: es la que decide que
    bytes se empaquetan.
    """
    assert claves_descargables({"png": "salida.png", "credenciales": "x"}) == [ClaveArchivo.PNG]
    assert claves_descargables({"glb": "salida.glb"}) == []


def test_el_nombre_del_zip_ignora_una_base_insegura() -> None:
    """Un `nombre_base` heredado sin sanear no puede salir en el header.

    Es la razon misma de que `nombre_de_zip` llame a `base_es_segura`: el stem
    se sanea al subir, pero despues se HEREDA de un trabajo a otro sin volver a
    pasar por el saneo, y el consumidor no puede confiar en esa convencion.
    """
    id_ = "2f1b3e0c-1111-4000-8000-000000000000"
    assert nombre_de_zip(id_, "buddy") == "buddy.zip"
    for insegura in ("../../evil", "con espacio", ".oculto", ""):
        assert nombre_de_zip(id_, insegura) == f"studiocutter-{id_[:8]}.zip", insegura


def test_la_imagen_no_cambia_el_nombre_de_descarga_del_trabajo(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Subir la foto no puede pisar el `nombre_base` del trabajo.

    De ese valor dependen los nombres de TODAS las descargas y el del ZIP. El
    PUT no pasa `nombre_cliente` justamente por eso, y esto lo fija.
    """
    trabajo = _cortante_listo(almacen, ajustes, nombre_base="buddy")
    sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("otro_nombre.jpg", jpg_minimo, "image/jpeg")},
    )

    assert (almacen.obtener(trabajo.id, "tester") or trabajo).nombre_base == "buddy"
    disposicion = sesion.get(f"/api/trabajos/{trabajo.id}/zip").headers["content-disposition"]
    assert 'filename="buddy.zip"' in disposicion


def test_la_imagen_el_cliente_no_nombra_el_archivo(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Un `filename` hostil no llega al disco: el destino sale de `NOMBRE_DE`."""
    trabajo = _cortante_listo(almacen, ajustes)
    directorio = ajustes.dir_trabajo / trabajo.id
    antes = sorted(p.name for p in directorio.iterdir())

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/imagen",
        files={"archivo": ("../../credenciales.json", jpg_minimo, "image/jpeg")},
    )

    assert r.status_code == 200, r.text
    assert sorted(p.name for p in directorio.iterdir()) == sorted([*antes, "vista.jpg"])
    # El `filename` apunta justo al archivo de credenciales que el andamiaje
    # deja un nivel arriba del directorio de trabajos. Existe por el fixture,
    # asi que lo que se comprueba no es que falte: es que **siga siendo el
    # JSON** y no los bytes del JPEG. Se parsea entero y no se mira el primer
    # byte, para que tambien falle si el JPEG quedo appendeado al final.
    assert "usuarios" in json.loads(ajustes.archivo_credenciales.read_text(encoding="utf-8"))


# ── F4 con varios diseños: la serie ─────────────────────────────────────────


def tarea_que_anota_su_paso(dir_trabajo: str, indice: int) -> None:
    """Deja rastro de CUANDO corrio, para poder afirmar que fue en serie.

    Escribe el momento de arranque y el de fin. Dos pasos en paralelo se
    solaparian en esa linea de tiempo; en serie no pueden.
    """
    inicio = time.monotonic()
    time.sleep(0.35)
    escribir_estado(
        Path(dir_trabajo),
        {
            "ok": True,
            "etapa": "listo",
            "archivos": {},
            "reporte": {"inicio": inicio, "fin": time.monotonic()},
        },
        indice,
    )


def tarea_que_falla_en_el_paso_dos(dir_trabajo: str, indice: int) -> None:
    """El segundo diseño no se puede leer; los otros si."""
    if indice == 2:
        escribir_estado(
            Path(dir_trabajo),
            {
                "ok": False,
                "etapa": "error",
                "archivos": {},
                "error": {"codigo": "malla_ilegible", "mensaje": "No se pudo leer.", "detalle": {}},
            },
            indice,
        )
        return
    escribir_estado(
        Path(dir_trabajo), {"ok": True, "etapa": "listo", "archivos": {}, "reporte": {}}, indice
    )


def _lanzar_serie(almacen, ajustes, tarea, pasos: int, usuario="tester"):
    trabajo = almacen.crear(usuario, TipoTrabajo.POST)
    destino = ajustes.dir_trabajo / trabajo.id
    destino.mkdir(parents=True, exist_ok=True)
    almacen.actualizar(trabajo.id, disenos=pasos)
    lanzar_serie(
        almacen=almacen,
        a=ajustes,
        trabajo=trabajo,
        objetivo=tarea,
        pasos=tuple((str(destino), i) for i in range(1, pasos + 1)),
        reporte_base={"nombres": [f"d{i}" for i in range(1, pasos + 1)]},
    )
    return trabajo


@pytest.mark.lento
def test_los_disenos_corren_de_a_uno_y_no_a_la_vez(
    almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Lo que se pidio: terminar un PID y recien ahi arrancar el siguiente.

    No se mide "tarda mas": se mide que los intervalos **no se solapan**. Con
    tres procesos en paralelo el total seria parecido al de uno solo y un test
    de duracion pasaria igual.
    """
    trabajo = _lanzar_serie(almacen, ajustes, tarea_que_anota_su_paso, 3)
    final = esperar_estado(almacen, trabajo.id, "tester")
    assert final.estado is EstadoTrabajo.LISTO, final.error

    tramos = [d["reporte"] for d in final.reporte["disenos"]]
    assert len(tramos) == 3
    for anterior, siguiente in itertools.pairwise(tramos):
        assert anterior["fin"] <= siguiente["inicio"], (
            f"dos diseños se solaparon: {anterior} y {siguiente}"
        )


@pytest.mark.lento
def test_un_diseno_que_falla_no_se_lleva_puestos_a_los_demas(
    almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Tirar 24 diseños buenos porque uno vino corrupto seria cambiar un
    problema chico por uno grande. Lo que falla se declara."""
    trabajo = _lanzar_serie(almacen, ajustes, tarea_que_falla_en_el_paso_dos, 3)
    final = esperar_estado(almacen, trabajo.id, "tester")

    assert final.estado is EstadoTrabajo.LISTO
    assert final.reporte["logrados"] == 2
    assert final.reporte["total"] == 3
    fallados = [d for d in final.reporte["disenos"] if not d["ok"]]
    assert [d["indice"] for d in fallados] == [2]
    assert fallados[0]["error"]["codigo"] == "malla_ilegible"


@pytest.mark.lento
def test_la_serie_conserva_lo_que_el_router_dejo_en_el_reporte(
    almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Sin `reporte_base`, el cierre reemplaza el reporte entero y las descargas
    se quedan sin nombre — y no se ve hasta bajar un archivo."""
    trabajo = _lanzar_serie(almacen, ajustes, tarea_que_anota_su_paso, 2)
    final = esperar_estado(almacen, trabajo.id, "tester")
    assert final.reporte["nombres"] == ["d1", "d2"]


@pytest.mark.lento
def test_una_serie_ocupa_un_solo_lugar_del_cupo(
    almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    """Y lo ocupa TAMBIEN entre paso y paso, cuando no tiene ningun hijo vivo.

    Ese hueco es lo que `_series` cierra: contando solo procesos, dos series
    alternadas se cuelan por encima del tope de `max_trabajos_simultaneos`.
    """
    trabajo = _lanzar_serie(almacen, ajustes, tarea_que_anota_su_paso, 3)
    assert vivos() >= 1
    esperar_estado(almacen, trabajo.id, "tester")
    assert vivos() == 0, "al cerrar, la serie tiene que soltar su lugar"


def test_la_foto_de_un_diseno_exige_que_ese_diseno_exista(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Pedir el diseño 20 de un trabajo de 3 es 404, y por una regla — no por no
    encontrar el archivo, que seria depender de un accidente del disco."""
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=3)

    for indice in (0, 4, 99):
        r = sesion.put(
            f"/api/trabajos/{trabajo.id}/diseno/{indice}/imagen/jpg_vista",
            files={"archivo": ("v.jpg", jpg_minimo, "image/jpeg")},
        )
        assert r.status_code == 404, indice
        assert r.json()["error"]["codigo"] == "diseno_inexistente"


def test_el_set_necesita_al_menos_una_foto(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes
) -> None:
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=2)
    r = sesion.post(f"/api/trabajos/{trabajo.id}/set")
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "sin_fotos"


def test_el_set_se_arma_con_las_fotos_que_hay(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Un diseño que fallo no impide el set: sale con los que si salieron."""
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=3)
    for indice in (1, 3):  # el 2 no tiene foto
        subida = sesion.put(
            f"/api/trabajos/{trabajo.id}/diseno/{indice}/imagen/jpg_set",
            files={"archivo": ("v.jpg", jpg_minimo, "image/jpeg")},
        )
        assert subida.status_code == 200, subida.text

    r = sesion.post(f"/api/trabajos/{trabajo.id}/set")
    assert r.status_code == 200, r.text
    assert r.json()["celdas"] == 2
    assert "set" in r.json()["trabajo"]["archivos"]
    assert (ajustes.dir_trabajo / trabajo.id / "set.jpg").is_file()


def _subir_fotos(
    sesion: TestClient,
    trabajo_id: str,
    cuantas: int,
    jpg: bytes,
    claves: tuple[str, ...] = ("jpg_vista", "jpg_set"),
) -> None:
    """Las fotos de `cuantas` diseños. Por default las DOS de cada uno.

    Son dos y no una porque el navegador rinde cada pieza dos veces: la foto
    suelta con los colores del diseño y la celda con los del set. Lo que entra
    al set es la celda; quien quiera probar que entra esa y no la otra, pasa
    `claves` y sube una sola.
    """
    for indice in range(1, cuantas + 1):
        for clave in claves:
            r = sesion.put(
                f"/api/trabajos/{trabajo_id}/diseno/{indice}/imagen/{clave}",
                files={"archivo": ("v.jpg", jpg, "image/jpeg")},
            )
            assert r.status_code == 200, r.text


def test_el_set_usa_el_fondo_que_se_le_pide_y_no_el_de_las_fotos(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """El fondo del set es SUYO: no se deduce de las fotos ni se puede.

    El set tiene su propia combinacion de pieza y fondo, y cada celda se rinde
    con ella; lo que llega aca es el mismo color con el que se rindieron, para
    los huecos. Deducirlo de un pixel de la primera celda volveria a atar dos
    cosas que el usuario eligio por separado.

    El caso esta armado para que no pueda pasar por casualidad: `jpg_minimo`
    tiene fondo **blanco** y el set se pide **violeta**. Si alguien volviera a
    leerlo de la primera foto, el pixel saldria blanco.

    ⚠ Se mira (1, 1) y eso vale **porque son dos diseños**: dos fotos van en una
    sola fila (`distribucion` = `(2,)`), la lamina es cuadrada igual y entonces
    arriba y abajo queda margen. Con cuatro, el reparto `2-2` llena el cuadro y
    la esquina seria la primera celda — que es justo el error que tuvo la
    primera version de este test.
    """
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=2)
    _subir_fotos(sesion, trabajo.id, 2, jpg_minimo)

    elegido = next(c for c in COLORES if c.nombre == "Violeta")
    r = sesion.post(f"/api/trabajos/{trabajo.id}/set", data={"fondo": elegido.hex})
    assert r.status_code == 200, r.text
    assert r.json()["distribucion"] == [2], "el reparto cambio y (1, 1) ya no es margen"

    with Image.open(ajustes.dir_trabajo / trabajo.id / "set.jpg") as imagen:
        esquina = imagen.convert("RGB").getpixel((1, 1))
    assert all(abs(a - b) <= 3 for a, b in zip(esquina, elegido.rgb, strict=True)), (
        f"el margen del set no tiene el color pedido: {esquina}"
    )


def test_el_set_se_arma_con_las_celdas_y_no_con_las_fotos_sueltas(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Las dos fotos por diseño existen justamente para esto.

    La suelta lleva los colores de su diseño y la celda los del set. Si el set
    se compusiera con las sueltas, los colores del set no podrian ser distintos
    de los de cada foto — que es todo el punto de que sean dos.

    Se prueba por la unica via que no depende de mirar pixeles: subiendo **solo**
    las sueltas. Si el set las usara, se armaria igual; como usa las celdas,
    responde que no hay ninguna. La otra mitad —que con las celdas si se arma—
    la cubre `test_el_set_se_arma_con_las_fotos_que_hay`.
    """
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=2)
    _subir_fotos(sesion, trabajo.id, 2, jpg_minimo, claves=("jpg_vista",))

    r = sesion.post(f"/api/trabajos/{trabajo.id}/set")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["codigo"] == "sin_fotos"
    assert not (ajustes.dir_trabajo / trabajo.id / "set.jpg").exists(), (
        "el set se armo con las fotos sueltas, que llevan el color de cada diseño"
    )


def test_el_glb_de_un_diseno_no_se_puede_subir(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """La clave viaja en la URL, asi que hay que decir cuales se aceptan.

    `ClaveDiseno` tiene tres miembros y uno —el `.glb`— lo produce el servidor
    al convertir el archivo que subio el usuario. Dejar que entre por la ruta de
    la foto seria aceptar una malla que nadie verifico, y encima pisar la que si
    se verifico. Es la misma razon por la que `CLAVES_DISENO_INTERNAS` existe.
    """
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=1)

    r = sesion.put(
        f"/api/trabajos/{trabajo.id}/diseno/1/imagen/glb",
        files={"archivo": ("v.jpg", jpg_minimo, "image/jpeg")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["codigo"] == "clave_no_subible"

    # Y una clave que no esta en el enum la para FastAPI antes de entrar.
    inventada = sesion.put(
        f"/api/trabajos/{trabajo.id}/diseno/1/imagen/jpg_lo_que_sea",
        files={"archivo": ("v.jpg", jpg_minimo, "image/jpeg")},
    )
    assert inventada.status_code == 422, inventada.text


def test_un_fondo_de_set_fuera_de_la_paleta_se_rechaza(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """El cliente elige de una lista cerrada, como en todo el resto de la API.

    No es que un hex cualquiera rompa la composicion —Pillow pinta cualquier
    terna— sino que la paleta tiene un solo dueño. Aceptar un color que no esta
    en `COLORES` seria dejar que el front invente uno, que es la misma puerta
    que este proyecto cierra con los nombres de archivo.
    """
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=1)
    _subir_fotos(sesion, trabajo.id, 1, jpg_minimo)

    r = sesion.post(f"/api/trabajos/{trabajo.id}/set", data={"fondo": "#123456"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["codigo"] == "fondo_invalido"
    assert not (ajustes.dir_trabajo / trabajo.id / "set.jpg").exists(), (
        "se escribio el set con un fondo que se rechazo"
    )


def test_el_set_sin_fondo_explicito_usa_el_primero_de_la_paleta(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """El default del endpoint y el que marca el template salen de `COLORES[0]`.

    La pantalla manda siempre el campo; esto cubre a quien no lo manda —un
    trabajo viejo, un test— y sobre todo fija que el default no sea un color
    escrito aparte, que es como se desincronizan dos verdades.
    """
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=2)
    _subir_fotos(sesion, trabajo.id, 2, jpg_minimo)

    assert sesion.post(f"/api/trabajos/{trabajo.id}/set").status_code == 200
    with Image.open(ajustes.dir_trabajo / trabajo.id / "set.jpg") as imagen:
        esquina = imagen.convert("RGB").getpixel((1, 1))
    assert all(abs(a - b) <= 3 for a, b in zip(esquina, COLORES[0].rgb, strict=True)), esquina


def test_armar_el_set_no_refresca_el_ttl(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Mismo motivo que subir la foto: describe el trabajo, no es usarlo."""
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=1)
    _subir_fotos(sesion, trabajo.id, 1, jpg_minimo)
    guardado = almacen.obtener(trabajo.id, "tester")
    assert guardado is not None
    antes = guardado.actualizado_en
    assert sesion.post(f"/api/trabajos/{trabajo.id}/set").status_code == 200
    despues = almacen.obtener(trabajo.id, "tester")
    assert despues is not None
    assert despues.actualizado_en == antes


def test_el_zip_de_un_post_trae_las_fotos_y_el_set(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """Las fotos por diseño NO estan en `archivos` —se piden por indice— asi que
    el ZIP tiene que juntarlas aparte. Bajar 25 de a una es lo que este boton
    existe para evitar."""
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=2, reporte={"nombres": ["kitty-bruja", "murcielago"]})
    _subir_fotos(sesion, trabajo.id, 2, jpg_minimo)
    sesion.post(f"/api/trabajos/{trabajo.id}/set")

    r = sesion.get(f"/api/trabajos/{trabajo.id}/zip")
    assert r.status_code == 200
    with zipfile.ZipFile(BytesIO(r.content)) as zip_:
        nombres = sorted(zip_.namelist())
    assert "kitty-bruja-01-vista.jpg" in nombres
    assert "murcielago-02-vista.jpg" in nombres
    assert any(n.endswith("-set.jpg") for n in nombres), nombres
    # ⚠ Y NO las celdas, aunque sean JPG por diseño que estan en disco: la
    # celda existe para armar la lamina, que ya va entera. Bajar las dos fotos
    # de cada pieza obliga a adivinar cual de las dos es la que se pidio.
    assert not any("-celda" in n for n in nombres), nombres


def test_dos_disenos_homonimos_no_se_pisan_en_el_zip(
    sesion: TestClient, almacen: AlmacenEnMemoria, ajustes: Ajustes, jpg_minimo: bytes
) -> None:
    """⚠ Sin el indice, la segunda foto entra con la misma clave que la primera
    y el usuario se lleva una foto menos creyendo que las tiene todas."""
    trabajo = _post_listo(almacen, ajustes)
    almacen.actualizar(trabajo.id, disenos=2, reporte={"nombres": ["gato", "gato"]})
    _subir_fotos(sesion, trabajo.id, 2, jpg_minimo)

    r = sesion.get(f"/api/trabajos/{trabajo.id}/zip")
    with zipfile.ZipFile(BytesIO(r.content)) as zip_:
        nombres = zip_.namelist()
    assert len(nombres) == len(set(nombres)) == 2, nombres
