"""Excepciones del dominio de cutter3d.

Politica de fallos (spec, decision D):

- **Falla duro** lo que produciria un archivo invalido: parametros fuera de rango,
  SVG que no se puede interpretar, escala que no converge, booleana que no cierra,
  malla que no queda manifold. Levantan excepcion y NO se escribe ningun archivo.
- **Advierte** lo que produce un archivo valido pero dificil de imprimir: huecos
  finos, trazo por encima del objetivo, muescas selladas por la dilatacion. Eso no
  levanta nada: va a `verify.ReporteFidelidad.advertencias` con sus numeros.

El contrato (`prompt_cortante.md`) es explicito: "no lo arregles por tu cuenta",
"reportalos como salgan". Nada de fallbacks silenciosos.
"""

from __future__ import annotations


class Cutter3DError(Exception):
    """Base de todos los errores del motor."""


class ParametroFueraDeRango(Cutter3DError):
    """Un parametro de dimension viola el rango permitido (> 0 y <= 1000 mm)."""

    def __init__(self, parametro: str, valor: object, limite: str) -> None:
        self.parametro = parametro
        self.valor = valor
        self.limite = limite
        super().__init__(f"{parametro} = {valor!r}: {limite}")


class SvgInvalido(Cutter3DError):
    """El SVG no se puede interpretar como line art de paths rellenos."""

    def __init__(self, ruta: str, motivo: str) -> None:
        self.ruta = ruta
        self.motivo = motivo
        super().__init__(f"{ruta}: {motivo}")


class NoConvergeError(Cutter3DError):
    """El bucle escala <-> dilatacion no cerro dentro del tope de iteraciones.

    Se levanta con los valores medidos en la ultima vuelta para que el usuario
    pueda decidir; nunca se cae al ultimo valor en silencio.
    """

    def __init__(
        self,
        iteraciones: int,
        lado_obtenido_mm: float,
        lado_objetivo_mm: float,
        mediana_trazo_mm: float,
    ) -> None:
        self.iteraciones = iteraciones
        self.lado_obtenido_mm = lado_obtenido_mm
        self.lado_objetivo_mm = lado_objetivo_mm
        self.mediana_trazo_mm = mediana_trazo_mm
        super().__init__(
            f"la escala no convergio en {iteraciones} iteraciones: "
            f"lado mayor {lado_obtenido_mm:.3f} mm contra un objetivo de "
            f"{lado_objetivo_mm:.3f} mm, con mediana de trazo {mediana_trazo_mm:.3f} mm"
        )


class BooleanaFallida(Cutter3DError):
    """Una union booleana no produjo un solido utilizable."""

    def __init__(self, objeto: str, motivo: str) -> None:
        self.objeto = objeto
        self.motivo = motivo
        super().__init__(f"la booleana de '{objeto}' fallo: {motivo}")


class MallaNoManifold(Cutter3DError):
    """Un objeto releido del .3mf exportado no es un solido cerrado."""

    def __init__(self, objeto: str, watertight: bool, euler: int, euler_esperado: int) -> None:
        self.objeto = objeto
        self.watertight = watertight
        self.euler = euler
        self.euler_esperado = euler_esperado
        super().__init__(
            f"'{objeto}' no es manifold: watertight={watertight}, "
            f"numero de Euler {euler} (se esperaba {euler_esperado})"
        )


class CuerpoSueltoEnCortador(Cutter3DError):
    """El cortador quedo con una pieza que no rodea nada de galletita.

    Una pieza del cortador es un anillo: rodea el pedazo de galletita que corta y
    por ahi se sostiene. Una que no rodea nada sale de la impresora como un
    fragmento de filo flotando adentro del cortante — no se puede usar y no se
    puede pegar. Entra en la categoria de **archivo invalido**, no en la de
    "dificil de imprimir": no hay nada que el usuario pueda hacer al imprimirlo.

    Con el puenteo de `geometry._puentear_colisiones` esto es inalcanzable — los
    cuerpos sueltos son exactamente los huecos de `o1`, y el puenteo los rellena
    en la silueta antes de los offsets. Queda como guarda, por lo mismo que
    `exigir_manifold` sigue chequeando el watertight de una malla que la booleana
    ya deberia haber cerrado: si aparece, es un bug de este motor y no del dibujo.
    Por eso **no** esta en `_TRADUCCIONES` de `app/errores.py` y sale como
    `interno` 500 con traza, igual que `ConversionInfiel`.
    """

    def __init__(self, cuerpos: int, area_mm2: float) -> None:
        self.cuerpos = cuerpos
        self.area_mm2 = area_mm2
        super().__init__(
            f"el cortador quedo con {cuerpos} cuerpo(s) suelto(s) "
            f"({area_mm2:.2f} mm2) que no rodean galletita"
        )


class ImagenInvalida(Cutter3DError):
    """La imagen de entrada no existe, esta vacia o su formato no esta soportado."""

    def __init__(self, ruta: str, motivo: str) -> None:
        self.ruta = ruta
        self.motivo = motivo
        super().__init__(f"{ruta}: {motivo}")


class MallaIlegible(Cutter3DError):
    """Un .3mf o .stl que entro desde afuera no se puede leer como malla.

    Es el gemelo de `SvgInvalido` en el otro extremo del pipeline: aca el archivo
    no lo produjo este motor y puede ser cualquier cosa —un zip que no es 3MF, una
    escena vacia, una malla mas grande que el techo declarado—. La ruta queda en
    el atributo y **nunca** en lo que ve el cliente, por lo mismo que en
    `SvgInvalido`: `app/errores.py` arma el mensaje desde `.motivo`.

    Es distinto de `BooleanaFallida` y de `MallaNoManifold`, que hablan de un
    solido que ESTE motor no pudo construir o cerrar. Aca no se construyo nada:
    no se pudo ni leer.
    """

    def __init__(self, ruta: str, motivo: str) -> None:
        self.ruta = ruta
        self.motivo = motivo
        super().__init__(f"{ruta}: {motivo}")


class ConversionInfiel(Cutter3DError):
    """El `.glb` derivado de una malla no describe la misma geometria que ella.

    No es un problema del archivo del usuario sino de este motor, y por eso no
    esta en `_TRADUCCIONES` de `app/errores.py`: sale como `interno` 500 y se
    loguea con traza. Existe por la misma razon que `verify` relee el `.3mf` del
    disco — una conversion que no se comprueba no prueba nada, y la foto que sale
    de ese `.glb` se presenta como la foto del archivo que se subio.
    """

    def __init__(self, magnitud: str, esperado: object, obtenido: object) -> None:
        self.magnitud = magnitud
        self.esperado = esperado
        self.obtenido = obtenido
        super().__init__(f"la conversion no preservo {magnitud}: {esperado!r} -> {obtenido!r}")
