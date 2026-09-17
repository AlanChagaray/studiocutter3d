"""La lamina del set: las fotos de varios diseños pegadas en una sola imagen.

El navegador rinde una celda por diseño —cada una es el mismo JPG cenital de
siempre, con su estudio de luces y su encuadre— y este modulo las pega en una
grilla. **No rinde nada en 3D**: compone imagenes ya hechas, que es justo lo que
permite que el set exista sin una segunda pila grafica.

Que la composicion viva del lado del servidor y no en el canvas es lo que la hace
**medible**: la grilla, la separacion, el lado y el fondo de los huecos se
afirman desde la suite. El comportamiento del JS no lo mira ningun gate de este
proyecto, asi que mover aca lo que se puede medir es ganar red de regresion.

## Las dos decisiones que no se deducen leyendo el codigo

1. **El color de los huecos es un PARAMETRO, y lo era al reves.**

   Durante un ciclo se leia de la esquina de la primera foto, con un argumento
   que era correcto mientras duro: las fotos traen su fondo pegado de borde a
   borde, asi que el color ya estaba ahi y pedirlo aparte creaba dos verdades
   sobre el mismo color.

   Dejo de valer cuando **cada diseño paso a elegir su propio fondo**. Con
   fondos mezclados no hay "el fondo de las fotos" que leer: hay hasta
   veinticinco, y tomar el de la primera es elegir uno por sorteo y pintar con
   el los huecos de las otras veinticuatro. Deja de haber una verdad que
   descubrir y pasa a haber una decision que tomar, asi que la toma el usuario
   —el set tiene su propia muestra— y llega hasta aca como argumento.

   Sigue sin haber dos verdades: ahora hay **una sola y esta afuera**. Este
   modulo no inventa un color por las suyas, y por eso `fondo` es obligatorio y
   no tiene default; el default de la pantalla vive en la paleta, que es donde
   el usuario lo ve.

   ⚠ Las fotos que entran aca **no son las que el usuario se baja sueltas**.
   Cada diseño se rinde dos veces: una con los colores que se le eligieron a el
   y otra con los del set, y esta segunda (`ClaveDiseno.JPG_SET`) es la celda.
   El `fondo` que llega es el mismo que el navegador uso de fondo al rendirla,
   asi que el hueco entre celdas y el fondo de adentro de cada celda son el
   mismo color y la lamina se lee como una imagen sola. Que este modulo no
   sepa nada de eso es a proposito: recibe imagenes y un color, y las pega.

2. **Las celdas se achican al abrir, no despues.** `Image.draft()` le pide al
   decodificador JPEG la imagen ya reducida a 1/2, 1/4 u 1/8; abrir 25 fotos de
   2048 px enteras y recien despues achicarlas serian 314 MB de pico contra los
   ~20 MB que cuesta asi. Es la misma leccion del presupuesto de pixeles de F2,
   y por eso se resuelve igual.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .errors import ImagenInvalida

LADO_MAX = 2048
"""Ancho de la lamina. El mismo que el de una foto suelta, a proposito: es el
tope seguro de `max_texture_size` de cualquier GPU con WebGL y alcanza para
imprimir en A5, y mantener el mismo numero evita que el set y sus partes se
vayan separando."""

SEPARACION_REL = 0.02
"""Aire entre celdas, como fraccion del ancho de la lamina. 41 px sobre 2048.

Es una separacion y no un marco: lo que se pidio es que las piezas esten juntas
en una imagen pero separadas entre si, no una grilla con bordes."""

SEPARACION_FILAS_REL = SEPARACION_REL * 0.9
"""Aire entre FILAS, un 10% menos que el de las columnas. 37 px sobre 2048.

Eran una sola separacion y se partieron por como se lee la lamina, no por
geometria: con celdas cuadradas y piezas mas anchas que altas, lo que separa dos
filas es el hueco MAS el fondo de arriba y de abajo de cada foto, asi que a igual
cantidad de pixeles el aire vertical se ve mayor que el horizontal. Bajarlo un
10% lo compensa de a poco, sin que las filas se toquen.

Es el unico numero que hay que mover si las filas siguen quedando lejos."""

MAX_CELDAS = 25
"""El mismo techo que `app.archivos.MAX_DISENOS`. Existe aca ademas de alla
porque este modulo tambien se puede llamar desde el CLI y desde un test, y un
techo que solo vive en la capa web no es un techo del dominio."""

CALIDADES = (0.92, 0.86, 0.78, 0.7, 0.6)
"""Escalera de calidad JPEG, de mayor a menor. La misma idea que en
`preview3d.js`: se prueba de mejor a peor y se devuelve la primera que entra en
`TOPE_BYTES`."""

TOPE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ReporteLamina:
    """Lo que se puede afirmar de la lamina, medido sobre el archivo escrito."""

    celdas: int

    distribucion: tuple[int, ...]
    """Cuantas fotos quedaron en cada fila. `(3, 2, 2)` para un set de siete.

    Es el dato de verdad del armado; `columnas` y `filas` son derivados y estan
    para poder decir "3x3" de un vistazo. Con filas desparejas, ese par **no
    describe** el reparto: 7 fotos y 9 dan las dos `3x3`."""

    columnas: int
    """La fila mas larga."""

    filas: int
    lado_celda_px: int

    separacion_px: int
    """El hueco entre columnas. El de las filas es mas chico, ver
    `SEPARACION_FILAS_REL`."""

    separacion_filas_px: int

    tamano_px: tuple[int, int]
    """Alto y ancho de la lamina. **Siempre iguales**: ver `_medidas`."""

    bytes_: int


def distribucion_de(celdas: int) -> tuple[int, ...]:
    """Cuantas fotos van en cada fila, de arriba hacia abajo.

    **No es una grilla rectangular: las filas pueden tener cantidades distintas**,
    y eso es lo que llena el cuadro. Una grilla pareja obliga a que la ultima fila
    quede corta —7 fotos en 3 columnas son `3-3-1`, con dos huecos juntos abajo—
    mientras que repartir las mismas 7 en `3-2-2` deja tres filas equilibradas y
    la mancha ocupa el cuadrado.

    La regla es una sola: **tantas filas como el lado del cuadrado mas cercano**
    (`round(sqrt(n))`), y las fotos repartidas lo mas parejo posible, con las
    filas mas largas arriba. De ahi salen los casos del pedido:

    | fotos | filas | reparto |
    |---|---|---|
    | 3 | 2 | `2-1` |
    | 5 | 2 | `3-2` |
    | 7 | 3 | `3-2-2` |
    | 9 | 3 | `3-3-3` |

    Es `round` y no `ceil`: con `ceil`, 5 fotos darian 3 filas (`2-2-1`), mas alto
    que ancho y con la mancha estirada. Se escribe `int(x + 0.5)` en vez de
    `round` porque `round` de Python redondea al par en los empates; aca no hay
    empates posibles —`sqrt(n)` nunca cae justo en `.5` para `n` entero— pero
    depender de que no los haya es depender de una casualidad.

    Las filas cortas se centran (ver `_origen`), asi que el faltante se lee como
    una decision y no como una foto que no entro.
    """
    if celdas < 1:
        raise ImagenInvalida("(lamina)", "no hay ninguna foto para armar el set")
    if celdas > MAX_CELDAS:
        raise ImagenInvalida("(lamina)", f"el set admite hasta {MAX_CELDAS} diseños")
    filas = int(math.sqrt(celdas) + 0.5)
    base, resto = divmod(celdas, filas)
    return tuple(base + 1 if i < resto else base for i in range(filas))


@dataclass(frozen=True)
class _Medidas:
    """La geometria de la lamina, en pixeles. Todo lo demas se deriva de esto."""

    distribucion: tuple[int, ...]
    columnas: int
    filas: int
    lado: int
    separacion: int
    separacion_filas: int

    lienzo: int
    """El lado de la lamina. **Es cuadrada siempre**: ver `_medidas`."""

    y0: int
    """Donde arranca la primera fila. Centra el bloque verticalmente."""


def _medidas(celdas: int) -> _Medidas:
    """Cuanto mide cada cosa. **La lamina es cuadrada, sin excepcion.**

    Que sea cuadrada es un requisito del uso, no una preferencia: un JPG mas
    ancho que alto, publicado en un marco cuadrado, sale con bandas arriba y
    abajo y **recortado de los costados**. Con la lamina ya cuadrada no hay nada
    que ajustar al postear.

    ⚠ **Con celdas cuadradas, un reparto que no es cuadrado no puede llenar un
    cuadrado.** Cinco fotos van en `3-2`: tres de ancho por dos de alto. Para que
    esas dos filas llegaran arriba y abajo, cada celda tendria que medir la mitad
    del lienzo, y entonces tres no entrarian a lo ancho. No hay forma de evitarlo
    —es geometria, no una decision— asi que el bloque se hace **lo mas grande que
    entra** (el lado sale de la dimension mas apretada, `max(columnas, filas)`) y
    se centra.

    Lo que sobra arriba y abajo **no se ve como banda**: se pinta del mismo
    color que los huecos entre celdas —el `fondo` que recibe `componer`, ver el
    punto 1 del docstring del modulo—. Queda como mas aire alrededor del set, no
    como un margen pegado.

    La separacion va solo ENTRE celdas: con `n` columnas hay `n - 1`
    separaciones, no `n + 1`. En la dimension que llena, las celdas llegan al
    filo del lienzo.
    """
    distribucion = distribucion_de(celdas)
    columnas = max(distribucion)
    filas = len(distribucion)
    separacion = round(LADO_MAX * SEPARACION_REL)
    separacion_filas = round(LADO_MAX * SEPARACION_FILAS_REL)
    # El lado sale de la dimension mas apretada, y cada eje se mide con SU
    # separacion: si se tomara solo el ancho, un reparto mas alto que ancho (una
    # columna de tres) se saldria por abajo.
    lado = min(
        (LADO_MAX - separacion * (columnas - 1)) // columnas,
        (LADO_MAX - separacion_filas * (filas - 1)) // filas,
    )
    ancho_bloque = columnas * lado + (columnas - 1) * separacion
    alto_bloque = filas * lado + (filas - 1) * separacion_filas
    # El lienzo es cuadrado y lo fija el bloque mas grande de los dos. Hoy es
    # siempre el ancho -en todo reparto `columnas >= filas`, y encima el hueco
    # horizontal es el mayor- pero tomar el maximo es lo que sostiene el cuadrado
    # si alguna de las dos cosas deja de valer.
    lienzo = max(ancho_bloque, alto_bloque)
    return _Medidas(
        distribucion=distribucion,
        columnas=columnas,
        filas=filas,
        lado=lado,
        separacion=separacion,
        separacion_filas=separacion_filas,
        lienzo=lienzo,
        y0=(lienzo - alto_bloque) // 2,
    )


def _celda(ruta: Path, lado: int) -> Image.Image:
    """Abre una foto ya reducida al tamaño de la celda. Ver el punto 2 de arriba."""
    try:
        imagen = Image.open(ruta)
        # ⚠ `draft` ANTES de tocar nada: es lo que evita decodificar 2048x2048
        # para despues tirar el 97% de los pixeles. Es un no-op fuera de JPEG.
        imagen.draft("RGB", (lado, lado))
        cuadro = imagen.convert("RGB")
        imagen.close()
    except OSError as exc:
        raise ImagenInvalida(str(ruta), "no se pudo leer la foto del diseño") from exc
    if cuadro.size != (lado, lado):
        cuadro = cuadro.resize((lado, lado), Image.Resampling.LANCZOS)
    return cuadro


def _origen(indice: int, m: _Medidas) -> tuple[int, int]:
    """Esquina superior izquierda de la celda `indice`, con su fila centrada.

    El indice es global —la foto numero 5 del set— y hay que ubicarlo en un
    reparto de filas desparejas, asi que se recorre restando: no hay division
    entera que sirva cuando las filas no miden todas lo mismo.

    **Se centra CADA fila corta, no solo la ultima.** Con el reparto en piramide
    puede haber varias (7 fotos son `3-2-2`: dos filas cortas), y alinearlas a la
    izquierda dejaria toda la falta de un lado, que es justo lo que se lee como
    error de armado.

    El bloque entero tambien va centrado en vertical (`m.y0`), que es lo que deja
    la lamina cuadrada sin que el set quede pegado arriba. Ver `_medidas`.

    **Los dos pasos no son iguales**: el vertical lleva la separacion de filas,
    que es mas chica. Ver `SEPARACION_FILAS_REL`.
    """
    paso_x = m.lado + m.separacion
    paso_y = m.lado + m.separacion_filas
    resto = indice
    for fila, cuantas in enumerate(m.distribucion):
        if resto < cuantas:
            ancho_fila = cuantas * m.lado + (cuantas - 1) * m.separacion
            x = (m.lienzo - ancho_fila) // 2 + resto * paso_x
            return x, m.y0 + fila * paso_y
        resto -= cuantas
    raise ImagenInvalida("(lamina)", f"la celda {indice} no entra en el reparto")


def componer(fotos: Sequence[Path], destino: Path, fondo: tuple[int, int, int]) -> ReporteLamina:
    """Pega las fotos en una grilla y escribe el JPG. Devuelve lo que se midio.

    Las fotos van **en el orden en que llegan**, que es el orden de los diseños
    en la pantalla: el usuario ve el set en el mismo orden en que armo la lista.

    `fondo` pinta los huecos entre celdas y el sobrante de arriba y abajo, y es
    **obligatorio a proposito**: ver el punto 1 del docstring del modulo. Puede
    no coincidir con el fondo de ninguna de las fotos, y eso no es un defecto
    sino el punto — con un fondo por diseño, "el fondo de las fotos" ya no
    existe como cosa unica.
    """
    if not fotos:
        raise ImagenInvalida("(lamina)", "no hay ninguna foto para armar el set")
    faltan = [f for f in fotos if not f.is_file()]
    if faltan:
        raise ImagenInvalida(str(faltan[0]), f"faltan {len(faltan)} fotos para armar el set")

    m = _medidas(len(fotos))
    lamina = Image.new("RGB", (m.lienzo, m.lienzo), fondo)

    for i, ruta in enumerate(fotos):
        celda = _celda(ruta, m.lado)
        lamina.paste(celda, _origen(i, m))
        # Una celda viva a la vez: es lo que mantiene el pico en el tamaño de la
        # lamina mas una celda, y no en el de las 25 fotos juntas.
        celda.close()

    destino.parent.mkdir(parents=True, exist_ok=True)
    escritos = _guardar(lamina, destino)
    lamina.close()

    return ReporteLamina(
        celdas=len(fotos),
        distribucion=m.distribucion,
        columnas=m.columnas,
        filas=m.filas,
        lado_celda_px=m.lado,
        separacion_px=m.separacion,
        separacion_filas_px=m.separacion_filas,
        tamano_px=(m.lienzo, m.lienzo),
        bytes_=escritos,
    )


def _guardar(lamina: Image.Image, destino: Path) -> int:
    """Escribe el JPG mas lindo que entre en `TOPE_BYTES`, o el mas chico si ninguno.

    Se escribe a un temporal y se renombra, como todo lo que este proyecto deja
    en disco: el destino puede ser un `set.jpg` vivo de una composicion anterior,
    y truncarlo antes de saber si la nueva sale deja una imagen a medias que se
    sirve como completa.
    """
    descriptor, temporal = tempfile.mkstemp(dir=destino.parent, prefix=".set-", suffix=".tmp")
    temporal_ruta = Path(temporal)
    try:
        with os.fdopen(descriptor, "wb") as salida:
            for calidad in CALIDADES:
                salida.seek(0)
                salida.truncate()
                lamina.save(salida, format="JPEG", quality=round(calidad * 100), optimize=True)
                if salida.tell() <= TOPE_BYTES:
                    break
            escritos = salida.tell()
        os.replace(temporal_ruta, destino)
    except BaseException:
        temporal_ruta.unlink(missing_ok=True)
        raise
    return escritos
