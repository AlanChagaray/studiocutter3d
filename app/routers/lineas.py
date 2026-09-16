"""F2 — correccion de lineas, por trabajo.

Va a un proceso hijo y no en linea porque el eje medial sobre una imagen
grande no es instantaneo y porque asi hereda el timeout: una imagen patologica
se corta sola en vez de dejar colgado al servidor.

**El contorneado de macizos viene encendido y la normalizacion de trazo
apagada**, las dos por decision explicita del usuario, y el resultado siempre
declara cuanto toco cada una. Esa declaracion no es decorativa: son las dos
unicas modificaciones del arte que el producto permite, asi que tienen que
quedar a la vista de quien las pidio.

Los dos milimetros de la normalizacion —ancho objetivo y lado mayor de la
pieza— **no se revalidan aca**, por la misma regla que en `cortante.py`:
`CutterParams` es el unico dueño de los limites y lo que este router hace es
traducir su error a un 422 con el nombre del campo. Se construye antes de crear
el trabajo para que un valor invalido falle de entrada y no dentro del proceso
hijo, donde el usuario solo veria un trabajo en error.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile

from cutter3d import CutterParams

from ..almacen import TipoTrabajo
from ..archivos import FORMATOS_LINEAS, dir_de_trabajo, resolver_entrada
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import traducir
from ..tareas import ejecutar_lineas
from ..trabajos import lanzar
from .cortante import CampoParametro

router = APIRouter(prefix="/api/lineas", tags=["lineas"])

_D = CutterParams()

CAMPOS: tuple[CampoParametro, ...] = (
    CampoParametro(
        "ancho_trazo_mm", "Ancho de trazo", _D.ancho_trazo_mm, 0.1, "A que grosor quedan los trazos"
    ),
    CampoParametro(
        "lado_mayor_mm",
        "Lado mayor",
        _D.lado_mayor_mm,
        1.0,
        "Tamaño con el que despues se va a generar el cortante",
    ),
)
"""Los dos campos de la normalizacion, para el formulario y la validacion.

Se arman aca y no se reusan los de `cortante.py` aunque compartan nombre,
default y limite: el numero tiene un solo dueño (`CutterParams`, de donde sale
`valor`), pero la ayuda **no dice lo mismo en las dos pantallas**. En el cortante
`ancho_trazo_mm` es un minimo —F3 solo engorda—; aca es exacto, porque la
normalizacion tambien afina. Copiar la ayuda del cortante seria copiar una
afirmacion falsa."""


@router.post("")
def corregir(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    *,
    archivo: Annotated[UploadFile | None, File()] = None,
    origen: Annotated[str | None, Form()] = None,
    contornear_macizos: Annotated[bool, Form()] = True,
    normalizar_trazo: Annotated[bool, Form()] = False,
    ancho_trazo_mm: Annotated[float, Form()] = _D.ancho_trazo_mm,
    lado_mayor_mm: Annotated[float, Form()] = _D.lado_mayor_mm,
) -> dict[str, object]:
    try:
        dimensiones = CutterParams(ancho_trazo_mm=ancho_trazo_mm, lado_mayor_mm=lado_mayor_mm)
    except Exception as exc:
        raise traducir(exc) from exc

    trabajo = almacen.crear(usuario, TipoTrabajo.LINEAS)
    destino = dir_de_trabajo(a, trabajo.id, crear=True)

    subida = resolver_entrada(
        a,
        almacen,
        flujo=archivo.file if archivo is not None else None,
        origen_id=origen,
        usuario=usuario,
        destino_dir=destino,
        permitidos=FORMATOS_LINEAS,
        # El nombre del cliente entra SOLO para que la descarga se llame
        # como el archivo original. `sanear_nombre_base` lo reduce a
        # [A-Za-z0-9._-] y nunca toca una ruta: el archivo en disco sigue
        # siendo `entrada.<ext>`.
        nombre_cliente=archivo.filename if archivo is not None else None,
    )
    # Si la entrada vino encadenada, `resolver_entrada` ya heredo el nombre del
    # trabajo anterior; si vino por upload, sale del que acaba de subir.
    almacen.actualizar(trabajo.id, nombre_base=subida.nombre_base)

    lanzar(
        almacen=almacen,
        a=a,
        trabajo=trabajo,
        objetivo=ejecutar_lineas,
        argumentos=(
            str(destino),
            str(subida.ruta),
            contornear_macizos,
            normalizar_trazo,
            # Solo las dos que F2 usa, y no el `asdict` entero de `CutterParams`:
            # mandar las diez seria prometerle al hijo una configuracion de
            # geometria que esta etapa no mira.
            {
                "ancho_trazo_mm": dimensiones.ancho_trazo_mm,
                "lado_mayor_mm": dimensiones.lado_mayor_mm,
            },
        ),
    )
    return trabajo.como_json()
