"""F3 — crear cortante. El modulo principal.

**Solo entra SVG.** Vectorizar aca adentro seria hacerlo a espaldas del
usuario, sobre un archivo que no eligio y sin poder revisar el resultado; el
Convertidor y la correccion de lineas ya dejan el SVG a la vista antes de este
paso. Lo que llega es lo que se imprime.

Los 10 parametros se reciben uno por uno y no como un JSON suelto: asi FastAPI
rechaza lo que no es un numero antes del handler, y `CutterParams` rechaza lo
que esta fuera de rango con el nombre del parametro adentro. **Los limites no
se revalidan aca** — mayor que cero y hasta 1000 mm ya son invariantes del
motor, y duplicarlos en la web seria dejar dos verdades que se van a
desincronizar. Lo que hace este router es traducir el error del motor a un 422
con el nombre del campo, que es lo que el formulario necesita para pintar el
campo en rojo.

`CAMPOS` es la misma lista que consume la pantalla, asi que el formulario y la
validacion no pueden discrepar: agregar un parametro es tocar un solo lugar.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, UploadFile

from cutter3d import CutterParams, Modo

from ..almacen import TipoTrabajo
from ..archivos import FORMATOS_CORTANTE, dir_de_trabajo, resolver_entrada
from ..dependencias import AjustesDep, AlmacenDep, UsuarioRequerido
from ..errores import traducir
from ..tareas import ejecutar_cortante
from ..trabajos import lanzar

router = APIRouter(prefix="/api/cortante", tags=["cortante"])

_D = CutterParams()


@dataclass(frozen=True)
class CampoParametro:
    """Un parametro tal como se dibuja en el formulario."""

    nombre: str
    etiqueta: str
    valor: float
    paso: float
    ayuda: str
    solo_marcador: bool = False
    """True si el motor lo ignora en modo `cortante`. La pantalla lo esconde.

    No es cosmetico: un campo que no cambia nada del resultado es una promesa
    falsa. Se marca aca, del lado del que conoce el motor, y no en el template.
    """


def _campo(
    nombre: str, etiqueta: str, paso: float, ayuda: str, *, solo_marcador: bool = False
) -> CampoParametro:
    """El valor sale de `CutterParams`: los defaults tienen un solo dueño."""
    return CampoParametro(nombre, etiqueta, getattr(_D, nombre), paso, ayuda, solo_marcador)


CAMPOS: tuple[CampoParametro, ...] = (
    _campo("lado_mayor_mm", "Lado mayor", 1.0, "Tamaño final de la pieza"),
    _campo("altura_base_mm", "Base", 0.1, "Espesor de la placa del marcador", solo_marcador=True),
    _campo("altura_trazos_mm", "Trazos", 0.1, "Alto del dibujo en relieve", solo_marcador=True),
    _campo("ancho_trazo_mm", "Ancho de trazo", 0.1, "Grosor minimo de linea", solo_marcador=True),
    # `luz_mm` NO es del marcador aunque lo parezca por el nombre: es el
    # offset donde arranca el filo, y en modo cortante separa el filo del
    # dibujo. Cambiarlo cambia la pieza en los dos modos.
    _campo("luz_mm", "Luz", 0.1, "Separacion entre el dibujo y el filo"),
    _campo("filo_ancho_mm", "Ancho del filo", 0.1, "Espesor de la pared que corta"),
    _campo("filo_alto_mm", "Alto del filo", 0.5, "Altura de la pared que corta"),
    _campo("pie_ancho_extra_mm", "Pie (ancho extra)", 0.1, "Refuerzo de apoyo"),
    _campo("pie_alto_mm", "Pie (alto)", 0.1, "Altura del refuerzo"),
    _campo(
        "distancia_colision_mm",
        "Distancia de colision",
        0.1,
        "Extremos del filo mas cerca que esto se juntan y la muesca no se corta",
    ),
)

LIMITE_MAX_MM = CutterParams.LIMITE_MAX_MM


@dataclass(frozen=True)
class ColorVista:
    """Un color de la paleta del visor.

    **No viaja al archivo.** El `.3mf` y el `.glb` llevan los materiales que
    les puso el motor, y al imprimir el color lo pone el filamento: esto sirve
    para mirar la pieza como va a quedar, no para configurarla. Por eso es un
    dato de pantalla y no un parametro de `generar_cortante`.

    Vive aca —y no suelto en el template— por lo mismo que `CAMPOS`: el nombre
    y el codigo se escriben una sola vez, y un test puede exigir la lista
    completa sin leer HTML.
    """

    nombre: str
    hex: str


COLORES: tuple[ColorVista, ...] = (
    ColorVista("Blanco", "#f4f3f0"),
    ColorVista("Gris", "#8c9298"),
    ColorVista("Rojo", "#c9302c"),
    ColorVista("Amarillo", "#eec12a"),
    ColorVista("Azul", "#2a63c4"),
    ColorVista("Verde", "#2f9c55"),
    ColorVista("Rosa", "#ef78a8"),
    ColorVista("Violeta", "#7d51c4"),
)
"""El primero es el default: blanco, que es como sale el PLA mas comun.

Ninguno es blanco puro (`#ffffff`) ni negro puro a proposito — el visor usa
tone mapping ACES, y un blanco saturado se quema y deja la pieza sin relieve.
"""


@router.post("")
def generar_cortante(
    usuario: UsuarioRequerido,
    almacen: AlmacenDep,
    a: AjustesDep,
    # Todo lo que sigue va por nombre: son los 10 parametros del contrato mas
    # las opciones, y una lista posicional de 17 seria imposible de leer.
    *,
    archivo: Annotated[UploadFile | None, File()] = None,
    origen: Annotated[str | None, Form()] = None,
    modo: Annotated[Literal["cortante", "cortante+marcador"], Form()] = "cortante+marcador",
    con_stl: Annotated[bool, Form()] = True,
    lado_mayor_mm: Annotated[float, Form()] = _D.lado_mayor_mm,
    altura_base_mm: Annotated[float, Form()] = _D.altura_base_mm,
    altura_trazos_mm: Annotated[float, Form()] = _D.altura_trazos_mm,
    ancho_trazo_mm: Annotated[float, Form()] = _D.ancho_trazo_mm,
    luz_mm: Annotated[float, Form()] = _D.luz_mm,
    filo_ancho_mm: Annotated[float, Form()] = _D.filo_ancho_mm,
    filo_alto_mm: Annotated[float, Form()] = _D.filo_alto_mm,
    pie_ancho_extra_mm: Annotated[float, Form()] = _D.pie_ancho_extra_mm,
    pie_alto_mm: Annotated[float, Form()] = _D.pie_alto_mm,
    distancia_colision_mm: Annotated[float, Form()] = _D.distancia_colision_mm,
) -> dict[str, object]:
    try:
        parametros = CutterParams(
            lado_mayor_mm=lado_mayor_mm,
            altura_base_mm=altura_base_mm,
            altura_trazos_mm=altura_trazos_mm,
            ancho_trazo_mm=ancho_trazo_mm,
            luz_mm=luz_mm,
            filo_ancho_mm=filo_ancho_mm,
            filo_alto_mm=filo_alto_mm,
            pie_ancho_extra_mm=pie_ancho_extra_mm,
            pie_alto_mm=pie_alto_mm,
            distancia_colision_mm=distancia_colision_mm,
        )
    except Exception as exc:
        raise traducir(exc) from exc

    trabajo = almacen.crear(usuario, TipoTrabajo.CORTANTE)
    destino = dir_de_trabajo(a, trabajo.id, crear=True)

    subida = resolver_entrada(
        a,
        almacen,
        flujo=archivo.file if archivo is not None else None,
        origen_id=origen,
        usuario=usuario,
        destino_dir=destino,
        permitidos=FORMATOS_CORTANTE,
    )

    lanzar(
        almacen=almacen,
        a=a,
        trabajo=trabajo,
        objetivo=ejecutar_cortante,
        argumentos=(
            str(destino),
            str(subida.ruta),
            Modo(modo).value,
            asdict(parametros),
            con_stl,
        ),
    )
    return trabajo.como_json()
