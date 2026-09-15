"""Punto de entrada: arma la app, monta todo y define el manejo de errores.

    .venv/Scripts/python -m uvicorn app.main:app --port 8000

Cuatro cosas que solo pueden vivir aca:

- **El limite de subida por `Content-Length`**, como middleware. Tiene que
  cortar *antes* de que Starlette lea el cuerpo: si se chequea dentro del
  handler, el archivo de 500 MB ya se escribio en el temporal del sistema y el
  daño esta hecho. El chequeo por bytes de `archivos.guardar_subida` sigue
  estando, para el cliente que miente en el header.
- **Los handlers de excepcion**, que son lo que garantiza que la forma del
  error sea la misma en toda la API y que ninguna ruta del servidor se escape
  al cliente.
- **La limpieza por TTL**, como tarea de fondo del ciclo de vida.
- **El armado del borde** (`proteccion.instalar`) y el `/salud`: son lo unico
  que se agrega al exponer la app fuera de localhost, y el orden en que se
  apilan los middleware solo se puede decidir donde se arma la app.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from urllib.parse import quote

from anyio import to_thread
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, proteccion, trabajos
from .archivos import limpiar_vencidos
from .dependencias import obtener_ajustes, obtener_almacen
from .errores import ErrorApi, RedireccionALogin, traducir
from .routers import auth, conversor, cortante, lineas, paginas
from .routers import trabajos as router_trabajos
from .seguridad import obtener_secreto_sesion

log = logging.getLogger("studiocutter")

MARGEN_MULTIPART = 64 * 1024
"""Lo que el envoltorio multipart suma sobre el archivo en si (bordes,
cabeceras de cada parte). Sin este margen, un archivo justo en el limite se
rechazaria por el peso del sobre y no por el del contenido."""


HILOS_MAXIMOS = 8
"""Tope del threadpool donde corren los handlers `def`.

anyio trae 40 por default, y **todos** los handlers de esta app son `def`: cada
pedido en vuelo ocupa un hilo. Ese numero es el multiplicador de todos los picos
de memoria del proceso web — 40 logins concurrentes son 40 veces el costo de un
`verify` de argon2, y 40 conversiones son 40 imagenes decodificadas a la vez.

En una instancia de 512 MB y 0,1 CPU, 40 hilos no compran paralelismo (no hay
CPU que repartir): solo multiplican el peor caso. `--limit-concurrency 100` de
`docker/arranque.sh` sigue siendo la red de afuera; esta es la de adentro.
"""


@asynccontextmanager
async def _ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
    to_thread.current_default_thread_limiter().total_tokens = HILOS_MAXIMOS
    tarea = asyncio.create_task(_limpiar_periodicamente())
    try:
        yield
    finally:
        tarea.cancel()
        with suppress(asyncio.CancelledError):
            await tarea


async def _limpiar_periodicamente() -> None:
    """Borra trabajos vencidos cada tanto, incluidos los huerfanos en disco."""
    a = obtener_ajustes()
    almacen = obtener_almacen()
    while True:
        try:
            vencidos = [t.id for t in almacen.vencidos(a.ttl_trabajo_s)]
            borrados = await asyncio.to_thread(limpiar_vencidos, a, almacen)
            for id_ in vencidos:
                trabajos.olvidar(id_)
            if borrados:
                log.info("limpieza: %d trabajos borrados", borrados)
        except Exception:  # la limpieza no puede tumbar el server
            log.exception("fallo la limpieza periodica")
        await asyncio.sleep(a.intervalo_limpieza_s)


def crear_app() -> FastAPI:
    a = obtener_ajustes()
    aplicacion = FastAPI(
        title="studioCutter3D",
        version=__version__,
        lifespan=_ciclo_de_vida,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    aplicacion.add_middleware(
        SessionMiddleware,
        secret_key=obtener_secreto_sesion(a),
        session_cookie=a.cookie_nombre,
        max_age=a.duracion_sesion_s,
        same_site=a.cookie_samesite,
        https_only=a.cookie_secure,
    )

    @aplicacion.middleware("http")
    async def limitar_tamano(
        request: Request, siguiente: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        crudo = request.headers.get("content-length", "")
        if crudo.isdigit() and int(crudo) > a.tamano_maximo_bytes + MARGEN_MULTIPART:
            error = ErrorApi(
                "archivo_muy_grande",
                f"El archivo supera el limite de {a.tamano_maximo_mb} MB.",
                estado=413,
            )
            return JSONResponse(error.como_json(), status_code=error.estado)
        return await siguiente(request)

    # Freno por IP, cabeceras de seguridad y filtro de `Host`. Va DESPUES del
    # limite de tamaño para quedar por fuera de el: asi el 413 tambien sale con
    # las cabeceras puestas, y un pedido que ya se paso de cupo no llega
    # siquiera a que se le lea el `Content-Length`.
    proteccion.instalar(aplicacion, a)

    aplicacion.mount(
        "/static", StaticFiles(directory=str(a.raiz / "app" / "static")), name="static"
    )

    @aplicacion.get("/salud", include_in_schema=False)
    def salud() -> dict[str, str]:
        """Latido para el orquestador (Render, compose, k8s). Sin sesion y sin datos.

        Que no exija login es deliberado —quien chequea la salud no tiene
        cuenta— y por eso no dice absolutamente nada: ni version, ni trabajos en
        curso, ni estado del disco. Un endpoint de salud conversador es un
        regalo de reconocimiento para cualquiera que pase.
        """
        return {"estado": "ok"}

    for router in (
        auth.router,
        paginas.router,
        conversor.router,
        lineas.router,
        cortante.router,
        router_trabajos.router,
    ):
        aplicacion.include_router(router)

    _registrar_errores(aplicacion)
    a.dir_trabajo.mkdir(parents=True, exist_ok=True)
    return aplicacion


def _registrar_errores(aplicacion: FastAPI) -> None:
    """Una sola forma de error para toda la API, y cero rutas del servidor."""

    @aplicacion.exception_handler(RedireccionALogin)
    async def _a_login(_: Request, exc: Exception) -> Response:
        destino = exc.destino if isinstance(exc, RedireccionALogin) else "/"
        return RedirectResponse(f"/login?destino={quote(destino, safe='/')}", status_code=303)

    @aplicacion.exception_handler(ErrorApi)
    async def _error_api(_: Request, exc: Exception) -> Response:
        api = traducir(exc)
        return JSONResponse(api.como_json(), status_code=api.estado)

    @aplicacion.exception_handler(RequestValidationError)
    async def _validacion(_: Request, exc: Exception) -> Response:
        # La forma nativa de FastAPI es otra. Se traduce para que el front
        # tenga un solo formato de error que interpretar.
        campos = _campos_invalidos(exc)
        return JSONResponse(
            ErrorApi(
                "peticion_invalida",
                "Falta un dato o tiene un valor que no se acepta.",
                estado=422,
                detalle={"campos": campos},
            ).como_json(),
            status_code=422,
        )

    @aplicacion.exception_handler(Exception)
    async def _inesperado(_: Request, exc: Exception) -> Response:
        api = traducir(exc)
        return JSONResponse(api.como_json(), status_code=api.estado)


def _campos_invalidos(exc: Exception) -> list[str]:
    if not isinstance(exc, RequestValidationError):
        return []
    return [".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()]


app = crear_app()
