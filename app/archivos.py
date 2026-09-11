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
from .config import Ajustes
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
}


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
}


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
    seguro = base if base_es_segura(base) else None
    return f"{seguro or f'studiocutter-{id_[:8]}'}{SUFIJO_DESCARGA[clave]}"


# ── Subidas ──────────────────────────────────────────────────────────────────


def guardar_subida(
    origen: BinaryIO,
    destino_dir: Path,
    *,
    permitidos: frozenset[Formato],
    limite_bytes: int,
    nombre_cliente: str | None = None,
) -> Subida:
    """Copia el archivo subido a `entrada.<ext>` validando contenido y tamaño.

    El tamaño se controla **mientras se copia** y no despues: si se pasa, lo
    escrito se borra y no queda basura en disco. El middleware de `main.py` ya
    rechaza por `Content-Length` antes de llegar aca; esto cubre el caso de un
    cliente que miente en el header.
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

    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"entrada{EXTENSION[formato]}"
    escritos = 0
    try:
        with destino.open("wb") as salida:
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
    except ErrorApi:
        destino.unlink(missing_ok=True)
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
