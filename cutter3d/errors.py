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


class ImagenInvalida(Cutter3DError):
    """La imagen de entrada no existe, esta vacia o su formato no esta soportado."""

    def __init__(self, ruta: str, motivo: str) -> None:
        self.ruta = ruta
        self.motivo = motivo
        super().__init__(f"{ruta}: {motivo}")
