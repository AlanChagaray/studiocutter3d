"""Directorios de trabajo, subidas y descargas.

**El cliente nunca nombra un archivo.** Ni el de entrada ni el de salida:

- Lo que sube se guarda como `entrada.<ext>`, donde la extension sale de
  **mirar los bytes**, no el nombre ni el `Content-Type` (los dos los elige
  quien sube). Un adjunto llamado `../../.ssh/authorized_keys` termina en
  `entrada.png` adentro del directorio del trabajo, y no hay nada que
  sanitizar porque el nombre original directamente no se usa.
- Lo que baja se pide por una **clave de un enum cerrado**, que este modulo
  traduce a un nombre fijo.

El directorio es `trabajo/<uuid4>/`. Que el id sea un UUID no es cosmetico:
`uuid.UUID(id_)` rechaza de plano cualquier cosa con puntos suspensivos o
separadores de ruta, asi que la validacion del id **es** la defensa contra el
path traversal. El chequeo de contencion posterior es el segundo cinturon.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
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


EXTENSION: dict[Formato, str] = {
    Formato.PNG: ".png",
    Formato.JPEG: ".jpg",
    Formato.WEBP: ".webp",
    Formato.SVG: ".svg",
}

#: Que acepta cada pantalla. F2 pide JPG si o si — es requisito del producto,
#: no una limitacion tecnica: la correccion de lineas trabaja sobre un raster.
FORMATOS_CONVERSOR = frozenset({Formato.PNG, Formato.JPEG, Formato.WEBP, Formato.SVG})
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


# ── Deteccion por contenido ──────────────────────────────────────────────────


def detectar_formato(cabecera: bytes) -> Formato | None:
    """Formato real segun los bytes. None si no es ninguno de los soportados."""
    if cabecera.startswith(b"\x89PNG\r\n\x1a\n"):
        return Formato.PNG
    if cabecera.startswith(b"\xff\xd8\xff"):
        # JFIF es JPEG: mismo magic, distinta extension. Por eso se detecta por
        # contenido y el ".jfif" del nombre no importa.
        return Formato.JPEG
    if cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WEBP":
        return Formato.WEBP
    if _parece_svg(cabecera):
        return Formato.SVG
    return None


def _parece_svg(cabecera: bytes) -> bool:
    texto = cabecera.lstrip(b"\xef\xbb\xbf").lstrip()[:512].lower()
    return texto.startswith(b"<?xml") or texto.startswith(b"<svg") or b"<svg" in texto


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


def nombre_de_descarga(id_: str, clave: ClaveArchivo) -> str:
    """Nombre que ve el usuario al bajar. Lo arma el servidor, no el cliente."""
    return f"studiocutter-{id_[:8]}{SUFIJO_DESCARGA[clave]}"


# ── Subidas ──────────────────────────────────────────────────────────────────


def guardar_subida(
    origen: BinaryIO,
    destino_dir: Path,
    *,
    permitidos: frozenset[Formato],
    limite_bytes: int,
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
        aceptados = ", ".join(sorted(f.value for f in permitidos))
        raise ErrorApi(
            "formato_no_soportado",
            f"Ese tipo de archivo no se puede usar en esta pantalla. Se aceptan: {aceptados}.",
            estado=415,
            detalle={"detectado": formato.value if formato else "desconocido"},
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

    return Subida(ruta=destino, formato=formato, bytes_escritos=escritos)


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
                return guardar_subida(
                    f, destino_dir, permitidos=permitidos, limite_bytes=a.tamano_maximo_bytes
                )

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
) -> Subida:
    """Una subida directa o la salida del trabajo anterior. Nunca las dos.

    Cada pantalla acepta las dos entradas —esa es la idea del encadenado con
    puerta libre— y las dos terminan en el mismo `entrada.<ext>` validado.
    """
    if flujo is not None:
        return guardar_subida(
            flujo, destino_dir, permitidos=permitidos, limite_bytes=a.tamano_maximo_bytes
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
