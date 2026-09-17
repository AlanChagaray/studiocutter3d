"""La lamina del set: las fotos de varios diseños en una sola imagen.

Este archivo existe por una razon concreta: **el set se compone del lado del
servidor justamente para poder afirmar esto**. Si lo armara el canvas, la grilla,
la separacion y el color de los huecos quedarian en el unico bloque del proyecto
que ningun gate mira. Aca son numeros.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops

from cutter3d.errors import ImagenInvalida
from cutter3d.lamina import (
    LADO_MAX,
    MAX_CELDAS,
    componer,
    distribucion_de,
)

FONDO = (244, 243, 240)
"""Un fondo de la paleta, no blanco puro: si fuera 255 un error de canal no se veria.

Se usa para las dos cosas que ahora son independientes —el fondo de cada foto y
el del set— en los tests a los que el color no les importa. Los que SI miran el
color usan dos distintos a proposito: ver
`test_el_fondo_del_set_no_sale_de_las_fotos`."""


def _parecido(leido: tuple[int, ...], esperado: tuple[int, int, int]) -> bool:
    """Igual salvo el ruido del JPEG. Comparar exacto seria un test que falla
    cuando cambia la version de Pillow y no cuando cambia la composicion."""
    return all(abs(a - b) <= 2 for a, b in zip(leido, esperado, strict=True))


def _foto(
    destino: Path,
    tono: int,
    fondo: tuple[int, int, int] = FONDO,
    *,
    pieza_completa: bool = False,
) -> Path:
    """Una foto cenital de mentira: fondo parejo con una pieza cuadrada al medio.

    Cuadrada y de 2048 px como las de verdad — `preview3d.js` rinde 1:1 a ese
    lado— para que lo que se mide sea la composicion y no un reescalado raro.

    `pieza_completa` la pinta de borde a borde: sirve para distinguir foto de
    fondo en las esquinas de la lamina, que es como se prueba que no quedo
    borde exterior.
    """
    color = (tono, 90, 200)
    imagen = Image.new("RGB", (2048, 2048), color if pieza_completa else fondo)
    if not pieza_completa:
        imagen.paste(Image.new("RGB", (1000, 1000), color), (524, 524))
    imagen.save(destino, format="JPEG", quality=92)
    return destino


def _fotos(tmp_path: Path, cuantas: int) -> list[Path]:
    return [_foto(tmp_path / f"vista-{i:02d}.jpg", 30 + i * 8) for i in range(cuantas)]


# ── La grilla ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("celdas", "esperado"),
    [
        (1, (1,)),
        (2, (2,)),
        (3, (2, 1)),
        (4, (2, 2)),
        (5, (3, 2)),
        (6, (3, 3)),
        (7, (3, 2, 2)),
        (8, (3, 3, 2)),
        (9, (3, 3, 3)),
        (12, (4, 4, 4)),
        (16, (4, 4, 4, 4)),
        (25, (5, 5, 5, 5, 5)),
    ],
)
def test_el_reparto_es_el_pedido(celdas: int, esperado: tuple[int, ...]) -> None:
    """Los cuatro casos del pedido, mas los que caen alrededor.

    3 en `2-1`, 5 en `3-2`, 7 en `3-2-2` y 9 en `3-3-3`: filas desparejas con las
    mas largas arriba, que es lo que llena el cuadro. Una grilla pareja daria
    `3-3-1` para siete — dos huecos juntos abajo.
    """
    assert distribucion_de(celdas) == esperado


def test_todas_las_fotos_entran_en_el_reparto() -> None:
    """Una celda que no entra es un diseño que desaparece del set sin avisar."""
    for celdas in range(1, MAX_CELDAS + 1):
        assert sum(distribucion_de(celdas)) == celdas, celdas


def test_las_filas_van_de_mayor_a_menor_y_no_difieren_en_mas_de_una() -> None:
    """Es lo que define "repartido lo mas parejo posible".

    Si dos filas difirieran en dos, mover una foto de la larga a la corta daria
    un armado mas equilibrado — o sea que el reparto no era el mejor.
    """
    for celdas in range(1, MAX_CELDAS + 1):
        reparto = distribucion_de(celdas)
        assert list(reparto) == sorted(reparto, reverse=True), celdas
        assert max(reparto) - min(reparto) <= 1, celdas


def test_ninguna_fila_queda_vacia() -> None:
    """Una fila de cero es una banda de fondo que nadie pidio."""
    for celdas in range(1, MAX_CELDAS + 1):
        assert min(distribucion_de(celdas)) >= 1, celdas


def test_el_reparto_es_lo_mas_cuadrado_que_se_puede() -> None:
    """La forma medible de "ocupa mejor el espacio cuadrado".

    Se compara contra TODOS los repartos parejos posibles —de 1 a n filas— y se
    exige que ninguno sea mas cuadrado que el elegido. Es mas fuerte que fijar
    los casos a mano: cubre los 25 y falla si alguien cambia la formula por una
    que anda solo en los ejemplos del pedido.
    """
    for celdas in range(1, MAX_CELDAS + 1):
        elegido = distribucion_de(celdas)
        propio = abs(max(elegido) - len(elegido))
        for filas in range(1, celdas + 1):
            columnas = -(-celdas // filas)  # ceil
            if columnas * (filas - 1) >= celdas:
                continue  # dejaria una fila vacia
            assert propio <= abs(columnas - filas), (celdas, elegido, filas)


@pytest.mark.parametrize("celdas", [0, -1, MAX_CELDAS + 1])
def test_una_cantidad_imposible_de_celdas_se_rechaza(celdas: int) -> None:
    with pytest.raises(ImagenInvalida):
        distribucion_de(celdas)


# ── La composicion ───────────────────────────────────────────────────────────


def test_la_lamina_no_tiene_borde_exterior(tmp_path: Path) -> None:
    """⚠ La separacion va solo ENTRE celdas, nunca alrededor.

    Un marco no separa nada de nada —afuera no hay otra foto— y lo unico que
    hace es achicar las piezas. La prueba directa: la esquina de la lamina tiene
    que ser **foto**, no fondo, cuando la primera fila esta completa.
    """
    fotos = [_foto(tmp_path / f"v{i}.jpg", 30, pieza_completa=True) for i in range(4)]
    reporte = componer(fotos, tmp_path / "set.jpg", FONDO)
    assert reporte.distribucion == (2, 2)
    # Con 2-2 el bloque llena el cuadro en las dos direcciones, asi que el lado
    # es exactamente celdas + separaciones y nada mas.
    assert reporte.tamano_px[0] == (
        reporte.columnas * reporte.lado_celda_px + (reporte.columnas - 1) * reporte.separacion_px
    )
    assert reporte.tamano_px[1] == reporte.tamano_px[0]
    with Image.open(tmp_path / "set.jpg") as imagen:
        rgb = imagen.convert("RGB")
        for punto in ((1, 1), (rgb.width - 2, 1), (1, rgb.height - 2)):
            leido = rgb.getpixel(punto)
            assert not _parecido(leido, FONDO), f"{punto} es fondo: quedo borde exterior"


@pytest.mark.parametrize("celdas", list(range(1, MAX_CELDAS + 1)))
def test_la_lamina_siempre_es_cuadrada(tmp_path: Path, celdas: int) -> None:
    """**El requisito duro de esta imagen**, y por eso se prueba en los 25.

    Un JPG mas ancho que alto, publicado en un marco cuadrado, sale con bandas
    arriba y abajo y recortado de los costados. Con la lamina ya cuadrada no hay
    nada que ajustar al postear — y eso no puede depender de cuantos diseños
    entraron, que es justo lo que pasaba antes: 5 daban 2047x1351.
    """
    reporte = componer(_fotos(tmp_path, celdas), tmp_path / "set.jpg", FONDO)
    assert reporte.tamano_px[0] == reporte.tamano_px[1], reporte.distribucion
    with Image.open(tmp_path / "set.jpg") as imagen:
        assert imagen.size[0] == imagen.size[1], "el archivo en disco tambien"


@pytest.mark.parametrize("celdas", list(range(1, MAX_CELDAS + 1)))
def test_la_lamina_usa_todo_el_lado_declarado(tmp_path: Path, celdas: int) -> None:
    """Cuadrada **y grande**: llenar el cuadro con una lamina de 300 px seria
    cumplir la letra del requisito y no el punto.

    Lo unico que puede faltar contra `LADO_MAX` es el redondeo entero del lado de
    la celda, que es menor que la cantidad de celdas por lado.
    """
    reporte = componer(_fotos(tmp_path, celdas), tmp_path / "set.jpg", FONDO)
    apretada = max(reporte.columnas, reporte.filas)
    assert reporte.tamano_px[0] <= LADO_MAX
    assert reporte.tamano_px[0] > LADO_MAX - apretada, celdas


def test_las_celdas_son_lo_mas_grandes_que_entran(tmp_path: Path) -> None:
    """Centrar el bloque no puede ser una excusa para achicarlo.

    Con `n` celdas por el lado mas apretado, una mas grande no entraria: el
    bloque se saldria del lienzo. Es la definicion de "lo mas grande que entra",
    y lo que separa esto de dejar aire de mas.
    """
    for celdas in (2, 5, 7, 10, 25):
        reporte = componer(_fotos(tmp_path, celdas), tmp_path / "set.jpg", FONDO)
        apretada = max(reporte.columnas, reporte.filas)
        un_pixel_mas = (reporte.lado_celda_px + 1) * apretada + (
            apretada - 1
        ) * reporte.separacion_px
        assert un_pixel_mas > reporte.tamano_px[0], celdas


def _caja_del_contenido(ruta: Path) -> tuple[int, int, int, int]:
    """El rectangulo que ocupa todo lo que NO es fondo, en una pasada de Pillow.

    Mirar pixeles sueltos no sirve para medir el bloque: con un reparto en
    piramide, una columna cualquiera puede caer en el hueco de una fila corta y
    en una celda de la de arriba. La caja del contenido no depende de donde se
    mire.

    El umbral es por el ruido del JPEG: `difference` sobre un fondo parejo no da
    cero exacto, y sin umbral la caja seria siempre la imagen entera.
    """
    with Image.open(ruta) as imagen:
        rgb = imagen.convert("RGB")
        plano = Image.new("RGB", rgb.size, FONDO)
        diferencia = ImageChops.difference(rgb, plano).convert("L")
        caja = diferencia.point(lambda v: 255 if v > 8 else 0).getbbox()
    assert caja is not None, "la lamina es todo fondo"
    return caja


def test_el_bloque_queda_centrado_en_el_cuadro(tmp_path: Path) -> None:
    """Lo que sobra va mitad arriba y mitad abajo, no todo de un lado.

    Con 5 diseños el reparto es `3-2`: dos filas de celdas en un cuadro de tres,
    asi que sobra casi una fila. Pegada arriba, el set se ve caido; repartida, se
    lee como aire — y ademas es del color del fondo de las fotos, asi que no hay
    ninguna banda que distinguir.

    ⚠ Se mide por SIMETRIA y no buscando el relleno: el relleno es del mismo
    color que el fondo de cada foto —ese es el punto del diseño— asi que no hay
    forma de distinguirlos. Lo que si se puede afirmar es que el contenido queda
    centrado en el cuadro, y que arriba sobra mas que a los costados, donde el
    bloque llena y lo unico que se ve es el margen propio de las fotos.
    """
    reporte = componer(_fotos(tmp_path, 5), tmp_path / "set.jpg", FONDO)
    assert reporte.distribucion == (3, 2)
    lado = reporte.tamano_px[0]

    izquierda, arriba, derecha, abajo = _caja_del_contenido(tmp_path / "set.jpg")
    assert abs(arriba - (lado - abajo)) <= 2, (
        f"{arriba} px de aire arriba contra {lado - abajo} abajo"
    )
    assert abs(izquierda - (lado - derecha)) <= 2
    # Y sobra MAS en vertical que en horizontal: es la mitad de una fila contra
    # el margen propio de una foto. Si fueran iguales, no hubo relleno.
    assert arriba > izquierda + reporte.separacion_px, (
        f"no hay relleno vertical: {arriba} arriba contra {izquierda} al costado"
    )


def _huecos_de(reporte) -> tuple[tuple[int, int], ...]:
    """Puntos que caen en la separacion entre celdas, nunca dentro de una foto.

    ⚠ **No sirve la esquina de la lamina**, que es lo que se miraba antes: con
    un reparto que llena el cuadro —4 fotos son `2-2`— la esquina ES la primera
    celda. Mientras el fondo del set y el de las fotos eran el mismo color eso
    pasaba desapercibido y el test media la foto creyendo medir el hueco; con
    los dos colores separados salta.

    Lo que siempre es separacion, en cualquier reparto de dos o mas columnas y
    dos o mas filas, es la cruz entre las dos primeras de cada una.
    """
    medio = reporte.separacion_px // 2
    canal = reporte.lado_celda_px + medio
    dentro = reporte.lado_celda_px // 2
    return ((canal, canal), (canal, dentro), (dentro, canal))


def test_los_huecos_toman_el_color_que_se_les_pide(tmp_path: Path) -> None:
    """El fondo del set es un parametro, y se respeta en toda la lamina."""
    reporte = componer(_fotos(tmp_path, 4), tmp_path / "set.jpg", FONDO)
    with Image.open(tmp_path / "set.jpg") as imagen:
        rgb = imagen.convert("RGB")
        for punto in _huecos_de(reporte):
            leido = rgb.getpixel(punto)
            assert all(abs(a - b) <= 2 for a, b in zip(leido, FONDO, strict=True)), (
                f"{punto} no tiene el fondo que se pidio: {leido}"
            )


def test_otro_fondo_da_otros_huecos(tmp_path: Path) -> None:
    """La otra mitad del de arriba: que use el parametro y no una constante."""
    oscuro = (40, 44, 48)
    reporte = componer(_fotos(tmp_path, 4), tmp_path / "set.jpg", oscuro)
    with Image.open(tmp_path / "set.jpg") as imagen:
        for punto in _huecos_de(reporte):
            leido = imagen.convert("RGB").getpixel(punto)
            assert all(abs(a - b) <= 2 for a, b in zip(leido, oscuro, strict=True)), (
                f"{punto}: {leido}"
            )


def test_el_fondo_del_set_no_sale_de_las_fotos(tmp_path: Path) -> None:
    """La garantia nueva, y la razon por la que el fondo dejo de leerse de ahi.

    Durante un ciclo el color de los huecos se sacaba de la esquina de la
    primera foto. Era correcto mientras todas las fotos compartian fondo; dejo
    de serlo cuando **cada diseño paso a elegir el suyo**, porque entonces "el
    fondo de las fotos" no es uno sino hasta veinticinco, y tomar el de la
    primera es pintar los huecos de las otras veinticuatro con un color que
    nadie eligio para ellas.

    Se prueba con el caso que no puede salir bien por casualidad: fotos de un
    color, set de otro. Si alguien volviera a deducirlo de la primera foto, los
    huecos saldrian claros y este test lo ve.
    """
    claro = (244, 243, 240)
    oscuro = (40, 44, 48)
    fotos = [_foto(tmp_path / f"v-{i}.jpg", 200, fondo=claro) for i in range(4)]
    reporte = componer(fotos, tmp_path / "set.jpg", oscuro)

    with Image.open(tmp_path / "set.jpg") as imagen:
        rgb = imagen.convert("RGB")
        for punto in _huecos_de(reporte):
            hueco = rgb.getpixel(punto)
            assert all(abs(a - b) <= 2 for a, b in zip(hueco, oscuro, strict=True)), (
                f"{punto} salio del color de las fotos y no del que se pidio: {hueco}"
            )

        # Y las celdas conservan el suyo: el fondo del set pinta lo que hay
        # ENTRE las fotos, no las fotos. Que las pisara seria el defecto
        # simetrico, y sin esta mitad "poner todo oscuro" tambien pasaria.
        #
        # Se mira cerca de la esquina de la primera celda: el encuadre de
        # `preview3d.js` deja margen alrededor de la pieza, asi que ahi hay
        # fondo de foto.
        cx, cy = _centro_de(reporte, 0)
        borde = reporte.lado_celda_px // 2 - 3
        leido = rgb.getpixel((cx - borde, cy - borde))
        assert all(abs(a - b) <= 3 for a, b in zip(leido, claro, strict=True)), (
            f"el fondo del set le piso el suyo a la celda: {leido}"
        )


def _centro_de(reporte, indice: int) -> tuple[int, int]:
    """El centro en pixeles de la celda `indice`, segun el reparto real.

    Se calcula igual que `_origen` y no con un `divmod` sobre columnas: con
    filas desparejas esa division no vale, y un test que la use puede caer
    adentro de la celda equivocada y pasar por casualidad.
    """
    paso = reporte.lado_celda_px + reporte.separacion_px
    resto = indice
    for fila, cuantas in enumerate(reporte.distribucion):
        if resto < cuantas:
            ancho_fila = cuantas * reporte.lado_celda_px + (cuantas - 1) * reporte.separacion_px
            x = (reporte.tamano_px[0] - ancho_fila) // 2 + resto * paso
            return x + reporte.lado_celda_px // 2, fila * paso + reporte.lado_celda_px // 2
        resto -= cuantas
    raise AssertionError(f"la celda {indice} no entra en {reporte.distribucion}")


def test_cada_celda_trae_su_propia_foto(tmp_path: Path) -> None:
    """Que la lamina no repita la primera foto en todas las celdas.

    Es el modo de falla mas probable de una composicion en bucle, y el mas facil
    de no ver: la imagen queda perfecta y esta mal. Se prueba con 7, que es el
    reparto desparejo (`3-2-2`): con una grilla pareja el error se escondia.
    """
    reporte = componer(_fotos(tmp_path, 7), tmp_path / "set.jpg", FONDO)
    with Image.open(tmp_path / "set.jpg") as imagen:
        rgb = imagen.convert("RGB")
        centros = [rgb.getpixel(_centro_de(reporte, i)) for i in range(reporte.celdas)]
    assert len({c[0] for c in centros}) == reporte.celdas, f"celdas repetidas: {centros}"
    assert not any(_parecido(c, FONDO) for c in centros), "una celda quedo sobre el fondo"


def test_las_filas_cortas_quedan_centradas(tmp_path: Path) -> None:
    """Un set de 7 es `3-2-2`: las dos filas de abajo tienen que ir al medio.

    Se mide directo — donde arranca la foto desde la izquierda y donde termina
    desde la derecha— y se exige que los dos margenes sean iguales. Alinear a la
    izquierda dejaria toda la falta de un lado, que es lo que se lee como error
    de armado.
    """
    reporte = componer(_fotos(tmp_path, 7), tmp_path / "set.jpg", FONDO)
    assert reporte.distribucion == (3, 2, 2)
    paso = reporte.lado_celda_px + reporte.separacion_px

    def margenes(rgb: Image.Image, fila: int) -> tuple[int, int]:
        """Cuanto fondo hay antes y despues de la pieza, en esa fila de celdas."""
        y = fila * paso + reporte.lado_celda_px // 2
        pixeles = [rgb.getpixel((x, y)) for x in range(rgb.width)]
        izquierda = next(x for x, p in enumerate(pixeles) if not _parecido(p, FONDO))
        derecha = next(x for x, p in enumerate(reversed(pixeles)) if not _parecido(p, FONDO))
        return izquierda, derecha

    with Image.open(tmp_path / "set.jpg") as imagen:
        rgb = imagen.convert("RGB")
        llena = margenes(rgb, 0)
        cortas = [margenes(rgb, fila) for fila in (1, 2)]

    # ⚠ Lo que se mide es la SIMETRIA, no el margen en si: cada foto trae su
    # propio fondo alrededor de la pieza —el encuadre de `preview3d.js` lo
    # garantiza— y ese fondo es del mismo color que el hueco de la lamina, que
    # es justamente el punto del diseño. Distinguirlos por color es imposible y
    # no hace falta: una fila centrada tiene el mismo aire de los dos lados.
    for fila, (izquierda, derecha) in zip((1, 2), cortas, strict=True):
        assert abs(izquierda - derecha) <= 2, (
            f"fila {fila}: {izquierda} px a la izquierda y {derecha} a la derecha"
        )
    # Y la fila corta tiene MAS aire que la llena, que es lo que la centra.
    assert min(m[0] for m in cortas) > llena[0] + reporte.separacion_px


def test_el_set_entra_en_el_tope_de_dos_megas(tmp_path: Path) -> None:
    """Con 25 celdas, que es el peor caso que el producto admite."""
    reporte = componer(_fotos(tmp_path, MAX_CELDAS), tmp_path / "set.jpg", FONDO)
    assert reporte.celdas == MAX_CELDAS
    assert (tmp_path / "set.jpg").stat().st_size <= 2 * 1024 * 1024


def test_una_foto_que_falta_no_se_compone_a_medias(tmp_path: Path) -> None:
    """Mejor no armar el set que armarlo con un hueco que parece intencional."""
    fotos = _fotos(tmp_path, 3)
    fotos[1].unlink()
    with pytest.raises(ImagenInvalida):
        componer(fotos, tmp_path / "set.jpg", FONDO)
    assert not (tmp_path / "set.jpg").exists(), "no queda un archivo a medias"


def test_sin_fotos_no_hay_set(tmp_path: Path) -> None:
    with pytest.raises(ImagenInvalida):
        componer([], tmp_path / "set.jpg", FONDO)


def test_recomponer_reemplaza_el_set_anterior(tmp_path: Path) -> None:
    """El destino puede ser un `set.jpg` vivo: se escribe a temporal y se renombra."""
    destino = tmp_path / "set.jpg"
    componer(_fotos(tmp_path, 9), destino, FONDO)
    primero = destino.stat().st_size
    componer(_fotos(tmp_path, 2), destino, FONDO)
    assert destino.stat().st_size != primero
    with Image.open(destino) as imagen:
        # Las dos son cuadradas —siempre lo son— pero con dos celdas el lado es
        # mayor, asi que la lamina nueva no puede ser la vieja.
        assert imagen.size[0] == imagen.size[1]
    assert componer(_fotos(tmp_path, 2), destino, FONDO).lado_celda_px > 900
