"""Login, autorizacion y sesion.

Lo que se verifica no es que el login "funcione": es que **no filtre** y que
**no se pueda esquivar**. Un login que anda pero le dice al atacante que el
usuario existe, o una ruta que se olvidaron de proteger, pasan cualquier
prueba funcional y fallan estas.
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
from pathlib import PurePosixPath

import pytest
from conftest import CLAVE, USUARIO
from fastapi.testclient import TestClient

PROTEGIDAS = ["/", "/conversor", "/lineas", "/cortante"]


def test_login_correcto_redirige_y_deja_cookie(cliente: TestClient) -> None:
    r = cliente.post("/login", data={"usuario": USUARIO, "clave": CLAVE})
    assert r.status_code == 303
    assert r.headers["location"] == "/conversor"
    assert "studiocutter_sesion" in r.headers.get("set-cookie", "")


def test_clave_incorrecta_no_deja_sesion(cliente: TestClient) -> None:
    r = cliente.post("/login", data={"usuario": USUARIO, "clave": "no-es-la-clave"})
    assert r.status_code == 401
    assert "studiocutter_sesion" not in cliente.cookies


def test_el_mensaje_no_distingue_usuario_inexistente_de_clave_mala(cliente: TestClient) -> None:
    """Si los mensajes difieren, el login es un enumerador de usuarios."""
    mala = cliente.post("/login", data={"usuario": USUARIO, "clave": "x" * 12})
    fantasma = cliente.post("/login", data={"usuario": "nadie", "clave": "x" * 12})
    assert mala.status_code == fantasma.status_code == 401
    assert "Usuario o contraseña incorrectos." in mala.text
    assert "Usuario o contraseña incorrectos." in fantasma.text


@pytest.mark.parametrize("ruta", PROTEGIDAS)
def test_pagina_protegida_sin_sesion_redirige_a_login(cliente: TestClient, ruta: str) -> None:
    r = cliente.get(ruta)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?destino=")


def test_api_protegida_sin_sesion_responde_401_json(cliente: TestClient) -> None:
    r = cliente.get("/api/trabajos/6f1b3e0c-0000-4000-8000-000000000000")
    assert r.status_code == 401
    assert r.json()["error"]["codigo"] == "no_autenticado"


@pytest.mark.parametrize("ruta", PROTEGIDAS)
def test_con_sesion_las_paginas_abren(sesion: TestClient, ruta: str) -> None:
    r = sesion.get(ruta, follow_redirects=True)
    assert r.status_code == 200


def test_logout_invalida_la_sesion(sesion: TestClient) -> None:
    assert sesion.get("/conversor").status_code == 200
    r = sesion.post("/logout")
    assert r.status_code == 303
    assert sesion.get("/conversor").status_code == 303


def test_no_se_puede_redirigir_afuera_despues_del_login(cliente: TestClient) -> None:
    """`?destino=` es un open redirect si no se filtra: se filtra."""
    for destino in ("//evil.example", "https://evil.example", "/\\evil"):
        r = cliente.post("/login", data={"usuario": USUARIO, "clave": CLAVE, "destino": destino})
        assert r.headers["location"] == "/conversor", destino


def test_el_destino_interno_si_se_respeta(cliente: TestClient) -> None:
    r = cliente.post("/login", data={"usuario": USUARIO, "clave": CLAVE, "destino": "/cortante"})
    assert r.headers["location"] == "/cortante"


def test_la_sesion_guarda_solo_el_usuario(sesion: TestClient) -> None:
    """La cookie viaja en cada pedido: no puede llevar nada mas que el usuario.

    No se testea "regeneracion de id" porque no hay id que regenerar: la sesion
    es una cookie firmada sin estado en el servidor. Lo que si se puede afirmar
    —y es lo que cierra la fijacion de sesion— es que despues del login la
    sesion contiene exactamente una clave y es la que puso el login.
    """
    crudo = sesion.cookies["studiocutter_sesion"]
    carga = crudo.split(".")[0]
    contenido = json.loads(base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)))
    assert list(contenido) == ["usuario"]
    assert contenido["usuario"] == USUARIO


def test_la_cookie_de_sesion_es_httponly_y_samesite(sesion: TestClient) -> None:
    """Sin HttpOnly, cualquier XSS se lleva la sesion puesta."""
    galleta = sesion.post("/login", data={"usuario": USUARIO, "clave": CLAVE}).headers["set-cookie"]
    assert "httponly" in galleta.lower()
    assert "samesite=lax" in galleta.lower()
    assert "max-age=604800" in galleta.lower()


def test_el_estatico_se_sirve_sin_internet(sesion: TestClient) -> None:
    """La app no puede depender de un CDN: three.js y las fuentes son locales."""
    for ruta in (
        "/static/vendor/three/three.module.js",
        "/static/vendor/fuentes/fuentes.css",
        "/static/css/estilo.css",
        "/static/js/app.js",
        "/static/js/preview3d.js",
    ):
        assert sesion.get(ruta).status_code == 200, ruta


_COMENTARIO_BLOQUE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMENTARIO_LINEA = re.compile(r"^\s*//.*$", re.MULTILINE)


_IMPORTA_DE = re.compile(r"""\b(?:import|export)\b[^()'"`;]*?\bfrom\s*(['"])([^'"]+)\1""")
"""Un `from '...'` de un import/export de verdad.

Lo que separa el import de la prosa es que entre la palabra clave y el `from`
no puede haber parentesis, comillas ni fin de sentencia. Eso descarta
`console.warn(\\`... colors from "srgb-linear" ...\\`)` de GLTFLoader **sin**
apoyarse en como este formateado el archivo — que es lo importante: el build
de three viene en una sola linea y ahi un patron anclado al margen no ve nada.
"""

_IMPORTA_SUELTO = re.compile(r"""\bimport\s*(['"])([^'"]+)\1""")
"""`import './x.js'` por efecto colateral, sin nombres."""


def _especificadores(codigo: str) -> set[str]:
    """Los modulos de los que depende este modulo.

    Los comentarios se sacan primero: cada addon de three documenta su propio
    import con `@three_import ... from 'three/addons/...'` adentro de un bloque
    JSDoc, y eso no es una dependencia real — es prosa.
    """
    limpio = _COMENTARIO_LINEA.sub("", _COMENTARIO_BLOQUE.sub("", codigo))
    return {m[1] for m in _IMPORTA_DE.findall(limpio)} | {
        m[1] for m in _IMPORTA_SUELTO.findall(limpio)
    }


def test_el_grafo_de_modulos_del_visor_cierra(sesion: TestClient) -> None:
    """Cada import de cada modulo servido tiene que resolver. Sin excepciones.

    Es el test que faltaba, y faltaba dos veces: el build de three importa
    `./three.core.js` y `GLTFLoader.js` importa dos utilidades de `../utils/`.
    Ninguno de los tres estaba vendorizado. Un import que 404ea **no tira
    ningun error visible**: el grafo entero deja de evaluarse, la vista previa
    nunca aparece y no hay nada ni en la pantalla ni en el log del servidor.

    Por eso se recorre el grafo y no se lista archivos a mano: una lista
    escrita a mano solo puede nombrar lo que uno ya sabe que existe, que es
    exactamente lo que este bug no era.
    """
    mapa = json.loads(
        re.search(
            r'<script type="importmap">\s*(\{.*?\})\s*</script>',
            sesion.get("/cortante").text,
            re.DOTALL,
        ).group(1)
    )["imports"]

    pendientes = ["/static/js/preview3d.js"]
    vistos: set[str] = set()
    while pendientes:
        url = pendientes.pop()
        if url in vistos:
            continue
        vistos.add(url)
        r = sesion.get(url)
        assert r.status_code == 200, f"{url} no se sirve (lo importa el grafo del visor)"
        for spec in _especificadores(r.text):
            if spec.startswith("/"):
                destino = spec
            elif spec.startswith("."):
                # `posixpath` y no `os.path`: son URLs, no rutas del disco.
                # En Windows `os.path.normpath` devuelve barras invertidas y el
                # servidor no encuentra nada.
                destino = posixpath.normpath(str(PurePosixPath(url).parent / spec))
            else:
                assert spec in mapa, f"{url} importa '{spec}' y el import map no lo declara"
                destino = mapa[spec]
            pendientes.append(destino)

    # Los tres que faltaban. Se nombran para que, si alguien vuelve a aplanar
    # el vendor, el test diga cual se perdio y no solo que el numero bajo.
    for imprescindible in ("three.core.js", "BufferGeometryUtils.js", "SkeletonUtils.js"):
        assert any(u.endswith(imprescindible) for u in vistos), (
            f"el grafo no llego a {imprescindible}; recorrido: {sorted(vistos)}"
        )


def test_el_css_define_los_dos_temas(sesion: TestClient) -> None:
    css = sesion.get("/static/css/estilo.css").text
    assert "--ground: #f1f8fc" in css, "falta el ground del modo claro"
    assert "--ground: #000000" in css, "falta el ground del modo oscuro"
    assert "#0e96c7" in css and "#4fc6ee" in css, "faltan los celestes de los dos modos"
    assert '[data-tema="oscuro"]' in css or "[data-tema='oscuro']" in css
    assert "prefers-color-scheme: dark" in css


def test_no_hay_ninguna_url_externa_en_lo_que_se_sirve(sesion: TestClient) -> None:
    """Cero pedidos a internet: es una herramienta que tiene que andar offline."""
    for ruta in ("/login", "/conversor", "/lineas", "/cortante"):
        cuerpo = sesion.get(ruta, follow_redirects=True).text
        for prohibido in ("//fonts.googleapis", "//cdn.", "//unpkg", "//cdnjs"):
            assert prohibido not in cuerpo, f"{ruta} apunta afuera ({prohibido})"
