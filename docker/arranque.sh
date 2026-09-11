#!/bin/sh
# Arranque del contenedor: valida el entorno y le pasa el proceso a uvicorn.
#
# Existe en vez de un `CMD` largo por tres cosas que un CMD no puede hacer:
#
# 1. **El puerto lo elige la plataforma.** Render (y casi cualquier PaaS) inyecta
#    `$PORT` y espera que el servicio escuche ahi. Un puerto fijo en el CMD
#    significa un servicio que arranca bien y al que nadie llega.
# 2. **`--forwarded-allow-ips` se deduce, no se configura aparte.** Es el mismo
#    hecho que `STUDIOCUTTER_DETRAS_DE_PROXY`: o hay un proxy adelante o no lo
#    hay. Dos variables para un solo hecho terminan en desacuerdo, y el
#    desacuerdo silencioso es feo — la app generando URLs `http://` adentro de
#    una pagina `https://`, que el navegador bloquea sin decir por que.
# 3. **Avisar de lo que va a doler despues.** Sin `STUDIOCUTTER_SECRET`, cada
#    reinicio cierra todas las sesiones abiertas. No es un error, pero enterarse
#    en el log del arranque es mejor que enterarse porque hay que loguearse otra
#    vez todos los lunes.
#
# `exec` al final no es adorno: hace que uvicorn REEMPLACE a este shell, asi las
# señales de `docker stop` le llegan al servidor y no a un `sh` que no las mira.

set -eu

# Con argumentos, se corre eso y listo: `docker compose run web sh`, o un
# `docker run ... python -c "..."` para mirar algo adentro de la imagen. Sin
# esto, el ENTRYPOINT se los come y todo termina levantando el servidor — que
# es exactamente lo que NO se pidio.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

PUERTO="${PORT:-8000}"
DIR_TRABAJO="${STUDIOCUTTER_DIR_TRABAJO:-/datos/trabajo}"

if [ "${STUDIOCUTTER_DETRAS_DE_PROXY:-0}" = "1" ]; then
    # Se le cree al `X-Forwarded-Proto` del proxy — es lo que hace que la app
    # sepa que la conexion original era HTTPS. La IP del cliente NO sale de aca:
    # la resuelve `app/proteccion.py`, que lee la cabecera de derecha a
    # izquierda justamente para que no se pueda falsificar.
    IPS_CONFIABLES="*"
else
    IPS_CONFIABLES="127.0.0.1"
fi

if [ -z "${STUDIOCUTTER_SECRET:-}" ]; then
    echo "studiocutter: aviso — STUDIOCUTTER_SECRET esta vacia." >&2
    echo "studiocutter: el secreto se genera en ${STUDIOCUTTER_ARCHIVO_SECRETO:-/datos/sesion.key}." >&2
    echo "studiocutter: si ese archivo no sobrevive al reinicio, se cierran todas las sesiones." >&2
fi

if ! mkdir -p "$DIR_TRABAJO" 2>/dev/null || [ ! -w "$DIR_TRABAJO" ]; then
    echo "studiocutter: ERROR — no puedo escribir en $DIR_TRABAJO." >&2
    echo "studiocutter: el volumen tiene que ser del uid 10001 (ver DESPLIEGUE.md)." >&2
    exit 1
fi

echo "studiocutter: escuchando en 0.0.0.0:${PUERTO} (proxy=${STUDIOCUTTER_DETRAS_DE_PROXY:-0})"

# `--workers 1` es un requisito, no una preferencia: el almacen de trabajos, el
# registro de procesos hijo y los frenos por IP viven en memoria del proceso.
# Con dos workers habria dos verdades distintas y un trabajo lanzado en uno
# seria un 404 en el otro. Escalar esto es mover ese estado afuera primero.
#
# `--limit-concurrency`: tope de pedidos en vuelo. Es la ultima red contra una
# inundacion de conexiones, por debajo del freno por IP de la app.
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$PUERTO" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "$IPS_CONFIABLES" \
    --no-server-header \
    --limit-concurrency 100 \
    --timeout-graceful-shutdown 20 \
    --log-level info
