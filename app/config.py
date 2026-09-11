"""Ajustes de la capa web.

**Todo lo que cambia entre "corre en mi maquina" y "corre expuesto a internet"
vive aca y en ningun otro lado.** El dia que esto sea un servicio, migrar es
cambiar valores en este archivo o pasarlos por entorno — no salir a cazar
constantes desperdigadas por los routers.

Los valores sensibles al entorno se pueden pisar con variables:

    STUDIOCUTTER_SECRET             secreto de firma de la sesion
    STUDIOCUTTER_COOKIE_SECURE      "1" cuando haya HTTPS adelante
    STUDIOCUTTER_DIR_TRABAJO        directorio de trabajos (util en tests)
    STUDIOCUTTER_CREDENCIALES       ruta alternativa a `credenciales.json`
    STUDIOCUTTER_CREDENCIALES_JSON  el JSON de credenciales en la variable misma
    STUDIOCUTTER_ARCHIVO_SECRETO    donde se persiste el secreto generado
    STUDIOCUTTER_MAX_TRABAJOS       tope de procesos hijo simultaneos
    STUDIOCUTTER_HOSTS              hosts aceptados en el header `Host`
    STUDIOCUTTER_DETRAS_DE_PROXY    "1" cuando hay un proxy inverso adelante

⛔ Nunca se leen desde un `.env`: el workflow lo prohibe. Variables del sistema
o los defaults de este archivo.

**Las seis ultimas nacieron con el despliegue en contenedor.** En la maquina
del desarrollador ninguna hace falta: los defaults son los de siempre. Lo que
cambia al publicar el servicio es que el archivo de credenciales ya no esta en
el repo (esta gitignoreado, y con razon), que la memoria del host es finita y
conocida, y que entre el navegador y la app hay un proxy que termina el TLS.
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


def _entero(nombre: str, *, por_defecto: int, minimo: int, maximo: int) -> int:
    """Entero de entorno, acotado. Un valor invalido NO se ignora: falla.

    Ignorar en silencio un `STUDIOCUTTER_MAX_TRABAJOS=cero` dejaria el tope en
    el default sin que nadie se entere, y el sintoma apareceria mucho despues —
    el contenedor muerto por OOM— lejos del typo que lo causo.
    """
    crudo = os.environ.get(nombre)
    if crudo is None or not crudo.strip():
        return por_defecto
    try:
        valor = int(crudo.strip())
    except ValueError as exc:
        raise ErrorDeConfiguracion(f"{nombre} tiene que ser un numero entero: {crudo!r}") from exc
    if not minimo <= valor <= maximo:
        raise ErrorDeConfiguracion(f"{nombre} tiene que estar entre {minimo} y {maximo}: {valor}")
    return valor


def _hosts(nombre: str) -> tuple[str, ...]:
    """Lista de hosts separados por coma. Vacia significa "cualquiera"."""
    crudo = os.environ.get(nombre, "")
    return tuple(h.strip() for h in crudo.split(",") if h.strip())


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

    # ── Borde expuesto (solo importa cuando esto no corre en localhost) ───
    detras_de_proxy: bool = False
    """Hay un proxy inverso adelante (Render, nginx, Traefik).

    Cambia UNA cosa concreta: de donde sale la IP del cliente. Con un proxy
    adelante, `request.client.host` es la IP del proxy —la misma para todo el
    mundo—, asi que frenar por IP sin mirar `X-Forwarded-For` frenaria a todos
    los usuarios juntos o a ninguno. Sin proxy, la cabecera **no se mira**:
    cualquiera puede mandarla y seria un salvoconducto para saltarse el freno.
    """

    hosts_permitidos: tuple[str, ...] = ()
    """Valores aceptados en el header `Host`. Vacio = cualquiera (localhost).

    Un `Host` arbitrario se refleja en las URLs absolutas que arma `url_for`.
    Con la lista puesta, un pedido con `Host: phishing.example` se rechaza antes
    de llegar a ningun handler."""

    max_intentos_login: int = 8
    """Intentos fallidos por IP antes del bloqueo temporal. Ocho deja lugar a
    la memoria mala del usuario legitimo y corta cualquier barrido de claves:
    con este tope, probar mil contraseñas lleva mas de dos dias."""

    ventana_login_s: int = 300
    """Los fallos se cuentan dentro de esta ventana movil."""

    bloqueo_login_s: int = 900
    """Cuanto dura el bloqueo una vez alcanzado el tope."""

    max_pedidos_ip: int = 240
    """Techo de pedidos por IP y por minuto, para toda la app.

    El numero sale de medir el peor caso legitimo: la pantalla del cortante
    pollea cada 0,8 s durante los primeros 30 s (~38 pedidos) y despues cada
    2 s, mas los estaticos de la carga inicial. 240 le deja cuatro veces de
    margen a un usuario activo con varias pestañas, y sigue siendo dos ordenes
    de magnitud menos que lo que tira un script."""

    ventana_pedidos_s: int = 60

    @property
    def tamano_maximo_bytes(self) -> int:
        return self.tamano_maximo_mb * MB

    @property
    def ttl_trabajo_s(self) -> int:
        return self.ttl_trabajo_h * 3600


def cargar_ajustes() -> Ajustes:
    """Arma los ajustes aplicando los overrides de entorno."""
    dir_trabajo = os.environ.get("STUDIOCUTTER_DIR_TRABAJO")
    credenciales = os.environ.get("STUDIOCUTTER_CREDENCIALES")
    # El unico archivo que la app ESCRIBE fuera del directorio de trabajos. En
    # un contenedor con el filesystem de solo lectura —que es como conviene
    # correrlo— tiene que poder apuntar al unico volumen escribible, o el
    # servidor no llega a arrancar.
    secreto = os.environ.get("STUDIOCUTTER_ARCHIVO_SECRETO")
    return Ajustes(
        dir_trabajo=Path(dir_trabajo).resolve() if dir_trabajo else RAIZ / "trabajo",
        archivo_credenciales=(
            Path(credenciales).resolve() if credenciales else RAIZ / "credenciales.json"
        ),
        archivo_secreto=Path(secreto).resolve() if secreto else RAIZ / "sesion.key",
        cookie_secure=_bandera("STUDIOCUTTER_COOKIE_SECURE", por_defecto=False),
        detras_de_proxy=_bandera("STUDIOCUTTER_DETRAS_DE_PROXY", por_defecto=False),
        hosts_permitidos=_hosts("STUDIOCUTTER_HOSTS"),
        # El tope de procesos hijo es lo primero que hay que bajar en un host
        # con poca RAM: cada hijo reimporta numpy, scipy y trimesh enteros, que
        # son ~400 MB. En una instancia de 512 MB el valor util es 1.
        max_trabajos_simultaneos=_entero(
            "STUDIOCUTTER_MAX_TRABAJOS", por_defecto=3, minimo=1, maximo=16
        ),
    )
