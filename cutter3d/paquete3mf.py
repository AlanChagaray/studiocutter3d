"""Confirmacion estructural de un 3MF: que sea un 3MF, y que no sea una bomba.

Vive en su propio modulo, separado de `malla.py`, por una razon de **costo de
import**: `malla.py` importa trimesh, y este chequeo lo necesita el router de la
capa web para poder rechazar un docx con un 415 limpio **sin gastar un proceso
hijo**. Importar trimesh en el proceso de uvicorn es exactamente lo que el ciclo
6 saco de encima (1200 modulos, ~89 MB), asi que lo que la web necesita tocar
tiene que costar lo que cuesta `zipfile`.

`cutter3d/__init__.py` ya es perezoso, asi que `from cutter3d.paquete3mf import
confirmar_3mf` arrastra `errors` y `params` —stdlib pura— y nada mas.

## Por que hace falta un paso aparte

La deteccion por magic bytes de `app/archivos.py` solo ve los primeros 64 KB, y
la firma de un 3MF es `PK\\x03\\x04`, o sea la de cualquier ZIP: docx, xlsx, jar y
epub la comparten. Lo que distingue a un 3MF vive en el **central directory**,
que esta al final del archivo. Por eso `Formato.TRES_MF` significa "zip que
podria ser un 3MF" y esta funcion es la que lo confirma de verdad.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .errors import MallaIlegible

MAX_ENTRADAS_ZIP = 64
"""Archivos dentro del 3MF. Uno real trae menos de diez; miles es un ataque."""

MAX_DESCOMPRIMIDO = 120 * 1024 * 1024
"""Suma de los tamaños descomprimidos declarados en el central directory.

Es la defensa contra el zip bomb y se lee del **indice**: no descomprime nada.
El tamaño declarado puede mentir —nada obliga a un ZIP a decir la verdad—, pero
mentir hacia abajo solo sirve para pasar este chequeo y caer en el siguiente, que
cuenta triangulos ya materializados (`malla.MAX_TRIANGULOS`) con el techo de RAM
del proceso hijo encima.

Esta calibrado contra ese techo de triangulos a proposito: un triangulo de 3MF
ocupa del orden de 70 bytes de XML entre su declaracion y su parte de la lista de
vertices, asi que 120 MB descomprimidos son mas o menos el mismo millon de
triangulos. Mover uno sin mirar el otro deja un agujero."""

SUFIJO_MODELO = ".model"
"""La pieza que ningun otro ZIP tiene. El 3MF la pone en `3D/3dmodel.model`, pero
el nombre exacto lo declara `[Content_Types].xml` y algunos productores lo
cambian, asi que se busca por sufijo y no por ruta literal."""


def confirmar_3mf(ruta: Path) -> None:
    """Levanta `MallaIlegible` si el ZIP no es un 3MF usable. No devuelve nada.

    Es barata a proposito: abre el indice, cuenta entradas y suma tamaños
    declarados. Nada de esto descomprime un solo byte.
    """
    try:
        with zipfile.ZipFile(ruta) as paquete:
            entradas = paquete.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise MallaIlegible(str(ruta), "el archivo no es un ZIP legible") from exc

    if len(entradas) > MAX_ENTRADAS_ZIP:
        raise MallaIlegible(
            str(ruta),
            f"el archivo trae {len(entradas)} entradas adentro y el maximo es {MAX_ENTRADAS_ZIP}",
        )

    total = sum(entrada.file_size for entrada in entradas)
    if total > MAX_DESCOMPRIMIDO:
        raise MallaIlegible(
            str(ruta),
            f"el archivo declara {total // (1024 * 1024)} MB descomprimidos y el maximo es "
            f"{MAX_DESCOMPRIMIDO // (1024 * 1024)} MB",
        )

    if not any(entrada.filename.lower().endswith(SUFIJO_MODELO) for entrada in entradas):
        raise MallaIlegible(str(ruta), "es un ZIP pero no un 3MF: no trae ningun modelo 3D adentro")
