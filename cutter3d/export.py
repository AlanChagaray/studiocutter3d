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
    rutas_3mf_objeto: tuple[Path, ...] = ()
    """Un `.3mf` por objeto, ademas del que los trae a los dos.

    El combinado sigue siendo el archivo bueno —los dos cuerpos en su posicion
    anidada real, que es lo que se imprime—, pero separados dan libertad de
    editar uno sin el otro. Vacio cuando hay un solo objeto: ahi el separado
    seria una copia del combinado y ofrecer dos descargas identicas confunde.
    """


OBJETOS = (NOMBRE_MARCADOR, NOMBRE_CORTADOR)
"""Los cuerpos de la escena, en el orden en que se exportan de a uno."""


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


def exportar_stl_unico(malla: trimesh.Trimesh, destino: Path) -> Path:
    """Un STL con UN cuerpo, al archivo que se pida.

    Es la pieza chica que comparten los dos productores de STL del proyecto: el
    motor, que escribe uno por objeto (`exportar_stl`), y el conversor de mallas
    (`cutter3d.malla.convertir`), que escribe uno solo porque el formato no sabe
    contener mas. Lo unico que aporta sobre `malla.export` es el temporal y el
    rename, que es justo lo que no conviene reimplementar dos veces.
    """
    datos = malla.export(file_type="stl")
    # `Trimesh.export` esta tipado como `dict | bytes | str` porque su retorno
    # depende del `file_type`, y el binario de STL siempre es `bytes`. El chequeo
    # es lo que se lo dice a mypy y, de paso, convierte esa promesa en algo que
    # falla ruidoso si alguna version de trimesh la rompe.
    if not isinstance(datos, bytes):  # pragma: no cover — trimesh devuelve bytes
        raise Cutter3DError(f"{destino}: trimesh no devolvio bytes para el STL")
    return _escribir_atomico(datos, destino)


def exportar_stl(escena: trimesh.Scene, destino_3mf: Path) -> tuple[Path, ...]:
    """Un STL por objeto, nombrado `<base>_<objeto>.stl`."""
    rutas: list[Path] = []
    for nombre in OBJETOS:
        malla = escena.geometry.get(nombre)
        if malla is None:
            continue
        ruta = destino_3mf.with_name(f"{destino_3mf.stem}_{nombre}.stl")
        rutas.append(exportar_stl_unico(malla, ruta))
    return tuple(rutas)


def exportar_3mf_por_objeto(escena: trimesh.Scene, destino_3mf: Path) -> tuple[Path, ...]:
    """Un `.3mf` por objeto, nombrado `<base>_<objeto>.3mf`.

    Cada uno es una `Scene` de un solo cuerpo, asi que conserva lo que el 3MF
    aporta sobre el STL: el nombre del objeto y los milimetros. **Las
    coordenadas no se tocan** — el cuerpo queda donde estaba en el conjunto, y
    abrir los dos archivos en el slicer los reencuentra exactamente anidados.

    Con un solo objeto devuelve vacio: seria una copia del combinado.
    """
    presentes = [n for n in OBJETOS if n in escena.geometry]
    if len(presentes) < len(OBJETOS):
        return ()
    rutas: list[Path] = []
    for nombre in presentes:
        sola = trimesh.Scene({nombre: escena.geometry[nombre]})
        datos = sola.export(file_type="3mf")
        ruta = destino_3mf.with_name(f"{destino_3mf.stem}_{nombre}.3mf")
        rutas.append(_escribir_atomico(bytes(datos), ruta))
    return tuple(rutas)


def exportar(escena: trimesh.Scene, destino_3mf: Path, con_stl: bool = False) -> Salidas:
    """Exporta el .3mf, el .glb del preview, los .3mf sueltos y, si se pide, los .stl."""
    ruta_3mf = exportar_3mf(escena, destino_3mf)
    ruta_glb = exportar_glb(escena, destino_3mf.with_suffix(".glb"))
    rutas_stl = exportar_stl(escena, destino_3mf) if con_stl else ()
    return Salidas(
        ruta_3mf=ruta_3mf,
        ruta_glb=ruta_glb,
        rutas_stl=rutas_stl,
        rutas_3mf_objeto=exportar_3mf_por_objeto(escena, destino_3mf),
    )


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
