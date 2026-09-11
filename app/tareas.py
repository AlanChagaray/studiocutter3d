"""Lo que corre DENTRO del proceso hijo.

Este modulo es la otra orilla de la frontera entre procesos, y por eso tiene
reglas propias:

- **Funciones a nivel de modulo.** En Windows el arranque es `spawn`: el hijo
  reimporta este archivo y busca la funcion por nombre. Un closure, un metodo
  ligado o una funcion anidada no se pueden pasar.
- **Solo tipos primitivos en los argumentos.** Nada de `CutterParams`, nada de
  la app, nada de objetos del almacen. Los parametros viajan como `dict` de
  floats y se reconstruyen de este lado.
- **No se devuelve nada.** El resultado viaja por `estado.json`, escrito de
  forma atomica. Un `return` del hijo no llega a ningun lado.
- **Ninguna excepcion escapa.** Si el motor falla, el fallo se traduce a un
  error seguro y se escribe igual: un hijo que muere sin dejar `estado.json`
  es indistinguible de uno que mato el timeout.

El costo conocido de `spawn` es que el hijo reimporta numpy, scipy y trimesh
en cada trabajo. Se mide y se publica; a cambio, `terminate()` es un timeout
de verdad y no una espera que se rinde.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .archivos import escribir_estado
from .errores import como_dict

ETAPA_VECTORIZANDO = "vectorizando la imagen"
ETAPA_GEOMETRIA = "construyendo la geometria"
ETAPA_LINEAS = "corrigiendo las lineas"
ETAPA_LISTO = "listo"


def _avisar(dir_trabajo: Path, etapa: str) -> None:
    """Deja el progreso en disco. `ok: null` significa todavia en curso."""
    escribir_estado(dir_trabajo, {"ok": None, "etapa": etapa, "archivos": {}})


def _terminar_bien(
    dir_trabajo: Path, archivos: dict[str, str], reporte: dict[str, Any] | None
) -> None:
    escribir_estado(
        dir_trabajo,
        {"ok": True, "etapa": ETAPA_LISTO, "archivos": archivos, "reporte": reporte},
    )


def _terminar_mal(dir_trabajo: Path, exc: Exception) -> None:
    """Traduce el fallo con el mismo criterio que la API y lo deja en disco."""
    escribir_estado(
        dir_trabajo, {"ok": False, "etapa": "error", "archivos": {}, "error": como_dict(exc)}
    )


# ── F2: correccion de lineas ─────────────────────────────────────────────────


def ejecutar_lineas(dir_trabajo_txt: str, entrada_txt: str, contornear_macizos: bool) -> None:
    """Deja `salida.png` en blanco y negro puro, su `salida.svg`, y el detalle.

    **La vectorizacion se hace aca y no en el cortante.** El cortante solo
    acepta SVG, y este es el mejor momento posible para producirlo: la imagen
    acaba de quedar en dos valores puros, que es justo la entrada con la que
    vtracer pierde menos detalle. Ademas el SVG queda descargable y el usuario
    puede mirarlo antes de mandarlo a geometria.
    """
    dir_trabajo = Path(dir_trabajo_txt)
    try:
        from cutter3d import raster, vector  # noqa: PLC0415 — ver el docstring del modulo

        _avisar(dir_trabajo, ETAPA_LINEAS)
        resultado = raster.preparar_lineas(Path(entrada_txt), contornear_macizos)
        png = raster.guardar_binaria(resultado, dir_trabajo / "salida.png")

        _avisar(dir_trabajo, ETAPA_VECTORIZANDO)
        vector.a_svg(png, dir_trabajo / "salida.svg")

        _terminar_bien(
            dir_trabajo,
            {"png": "salida.png", "svg": "salida.svg"},
            {
                "umbral_usado": resultado.umbral_usado,
                "ancho_trazo_px": round(resultado.ancho_trazo_px, 2),
                "zonas_contorneadas": resultado.zonas_contorneadas,
                "area_contorneada_px": resultado.area_contorneada_px,
                "contorneado_activo": resultado.contorneado_activo,
            },
        )
    except Exception as exc:  # el hijo NUNCA puede morir en silencio
        _terminar_mal(dir_trabajo, exc)


# ── F3: cortante y marcador ──────────────────────────────────────────────────


def ejecutar_cortante(
    dir_trabajo_txt: str,
    entrada_txt: str,
    modo_txt: str,
    parametros: dict[str, float],
) -> None:
    """Genera el `.3mf`, el `.glb` del preview y los `.stl`, siempre.

    No vectoriza nada: la entrada ya es SVG porque el router solo acepta SVG.
    Lo que llega aca es exactamente lo que el usuario vio y aprobo.

    El `con_stl` del motor queda fijo en `True`: la web no lo pregunta mas (ver
    el docstring de `app/routers/cortante.py`), y un parametro que siempre vale
    lo mismo en el unico llamador no es una opcion, es ruido en la frontera
    entre procesos.
    """
    dir_trabajo = Path(dir_trabajo_txt)
    try:
        from cutter3d import (  # noqa: PLC0415 — ver el docstring del modulo
            CutterParams,
            Modo,
            generar,
        )

        archivos: dict[str, str] = {}

        _avisar(dir_trabajo, ETAPA_GEOMETRIA)
        resultado = generar(
            Path(entrada_txt),
            Modo(modo_txt),
            dir_trabajo / "salida.3mf",
            params=CutterParams(**parametros),
            con_stl=True,
        )

        archivos["3mf"] = resultado.ruta_3mf.name
        archivos["glb"] = resultado.ruta_glb.name
        for stl in resultado.rutas_stl:
            clave = "stl_marcador" if "marcador" in stl.name else "stl_cortador"
            archivos[clave] = stl.name
        for suelto in resultado.rutas_3mf_objeto:
            clave = "3mf_marcador" if "marcador" in suelto.name else "3mf_cortador"
            archivos[clave] = suelto.name

        _terminar_bien(dir_trabajo, archivos, reporte_como_json(resultado.reporte))
    except Exception as exc:  # ver el docstring del modulo
        _terminar_mal(dir_trabajo, exc)


# ── Serializacion del reporte ────────────────────────────────────────────────


def reporte_como_json(r: Any) -> dict[str, Any]:
    """`ReporteFidelidad` a JSON, con las propiedades derivadas incluidas.

    Se agregan explicitamente porque `asdict()` solo copia campos: `todo_ok`,
    `ok` y `desvio_relativo` son properties, y son justo lo que la pantalla
    necesita para pintar el veredicto sin recalcular nada.

    El tipo va como `Any` a proposito: este modulo no importa `cutter3d.verify`
    (la web no depende de la implementacion del reporte), solo lee atributos
    del objeto que `generar()` ya devolvio.
    """
    return {
        "todo_ok": r.todo_ok,
        "conteos_coinciden": r.conteos_coinciden,
        "desvio_acotado": r.desvio_acotado,
        "objetos": list(r.objetos),
        "topologias": [
            {
                "objeto": t.objeto,
                "watertight": t.watertight,
                "euler": t.euler,
                "euler_esperado": t.euler_esperado,
                "z_min_mm": round(t.z_min_mm, 4),
                "z_max_mm": round(t.z_max_mm, 4),
                "ok": t.ok,
            }
            for t in r.topologias
        ],
        "secciones": [
            {
                "z_mm": round(s.z_mm, 3),
                "area_medida_mm2": round(s.area_medida_mm2, 3),
                "area_esperada_mm2": round(s.area_esperada_mm2, 3),
                "desvio_relativo": round(s.desvio_relativo, 5),
                "ok": s.ok,
            }
            for s in r.secciones
        ],
        "lado_mayor_final_mm": r.lado_mayor_final_mm,
        "luz_minima_real_mm": r.luz_minima_real_mm,
        "luz_nominal_mm": r.luz_nominal_mm,
        "contornos_original": r.contornos_original,
        "contornos_final": r.contornos_final,
        "huecos_original": r.huecos_original,
        "huecos_final": r.huecos_final,
        "area_diferencia_mm2": r.area_diferencia_mm2,
        "desvio_p50_mm": r.desvio_p50_mm,
        "desvio_p99_mm": r.desvio_p99_mm,
        "desvio_max_mm": r.desvio_max_mm,
        "dilatacion_aplicada_mm": r.dilatacion_aplicada_mm,
        "mediana_trazo_antes_mm": r.mediana_trazo_antes_mm,
        "muescas_selladas_mm": r.muescas_selladas_mm,
        "muescas_selladas_pct": r.muescas_selladas_pct,
        "trazo": dataclasses.asdict(r.trazo) if r.trazo is not None else None,
        "huecos": dataclasses.asdict(r.huecos) if r.huecos is not None else None,
        "zonas_contorneadas": r.zonas_contorneadas,
        "area_contorneada_mm2": r.area_contorneada_mm2,
        "colisiones_puenteadas": r.colisiones_puenteadas,
        "area_puenteada_mm2": r.area_puenteada_mm2,
        "area_puenteada_pct": r.area_puenteada_pct,
        "advertencias": list(r.advertencias),
    }
