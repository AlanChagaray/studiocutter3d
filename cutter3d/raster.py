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
- El **contorneado de zonas macizas esta ENCENDIDO por default**. Es la unica
  etapa del flujo que modifica el arte, y por eso **siempre declara cuantas zonas
  toco y que area**: la modificacion no puede ser invisible. El modulo de cortante
  (F3) nunca hace esto — reproduce fiel lo que reciba.

El umbral de "macizo" se autocalibra con el propio dibujo: se mide su ancho de
trazo mediano `w` y se considera macizo toda region donde entre un disco de radio
`0,75 w`. Un trazo de ancho `w` tiene medio ancho `0,5 w` y no entra; una mancha
si. Asi no hace falta conocer la escala fisica de la imagen.

La salida de F2 se guarda en **PNG y nunca en JPG**: el contrato es explicito en
que el ruido de compresion ensucia el borde y mete dientes en el filo.
"""

from __future__ import annotations

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
from scipy.ndimage import label
from skimage.filters import threshold_otsu
from skimage.morphology import disk, erosion, medial_axis, opening

from .errors import Cutter3DError, ImagenInvalida

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

CALIDAD_JPG = 95
FACTOR_MACIZO = 0.75
"""Radio del disco de apertura, en unidades de ancho de trazo mediano."""


@dataclass(frozen=True)
class ResultadoF2:
    imagen: npt.NDArray[np.uint8]
    """uint8 con exactamente dos valores: 0 (tinta) y 255 (fondo)."""

    umbral_usado: int
    ancho_trazo_px: float
    zonas_contorneadas: int
    area_contorneada_px: int
    contorneado_activo: bool


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


def _aplanar_sobre_blanco(imagen: Image.Image) -> Image.Image:
    """JPEG no tiene canal alfa: lo transparente se compone sobre blanco."""
    if imagen.mode in ("RGBA", "LA") or (imagen.mode == "P" and "transparency" in imagen.info):
        rgba = imagen.convert("RGBA")
        fondo = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(fondo, rgba).convert("RGB")
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
            rgb = _aplanar_sobre_blanco(imagen)
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
        if entrada.suffix.lower() == ".png":
            return entrada
        destino_png.parent.mkdir(parents=True, exist_ok=True)
        _aplanar_sobre_blanco(imagen).save(destino_png, format="PNG")
    return destino_png


def _ancho_trazo_px(tinta: Mascara) -> float:
    """Ancho mediano del trazo, en pixeles, medido sobre el propio dibujo."""
    ejes, distancia = medial_axis(tinta, return_distance=True)
    valores = 2.0 * np.asarray(distancia)[np.asarray(ejes, dtype=bool)]
    return float(np.median(valores)) if valores.size else 1.0


def _contornear_macizos(tinta: Mascara, ancho_px: float) -> tuple[Mascara, int, int]:
    """Reemplaza cada zona maciza por un anillo del ancho de trazo del dibujo."""
    # `opening` / `erosion` y no sus variantes `binary_*`: estan deprecadas
    # desde skimage 0.26. Sobre entrada booleana hacen morfologia binaria igual.
    radio_apertura = max(1, round(FACTOR_MACIZO * ancho_px))
    macizos = np.asarray(opening(tinta, disk(radio_apertura)), dtype=bool)
    if not macizos.any():
        return tinta, 0, 0
    radio_anillo = max(1, round(ancho_px))
    nucleo = np.asarray(erosion(macizos, disk(radio_anillo)), dtype=bool)
    anillo = macizos & ~nucleo
    _, cantidad = label(macizos)
    resultado = (tinta & ~macizos) | anillo
    return resultado, int(cantidad), int(macizos.sum())


def preparar_lineas(
    jpg: Path, contornear_macizos: bool = True, umbral: int | None = None
) -> ResultadoF2:
    """F2 — jpg a blanco y negro puro de lineas.

    `contornear_macizos` viene en True por decision explicita del usuario. El
    resultado siempre declara cuantas zonas se tocaron y que area, para que la
    modificacion del arte quede a la vista.
    """
    if jpg.suffix.lower() not in (".jpg", ".jpeg", ".jfif"):
        raise ImagenInvalida(str(jpg), "esta etapa recibe JPG si o si")
    with _abrir_como_pil(jpg) as imagen:
        if imagen.format != "JPEG":
            raise ImagenInvalida(str(jpg), f"esta etapa recibe JPG si o si (es {imagen.format})")
        grises = np.asarray(imagen.convert("L"), dtype=np.uint8)

    corte = int(threshold_otsu(grises)) if umbral is None else int(umbral)
    tinta: Mascara = grises <= corte

    ancho_px = _ancho_trazo_px(tinta) if tinta.any() else 1.0
    zonas = 0
    area = 0
    if contornear_macizos and tinta.any():
        tinta, zonas, area = _contornear_macizos(tinta, ancho_px)

    salida = np.where(tinta, np.uint8(0), np.uint8(255)).astype(np.uint8)
    return ResultadoF2(
        imagen=salida,
        umbral_usado=corte,
        ancho_trazo_px=ancho_px,
        zonas_contorneadas=zonas,
        area_contorneada_px=area,
        contorneado_activo=contornear_macizos,
    )


def guardar_binaria(resultado: ResultadoF2, salida: Path) -> Path:
    """Guarda en PNG. Nunca en JPG: la compresion volveria a meter grises."""
    if salida.suffix.lower() != ".png":
        raise ImagenInvalida(str(salida), "la salida de la correccion de lineas va en PNG")
    salida.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(resultado.imagen, mode="L").save(salida, format="PNG", optimize=True)
    return salida
