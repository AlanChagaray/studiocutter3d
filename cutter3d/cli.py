"""Interfaz de linea de comandos.

El subcomando `cortante` es el default: si el primer argumento no es un
subcomando conocido, se asume. Asi la invocacion del contrato sigue siendo
literal...

    python -m cutter3d --svg arte.svg --modo cortante+marcador --out salida.3mf --reporte

...y a la vez quedan disponibles las otras etapas:

    python -m cutter3d jpg dibujo.png -o dibujo.jpg
    python -m cutter3d lineas dibujo.jpg -o lineas.png
    python -m cutter3d vectorizar lineas.png -o arte.svg
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import Modo, generar
from .errors import Cutter3DError
from .params import AjustesMotor, CutterParams
from .raster import convertir_a_jpg, guardar_binaria, preparar_lineas
from .vector import a_svg
from .verify import render_texto

SUBCOMANDOS = ("cortante", "jpg", "lineas", "vectorizar")
COMANDO_DEFAULT = "cortante"


def _agregar_parametros(sub: argparse.ArgumentParser) -> None:
    d = CutterParams()
    grupo = sub.add_argument_group("dimensiones (mm, cada una > 0 y <= 1000)")
    grupo.add_argument("--lado-mayor", type=float, default=d.lado_mayor_mm)
    grupo.add_argument("--altura-base", type=float, default=d.altura_base_mm)
    grupo.add_argument("--altura-trazos", type=float, default=d.altura_trazos_mm)
    grupo.add_argument("--ancho-trazo", type=float, default=d.ancho_trazo_mm)
    grupo.add_argument("--luz", type=float, default=d.luz_mm)
    grupo.add_argument("--filo-ancho", type=float, default=d.filo_ancho_mm)
    grupo.add_argument("--filo-alto", type=float, default=d.filo_alto_mm)
    grupo.add_argument("--pie-ancho-extra", type=float, default=d.pie_ancho_extra_mm)
    grupo.add_argument("--pie-alto", type=float, default=d.pie_alto_mm)
    ajustes = AjustesMotor()
    tuning = sub.add_argument_group("ajustes del motor")
    tuning.add_argument("--px-mm", type=float, default=ajustes.px_por_mm)
    tuning.add_argument("--max-iteraciones", type=int, default=ajustes.max_iteraciones)


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cutter3d", description=__doc__)
    subs = parser.add_subparsers(dest="comando", required=True)

    cortante = subs.add_parser("cortante", help="SVG a .3mf (y .glb / .stl)")
    cortante.add_argument("--svg", type=Path, required=True)
    cortante.add_argument("--out", type=Path, required=True, help="ruta del .3mf")
    cortante.add_argument(
        "--modo", choices=[m.value for m in Modo], default=Modo.CORTANTE_MARCADOR.value
    )
    cortante.add_argument("--stl", action="store_true", help="ademas del .3mf, dos .stl")
    cortante.add_argument("--reporte", action="store_true", help="imprime la fidelidad")
    _agregar_parametros(cortante)

    a_jpg = subs.add_parser("jpg", help="png / jfif / webp / svg a jpg")
    a_jpg.add_argument("entrada", type=Path)
    a_jpg.add_argument("-o", "--out", type=Path, required=True)
    a_jpg.add_argument("--calidad", type=int, default=95)

    lineas = subs.add_parser("lineas", help="jpg a blanco y negro puro de lineas")
    lineas.add_argument("entrada", type=Path)
    lineas.add_argument("-o", "--out", type=Path, required=True, help="ruta del .png")
    lineas.add_argument(
        "--no-contornear-macizos",
        action="store_true",
        help="deja las zonas macizas intactas (por default se contornean)",
    )
    lineas.add_argument("--umbral", type=int, default=None, help="0-255; por default, Otsu")

    vec = subs.add_parser("vectorizar", help="imagen a svg de paths rellenos")
    vec.add_argument("entrada", type=Path)
    vec.add_argument("-o", "--out", type=Path, required=True)

    return parser


def _params_de(args: argparse.Namespace) -> CutterParams:
    return CutterParams(
        lado_mayor_mm=args.lado_mayor,
        altura_base_mm=args.altura_base,
        altura_trazos_mm=args.altura_trazos,
        ancho_trazo_mm=args.ancho_trazo,
        luz_mm=args.luz,
        filo_ancho_mm=args.filo_ancho,
        filo_alto_mm=args.filo_alto,
        pie_ancho_extra_mm=args.pie_ancho_extra,
        pie_alto_mm=args.pie_alto,
    )


def _correr_cortante(args: argparse.Namespace) -> int:
    resultado = generar(
        svg=args.svg,
        modo=Modo(args.modo),
        salida=args.out,
        params=_params_de(args),
        ajustes=AjustesMotor(px_por_mm=args.px_mm, max_iteraciones=args.max_iteraciones),
        con_stl=args.stl,
    )
    print(f"3mf: {resultado.ruta_3mf}")
    print(f"glb: {resultado.ruta_glb}")
    for ruta in resultado.rutas_stl:
        print(f"stl: {ruta}")
    if args.reporte:
        print()
        print(render_texto(resultado.reporte))
    return 0


def _correr_jpg(args: argparse.Namespace) -> int:
    print(f"jpg: {convertir_a_jpg(args.entrada, args.out, calidad=args.calidad)}")
    return 0


def _correr_lineas(args: argparse.Namespace) -> int:
    resultado = preparar_lineas(
        args.entrada,
        contornear_macizos=not args.no_contornear_macizos,
        umbral=args.umbral,
    )
    print(f"png: {guardar_binaria(resultado, args.out)}")
    print(f"umbral usado      : {resultado.umbral_usado}")
    print(f"ancho de trazo    : {resultado.ancho_trazo_px:.1f} px")
    if resultado.contorneado_activo:
        print(
            f"zonas contorneadas: {resultado.zonas_contorneadas} "
            f"({resultado.area_contorneada_px} px de area modificada)"
        )
    else:
        print("zonas contorneadas: 0 (contorneado desactivado)")
    return 0


def _correr_vectorizar(args: argparse.Namespace) -> int:
    print(f"svg: {a_svg(args.entrada, args.out)}")
    return 0


_DESPACHO = {
    "cortante": _correr_cortante,
    "jpg": _correr_jpg,
    "lineas": _correr_lineas,
    "vectorizar": _correr_vectorizar,
}


def main(argv: Sequence[str] | None = None) -> int:
    args_crudos = list(sys.argv[1:] if argv is None else argv)
    parser = construir_parser()
    if not args_crudos:
        # Sin argumentos, la ayuda util es la de nivel superior con los 4
        # subcomandos, no el error de argparse acotado a `cortante`.
        parser.print_help()
        return 2
    if args_crudos[0] not in (*SUBCOMANDOS, "-h", "--help"):
        args_crudos.insert(0, COMANDO_DEFAULT)
    args = parser.parse_args(args_crudos)
    try:
        return _DESPACHO[args.comando](args)
    except Cutter3DError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
