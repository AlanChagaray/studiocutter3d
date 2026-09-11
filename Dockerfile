# studioCutter3D — imagen de produccion.
#
# Dos etapas, y la razon es una sola: **lo que hace falta para INSTALAR no tiene
# por que viajar en lo que hace falta para CORRER**. La etapa `constructor` arma
# el venv; la final se lleva el venv ya armado y nada mas. pip no queda instalado
# como herramienta de ataque, y la imagen pesa lo que pesa la app.
#
# Decisiones de seguridad, todas visibles en este archivo:
#
# - **La base va clavada por digest**, no por etiqueta. `python:3.13-slim` de hoy
#   y el de dentro de un mes son dos imagenes distintas: con el digest, el build
#   es el mismo siempre y una base cambiada se nota (falla) en vez de entrar sin
#   que nadie la mire. Para actualizarla: `docker pull python:3.13-slim-bookworm`
#   y copiar el digest nuevo aca.
# - **El proceso NO corre como root** (`USER 10001`), y el codigo es de root con
#   permiso de lectura nada mas. Es a proposito: aunque alguien consiguiera
#   ejecucion dentro del contenedor, no puede reescribir la app para quedarse.
# - **Lo unico escribible es `/datos`**, que ademas es el unico que hace falta
#   montar. Todo lo demas puede correr con el filesystem de solo lectura
#   (`read_only: true` en el compose).
# - **Las credenciales no estan en la imagen.** `credenciales.json` esta
#   gitignoreado y tiene que seguir asi: los hashes quedarian en una capa, y
#   cualquiera con acceso al registro se los lleva. Llegan montadas en
#   `/etc/secrets/` o por `STUDIOCUTTER_CREDENCIALES_JSON` (ver DESPLIEGUE.md).
#
# Build y corrida local:
#   docker build -t studiocutter3d .
#   docker compose up --build

# ── Etapa 1: el venv ─────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS constructor

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY requirements.txt /tmp/requirements.txt

# `--only-binary=:all:` no es una optimizacion, es una alarma: esta imagen no
# trae compilador, asi que una dependencia sin wheel para linux/cp313 tiene que
# fallar ACA, con el nombre del paquete, y no doce lineas mas abajo con un error
# de gcc que no existe. Las versiones vienen todas clavadas de requirements.txt.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt

# ── Etapa 2: lo que corre ────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e AS final

# `tini` como PID 1. La app lanza un proceso hijo por trabajo y lo mata por
# timeout: sin un init de verdad arriba, un hijo que quede colgado no tiene
# quien lo recoja, y las señales de `docker stop` no bajan al arbol.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    STUDIOCUTTER_DIR_TRABAJO=/datos/trabajo \
    STUDIOCUTTER_ARCHIVO_SECRETO=/datos/sesion.key \
    STUDIOCUTTER_CREDENCIALES=/etc/secrets/credenciales.json \
    STUDIOCUTTER_MAX_TRABAJOS=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2

# Por que el default de trabajos simultaneos baja de 3 a 1: cada hijo reimporta
# numpy, scipy, trimesh y manifold3d enteros. En una maquina con 16 GB eso es
# gratis; en una instancia de 512 MB, tres hijos a la vez es el OOM killer. El
# que tenga mas RAM lo sube por entorno, que es justo para lo que esta.
#
# Y los `*_NUM_THREADS=1`: BLAS abre un hilo por core POR PROCESO, y esta app
# hace geometria, no algebra de matrices grandes. Con varios hijos vivos, esos
# hilos son memoria y cambios de contexto que no compran nada.

# El `--groups 1000` es especifico de Render y no es opcional: los *secret
# files* se montan en `/etc/secrets/` con el grupo 1000, y sin pertenecer a el
# el proceso no puede leer `credenciales.json`. El sintoma es engañoso —la app
# arranca perfecta y el login dice "no hay usuarios dados de alta"—, asi que el
# grupo se crea siempre, aunque afuera de Render no haga nada.
RUN if ! getent group 1000 >/dev/null; then groupadd --gid 1000 secretos; fi \
    && useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin cortante \
    && usermod --append --groups 1000 cortante \
    && mkdir -p /datos \
    && chown cortante:cortante /datos

# El venv entero, ya resuelto, y NADA de lo que hizo falta para armarlo: ni pip,
# ni las cabeceras de compilacion, ni el cache de descargas.
COPY --from=constructor --chown=root:root /opt/venv /opt/venv

WORKDIR /app

# El codigo queda de root y el proceso corre como `cortante`: solo lectura sobre
# lo suyo. Un contenedor que no puede reescribir su propio codigo es un
# contenedor donde una ejecucion remota no persiste.
COPY --chown=root:root cutter3d/ ./cutter3d/
COPY --chown=root:root app/ ./app/
COPY --chown=root:root docker/arranque.sh /usr/local/bin/arranque.sh

# El `sed` saca los retornos de carro. No es paranoia: el repo se edita en
# Windows, y un `.sh` que llega con CRLF hace que el kernel busque un
# interprete llamado "/bin/sh\r". El error es
# `exec .../arranque.sh: no such file or directory` sobre un archivo que esta
# ahi y tiene permiso de ejecucion — media hora de mirar el lugar equivocado.
RUN sed -i 's/\r$//' /usr/local/bin/arranque.sh \
    && chmod 0555 /usr/local/bin/arranque.sh

# Por nombre y no `10001:10001`: con el par numerico, Docker aplica ese uid y
# ese gid exactos y NO resuelve los grupos suplementarios — o sea, se perderia
# el grupo 1000 de los secret files, que es justo lo de arriba. Por nombre, los
# toma de `/etc/group`. (Sigue siendo el uid 10001, que es lo que importa: no
# es root.)
USER cortante
EXPOSE 8000

# El latido usa el mismo `/salud` que mira Render: un solo endpoint de estado,
# sin sesion y sin datos adentro.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request as u; u.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/salud', timeout=4).read()"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/arranque.sh"]
