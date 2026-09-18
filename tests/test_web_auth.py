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

from app import __version__
from app.archivos import SUFIJO_DESCARGA, ClaveArchivo
from app.config import Ajustes
from app.routers.cortante import COLORES, COLORES_FONDO
from app.routers.paginas import MODULOS

PROTEGIDAS = ["/", "/conversor", "/lineas", "/cortante", "/post"]

#: Las dos pantallas con visor 3D. Comparten `preview3d.js`, los ids del
#: bloque de vista previa y las paletas: los tests de contrato del front
#: valen para las dos o no valen para ninguna.
CON_VISOR = ["/cortante", "/post"]


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


@pytest.mark.parametrize("ruta", PROTEGIDAS)
def test_la_version_desplegada_se_ve_debajo_del_logo(sesion: TestClient, ruta: str) -> None:
    """La misma `__version__` que taggea `ci-release`, en todas las pantallas.

    Se busca el elemento y no solo el texto: `v0.2.0` suelto podria venir de
    cualquier lado (un comentario, three.js). Y una sola vez, porque la marca se
    dibuja una sola vez.
    """
    html = sesion.get(ruta, follow_redirects=True).text
    etiquetas = re.findall(r'<span class="marca__version"[^>]*>v([^<]+)</span>', html)
    assert etiquetas == [__version__]


def test_el_login_dice_que_version_corre(cliente: TestClient) -> None:
    """La version se ve tambien antes de entrar, debajo de la tarjeta del login.

    Decision del 2026-09-18: sirve para comprobar que version llego a produccion
    sin tener que entrar. Una sola vez y en el mismo pill que la barra; `/salud`
    sigue sin decirla.
    """
    html = cliente.get("/login").text
    etiquetas = re.findall(r'<span class="marca__version"[^>]*>v([^<]+)</span>', html)
    assert etiquetas == [__version__]


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


def test_la_cookie_de_sesion_es_httponly_y_samesite(sesion: TestClient, ajustes: Ajustes) -> None:
    """Sin HttpOnly, cualquier XSS se lleva la sesion puesta."""
    galleta = sesion.post("/login", data={"usuario": USUARIO, "clave": CLAVE}).headers["set-cookie"]
    assert "httponly" in galleta.lower()
    assert "samesite=lax" in galleta.lower()
    # Contra el ajuste y no contra un numero escrito aca: dos verdades sobre la
    # misma duracion se desincronizan, y la que manda es la que viaja en la
    # cookie. Cuanto vale ese ajuste lo fija el test de abajo.
    assert f"max-age={ajustes.duracion_sesion_s}" in galleta.lower()


def test_la_sesion_dura_ocho_horas(ajustes: Ajustes) -> None:
    """Es una decision de producto, no un default que se pueda mover sin querer.

    ⚠ Y es un tope de **inactividad**: `SessionMiddleware` reescribe la cookie
    en toda respuesta con sesion, asi que el reloj arranca de nuevo con cada
    click. Una jornada de trabajo entera no vuelve a pedir login; una pestaña
    que quedo abierta de ayer, si.
    """
    assert ajustes.duracion_sesion_s == 8 * 60 * 60


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


def _sin_comentarios(codigo: str) -> str:
    """El JS sin prosa.

    Hace falta cada vez que un test afirma sobre el CODIGO: este repo comenta
    denso y explica en los comentarios lo que saco, asi que un `x in js` pelado
    encuentra el nombre en la explicacion de por que ya no esta.
    """
    return _COMENTARIO_LINEA.sub("", _COMENTARIO_BLOQUE.sub("", codigo))


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


@pytest.mark.parametrize("pagina", CON_VISOR)
def test_el_grafo_de_modulos_del_visor_cierra(sesion: TestClient, pagina: str) -> None:
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
    # El `[^>]*` cubre el `nonce` que le pone el CSP: el import map es el unico
    # script inline del sitio, y sin nonce el navegador no lo evalua.
    mapa = json.loads(
        re.search(
            r'<script type="importmap"[^>]*>\s*(\{.*?\})\s*</script>',
            sesion.get(pagina).text,
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


_MUESTRA = re.compile(r'<button[^>]*\bclass="paleta__color"[^>]*>', re.DOTALL)


def _atributo(etiqueta: str, nombre: str) -> str:
    hallado = re.search(rf'\b{nombre}="([^"]*)"', etiqueta)
    return hallado.group(1) if hallado else ""


def _paleta(html: str, ident: str) -> list[str]:
    """Las muestras de UNA paleta, por el id de su contenedor.

    Se scopea por contenedor en vez de contar todas las muestras de la pagina:
    la pantalla dibuja TRES paletas —la que flota sobre el visor 3D, y la de la
    pieza y la del fondo de la vista imagen— y lo que hay que exigir es que
    cada una este completa, no que la suma de las tres de un numero.
    """
    bloque = re.search(rf'<div[^>]*\bid="{ident}"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert bloque is not None, f"falta la paleta #{ident}"
    return _MUESTRA.findall(bloque.group(1))


# id del contenedor -> nombre del color que tiene que arrancar marcado.
#
# Los tres arrancan en Blanco, y el fondo cambio en este ciclo (venia en Gris).
# Antes tenerlos iguales dejaba la foto de un solo tono, porque el estudio de
# luz de la vista imagen apenas proyectaba sombra sobre el fondo. Con la luz
# del visor la pieza tiene volumen y sombra propia, asi que se lee sobre
# blanco — y blanco es lo que se pidio para el fondo de la foto.
# La del fondo es la unica que NO dibuja `COLORES`: le toca `COLORES_FONDO`,
# que son los mismos ocho mas `Sin fondo`.
PALETAS = {
    "paleta": (COLORES, "Blanco"),
    "paleta-pieza": (COLORES, "Blanco"),
    "paleta-fondo": (COLORES_FONDO, "Blanco"),
}


@pytest.mark.parametrize("pagina", CON_VISOR)
def test_las_tres_paletas_dibujan_su_lista_completa(sesion: TestClient, pagina: str) -> None:
    """Cada paleta es exactamente la lista que le toca, en orden y completa.

    Vale para las dos pantallas con visor: post importa `COLORES` del router de
    cortante, no una copia. Si alguien duplicara la lista, este test lo ve.
    """
    html = sesion.get(pagina).text
    assert len(COLORES) == 8
    assert len(COLORES_FONDO) == 9, "el fondo suma `Sin fondo` a los ocho colores"

    nombres = [c.nombre for c in COLORES]
    assert nombres[0] == "Blanco", "el default de la pieza es el primero de la lista"
    assert set(nombres) == {
        "Blanco",
        "Gris",
        "Rojo",
        "Amarillo",
        "Azul",
        "Verde",
        "Rosa",
        "Violeta",
    }

    for ident, (lista, inicial) in PALETAS.items():
        muestras = _paleta(html, ident)
        assert len(muestras) == len(lista), f"#{ident} no dibuja las {len(lista)} muestras"

        for etiqueta, color in zip(muestras, lista, strict=True):
            assert _atributo(etiqueta, "data-color") == color.hex, ident
            assert _atributo(etiqueta, "aria-label") == color.nombre, ident
            assert re.fullmatch(r"#[0-9a-f]{6}", color.hex), color

        marcadas = [e for e in muestras if _atributo(e, "aria-pressed") == "true"]
        assert len(marcadas) == 1, f"#{ident} tiene que arrancar con una sola muestra elegida"
        assert _atributo(marcadas[0], "aria-label") == inicial, ident

    # El fondo de la foto arranca en blanco. Es un pedido explicito, no una
    # consecuencia: se afirma para que nadie lo "arregle" volviendo al gris.
    assert PALETAS["paleta-fondo"][1] == COLORES[0].nombre


@pytest.mark.parametrize("pagina", CON_VISOR)
def test_sin_fondo_es_solo_del_fondo_y_apaga_el_piso(sesion: TestClient, pagina: str) -> None:
    """`Sin fondo`: ultima muestra del fondo, blanco puro y con `data-sin-piso`.

    Las tres puntas tienen que coincidir o la opcion existe a medias: el router
    la declara con `piso=False`, el macro la marca con `data-sin-piso` y el JS
    decide por ese atributo. Si una se mueve sola, la muestra se dibuja igual y
    el fondo sigue con su sombra proyectada — sin un solo error a la vista.
    """
    sin_fondo = COLORES_FONDO[-1]
    assert sin_fondo.nombre == "Sin fondo"
    assert sin_fondo.hex == "#ffffff", "el pedido es blanco TOTAL, no el casi blanco de la pieza"
    assert sin_fondo.piso is False
    assert all(c.piso for c in COLORES), "ningun color de PIEZA apaga el piso"
    assert sin_fondo not in COLORES, (
        "`Sin fondo` no puede ser color de pieza: blanco puro se quema con ACES "
        "y deja el cortante sin relieve"
    )

    html = sesion.get(pagina).text
    for ident, (lista, _) in PALETAS.items():
        muestras = _paleta(html, ident)
        marcadas = [e for e in muestras if _atributo(e, "data-sin-piso") == "1"]
        assert len(marcadas) == len([c for c in lista if not c.piso]), (
            f"#{ident}: las muestras con `data-sin-piso` no son las que declara el router"
        )

    # El front decide por el atributo y no por el hex. Con el color escrito en
    # el JS habria una segunda lista, y cambiarlo en el router dejaria de
    # apagar el piso — la muestra se veria igual y la foto saldria con sombra.
    js = sesion.get("/static/js/app.js").text + sesion.get("/static/js/preview3d.js").text
    assert sin_fondo.hex not in js, "el JS escribe el hex de `Sin fondo` en vez de leer el atributo"
    assert "sinPiso" in js, "ningun script mira `data-sin-piso`"


def test_el_default_de_las_paletas_lo_marca_solo_el_template(sesion: TestClient) -> None:
    """La muestra inicial se declara en UN lugar: el `activo` del macro.

    Este test existe por un bug de este mismo ciclo. `app.js` tenia un segundo
    default (`indiceDefecto`) para que el fondo arrancara en la segunda
    muestra, y al mover el del template y no el del JS el HTML servido marcaba
    blanco mientras la pantalla mostraba gris: `iniciarPaleta` reescribe
    `aria-pressed` en todas las muestras al arrancar, asi que el JS gana
    siempre.

    **No lo vio ningun test**, y no podia verlo: los de paleta validan el HTML
    con regex y en este proyecto ninguna prueba ejecuta JS. Lo unico que si se
    puede afirmar desde la suite es que no exista la segunda fuente de verdad.
    """
    js = sesion.get("/static/js/app.js").text
    codigo = _COMENTARIO_LINEA.sub("", _COMENTARIO_BLOQUE.sub("", js))
    assert "disponibles[0]" in codigo, "el JS tiene que aplicar la primera muestra"
    assert "indiceDefecto" not in codigo, (
        "volvio el segundo default de las paletas; el unico es el `activo` del macro"
    )


def test_ningun_color_de_la_paleta_esta_escrito_dos_veces(sesion: TestClient) -> None:
    """El front recibe los codigos, no los guarda.

    El CSS los toma de `--muestra` y el visor los lee del boton marcado. Si
    alguno aparece escrito en el JS o en el CSS hay dos listas, y la que se ve
    en pantalla va a dejar de coincidir con la que pinta la pieza.
    """
    servido = "".join(
        sesion.get(ruta).text
        for ruta in ("/static/js/app.js", "/static/js/preview3d.js", "/static/css/estilo.css")
    )
    for color in COLORES:
        assert color.hex not in servido, (
            f"{color.hex} ({color.nombre}) tambien esta escrito en el front; "
            "la paleta tiene un solo dueño: COLORES en app/routers/cortante.py"
        )


def test_el_boton_de_generar_nace_apagado_y_con_su_pista(sesion: TestClient) -> None:
    """Los ids que `app.js` prende y apaga tienen que existir en la pantalla.

    Si uno se renombra de un solo lado no falla nada visible: el boton queda
    trabado despues de la primera pieza y no hay forma de volver a generar.
    """
    html = sesion.get("/cortante").text
    boton = re.search(r'<button[^>]*id="generar"[^>]*>', html)
    assert boton is not None and "disabled" in boton.group(0)
    # Se busca la etiqueta por su id y despues se mira adentro, en vez de exigir
    # `class` antes que `id`: la version anterior se ponia en rojo con solo
    # reordenar dos atributos, que no cambia nada de lo que el test verifica.
    pista = re.search(r'<[^>]*id="pista-generar"[^>]*>', html)
    assert pista is not None and "oculto" in pista.group(0)

    js = sesion.get("/static/js/app.js").text
    assert "#pista-generar" in js
    assert ".paleta__color" in js
    for ident in PALETAS:
        assert f"'#{ident}'" in js, f"app.js no maneja la paleta #{ident}"


def test_el_menu_de_modulos_se_dibuja_una_sola_vez(sesion: TestClient) -> None:
    """CA-09 — un solo menu, servido desde Python.

    Hasta el ciclo anterior la lista de modulos estaba escrita DOS veces en
    `base.html` —el sidebar y la barra inferior de mobile— con etiquetas
    distintas en cada copia, y los dos `<nav>` tenian el mismo nombre accesible.
    Agregar un modulo eran dos ediciones y nadie se acordaba de la segunda.
    """
    for pagina in ("conversor", "lineas", "cortante", "post"):
        html = sesion.get(f"/{pagina}").text

        etiquetas = re.findall(r"<nav[^>]*aria-label=\"([^\"]*)\"", html)
        assert len(etiquetas) == len(set(etiquetas)), f"/{pagina}: dos <nav> con el mismo nombre"

        enlaces = re.findall(r'class="nav__item"[^>]*href="([^"]*)"', html)
        assert enlaces == [m.ruta for m in MODULOS], f"/{pagina}: {enlaces}"

        actual = re.findall(r'nav__item[^>]*aria-current="page"', html)
        assert len(actual) == 1, f"/{pagina}: {len(actual)} items marcados como actuales"

    assert "barra-inferior" not in sesion.get("/cortante").text


def test_cada_modulo_tiene_nombre_accesible_propio(sesion: TestClient) -> None:
    """CA-09 — a <=860 px el rotulo se oculta y solo queda el icono.

    Sin `aria-label` en el enlace, ocultar el `<span>` con CSS deja tres
    enlaces sin nombre accesible: para un lector de pantalla la navegacion
    desaparece justo en el tamaño donde mas se usa.
    """
    html = sesion.get("/conversor").text
    for mod in MODULOS:
        assert f'aria-label="{mod.etiqueta}"' in html, mod.etiqueta


def test_el_css_ya_no_conoce_el_ancho_del_sidebar(sesion: TestClient) -> None:
    """CA-10 — la columna lateral se fue, su token tambien.

    `--sidebar-ancho` era el unico acople al layout viejo: si sobrevive, quedo
    una regla calculando un corrimiento lateral que ya no existe.
    """
    css = sesion.get("/static/css/estilo.css").text
    assert "--sidebar-ancho" not in css
    assert "--barra-alto" in css and "--ancho-contenido" in css
    # `.barra` a secas es la barra de PROGRESO de las filas del conversor. Si
    # el encabezado volviera a llamarse asi, la pisaria y quedaria de 5 px.
    assert ".barra-superior {" in css


def test_el_estado_de_pantalla_vive_en_sessionstorage(sesion: TestClient) -> None:
    """CA-11 — contrato del JS, hasta donde se puede verificar sin navegador.

    No se ejecuta JS en la suite, asi que esto comprueba que las piezas estan y
    no que funcionen: el comportamiento de verdad son los pasos manuales.
    """
    js = sesion.get("/static/js/app.js").text
    assert "sessionStorage" in js
    assert "sc3d:" in js, "falta el prefijo de las claves de estado"
    assert "getEntriesByType('navigation')" in js, "sin esto el F5 no limpia"
    assert "#quitar" in js, "falta el boton de quitar el archivo"
    assert "nombreDeTrabajo" in js, "falta el nombre real en el encadenado"


def test_el_css_define_los_dos_temas(sesion: TestClient) -> None:
    css = sesion.get("/static/css/estilo.css").text
    assert "--ground: #f1f8fc" in css, "falta el ground del modo claro"
    assert "--ground: #000000" in css, "falta el ground del modo oscuro"
    assert "#0e96c7" in css and "#4fc6ee" in css, "faltan los celestes de los dos modos"
    assert '[data-tema="oscuro"]' in css or "[data-tema='oscuro']" in css
    assert "prefers-color-scheme: dark" in css


def test_la_vista_previa_ofrece_las_dos_opciones(sesion: TestClient) -> None:
    """El panel se titula "VISTA PREVIA" y abajo elige entre 3D e imagen.

    El titulo ya no puede nombrar a una de las dos vistas: "VISTA PREVIA 3D"
    con la vista imagen abierta seria un rotulo que miente.
    """
    html = sesion.get("/cortante").text
    assert "VISTA PREVIA" in html
    assert "VISTA PREVIA 3D" not in html, "el rotulo no puede nombrar una sola vista"

    botones = re.findall(r"<button[^>]*\bdata-vista=\"([^\"]*)\"[^>]*>", html, re.DOTALL)
    assert botones == ["3d", "imagen"], "las dos opciones, y 3D primero"

    marcados = re.findall(
        r"<button[^>]*\bdata-vista=\"([^\"]*)\"[^>]*\baria-pressed=\"true\"", html, re.DOTALL
    )
    assert marcados == ["3d"], "3D es el default y no cambia"

    # El visor 3D arranca visible y la imagen escondida. Al revés, la pantalla
    # abriria en una vista que el usuario no eligio.
    visor = re.search(r'<div[^>]*\bid="visor"[^>]*>', html)
    imagen = re.search(r'<div[^>]*\bid="vista-imagen"[^>]*>', html)
    assert visor is not None and "oculto" not in visor.group(0)
    assert imagen is not None and "oculto" in imagen.group(0)


# Lo que la vista imagen necesita tener en la pantalla. Si uno de estos ids se
# renombra de un solo lado no falla nada visible: el boton deja de prenderse o
# la paleta del fondo deja de mover el color, sin un solo error en la consola.
IDS_VISTA_IMAGEN = (
    "lienzo",
    "vista-imagen",
    "paleta-pieza",
    "paleta-fondo",
    "pista-imagen",
)

#: `#bajar-todo` es el ZIP, y el ZIP es solo de cortante: post produce un
#: unico archivo y un zip de uno confunde. El resto de los ids SI valen para
#: las dos pantallas — `preview3d.js` los resuelve por nombre a nivel de
#: modulo y no tiene namespace por pantalla.
IDS_SOLO_CORTANTE = ("bajar-todo",)


@pytest.mark.parametrize("pagina", CON_VISOR)
def test_los_ids_de_la_vista_imagen_existen_en_las_dos_puntas(
    sesion: TestClient, pagina: str
) -> None:
    html = sesion.get(pagina).text
    js = _sin_comentarios(
        sesion.get("/static/js/app.js").text + sesion.get("/static/js/preview3d.js").text
    )
    esperados = IDS_VISTA_IMAGEN + (IDS_SOLO_CORTANTE if pagina == "/cortante" else ())
    for ident in esperados:
        assert f'id="{ident}"' in html, f"{pagina} no dibuja #{ident}"
        # Con `#` o con `getElementById`, no el nombre suelto: que la palabra
        # "lienzo" aparezca en el archivo no prueba que alguien lo busque.
        assert f"'#{ident}'" in js or f"getElementById('{ident}')" in js, (
            f"ningun script busca #{ident}"
        )


#: El grupo de descargas, en orden. La foto va con los demas archivos y el ZIP
#: cierra: es lo que se pidio, "la imagen en el mismo grupo que los .stl".
CLAVES_DESCARGA = (
    "3mf",
    "3mf_cortador",
    "3mf_marcador",
    "stl_cortador",
    "stl_marcador",
    "jpg_vista",
)


def test_el_grupo_de_descargas_incluye_la_imagen_y_el_zip(sesion: TestClient) -> None:
    """La foto es un archivo mas del grupo, y ya no tiene boton propio.

    El boton viejo (`#bajar-imagen`, al lado del visor) bajaba del canvas. Se
    saco a proposito: dos lugares para bajar lo mismo, con dos nombres de
    archivo distintos, era justamente el problema.
    """
    html = sesion.get("/cortante").text
    grupo = re.search(r'<div[^>]*id="botones-descarga"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert grupo is not None, "no existe el grupo de descargas"
    cuerpo = grupo.group(1)

    assert re.findall(r'data-clave="([^"]+)"', cuerpo) == list(CLAVES_DESCARGA)
    assert re.search(r'<button[^>]*id="bajar-todo"', cuerpo), "el ZIP no esta en el grupo"
    assert 'id="bajar-imagen"' not in html, "quedo el boton viejo de bajar la imagen"

    # Las TRES puntas de la clave de la foto atadas entre si: el enum del
    # servidor, el `data-clave` del template y el literal de `app.js`. Sin esto
    # un typo o un rename en el JS rompia la entrada de la foto con toda la
    # suite en verde, porque cada lado se comparaba contra su propia constante.
    assert ClaveArchivo.JPG_VISTA.value in CLAVES_DESCARGA
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert f"CLAVE_FOTO = '{ClaveArchivo.JPG_VISTA.value}'" in js


def test_el_handshake_distingue_carga_de_exportacion(sesion: TestClient) -> None:
    """`cortante:imagen` lleva motivo, y los dos lados lo miran.

    El mismo evento avisa dos cosas distintas: que el modelo cargo (`carga`) y
    que una exportacion termino (`exportar`). Sin distinguirlas, un `.glb` que
    terminara de cargar entre el clic y la respuesta del PUT resolvia la
    promesa de `pedirFoto` antes de tiempo — la descarga arrancaba con la
    subida en vuelo y el ZIP se llevaba la foto vieja, que es exactamente lo
    que el `await` existe para impedir. En el otro sentido: un fallo
    transitorio de exportacion no puede esconder la entrada del JPG.
    """
    visor = _sin_comentarios(sesion.get("/static/js/preview3d.js").text)
    app = _sin_comentarios(sesion.get("/static/js/app.js").text)

    assert "motivo = 'carga'" in visor, "el motivo por defecto es el aviso de carga"
    assert "'exportar'" in visor, "las respuestas a una exportacion tienen que marcarse"
    # Quien espera una exportacion acepta solo su respuesta...
    assert "motivo === 'exportar'" in app
    # ...y quien decide si la entrada se ofrece mira solo la carga.
    assert "motivo !== 'carga'" in app


def test_el_tres_mf_completo_sigue_siendo_la_accion_primaria(sesion: TestClient) -> None:
    """Agregar el ZIP no le saco el primer plano al archivo que mas se busca."""
    html = sesion.get("/cortante").text
    enlace = re.search(r'<a[^>]*data-clave="3mf"[^>]*>', html)
    assert enlace is not None
    assert "boton--secundario" not in enlace.group(0)


def test_los_eventos_entre_la_pantalla_y_el_visor_cierran(sesion: TestClient) -> None:
    """Los tres avisos tienen emisor en `app.js` y oyente en `preview3d.js`.

    Es el contrato entre los dos scripts —no hay bundler ni imports entre
    ellos, se hablan por eventos del documento—, asi que un nombre cambiado de
    un solo lado deja la mitad del pipeline muda y sin ningun error.
    """
    app = _sin_comentarios(sesion.get("/static/js/app.js").text)
    visor = _sin_comentarios(sesion.get("/static/js/preview3d.js").text)

    # ⚠ Sobre el CODIGO y verificando la DIRECCION, no `evento in archivo`.
    # Asi estaba escrito y no servia para lo que decia custodiar: los cinco
    # nombres aparecen tambien en los comentarios que explican el handshake,
    # asi que borrar el `dispatchEvent` y el `addEventListener` y dejar la
    # prosa lo dejaba verde — justo el caso que cuelga la pantalla.
    for evento, emisor, oyente in (
        ("cortante:color", app, visor),
        ("cortante:fondo", app, visor),
        ("cortante:vista", app, visor),
        ("cortante:exportar", app, visor),
        # El unico que viaja al reves: lo emite el visor y lo espera la pantalla.
        ("cortante:imagen", visor, app),
    ):
        # Del lado del emisor alcanza con el literal —`iniciarPaleta` recibe el
        # nombre como dato y despacha con la variable, asi que exigir
        # `CustomEvent('...'` seria exigir una forma de escribirlo—, pero del
        # lado del oyente el `addEventListener` va literal siempre. Lo que este
        # test agrega respecto de como estaba es la DIRECCION: antes pedia el
        # nombre en los dos archivos y un handshake invertido pasaba igual.
        assert f"'{evento}'" in emisor, f"nadie emite {evento}"
        assert f"addEventListener('{evento}'" in oyente, f"nadie escucha {evento}"
        assert f"addEventListener('{evento}'" not in emisor, (
            f"{evento} se escucha del lado que tiene que emitirlo"
        )


def test_la_respuesta_del_handshake_llega_aunque_no_haya_visor(sesion: TestClient) -> None:
    """Sin WebGL tambien hay que contestar, y por eso el oyente no vive adentro.

    Si el listener de `cortante:exportar` se registrara dentro de
    `iniciarImagen`, un navegador sin WebGL —donde esa funcion tira y el `try`
    del arranque se lo come— dejaria el evento sin nadie escuchando y el clic
    en "Descargar todo" se colgaria sin decir nada. La alternativa del otro
    lado seria un timeout, que es adivinar.
    """
    js = sesion.get("/static/js/preview3d.js").text

    # La propiedad es "a nivel de modulo", o sea SIN indentacion — no "mas
    # arriba que otra funcion". Comparar offsets dejaba pasar que el oyente se
    # mudara adentro de `iniciar()`, que esta antes en el archivo y tambien
    # puede tirar sin WebGL: el test quedaba verde y la garantia que nombra,
    # destruida.
    assert re.search(r"^document\.addEventListener\('cortante:exportar'", js, re.MULTILINE), (
        "el oyente de `cortante:exportar` tiene que estar a nivel de modulo, sin indentar"
    )
    assert "avisarImagen(false, 'este navegador no puede generar la imagen', 'exportar')" in js

    # Y del otro lado, la red que hace que el ZIP no dependa del visor: los
    # archivos ya estan en el servidor, asi que una vista que no contesta no
    # puede dejar la descarga colgada.
    app = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert "MS_ESPERA_FOTO" in app, "falta el tope de espera del handshake"


def test_la_imagen_se_exporta_en_jpg_y_con_tope_de_tamano(sesion: TestClient) -> None:
    """El pedido era JPG descargable de hasta 2 MB: las piezas tienen que estar.

    No se ejecuta JS en la suite, asi que esto comprueba que el mecanismo
    existe y no que el archivo salga pesando lo que tiene que pesar — eso es un
    paso manual.
    """
    js = sesion.get("/static/js/preview3d.js").text
    assert "image/jpeg" in js, "la descarga tiene que ser JPG"
    assert "2 * 1024 * 1024" in js, "falta el tope de 2 MB"
    assert "CALIDADES_JPG" in js, "sin escalera de calidad el tope no se puede respetar"
    assert "LADOS_JPG = [2048" in js, "el lado de exportacion sigue siendo 2048 px"
    # `toBlob` es asincronico: sin esto el buffer ya se limpio cuando el
    # navegador lo va a leer y el JPG sale en negro.
    assert "preserveDrawingBuffer" in js
    # El `-vista.jpg` dejo de armarse en JS: ahora la foto se sube al trabajo y
    # el nombre lo pone el servidor, con el mismo criterio que el `.3mf`.
    assert SUFIJO_DESCARGA[ClaveArchivo.JPG_VISTA] == "-vista.jpg"
    # ⚠ La URL ya NO la arma el visor: desde F4 hay dos destinos posibles
    # —`/imagen` para un cortante y `/diseno/<n>/imagen` para cada diseño de un
    # post— y elegir entre ellos desde `preview3d.js` seria meterle al visor una
    # idea de que pantalla lo llamo. El visor recibe el destino y hace el PUT;
    # las URLs las arma `app.js`. Se verifican las dos mitades, porque cada una
    # sola pasaria con la otra rota.
    assert re.search(r"fetch\(destino,\s*\{\s*method:\s*'PUT'", _sin_comentarios(js)), (
        "el visor tiene que subir con PUT al destino que le pasan"
    )
    app = _sin_comentarios(sesion.get("/static/js/app.js").text)
    for destino in (
        r"/api/trabajos/\$\{encodeURIComponent\(id\)\}/imagen",
        r"/api/trabajos/\$\{encodeURIComponent\(trabajoId\)\}/diseno/\$\{n\}/imagen",
    ):
        assert re.search(destino, app), f"la pantalla no arma el destino {destino}"


def test_la_imagen_es_cenital_con_fondo_liso(sesion: TestClient) -> None:
    """Lo que NO cambio del rediseño: sigue siendo una toma a plomo sobre liso."""
    js = sesion.get("/static/js/preview3d.js").text
    # Cenital: una camara que mira a plomo no tiene vertical propia. Sin este
    # `up` explicito entra en gimbal lock y la pieza sale de costado.
    assert "camara.up.set(0, 0, -1)" in js
    # Fondo liso: va como `scene.background`, que es un clearColor y no lo toca
    # ni el tone mapping ni ninguna luz. El hex del JPG es el de la muestra.
    assert "escena.background" in js
    # Sombra sobre el fondo sin degradarlo: `ShadowMaterial` es transparente
    # salvo donde cae la sombra.
    assert "ShadowMaterial" in js
    # Sombra EN el cortante: las paredes del filo sombreandose entre si.
    assert "o.receiveShadow = true" in js and "o.castShadow = true" in js


def _reparto(codigo: str, nombre: str) -> dict[str, float]:
    """Un reparto de paneles del estudio, leido del JS como numeros.

    Se parsea en vez de afirmar sobre el texto porque lo que hay que fijar es
    una RELACION entre dos repartos ("el de la foto tiene menos cenital que el
    del visor"), no un valor. Si manana hay que retocar el numero, el test
    tiene que seguir cuidando la garantia en lugar de pedir que lo actualicen.
    """
    bloque = re.search(rf"const {nombre} = \{{(.*?)\}};", codigo, re.DOTALL)
    assert bloque, f"no existe el reparto {nombre}"
    return {
        clave: float(int(valor, 16)) if valor.startswith("0x") else float(valor)
        for clave, valor in re.findall(r"(\w+):\s*(0x[0-9a-fA-F]+|[\d.]+)", bloque.group(1))
    }


def test_la_foto_comparte_la_maquinaria_del_visor(sesion: TestClient) -> None:
    """Las dos vistas salen de las mismas funciones, no de dos rigs paralelos.

    Se afirma sobre los MECANISMOS y no sobre el resultado, porque en este
    proyecto ningun test ejecuta JS: que las dos vistas se armen con las mismas
    funciones, la misma exposicion y el mismo tipo de sombra es lo mas cerca
    que la suite puede estar de "tienen el mismo aspecto". Lo que la foto
    realmente muestra es CV-01, manual.

    Lo que SI cambia entre las dos —cuanta luz sin direccion hay— lo cuida
    `test_la_luz_de_la_foto_es_neutra_y_no_toca_la_sombra`. Este test dice que
    esa es la unica diferencia.
    """
    js = sesion.get("/static/js/preview3d.js").text
    codigo = _sin_comentarios(js)

    # Una sola caja de estudio y un solo rig de luces para las dos vistas: la
    # foto no arma los suyos, le pasa su reparto a la misma funcion.
    assert codigo.count("crearEntorno(renderer)") == 1, "el visor va con el reparto por default"
    assert codigo.count("crearEntorno(renderer, ESTUDIO_FOTO)") == 1, "la foto pasa el suyo"
    # Con el `;` para contar las LLAMADAS y no la definicion, que tambien dice
    # `agregarLuces(escena)`.
    assert codigo.count("agregarLuces(escena);") == 2, "las dos vistas comparten el rig de luces"

    # La exposicion y el tipo de sombra salen de un solo lugar cada uno.
    #
    # Se afirma la INVARIANTE ("no hay una segunda fuente") y no el numero de
    # copias: contar `== 2` fijaba la duplicacion, asi que extraer las lineas
    # compartidas a un helper —que es justo lo que el codigo pide— ponia el test
    # en rojo mientras la garantia se volvia mas fuerte. Un test no puede
    # castigar el refactor que el ciclo quiere.
    assert re.search(r"EXPOSICION_VISOR = 1\.05", js), "la exposicion del visor es 1,05"
    assert not re.search(r"toneMappingExposure\s*=\s*[0-9]", js), (
        "la exposicion no puede escribirse como numero: la unica fuente es la constante"
    )
    assert "toneMappingExposure = EXPOSICION_VISOR" in js
    assert re.search(r"const TIPO_SOMBRA = THREE\.\w+ShadowMap", js), (
        "el tipo de mapa de sombra tiene que salir de una sola constante"
    )
    assert not re.search(r"shadowMap\.type\s*=\s*THREE\.", js), (
        "el tipo de sombra no puede escribirse directo: la unica fuente es TIPO_SOMBRA"
    )
    assert codigo.count("shadowMap.type = TIPO_SOMBRA") == 2, (
        "las dos vistas tienen que tomar el mismo tipo de sombra"
    )
    assert codigo.count("new THREE.ShadowMaterial(") == 1, "un solo material de sombra"

    # Y la direccion de la luz principal es literalmente la misma constante.
    assert "DIR_PRINCIPAL" in js
    assert "principal.position.set(...DIR_PRINCIPAL)" in js

    # El rig de foto VIEJO —luz clave propia, dos sombras, exposicion mas baja—
    # no puede volver. Sobre el CODIGO y no sobre la prosa: los comentarios
    # nombran lo que se saco justamente para contar por que.
    for retirado in ("const FOTO", "ajustarSombra", "aporteClave", "aporteCenital"):
        assert retirado not in codigo, f"volvio el rig de foto viejo: {retirado}"


def test_la_sombra_se_difumina_donde_termina(sesion: TestClient) -> None:
    """El pedido: el tono igual, el borde difuso. Son dos cosas distintas.

    Aclarar la mancha y difuminarle el borde se parecen en una miniatura y no
    son lo mismo: una sombra mas clara sigue terminando de golpe, y una sombra
    difuminada sigue pesando lo mismo donde apoya la pieza. Este test cuida las
    dos mitades — que el borde sea un parametro, y que la densidad no se haya
    movido para conseguirlo.

    ⚠ **El ancho de la penumbra solo puede ser un parametro con VSM.** Con
    filtrado PCF, `shadow.radius` lo ignora el sombreador de three: la penumbra
    sale del tamaño del texel, o sea de la resolucion del mapa contra el
    frustum. Volver a PCF no rompe nada visible al abrir la pantalla — deja de
    difuminar y ya— asi que el tipo se afirma aca.

    Como se ve es CV, manual. Esto dice que las decisiones que lo producen
    siguen tomadas.
    """
    codigo = _sin_comentarios(sesion.get("/static/js/preview3d.js").text)

    assert "const TIPO_SOMBRA = THREE.VSMShadowMap" in codigo, (
        "sin VSM el ancho de la penumbra vuelve a depender de la resolucion"
    )
    assert re.search(r"PENUMBRA_REL = 0\.\d+", codigo), "no hay ancho de penumbra declarado"
    assert "blurSamples = MUESTRAS_PENUMBRA" in codigo, "el desenfoque no declara sus muestras"

    # Se traduce a texels en CADA vista: las dos reparten 2048 sobre frustums
    # distintos —la foto se lo ajusta a la pieza, el visor lo tiene fijo— asi
    # que el mismo radio en texels daria dos penumbras distintas, que es como
    # las dos vistas dejan de verse iguales.
    cuerpo = codigo.split("function ajustarPenumbra(", 1)[1].split("}", 1)[0]
    assert "c.right - c.left" in cuerpo, "la penumbra no se mide contra el frustum de cada vista"
    assert "PENUMBRA_REL * radio" in cuerpo, "la penumbra no es proporcional a la pieza"
    assert codigo.count("ajustarPenumbra(") == 3, (
        "la definicion y las dos vistas: alguna dejo de ajustar la penumbra"
    )

    # El mapa no se recalcula solo —la luz no se mueve y la pieza tampoco— y
    # eso es lo que hace pagable el desenfoque en una vista que anima. Pero si
    # nadie pide el re-render, la sombra queda congelada en la de la pieza
    # anterior **sin un solo error a la vista**.
    assert "autoUpdate = false" in codigo, "el mapa de sombra se re-rinde en cada frame"
    assert "needsUpdate = true" in cuerpo, (
        "se ajusta la penumbra y no se pide el re-render: el mapa queda viejo"
    )

    # ⚠ Y el TONO no se toca. Difuminar es mover el borde, no aclarar la mancha:
    # si alguien "suaviza" bajando la alfa, esto lo frena.
    assert "new THREE.ShadowMaterial({ opacity: 0.14 })" in codigo, (
        "cambio la densidad de la sombra"
    )
    assert "oscuro ? 0.24 : 0.14" in codigo, "cambio la densidad de la sombra en tema oscuro"


def test_la_luz_de_la_foto_es_neutra_y_no_toca_la_sombra(sesion: TestClient) -> None:
    """El pedido: sin brillo lavando el grabado, luz neutra, la sombra igual.

    Son tres garantias distintas y se afirman por separado, siempre en RELACION
    contra el visor y no contra numeros sueltos: si manana hay que retocar un
    valor, el test tiene que seguir cuidando la garantia.

    Ninguna de estas afirmaciones puede decir como se ve la foto — eso es CV-01
    y se mira con los ojos. Lo que si dicen es que las tres decisiones que
    hacen que se vea asi siguen tomadas.
    """
    js = sesion.get("/static/js/preview3d.js").text
    codigo = _sin_comentarios(js)

    orbita = _reparto(codigo, "ESTUDIO_ORBITA")
    foto = _reparto(codigo, "ESTUDIO_FOTO")
    ambiente = _reparto(codigo, "AMBIENTE_FOTO")

    # 1. Sin lavado. El panel cenital cae por igual sobre el plato y sobre el
    #    fondo del surco —ahi adentro no hay oclusion ambiental que lo tape—,
    #    asi que en una toma a plomo es lo que borra el grabado. Tiene que
    #    pesar bastante menos que en el visor, donde en cambio es lo que le da
    #    volumen a la pieza mientras gira.
    assert foto["cenital"] < orbita["cenital"] / 2, (
        "el panel cenital del visor es lo que lava el grabado visto a plomo"
    )

    # 2. Luz neutra. Los dos paneles laterales de la foto son del mismo blanco;
    #    el visor conserva los suyos tinteados, y que sigan siendo distintos
    #    entre si es la prueba de que no se unificaron de un lado por accidente.
    assert foto["frio"] == foto["calido"] == float(0xFFFFFF), "la foto va con paneles blancos"
    assert orbita["frio"] != orbita["calido"], "el visor conserva su tinte"
    assert "relleno.color.set(0xffffff)" in codigo, "el relleno de la foto tambien va blanco"

    #    Y con menos ambiente que el visor, que es lo que deja que la sombra
    #    propia del relieve valga algo.
    cielo = re.search(r"const cielo = new THREE\.HemisphereLight\(([^)]*)\)", codigo)
    relleno = re.search(r"const relleno = new THREE\.DirectionalLight\(([^)]*)\)", codigo)
    assert cielo and relleno, "el rig del visor declara el hemisferico y el relleno"
    assert ambiente["hemisferico"] < float(cielo.group(1).split(",")[-1])
    assert ambiente["relleno"] < float(relleno.group(1).split(",")[-1])

    # 3. La sombra queda igual. Lo unico que la proyecta es `principal`, y
    #    ningun ajuste de la foto la toca: que `neutralizarAmbiente` no pueda
    #    nombrarla es la garantia. Si alguien le baja la intensidad ahi adentro
    #    para "suavizar", este test lo frena.
    neutraliza = re.search(r"function neutralizarAmbiente\(.*?\n\}", codigo, re.DOTALL)
    assert neutraliza, "la foto neutraliza su ambiente en una sola funcion"
    assert "principal" not in neutraliza.group(0), (
        "neutralizarAmbiente no puede tocar la luz que proyecta la sombra"
    )
    assert not re.search(r"\bprincipal\.(intensity|color)\b", codigo), (
        "la intensidad y el color de la luz principal son los del visor, sin retoque"
    )


def test_la_vista_imagen_no_dibuja_la_grilla(sesion: TestClient) -> None:
    """El fondo de la foto es liso: la cuadricula es del visor y solo del visor.

    La grilla tiene un solo lugar donde se crea (`agregarPiso`) y la foto pide
    explicitamente que no se dibuje. Un `GridHelper` suelto en otro lado seria
    justo la forma de que reapareciera sin que nadie lo note.
    """
    js = sesion.get("/static/js/preview3d.js").text
    assert js.count("new THREE.GridHelper(") == 1, "la grilla se crea en un solo lugar"
    assert "agregarPiso(escena, { grilla: false })" in js, "la foto tiene que pedir piso sin grilla"
    assert "agregarPiso(escena)" in js, "el visor 3D si conserva la grilla"


def test_la_luz_de_la_foto_escala_con_la_pieza(sesion: TestClient) -> None:
    """Lo unico de la luz del visor que la foto NO copia tal cual, y hace falta.

    `agregarLuces` deja el frustum de sombra fijo en ±160, que alcanza cuando
    la pieza se ve entera y de lejos. En una exportacion a 2048 px no: como
    `lado_mayor_mm` admite hasta 1000, una pieza de mas de 320 mm se saldria
    del frustum y **se quedaria sin sombra** —desaparecida, no mas chica—, y
    eso sale en el archivo que el usuario se lleva.
    """
    js = sesion.get("/static/js/preview3d.js").text
    assert "encuadrarLuz(principal" in js, "la luz de la foto tiene que reajustarse a la pieza"
    assert re.search(r"c\.left = -radio \* [0-9.]+", js), "el frustum se mide en radios de pieza"
    assert re.search(r"c\.far = radio \* [0-9.]+", js)


def test_la_textura_de_impresion_engancha_en_chunks_que_existen(sesion: TestClient) -> None:
    """Los `#include` que `preview3d.js` reemplaza tienen que estar en el three vendorizado.

    `inyectarTexturaImpresion` no escribe un sombreador propio: le mete las
    lineas de capa al sombreador estandar de three reemplazando el texto de
    tres `#include`. Si upstream renombra o parte uno de esos chunks —pasa
    entre revisiones— el `.replace()` **no falla**: devuelve la cadena igual,
    el patron nunca entra y la pieza se ve exactamente como antes. Nadie se
    entera nunca, que es el mismo modo de fallar del import que 404ea.

    Se afirma sobre el TEXTO de `three.module.js` y no sobre un render porque
    la suite no ejecuta JS ni tiene un contexto WebGL. Eso alcanza para lo
    unico silencioso: que el punto de enganche exista. Que el patron se vea
    bien es CV, y se mira con los ojos.
    """
    js = _sin_comentarios(sesion.get("/static/js/preview3d.js").text)
    three = sesion.get("/static/vendor/three/three.module.js").text

    enganches = re.findall(r"'(#include <[\w\d./]+>)'", js)
    assert enganches, "la inyeccion de la textura dejo de reemplazar chunks de three"
    for chunk in set(enganches):
        nombre = chunk[len("#include <") : -1]
        assert f'var {nombre} = "' in three or f"ShaderChunk.{nombre}" in three, (
            f"`preview3d.js` reemplaza `{chunk}` y ese chunk ya no existe en el three vendorizado"
        )

    # La perturbacion tiene que quedar DESPUES de `normal_fragment_begin`: ese
    # chunk termina asignando `nonPerturbedNormal`, que es la normal que three
    # usa para el `normalBias` de la sombra y para la rugosidad por curvatura.
    # Colarse antes llenaria la pieza de acne de sombra.
    assert re.search(r"#include <normal_fragment_begin>\s*\n\s*\{", js), (
        "el patron tiene que ir a continuacion del chunk, no en su lugar"
    )

    # Y el programa compilado tiene que distinguirse del de un material sin
    # capas: dos materiales identicos salvo el color comparten el compilado de
    # three, asi que sin clave propia el cortador podria recibir el programa
    # del marcador sin inyectar, o al reves, segun el orden de carga.
    assert "customProgramCacheKey" in js, "sin clave de cache el patron entra o no segun el orden"

    # Se textura la RAIZ y no cada clon: los clones comparten material, que es
    # lo mismo que hace que `pintarPieza` pinte las dos vistas de una.
    assert "texturarComoImpresion(cargado);" in js
    assert js.index("texturarComoImpresion(cargado);") < js.index("o.alListo(cargado.clone())"), (
        "hay que texturar antes de repartir los clones y antes del primer render"
    )


def test_no_hay_ninguna_url_externa_en_lo_que_se_sirve(sesion: TestClient) -> None:
    """Cero pedidos a internet: es una herramienta que tiene que andar offline."""
    for ruta in ("/login", "/conversor", "/lineas", "/cortante", "/post"):
        cuerpo = sesion.get(ruta, follow_redirects=True).text
        for prohibido in ("//fonts.googleapis", "//cdn.", "//unpkg", "//cdnjs"):
            assert prohibido not in cuerpo, f"{ruta} apunta afuera ({prohibido})"


# ── F2: la pantalla Correcto ────────────────────────────────────────────────

#: Los ganchos de la normalizacion de trazo. Los dos `p-*` los dibuja el macro
#: `campo_parametro` a partir del nombre del parametro, asi que este test ata el
#: nombre del campo del formulario al que el router recibe: renombrar uno solo
#: deja el switch encendido mandando el default de siempre.
IDS_NORMALIZAR = (
    "boton-normalizar",
    "switch-normalizar",
    "campos-normalizar",
    "p-ancho_trazo_mm",
    "p-lado_mayor_mm",
    # Donde se declara la escala de trabajo: la normalizacion amplia la imagen
    # y este es el unico lugar de la pantalla que lo dice.
    "d-resolucion",
)


def test_los_ganchos_de_la_normalizacion_existen_en_las_dos_puntas(sesion: TestClient) -> None:
    html = sesion.get("/lineas").text
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    for ident in IDS_NORMALIZAR:
        assert f'id="{ident}"' in html, f"/lineas no dibuja #{ident}"
    for gancho in ("#boton-normalizar", "#switch-normalizar", "#campos-normalizar"):
        assert f"'{gancho}'" in js, f"ningun script busca {gancho}"
    # Los campos se resuelven por plantilla (`#p-${nombre}`), asi que lo que hay
    # que exigir es la lista de nombres y no el id armado.
    assert "'ancho_trazo_mm', 'lado_mayor_mm'" in js


def test_la_normalizacion_nace_apagada(sesion: TestClient) -> None:
    """El default es no tocar el dibujo, y el panel de medidas nace escondido.

    Es el contrato de la opcion: quien no la pide se lleva la misma salida de
    siempre. Un `aria-checked="true"` de arranque la encenderia para todos sin
    que nadie la haya elegido.
    """
    html = sesion.get("/lineas").text
    assert '<span class="switch" id="switch-normalizar" role="switch" aria-checked="false">' in html
    assert '<div class="parametros oculto" id="campos-normalizar">' in html


def test_un_origen_nuevo_en_la_url_no_retoma_el_trabajo_de_otro_diseno(
    sesion: TestClient,
) -> None:
    """El trabajo recordado en `sessionStorage` vale solo para la entrada recordada.

    El bug: generar el cortante de A, volver a Correcto, hacer B y "seguir a
    crear cortante". La pantalla tomaba el origen B de la URL pero retomaba el
    trabajo de A, cuyo `retomarTrabajo` firma el estado ACTUAL como generado:
    el cortante viejo aparecia pintado y "Generar" quedaba apagado hasta un F5.
    Correcto tenia el mismo patron y pintaba la correccion anterior al lado del
    original nuevo.

    Sin navegador solo se puede exigir el contrato del codigo: una unica regla
    de modulo, que las DOS pantallas la usen, y que la linea vieja —tomar el
    trabajo recordado a ciegas— no vuelva.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert "function trabajoQueSigueVigente(recordado, origenDeUrl)" in js
    assert "if (origenDeUrl && origenDeUrl !== recordado.origen) return null;" in js

    # Solo las dos pantallas que se encadenan por `?origen=`. Post no lo hace
    # —recibe archivos, no un trabajo anterior— y retomar a ciegas ahi es
    # correcto, asi que la prohibicion se mira por pantalla y no en todo el JS.
    # El cuerpo de cada `iniciar*` termina donde arranca la siguiente funcion
    # de nivel de modulo: las internas van indentadas y no cortan.
    for pantalla in ("Lineas", "Cortante"):
        cuerpo = js.split(f"function iniciar{pantalla}()")[1].split("\nfunction ")[0]
        assert "let trabajoId = trabajoQueSigueVigente(recordado, origenDeUrl);" in cuerpo, (
            f"iniciar{pantalla} no aplica la regla"
        )
        assert "recordado.trabajo || null" not in cuerpo, (
            f"iniciar{pantalla} volvio a retomar el trabajo recordado a ciegas"
        )


# ── F4: la pantalla post ────────────────────────────────────────────────────


def test_el_grupo_de_descargas_de_post_lo_dibuja_el_js(sesion: TestClient) -> None:
    """Post tiene una descarga por diseño y la cantidad no se sabe de antemano.

    Por eso el grupo llega **vacio** del servidor y lo llena `app.js` cuando el
    lote termina. Lo que si tiene que estar es el contenedor, que es el gancho.

    ⚠ `#bajar-todo` NO puede existir en esta pantalla, y no es cosmetico: el
    modulo compartido intercepta ese id para rendir y subir la foto ANTES de
    navegar. En post las fotos ya estan todas arriba, asi que esa interceptacion
    volveria a rendir la del diseño que quedo en el visor y la subiria encima de
    otra. El ZIP de post es un enlace comun que dibuja el JS.
    """
    html = sesion.get("/post").text
    grupo = re.search(r'<div[^>]*id="botones-descarga"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert grupo is not None, "no existe el grupo de descargas"
    assert not grupo.group(1).strip(), "el grupo tiene que llegar vacio: lo llena el JS"
    assert 'id="bajar-todo"' not in html, "ese id dispara la coreografia de cortante"

    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert "subirAlDescargar: false" in js, "post no puede re-rendir la foto al bajarla"


def test_la_vista_previa_tiene_una_sola_implementacion(sesion: TestClient) -> None:
    """Las dos pantallas con visor comparten el codigo, no lo copian.

    Es la unica forma de sostener lo que F4 promete: que la foto de un archivo
    viejo sea **la misma** que la de su ciclo. Dos copias parecidas de la
    coreografia de la foto se separan sin que nadie se entere — es el mismo
    razonamiento por el que `preview3d.js` tampoco tiene una version por
    pantalla.
    """
    codigo = _sin_comentarios(sesion.get("/static/js/app.js").text)

    assert codigo.count("function iniciarVistaPrevia3D(") == 1
    assert codigo.count("function pedirFoto(") == 1, "la coreografia de la foto esta duplicada"
    assert codigo.count("function iniciarPaleta(") == 1
    assert codigo.count("function iniciarVistas(") == 1

    # Y las dos pantallas la usan de verdad. Se cuentan los llamados y no las
    # apariciones: la definicion tambien contiene el nombre seguido de `({`.
    assert codigo.count("= iniciarVistaPrevia3D({") == 2, (
        "cortante y post tienen que arrancar la MISMA vista previa"
    )
    assert "if (pagina === 'post') iniciarPost();" in codigo


def test_el_color_de_un_diseno_se_elige_en_la_vista_previa(sesion: TestClient) -> None:
    """La fila de la lista NO tiene muestras de color, y eso es el pedido.

    Las tuvo un ciclo: una paleta de pieza y una de fondo por diseño, clonadas
    de las de la pagina. Se fueron porque el color hay que verlo **sobre la
    pieza** —veinticinco filas por dos paletas son cientos de pastillas para
    elegir a ciegas— y porque la fila ya dice lo suyo, que es como quedaron
    agrupados los archivos. El color se elige donde se ve: en la vista previa.

    Lo que la fila gana a cambio es `Ver`, que trae ese diseño al visor. Sin eso
    no habria forma de elegir a cual de las veinticinco le esta pegando la
    paleta, y el control quedaria apuntando siempre al ultimo del lote.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)

    assert "clonarPaleta" not in js, "volvieron las copias de paleta por fila"
    assert "cloneNode" not in js.split("function iniciarPost()")[1], (
        "post vuelve a clonar algo para las filas"
    )
    assert "boton('Ver'" in js, "la fila no tiene como traer el diseño a la vista previa"
    assert "mostrarDiseno" in js, "nadie trae un diseño al visor"


def test_las_paletas_de_post_le_pegan_a_lo_que_se_esta_mirando(sesion: TestClient) -> None:
    """Un control que a veces pega en uno y a veces en todos tiene que decirlo.

    En post la misma paleta hace las dos cosas: con un diseño en el visor le
    cambia el color a ese, y sin ninguno —antes del lote, cuando no hay foto que
    mirar— a todos. Es lo unico que puede significar en cada caso, pero no se
    adivina: por eso el rotulo se reescribe y nombra a quien le esta pegando.
    """
    html = sesion.get("/post").text
    for ident in ("rotulo-pieza", "rotulo-fondo"):
        assert f'id="{ident}"' in html, f"#{ident} es lo que dice a quien le pega la paleta"

    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    elegir = re.search(r"function elegirColor\(.*?\n  \}", js, re.DOTALL)
    assert elegir is not None, "no existe `elegirColor`"
    # ⚠ Sobre la LISTA que se escribe, no sobre un `mirando ?` suelto: el cuerpo
    # tiene otro ternario con `mirando` —el que decide que fotos rehacer— y
    # exigir solo el nombre dejaba pasar una version que le pegaba a los
    # veinticinco. Es la misma trampa del `cloneNode` del ciclo anterior.
    assert "[disenos[mirando - 1]] : disenos" in elegir.group(0), (
        "la paleta le pega siempre a lo mismo: o al que se mira o a todos, pero no a los dos"
    )
    assert "rotularPaletas" in js, "el rotulo no dice a quien le pega la paleta"


def test_la_foto_de_cada_diseno_se_rinde_con_sus_colores(sesion: TestClient) -> None:
    """El orden importa: los colores van antes de CADA exportacion.

    `preview3d.js` pinta la pieza cuando termina de cargarla, con el color que
    tenga puesto en ese momento — pero lo que decide el color del JPG es lo que
    el visor tiene puesto cuando se rinde. Y desde que cada pieza se fotografia
    **dos veces** con colores distintos, pintar solo al cargar no alcanza: la
    segunda foto saldria con los colores de la primera.

    Se afirma sobre el orden en el CODIGO porque ningun test de este proyecto
    ejecuta JS. Es lo mas cerca que la suite puede estar de "cada foto salio de
    su color"; que se vea bien es CV y se mira con los ojos.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    rendir = re.search(r"async function rendirDiseno\(.*?\n  \}", js, re.DOTALL)
    assert rendir is not None, "no existe `rendirDiseno`"
    cuerpo = rendir.group(0)

    assert cuerpo.index("previa.pintar") < cuerpo.index("previa.cargar"), (
        "el modelo se carga antes de pintarlo: aparece un frame con el color del anterior"
    )
    bucle = cuerpo[cuerpo.index("for (const paso of pasos)") :]
    assert bucle.index("previa.pintar") < bucle.index("previa.exportar"), (
        "se exporta antes de pintar: la segunda foto sale con los colores de la primera"
    )


def test_cambiar_un_color_despues_del_lote_rehace_su_foto(sesion: TestClient) -> None:
    """El desfasaje que este cambio podia introducir, y lo que lo cierra.

    Despues del lote hay un JPG por diseño en el servidor. Cambiar el color de
    uno y no rehacer su foto dejaria la pantalla diciendo un color y la descarga
    con el anterior, sin un solo error a la vista: es exactamente el fallback
    silencioso que este proyecto no se permite.

    Y la URL de la descarga tiene que cambiar, porque el archivo del servidor se
    llama igual: sin el `?v=` el navegador sirve la que ya tenia en cache.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert "encolarVistas" in js, "nadie rehace la foto de un diseño repintado"
    assert "conFoto" in js, (
        "hay que distinguir el diseño que ya tiene foto del que todavia no: "
        "antes de procesar no hay nada que rehacer"
    )
    assert "?v=${versionFotos}" in js, "la descarga no invalida la foto cacheada"


def test_el_set_tiene_su_propia_pieza_y_su_propio_fondo(sesion: TestClient) -> None:
    """El set es un entregable aparte, con su combinacion de colores.

    No es un recorte de las fotos sueltas: cada diseño se vuelve a rendir con
    estos dos colores y esa segunda foto es la celda. Por eso son DOS paletas y
    no una, y por eso la del fondo es `COLORES_FONDO` como la de la vista
    imagen — aca tambien hay un render con piso y con sombra que `Sin fondo`
    puede sacar. Cuando el set solo pegaba fotos ya hechas esa muestra no
    habria querido decir nada, y por eso entonces no estaba.
    """
    html = sesion.get("/post").text
    for ident, lista in (("paleta-set-pieza", COLORES), ("paleta-set-fondo", COLORES_FONDO)):
        muestras = _paleta(html, ident)
        assert len(muestras) == len(lista), f"#{ident} no dibuja las {len(lista)} muestras"
        for etiqueta, color in zip(muestras, lista, strict=True):
            assert _atributo(etiqueta, "data-color") == color.hex, ident

    sin_piso = [e for e in _paleta(html, "paleta-set-fondo") if _atributo(e, "data-sin-piso")]
    assert len(sin_piso) == 1, "el fondo del set tiene que ofrecer `Sin fondo`, como el de la foto"
    assert not any(_atributo(e, "data-sin-piso") for e in _paleta(html, "paleta-set-pieza")), (
        "ningun color de PIEZA apaga el piso"
    )

    # Son de post: cortante hace una sola foto, no hay set que armar.
    cortante = sesion.get("/cortante").text
    assert 'id="paleta-set-pieza"' not in cortante
    assert 'id="paleta-set-fondo"' not in cortante


def test_los_colores_del_set_se_pueden_elegir_antes_del_lote(sesion: TestClient) -> None:
    """El panel del set arranca visible y lo que se esconde es su resultado.

    No es una preferencia de layout: **es el costo**. Esos dos colores son los
    que el navegador usa al rendir cada celda durante el lote, asi que si las
    paletas aparecieran recien con el set hecho, elegirlas costaria rendir las
    veinticinco piezas de nuevo. Antes del lote la eleccion es gratis.
    """
    html = sesion.get("/post").text
    panel = re.search(r'<div class="([^"]*)" id="panel-set">', html)
    assert panel is not None, "no existe el panel del set"
    assert "oculto" not in panel.group(1), (
        "el panel del set arranca escondido y sus colores se eligen cuando ya cuestan renders"
    )
    resultado = re.search(r'<div class="([^"]*)" id="resultado-set"', html)
    assert resultado is not None, "no existe el bloque del resultado del set"
    assert "oculto" in resultado.group(1), "la lamina se muestra antes de existir"

    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert "mostrar($('#panel-set')" not in js, "el JS esconde el panel entero, con sus paletas"
    assert "mostrar($('#resultado-set'), true)" in js, "nadie muestra la lamina cuando esta lista"


def test_el_set_se_rinde_aparte_y_no_toca_las_fotos_sueltas(sesion: TestClient) -> None:
    """Las dos fotos de cada pieza, y que sus colores no se crucen.

    La celda se sube con la otra clave (`jpg_set`), se rinde con `colorSet` y no
    con los del diseño, y cambiar un color del set encola celdas — nunca fotos
    sueltas. Si alguna de esas tres se cruzara, elegir el color del set le
    cambiaria el color a lo que el usuario se baja por separado, que es
    exactamente lo que se pidio que no pase.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)

    assert "CLAVE_CELDA = 'jpg_set'" in js, "no existe la clave de la foto del set"
    assert "/imagen/${clave}" in js, "la subida no dice cual de las dos fotos es"

    encolar = re.search(r"function encolarElSet\(.*?\n  \}", js, re.DOTALL)
    assert encolar is not None, "no existe `encolarElSet`"
    assert "pendientesSet" in encolar.group(0)
    assert "pendientesVista" not in encolar.group(0), (
        "cambiar el color del set rehace tambien las fotos sueltas"
    )

    rendir = re.search(r"async function rendirDiseno\(.*?\n  \}", js, re.DOTALL)
    assert rendir is not None
    assert "colores: colorSet, clave: CLAVE_CELDA" in rendir.group(0), (
        "la celda no se rinde con los colores del set"
    )
    assert "append('fondo', colorSet.fondo)" in js, "el fondo del set no se manda al servidor"


def test_las_paletas_no_aceptan_un_color_con_el_canvas_ocupado(sesion: TestClient) -> None:
    """Hay UN canvas, asi que mientras rinde una foto no se le cambia el color.

    `iniciarPaleta` le despacha el color al visor **en el acto**, antes de
    avisarle a la pantalla, asi que un clic mientras corre la cola le llega a la
    exportacion en vuelo: la foto se sube con un color que no es el que le toca,
    y la cola no sabe que tiene que rehacerla. La ventana es angosta —el render
    de 2048 px arranca en el mismo bloque sincronico que la pintada— pero la
    escalera de calidades de `aJpg` tiene `await` en el medio.

    Se apagan las muestras, no se ignoran los clics: un boton que acepta el clic
    y no cumple es peor que uno apagado.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    controles = re.search(r"function actualizarControles\(.*?\n  \}", js, re.DOTALL)
    assert controles is not None, "no existe `actualizarControles`"
    cuerpo = controles.group(0)
    assert "trabajando || rehaciendo" in cuerpo, "el `ocupado` no mira las dos cosas"
    assert ".paleta__color" in cuerpo and "b.disabled = ocupado" in cuerpo, (
        "las paletas siguen aceptando colores mientras se rinde una foto"
    )
    assert ":disabled" in sesion.get("/static/css/estilo.css").text


def test_la_vista_previa_avisa_mientras_carga(sesion: TestClient) -> None:
    """El velo de carga, en las DOS pantallas con visor.

    Un `.glb` tarda, y mas en post, donde el lote carga uno por diseño. Sin
    aviso el panel se queda quieto y no hay forma de distinguir "esta bajando"
    de "se colgo".

    Lo unico que la suite puede afirmar es que exista y que este atado a los dos
    avisos que lo apagan. Que se vea girar es CV.
    """
    for pagina in CON_VISOR:
        servido = sesion.get(pagina).text
        assert 'id="cargando"' in servido, f"{pagina} no tiene velo de carga"
        # El texto tiene id propio porque el velo dice a QUE esta esperando, y
        # eso lo reescribe `app.js` leyendo el de fabrica de aca.
        assert 'id="cargando-texto"' in servido, f"{pagina} no puede decir que esta esperando"

    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    velo = re.search(r"function cargando\(.*?\n  \}", js, re.DOTALL)
    assert velo is not None, "no existe el interruptor del velo"
    # ⚠ Se apaga solo. Sin WebGL no hay quien emita el aviso de carga, y un velo
    # pegado tapa una pantalla que por lo demas anda.
    assert "setTimeout" in velo.group(0), "el velo no se apaga solo si el aviso no llega"
    assert "cargando(true)" in js and "cargando(false)" in js

    css = sesion.get("/static/css/estilo.css").text
    assert ".cargando" in css and "@keyframes girar" in css, "el velo no tiene rueda"


def test_el_visor_se_tapa_mientras_la_cola_lo_pinta_de_otro_color(sesion: TestClient) -> None:
    """El desfasaje que no se puede ver: la pieza pintada de un color ajeno.

    Rehacer las celdas pinta cada diseño con los colores del SET —que no son los
    de ninguna foto suelta— y al terminar devuelve el visor a lo que se estaba
    mirando. Ese ida y vuelta, en pantalla, se lee como si el color del diseño se
    hubiera cambiado solo y vuelto atras. Se tapa mientras dura.

    Lo que NO se tapa es rehacer la foto del diseño que se esta mirando: ahi el
    usuario acaba de elegir ese color y taparselo seria esconderle justo lo que
    pidio.

    El velo tiene que ser SOSTENIDO: el lote carga un modelo por diseño y cada
    carga termina apagandolo, asi que uno normal parpadearia una vez por diseño
    —y entre parpadeo y parpadeo se veria exactamente lo que se quiso ocultar.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)

    tapa = re.search(r"function laColaTapa\(.*?\n  \}", js, re.DOTALL)
    assert tapa is not None, "no existe `laColaTapa`"
    assert "pendientesSet.size) return true" in tapa.group(0), (
        "las celdas del set no tapan el visor"
    )
    assert "!== mirando" in tapa.group(0), (
        "una foto suelta de otro diseño tampoco es lo que se esta mirando"
    )

    velo = re.search(r"function cargando\(.*?\n  \}", js, re.DOTALL)
    assert "if (sostenido) return;" in velo.group(0), (
        "una carga puede apagar el velo sostenido: parpadea una vez por diseño"
    )

    # ⚠ Hay mas de un `#procesar` en este archivo —Correcto tiene el suyo— asi
    # que el bloque se elige por lo que hace y no por el primero que aparece.
    bloques = re.finditer(r"procesar\.addEventListener\(.*?\n  \}\);", js, re.DOTALL)
    lote = next((b.group(0) for b in bloques if "sacarLasFotos(" in b.group(0)), None)
    assert lote is not None, "no existe el boton del lote de post"
    assert "previa.tapar(true" in lote, "el lote no tapa el visor"
    assert "previa.tapar(false)" in lote, "el lote no lo destapa"

    cola = re.search(r"async function correrPendientes\(.*?\n  \}", js, re.DOTALL)
    assert cola is not None
    assert "previa.tapar(laColaTapa()" in cola.group(0), "la cola no consulta si tiene que tapar"
    assert "previa.tapar(false)" in cola.group(0), "la cola no destapa al terminar"


def test_el_lote_saca_las_fotos_sueltas_antes_que_las_del_set(sesion: TestClient) -> None:
    """Dos fases, no una pasada que intercala las dos fotos de cada diseño.

    Son dos entregables distintos —lo que se baja por diseño y la materia prima
    del set— y separarlos deja las descargas listas sin esperar a la lamina. El
    precio es cargar cada modelo dos veces, una por fase; esta escrito en el
    docstring de `sacarLasFotos`.

    Y al destapar, el visor queda en el PRIMER diseño: la lista se revisa desde
    arriba. El ultimo es donde quedo la maquina, que no es una razon.
    """
    js = _sin_comentarios(sesion.get("/static/js/app.js").text)

    fotos = re.search(r"async function sacarLasFotos\(.*?\n  \}", js, re.DOTALL)
    assert fotos is not None, "no existe `sacarLasFotos`"
    cuerpo = fotos.group(0)
    assert "{ vista: true, celda: false }" in cuerpo, "la primera fase no es solo la foto suelta"
    assert "{ vista: true, celda: true }" not in cuerpo, (
        "el lote sigue sacando las dos fotos en la misma pasada"
    )
    assert cuerpo.index("pintarDescargas(") < cuerpo.index("sacarLasCeldas("), (
        "las descargas por diseño esperan al set"
    )
    assert "fijarMirado(ok[0].indice)" in cuerpo, "el visor no queda en el primer diseño"

    celdas = re.search(r"async function sacarLasCeldas\(.*?\n  \}", js, re.DOTALL)
    assert celdas is not None, "no existe la fase del set"
    assert "{ vista: false, celda: true }" in celdas.group(0), (
        "la segunda fase vuelve a sacar la foto suelta"
    )


def test_el_set_avisa_mientras_se_arma(sesion: TestClient) -> None:
    """La lamina tarda dos cosas y las dos van bajo el mismo aviso.

    Rendir una celda por diseño y despues pegarlas del lado del servidor son,
    para quien mira, una sola espera. El aviso va en el panel del set y no sobre
    el visor: aca no hay nada que tapar, hay un lugar vacio que explicar.

    ⚠ **Se apaga con el `load` de la imagen, no con la respuesta del servidor.**
    El POST contesta cuando la lamina esta escrita en disco; recien ahi el
    navegador sale a buscarla, y son 2 MB.
    """
    pagina = sesion.get("/post").text
    assert 'id="cargando-set"' in pagina, "el set no tiene aviso propio"
    assert "cargando--bloque" in pagina, "el aviso del set tapa en vez de ocupar su lugar"
    assert ".cargando--bloque" in sesion.get("/static/css/estilo.css").text

    js = _sin_comentarios(sesion.get("/static/js/app.js").text)
    assert "$('#img-set').addEventListener('load'" in js, (
        "el aviso del set no espera a que la imagen este en pantalla"
    )
    celdas = re.search(r"async function sacarLasCeldas\(.*?\n  \}", js, re.DOTALL)
    assert celdas is not None
    assert "mostrarArmandoElSet(true)" in celdas.group(0), (
        "la fase del set no avisa que esta armando"
    )


def test_post_es_tan_ancho_como_cortante(sesion: TestClient) -> None:
    """Las dos pantallas del banco de tres columnas necesitan el ancho grande.

    Post se quedo afuera de esa regla un ciclo y el sintoma fue exacto: con los
    900 px del default, las dos columnas fijas del banco (290 + 340 mas los
    gaps) dejaban al visor unos 200 px en monitor, y en el telefono se veia
    bien — abajo de 1200 px la grilla se apila y el visor se lleva el ancho
    entero.

    Es lo unico de este arreglo que se puede afirmar desde la suite: **ningun
    gate de este proyecto mira el CSS**, y el ancho que termina midiendo el
    visor depende del layout, que no hay como calcular sin navegador.
    """
    css = sesion.get("/static/css/estilo.css").text
    regla = re.search(r"([^}]*)\{\s*--ancho-contenido:\s*1400px", css)
    assert regla is not None, "se fue la regla del ancho grande"
    for pagina in ("cortante", "post"):
        assert f"data-pagina='{pagina}'" in regla.group(1), (
            f"{pagina} no recibe el ancho del banco de tres columnas"
        )

    # Y las dos lo declaran en el `body`, que es a quien apunta el selector.
    for pagina in CON_VISOR:
        assert f'data-pagina="{pagina.lstrip("/")}"' in sesion.get(pagina).text


def test_post_usa_los_mismos_eventos_que_cortante(sesion: TestClient) -> None:
    """Los `cortante:*` son contrato con `preview3d.js`, no el nombre de una pantalla.

    `preview3d.js` los escucha literales y resuelve sus ganchos por id una sola
    vez a nivel de modulo: no hay namespace por pantalla. Renombrarlos para post
    dejaria la vista previa muerta y sin un solo error a la vista.
    """
    visor = _sin_comentarios(sesion.get("/static/js/preview3d.js").text)
    for evento in ("cortante:listo", "cortante:color", "cortante:fondo", "cortante:vista"):
        assert evento in visor, f"{evento} dejo de existir en el visor"

    html = sesion.get("/post").text
    for ident in ("visor", "lienzo", "paleta", "paleta-pieza", "paleta-fondo"):
        assert f'id="{ident}"' in html, f"/post no dibuja #{ident}, que `preview3d.js` busca"


def test_post_aparece_en_el_menu_con_su_icono(sesion: TestClient) -> None:
    """Sin la rama en el macro `icono`, el `<svg>` del menu sale vacio."""
    claves = [m.clave for m in MODULOS]
    assert "post" in claves, "el modulo no esta declarado en MODULOS"

    html = sesion.get("/post").text
    enlace = re.search(r'<a[^>]*href="/post"[^>]*>(.*?)</a>', html, re.DOTALL)
    assert enlace is not None, "el menu no enlaza a /post"
    assert re.search(r"<(path|rect|circle)", enlace.group(1)), "el icono de post sale vacio"
