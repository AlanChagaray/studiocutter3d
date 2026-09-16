"""Las defensas del borde: IP real, freno, cabeceras y `Host` permitido.

Dos escalas de test conviviendo, a proposito:

- **Sobre la app de verdad** lo que tiene que estar atado a ella: que el login
  se frene, que `/salud` no pida sesion, y que el `nonce` del CSP sea el mismo
  que lleva el import map del cortante (si se desincronizan, la vista previa
  desaparece sin un solo error a la vista).
- **Sobre una app de banco de pruebas** lo que depende de ajustes que la app
  real toma una sola vez al arrancar —`hosts_permitidos`, `cookie_secure`, el
  tope de pedidos—. El middleware no recibe `dependency_overrides`, asi que la
  unica forma honesta de probar esos casos es armar una app chica con los
  ajustes puestos, en vez de aflojar el codigo de produccion para que sea
  testeable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import CLAVE, USUARIO
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Ajustes, ErrorDeConfiguracion
from app.proteccion import (
    FRENO_LOGIN,
    Freno,
    aplicar_cabeceras,
    instalar,
    ip_cliente,
    politica_de_contenido,
)
from app.seguridad import VARIABLE_CREDENCIALES, cargar_usuarios

IP_LOCAL = "testclient"
"""La IP que usa `TestClient`. Todos los tests salen de ella, que es justo el
motivo por el que los frenos se reinician entre test (ver `conftest`)."""


# ── Banco de pruebas ─────────────────────────────────────────────────────────


def _app_de_borde(**ajustes: object) -> TestClient:
    """Una app minima con las defensas instaladas y los ajustes que se pidan."""
    a = Ajustes(**ajustes)  # type: ignore[arg-type]
    aplicacion = FastAPI()

    @aplicacion.get("/eco")
    def eco() -> dict[str, bool]:
        return {"ok": True}

    @aplicacion.get("/static/falso.css")
    def estatico() -> dict[str, bool]:
        return {"ok": True}

    instalar(aplicacion, a)
    return TestClient(aplicacion, follow_redirects=False)


# ── De quien es el pedido ────────────────────────────────────────────────────


def _pedido(cabecera: str | None, cliente: str = "10.0.0.9") -> Request:
    cabeceras = [(b"x-forwarded-for", cabecera.encode())] if cabecera is not None else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": "http",
            "query_string": b"",
            "headers": cabeceras,
            "client": (cliente, 1234),
        }
    )


def test_sin_proxy_la_cabecera_reenviada_se_ignora() -> None:
    """Es el caso que convierte la defensa en su propio bypass si se hace mal."""
    a = Ajustes(detras_de_proxy=False)
    assert ip_cliente(_pedido("1.2.3.4"), a) == "10.0.0.9"


def test_con_proxy_se_toma_la_ultima_publica_y_no_la_primera() -> None:
    """La izquierda la escribe el cliente; la derecha, la infraestructura.

    `8.8.8.8` es lo que el atacante se invento para saltarse el freno. Lo que
    vale es `200.1.2.3`, que lo agrego el proxy.
    """
    a = Ajustes(detras_de_proxy=True)
    assert ip_cliente(_pedido("8.8.8.8, 200.1.2.3"), a) == "200.1.2.3"


def test_los_saltos_internos_del_proxy_no_tapan_al_cliente() -> None:
    """Las privadas de la derecha se saltean: son iguales para todo el mundo."""
    a = Ajustes(detras_de_proxy=True)
    assert ip_cliente(_pedido("8.8.8.8, 200.1.2.3, 10.0.0.5"), a) == "200.1.2.3"


def test_sin_cabecera_reenviada_se_usa_el_peer() -> None:
    a = Ajustes(detras_de_proxy=True)
    assert ip_cliente(_pedido(None), a) == "10.0.0.9"


# ── El freno ─────────────────────────────────────────────────────────────────


def test_el_freno_bloquea_al_llegar_al_tope() -> None:
    freno = Freno()
    for _ in range(2):
        assert freno.registrar("x", ventana_s=60, tope=3, bloqueo_s=30) == 0

    assert freno.registrar("x", ventana_s=60, tope=3, bloqueo_s=30) == 30
    assert freno.espera_restante("x") > 0
    assert freno.espera_restante("otra") == 0, "el bloqueo es por clave, no global"


def test_perdonar_borra_el_historial() -> None:
    """Lo que hace que tres errores y despues un acierto no dejen deuda."""
    freno = Freno()
    freno.registrar("x", ventana_s=60, tope=3, bloqueo_s=30)
    freno.registrar("x", ventana_s=60, tope=3, bloqueo_s=30)
    freno.perdonar("x")

    for _ in range(2):
        assert freno.registrar("x", ventana_s=60, tope=3, bloqueo_s=30) == 0


def test_el_freno_no_crece_sin_limite() -> None:
    """Un contador por IP sin tope es, el mismo, un agotamiento de memoria.

    Con 5 de tope y 200 IPs distintas —que es lo que deja un atacante rotando
    origen— la memoria tiene que quedarse en 5 y no en 200.
    """
    freno = Freno(max_claves=5)
    for i in range(200):
        freno.registrar(f"ip-{i}", ventana_s=60, tope=1000, bloqueo_s=30)

    assert freno.claves_recordadas() <= 5 + 1, "la poda desaloja las mas viejas"


def test_el_middleware_corta_la_rafaga_y_dice_cuando_volver() -> None:
    cliente = _app_de_borde(max_pedidos_ip=3, ventana_pedidos_s=60)
    for _ in range(2):
        assert cliente.get("/eco").status_code == 200

    respuesta = cliente.get("/eco")
    assert respuesta.status_code == 429
    assert respuesta.json()["error"]["codigo"] == "demasiados_pedidos"
    assert int(respuesta.headers["retry-after"]) > 0


# ── Cabeceras ────────────────────────────────────────────────────────────────


def test_toda_respuesta_lleva_las_cabeceras_de_seguridad(cliente: TestClient) -> None:
    respuesta = cliente.get("/login")
    assert respuesta.headers["x-content-type-options"] == "nosniff"
    assert respuesta.headers["x-frame-options"] == "DENY"
    assert respuesta.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in respuesta.headers["content-security-policy"]
    assert respuesta.headers["cache-control"] == "no-store"


def test_los_estaticos_si_se_pueden_cachear() -> None:
    """Son publicos, y cachearlos es lo que baja three.js una sola vez."""
    cliente = _app_de_borde()
    respuesta = cliente.get("/static/falso.css")
    assert "content-security-policy" in respuesta.headers
    assert "cache-control" not in respuesta.headers


def test_hsts_solo_con_tls_adelante() -> None:
    """Mandarlo desde localhost dejaria la app inaccesible hasta limpiar el navegador."""
    sin_tls = _app_de_borde(cookie_secure=False).get("/eco")
    con_tls = _app_de_borde(cookie_secure=True).get("/eco")

    assert "strict-transport-security" not in sin_tls.headers
    assert "max-age=" in con_tls.headers["strict-transport-security"]


def test_el_csp_no_permite_scripts_inline_sueltos() -> None:
    politica = politica_de_contenido("abc123")
    assert "script-src 'self' 'nonce-abc123'" in politica
    assert "'unsafe-inline'" not in politica.split("style-src")[0], (
        "el script-src no puede permitir inline: el nonce es toda la excepcion"
    )


def test_las_cabeceras_no_dependen_de_que_haya_cuerpo() -> None:
    """`aplicar_cabeceras` es una funcion pura sobre la respuesta ya armada."""
    respuesta = JSONResponse({})
    aplicar_cabeceras(respuesta, nonce="n", a=Ajustes(), es_estatico=False)
    assert respuesta.headers["cross-origin-opener-policy"] == "same-origin"


@pytest.mark.parametrize("pagina", ["/cortante", "/post"])
def test_el_nonce_del_csp_es_el_que_lleva_el_import_map(sesion: TestClient, pagina: str) -> None:
    """Si se desincronizan, el import map no se evalua y el visor 3D desaparece.

    Y no hay error visible en ningun lado: el grafo de modulos deja de
    evaluarse en silencio. Es el mismo modo de falla que ya costo un ciclo.
    """
    respuesta = sesion.get(pagina)
    nonce_cabecera = re.search(r"'nonce-([^']+)'", respuesta.headers["content-security-policy"])
    nonce_html = re.search(r'<script type="importmap" nonce="([^"]+)"', respuesta.text)

    assert nonce_cabecera is not None and nonce_html is not None
    assert nonce_cabecera.group(1) == nonce_html.group(1)


def test_el_nonce_cambia_en_cada_pedido(sesion: TestClient) -> None:
    """Un nonce fijo es un `'unsafe-inline'` con pasos de mas."""
    primero = sesion.get("/cortante").headers["content-security-policy"]
    segundo = sesion.get("/cortante").headers["content-security-policy"]
    assert primero != segundo


# ── Host permitido ───────────────────────────────────────────────────────────


def test_el_host_ajeno_se_rechaza() -> None:
    """Un `Host` arbitrario se refleja en las URLs absolutas que arma `url_for`."""
    cliente = _app_de_borde(hosts_permitidos=("studiocutter.example",))

    assert cliente.get("/eco", headers={"Host": "studiocutter.example"}).status_code == 200
    assert cliente.get("/eco", headers={"Host": "phishing.example"}).status_code == 400


def test_sin_lista_de_hosts_entra_cualquiera() -> None:
    """El default tiene que seguir sirviendo en localhost sin configurar nada."""
    cliente = _app_de_borde()
    assert cliente.get("/eco", headers={"Host": "lo-que-sea.local"}).status_code == 200


# ── Login ────────────────────────────────────────────────────────────────────


def test_el_login_se_bloquea_tras_los_intentos_fallidos(cliente: TestClient) -> None:
    """El unico test que paga los argon2 de verdad. Por eso hay uno solo."""
    a = Ajustes()
    for intento in range(a.max_intentos_login - 1):
        r = cliente.post("/login", data={"usuario": USUARIO, "clave": f"mala-{intento}"})
        assert r.status_code == 401, "hasta el tope, la respuesta es la de siempre"

    ultimo = cliente.post("/login", data={"usuario": USUARIO, "clave": "mala-final"})
    assert ultimo.status_code == 429
    assert "intentos" in ultimo.text

    # Y con el bloqueo puesto, ni siquiera la contraseña correcta pasa: si
    # pasara, el atacante tendria intentos infinitos mientras acierte alguno.
    assert cliente.post("/login", data={"usuario": USUARIO, "clave": CLAVE}).status_code == 429


def test_el_freno_del_login_arranca_limpio(cliente: TestClient) -> None:
    """Dicho de otra forma: el reinicio entre tests existe y funciona."""
    assert FRENO_LOGIN.espera_restante(IP_LOCAL) == 0
    assert cliente.post("/login", data={"usuario": USUARIO, "clave": CLAVE}).status_code == 303


# ── Salud ────────────────────────────────────────────────────────────────────


def test_salud_no_pide_sesion_y_no_cuenta_nada(cliente: TestClient) -> None:
    respuesta = cliente.get("/salud")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"estado": "ok"}


# ── Credenciales por entorno ─────────────────────────────────────────────────


@pytest.fixture
def sin_archivo(tmp_path: Path) -> Ajustes:
    return Ajustes(archivo_credenciales=tmp_path / "no-existe.json")


def test_las_credenciales_pueden_venir_del_entorno(
    sin_archivo: Ajustes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Es lo que hace desplegable la imagen: el archivo esta gitignoreado."""
    monkeypatch.setenv(
        VARIABLE_CREDENCIALES, json.dumps({"usuarios": [{"usuario": "ana", "hash": "$argon2id$x"}]})
    )
    assert cargar_usuarios(sin_archivo) == {"ana": "$argon2id$x"}


def test_el_entorno_le_gana_al_archivo(ajustes: Ajustes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        VARIABLE_CREDENCIALES, json.dumps({"usuarios": [{"usuario": "ana", "hash": "$argon2id$x"}]})
    )
    assert USUARIO not in cargar_usuarios(ajustes)


def test_sin_archivo_y_sin_variable_el_error_nombra_las_dos_fuentes(
    sin_archivo: Ajustes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con dos origenes posibles, "falta el archivo" ya no alcanza como pista."""
    monkeypatch.delenv(VARIABLE_CREDENCIALES, raising=False)
    with pytest.raises(ErrorDeConfiguracion, match=VARIABLE_CREDENCIALES):
        cargar_usuarios(sin_archivo)


@pytest.fixture(autouse=True)
def _sin_credenciales_heredadas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ningun test corre con la variable puesta por el entorno de la maquina."""
    monkeypatch.delenv(VARIABLE_CREDENCIALES, raising=False)
    yield
