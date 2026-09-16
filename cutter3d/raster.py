"""F1 (conversion a JPG) y F2 (correccion de lineas).

**F1** — cualquier imagen que se sepa abrir, a jpg. El SVG se rasteriza con
`resvg-py` (Rust, sin dependencias del sistema; `cairosvg` necesitaria la DLL de
Cairo, que en Windows no viene). Ojo: pasar un SVG por aca **pierde fidelidad** —
si ya tenes el vector, va derecho al modulo de cortante y no por esta etapa.

Tres decodificadores y una regla de precedencia que importa:

- **resvg** para SVG.
- **LibRaw** (via `rawpy`) para los RAW de camara.
- **Pillow** para todo lo demas, con `pillow-heif` registrado para HEIC/HEIF.

La regla: ante un contenedor TIFF o ISO-BMFF se prueba **LibRaw primero**. No es
arbitrario — Pillow *abre* un `.NEF` sin fallar, pero devuelve el **preview JPEG
embebido** en vez de la foto, asi que "probar Pillow y si falla usar LibRaw"
produce una salida silenciosamente incorrecta. LibRaw, en cambio, rechaza lo que
no es RAW con un error limpio (`LibRawFileUnsupportedError`), asi que es el unico
discriminador confiable de los dos.

**F2** — jpg a blanco y negro puro de lineas. Dos reglas que vienen del pedido:

- La salida contiene **solo 0 y 255**. Cero grises, cero sombreados.
- El **contorneado de zonas macizas esta ENCENDIDO por default**. Es una de las
  dos etapas del flujo que modifican el arte, y por eso **siempre declara cuantas
  zonas toco y que area**: la modificacion no puede ser invisible. El modulo de
  cortante (F3) nunca hace esto — reproduce fiel lo que reciba.

El umbral de "macizo" se autocalibra con el propio dibujo: se mide su ancho de
trazo mediano `w` y se considera macizo toda region donde entre un disco de radio
`0,75 w`. Un trazo de ancho `w` tiene medio ancho `0,5 w` y no entra; una mancha
si. Asi no hace falta conocer la escala fisica de la imagen.

**La normalizacion de ancho de trazo esta APAGADA por default**, y es la otra
modificacion del arte que F2 sabe hacer. Deja todos los trazos del dibujo al
mismo ancho —1 mm medido sobre la pieza final— reconstruyendolos desde su eje
medial: engorda los finos y **afina los gruesos**, que es lo que la dilatacion
uniforme de F3 no puede hacer. Sirve para los line art donde el contorno viene
mucho mas grueso que el detalle interior, o al reves: el detalle interior tan
fino que el marcador impreso se dobla o se rompe.

⚠ **Es lo unico de F2 que necesita saber la escala fisica**, y la deduce: F3
escala el dibujo para que su lado mayor mida `lado_mayor_mm`, asi que un
milimetro de la pieza son `lado_mayor_px_de_la_tinta / lado_mayor_mm` pixeles de
esta imagen. Los dos numeros que entran —ancho objetivo y lado mayor— son los
mismos de `CutterParams`, y se **declaran en el resultado**: si despues se genera
el cortante con otro tamaño, la normalizacion quedo calibrada para otra cosa.

⚠ **Y es lo unico de F2 que puede AMPLIAR la imagen.** Un dibujo chico —un JPG
de 339 px bajado de internet es lo normal— trae el trazo objetivo en 3 o 4 px,
y reconstruir un trazo de 3 px desde un eje de 1 px deja el centro y el ancho
clavados a la grilla con medio pixel de error: la linea sale temblorosa y la
impresora vibra siguiendola. Por eso, con la normalizacion encendida, la etapa
amplia los grises por un factor entero hasta que el objetivo mida
`ANCHO_MINIMO_NORMALIZACION_PX`, trabaja y **entrega** a esa escala, y lo
declara en `factor_ampliacion`. El camino sin normalizar no se toca.

Viene apagada por lo que hace, no por lo que cuesta: es una modificacion del
arte, y en este proyecto eso se elige a proposito. **El costo medido es de
tiempo y no de memoria**, que es lo contrario de lo que sugiere el segundo
`medial_axis` que necesita: a 3 MP (1732x1732, este venv) el pico de Working Set
de `preparar_lineas` da **267,1-267,4 MB sin normalizar y 267,1 MB
normalizando** —tres corridas de cada uno—, y el tiempo sube de **0,72-0,76 s a
1,44-1,57 s**. El pico no se mueve porque los dos ejes mediales corren en serie
y el primero libera sus arrays antes del segundo: el techo lo sigue poniendo
**uno** de los dos, y lo que la normalizacion agrega encima (la transformada de
distancia en float64, las piezas del eje en int32 y unas mascaras booleanas)
entra holgado abajo de los ~190 MB que ese techo ya tenia reservados. Un dibujo
chico que se amplia hasta el presupuesto paga lo mismo: buzz (339x307, x5 →
2,6 MP) pasa de 0,04 s sin normalizar a **1,3 s** normalizando, con un pico de
226 MB.

La salida de F2 se guarda en **PNG y nunca en JPG**: el contrato es explicito en
que el ruido de compresion ensucia el borde y mete dientes en el filo. Aparte de
esa salida —y sin reemplazarla— `guardar_editable` escribe una **copia en JPG**
para que el usuario la retoque a mano y la vuelva a subir: es el formato que sus
programas editan y el unico que esta etapa acepta de entrada.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pillow_heif
import rawpy
import resvg_py
from PIL import Image
from scipy.ndimage import convolve, distance_transform_edt, label
from skimage.filters import threshold_otsu
from skimage.morphology import medial_axis

from .errors import Cutter3DError, ImagenInvalida
from .params import CutterParams

pillow_heif.register_heif_opener()
"""Registra HEIC/HEIF (y de paso AVIF) en Pillow. Va al importar el modulo: sin
esto `Image.open` sobre un .heif levanta `UnidentifiedImageError`."""

Mascara = npt.NDArray[np.bool_]

EXTENSIONES_CAMARA = frozenset({".tiff", ".cr3"})
"""Contenedores que PUEDEN traer un RAW de camara adentro.

`.tiff` cubre ademas de un TIFF comun a ARW, CR2, NEF y DNG, que son TIFF con
las IFD del fabricante; `.cr3` es ISO-BMFF. La ambiguedad es real y viene de los
formatos, no del codigo: por eso la resuelve LibRaw y no una tabla."""

EXTENSIONES_RASTER = (
    frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".jfif",
            ".webp",
            ".gif",
            ".bmp",
            ".ico",
            ".tga",
            ".psd",
            ".avif",
            ".heif",
        }
    )
    | EXTENSIONES_CAMARA
)
EXTENSIONES_VECTOR = frozenset({".svg"})
EXTENSIONES_ENTRADA = EXTENSIONES_RASTER | EXTENSIONES_VECTOR

MAX_PIXELES = 89_478_485
"""Tope de pixeles de la imagen decodificada.

Es el mismo numero que Pillow usa como guard de decompression bomb
(`Image.MAX_IMAGE_PIXELS`), reusado a proposito para tener **un solo limite**.
Hace falta declararlo aca porque `rawpy` **no pasa por Pillow**: sin esto, un RAW
de 25 MB decodifica a mas de 100 megapixeles sin que nada lo frene. Cubre toda
camara de consumo, incluidas las de 61 MP."""

MAX_PIXELES_TRABAJO = 3_000_000
"""Presupuesto de pixeles con el que se TRABAJA. Distinto de `MAX_PIXELES`.

Los dos limites tienen roles distintos y por eso son dos numeros:

- `MAX_PIXELES` (89,4 MP) dice **que se acepta decodificar**. Es un guard de
  seguridad heredado de Pillow, y sigue cubriendo la camara de 61 MP.
- `MAX_PIXELES_TRABAJO` dice **a que tamaño se procesa**. Es un presupuesto de
  memoria, y el numero salio de medir, no de estimar.

**Lo medido** (pico de Working Set de `preparar_lineas` con contorneado, sobre
line art sintetico, en este venv):

| Presupuesto | Trabajo | Pico |
|---|---|---|
| 2,5 MP | 1825x1369 | 243 MB |
| **3 MP** | **2000x1500** | **263 MB** |
| 4 MP | 2309x1732 | 374 MB |
| 12 MP | 4000x3000 | **830 MB** |
| 16 MP | 4618x3464 | **1275 MB** |

El costo marginal es de **~63 MB por megapixel**, y casi todo es
`medial_axis(..., return_distance=True)`: no son los 8 bytes/px de la
transformada de distancia float64 sino ~63, porque skimage materializa ademas
el orden de los pixeles de tinta y varios intermedios del mismo tamaño. Es el
unico paso de F2 que pica: la morfologia del contorneado —hoy por transformada
de distancia, ver `_macizos`— suma ~3 MB de transitorios float64 a 3 MP (la
tabla se midio con `opening` por footprint y daba 264; hoy da 267), y `label`
no mueve el pico.

⚠ **La normalizacion de trazo corre un segundo `medial_axis` y aun asi no mueve
este numero.** Medido a 3 MP: 267,1-267,4 MB sin normalizar contra 267,1 MB
normalizando. No es que el segundo sea gratis — es que los dos corren **en
serie**, el primero suelta sus arrays antes de que arranque el segundo, y el
techo lo pone el mas caro de los dos y no la suma. Lo que sube es el tiempo
(0,72-0,76 s -> 1,44-1,57 s; la poda de espigas son `medio_ancho` pasadas de
convolucion 3x3 y a esta escala el radio da 9). Quien ajuste este presupuesto
no necesita una segunda cuenta para el camino normalizado.

De ahi sale el diagnostico: **una foto de telefono de 12 MP —la entrada mas
normal que existe— picaba 830 MB en una instancia de 512 MB.** No hacia falta
ningun caso patologico.

**Por que 3 MP no es poco.** El motor rasteriza la geometria a
`AjustesMotor.px_por_mm = 20`, asi que un cortante de 90 mm se construye sobre
1800 px: 2000x1500 ya esta por encima de la resolucion a la que F3 va a trabajar.
Y lo que F2 le entrega a F3 es un **SVG** —vectorial—, asi que esta resolucion
decide cuan fielmente se trazan las curvas, no el tamaño de la pieza: subirla
compra detalle que la geometria no usa, y se paga a 63 MB el megapixel.

⚠ **El presupuesto acota en las dos direcciones.** La normalizacion de trazo
**amplia** un dibujo chico hasta que el trazo objetivo tenga
`ANCHO_MINIMO_NORMALIZACION_PX` —sin eso la linea sale temblorosa, ver esa
constante— y el factor se elige para no pasar de este mismo numero. Asi la
imagen ampliada nunca es mas grande que la foto grande ya reducida, y el techo
de memoria que se midio arriba sigue siendo el techo: no hay una segunda
cuenta para el camino ampliado.

⚠ Lo que se reduce o se amplia **se declara** (`ResultadoF2.tamano_original`,
`tamano_usado` y `factor_ampliacion`): cambiar la escala en silencio seria
exactamente el tipo de modificacion del arte que este proyecto no se permite."""

CALIDAD_JPG = 95
FACTOR_MACIZO = 0.75
"""Radio del disco de apertura, en unidades de ancho de trazo mediano."""

SEMILLA_EJE = 0
"""Semilla de `medial_axis`, para que dos corridas den lo mismo.

⚠ **`medial_axis` NO es determinista si no se le pasa `rng`.** Desempata el
orden de los pixeles de tinta al azar, y el eje cambia de una corrida a la otra:
medido sobre `tests/buzz-lightyear.jpg`, cuatro corridas dieron ejes de 3286,
3289, 3288 y 3287 pixeles, y la normalizacion declaro `area_engrosada_px` 227,
224 y 224 en tres corridas del mismo archivo.

Que la cifra declarada se mueva sola tira abajo el punto del proyecto: el
reporte afirma cuanto se toco el dibujo, y una afirmacion que cambia sin que
cambie la entrada no afirma nada. La semilla va fija en el modulo y no como
parametro — no hay ningun caso en el que convenga otra."""

VECINDAD = np.ones((3, 3), np.uint8)
"""Los 8 vecinos mas el propio pixel. Contar con esto es mas barato que
`label` o que una morfologia con footprint, y alcanza para las dos cuentas que
hacen falta: grado de un pixel del eje y mayoria de una ventana 3x3."""

CUENTA_DE_PUNTA = 2
"""Cuanto da `VECINDAD` sobre una punta del eje: el propio pixel mas un vecino.

Un pixel aislado da 1 y uno de tramo da 3 o mas, asi que `<= 2` es exactamente
"punta o mota" — la condicion de la poda."""

MAYORIA_DE_LA_VENTANA = 5
"""Cuantos de los 9 pixeles de la ventana tienen que ser tinta para que el del
medio lo siga siendo. Cinco es la mayoria estricta de nueve."""

ANCHO_MINIMO_NORMALIZACION_PX = 16
"""Cuantos pixeles tiene que medir el trazo objetivo para reconstruirlo liso.

Es la constante que decide **a que resolucion se normaliza**, y sale de una
limitacion aritmetica que ningun filtro arregla: el eje medial es una curva de
un pixel, asi que el centro del trazo reconstruido queda clavado a la grilla
con un error de hasta medio pixel, y el ancho —`2k + 1`— con el mismo error
para cada lado. Sobre un objetivo de 3,5 px eso es ±17% de ancho y una linea
que bambolea medio pixel: se ve como una linea temblorosa, y el SVG trazado se
lo lleva a la impresora. Sobre un objetivo de 16 px el mismo medio pixel es
±3%.

Medido sobre `tests/buzz-lightyear.jpg` (339x307, objetivo 3,5 px), ancho del
trazo reconstruido p5/p50/p95 **en pixeles nativos**:

| Grilla | Objetivo | p5 / p50 / p95 | Dispersion |
|---|---|---|---|
| nativa | 3,5 px | 2,83 / 2,83 / 4,00 | **41%** |
| x3 | 10,5 px | 3,40 / 3,59 / 4,00 | 17% |
| x5 | 17,5 px | 3,22 / 3,30 / 3,60 | **11%** |

El numero es del mismo orden que `AjustesMotor.px_por_mm = 20`, que es la
grilla sobre la que F3 va a rasterizar despues: normalizar mas fino que eso
compra detalle que la geometria no usa, y mas grueso deja el defecto de la
tabla. Un dibujo que ya viene grande —una foto de 3 MP trae el objetivo en
17-19 px— no se toca."""

DIMENSIONES = CutterParams()
"""Los defaults de la pieza, para no re-escribirlos aca.

F2 no inventa el ancho de trazo objetivo ni el tamaño final: son los **mismos
dos numeros** que despues usa F3, y `params.py` es su unico dueño —tambien de
sus limites—. Importarlo no cuesta nada: `params` es `math` + `dataclasses`."""


@dataclass(frozen=True)
class ResultadoF2:
    imagen: npt.NDArray[np.uint8]
    """uint8 con exactamente dos valores: 0 (tinta) y 255 (fondo)."""

    umbral_usado: int
    ancho_trazo_px: float
    zonas_contorneadas: int
    area_contorneada_px: int
    contorneado_activo: bool

    tamano_original: tuple[int, int]
    """Ancho y alto con los que llego la imagen, antes del presupuesto."""

    tamano_usado: tuple[int, int]
    """Ancho y alto con los que se trabajo, que son los de `imagen`.

    Distinto de `tamano_original` en dos casos opuestos: cuando la entrada
    superaba `MAX_PIXELES_TRABAJO` y hubo que **reducirla**, y cuando se
    normalizo un dibujo chico y hubo que **ampliarlo** para que el trazo
    objetivo tuviera cuerpo (ver `factor_ampliacion`). Se declara por la misma
    razon que `zonas_contorneadas`: toda modificacion del arte se mide y se
    reporta, nunca se hace en silencio. Todo lo que este resultado mide en
    pixeles —`ancho_trazo_px`, `ancho_objetivo_px`, las areas— y el PNG de
    salida estan en esta escala, no en la original."""

    factor_ampliacion: int = 1
    """Cuantas veces se amplio la imagen antes de normalizar. 1 si no se toco.

    La normalizacion necesita que el trazo objetivo mida al menos
    `ANCHO_MINIMO_NORMALIZACION_PX` para salir liso, y un dibujo chico no lo
    trae: se amplia desde los **grises** —el antialiasing del JPG guarda la
    posicion sub-pixel del borde, que el umbral tira— por un factor entero, y
    de ahi en adelante toda la etapa trabaja y entrega a esa escala. Es
    entero para que cada pixel original sea exactamente `k x k` de los nuevos
    y las medidas se puedan volver a la escala original dividiendo.

    Lo acota `MAX_PIXELES_TRABAJO`, el mismo presupuesto de la reduccion: la
    imagen ampliada nunca pasa el tamaño al que ya se procesa una foto grande,
    asi que el techo de memoria es el que ya estaba. Solo se amplia cuando se
    normaliza — el camino sin normalizar entrega en la escala que recibio."""

    normalizacion_activa: bool = False
    """Si se llevaron todos los trazos al mismo ancho. Ver `_normalizar_trazo`."""

    ancho_objetivo_px: float = 0.0
    """Ancho que la normalizacion PIDIO, en pixeles de `imagen`. 0 si no corrio."""

    ancho_logrado_px: int = 0
    """Ancho que la normalizacion ENTREGO, en pixeles. Siempre impar.

    Distinto de `ancho_objetivo_px` por la grilla: reconstruir un eje de un
    pixel a distancia `k` solo puede dar `2k + 1`. Se declara aparte por lo
    mismo que `tamano_usado` se declara aparte de `tamano_original` — lo que se
    entrego y lo que se pidio no son el mismo dato, y la diferencia importa
    justo cuando la imagen es chica. F3 termina de ajustar la mediana al
    objetivo real en mm con su dilatacion uniforme, que para eso esta."""

    ancho_objetivo_mm: float = 0.0
    lado_mayor_supuesto_mm: float = 0.0
    """Los dos numeros con los que se tradujo el objetivo de mm a pixeles.

    Se declaran porque la traduccion es una **suposicion sobre F3**: lo que esta
    etapa fija de verdad es una **proporcion** —el trazo queda en
    `ancho_trazo_mm / lado_mayor_mm` del lado mayor del dibujo, 1/90 con los
    defaults— y esa proporcion recien se vuelve milimetros cuando F3 elige el
    tamaño. Si los dos lados mayores no coinciden, el trazo sale escalado por
    `lado_mayor_de_F3 / lado_mayor_supuesto_mm`.

    ⚠ **El desvio no es simetrico, y conviene errarle para abajo.** Medido sobre
    `tests/murcielago.jpg` normalizado suponiendo 90 mm:

    | Cortante generado a | Trazo antes de dilatar | Final |
    |---|---|---|
    | 60 mm | 0,54 mm | **1,03 mm** (F3 dilata 0,231) |
    | 90 mm | 0,81 mm | **1,03 mm** (F3 dilata 0,097) |
    | 120 mm | 1,10 mm | **1,10 mm** (F3 no adelgaza; avisa) |

    Una pieza mas chica que la supuesta la arregla F3 sola, porque su dilatacion
    uniforme **engorda**; una mas grande no, porque F3 nunca adelgaza —levanta la
    advertencia de "el trazo viene mas grueso que el objetivo" y lo deja como
    esta. Sin este campo declarado, el unico rastro de la causa seria esa
    advertencia, tres pantallas mas adelante."""

    area_engrosada_px: int = 0
    """Pixeles que la normalizacion agrego (trazos que venian finos)."""

    area_afinada_px: int = 0
    """Pixeles que la normalizacion saco (trazos que venian gruesos).

    Es la mitad que la dilatacion uniforme de F3 **no sabe hacer** —solo engorda,
    nunca adelgaza—, y por eso se declara aparte en vez de sumarse en un neto con
    la anterior: son las dos direcciones de la correccion y no la misma cosa."""

    area_protegida_px: int = 0
    """Pixeles de zona maciza que la normalizacion dejo como estaban.

    Solo puede ser > 0 con el contorneado apagado, que es cuando las manchas se
    respetan (ver `_normalizar_trazo`). Se declara porque es el unico caso en el
    que la opcion **no** hace lo que promete sobre una parte del dibujo, y sin
    este numero la respuesta a "por que este contorno sigue grueso" no esta en
    ningun lado."""

    @property
    def fue_reducida(self) -> bool:
        """Si el presupuesto de pixeles obligo a achicar la entrada.

        Excluye la ampliacion a proposito: las dos cambian `tamano_usado`, pero
        una tira detalle y la otra no, y el aviso al usuario no es el mismo.
        No pueden darse juntas — una imagen reducida quedo en el presupuesto y
        no hay factor entero que la amplie sin pasarse—, asi que con el factor
        alcanza para distinguirlas.
        """
        return self.factor_ampliacion == 1 and self.tamano_usado != self.tamano_original

    @property
    def fue_ampliada(self) -> bool:
        return self.factor_ampliacion > 1

    @property
    def px_por_mm(self) -> float:
        """Escala con la que se normalizo. 0 si no se normalizo."""
        if self.ancho_objetivo_mm <= 0:
            return 0.0
        return self.ancho_objetivo_px / self.ancho_objetivo_mm


def _exigir_tamano(entrada: Path, ancho: int, alto: int) -> None:
    """Corta ANTES de decodificar. Despues ya se reservo la memoria."""
    pixeles = ancho * alto
    if pixeles > MAX_PIXELES:
        raise ImagenInvalida(
            str(entrada),
            f"la imagen es de {ancho}x{alto} ({pixeles / 1e6:.0f} megapixeles) y el "
            f"limite es {MAX_PIXELES / 1e6:.0f}",
        )


def _abrir_raw(entrada: Path) -> Image.Image | None:
    """Decodifica un RAW de camara, o devuelve `None` si no era uno.

    `imread` solo parsea la metadata —es barato— asi que el tamano se chequea
    entre el y `postprocess`, que es la decodificacion cara de verdad.
    """
    try:
        with rawpy.imread(str(entrada)) as raw:
            _exigir_tamano(entrada, raw.sizes.raw_width, raw.sizes.raw_height)
            return Image.fromarray(raw.postprocess())
    except rawpy.LibRawFileUnsupportedError:
        return None  # no era RAW: que lo abra Pillow
    except ImagenInvalida:
        raise
    except Exception as exc:
        raise ImagenInvalida(str(entrada), f"no se pudo abrir el RAW: {sin_ruta(exc)}") from exc


_RUTA_CITADA = re.compile(r"""(['"])[^'"]*[\\/][^'"]*\1""")


def sin_ruta(exc: BaseException) -> str:
    """Mensaje de una excepcion de terceros, sin rutas del servidor adentro.

    `app.errores` manda el `motivo` de `ImagenInvalida` **verbatim** al cliente,
    y varias excepciones de Pillow traen la ruta absoluta en su propio texto:
    `cannot identify image file 'C:/.../trabajo/<uuid>/entrada.tiff'`. Medido
    sobre los formatos que acepta el conversor, 4 de 6 la filtran. Interpolar
    `str(exc)` crudo tira abajo justo la garantia que `app.errores` declara
    tener, y el bug no esta ahi sino aca, en quien construye el motivo.

    Se redacta cualquier cosa entrecomillada que contenga un separador de ruta,
    en vez de descartar el mensaje entero: el texto de la libreria es lo que
    hace accionable el error para el usuario.
    """
    return _RUTA_CITADA.sub("'<archivo>'", str(exc))


def _abrir_como_pil(entrada: Path) -> Image.Image:
    sufijo = entrada.suffix.lower()
    if not entrada.is_file():
        raise ImagenInvalida(str(entrada), "el archivo no existe")
    if sufijo not in EXTENSIONES_ENTRADA:
        soportadas = ", ".join(sorted(EXTENSIONES_ENTRADA))
        raise ImagenInvalida(str(entrada), f"formato no soportado. Se aceptan: {soportadas}")
    if sufijo in EXTENSIONES_VECTOR:
        try:
            png = resvg_py.svg_to_bytes(svg_path=str(entrada), background="#ffffff")
        except Exception as exc:
            raise ImagenInvalida(
                str(entrada), f"no se pudo rasterizar el SVG: {sin_ruta(exc)}"
            ) from exc
        # El `return` de esta rama salteaba el tope de abajo: un SVG que declara
        # 14000x14000 llegaba a 196 megapixeles y lo frenaba recien el guard
        # propio de Pillow, con otro limite (178 MP) y como error no tipado.
        # ⚠ Lo que sigue sin resolverse es que resvg YA reservo el bitmap antes
        # de esta linea: acota el limite y el tipo de error, no la asignacion.
        vectorial = Image.open(BytesIO(bytes(png)))
        _exigir_tamano(entrada, *vectorial.size)
        return vectorial
    if sufijo in EXTENSIONES_CAMARA and (crudo := _abrir_raw(entrada)) is not None:
        return crudo
    try:
        imagen = Image.open(entrada)
    except Exception as exc:
        raise ImagenInvalida(str(entrada), f"no se pudo abrir: {sin_ruta(exc)}") from exc
    _exigir_tamano(entrada, *imagen.size)
    return imagen


def tamano_de(ruta: Path) -> tuple[int, int] | None:
    """Ancho y alto de una imagen raster ya escrita, o `None` si no se puede leer.

    Existe para que la capa web pueda declarar a que tamaño quedo una salida sin
    importar Pillow ni abrir archivos por su cuenta: la frontera dice que `app/`
    orquesta y `cutter3d/` es el unico que sabe de imagenes.

    `Image.open` es lazy, asi que esto lee el header y no decodifica nada.
    """
    try:
        with Image.open(ruta) as imagen:
            return (imagen.width, imagen.height)
    except (OSError, ValueError):
        return None


def tamano_de_trabajo(ancho: int, alto: int) -> tuple[int, int]:
    """Con que tamaño se va a procesar una imagen de `ancho` x `alto`.

    Es publica para que el llamador pueda **declarar** la reduccion sin
    decodificar nada: con el header de la imagen alcanza. Es la unica cuenta del
    presupuesto, y `_acotar_a_presupuesto` la usa tambien — dos formas de
    calcular lo mismo se desincronizan.
    """
    pixeles = ancho * alto
    if pixeles <= MAX_PIXELES_TRABAJO:
        return ancho, alto
    factor = (MAX_PIXELES_TRABAJO / pixeles) ** 0.5
    return max(1, int(ancho * factor)), max(1, int(alto * factor))


def _acotar_a_presupuesto(imagen: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    """Reduce la imagen a `MAX_PIXELES_TRABAJO` si se pasa. Devuelve (imagen, original).

    El segundo elemento es SIEMPRE el tamaño con el que llego, se haya reducido
    o no: es lo que deja declarar la reduccion sin que el llamador tenga que
    acordarse de medir antes.

    Se reduce con Lanczos y respetando la relacion de aspecto. No es una
    optimizacion oportunista: es lo que evita que una imagen legitima —un
    escaneo a 1200 DPI, una foto de 61 MP— pida mas memoria de la que la
    instancia tiene y se lleve puesto el servidor entero.
    """
    original = (imagen.width, imagen.height)
    destino = tamano_de_trabajo(*original)
    if destino == original:
        return imagen, original
    # `draft` decodifica el JPEG directo a escala 1/2, 1/4 u 1/8 —lo hace el
    # decodificador, sin materializar el bitmap completo— y es un no-op en los
    # demas formatos. Sin esto el presupuesto llegaba tarde: medido sobre un JPEG
    # de 48 MP, abrirlo y reducirlo picaba 441 MB **antes** de que F2 empezara,
    # porque `resize` necesita la imagen entera decodificada primero.
    imagen.draft(None, destino)
    if (imagen.width, imagen.height) == destino:
        return imagen, original
    return imagen.resize(destino, Image.Resampling.LANCZOS), original


def _aplanar_sobre_blanco(imagen: Image.Image) -> Image.Image:
    """JPEG no tiene canal alfa: lo transparente se compone sobre blanco.

    El `paste` con la mascara alfa hace la misma cuenta que
    `Image.alpha_composite` —`salida = color*alfa + blanco*(1-alfa)`— sobre un
    fondo que ya es opaco, pero con **dos** buffers en vez de cuatro: la version
    anterior materializaba el RGBA de la entrada, un fondo RGBA entero, el
    resultado de la composicion y recien despues el RGB final. A 16 MP eso es la
    diferencia entre 112 MB y 240 MB de pico, por una imagen con transparencia.
    """
    if imagen.mode in ("RGBA", "LA") or (imagen.mode == "P" and "transparency" in imagen.info):
        rgba = imagen.convert("RGBA")
        fondo = Image.new("RGB", rgba.size, (255, 255, 255))
        fondo.paste(rgba, mask=rgba.getchannel("A"))
        return fondo
    return imagen.convert("RGB")


def convertir_a_jpg(entrada: Path, salida: Path, calidad: int = CALIDAD_JPG) -> Path:
    """F1 — cualquier imagen soportada a jpg.

    El `try` envuelve TODO y no solo la apertura: `Image.open` es lazy, asi que
    los errores de decodificacion de verdad —archivo truncado, bomba de
    descompresion, fallo de libheif— saltan recien al TOCAR los pixeles, en
    `_aplanar_sobre_blanco` y en `save`. Sin esto escapan como excepcion no
    tipada y el llamador web las convierte en un 500 en vez de un 422, dejando
    ademas el trabajo colgado en `en_cola` hasta el TTL.
    """
    try:
        with _abrir_como_pil(entrada) as imagen:
            acotada, _ = _acotar_a_presupuesto(imagen)
            rgb = _aplanar_sobre_blanco(acotada)
            salida.parent.mkdir(parents=True, exist_ok=True)
            rgb.save(salida, format="JPEG", quality=calidad, subsampling=0)
    except Cutter3DError:
        raise
    except Exception as exc:
        raise ImagenInvalida(str(entrada), f"no se pudo convertir: {sin_ruta(exc)}") from exc
    return salida


def preparar_para_vectorizar(entrada: Path, destino_png: Path) -> Path:
    """Deja la imagen en algo que vtracer sepa leer, con el tope ya aplicado.

    vtracer trae su propio decodificador (el crate `image` de Rust), asi que
    dandole el archivo original se esquivaba `MAX_PIXELES` por completo — era el
    unico camino de decodificacion sin ningun guard. Pasando por `_abrir_como_pil`
    el tope vale para los tres decodificadores y no solo para dos.

    Y resuelve de paso un agujero funcional: el crate `image` **no lee** HEIC,
    AVIF ni RAW de camara. Con esos formatos vtracer no devolvia un error, sino
    que **paniqueaba en Rust**, y `pyo3` convierte los panics en `PanicException`,
    que hereda de `BaseException` y **no** de `Exception`: el `except Exception`
    de `a_svg` no lo veia y salia un 500.

    Un PNG se devuelve **sin tocar**. No es una optimizacion: es el camino de F2,
    cuyo contrato exige que la imagen binarizada llegue a vectorizar con sus dos
    valores exactos, y re-codificarla seria arriesgar eso por nada.
    """
    with _abrir_como_pil(entrada) as imagen:
        dentro_del_presupuesto = imagen.width * imagen.height <= MAX_PIXELES_TRABAJO
        if entrada.suffix.lower() == ".png" and dentro_del_presupuesto:
            return entrada
        # El atajo del PNG vale solo mientras el archivo entre en el presupuesto.
        # Si no, hay que reducirlo igual: vtracer decodifica por su cuenta, en
        # Rust, y esa memoria la cuenta el cgroup lo mismo que la de Python.
        acotada, _ = _acotar_a_presupuesto(imagen)
        destino_png.parent.mkdir(parents=True, exist_ok=True)
        _aplanar_sobre_blanco(acotada).save(destino_png, format="PNG")
    return destino_png


def _ancho_trazo_px(tinta: Mascara) -> float:
    """Ancho mediano del trazo, en pixeles, medido sobre el propio dibujo."""
    ejes, distancia = medial_axis(tinta, return_distance=True, rng=SEMILLA_EJE)
    valores = 2.0 * np.asarray(distancia)[np.asarray(ejes, dtype=bool)]
    return float(np.median(valores)) if valores.size else 1.0


def _macizos(tinta: Mascara, ancho_px: float) -> Mascara:
    """Zonas donde entra un disco de `FACTOR_MACIZO` anchos de trazo.

    El criterio de "macizo" tiene un solo dueño y es esta funcion: la usan el
    contorneado —que las reemplaza por su anillo— y la normalizacion —que las
    respeta cuando el contorneado esta apagado—. Con dos copias, apagar el
    contorneado y encender la normalizacion clasificaria distinto la misma
    mancha segun quien preguntara.

    Es la apertura morfologica por un disco de ese radio, calculada con dos
    transformadas de distancia y no con `opening(tinta, disk(r))`: el footprint
    cuesta O(pixeles x r^2) y el radio crece con la grilla — normalizando buzz a
    x5 da 14 sobre 2,6 MP, y con footprint el contorneado se llevaba **4,1 s de
    los 5,1** de toda la etapa (medido). La equivalencia es exacta y un test la
    fija pixel a pixel: erosionar por un disco de radio `r` es quedarse con los
    pixeles que no tienen fondo a distancia `<= r`, y dilatar es sumar los que
    tienen tinta a distancia `<= r`; las dos preguntas las responde la
    transformada en tiempo lineal, con el mismo tratamiento del borde de la
    imagen que skimage (afuera cuenta como tinta al erosionar y como fondo al
    dilatar).
    """
    radio_apertura = max(1, round(FACTOR_MACIZO * ancho_px))
    return _dilatar(_erosionar(tinta, radio_apertura), radio_apertura)


def _distancia_a(mascara: Mascara) -> npt.NDArray[np.float64]:
    """Distancia euclidea de cada pixel al pixel True mas cercano de `mascara`.

    Es el unico lugar que llama a `distance_transform_edt`, por una trampa que
    ya costo seis tests: la transformada mide la distancia al **cero** mas
    cercano, y si no hay ninguno **no falla — devuelve la distancia a un punto
    fantasma pegado a la esquina superior izquierda** (medido: sobre un 4x5 de
    unos da 1, 1,41, 2,24... desde (0, 0), o sea la distancia a (-1, 0)). Sobre un
    dibujo sin manchas macizas —o sea, todo line art puro— la erosion no deja
    nada, dilatar esa mascara vacia pintaba un cuarto de disco de "macizo" en la
    esquina superior izquierda, el contorneado lo convertia en un arquito de
    tinta que no estaba en el dibujo, y ese arquito corria la caja de la tinta
    y con ella la escala de la normalizacion. Sin un solo True la distancia es
    infinita, y eso es lo que se devuelve.
    """
    if not mascara.any():
        return np.full(mascara.shape, np.inf)
    return np.asarray(distance_transform_edt(~mascara), dtype=np.float64)


def _erosionar(mascara: Mascara, radio: int) -> Mascara:
    """Erosion por disco euclideo de `radio`, en tiempo lineal. Ver `_macizos`.

    Queda el pixel que no tiene fondo a distancia `<= radio`. Con la mascara
    llena la distancia al fondo es infinita y no se erosiona nada, que es lo
    mismo que hace skimage al rellenar el borde con tinta.
    """
    return np.asarray(_distancia_a(~mascara) > radio, dtype=bool)


def _dilatar(mascara: Mascara, radio: int) -> Mascara:
    """Dilatacion por disco euclideo de `radio`, en tiempo lineal. Ver `_macizos`.

    Entra el pixel que tiene tinta a distancia `<= radio`. Con la mascara vacia
    la distancia es infinita y no entra ninguno.
    """
    return np.asarray(_distancia_a(mascara) <= radio, dtype=bool)


def _contornear_macizos(tinta: Mascara, ancho_px: float) -> tuple[Mascara, int, int]:
    """Reemplaza cada zona maciza por un anillo del ancho de trazo del dibujo."""
    macizos = _macizos(tinta, ancho_px)
    if not macizos.any():
        return tinta, 0, 0
    nucleo = _erosionar(macizos, max(1, round(ancho_px)))
    anillo = macizos & ~nucleo
    _, cantidad = label(macizos)
    resultado = (tinta & ~macizos) | anillo
    return resultado, int(cantidad), int(macizos.sum())


def _lado_mayor_tinta_px(tinta: Mascara) -> int:
    """Lado mayor de la caja de la tinta, en pixeles. 0 si no hay tinta.

    Sale de dos reducciones booleanas y no de `np.nonzero`: nonzero materializa
    dos arrays de indices con un elemento por pixel de tinta —a 3 MP con 6% de
    tinta son 3 MB que no hacen falta— y aca alcanza con saber en que filas y
    en que columnas hay algo.
    """
    filas = np.flatnonzero(np.any(tinta, axis=1))
    if filas.size == 0:
        return 0
    columnas = np.flatnonzero(np.any(tinta, axis=0))
    return int(max(filas[-1] - filas[0], columnas[-1] - columnas[0])) + 1


def escala_px_por_mm(tinta: Mascara, lado_mayor_mm: float) -> float:
    """Cuantos pixeles de esta imagen va a medir un milimetro de la pieza final.

    F2 trabaja en pixeles y no sabe nada de milimetros. El puente es que F3
    escala el dibujo para que **su lado mayor** mida `lado_mayor_mm`, asi que la
    escala se deduce de la caja de la tinta y no del lienzo: el margen blanco
    que rodea al dibujo no viaja a la pieza y contarlo achicaria el objetivo.

    Devuelve 0 si no hay tinta — un lienzo en blanco no tiene escala.
    """
    lado_px = _lado_mayor_tinta_px(tinta)
    return lado_px / lado_mayor_mm if lado_px else 0.0


def factor_de_ampliacion(tinta: Mascara, objetivo_px: float) -> int:
    """Por cuanto ampliar para que el objetivo llegue a `ANCHO_MINIMO_NORMALIZACION_PX`.

    Entero, `>= 1`, y acotado por `MAX_PIXELES_TRABAJO`: se amplia lo que hace
    falta y no mas, y nunca por encima del presupuesto. Cuando el presupuesto no
    alcanza para llegar al minimo se amplia hasta donde se pueda — un objetivo
    de 9 px es peor que uno de 16 pero mejor que uno de 3, y el redondeo del
    medio ancho en `_normalizar_trazo` sigue cubriendo el caso degradado.

    Es publica porque la capa web y los tests necesitan la misma cuenta para
    volver las medidas a la escala original, y dos cuentas se desincronizan.
    """
    if objetivo_px <= 0:
        return 1
    necesario = math.ceil(ANCHO_MINIMO_NORMALIZACION_PX / objetivo_px)
    permitido = int(math.sqrt(MAX_PIXELES_TRABAJO / tinta.size))
    return max(1, min(necesario, permitido))


def _ampliar_para_normalizar(
    gris: Image.Image, tinta: Mascara, corte: int, dimensiones: CutterParams
) -> tuple[Mascara, int]:
    """La mascara sobre la que se va a normalizar, y por cuanto se amplio.

    Se amplia desde los **grises**, no desde la mascara binaria: el antialiasing
    del JPG guarda en cada pixel gris del borde a que altura pasa el borde de
    verdad, y la interpolacion devuelve un contorno en esa posicion sub-pixel,
    que el umbral habia tirado. Ampliar la mascara ya binaria solo agrandaria
    la escalera.

    ⚠ **El nivel al que se corta la imagen ampliada NO es `corte`.** `corte`
    responde *que* pixel es tinta; aca la pregunta es otra: *por donde pasa el
    borde* entre un pixel de tinta y uno de papel. La rampa interpolada entre
    los dos cruza el **punto medio de sus dos niveles** exactamente en el
    limite entre ambos pixeles, que es donde esta el borde de la mascara
    nativa. Cortar en `corte` corre ese borde: sobre un line art puro guardado
    como JPG, Otsu cae en 3 —el histograma es 0 y 255 y casi nada en el
    medio— y la rampa 0→255 cruza el 3 pegada al centro del pixel de tinta,
    asi que cada linea perdia medio pixel por lado (2/4/6 px salian 1/3/5) y
    el detector de macizos, calibrado con esa mediana achicada, clasificaba
    mal. Con el punto medio salen 2/4/6 exactos. Sobre una foto real Otsu ya
    esta cerca del medio (139 contra 139 en `tests/buzz-lightyear.jpg`) y las
    dos formas coinciden.

    Bicubico y no Lanczos ni bilineal, medido sobre la dispersion del ancho
    normalizado en el fixture de la onda (p95-p5 sobre p50): **bicubico 0,056**,
    bilineal 0,153, Lanczos 0,359 — los lobulos negativos de Lanczos dejan un
    halo alrededor de cada linea que ensucia el borde. En buzz los tres empatan.
    """
    objetivo = dimensiones.ancho_trazo_mm * escala_px_por_mm(tinta, dimensiones.lado_mayor_mm)
    factor = factor_de_ampliacion(tinta, objetivo)
    if factor == 1:
        return tinta, 1
    grises = np.asarray(gris, dtype=np.uint8)
    nivel_del_borde = (float(grises[tinta].mean()) + float(grises[~tinta].mean())) / 2.0
    ampliada = gris.resize((gris.width * factor, gris.height * factor), Image.Resampling.BICUBIC)
    tinta_ampliada: Mascara = np.asarray(ampliada, dtype=np.uint8) <= nivel_del_borde
    return tinta_ampliada, factor


def _podar_espigas(ejes: Mascara, pasos: int) -> Mascara:
    """Saca las puntas del eje `pasos` veces, sin borrar ninguna pieza entera.

    El eje medial de un trazo real no es una curva limpia: cada irregularidad
    del borde —y un JPG de line art las tiene por todos lados— le cuelga una
    **espiga**, una ramita de dos o tres pixeles. Medido sobre buzz: 172
    bifurcaciones y 34 puntas en un dibujo de 13 piezas. Al reconstruir, cada
    espiga se convierte en un bulto del ancho del trazo pegado al costado de la
    linea, que es la mitad del "ruido" que se ve en el resultado.

    Podar con `pasos = medio_ancho` es la eleccion natural y no un numero al
    tanteo: una espiga mas corta que el radio de reconstruccion queda **adentro**
    del disco que dibuja el trazo del que cuelga, asi que no aporta forma, solo
    bulto. Y por el mismo motivo la poda no acorta los trazos: le come
    `medio_ancho` pixeles a cada punta, y la reconstruccion se los devuelve al
    apoyar ahi el disco de radio `medio_ancho`.

    ⚠ **Lo que si puede pasar es que una pieza entera sea mas corta que la poda**
    —un punto, una pestaña, una tilde— y desaparezca. Eso seria perder arte en
    silencio, que es justo lo que este proyecto no se permite, asi que las piezas
    que se quedarian sin un solo pixel se restauran **completas**: sus espigas
    son tan chicas como ellas y no hay bulto que sacar.
    """
    if pasos < 1:
        return ejes
    etiquetas, cuantas = label(ejes, structure=VECINDAD)
    podados = ejes
    for _ in range(pasos):
        cuenta = convolve(podados.astype(np.uint8), VECINDAD, mode="constant")
        podados = podados & (cuenta > CUENTA_DE_PUNTA)
    sobrevive = np.zeros(cuantas + 1, dtype=bool)
    sobrevive[etiquetas[podados]] = True
    sobrevive[0] = True  # el fondo no es una pieza
    return np.asarray(podados | (ejes & ~sobrevive[etiquetas]), dtype=bool)


def _limar(tinta: Mascara) -> Mascara:
    """Mayoria 3x3: deja el pixel si el o la mayoria de sus 8 vecinos son tinta.

    Es lo que saca el serrucho. La reconstruccion apoya discos **discretos**
    sobre un eje que avanza en escalera, y con radios chicos ese disco no es
    redondo: a radio 1 la bola euclidea son 5 pixeles en cruz, asi que una linea
    diagonal sale dentada y con el borde deshilachado. El filtro de mayoria es
    la forma barata de volver a apoyar esa figura sobre la grilla midiendo area
    en vez de centros.

    No adelgaza el trazo: en una banda de ancho 3 la fila del medio cuenta 9 y
    las de los costados 6, las dos por encima del umbral. Lo que se lleva son los
    pixeles sueltos del borde —que cuentan 3 o 4— y de paso **rellena** la
    diagonal, que es de donde sale la mejora mas grande: en `murcielago.jpg` el
    percentil 5 del ancho pasa de 2,0 a 6,3 px con un objetivo de 7,26.

    Redondea las esquinas vivas en un pixel. Se acepta a sabiendas: entra en el
    neto que `area_engrosada_px` y `area_afinada_px` ya declaran, porque esos dos
    se cuentan contra la tinta original y al final de todo, no paso por paso.
    """
    cuenta = convolve(tinta.astype(np.uint8), VECINDAD, mode="constant")
    return np.asarray(cuenta >= MAYORIA_DE_LA_VENTANA, dtype=bool)


@dataclass(frozen=True)
class _Normalizacion:
    """Lo que `_normalizar_trazo` devuelve, junto.

    Son cinco valores que salen de la misma pasada y que despues hay que
    declarar de a uno; devolverlos en una tupla de cinco obliga al llamador a
    acordarse del orden, que es la forma mas barata de escribir un numero en el
    campo de otro.

    Los cuatro contadores arrancan en cero para que el camino sin normalizar
    pueda construir el mismo objeto con la tinta sola, en vez de arrastrar
    cuatro variables sueltas hasta el `return`.
    """

    tinta: Mascara
    ancho_logrado_px: int = 0
    engrosada: int = 0
    afinada: int = 0
    protegida: int = 0


def _normalizar_trazo(
    tinta: Mascara, objetivo_px: float, protegido: Mascara | None
) -> _Normalizacion:
    """Deja todos los trazos al mismo ancho, reconstruyendolos desde su eje medial.

    El procedimiento es el inverso exacto de la medicion: la tinta es la union
    de los discos inscritos centrados en el eje medial, cada uno con **su**
    radio local. Redibujarla con un radio **unico** da la misma figura con todos
    los trazos al mismo ancho — engorda los finos y afina los gruesos, que es
    justo lo que la dilatacion uniforme de F3 no puede hacer.

    ⚠ **Los anchos alcanzables son impares y nada mas.** El eje medial es una
    curva de un pixel de ancho, asi que reconstruirlo a distancia `k` deja una
    banda de `2k + 1` pixeles: 1, 3, 5, 7... Por eso el medio ancho se **redondea
    al entero mas cercano** en vez de truncarse. Truncar parece conservador y no
    lo es: con un objetivo de 2,89 px —un dibujo de 260 px de lado para una
    pieza de 90 mm, que es de lo mas comun— el medio ancho da 0,94, trunca a 0 y
    el dibujo entero sale en **1 pixel de ancho**, o sea el esqueleto pelado. El
    ancho que realmente se logro se devuelve y se declara: pedir 2,89 y entregar
    3 no es lo mismo que pedir 2,89.

    `protegido` son las zonas que NO son trazo y hay que dejar como estan. Sin
    eso, el eje medial de una mancha maciza es un arbolito y reconstruirlo con
    radio fijo la convierte en una figura de palitos: exactamente lo que el
    usuario evita cuando apaga el contorneado.

    La reconstruccion va por transformada de distancia y no por un `dilation`
    con footprint de disco: el radio depende de la escala del dibujo, y un
    footprint de radio 20 px sobre 3 MP son 5.000 millones de operaciones. La
    transformada es O(pixeles) sin importar el radio.

    ⚠ **Reconstruir a secas sale ruidoso, y hacen falta los tres pasos.** Son el
    eje podado (`_podar_espigas`), la reconstruccion, y la limada
    (`_limar`) —cada uno saca una parte distinta del ruido y ninguno reemplaza
    al otro—. Sin ellos la linea sale dentada y con bultos, y eso viaja: el SVG
    que vtracer saca de la version cruda tiene **+22% de nodos en buzz, +28% en
    calabaza y +46% en murcielago** contra el mismo dibujo sin normalizar, y
    cada nodo de mas es un movimiento que la impresora sigue. Con los tres pasos
    el exceso baja a **+9%, +20% y +20%**. El detalle de cual saca que esta en
    cada funcion.
    """
    medio_ancho = max(0, round((objetivo_px - 1.0) / 2.0))
    ejes = _podar_espigas(np.asarray(medial_axis(tinta, rng=SEMILLA_EJE), dtype=bool), medio_ancho)
    nueva: Mascara = _distancia_a(ejes) <= medio_ancho
    if medio_ancho >= 1:
        # Con `medio_ancho` 0 la reconstruccion ES el eje pelado, de un pixel, y
        # limar un pixel suelto lo borra. Ese caso ya esta degradado de por si
        # (el objetivo no llega a 1,5 px) y no hay serrucho que sacarle.
        nueva = _limar(nueva)
    protegida = 0
    if protegido is not None:
        nueva |= protegido
        protegida = int(np.count_nonzero(protegido))
    return _Normalizacion(
        tinta=nueva,
        ancho_logrado_px=2 * medio_ancho + 1,
        engrosada=int(np.count_nonzero(nueva & ~tinta)),
        afinada=int(np.count_nonzero(tinta & ~nueva)),
        protegida=protegida,
    )


def preparar_lineas(
    jpg: Path,
    contornear_macizos: bool = True,
    umbral: int | None = None,
    *,
    normalizar_trazo: bool = False,
    ancho_trazo_mm: float = DIMENSIONES.ancho_trazo_mm,
    lado_mayor_mm: float = DIMENSIONES.lado_mayor_mm,
) -> ResultadoF2:
    """F2 — jpg a blanco y negro puro de lineas.

    `contornear_macizos` viene en True y `normalizar_trazo` en False, las dos
    por decision explicita del usuario. Son las dos modificaciones del arte que
    esta etapa puede hacer, y el resultado **siempre declara** cuanto toco cada
    una: cuantas zonas y que area el contorneado, cuantos pixeles engordo y
    cuantos afino la normalizacion.

    `ancho_trazo_mm` y `lado_mayor_mm` solo se usan con `normalizar_trazo`
    encendido, pero se validan siempre: son dimensiones del producto y sus
    limites los pone `CutterParams`, aca igual que en F3 y sin una segunda tabla.

    ⚠ **El orden no es intercambiable: primero se contornea y despues se
    normaliza.** Al reves, el anillo saldria del ancho viejo y habria que
    normalizarlo de nuevo; y peor, una mancha maciza pasaria por la
    normalizacion —que la reduciria a una figura de palitos— antes de que el
    contorneado llegue a reconocerla como mancha.
    """
    # No se revalidan los limites aca: `CutterParams` es su unico dueño y lo que
    # levanta (`ParametroFueraDeRango`, con el nombre del campo adentro) es lo
    # que la capa web ya sabe traducir a un 422.
    dimensiones = CutterParams(ancho_trazo_mm=ancho_trazo_mm, lado_mayor_mm=lado_mayor_mm)
    if jpg.suffix.lower() not in (".jpg", ".jpeg", ".jfif"):
        raise ImagenInvalida(str(jpg), "esta etapa recibe JPG si o si")
    with _abrir_como_pil(jpg) as imagen:
        if imagen.format != "JPEG":
            raise ImagenInvalida(str(jpg), f"esta etapa recibe JPG si o si (es {imagen.format})")
        # El presupuesto se aplica ACA, antes de `_ancho_trazo_px`: el pico de
        # esta funcion es el float64 de `medial_axis`, y a partir de este punto
        # todo lo que se reserva es proporcional al tamaño de `grises`.
        acotada, tamano_original = _acotar_a_presupuesto(imagen)
        gris = acotada.convert("L")
    grises = np.asarray(gris, dtype=np.uint8)

    corte = int(threshold_otsu(grises)) if umbral is None else int(umbral)
    tinta: Mascara = grises <= corte

    # La ampliacion va ANTES de medir el ancho y de contornear: de aca en
    # adelante toda la etapa trabaja en la grilla ampliada, y `ancho_trazo_px`,
    # los anillos del contorneado y las areas declaradas salen en esa escala.
    factor = 1
    if normalizar_trazo and tinta.any():
        tinta, factor = _ampliar_para_normalizar(gris, tinta, corte, dimensiones)

    ancho_px = _ancho_trazo_px(tinta) if tinta.any() else 1.0
    zonas = 0
    area = 0
    if contornear_macizos and tinta.any():
        tinta, zonas, area = _contornear_macizos(tinta, ancho_px)

    objetivo_px = 0.0
    normalizado = _Normalizacion(tinta)
    if normalizar_trazo and tinta.any():
        objetivo_px = dimensiones.ancho_trazo_mm * escala_px_por_mm(
            tinta, dimensiones.lado_mayor_mm
        )
        # Las manchas macizas se protegen SOLO cuando el contorneado no corrio.
        # Si corrio ya son anillos —o sea, trazo— y protegerlas ahi las dejaria
        # con el ancho viejo, que es justo lo que se vino a corregir.
        protegido = None if contornear_macizos else _macizos(tinta, ancho_px)
        normalizado = _normalizar_trazo(tinta, objetivo_px, protegido)
        tinta = normalizado.tinta

    salida = np.where(tinta, np.uint8(0), np.uint8(255)).astype(np.uint8)
    return ResultadoF2(
        imagen=salida,
        umbral_usado=corte,
        ancho_trazo_px=ancho_px,
        zonas_contorneadas=zonas,
        area_contorneada_px=area,
        contorneado_activo=contornear_macizos,
        tamano_original=tamano_original,
        tamano_usado=(int(salida.shape[1]), int(salida.shape[0])),
        factor_ampliacion=factor,
        normalizacion_activa=normalizar_trazo,
        ancho_objetivo_px=objetivo_px,
        ancho_logrado_px=normalizado.ancho_logrado_px,
        ancho_objetivo_mm=dimensiones.ancho_trazo_mm if normalizar_trazo else 0.0,
        lado_mayor_supuesto_mm=dimensiones.lado_mayor_mm if normalizar_trazo else 0.0,
        area_engrosada_px=normalizado.engrosada,
        area_afinada_px=normalizado.afinada,
        area_protegida_px=normalizado.protegida,
    )


def guardar_binaria(resultado: ResultadoF2, salida: Path) -> Path:
    """Guarda en PNG. Nunca en JPG: la compresion volveria a meter grises."""
    if salida.suffix.lower() != ".png":
        raise ImagenInvalida(str(salida), "la salida de la correccion de lineas va en PNG")
    salida.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(resultado.imagen, mode="L").save(salida, format="PNG", optimize=True)
    return salida


def guardar_editable(resultado: ResultadoF2, salida: Path) -> Path:
    """La copia en JPG para retocar a mano. NO es la salida de la etapa.

    Lo que sigue en el flujo —vectorizar, y de ahi el cortante— lee el PNG de
    `guardar_binaria`, y eso no cambia: el contrato dice que la compresion JPG
    ensucia el borde. Esta copia existe para otra cosa: el usuario abre la
    correccion en Paint, arregla lo que la imagen traia mal, y la vuelve a subir
    a Correcto — que acepta **solo JPG**. Bajar el PNG lo obligaba a convertir.

    Va con la misma calidad y sin submuestreo de croma que el Convertidor
    (`CALIDAD_JPG`, `subsampling=0`), y sobre una imagen de dos valores el ruido
    de compresion queda a decenas de niveles de un umbral: re-umbralizarla
    devuelve exactamente la misma mascara, y hay un test que lo fija.
    """
    if salida.suffix.lower() not in (".jpg", ".jpeg"):
        raise ImagenInvalida(str(salida), "la copia editable va en JPG")
    salida.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(resultado.imagen, mode="L").save(
        salida, format="JPEG", quality=CALIDAD_JPG, subsampling=0
    )
    return salida
