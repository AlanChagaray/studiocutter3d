"""Directorios de trabajo, subidas y descargas.

**El cliente no nombra ningun archivo EN DISCO.** Ni el de entrada ni el de
salida:

- Lo que sube se guarda como `entrada.<ext>`, donde la extension sale de
  **mirar los bytes**, no el nombre ni el `Content-Type` (los dos los elige
  quien sube). Un adjunto llamado `../../.ssh/authorized_keys` termina en
  `entrada.png` adentro del directorio del trabajo.
- Lo que baja se pide por una **clave de un enum cerrado**, que este modulo
  traduce a un nombre fijo (`NOMBRE_DE`).

**Lo unico que el cliente si nombra es como se VE la descarga.** Para que
`buddy.heif` baje como `buddy.jpg` y no como `studiocutter-a750882a.jpg`, se
guarda el stem del nombre original —saneado por `sanear_nombre_base`— y se usa
en el `filename=` de la respuesta. Esa es toda su influencia: **nunca toca una
ruta**. Las dos mitades no se cruzan, y por eso conservar el nombre no reabre
el path traversal que `validar_id` y `EXTENSION` cierran.

El directorio es `trabajo/<uuid4>/`. Que el id sea un UUID no es cosmetico:
`uuid.UUID(id_)` rechaza de plano cualquier cosa con puntos suspensivos o
separadores de ruta, asi que la validacion del id **es** la defensa contra el
path traversal. El chequeo de contencion posterior es el segundo cinturon.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

from .almacen import AlmacenTrabajos
from .config import MB, Ajustes
from .errores import ErrorApi

NOMBRE_ESTADO = "estado.json"
"""Frontera entre el proceso padre y el hijo. Lo escribe `tareas.py`."""

TROZO = 64 * 1024


class Formato(StrEnum):
    """Formatos que la app sabe leer, detectados por contenido."""

    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"
    SVG = "svg"
    GIF = "gif"
    BMP = "bmp"
    ICO = "ico"
    TGA = "tga"
    PSD = "psd"
    AVIF = "avif"
    HEIF = "heif"
    TIFF = "tiff"
    """Contenedor TIFF. **Deliberadamente ambiguo**: tambien son TIFF los RAW
    de camara ARW, CR2, NEF y DNG, y los primeros bytes no los distinguen. Quien
    decide cual es —y por lo tanto que decodificador usar— es LibRaw al abrirlo,
    en `cutter3d.raster`. Separarlos aca seria inventar una certeza que los
    bytes no dan."""

    CR3 = "cr3"
    """El RAW de Canon moderno. No es TIFF: es ISO-BMFF, el mismo contenedor
    que HEIC y AVIF, y lo separa de ellos la marca `crx `."""


EXTENSION: dict[Formato, str] = {
    Formato.PNG: ".png",
    Formato.JPEG: ".jpg",
    Formato.WEBP: ".webp",
    Formato.SVG: ".svg",
    Formato.GIF: ".gif",
    Formato.BMP: ".bmp",
    Formato.ICO: ".ico",
    Formato.TGA: ".tga",
    Formato.PSD: ".psd",
    Formato.AVIF: ".avif",
    Formato.HEIF: ".heif",
    Formato.TIFF: ".tiff",
    Formato.CR3: ".cr3",
}

#: Que acepta cada pantalla. F2 pide JPG si o si — es requisito del producto,
#: no una limitacion tecnica: la correccion de lineas trabaja sobre un raster.
#: El Convertidor acepta todo lo que se sabe abrir: es la puerta de entrada, y
#: su trabajo es justamente normalizar a jpg o svg lo que venga.
FORMATOS_CONVERSOR = frozenset(Formato)
FORMATOS_LINEAS = frozenset({Formato.JPEG})
FORMATOS_CORTANTE = frozenset({Formato.SVG})
"""**Solo SVG.** El motor trabaja sobre curvas: cada raster que entra hay que
vectorizarlo, y una vectorizacion escondida adentro del cortante es una perdida
de fidelidad que el usuario no eligio ni puede revisar. Vectorizar es trabajo
del Convertidor y de F2, que dejan el SVG a la vista antes de este paso."""

FORMATOS_VISTA = frozenset({Formato.JPEG})
"""Lo unico que acepta el PUT de la foto del cortante.

No se reusa `FORMATOS_LINEAS` aunque hoy valga lo mismo: que coincidan es
casualidad, no un contrato compartido. El dia que F2 acepte PNG, este no
tiene por que seguirlo."""

LIMITE_VISTA_BYTES = 4 * MB
"""Tope propio de la foto, mas ceñido que `tamano_maximo_bytes` (25 MB).

El navegador ya la acota a 2 MB por construccion (`TOPE_BYTES` en
`preview3d.js`), asi que 4 deja margen para el peor caso de la escalera de
calidades y nada mas. El limite general es para el arte que sube el usuario;
esto lo produce nuestro propio front y se sabe cuanto pesa."""


class ClaveArchivo(StrEnum):
    """Lo unico que el cliente puede pedir. Fuera de esta lista es 4xx."""

    TRES_MF = "3mf"
    TRES_MF_MARCADOR = "3mf_marcador"
    TRES_MF_CORTADOR = "3mf_cortador"
    GLB = "glb"
    STL_MARCADOR = "stl_marcador"
    STL_CORTADOR = "stl_cortador"
    JPG = "jpg"
    PNG = "png"
    SVG = "svg"
    JPG_VISTA = "jpg_vista"
    """La foto cenital del cortante, rendida por el navegador y subida aca.

    **No es `JPG`**, que es la salida del Convertidor (`salida.jpg`). Son dos
    archivos distintos, producidos por dos cosas distintas, y darles la misma
    clave haria que un trabajo encadenado pisara uno con el otro."""


NOMBRE_DE: dict[ClaveArchivo, str] = {
    ClaveArchivo.TRES_MF: "salida.3mf",
    ClaveArchivo.TRES_MF_MARCADOR: "salida_marcador.3mf",
    ClaveArchivo.TRES_MF_CORTADOR: "salida_cortador.3mf",
    ClaveArchivo.GLB: "salida.glb",
    ClaveArchivo.STL_MARCADOR: "salida_marcador.stl",
    ClaveArchivo.STL_CORTADOR: "salida_cortador.stl",
    ClaveArchivo.JPG: "salida.jpg",
    ClaveArchivo.PNG: "salida.png",
    ClaveArchivo.SVG: "salida.svg",
    ClaveArchivo.JPG_VISTA: "vista.jpg",
}

MEDIO_DE: dict[ClaveArchivo, str] = {
    ClaveArchivo.TRES_MF: "model/3mf",
    ClaveArchivo.TRES_MF_MARCADOR: "model/3mf",
    ClaveArchivo.TRES_MF_CORTADOR: "model/3mf",
    ClaveArchivo.GLB: "model/gltf-binary",
    ClaveArchivo.STL_MARCADOR: "model/stl",
    ClaveArchivo.STL_CORTADOR: "model/stl",
    ClaveArchivo.JPG: "image/jpeg",
    ClaveArchivo.PNG: "image/png",
    ClaveArchivo.SVG: "image/svg+xml",
    ClaveArchivo.JPG_VISTA: "image/jpeg",
}

CLAVES_INTERNAS: frozenset[ClaveArchivo] = frozenset({ClaveArchivo.GLB})
"""Lo que el trabajo tiene pero el usuario no baja.

El `.glb` existe para alimentar al visor 3D: no esta en el grupo de descargas
de la pantalla y por lo tanto tampoco va en el ZIP de "descargar todo". Vive
aca —y no como una lista escrita a mano en el router— para que la pantalla y
el ZIP no puedan discrepar sobre que es descargable."""

CLAVES_NO_ENCADENABLES: frozenset[ClaveArchivo] = frozenset(
    {ClaveArchivo.GLB, ClaveArchivo.JPG_VISTA}
)
"""Lo que un trabajo deja pero NO puede ser la entrada del siguiente.

Es una lista distinta de `CLAVES_INTERNAS` y la diferencia importa: aquella es
"que puede bajar el usuario", esta es "que es arte del usuario". La foto de la
vista se baja —es el punto de todo esto— pero **no es el dibujo**: es un render
de la pieza terminada.

Sin esta lista, agregar `JPG_VISTA` cambiaba en silencio el contrato de otra
pantalla. `copiar_desde_trabajo` elige el primer archivo del origen que la
pantalla destino sepa leer, y `FORMATOS_LINEAS` es `{JPEG}`: un cortante que
antes daba `encadenado_incompatible` (409) al encadenarlo con Correcto pasaba a
dar 200 **metiendo el render de WebGL como si fuera el arte**, y heredando el
nombre original para disimularlo. Nadie lo pidio y nadie lo habria visto."""


class FormatoSalida(StrEnum):
    """A que puede convertir el Convertidor. Lo elige el usuario por archivo."""

    JPG = "jpg"
    SVG = "svg"


CLAVE_SALIDA: dict[FormatoSalida, ClaveArchivo] = {
    FormatoSalida.JPG: ClaveArchivo.JPG,
    FormatoSalida.SVG: ClaveArchivo.SVG,
}
"""El formato pedido decide bajo que clave queda el resultado, y la clave
decide el nombre en disco. El cliente sigue sin nombrar ningun archivo."""


SUFIJO_DESCARGA: dict[ClaveArchivo, str] = {
    ClaveArchivo.TRES_MF: ".3mf",
    ClaveArchivo.TRES_MF_MARCADOR: "-marcador.3mf",
    ClaveArchivo.TRES_MF_CORTADOR: "-cortador.3mf",
    ClaveArchivo.GLB: ".glb",
    ClaveArchivo.STL_MARCADOR: "-marcador.stl",
    ClaveArchivo.STL_CORTADOR: "-cortador.stl",
    ClaveArchivo.JPG: ".jpg",
    ClaveArchivo.PNG: ".png",
    ClaveArchivo.SVG: ".svg",
    ClaveArchivo.JPG_VISTA: "-vista.jpg",
}

SUFIJO_ZIP = ".zip"
"""El de "descargar todo". No esta en `SUFIJO_DESCARGA` porque el ZIP no es un
archivo del trabajo: se arma al vuelo y no tiene clave en `ClaveArchivo`."""


@dataclass(frozen=True)
class Subida:
    """Lo que quedo guardado. `formato` sale del contenido, no del nombre."""

    ruta: Path
    formato: Formato
    bytes_escritos: int
    nombre_base: str | None = None
    """Stem saneado del nombre que mando el cliente, o `None`.

    Alimenta **solo** el `filename=` de la descarga. La ruta en disco sigue
    saliendo de `EXTENSION` y `NOMBRE_DE`, que no dependen de esto."""


# ── Deteccion por contenido ──────────────────────────────────────────────────


#: Marcas de la caja `ftyp` que identifican cada formato ISO-BMFF.
MARCAS_HEIF = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1", b"heif"})
MARCAS_AVIF = frozenset({b"avif", b"avis"})

#: Formatos que se reconocen SOLO para poder nombrarlos al rechazarlos. No se
#: abren: EPS necesita Ghostscript instalado en el sistema, y DICOM, OpenEXR y
#: XCF son parsers grandes sobre archivos que sube un usuario. Reconocerlos
#: cuesta una comparacion y convierte un "formato desconocido" en un mensaje
#: que dice que paso.
FUERA_DE_ALCANCE: tuple[tuple[bytes, str], ...] = (
    (b"%!PS", "EPS"),
    (b"\xc5\xd0\xd3\xc6", "EPS"),
    (b"\x76\x2f\x31\x01", "EXR"),
    (b"gimp xcf ", "XCF"),
)


#: Firmas por prefijo, en el orden en que se prueban. Es una TABLA y no una
#: cadena de `if` a proposito: agregar un formato es agregar una fila, y la
#: complejidad de `detectar_formato` no crece con la cantidad de formatos.
FIRMAS: tuple[tuple[bytes, Formato], ...] = (
    (b"\x89PNG\r\n\x1a\n", Formato.PNG),
    # JFIF es JPEG: mismo magic, distinta extension. Por eso se detecta por
    # contenido y el ".jfif" del nombre no importa.
    (b"\xff\xd8\xff", Formato.JPEG),
    (b"GIF87a", Formato.GIF),
    (b"GIF89a", Formato.GIF),
    (b"8BPS", Formato.PSD),
    (b"\x00\x00\x01\x00", Formato.ICO),
    (b"II*\x00", Formato.TIFF),
    (b"MM\x00*", Formato.TIFF),
    # `BM` son solo dos bytes, la firma mas debil de la tabla: va ultima para
    # que ningun formato con firma mas larga caiga aca por accidente.
    (b"BM", Formato.BMP),
)

LARGO_HEADER_TGA = 18


def detectar_formato(cabecera: bytes) -> Formato | None:
    """Formato real segun los bytes. None si no es ninguno de los soportados.

    **El orden importa y no es alfabetico.** Primero lo que necesita mirar mas
    que un prefijo (WEBP y los ISO-BMFF), despues la tabla de firmas, y
    `_parece_svg` SIEMPRE al final: su ultima rama es un `b"<svg" in` sobre los
    primeros 512 bytes, o sea un catch-all textual. Un formato agregado despues
    de el no se alcanzaria nunca.
    """
    if cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WEBP":
        return Formato.WEBP
    if (bmff := _marca_bmff(cabecera)) is not None:
        return bmff
    for firma, formato in FIRMAS:
        if cabecera.startswith(firma):
            return formato
    if _parece_tga(cabecera):
        return Formato.TGA
    if _parece_svg(cabecera):
        return Formato.SVG
    return None


def _marca_bmff(cabecera: bytes) -> Formato | None:
    """ISO-BMFF: caja `ftyp` en el offset 4, marca en el 8.

    Tres formatos de la lista comparten este contenedor y solo la marca los
    separa: el RAW de Canon (`crx `), HEIC/HEIF y AVIF. Mirar unicamente los
    primeros cuatro bytes los confundiria entre si.
    """
    if cabecera[4:8] != b"ftyp":
        return None
    marca = cabecera[8:12]
    if marca == b"crx ":
        return Formato.CR3
    if marca in MARCAS_AVIF:
        return Formato.AVIF
    if marca in MARCAS_HEIF:
        return Formato.HEIF
    return None


def _parece_tga(cabecera: bytes) -> bool:
    """TGA no tiene firma al principio: se valida el header de 18 bytes.

    La firma `TRUEVISION-XFILE` de TGA 2.0 vive al FINAL del archivo, y aca solo
    hay los primeros `TROZO` bytes. Asi que se chequean los campos del header:
    tipo de paleta, tipo de imagen de una lista cerrada y profundidad de color
    valida. **Es una heuristica, no una firma** — por eso corre ultimo entre los
    binarios, cuando ya se descarto todo lo que si tiene magic.
    """
    if len(cabecera) < LARGO_HEADER_TGA:
        return False
    return (
        cabecera[1] in (0, 1)
        and cabecera[2] in (1, 2, 3, 9, 10, 11)
        and cabecera[16] in (8, 15, 16, 24, 32)
    )


def _parece_svg(cabecera: bytes) -> bool:
    texto = cabecera.lstrip(b"\xef\xbb\xbf").lstrip()[:512].lower()
    return texto.startswith(b"<?xml") or texto.startswith(b"<svg") or b"<svg" in texto


def nombrar_no_soportado(cabecera: bytes) -> str:
    """Como llamar a lo que se rechaza, para que el mensaje diga que paso."""
    if cabecera[128:132] == b"DICM":
        return "DICOM"
    for firma, nombre in FUERA_DE_ALCANCE:
        if cabecera.startswith(firma):
            return nombre
    return "desconocido"


# ── Directorios ──────────────────────────────────────────────────────────────


def validar_id(id_: str) -> str:
    """Devuelve el id o corta. Es la defensa contra el path traversal."""
    try:
        uuid.UUID(id_)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404) from exc
    return id_


def dir_de_trabajo(a: Ajustes, id_: str, *, crear: bool = False) -> Path:
    destino = (a.dir_trabajo / validar_id(id_)).resolve()
    raiz = a.dir_trabajo.resolve()
    if not destino.is_relative_to(raiz):  # segundo cinturon: no deberia pasar nunca
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)
    if crear:
        destino.mkdir(parents=True, exist_ok=True)
    return destino


def ruta_de(a: Ajustes, id_: str, clave: ClaveArchivo) -> Path | None:
    ruta = dir_de_trabajo(a, id_) / NOMBRE_DE[clave]
    return ruta if ruta.is_file() else None


LARGO_MAX_NOMBRE = 60
"""Tope del stem saneado. No es estetico: un nombre de 400 caracteres revienta
`NAME_MAX` del sistema de archivos, y el sintoma seria un `OSError` a mitad de
la copia en vez de un rechazo limpio."""

_SEPARADORES = re.compile(r"[\\/]")
_NO_PERMITIDO = re.compile(r"[^A-Za-z0-9._-]+")
_REPETIDOS = re.compile(r"_{2,}")
_BASE_SEGURA = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]|[A-Za-z0-9]")


def sanear_nombre_base(nombre: str | None) -> str | None:
    """Stem del nombre que mando el cliente, reducido a `[A-Za-z0-9._-]`.

    Alimenta **solo** el `filename=` de la descarga: nunca una ruta. Los nombres
    en disco los sigue poniendo `EXTENSION` para la entrada y `NOMBRE_DE` para
    las salidas, que no dependen de esto.

    Devuelve `None` cuando no queda nada utilizable, y ahi el llamador cae al
    `studiocutter-{id}` de siempre. **El recorte de `._-` en los bordes no es
    cosmetico**: es lo que convierte `"..."` en `None` en vez de en un nombre
    que empieza con puntos.

    La whitelist es estricta a proposito: `mi dibujo.png` baja como
    `mi_dibujo.jpg` y `nino.heif` conserva solo lo ASCII. Se prefirio un saneo
    auditable de un vistazo antes que uno permisivo que hay que razonar.
    """
    if not nombre:
        return None
    ultimo = _SEPARADORES.split(nombre)[-1]
    stem = ultimo.rsplit(".", 1)[0] if "." in ultimo[1:] else ultimo
    limpio = _REPETIDOS.sub("_", _NO_PERMITIDO.sub("_", stem)).strip("._-")
    return limpio[:LARGO_MAX_NOMBRE].strip("._-") or None


def base_es_segura(base: str | None) -> bool:
    """Si un stem ya guardado sigue cumpliendo lo que `sanear_nombre_base` produce.

    Existe por el punto de consumo, no por el de entrada: el stem se sanea al
    subir y despues se HEREDA de un trabajo a otro sin volver a pasar por el
    saneo. Que todos los escritores se acuerden es una convencion; esto la
    convierte en algo que el consumidor impone.

    Es una validacion y **no** una segunda pasada de `sanear_nombre_base`, que
    no es idempotente: esa funcion descarta la extension, asi que aplicarla dos
    veces se come parte del nombre — `mi.archivo.png` daria `mi.archivo` y
    despues `mi`. Medido: 169 de 20000 entradas cambian en la segunda pasada.

    ⛔ **Esto es una frontera de seguridad, no una regla de estetica.** Nacio
    cosmetica —el `filename=` de `FileResponse` lo percent-encodea Starlette,
    asi que un nombre raro se veia mal y nada mas—, pero `descargar_todo` arma
    el `Content-Disposition` del ZIP **a mano** y esta whitelist es lo unico
    que impide meter un CRLF en un header de respuesta. Concretamente:

    - Que sea `fullmatch` y no `match` **es toda la defensa**. Con `match`,
      `a"b`, `a;b`, `a b`, `a\\rb` y `a\\nb` pasan todos: la regex ancla el
      arranque pero no el final.
    - La clase es un rango ASCII literal y no `\\w`, asi que `re.UNICODE` no la
      ensancha. Aflojarla para aceptar acentos abre la inyeccion.
    - Hay un cinturon mas, y es implicito: `requirements.txt` fija `uvicorn`
      **sin `[standard]`**, asi que el writer HTTP es `h11`, que valida los
      valores de header contra el ABNF y rechaza CR/LF. Instalar
      `uvicorn[standard]` cambia a `httptools`, que **no valida**, y deja esta
      funcion como unica defensa.

    Si hay que tocar la whitelist, mirar primero `routers/trabajos.py`.
    """
    return (
        base is not None
        and len(base) <= LARGO_MAX_NOMBRE
        and _BASE_SEGURA.fullmatch(base) is not None
    )


def nombre_de_descarga(id_: str, clave: ClaveArchivo, base: str | None = None) -> str:
    """Nombre que ve el usuario al bajar.

    Con el stem del archivo original —ya saneado— se respeta como se llamaba:
    `buddy.heif` baja como `buddy.jpg`. Sin el (un trabajo sin nombre, uno que
    saneo a nada, o uno que no pasa la validacion) cae al id, que es el
    comportamiento historico.
    """
    return f"{_raiz_del_nombre(id_, base)}{SUFIJO_DESCARGA[clave]}"


def nombre_de_zip(id_: str, base: str | None = None) -> str:
    """Nombre del ZIP de "descargar todo". Mismo criterio que las descargas sueltas."""
    return f"{_raiz_del_nombre(id_, base)}{SUFIJO_ZIP}"


def _raiz_del_nombre(id_: str, base: str | None) -> str:
    """Lo que va antes del sufijo, para las descargas sueltas y para el ZIP.

    Vive en un solo lugar y no copiado en los dos porque **es el criterio**, no
    un detalle de formato: el stem si pasa `base_es_segura`, y el fallback al id
    si no. Que el ZIP se llamara distinto que sus propios miembros seria el tipo
    de incoherencia que nadie mira hasta que la ve."""
    seguro = base if base_es_segura(base) else None
    return seguro or f"studiocutter-{id_[:8]}"


def claves_descargables(archivos: dict[str, str]) -> list[ClaveArchivo]:
    """Las claves del trabajo que el usuario puede bajar, en el orden del enum.

    Filtra dos cosas: lo que no esta en `ClaveArchivo` —imposible hoy, porque
    `_claves_conocidas` ya lo descarto en la frontera con el hijo, pero esto no
    depende de eso— y `CLAVES_INTERNAS`.

    El orden sale del enum y no del dict del trabajo: asi el ZIP lista siempre
    igual y dos corridas del mismo trabajo dan el mismo archivo.
    """
    presentes = set(archivos)
    return [c for c in ClaveArchivo if c.value in presentes and c not in CLAVES_INTERNAS]


# ── Subidas ──────────────────────────────────────────────────────────────────


def guardar_subida(
    origen: BinaryIO,
    destino_dir: Path,
    *,
    permitidos: frozenset[Formato],
    limite_bytes: int,
    nombre_cliente: str | None = None,
    destino_nombre: str | None = None,
) -> Subida:
    """Copia el archivo subido a `entrada.<ext>` validando contenido y tamaño.

    El tamaño se controla **mientras se copia** y no despues: si se pasa, lo
    escrito se borra y no queda basura en disco. El middleware de `main.py` ya
    rechaza por `Content-Length` antes de llegar aca; esto cubre el caso de un
    cliente que miente en el header.

    `destino_nombre` pisa el `entrada.<ext>` cuando lo que entra no es el arte
    del usuario sino una salida que produjo nuestro propio front —hoy, la foto
    del cortante, que va a `vista.jpg`—. **Sigue sin venir del cliente**: el
    llamador lo saca de `NOMBRE_DE`, igual que las descargas. Con `None` el
    comportamiento es el de siempre.
    """
    cabecera = origen.read(TROZO)
    formato = detectar_formato(cabecera)
    if formato is None or formato not in permitidos:
        detectado = formato.value if formato else nombrar_no_soportado(cabecera)
        que = "Ese tipo de archivo" if detectado == "desconocido" else f"Un archivo {detectado}"
        aceptados = ", ".join(sorted(f.value for f in permitidos))
        raise ErrorApi(
            "formato_no_soportado",
            f"{que} no se puede usar en esta pantalla. Se aceptan: {aceptados}.",
            estado=415,
            detalle={"detectado": detectado},
        )

    # El nombre pedido tiene que corresponderse con lo que los BYTES dijeron
    # que es. Sin esto nada ata `destino_nombre` a `permitidos`: un llamador
    # podia guardar un JPEG como `salida.png` y despues `MEDIO_DE` mentia en el
    # `content-type` de la descarga. Es justo el tipo de desfasaje que mypy no
    # ve, porque los dos lados son strings.
    if destino_nombre is not None and not destino_nombre.endswith(EXTENSION[formato]):
        raise ErrorApi(
            "formato_no_soportado",
            f"Ese archivo no se puede guardar como {destino_nombre}.",
            estado=415,
            detalle={"detectado": formato.value},
        )
    # Y que sea un NOMBRE, no una ruta. `Path("/dir") / "/etc/x.jpg"` descarta
    # la izquierda y devuelve la absoluta, y `"../x.jpg"` sale del directorio:
    # las dos cosas pasan el chequeo de sufijo de arriba sin despeinarse. Hoy el
    # unico llamador pasa una constante de `NOMBRE_DE`, pero eso es una
    # convencion del llamador y esto la convierte en algo que el consumidor
    # impone — el mismo criterio que `base_es_segura` aplica al nombre de
    # descarga, y por el mismo motivo.
    if destino_nombre is not None and Path(destino_nombre).name != destino_nombre:
        raise ErrorApi("interno", "No se pudo guardar el archivo.", estado=500)

    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / (destino_nombre or f"entrada{EXTENSION[formato]}")

    # ⚠ Se escribe a un temporal y se renombra al final, en vez de abrir el
    # destino en `"wb"`. Con `entrada.<ext>` daba igual —el destino era un
    # archivo nuevo en un directorio nuevo—, pero desde que `destino_nombre`
    # existe el destino puede ser un archivo VIVO (`vista.jpg`), y truncarlo
    # antes de saber si la subida entra deja tres estados que antes no
    # existian: una segunda subida demasiado grande destruia la foto valida
    # anterior, un `OSError` a mitad de copia dejaba una truncada que se sirve
    # como completa, y dos subidas simultaneas intercalaban sus bytes.
    #
    # Es el mismo patron —y por el mismo motivo— que `escribir_estado` unas
    # lineas mas abajo: `os.replace` es atomico dentro del mismo filesystem, y
    # el temporal vive en el mismo directorio para garantizarlo.
    fd, temporal_txt = tempfile.mkstemp(dir=destino_dir, prefix=".subida-", suffix=".tmp")
    temporal = Path(temporal_txt)
    escritos = 0
    try:
        with os.fdopen(fd, "wb") as salida:
            trozo = cabecera
            while trozo:
                escritos += len(trozo)
                if escritos > limite_bytes:
                    tope = limite_bytes // (1024 * 1024)
                    raise ErrorApi(
                        "archivo_muy_grande",
                        f"El archivo supera el limite de {tope} MB.",
                        estado=413,
                    )
                salida.write(trozo)
                trozo = origen.read(TROZO)
        os.replace(temporal, destino)
    except BaseException:
        # `BaseException` y no `ErrorApi`: un `OSError` por disco lleno tiene
        # que limpiar igual, y antes se escapaba dejando basura.
        temporal.unlink(missing_ok=True)
        raise

    return Subida(
        ruta=destino,
        formato=formato,
        bytes_escritos=escritos,
        nombre_base=sanear_nombre_base(nombre_cliente),
    )


# ── Frontera con el proceso hijo ─────────────────────────────────────────────


def escribir_estado(dir_trabajo: Path, datos: dict[str, Any]) -> None:
    """Escribe `estado.json` de forma atomica. Lo llama el proceso HIJO.

    Atomico de verdad —temporal en el mismo directorio y despues `os.replace`—
    porque el padre puede estar leyendolo justo en el medio. Un JSON a medio
    escribir se leeria como trabajo fallido.
    """
    dir_trabajo.mkdir(parents=True, exist_ok=True)
    fd, temporal = tempfile.mkstemp(dir=dir_trabajo, prefix=".estado-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False)
        os.replace(temporal, dir_trabajo / NOMBRE_ESTADO)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise


def leer_estado(dir_trabajo: Path) -> dict[str, Any] | None:
    """Lee `estado.json`. Lo llama el proceso PADRE.

    **Su ausencia significa fallo, no "todavia no".** El hijo lo escribe
    siempre, termine bien o mal; si no esta, el hijo murio antes de poder
    escribirlo (lo mato el timeout, o el sistema operativo).
    """
    ruta = dir_trabajo / NOMBRE_ESTADO
    if not ruta.is_file():
        return None
    try:
        datos: Any = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return datos if isinstance(datos, dict) else None


# ── Limpieza ─────────────────────────────────────────────────────────────────


def limpiar_vencidos(a: Ajustes, almacen: AlmacenTrabajos) -> int:
    """Borra los trabajos pasados de TTL, en memoria y en disco."""
    borrados = 0
    for trabajo in almacen.vencidos(a.ttl_trabajo_s):
        shutil.rmtree(a.dir_trabajo / trabajo.id, ignore_errors=True)
        almacen.eliminar(trabajo.id)
        borrados += 1
    return borrados + _limpiar_huerfanos(a, almacen)


def _limpiar_huerfanos(a: Ajustes, almacen: AlmacenTrabajos) -> int:
    """Directorios en disco sin trabajo en memoria: quedaron de un reinicio.

    El almacen es volatil y el disco no; sin esto, cada reinicio de uvicorn
    dejaria carpetas para siempre.
    """
    if not a.dir_trabajo.is_dir():
        return 0
    vivos = {t.id for t in almacen.vencidos(0)}
    corte = time.time() - a.ttl_trabajo_s
    borrados = 0
    for hijo in a.dir_trabajo.iterdir():
        if not hijo.is_dir() or hijo.name in vivos:
            continue
        try:
            uuid.UUID(hijo.name)
        except ValueError:
            continue  # algo que no creamos nosotros: no lo tocamos
        if hijo.stat().st_mtime < corte:
            shutil.rmtree(hijo, ignore_errors=True)
            borrados += 1
    return borrados


# ── Encadenado entre pantallas ───────────────────────────────────────────────


def copiar_desde_trabajo(
    a: Ajustes,
    almacen: AlmacenTrabajos,
    *,
    origen_id: str,
    usuario: str,
    destino_dir: Path,
    permitidos: frozenset[Formato],
) -> Subida:
    """Trae la salida de un trabajo anterior como entrada de uno nuevo.

    Es lo que hace andar el boton de seguir a la pantalla siguiente. Pasa por
    las mismas validaciones que una subida —contenido y propietario— aunque el
    archivo lo haya producido la propia app: el id del origen viene del
    cliente, asi que no es mas confiable que un upload.
    """
    origen = almacen.obtener(validar_id(origen_id), usuario)
    if origen is None:
        raise ErrorApi("trabajo_inexistente", "No existe ese trabajo.", estado=404)

    dir_origen = dir_de_trabajo(a, origen_id)
    for clave_txt in origen.archivos:
        clave = ClaveArchivo(clave_txt)
        if clave in CLAVES_NO_ENCADENABLES:
            continue
        ruta = dir_origen / NOMBRE_DE[clave]
        if not ruta.is_file():
            continue
        with ruta.open("rb") as f:
            if detectar_formato(f.read(TROZO)) in permitidos:
                f.seek(0)
                subida = guardar_subida(
                    f, destino_dir, permitidos=permitidos, limite_bytes=a.tamano_maximo_bytes
                )
                # El nombre viaja por la cadena: si el conversor arranco con
                # `buddy.heif`, el cortante tres pantallas despues sigue
                # bajando `buddy.3mf`. Sale del trabajo de origen, que es del
                # mismo usuario y ya lo tenia saneado — no se vuelve a sanear.
                return replace(subida, nombre_base=origen.nombre_base)

    raise ErrorApi(
        "encadenado_incompatible",
        "El trabajo anterior no dejo ningun archivo que esta pantalla pueda usar.",
        estado=409,
        detalle={"origen": origen_id},
    )


def resolver_entrada(
    a: Ajustes,
    almacen: AlmacenTrabajos,
    *,
    flujo: BinaryIO | None,
    origen_id: str | None,
    usuario: str,
    destino_dir: Path,
    permitidos: frozenset[Formato],
    nombre_cliente: str | None = None,
) -> Subida:
    """Una subida directa o la salida del trabajo anterior. Nunca las dos.

    Cada pantalla acepta las dos entradas —esa es la idea del encadenado con
    puerta libre— y las dos terminan en el mismo `entrada.<ext>` validado.

    `nombre_cliente` solo aplica a la subida directa: cuando la entrada viene
    encadenada, el nombre lo hereda `copiar_desde_trabajo` del trabajo anterior.
    """
    if flujo is not None:
        return guardar_subida(
            flujo,
            destino_dir,
            permitidos=permitidos,
            limite_bytes=a.tamano_maximo_bytes,
            nombre_cliente=nombre_cliente,
        )
    if origen_id:
        return copiar_desde_trabajo(
            a,
            almacen,
            origen_id=origen_id,
            usuario=usuario,
            destino_dir=destino_dir,
            permitidos=permitidos,
        )
    raise ErrorApi(
        "falta_archivo",
        "Hay que subir un archivo o venir de una pantalla anterior.",
        estado=422,
    )
