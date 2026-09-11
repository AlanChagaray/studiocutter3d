"""Defensas del borde: quien pide, cuanto puede pedir, y con que cabeceras se responde.

Hasta el ciclo anterior esto no hacia falta: la app corria en `127.0.0.1` y el
unico que podia pedirle algo era el que estaba sentado adelante. Publicarla
cambia tres cosas, y cada una es una parte de este modulo:

1. **Ya no se sabe quien es el cliente sin mirar.** Con un proxy inverso
   adelante, `request.client.host` es la IP del proxy —identica para todo el
   mundo—, asi que cualquier limite por IP frenaria a todos juntos o a nadie.
   `ip_cliente` reconstruye la real, y de la forma que NO se puede falsificar
   (ver su docstring).
2. **El login pasa a estar al alcance de cualquiera.** Un usuario y una
   contraseña sin freno son un ataque de fuerza bruta esperando a que alguien
   lo escriba. argon2id ya hace cara cada prueba; el `Freno` la hace finita.
3. **El navegador no sabe solo en que puede confiar.** Las cabeceras de
   seguridad son las que le dicen que no adivine el tipo de un archivo, que no
   deje que un tercero meta la pagina en un iframe, y de donde puede cargar
   scripts.

⛔ Ninguna de las tres decide nada sobre el contenido: no leen el cuerpo, no
tocan archivos y no saben que es un cortante. Son borde, y nada mas.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Ajustes
from .errores import ErrorApi

log = logging.getLogger("studiocutter")

IP_DESCONOCIDA = "desconocida"

MAX_CLAVES = 8192
"""Cuantas claves distintas se recuerdan a la vez.

Un contador por IP sin tope es, el mismo, un vector de agotamiento de memoria:
quien puede variar la IP de origen escribe una entrada nueva por pedido. Con el
tope, lo peor que consigue es desalojar las entradas mas viejas, que es
exactamente lo que el paso del tiempo iba a hacer igual.
"""

LARGO_NONCE = 16


# ── Quien es el cliente ──────────────────────────────────────────────────────


def ip_cliente(request: Request, a: Ajustes) -> str:
    """La IP del cliente, atravesando el proxy inverso si hay uno.

    **Se lee de derecha a izquierda, y no al reves.** `X-Forwarded-For` es una
    lista donde cada proxy *agrega* la IP del que le hablo, asi que la parte
    izquierda la escribio el cliente y puede decir cualquier cosa: confiar en
    ella es regalar el salteo del freno —basta mandar `X-Forwarded-For: 1.2.3.4`
    con un numero distinto en cada pedido—. La parte derecha la escribio la
    infraestructura, que es lo unico que el atacante no controla.

    Yendo de derecha a izquierda se saltean las privadas (`10.x`, `172.16-31.x`,
    `192.168.x`, loopback, link-local): son saltos internos de la plataforma,
    iguales para todos los pedidos. La primera publica es el cliente de verdad.

    Sin proxy adelante la cabecera **no se mira en absoluto**. Ahi la puede
    mandar cualquiera, y hacerle caso convertiria la defensa en su propio
    bypass.
    """
    if not a.detras_de_proxy:
        return request.client.host if request.client else IP_DESCONOCIDA

    crudo = request.headers.get("x-forwarded-for", "")
    cadena = [t.strip() for t in crudo.split(",") if t.strip()]
    for candidata in reversed(cadena):
        if _es_publica(candidata):
            return candidata
    if cadena:
        return cadena[-1]
    return request.client.host if request.client else IP_DESCONOCIDA


def _es_publica(texto: str) -> bool:
    """True si es una IP valida y enrutable en internet."""
    try:
        ip = ipaddress.ip_address(texto)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


# ── Freno por clave ──────────────────────────────────────────────────────────


class Freno:
    """Cuenta eventos por clave en una ventana movil y bloquea al pasarse.

    No es un token bucket ni un leaky bucket: es lo mas chico que contesta la
    pregunta que importa, que es "cuantas veces paso esto en los ultimos N
    segundos". Vive en memoria del proceso a proposito — con una sola instancia
    de la app, sumar Redis seria meterle al despliegue una pieza mas que se
    puede caer para resolver algo que un `dict` con un lock ya resuelve. El dia
    que haya dos instancias esto se vuelve un contador por instancia (mas
    permisivo, nunca mas restrictivo) y recien ahi corresponde mover el estado
    afuera.

    Thread-safe porque uvicorn corre los handlers sincronos en un threadpool:
    dos pedidos pueden estar aca adentro al mismo tiempo.
    """

    def __init__(self, *, max_claves: int = MAX_CLAVES) -> None:
        self._eventos: OrderedDict[str, deque[float]] = OrderedDict()
        self._bloqueos: dict[str, float] = {}
        self._lock = threading.Lock()
        self._max_claves = max_claves

    def claves_recordadas(self) -> int:
        """Cuantas claves tiene vivas. Hace observable el tope de memoria."""
        with self._lock:
            return len(self._eventos)

    def espera_restante(self, clave: str) -> float:
        """Segundos que faltan para que la clave se desbloquee. 0 si esta libre."""
        with self._lock:
            return self._restante(clave, time.monotonic())

    def registrar(self, clave: str, *, ventana_s: int, tope: int, bloqueo_s: int) -> float:
        """Suma un evento. Devuelve los segundos de bloqueo, o 0 si todavia hay margen."""
        ahora = time.monotonic()
        with self._lock:
            restante = self._restante(clave, ahora)
            if restante > 0:
                return restante

            marcas = self._eventos.get(clave)
            if marcas is None:
                marcas = deque()
                self._eventos[clave] = marcas
            self._eventos.move_to_end(clave)

            marcas.append(ahora)
            while marcas and ahora - marcas[0] > ventana_s:
                marcas.popleft()

            if len(marcas) >= tope:
                self._bloqueos[clave] = ahora + bloqueo_s
                marcas.clear()
                return float(bloqueo_s)

            if len(self._eventos) > self._max_claves:
                # La poda recorre todo, asi que NO va en cada pedido: se
                # dispara solo cuando el diccionario paso el tope, que con el
                # trafico normal no pasa nunca. Amortizado, el pedido tipico no
                # paga nada.
                self._podar(ahora, ventana_s)
            return 0.0

    def perdonar(self, clave: str) -> None:
        """Borra el historial de una clave. La llama el login que SI funciono."""
        with self._lock:
            self._eventos.pop(clave, None)
            self._bloqueos.pop(clave, None)

    def reiniciar(self) -> None:
        """Vacia todo. Existe para que cada test arranque sin la herencia del anterior."""
        with self._lock:
            self._eventos.clear()
            self._bloqueos.clear()

    def _restante(self, clave: str, ahora: float) -> float:
        vence = self._bloqueos.get(clave)
        if vence is None:
            return 0.0
        if vence <= ahora:
            del self._bloqueos[clave]
            return 0.0
        return vence - ahora

    def _podar(self, ahora: float, ventana_s: int) -> None:
        """Tira lo vencido y, si aun asi sobran claves, las mas viejas. Ver MAX_CLAVES."""
        for clave, marcas in list(self._eventos.items()):
            if not marcas or ahora - marcas[-1] > ventana_s:
                del self._eventos[clave]
        for clave, vence in list(self._bloqueos.items()):
            if vence <= ahora:
                del self._bloqueos[clave]
        while len(self._eventos) > self._max_claves:
            self._eventos.popitem(last=False)


FRENO_LOGIN = Freno()
"""Intentos de login fallidos.

Lo alimenta `routers/auth.py` y no el middleware: solo el router sabe si la
contraseña estaba bien, y un login correcto no tiene por que gastar cupo.

**Se cuenta por IP y no por usuario**, aunque la tentacion sea al reves. Contar
por usuario deja que cualquiera bloquee al dueño de la cuenta mandando ocho
contraseñas malas: la defensa se convierte en el ataque. Por IP, el que falla
es el unico que se queda afuera.
"""

FRENO_PEDIDOS = Freno()
"""Pedidos por IP, de toda la app."""


def reiniciar_frenos() -> None:
    """Deja los dos frenos en cero. Solo para los tests."""
    FRENO_LOGIN.reiniciar()
    FRENO_PEDIDOS.reiniciar()


# ── Cabeceras ────────────────────────────────────────────────────────────────


def politica_de_contenido(nonce: str) -> str:
    """El CSP de la app, que es casi todo prohibiciones.

    Se puede ser tan estricto porque el sitio ya estaba escrito asi: no hay una
    sola URL externa (hay un test que lo verifica), los scripts son archivos y
    no codigo adentro del HTML, y lo unico inline es el import map del cortante
    —declarativo, no ejecuta nada—, que lleva el `nonce` de este pedido.

    Las dos concesiones, las dos con motivo:

    - `style-src 'unsafe-inline'`: las plantillas usan atributos `style=` para
      ajustes puntuales de layout. La version fina seria `style-src-attr`, pero
      Firefox no la implementa, y ahi cae en `style-src 'self'`, que rompe la
      pagina entera. Una plantilla rota en un navegador es peor que un atributo
      de estilo permitido.
    - `img-src blob:`: el visor 3D y la vista previa arman imagenes en el
      cliente con `URL.createObjectURL`.
    """
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self'",
            "connect-src 'self'",
            "worker-src 'self' blob:",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "object-src 'none'",
        )
    )


def aplicar_cabeceras(respuesta: Response, *, nonce: str, a: Ajustes, es_estatico: bool) -> None:
    """Pone las cabeceras de seguridad sobre una respuesta ya armada."""
    respuesta.headers["Content-Security-Policy"] = politica_de_contenido(nonce)
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["X-Frame-Options"] = "DENY"
    respuesta.headers["Referrer-Policy"] = "same-origin"
    respuesta.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    respuesta.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    respuesta.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"

    if a.cookie_secure:
        # Solo con TLS adelante. Mandar HSTS desde `http://localhost` obligaria
        # al navegador a pedir HTTPS en un puerto donde no hay nada escuchando,
        # y la app quedaria inaccesible hasta limpiar el estado del navegador.
        respuesta.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    if not es_estatico:
        # Las paginas llevan el nombre del usuario y las descargas son sus
        # archivos: nada de eso puede quedar en un cache intermedio. Los
        # estaticos quedan afuera porque son publicos, y cachearlos es lo que
        # hace que los 2,2 MB de three.js se bajen una sola vez.
        respuesta.headers["Cache-Control"] = "no-store"


# ── Instalacion en la app ────────────────────────────────────────────────────


def instalar(aplicacion: FastAPI, a: Ajustes) -> None:
    """Registra el freno por IP, las cabeceras y el filtro de `Host`.

    El orden importa, y es el inverso al de registro: lo ultimo que se agrega
    es lo primero que corre. Queda, de afuera hacia adentro, `Host` permitido →
    freno + cabeceras → limite de tamaño → sesion. Rechazar por `Host` antes
    que nada es lo correcto: un pedido dirigido a otro dominio no merece que se
    le cuente el cupo ni que se le lea el cuerpo.
    """

    @aplicacion.middleware("http")
    async def frenar_y_endurecer(
        request: Request, siguiente: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonce = secrets.token_urlsafe(LARGO_NONCE)
        request.state.csp_nonce = nonce
        es_estatico = request.url.path.startswith("/static/")

        espera = FRENO_PEDIDOS.registrar(
            ip_cliente(request, a),
            ventana_s=a.ventana_pedidos_s,
            tope=a.max_pedidos_ip,
            bloqueo_s=a.ventana_pedidos_s,
        )
        respuesta: Response
        if espera > 0:
            respuesta = respuesta_429(
                "demasiados_pedidos",
                "Demasiados pedidos desde esta conexion. Espera un momento.",
                espera,
            )
        else:
            respuesta = await siguiente(request)

        aplicar_cabeceras(respuesta, nonce=nonce, a=a, es_estatico=es_estatico)
        return respuesta

    if a.hosts_permitidos:
        aplicacion.add_middleware(TrustedHostMiddleware, allowed_hosts=list(a.hosts_permitidos))


def respuesta_429(codigo: str, mensaje: str, espera_s: float) -> JSONResponse:
    """429 con `Retry-After`, que es lo que un cliente serio sabe leer."""
    segundos = max(1, int(espera_s))
    error = ErrorApi(codigo, mensaje, estado=429, detalle={"reintentar_en_s": segundos})
    return JSONResponse(
        error.como_json(), status_code=error.estado, headers={"Retry-After": str(segundos)}
    )
