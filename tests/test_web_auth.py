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

from app.archivos import SUFIJO_DESCARGA, ClaveArchivo
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
    assert "THREE.PCFSoftShadowMap" in js
    assert not re.search(r"THREE\.PCFShadowMap\b", js), (
        "el mapa de sombra de la foto tiene que ser el mismo del visor"
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
