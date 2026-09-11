"""Parametros del cortante y ajustes internos del motor.

`CutterParams` son las **10 dimensiones** del producto, las que el usuario edita.
Sus defaults salen literalmente de `prompt_cortante.md`. La validacion de rango
vive aca y solo aca: ningun valor <= 0, ninguno > 1000 mm.

`AjustesMotor` es tuning: resolucion de rasterizado, tolerancias, topes. No son
dimensiones del producto y tienen sus propios limites.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import ClassVar

from .errors import ParametroFueraDeRango


@dataclass(frozen=True)
class CutterParams:
    """Las 10 dimensiones del cortante y el marcador, en milimetros.

    Los offsets del cortador se **derivan** de estos valores en vez de estar
    hardcodeados: si se cambia el ancho del filo, la geometria sigue siendo
    coherente en lugar de romperse contra constantes sueltas. Con los defaults
    dan 0,7 / 1,7 / 3,5 mm, que son los del contrato.
    """

    lado_mayor_mm: float = 90.0
    altura_base_mm: float = 1.0
    altura_trazos_mm: float = 3.0
    ancho_trazo_mm: float = 1.0
    luz_mm: float = 0.7
    filo_ancho_mm: float = 1.0
    filo_alto_mm: float = 10.0
    pie_ancho_extra_mm: float = 1.8
    pie_alto_mm: float = 2.0
    distancia_colision_mm: float = 1.0
    """Luz remanente entre dos extremos del filo por debajo de la cual se fuerza la colision.

    Se mide sobre `o2`, el borde externo del filo, que es donde las dos paredes se
    encuentran. Con los defaults, dos extremos con una boca de silueta menor o igual
    a 4,4 mm (2*o2 + esto) quedan fusionados y su muesca deja de cortarse.
    """

    LIMITE_MAX_MM: ClassVar[float] = 1000.0

    def __post_init__(self) -> None:
        for campo in fields(self):
            valor: object = getattr(self, campo.name)
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                raise ParametroFueraDeRango(campo.name, valor, "tiene que ser un numero")
            numero = float(valor)
            if not math.isfinite(numero):
                raise ParametroFueraDeRango(campo.name, valor, "tiene que ser un numero finito")
            if numero <= 0:
                raise ParametroFueraDeRango(campo.name, valor, "tiene que ser mayor que 0")
            if numero > self.LIMITE_MAX_MM:
                raise ParametroFueraDeRango(
                    campo.name, valor, f"no puede superar {self.LIMITE_MAX_MM:.0f} mm"
                )

    @property
    def offset_o1_mm(self) -> float:
        """Borde interno del cortador: la luz que lo separa del marcador."""
        return self.luz_mm

    @property
    def offset_o2_mm(self) -> float:
        """Borde externo del filo."""
        return self.luz_mm + self.filo_ancho_mm

    @property
    def offset_o3_mm(self) -> float:
        """Borde externo del pie."""
        return self.luz_mm + self.filo_ancho_mm + self.pie_ancho_extra_mm

    @property
    def altura_total_marcador_mm(self) -> float:
        """Base maciza + trazos extruidos encima."""
        return self.altura_base_mm + self.altura_trazos_mm

    @property
    def ancho_total_cortador_mm(self) -> float:
        """Ancho del cortador medido desde la luz hacia afuera."""
        return self.filo_ancho_mm + self.pie_ancho_extra_mm


@dataclass(frozen=True)
class AjustesMotor:
    """Tuning del motor. No son dimensiones del producto."""

    px_por_mm: float = 20.0
    """Resolucion del rasterizado para medir el ancho de trazo.

    20 px/mm = 0,05 mm por pixel, la misma tolerancia con la que se aplanan las
    curvas del SVG: la medicion no es mas gruesa que el arte. Un trazo de 1 mm se
    mide sobre 20 pixeles, suficiente para percentiles confiables.
    """

    flatten_mm: float = 0.05
    simplify_arte_mm: float = 0.02
    simplify_offsets_mm: float = 0.01
    poda_esqueleto_mm: float = 0.5
    umbral_muesca_mm: float = 0.15
    umbral_hueco_imprimible_mm: float = 0.4
    max_iteraciones: int = 12
    tol_convergencia_mm: float = 0.05
    max_px_lado: int = 6000
    """Tope duro del rasterizado, para que una resolucion alta no reviente la memoria."""

    def __post_init__(self) -> None:
        for campo in fields(self):
            valor: object = getattr(self, campo.name)
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                raise ParametroFueraDeRango(campo.name, valor, "tiene que ser un numero")
            numero = float(valor)
            if not math.isfinite(numero) or numero <= 0:
                raise ParametroFueraDeRango(
                    campo.name, valor, "tiene que ser un numero finito mayor que 0"
                )
        if self.max_iteraciones < 1:
            raise ParametroFueraDeRango(
                "max_iteraciones", self.max_iteraciones, "tiene que ser al menos 1"
            )
