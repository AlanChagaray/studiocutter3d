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

from app.routers.cortante import COLORES
from app.routers.paginas import MODULOS

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
# El fondo arranca en el segundo y no en el primero a proposito: con el fondo y
# la pieza del mismo color la foto sale de un solo tono, y la pieza arranca en
# el primero.
PALETAS = {
    "paleta": "Blanco",
    "paleta-pieza": "Blanco",
    "paleta-fondo": "Gris",
}


def test_las_tres_paletas_dibujan_los_ocho_colores(sesion: TestClient) -> None:
    """Cada paleta es exactamente `COLORES`, en orden y completa."""
    html = sesion.get("/cortante").text
    assert len(COLORES) == 8

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

    for ident, inicial in PALETAS.items():
        muestras = _paleta(html, ident)
        assert len(muestras) == len(COLORES), f"#{ident} no dibuja los 8 colores"

        for etiqueta, color in zip(muestras, COLORES, strict=True):
            assert _atributo(etiqueta, "data-color") == color.hex, ident
            assert _atributo(etiqueta, "aria-label") == color.nombre, ident
            assert re.fullmatch(r"#[0-9a-f]{6}", color.hex), color

        marcadas = [e for e in muestras if _atributo(e, "aria-pressed") == "true"]
        assert len(marcadas) == 1, f"#{ident} tiene que arrancar con una sola muestra elegida"
        assert _atributo(marcadas[0], "aria-label") == inicial, ident

    # Los dos defaults tienen que ser distintos: si alguien los iguala, la
    # primera foto sale de un solo tono y parece que la vista esta rota.
    assert PALETAS["paleta-fondo"] != PALETAS["paleta-pieza"]


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
    for pagina in ("conversor", "lineas", "cortante"):
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
    "bajar-imagen",
    "pista-imagen",
)


def test_los_ids_de_la_vista_imagen_existen_en_las_dos_puntas(sesion: TestClient) -> None:
    html = sesion.get("/cortante").text
    js = sesion.get("/static/js/app.js").text + sesion.get("/static/js/preview3d.js").text
    for ident in IDS_VISTA_IMAGEN:
        assert f'id="{ident}"' in html, f"la pantalla no dibuja #{ident}"
        assert ident in js, f"ningun script maneja #{ident}"


def test_el_boton_de_la_imagen_nace_apagado_con_el_motivo(sesion: TestClient) -> None:
    """Sin cortante generado no hay nada que fotografiar, y se dice."""
    html = sesion.get("/cortante").text
    boton = re.search(r'<button[^>]*id="bajar-imagen"[^>]*>', html, re.DOTALL)
    assert boton is not None
    assert "disabled" in boton.group(0)
    assert "title=" in boton.group(0), "un boton apagado sin motivo no se entiende"
    # Lo prende el modulo del visor, que es el unico que sabe si el .glb cargo.
    assert "boton.disabled = false" in sesion.get("/static/js/preview3d.js").text


def test_los_eventos_entre_la_pantalla_y_el_visor_cierran(sesion: TestClient) -> None:
    """Los tres avisos tienen emisor en `app.js` y oyente en `preview3d.js`.

    Es el contrato entre los dos scripts —no hay bundler ni imports entre
    ellos, se hablan por eventos del documento—, asi que un nombre cambiado de
    un solo lado deja la mitad del pipeline muda y sin ningun error.
    """
    app = sesion.get("/static/js/app.js").text
    visor = sesion.get("/static/js/preview3d.js").text
    for evento in ("cortante:color", "cortante:fondo", "cortante:vista"):
        assert evento in app, f"app.js no emite {evento}"
        assert evento in visor, f"preview3d.js no escucha {evento}"


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
    assert "-vista.jpg" in js, "la descarga tiene que salir con extension .jpg"
    # `toBlob` es asincronico: sin esto el buffer ya se limpio cuando el
    # navegador lo va a leer y el JPG sale en negro.
    assert "preserveDrawingBuffer" in js


def test_la_imagen_es_cenital_con_fondo_liso_y_sombra(sesion: TestClient) -> None:
    """Las cuatro decisiones de la toma, cada una con su mecanismo en el codigo."""
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
    # Dos sombras con aporte parcial: `getShadowMask()` multiplica el de cada
    # luz, asi que donde se superponen queda el nucleo oscuro del contacto.
    assert "aporteClave" in js and "aporteCenital" in js, "falta el aporte de cada sombra"
    assert "luz.shadow.intensity = aporte" in js, "el aporte no llega a la luz"


def test_la_sombra_es_difusa_y_no_una_silueta_pegada(sesion: TestClient) -> None:
    """La sombra tiene que degradar, no recortar.

    Medido contra la foto de referencia (`tests/ejemplo-sombras.png`), que cae
    32% con 18 niveles por 1% del ancho: asi queda en 29% con 9 — la misma
    presencia y el doble de difuminada. Antes caia 34% de golpe, en un escalon
    de 50 niveles, y eso es lo que se veia "marcado".
    """
    js = sesion.get("/static/js/preview3d.js").text

    # El tipo de shadow map es el ajuste que decide si esto es posible:
    # `PCFSoftShadowMap` —el del visor— IGNORA `shadow.radius`, asi que su
    # borde es suave dos pixeles y despues es un escalon.
    assert "THREE.PCFShadowMap" in js, "sin PCFShadowMap el radio de difusion no hace nada"
    assert "luz.shadow.radius = difusion" in js, "la difusion no llega a la luz"

    # Las dos sombras tienen difusion distinta: la clave arma la penumbra ancha
    # y la cenital el apoyo pegado al contacto. Iguales, la sombra queda de
    # densidad pareja, que es justo lo que se veia mal.
    clave = re.search(r"\bdifusionClave:\s*([0-9.]+)", js)
    cenital = re.search(r"\bdifusionCenital:\s*([0-9.]+)", js)
    assert clave is not None and cenital is not None, "faltan las difusiones"
    assert float(clave.group(1)) >= 30, "la penumbra ancha necesita un radio grande"
    assert float(cenital.group(1)) < float(clave.group(1)), (
        "la sombra de contacto tiene que ser mas cerrada que la penumbra ancha"
    )

    # El fondo liso no se negocia: VSM da una penumbra mas pareja pero filtra
    # luz y deja bandas diagonales sobre el fondo (desvio 0,77 contra 0,00).
    assert "THREE.VSMShadowMap" not in js.replace("`VSMShadowMap`", ""), (
        "VSM raya el fondo; si vuelve, medir el desvio del fondo antes"
    )


def test_la_foto_no_lava_el_relieve_del_marcador(sesion: TestClient) -> None:
    """Los dos ajustes que deciden si el marcador se distingue o no.

    Son los dos que ya estuvieron mal una vez, y los dos fallan **en silencio**:
    la imagen sale, se descarga, y el relieve simplemente no esta. Medido en la
    region del marcador, con el bias grande el detalle local daba 6,7 y el plato
    salia en 241 de 243 de maximo; con estos valores da 11,8 y 219.
    """
    js = sesion.get("/static/js/preview3d.js").text

    # 1. `normalBias` en milimetros. Con 0,35 —el valor del visor, donde la
    #    pieza se ve entera y de lejos— el corrimiento es el 17% de la altura
    #    de un trazo de 2 mm y borra justo las sombras propias del relieve.
    bias = re.search(r"\bbiasNormal:\s*([0-9.]+)", js)
    assert bias is not None, "falta `biasNormal` en el bloque FOTO"
    assert float(bias.group(1)) <= 0.05, (
        f"normalBias de {bias.group(1)} mm borra el relieve del marcador; "
        "el del visor 3D (0,4) no sirve para una toma cenital de cerca"
    )

    # 2. La toma cenital NO puede reusar la caja de estudio del visor: su panel
    #    de arriba ilumina igual el plato y el relieve, y eso es exactamente el
    #    lavado que tapa el marcador.
    assert "ESTUDIO_ORBITA" in js and "estudio:" in js, "faltan los dos repartos de estudio"
    orbita = re.search(r"ESTUDIO_ORBITA = \{ cenital: ([0-9.]+)", js)
    foto = re.search(r"estudio: \{ cenital: ([0-9.]+)", js)
    assert orbita is not None and foto is not None
    assert float(foto.group(1)) < float(orbita.group(1)), (
        "el panel cenital de la foto tiene que ser MAS debil que el del visor"
    )


def test_no_hay_ninguna_url_externa_en_lo_que_se_sirve(sesion: TestClient) -> None:
    """Cero pedidos a internet: es una herramienta que tiene que andar offline."""
    for ruta in ("/login", "/conversor", "/lineas", "/cortante"):
        cuerpo = sesion.get(ruta, follow_redirects=True).text
        for prohibido in ("//fonts.googleapis", "//cdn.", "//unpkg", "//cdnjs"):
            assert prohibido not in cuerpo, f"{ruta} apunta afuera ({prohibido})"
