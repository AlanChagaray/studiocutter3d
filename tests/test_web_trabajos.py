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

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.almacen import AlmacenEnMemoria, EstadoTrabajo, TipoTrabajo
from app.archivos import escribir_estado
from app.config import Ajustes
from app.errores import ErrorApi
from app.trabajos import cancelar, lanzar, proceso_de

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
    assert "studiocutter-" in descarga.headers["content-disposition"]


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


def test_los_nueve_parametros_por_defecto_se_aceptan(sesion: TestClient) -> None:
    """Con los defaults, el rechazo tiene que ser por el archivo y no por un numero."""
    r = sesion.post("/api/cortante", data={})
    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "falta_archivo"


def test_en_modo_cortante_los_parametros_del_marcador_son_opcionales(
    sesion: TestClient,
) -> None:
    """La pantalla no los manda porque el motor los ignora: no pueden ser obligatorios."""
    estrella = (FIXTURES / "estrella.svg").read_bytes()
    r = sesion.post(
        "/api/cortante",
        files={"archivo": ("estrella.svg", estrella, "image/svg+xml")},
        data={"modo": "cortante", "lado_mayor_mm": "70", "con_stl": "false"},
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

    cuerpo = str(final)
    for filtracion in ("C:\\", "/Users/", ".venv", str(ajustes.dir_trabajo), "Temp"):
        assert filtracion not in cuerpo, f"la respuesta filtro {filtracion!r}: {cuerpo}"


# ── Motor real, de punta a punta ─────────────────────────────────────────────


@pytest.mark.lento
def test_cortante_real_de_punta_a_punta(sesion: TestClient, ajustes: Ajustes) -> None:
    """El unico test que corre el motor: POST, polling, y los archivos abren."""
    svg = (FIXTURES / "estrella.svg").read_bytes()
    r = sesion.post(
        "/api/cortante",
        files={"archivo": ("estrella.svg", svg, "image/svg+xml")},
        data={"modo": "cortante+marcador", "con_stl": "true"},
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
    """F2 deja PNG **y** SVG, y ese SVG es lo que acepta el cortante.

    Es el unico camino que queda desde una imagen: el cortante solo toma SVG,
    asi que si F2 no lo produjera, el boton de seguir estaria roto.
    """
    r = sesion.post("/api/lineas", files={"archivo": ("d.jpg", jpg_minimo, "image/jpeg")})
    assert r.status_code == 200
    lineas = sondear_hasta_el_final(sesion, r.json()["id"], limite_s=180)
    assert lineas["estado"] == "listo", lineas
    assert set(lineas["archivos"]) == {"png", "svg"}, lineas["archivos"]

    svg = sesion.get(f"/api/trabajos/{lineas['id']}/archivo/svg")
    assert svg.status_code == 200
    assert b"<svg" in svg.content[:512].lower()

    encadenado = sesion.post("/api/cortante", data={"origen": lineas["id"]})
    assert encadenado.status_code == 200, encadenado.text
    cancelar_trabajo(sesion, encadenado.json()["id"])
