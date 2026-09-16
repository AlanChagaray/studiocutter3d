"""Lectura de una malla que este motor NO construyo, y su `.glb` para el visor.

Todo el resto del paquete parte de un SVG y termina en un `.3mf`. Este modulo va
en la direccion contraria: toma un `.3mf` o un `.stl` **que entro desde afuera**
—de una corrida vieja, o de cualquier otro programa— y produce el `.glb` que la
capa web le da al visor de three.js, para que se le pueda sacar la misma foto
cenital que a un cortante recien generado.

## Por que no vive en `export.py`

`export.py` serializa lo que este motor acaba de construir: su entrada es
confiable y no tiene que defenderse de nada. Aca la entrada es un archivo que
subio un usuario, asi que el modulo carga cotas explicitas y un formato que hay
que confirmar. Son dos modelos de amenaza distintos y conviene que se vean
distintos; la mitad que si comparten —escribir el `.glb`— se reusa llamando a
`export.exportar_glb`, que ya escribe con temporal y rename.

## Las dos cosas que no se deducen leyendo trimesh

1. **Un `.3mf` no transporta materiales.** Medido en este repo: el `.3mf` que
   escribe `export` vuelve a leerse con la geometria intacta (mismos bounds,
   mismos triangulos, mismo watertight) pero **sin material**, y el `.glb`
   derivado sale sin array `materials`. Un glTF sin materiales usa el default de
   la spec —`metallicFactor` 1.0, `roughnessFactor` 1.0—, o sea que la pieza se
   veria como metal rugoso en vez de PLA mate, y el front solo pisa el color
   (`pintarPieza`), nunca el acabado. Por eso `a_glb` llama a
   `solids.aplicar_acabado`: es lo unico que hace que la foto de un archivo viejo
   sea la misma foto que la de su ciclo.

2. **La escala y la orientacion si se conservan.** Tambien medido: los bounds del
   `.3mf`, los del `.stl` y los del `.glb` del motor coinciden al milimetro, con
   la pieza apoyada en z=0 y Z arriba. El visor rota -90 grados en X al cargar,
   y eso vale igual para un archivo derivado. No hay nada que reorientar.

## Las cotas

`MAX_TRIANGULOS` de aca y `paquete3mf.MAX_DESCOMPRIMIDO` estan calibradas entre
si a proposito: son las dos mitades de la misma defensa —una acota lo que se
descomprime y la otra lo que se materializa— y mover una sin mirar la otra deja
un agujero.

Importan porque el techo de RAM del proceso hijo (`app/tareas.py`) **es POSIX
only**: en Windows no hay `RLIMIT_DATA` y estas cotas son la unica defensa. Por
eso se aplican, en lo posible, **antes** de materializar la escena —para eso esta
`cota_de_triangulos`, que deduce el techo del tamaño del archivo sin abrirlo.

La confirmacion estructural del 3MF vive en `paquete3mf.py`, aparte, porque la
capa web la necesita **sin** pagar el import de trimesh. Ver su docstring.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

from .errors import ConversionInfiel, MallaIlegible
from .export import exportar_glb
from .paquete3mf import confirmar_3mf
from .params import MAX_ARCHIVOS_POR_DISENO
from .solids import NOMBRE_CORTADOR, NOMBRE_MARCADOR, aplicar_acabado

MAX_TRIANGULOS = 1_000_000
"""Techo de triangulos de la escena entera. Los cortantes de este motor rondan 5.000.

Calibrado contra `paquete3mf.MAX_DESCOMPRIMIDO`, que es la otra mitad de la
misma defensa: uno acota lo que se descomprime, este lo que se materializa."""

TOLERANCIA_MM = 1e-4
"""Cuanto puede moverse un borde entre la malla leida y el `.glb` escrito."""

TOLERANCIA_VOLUMEN = 1e-6
"""Desvio relativo admitido en el volumen. Es un roundtrip binario: deberia dar 0."""

SUFIJOS = {".3mf": "3mf", ".stl": "stl"}
"""Extension en disco -> `file_type` de trimesh. El tipo se fuerza, nunca se adivina."""

ROL_CORTADOR = "cortador"
ROL_MARCADOR = "marcador"
ROL_UNICO = "unico"
ROLES = (ROL_CORTADOR, ROL_MARCADOR, ROL_UNICO)
"""Que es cada archivo dentro de su diseño.

Lo decide quien arma los grupos —el navegador, que es el unico que ve los nombres
originales— y lo valida la capa web contra esta lista. **Aca no se mira ningun
nombre de archivo**: para este modulo el rol es un dato de entrada, no algo que
se deduzca. Es lo que mantiene la regla de que el cliente no nombra nada en
disco: manda un rol de un conjunto cerrado, no un nombre."""

_CABECERA_STL_BINARIO = 84
"""80 bytes de cabecera libre + un uint32 con la cantidad de triangulos."""

_BYTES_POR_TRIANGULO_STL = 50
"""12 floats de 4 bytes (normal + 3 vertices) + 2 de atributo. El formato es fijo."""


@dataclass(frozen=True)
class ReporteMalla:
    """Lo que se puede afirmar del archivo que se subio, medido sobre el `.glb`.

    Es la version chica de `verify.ReporteFidelidad`: no hay dibujo original
    contra el cual comparar, asi que lo unico demostrable es que el `.glb` que se
    va a fotografiar describe la misma geometria que el archivo de entrada.
    """

    objetos: tuple[str, ...]
    triangulos: int
    medidas_mm: tuple[float, float, float]
    cerrado: bool
    volumen_mm3: float
    advertencias: tuple[str, ...]


def cota_de_triangulos(ruta: Path, tipo: str) -> int | None:
    """Techo de triangulos deducible del tamaño del archivo, SIN abrirlo como malla.

    Existe por el riesgo declarado en el plan: contar triangulos despues de
    cargar llega tarde en Windows, donde no hay techo de RAM en el proceso hijo.
    Para STL la cota es exacta o casi —el formato tiene tamaño fijo por
    triangulo—; para 3MF no hay forma barata y devuelve `None`, porque ahi el
    limite lo pone `MAX_DESCOMPRIMIDO`, que se chequea en `confirmar_3mf`.
    """
    if tipo != "stl":
        return None
    tamano = ruta.stat().st_size
    with ruta.open("rb") as archivo:
        cabecera = archivo.read(_CABECERA_STL_BINARIO)
    if len(cabecera) == _CABECERA_STL_BINARIO:
        declarados = int.from_bytes(cabecera[80:84], "little")
        if tamano == _CABECERA_STL_BINARIO + declarados * _BYTES_POR_TRIANGULO_STL:
            return declarados
    # ASCII: la facet mas corta que se puede escribir pasa los 50 caracteres
    # (`facet normal` + `outer loop` + tres `vertex` + los dos `end`), asi que
    # dividir por 50 es una cota superior holgada y siempre valida.
    return tamano // _BYTES_POR_TRIANGULO_STL


def cargar_malla(ruta: Path) -> trimesh.Scene:
    """Lee un `.3mf` o un `.stl` como escena, con el tipo forzado por la extension.

    El `file_type` se pasa explicito en vez de dejar que trimesh olfatee: quien
    decidio que esto es un 3MF fue la deteccion por bytes de la capa web, y que
    una segunda pieza vuelva a decidirlo por su cuenta es justamente como se
    desincronizan dos verdades sobre el mismo archivo.
    """
    tipo = SUFIJOS.get(ruta.suffix.lower())
    if tipo is None:
        raise MallaIlegible(str(ruta), f"extension no soportada: '{ruta.suffix}'")
    if tipo == "3mf":
        confirmar_3mf(ruta)

    cota = cota_de_triangulos(ruta, tipo)
    if cota is not None and cota > MAX_TRIANGULOS:
        raise MallaIlegible(
            str(ruta),
            f"la malla tiene del orden de {cota} triangulos y el maximo es {MAX_TRIANGULOS}",
        )

    try:
        escena = trimesh.load(str(ruta), file_type=tipo, force="scene")
    except MallaIlegible:
        raise
    except Exception as exc:  # trimesh levanta de todo, incluidos errores de lxml
        raise MallaIlegible(str(ruta), "no se pudo interpretar como malla 3D") from exc

    if not isinstance(escena, trimesh.Scene) or not escena.geometry:
        raise MallaIlegible(str(ruta), "el archivo no contiene ninguna malla")

    triangulos = sum(len(malla.faces) for malla in escena.geometry.values())
    if triangulos == 0:
        raise MallaIlegible(str(ruta), "el archivo no contiene ningun triangulo")
    if triangulos > MAX_TRIANGULOS:
        raise MallaIlegible(
            str(ruta), f"la malla tiene {triangulos} triangulos y el maximo es {MAX_TRIANGULOS}"
        )
    return escena


def a_glb(entradas: Sequence[Path], destino: Path, roles: Sequence[str] = ()) -> ReporteMalla:
    """Un diseño —uno o dos archivos— al `.glb` del visor, probando que es el mismo.

    **Recibe una lista porque un diseño puede venir partido en dos archivos.** El
    motor exporta el cortante y su marcador juntos en un `.3mf` y tambien sueltos
    (`<base>_cortador.stl` + `<base>_marcador.stl`), y las dos formas describen la
    misma pieza: fotografiar el cortador sin su marcador seria fotografiar otra
    cosa. Las coordenadas **no se tocan** —el motor las conserva entre el
    combinado y los sueltos, medido— asi que unir las escenas los reencuentra
    exactamente anidados, sin mover nada.

    `roles` dice que es cada archivo (`cortador`, `marcador` o `unico`) y lo
    decide quien arma los grupos, no este modulo: aca no se mira ningun nombre de
    archivo. Sirve para dos cosas — el color de cada cuerpo y como se lo nombra
    en el reporte. Un `unico` que ya trae los objetos nombrados (el `.3mf`
    combinado) conserva sus nombres.

    El acabado se re-aplica siempre (ver el docstring del modulo) y la
    equivalencia se comprueba **releyendo el `.glb` del disco**, por la misma
    razon que `verify` relee el `.3mf`: comparar la escena en memoria contra si
    misma no prueba nada sobre el archivo que se entrega.
    """
    if not entradas:
        raise MallaIlegible("(sin archivos)", "un diseño necesita al menos un archivo")
    if len(entradas) > MAX_ARCHIVOS_POR_DISENO:
        raise MallaIlegible(
            str(entradas[0]),
            f"un diseño admite hasta {MAX_ARCHIVOS_POR_DISENO} archivos y llegaron {len(entradas)}",
        )

    escena = trimesh.Scene()
    triangulos = 0
    for i, ruta in enumerate(entradas):
        rol = roles[i] if i < len(roles) else ROL_UNICO
        parcial = cargar_malla(ruta)
        for crudo, malla in parcial.geometry.items():
            objeto = _objeto_de(rol, str(crudo))
            aplicar_acabado(malla, objeto)
            escena.add_geometry(malla, geom_name=_libre(escena, objeto or "cuerpo"))
        triangulos += sum(len(m.faces) for m in parcial.geometry.values())
        # La escena parcial ya no se necesita: sus mallas se movieron a `escena`
        # por referencia, y lo que queda es el indice. Soltarlo importa poco de a
        # uno y bastante en un lote de 25 diseños seguidos.
        del parcial

    # El techo vale para el diseño ENTERO y no por archivo: dos mitades de
    # 600.000 triangulos pasan `cargar_malla` sueltas y juntas no entran.
    if triangulos > MAX_TRIANGULOS:
        raise MallaIlegible(
            str(entradas[0]),
            f"el diseño suma {triangulos} triangulos y el maximo es {MAX_TRIANGULOS}",
        )

    exportar_glb(escena, destino)
    return _comprobar(escena, destino)


def _objeto_de(rol: str, crudo: str) -> str | None:
    """El nombre de cuerpo del motor que le corresponde a esta malla, o `None`.

    `None` significa "no se sabe": `aplicar_acabado` lo trata como cortador, que
    es el caso normal de una malla anonima.
    """
    if rol == ROL_CORTADOR:
        return NOMBRE_CORTADOR
    if rol == ROL_MARCADOR:
        return NOMBRE_MARCADOR
    # Rol `unico`: si el archivo ya traia los objetos nombrados (el `.3mf`
    # combinado del motor) se respetan; si no, no se inventa ninguno.
    return crudo if crudo in {NOMBRE_MARCADOR, NOMBRE_CORTADOR} else None


def _libre(escena: trimesh.Scene, propuesto: str) -> str:
    """Un nombre de geometria que no pise a otro ya cargado.

    Hace falta porque los dos archivos de un diseño pueden traer cuerpos
    homonimos, y `add_geometry` con un nombre repetido **reemplaza en silencio**:
    el segundo archivo se comeria al primero y el diseño saldria a medias sin un
    solo error.
    """
    if propuesto not in escena.geometry:
        return propuesto
    i = 2
    while f"{propuesto}-{i}" in escena.geometry:
        i += 1
    return f"{propuesto}-{i}"


def _nombrar(crudos: list[object]) -> tuple[str, ...]:
    """Nombres de cuerpo presentables, sin filtrar el nombre del archivo en disco.

    Un `.3mf` de este motor trae los objetos nombrados (`marcador`, `cortador`) y
    esos se conservan: dicen algo. STL **no tiene nombres de objeto** —el formato
    no los soporta— y trimesh usa el nombre del archivo, que aca siempre es
    `entrada.stl` porque asi lo guarda la capa web. Mostrar eso seria filtrar un
    detalle interno y ademas no informa nada, que es la peor combinacion.
    """
    conocidos = {NOMBRE_MARCADOR, NOMBRE_CORTADOR}
    if all(str(n) in conocidos for n in crudos):
        return tuple(str(n) for n in crudos)
    if len(crudos) == 1:
        return ("cuerpo",)
    return tuple(f"cuerpo {i}" for i in range(1, len(crudos) + 1))


def _comprobar(escena: trimesh.Scene, glb: Path) -> ReporteMalla:
    """Relee el `.glb` escrito y lo compara contra la malla de entrada."""
    try:
        releida = trimesh.load(str(glb), file_type="glb", force="scene")
    except Exception as exc:
        raise ConversionInfiel("la lectura del glb", "una escena", "un error") from exc
    if not isinstance(releida, trimesh.Scene):
        raise ConversionInfiel("la forma del glb", "Scene", type(releida).__name__)

    esperados = sum(len(malla.faces) for malla in escena.geometry.values())
    obtenidos = sum(len(malla.faces) for malla in releida.geometry.values())
    if esperados != obtenidos:
        raise ConversionInfiel("los triangulos", esperados, obtenidos)

    if not np.allclose(escena.bounds, releida.bounds, atol=TOLERANCIA_MM):
        raise ConversionInfiel(
            "las medidas",
            np.round(escena.extents, 4).tolist(),
            np.round(releida.extents, 4).tolist(),
        )

    volumen = float(sum(abs(malla.volume) for malla in releida.geometry.values()))
    volumen_entrada = float(sum(abs(malla.volume) for malla in escena.geometry.values()))
    if (
        volumen_entrada > 0
        and abs(volumen - volumen_entrada) / volumen_entrada > TOLERANCIA_VOLUMEN
    ):
        raise ConversionInfiel("el volumen", volumen_entrada, volumen)

    cerrado = all(malla.is_watertight for malla in releida.geometry.values())
    advertencias: list[str] = []
    if not cerrado:
        # Advierte y no falla: un archivo viejo imperfecto igual merece su foto, y
        # esta pantalla no imprime nada. Quien lo mande a la impresora tiene que
        # enterarse, por eso se dice; quien solo quiere la foto, no se traba.
        advertencias.append(
            "La malla no es un solido cerrado. La foto sale igual, pero el archivo "
            "puede dar problemas al laminar."
        )

    medidas = releida.extents
    return ReporteMalla(
        objetos=_nombrar(list(releida.geometry)),
        triangulos=obtenidos,
        medidas_mm=(
            round(float(medidas[0]), 3),
            round(float(medidas[1]), 3),
            round(float(medidas[2]), 3),
        ),
        cerrado=cerrado,
        volumen_mm3=round(volumen, 3),
        advertencias=tuple(advertencias),
    )
