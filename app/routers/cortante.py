"""F3 — crear cortante. El modulo principal.

**Solo entra SVG.** Vectorizar aca adentro seria hacerlo a espaldas del
usuario, sobre un archivo que no eligio y sin poder revisar el resultado; el
Convertidor y Correcto (la correccion de lineas) ya dejan el SVG a la vista
antes de este paso. Lo que llega es lo que se imprime.

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

**Los `.stl` salen siempre.** Eran un checkbox de la pantalla y dejaron de
serlo: exportarlos cuesta milisegundos sobre una geometria que ya esta
calculada, y la unica consecuencia de tildarlo mal era volver a generar todo
para conseguir un archivo que ya estaba hecho. El flag sigue existiendo en el
motor (`generar(con_stl=...)`) y en el CLI, que son de uso programatico; la web
no lo ofrece. Un `con_stl` que llegue en el form se ignora.
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
    """True si el motor lo ignora en modo `cortante`. La pantalla lo apaga.

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

    @property
    def rgb(self) -> tuple[int, int, int]:
        """Los tres canales, para lo unico que se pinta del lado del servidor.

        El color de la paleta es de PANTALLA y por eso casi nunca sale de aca: la
        pieza la pinta el navegador y el archivo lleva los materiales del motor.
        La excepcion es el fondo del set, que compone Pillow (`cutter3d.lamina`)
        y necesita canales, no un `#rrggbb`.

        Que la conversion viva en el dato y no en el router es lo que evita que
        aparezca un segundo parser de hex la proxima vez que algo del servidor
        tenga que pintar: la paleta sigue teniendo un solo dueño, y ahora
        tambien sabe traducirse.
        """
        return (int(self.hex[1:3], 16), int(self.hex[3:5], 16), int(self.hex[5:7], 16))

    # Si el fondo es una mesa de fotos o no hay nada abajo de la pieza.
    #
    # Solo lo mira la paleta del FONDO —en la de la pieza siempre es el
    # default—. Un fondo CON piso recibe la sombra que el cortante proyecta;
    # uno SIN piso deja nada mas la sombra propia de la pieza, la del relieve
    # y las paredes del filo. Es un dato de la paleta y no del front por lo
    # mismo que el hex: si el front eligiera cual muestra apaga el piso,
    # habria dos listas.
    piso: bool = True


COLORES: tuple[ColorVista, ...] = (
    ColorVista("Blanco", "#f4f3f0"),
    ColorVista("Gris", "#9da3aa"),
    ColorVista("Rojo", "#e26562"),
    ColorVista("Amarillo", "#dbbd5b"),
    ColorVista("Azul", "#5c8ad7"),
    ColorVista("Verde", "#4ab36f"),
    ColorVista("Rosa", "#e28caf"),
    ColorVista("Violeta", "#9c78d6"),
)
"""El primero es el default: blanco, que es como sale el PLA mas comun.

Ninguno es blanco puro (`#ffffff`) ni negro puro a proposito — el visor usa
tone mapping ACES, y un blanco saturado se quema y deja la pieza sin relieve.

⚠ **Los ocho salen de una construccion, no de elegir hexes a ojo**, y eso
es lo que sostiene que sean una familia y no ocho decisiones sueltas:

1. **El tono es el de siempre.** No se toca ninguno: se lee del color original
   y se vuelve a usar tal cual. Medido, la deriva es de 0,29 grados o menos en
   los seis cromaticos (`Violeta` 0,02; `Amarillo` 0,29). `Gris` deriva 2,3
   grados, que a 4,4 de croma no significa nada. `Blanco` no cambia.
2. **La luminosidad es la propia de cada tono**, no una sola para todos: a
   igual croma, un amarillo con el L* de un azul deja de parecer amarillo. Van
   entre 57 y 78 de L*, que es la banda en la que cada uno se llama como se
   llama.
3. **El croma es la misma FRACCION de lo que cada tono puede dar** a esa
   luminosidad — el 65% del borde del gamut sRGB —, y no un numero absoluto
   comun. Un absoluto comun deja al amarillo apagado y al azul contra su techo,
   porque el amarillo llega mucho mas lejos en sRGB: los topes a estas
   luminosidades son 81 para el amarillo y 69 para el azul.

El resultado queda en el medio de las dos paletas que hubo antes. Croma medio:
**43,1**, contra 54,7 de la original —demasiado fuerte, el color se comia su
propio relieve: el filo, el pie y el grabado se distinguen por pocos niveles de
luminancia y sobre un color muy saturado esas diferencias caen en la parte
comprimida de la curva ACES— y 28,0 de la pasada pastel, que corrigio eso pero
se llevo puesta la vitalidad.

Y recupera la separacion, que es lo que el pastel habia perdido: el par mas
cercano vuelve a 26,3 de dE76 (`Azul`/`Violeta`), practicamente los 26,5 de la
paleta original, contra los 18,0 del pastel.
"""


COLORES_FONDO: tuple[ColorVista, ...] = (
    *COLORES,
    ColorVista("Sin fondo", "#ffffff", piso=False),
)
"""La paleta del FONDO de la foto: los mismos colores, mas `Sin fondo`.

Es una lista aparte y no `COLORES` con un agregado porque `Sin fondo` **no es
un color de pieza**: un cortante pintado de blanco puro se quema con el tone
mapping ACES y sale sin relieve, que es justo lo que evita la nota de arriba.
Como fondo no pasa: `scene.background` es un `clearColor` y no lo toca ni el
tone mapping ni ninguna luz, asi que el `#ffffff` llega literal al JPG.

Que sea la ultima y no la primera es deliberado: el default del fondo sigue
siendo `Blanco`, y esta es la opcion que se elige a proposito.

`piso=False` es todo lo que la distingue, y lo que hace es sacar el piso de la
escena: sin piso no hay sombra proyectada sobre el fondo —queda blanco parejo
de borde a borde— y la pieza conserva la suya propia, la del relieve. El
encuadre tambien lo mira: sin sombra que meter en el cuadro, el cortante se
lleva todo el lugar que antes le reservaba.
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
        objetivo=ejecutar_cortante,
        argumentos=(
            str(destino),
            str(subida.ruta),
            Modo(modo).value,
            asdict(parametros),
        ),
    )
    return trabajo.como_json()
