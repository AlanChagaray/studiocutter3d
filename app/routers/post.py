"""F4 — las fotos de cortantes que ya existen, de a uno o de a veinticinco.

La foto cenital del cortante (`vista.jpg`) es un subproducto de generarlo: la
rinde el navegador con `preview3d.js` sobre el `.glb` que dejo el motor, y si no
se bajo en ese momento se va con el trabajo a las 6 h. Los cortantes anteriores a
la vista imagen directamente no tienen ninguna.

Esta pantalla cierra eso: entran mallas ya construidas y sale **la misma foto**.
La misma de verdad, no una parecida — el `.glb` se arma con el acabado de
`cutter3d.solids` y lo fotografia el mismo modulo del front, con el mismo estudio
de luces, el mismo encuadre y las mismas paletas. Ver `cutter3d/malla.py`.

## Diseños, no archivos

**Un diseño puede venir en uno o en dos archivos.** El motor exporta el cortante
y su marcador juntos en un `.3mf` y tambien sueltos
(`<base>_cortador.stl` + `<base>_marcador.stl`), y las dos formas describen la
misma pieza: fotografiar el cortador sin su marcador seria fotografiar otra cosa.

Quien arma los grupos es **el navegador**, que es el unico que ve los nombres
originales: empareja por el sufijo que escribe el motor y deja corregirlo a mano,
porque un archivo renombrado, o dos piezas que no siguen la convencion, no se
pueden emparejar solos. Aca llega ya resuelto, en `agrupacion`, y lo unico que se
hace es **validarlo**: indices dentro del lote, cada archivo en un solo grupo,
roles de un conjunto cerrado y los topes.

Esa division no es comodidad: el servidor no mira ni un nombre de archivo del
cliente para decidir nada, que es la regla de `app/archivos.py`. Lo que llega es
una lista de enteros y de roles.

## En serie, nunca en paralelo

Los diseños se convierten **de a uno** (`trabajos.lanzar_serie`): un proceso por
diseño, el siguiente arranca cuando termina el anterior. No hace falta
paralelismo y si hace falta no saturar el servidor — cada hijo carga trimesh
entero y el techo de RAM es por proceso.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from cutter3d.errors import MallaIlegible

# ⚠ `paquete3mf` y no `cutter3d.malla`: el segundo importa trimesh, y meter
# trimesh en el proceso de uvicorn revierte el ciclo 6 (1200 modulos, ~89 MB en
# un proceso que no construye un solo poligono). Este modulo cuesta `zipfile`.
from cutter3d.paquete3mf import confirmar_3mf
from cutter3d.params import MAX_ARCHIVOS_POR_DISENO

from ..almacen import TipoTrabajo
from ..archivos import (
    FORMATOS_POST,
    LIMITE_MALLA_BYTES,
    MAX_DISENOS,
    ClaveDiseno,
    Formato,
    dir_de_trabajo,
    guardar_subida,
    nombre_de_diseno,
    prefijo_de_entrada,
    sanear_nombre_base,
)
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import ErrorApi, traducir
from ..tareas import ejecutar_post
from ..trabajos import lanzar_serie

# Las paletas son las de F3, importadas y no copiadas: las dos pantallas tienen
# que ofrecer exactamente los mismos colores o la normalizacion de los posts se
# rompe justo donde tiene que funcionar. Un segundo listado seria una segunda
# verdad sobre el mismo set de filamentos.
from .cortante import COLORES, COLORES_FONDO

__all__ = ["COLORES", "COLORES_FONDO", "router"]

router = APIRouter(prefix="/api/post", tags=["post"])

ROLES = ("cortador", "marcador", "unico")
"""Los mismos de `cutter3d.malla.ROLES`. Se repiten aca —y hay un test que lo
exige— para no importar `malla` y con el trimesh: ver la nota del import."""

_AGRUPACION = re.compile(r"\A\d+:[a-z]+(,\d+:[a-z]+)*(;\d+:[a-z]+(,\d+:[a-z]+)*)*\Z")
"""`0:cortador,1:marcador;2:unico` — grupos por `;`, archivos por `,`.

Es una forma compacta y no un JSON a proposito: lo unico que viaja son enteros y
palabras de una lista cerrada, y una gramatica que entra en una linea se puede
validar de un vistazo. Un JSON aca seria una superficie de parseo mas grande
para expresar exactamente lo mismo."""


def _parsear(agrupacion: str, cantidad: int) -> list[list[tuple[int, str]]]:
    """`agrupacion` a grupos de (indice de archivo, rol). Valida todo o falla.

    Lo que se exige, y por que cada cosa:

    - **Forma.** Un regex, antes de tocar nada.
    - **Cada archivo en exactamente un grupo.** Repetir uno lo fotografiaria dos
      veces; omitirlo dejaria un archivo subido que no entra en ningun diseño y
      el usuario nunca sabria por que falta.
    - **Roles de la lista cerrada**, y a lo sumo `MAX_ARCHIVOS_POR_DISENO` por
      grupo.
    - **Un grupo de dos tiene que ser cortador + marcador.** Dos cortadores no
      son un diseño: son dos diseños que alguien agrupo por error, y superponer
      dos cortantes distintos da una foto que no es de ninguno de los dos.
    """
    if not _AGRUPACION.fullmatch(agrupacion):
        raise ErrorApi(
            "agrupacion_invalida", "No se entendio como agrupar los archivos.", estado=422
        )

    grupos: list[list[tuple[int, str]]] = []
    for crudo in agrupacion.split(";"):
        grupo: list[tuple[int, str]] = []
        for parte in crudo.split(","):
            indice_txt, rol = parte.split(":", 1)
            grupo.append((int(indice_txt), rol))
        grupos.append(grupo)

    if len(grupos) > MAX_DISENOS:
        raise ErrorApi(
            "demasiados_disenos",
            f"Se pueden fotografiar hasta {MAX_DISENOS} diseños por vez.",
            estado=422,
            detalle={"maximo": MAX_DISENOS, "recibidos": len(grupos)},
        )

    vistos: set[int] = set()
    for grupo in grupos:
        _validar_grupo(grupo, cantidad, vistos)

    if len(vistos) != cantidad:
        raise ErrorApi(
            "agrupacion_invalida",
            "Quedaron archivos sin asignar a ningun diseño.",
            estado=422,
            detalle={"archivos": cantidad, "asignados": len(vistos)},
        )
    return grupos


def _invalida(motivo: str) -> ErrorApi:
    return ErrorApi("agrupacion_invalida", motivo, estado=422)


def _validar_grupo(grupo: list[tuple[int, str]], cantidad: int, vistos: set[int]) -> None:
    """Las reglas de UN diseño. Muta `vistos`, que es como se detecta el repetido."""
    if len(grupo) > MAX_ARCHIVOS_POR_DISENO:
        raise _invalida(f"Un diseño puede tener hasta {MAX_ARCHIVOS_POR_DISENO} archivos.")
    for indice, rol in grupo:
        if rol not in ROLES:
            raise _invalida("Hay un rol desconocido.")
        if not 0 <= indice < cantidad:
            raise _invalida("La agrupacion nombra un archivo que no llego.")
        if indice in vistos:
            raise _invalida("Un archivo no puede estar en dos diseños.")
        vistos.add(indice)
    if len(grupo) == MAX_ARCHIVOS_POR_DISENO and sorted(r for _, r in grupo) != [
        "cortador",
        "marcador",
    ]:
        raise _invalida("Un diseño de dos archivos tiene que ser un cortador y su marcador.")


def _por_defecto(cantidad: int) -> str:
    """Sin `agrupacion`, cada archivo es su propio diseño.

    Es el default honesto: el servidor no ve nombres, asi que no puede emparejar
    nada por su cuenta. Emparejar es del navegador, y si no lo dijo es porque no
    hay nada que emparejar.
    """
    return ";".join(f"{i}:unico" for i in range(cantidad))


@router.post("")
def fotografiar(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    *,
    archivo: Annotated[list[UploadFile], File()],
    agrupacion: Annotated[str | None, Form()] = None,
    nombres: Annotated[str | None, Form()] = None,
) -> dict[str, object]:
    """Recibe las mallas, lanza la conversion en serie y devuelve el trabajo.

    Los archivos se guardan como `entrada-<diseño><a|b>.<ext>`: el cliente sigue
    sin nombrar nada en disco, y el numero del diseño sale de la agrupacion ya
    validada, no del nombre que subio.

    `nombres` es como se va a LLAMAR la descarga de cada diseño, separados por
    `;`. Lo manda el navegador porque es el unico que sabe cual es el nombre del
    diseño: en un par, `kitty-bruja_cortador.stl` y `kitty-bruja_marcador.stl`
    son un solo diseño que se llama `kitty-bruja`, y eso no se puede deducir de
    un archivo solo. Cada uno pasa por `sanear_nombre_base` igual que cualquier
    nombre de cliente, y **nunca toca una ruta**: el archivo en disco ya se llama
    `entrada-NNx.<ext>`.
    """
    if not archivo:
        raise ErrorApi("falta_archivo", "Hay que subir al menos un archivo.", estado=422)
    grupos = _parsear(agrupacion or _por_defecto(len(archivo)), len(archivo))

    trabajo = almacen.crear(usuario, TipoTrabajo.POST)
    destino = dir_de_trabajo(a, trabajo.id, crear=True)

    # `nombres` viene como `a;b;c`. Se parte antes de crear nada para que un
    # valor absurdo falle temprano; que sobren o falten no es error, se completa
    # con el nombre del archivo.
    pedidos = (nombres or "").split(";") if nombres else []

    pasos: list[tuple[object, ...]] = []
    etiquetas: list[str | None] = []
    for numero, grupo in enumerate(grupos, start=1):
        rutas: list[str] = []
        roles: list[str] = []
        for orden, (indice, rol) in enumerate(grupo):
            subida = guardar_subida(
                archivo[indice].file,
                destino,
                permitidos=FORMATOS_POST,
                limite_bytes=LIMITE_MALLA_BYTES,
                prefijo=prefijo_de_entrada(numero, orden),
            )
            # La firma `PK\x03\x04` la comparten docx, xlsx, jar y epub: lo que
            # confirma que es un 3MF esta en el central directory, al final del
            # archivo, y la deteccion por bytes solo ve los primeros 64 KB. Se
            # chequea ACA para que un zip cualquiera se vaya con un 422 inmediato
            # en vez de gastar un proceso hijo.
            if subida.formato is Formato.TRES_MF:
                try:
                    confirmar_3mf(subida.ruta)
                except MallaIlegible as exc:
                    raise traducir(exc) from exc
            rutas.append(str(subida.ruta))
            roles.append(rol)
        # Lo que el navegador dijo que se llama este diseño; si no dijo nada,
        # el nombre del primer archivo del grupo. Las dos puntas pasan por el
        # mismo saneo y ninguna toca una ruta.
        propuesto = pedidos[numero - 1] if numero - 1 < len(pedidos) else None
        etiquetas.append(sanear_nombre_base(propuesto or archivo[grupo[0][0]].filename or ""))
        pasos.append(
            (
                str(destino),
                numero,
                tuple(rutas),
                tuple(roles),
                nombre_de_diseno(ClaveDiseno.GLB, numero),
            )
        )

    # ⚠ `nombre_base` es del TRABAJO —lo usan el ZIP y la lamina del set— y un
    # lote de veinticinco diseños no tiene uno: llamar al ZIP como el primer
    # diseño seria mentir sobre lo que trae. Con un solo diseño si es su nombre.
    almacen.actualizar(
        trabajo.id,
        disenos=len(pasos),
        nombre_base=etiquetas[0] if len(pasos) == 1 else None,
    )
    lanzar_serie(
        almacen=almacen,
        a=a,
        trabajo=trabajo,
        objetivo=ejecutar_post,
        pasos=tuple(pasos),
        # Sin esto, el cierre de la serie reemplaza el reporte entero y las
        # descargas se quedan sin nombre. Ver `lanzar_serie`.
        reporte_base={"nombres": etiquetas},
    )
    return trabajo.como_json()
