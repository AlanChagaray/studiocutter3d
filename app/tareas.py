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

**Los imports del motor van adentro de cada tarea, y desde el ciclo 6 eso sirve
de verdad.** Antes no: `from .errores import como_dict` (abajo) arrastraba
`cutter3d.errors`, y el `__init__` del paquete importaba el motor entero, asi
que el hijo pagaba ~107 MB antes de ejecutar una linea. Con el `__init__`
perezoso de `cutter3d`, importar este modulo cuesta 22 MB y cada tarea carga
solo lo suyo: F2 no toca trimesh ni manifold3d.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path
from typing import Any

from .archivos import escribir_estado
from .errores import como_dict

log = logging.getLogger("studiocutter")

LIMITE_RAM_HIJO_MB = 380
"""Techo de memoria del proceso hijo, en MB. 0 lo desactiva.

**Es la diferencia entre "fallo el trabajo" y "murio el servidor".** Sin techo,
una asignacion desbocada —el `float64` de `medial_axis` sobre una imagen enorme,
una malla con mas triangulos de los previstos— la corta el kernel, y lo que mata
es el contenedor entero: en Render eso sale como
`Ran out of memory (used over 512MB) while running your code` y se lleva puesta
la sesion de cualquiera que estuviera usando la app.

Con el techo puesto, la misma asignacion levanta `MemoryError` **adentro del
hijo**, donde el `except Exception` de cada tarea ya la traduce a un
`estado.json` con `ok: false`. El trabajo falla con un mensaje que se entiende
(`app/errores.py:MENSAJE_SIN_MEMORIA`); el servidor no se entera.

El valor sale de medir **en el contenedor**, que es donde esto corre. Con tres
cortantes seguidos (estrella, kitty_bruja, murcielago) el proceso pico en:

| Metrica | Pico | Que la acota |
|---|---|---|
| `VmPeak` — espacio de direcciones | **611 MB** | `RLIMIT_AS` |
| `VmData` — heap anonimo | **277 MB** | `RLIMIT_DATA` |
| `VmHWM` — RSS | **291 MB** | el cgroup, o sea Render |

⚠ **Por eso el limite es `RLIMIT_DATA` y no `RLIMIT_AS`.** Un cortante normal
reserva 611 MB de espacio de direcciones para usar 291 de memoria real: numpy,
manifold3d y las libc pisan mucha mas VA de la que tocan. Acotar `RLIMIT_AS`
contra el numero de RSS hace fallar hasta la estrella — pasó, y lo agarro recien
el end-to-end contra el contenedor, porque en Windows la metrica ni existe.
`VmData` queda a un 5% del RSS, que es lo unico que Render mide.

380 MB deja un 37% de aire sobre los 277 que pide un cortante normal, y con los
~71 MB del proceso web el total peor caso queda en ~450 de 512. Frena lo
patologico, no lo pesado.
"""


def _acotar_memoria() -> None:
    """Le pone el techo de RAM al proceso hijo. Silencioso si no se puede.

    `resource` es POSIX: en Windows no existe, y el desarrollo de este proyecto
    es en Windows. No es una perdida — el techo es una defensa de la instancia
    de 512 MB, y en la maquina de desarrollo no hay nada que defender.

    El corte se hace con `sys.platform` y no con un `try/ImportError` para que
    mypy pueda estrechar el tipo: typeshed declara `resource` solo fuera de
    win32, y con el `try` la funcion entera queda sin chequear. ⚠ La contra es
    que en Windows mypy da por inalcanzable lo que sigue: para verificarlo hay
    que correr `mypy --platform linux`.

    Se usa `RLIMIT_DATA` y **no** `RLIMIT_AS`: ver la tabla de
    `LIMITE_RAM_HIJO_MB`. Desde Linux 4.7 `RLIMIT_DATA` alcanza tambien a las
    asignaciones anonimas por `mmap` —que es como numpy pide los arrays
    grandes—, asi que sigue el heap real en vez del espacio de direcciones
    reservado, que en este stack es mas del doble.
    """
    if LIMITE_RAM_HIJO_MB <= 0 or sys.platform == "win32":
        return

    import resource  # noqa: PLC0415 — POSIX only, ver el docstring

    techo = LIMITE_RAM_HIJO_MB * 1024 * 1024
    blando, duro = resource.getrlimit(resource.RLIMIT_DATA)
    # Nunca SUBIR un techo que ya venga puesto: si el contenedor o el host ya
    # acotaron el proceso, ese limite manda. Solo se baja.
    for vigente in (blando, duro):
        if vigente != resource.RLIM_INFINITY and vigente > 0:
            techo = min(techo, vigente)
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (techo, duro))
    except (ValueError, OSError):  # pragma: no cover — depende del sandbox del host
        log.warning("no se pudo acotar la memoria del proceso hijo")


ETAPA_VECTORIZANDO = "vectorizando la imagen"
ETAPA_GEOMETRIA = "construyendo la geometria"
ETAPA_LINEAS = "corrigiendo las lineas"
ETAPA_LEYENDO_MALLA = "leyendo la malla"
ETAPA_CONVIRTIENDO_MALLA = "convirtiendo la malla"
ETAPA_LISTO = "listo"


def _avisar(dir_trabajo: Path, etapa: str, indice: int | None = None) -> None:
    """Deja el progreso en disco. `ok: null` significa todavia en curso."""
    escribir_estado(dir_trabajo, {"ok": None, "etapa": etapa, "archivos": {}}, indice)


def _terminar_bien(
    dir_trabajo: Path,
    archivos: dict[str, str],
    reporte: dict[str, Any] | None,
    indice: int | None = None,
) -> None:
    escribir_estado(
        dir_trabajo,
        {"ok": True, "etapa": ETAPA_LISTO, "archivos": archivos, "reporte": reporte},
        indice,
    )


def _terminar_mal(dir_trabajo: Path, exc: Exception, indice: int | None = None) -> None:
    """Traduce el fallo con el mismo criterio que la API y lo deja en disco."""
    escribir_estado(
        dir_trabajo,
        {"ok": False, "etapa": "error", "archivos": {}, "error": como_dict(exc)},
        indice,
    )


# ── F2: correccion de lineas ─────────────────────────────────────────────────


def ejecutar_lineas(
    dir_trabajo_txt: str,
    entrada_txt: str,
    contornear_macizos: bool,
    normalizar_trazo: bool,
    dimensiones: dict[str, float],
) -> None:
    """Deja `salida.png` en blanco y negro puro, su `salida.svg`, y el detalle.

    **La vectorizacion se hace aca y no en el cortante.** El cortante solo
    acepta SVG, y este es el mejor momento posible para producirlo: la imagen
    acaba de quedar en dos valores puros, que es justo la entrada con la que
    vtracer pierde menos detalle. Ademas el SVG queda descargable y el usuario
    puede mirarlo antes de mandarlo a geometria.

    `dimensiones` son los milimetros que necesita la normalizacion —ancho de
    trazo objetivo y lado mayor de la pieza— y viaja como `dict` de floats por
    lo mismo que el de `ejecutar_cortante`: de este lado de la frontera solo
    entran primitivos, y un dict mantiene la firma legible.
    """
    _acotar_memoria()
    dir_trabajo = Path(dir_trabajo_txt)
    try:
        from cutter3d import raster, vector  # noqa: PLC0415 — ver el docstring del modulo

        _avisar(dir_trabajo, ETAPA_LINEAS)
        resultado = raster.preparar_lineas(
            Path(entrada_txt),
            contornear_macizos,
            normalizar_trazo=normalizar_trazo,
            # Campo por campo y no `**dimensiones`: asi el tipado ve que el dict
            # no puede colarse en `umbral`, y un dict con una clave de mas falla
            # aca en vez de terminar en un `TypeError` adentro del motor.
            ancho_trazo_mm=dimensiones["ancho_trazo_mm"],
            lado_mayor_mm=dimensiones["lado_mayor_mm"],
        )
        png = raster.guardar_binaria(resultado, dir_trabajo / "salida.png")
        # La copia para retocar a mano. El PNG sigue siendo lo que se vectoriza
        # y lo que muestra la pantalla; el JPG es lo que el usuario se baja,
        # porque es lo que edita y lo unico que Correcto acepta de vuelta.
        raster.guardar_editable(resultado, dir_trabajo / "editable.jpg")

        _avisar(dir_trabajo, ETAPA_VECTORIZANDO)
        vector.a_svg(png, dir_trabajo / "salida.svg")

        _terminar_bien(
            dir_trabajo,
            {"png": "salida.png", "svg": "salida.svg", "jpg_editable": "editable.jpg"},
            {
                "umbral_usado": resultado.umbral_usado,
                "ancho_trazo_px": round(resultado.ancho_trazo_px, 2),
                "zonas_contorneadas": resultado.zonas_contorneadas,
                "area_contorneada_px": resultado.area_contorneada_px,
                "contorneado_activo": resultado.contorneado_activo,
                # La normalizacion es la otra modificacion del arte, y se
                # declara con el mismo criterio: cuanto engordo, cuanto afino, y
                # sobre que suposicion de tamaño final se calibro el objetivo.
                "normalizacion_activa": resultado.normalizacion_activa,
                "ancho_objetivo_px": round(resultado.ancho_objetivo_px, 2),
                "ancho_logrado_px": resultado.ancho_logrado_px,
                "ancho_objetivo_mm": resultado.ancho_objetivo_mm,
                "lado_mayor_supuesto_mm": resultado.lado_mayor_supuesto_mm,
                "area_engrosada_px": resultado.area_engrosada_px,
                "area_afinada_px": resultado.area_afinada_px,
                "area_protegida_px": resultado.area_protegida_px,
                # La reduccion por presupuesto de memoria se declara igual que
                # el contorneado: es una modificacion del arte y no se hace en
                # silencio. `ancho_trazo_px` esta en la escala de `tamano_usado`.
                "tamano_original": list(resultado.tamano_original),
                "tamano_usado": list(resultado.tamano_usado),
                "fue_reducida": resultado.fue_reducida,
                # La ampliacion es la otra direccion del mismo presupuesto: la
                # normalizacion trabaja y entrega en una grilla mas fina para
                # que el trazo salga liso, y el PNG/SVG cambian de tamaño.
                "factor_ampliacion": resultado.factor_ampliacion,
                "fue_ampliada": resultado.fue_ampliada,
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
    _acotar_memoria()
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


# ── F4: foto de una malla ya construida ──────────────────────────────────────


def ejecutar_post(
    dir_trabajo_txt: str,
    indice: int,
    entradas_txt: tuple[str, ...],
    roles: tuple[str, ...],
    salida_txt: str,
) -> None:
    """Deja el `.glb` de UN diseño, a partir de sus uno o dos archivos.

    Es la tarea mas corta de las cuatro, y lo unico que tiene de propio es que
    **no construye geometria**: la lee. La foto la rinde despues el navegador con
    el mismo `preview3d.js` que la del cortante, que es lo que garantiza que
    salga igual; aca solo hay que dejarle el `.glb` con el acabado puesto.

    Corre **una vez por diseño**, en su propio proceso y en serie con los demas
    (`trabajos.lanzar_serie`), asi que escribe `estado-<indice>.json` y no
    `estado.json`: con un solo archivo, el diseño siguiente pisaria el resultado
    del anterior antes de que el padre lo haya leido.

    `entradas_txt` y `roles` son tuplas de strings porque los argumentos cruzan
    un `spawn`: solo primitivos, como dice el docstring del modulo.

    Importa `cutter3d.malla` y nada mas: trimesh entra igual, pero manifold3d,
    shapely y skimage no, porque no se construye ni una booleana.
    """
    _acotar_memoria()
    dir_trabajo = Path(dir_trabajo_txt)
    try:
        from cutter3d.malla import a_glb  # noqa: PLC0415 — ver el docstring del modulo

        _avisar(dir_trabajo, ETAPA_LEYENDO_MALLA, indice)
        reporte = a_glb([Path(e) for e in entradas_txt], dir_trabajo / salida_txt, list(roles))

        _terminar_bien(
            dir_trabajo,
            {},
            {
                "objetos": list(reporte.objetos),
                "triangulos": reporte.triangulos,
                "medidas_mm": list(reporte.medidas_mm),
                "cerrado": reporte.cerrado,
                "volumen_mm3": reporte.volumen_mm3,
                "advertencias": list(reporte.advertencias),
                "archivos": len(entradas_txt),
            },
            indice,
        )
    except Exception as exc:  # ver el docstring del modulo
        _terminar_mal(dir_trabajo, exc, indice)


# ── F1-mallas: 3MF <-> STL ───────────────────────────────────────────────────


def ejecutar_malla(
    dir_trabajo_txt: str,
    entrada_txt: str,
    salida_txt: str,
    clave_txt: str,
) -> None:
    """Pasa la malla subida al otro formato y deja el reporte de lo que midio.

    **Es la unica parte del Convertidor que no corre en linea, y el motivo no es
    que tarde.** Tarda milisegundos. Lo que no puede es correr adentro de
    uvicorn: `cutter3d.malla` importa trimesh, y eso son ~1200 modulos y ~89 MB
    en un proceso que no construye un solo poligono — exactamente lo que el
    ciclo 6 saco de ahi. La contra es el arranque en frio del hijo (~1 s), y a
    cambio la conversion hereda el techo de RAM y el timeout, que es lo que un
    STL de 20 MB de un desconocido justifica por si solo.

    `salida_txt` es un nombre, no una ruta, y sale de `NOMBRE_DE`: el cliente
    sigue sin nombrar nada en disco. El formato de destino se deduce de su
    extension —`convertir` es quien la traduce a `file_type`— asi que no hay un
    segundo parametro que pueda discrepar del nombre del archivo.
    """
    _acotar_memoria()
    dir_trabajo = Path(dir_trabajo_txt)
    try:
        from cutter3d.malla import convertir  # noqa: PLC0415 — ver el docstring del modulo

        _avisar(dir_trabajo, ETAPA_CONVIRTIENDO_MALLA)
        reporte = convertir(Path(entrada_txt), dir_trabajo / salida_txt)

        _terminar_bien(
            dir_trabajo,
            {clave_txt: salida_txt},
            {
                "formato_original": reporte.origen,
                "formato_destino": reporte.destino,
                "objetos": list(reporte.objetos),
                "triangulos": reporte.triangulos,
                "medidas_mm": list(reporte.medidas_mm),
                "cerrado": reporte.cerrado,
                "volumen_mm3": reporte.volumen_mm3,
                # Las dos modificaciones que el formato obliga se declaran, con
                # el mismo criterio que la reduccion por presupuesto de F2: lo
                # que se toco se dice, con el numero.
                "unidad_origen": reporte.unidad_origen,
                "escala_a_mm": reporte.escala_a_mm,
                "cuerpos_unidos": reporte.cuerpos_unidos,
                "advertencias": list(reporte.advertencias),
            },
        )
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
