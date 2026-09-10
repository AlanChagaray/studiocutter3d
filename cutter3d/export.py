"""Serializacion: .3mf, .glb y .stl.

- **`.3mf`** es el formato bueno y el que pide el contrato: milimetros, dos
  objetos **nombrados** (`marcador` y `cortador`), los dos apoyados en z=0 y en
  su posicion anidada real. El exporter de trimesh escribe el nombre de cada
  geometria de la `Scene` en el atributo `name` de su `<object>`.
- **`.glb`** es el mismo `Scene` con los materiales PBR, para el preview 3D. Sale
  de la misma escena, asi que la geometria que se ve es exactamente la que se
  descarga.
- **`.stl`** son **dos archivos separados**, uno por objeto. STL no tiene nombres
  de objeto ni unidades: metidos en un mismo archivo, el slicer los ve como un
  unico cuerpo de dos cascaras y no se pueden mover por separado.

Todo se escribe a un temporal y se renombra al final. Si algo falla a mitad de
camino no queda un archivo a medias que parezca valido.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import trimesh

from .errors import Cutter3DError
from .solids import NOMBRE_CORTADOR, NOMBRE_MARCADOR


@dataclass(frozen=True)
class Salidas:
    ruta_3mf: Path
    ruta_glb: Path
    rutas_stl: tuple[Path, ...]


def _escribir_atomico(datos: bytes, destino: Path) -> Path:
    """Escribe a un temporal y renombra. Si algo falla, no queda archivo a medias.

    El temporal se crea con `mkstemp` en el MISMO directorio del destino (que es
    lo que `os.replace` exige para ser atomico) y con nombre impredecible. Un
    nombre fijo como `<destino>.tmp` tiene dos problemas: colisiona entre
    procesos concurrentes — y en el ciclo 2 el motor va a correr en un pool —, y
    en un directorio con permiso de escritura ajeno es un blanco previsible
    (CWE-377). `mkstemp` ademas crea el archivo con permisos restrictivos.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(
        dir=destino.parent, prefix=f".{destino.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(datos)
        os.replace(temporal, destino)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise
    return destino


def exportar_3mf(escena: trimesh.Scene, destino: Path) -> Path:
    datos = escena.export(file_type="3mf")
    return _escribir_atomico(bytes(datos), destino)


def exportar_glb(escena: trimesh.Scene, destino: Path) -> Path:
    datos = escena.export(file_type="glb")
    return _escribir_atomico(bytes(datos), destino)


def exportar_stl(escena: trimesh.Scene, destino_3mf: Path) -> tuple[Path, ...]:
    """Un STL por objeto, nombrado `<base>_<objeto>.stl`."""
    rutas: list[Path] = []
    for nombre in (NOMBRE_MARCADOR, NOMBRE_CORTADOR):
        malla = escena.geometry.get(nombre)
        if malla is None:
            continue
        datos = malla.export(file_type="stl")
        ruta = destino_3mf.with_name(f"{destino_3mf.stem}_{nombre}.stl")
        rutas.append(_escribir_atomico(bytes(datos), ruta))
    return tuple(rutas)


def exportar(escena: trimesh.Scene, destino_3mf: Path, con_stl: bool = False) -> Salidas:
    """Exporta el .3mf, el .glb del preview y, si se pide, los .stl."""
    ruta_3mf = exportar_3mf(escena, destino_3mf)
    ruta_glb = exportar_glb(escena, destino_3mf.with_suffix(".glb"))
    rutas_stl = exportar_stl(escena, destino_3mf) if con_stl else ()
    return Salidas(ruta_3mf=ruta_3mf, ruta_glb=ruta_glb, rutas_stl=rutas_stl)


def releer_3mf(ruta: Path) -> dict[str, trimesh.Trimesh]:
    """Relee el .3mf **del disco**.

    La verificacion de topologia se hace siempre sobre esto y nunca sobre la
    malla en memoria: es la unica forma de probar que lo que se entrega es lo que
    se valido.
    """
    escena = trimesh.load(str(ruta), file_type="3mf", force="scene")
    if not isinstance(escena, trimesh.Scene):
        raise Cutter3DError(
            f"{ruta}: el 3MF no se pudo leer como escena (trimesh devolvio {type(escena).__name__})"
        )
    return {str(nombre): malla for nombre, malla in escena.geometry.items()}
