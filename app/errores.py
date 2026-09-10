"""Traduccion de errores del motor a respuestas HTTP seguras.

**La regla que justifica este modulo entero:** las excepciones de `cutter3d`
llevan la ruta del archivo en su `str()` — `SvgInvalido` formatea
`f"{ruta}: {motivo}"`. Mandar eso al cliente filtra el filesystem del servidor.
Por eso **el mensaje se arma desde los atributos** (`.motivo`, `.parametro`,
`.limite`) y nunca con `str(exc)`.

Lo que no tenga atributos seguros se responde como `interno` con un mensaje
generico, y el detalle completo va al log del servidor, donde si corresponde.

Los codigos son un conjunto cerrado: el front puede ramificar sobre ellos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from cutter3d.errors import (
    BooleanaFallida,
    Cutter3DError,
    ImagenInvalida,
    MallaNoManifold,
    NoConvergeError,
    ParametroFueraDeRango,
    SvgInvalido,
)

log = logging.getLogger("studiocutter")

MENSAJE_GENERICO = "No se pudo procesar el archivo. Revisa el log del servidor para el detalle."


class ErrorApi(Exception):
    """Error con forma de respuesta JSON uniforme."""

    def __init__(
        self,
        codigo: str,
        mensaje: str,
        *,
        estado: int = 400,
        detalle: dict[str, Any] | None = None,
    ) -> None:
        self.codigo = codigo
        self.mensaje = mensaje
        self.estado = estado
        self.detalle = detalle or {}
        super().__init__(f"{codigo}: {mensaje}")

    def como_json(self) -> dict[str, Any]:
        cuerpo: dict[str, Any] = {"codigo": self.codigo, "mensaje": self.mensaje}
        if self.detalle:
            cuerpo["detalle"] = self.detalle
        return {"error": cuerpo}


class RedireccionALogin(Exception):
    """Una pagina protegida pedida sin sesion. La maneja el handler de la app."""

    def __init__(self, destino: str) -> None:
        self.destino = destino
        super().__init__(destino)


@dataclass(frozen=True)
class Traduccion:
    codigo: str
    estado: int


_TRADUCCIONES: dict[type[Cutter3DError], Traduccion] = {
    ParametroFueraDeRango: Traduccion("parametro_invalido", 422),
    SvgInvalido: Traduccion("svg_invalido", 422),
    ImagenInvalida: Traduccion("imagen_invalida", 422),
    NoConvergeError: Traduccion("no_converge", 422),
    BooleanaFallida: Traduccion("malla_invalida", 422),
    MallaNoManifold: Traduccion("malla_invalida", 422),
}


def _detalle_seguro(exc: Cutter3DError) -> tuple[str, dict[str, Any]]:
    """Mensaje y detalle armados desde los ATRIBUTOS, nunca desde `str(exc)`."""
    if isinstance(exc, ParametroFueraDeRango):
        return (
            f"El parametro '{exc.parametro}' {exc.limite}.",
            {"parametro": exc.parametro, "limite": exc.limite},
        )
    if isinstance(exc, (SvgInvalido, ImagenInvalida)):
        return (f"El archivo no se pudo interpretar: {exc.motivo}", {})
    if isinstance(exc, NoConvergeError):
        return (
            "El dibujo no converge al tamaño pedido: quedo en "
            f"{exc.lado_obtenido_mm:.2f} mm contra {exc.lado_objetivo_mm:.2f} mm "
            f"despues de {exc.iteraciones} iteraciones.",
            {"lado_obtenido_mm": exc.lado_obtenido_mm, "iteraciones": exc.iteraciones},
        )
    if isinstance(exc, MallaNoManifold):
        return (
            f"El solido '{exc.objeto}' no cerro: no se puede imprimir. "
            "Proba con un trazo mas grueso o un tamaño mayor.",
            {"objeto": exc.objeto},
        )
    if isinstance(exc, BooleanaFallida):
        return (f"No se pudo construir el solido '{exc.objeto}'.", {"objeto": exc.objeto})
    return (MENSAJE_GENERICO, {})


def traducir(exc: Exception) -> ErrorApi:
    """Cualquier excepcion a un `ErrorApi`. Nunca deja escapar una ruta."""
    if isinstance(exc, ErrorApi):
        return exc
    if isinstance(exc, Cutter3DError):
        traduccion = _TRADUCCIONES.get(type(exc), Traduccion("interno", 500))
        mensaje, detalle = _detalle_seguro(exc)
        if traduccion.codigo == "interno":
            log.exception("error no mapeado del motor", exc_info=exc)
        return ErrorApi(traduccion.codigo, mensaje, estado=traduccion.estado, detalle=detalle)
    log.exception("error inesperado", exc_info=exc)
    return ErrorApi("interno", MENSAJE_GENERICO, estado=500)


def como_dict(exc: Exception) -> dict[str, Any]:
    """Serializa un error para `estado.json` — mismo criterio de seguridad."""
    api = traducir(exc)
    return {"codigo": api.codigo, "mensaje": api.mensaje, "detalle": api.detalle}
