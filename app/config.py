"""Ajustes de la capa web.

**Todo lo que cambia entre "corre en mi maquina" y "corre expuesto a internet"
vive aca y en ningun otro lado.** El dia que esto sea un servicio, migrar es
cambiar valores en este archivo o pasarlos por entorno — no salir a cazar
constantes desperdigadas por los routers.

Los valores sensibles al entorno se pueden pisar con variables:

    STUDIOCUTTER_SECRET         secreto de firma de la sesion
    STUDIOCUTTER_COOKIE_SECURE  "1" cuando haya HTTPS adelante
    STUDIOCUTTER_DIR_TRABAJO    directorio de trabajos (util en tests)

⛔ Nunca se leen desde un `.env`: el workflow lo prohibe. Variables del sistema
o los defaults de este archivo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RAIZ = Path(__file__).resolve().parent.parent

MB = 1024 * 1024


class ErrorDeConfiguracion(RuntimeError):
    """La app no puede arrancar por como esta configurado el entorno."""


def _bandera(nombre: str, *, por_defecto: bool) -> bool:
    crudo = os.environ.get(nombre)
    if crudo is None:
        return por_defecto
    return crudo.strip().lower() in {"1", "true", "si", "yes", "on"}


@dataclass(frozen=True)
class Ajustes:
    """Configuracion de la aplicacion. Inmutable: se arma una vez al arrancar."""

    raiz: Path = RAIZ
    dir_trabajo: Path = RAIZ / "trabajo"
    archivo_credenciales: Path = RAIZ / "credenciales.json"
    archivo_secreto: Path = RAIZ / "sesion.key"

    # ── Limites operativos (decididos con el usuario en el spec) ──────────
    tamano_maximo_mb: int = 25
    """25 MB: un scan de 1500 px ronda los 3-8 MB, asi que deja margen comodo
    sin abrir la puerta a un archivo absurdo."""

    timeout_trabajo_s: int = 120
    """120 s: ~17 veces lo que tarda el fixture de referencia (3,7 s + arranque
    del hijo). Un dibujo mucho mas complejo entra; algo colgado se corta."""

    max_trabajos_simultaneos: int = 3
    """Cuantos procesos hijo pueden estar vivos a la vez.

    Sin tope, N pedidos son N procesos, y cada uno carga numpy, scipy y trimesh
    completos: media docena alcanza para dejar la maquina sin memoria. Tres es
    holgado para un usuario y acota el peor caso a algo que el sistema aguanta.
    """

    ttl_trabajo_h: int = 6
    intervalo_limpieza_s: int = 900

    # ── Sesion ────────────────────────────────────────────────────────────
    duracion_sesion_s: int = 604800  # 7 dias, renovando en cada uso
    cookie_nombre: str = "studiocutter_sesion"
    cookie_secure: bool = False
    """False en localhost, que no tiene HTTPS. Con un proxy TLS adelante se
    pone en True por `STUDIOCUTTER_COOKIE_SECURE=1`."""

    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    """Tipado cerrado y no `str`: el valor va derecho a la cookie, y un typo
    silencioso ahi es una proteccion CSRF que deja de existir sin avisar."""

    @property
    def tamano_maximo_bytes(self) -> int:
        return self.tamano_maximo_mb * MB

    @property
    def ttl_trabajo_s(self) -> int:
        return self.ttl_trabajo_h * 3600


def cargar_ajustes() -> Ajustes:
    """Arma los ajustes aplicando los overrides de entorno."""
    dir_trabajo = os.environ.get("STUDIOCUTTER_DIR_TRABAJO")
    return Ajustes(
        dir_trabajo=Path(dir_trabajo).resolve() if dir_trabajo else RAIZ / "trabajo",
        cookie_secure=_bandera("STUDIOCUTTER_COOKIE_SECURE", por_defecto=False),
    )
