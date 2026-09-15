# Despliegue — studioCutter3D en contenedor

Guía de cómo esta app pasa de `127.0.0.1:8000` a estar publicada, y de qué
cambia cuando eso pasa. Las credenciales siguen siendo **las mismas que se usan
hoy** (`credenciales.json`, con su hash argon2id): lo único que cambia es **cómo
llega ese archivo al contenedor**, porque está gitignoreado y no puede viajar
adentro de la imagen.

| Archivo | Para qué |
|---|---|
| `Dockerfile` | La imagen. Dos etapas, proceso no-root, base clavada por digest |
| `requirements.txt` | Las 39 dependencias de runtime con versión exacta |
| `.dockerignore` | Lista blanca: al contexto de build entra lo justo y nada más |
| `docker/arranque.sh` | Arranque: puerto de la plataforma, proxy, validaciones |
| `docker-compose.yml` | Correrla local o en un VPS propio, con el endurecimiento puesto |
| `render.yaml` | El servicio de Render descrito como código |

---

## 1. Probarla local antes de publicar

```bash
# El secreto de firma de la sesión. Sin esto igual arranca (se genera solo),
# pero fijarlo hace que las sesiones sobrevivan a que se borre el volumen.
export STUDIOCUTTER_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

docker compose up --build
```

→ http://127.0.0.1:8000, con el mismo usuario y la misma contraseña de siempre.

⚠ **`credenciales.json` tiene que existir antes del primer `up`.** Docker, ante
un bind mount de un archivo que no está, crea un **directorio** con ese nombre;
la app arranca bien y el login contesta *"no hay usuarios dados de alta"*. Es el
error más confuso de todo el despliegue.

Para mirar adentro del contenedor sin levantar el servidor:

```bash
docker compose run --rm web sh          # una shell
docker compose run --rm web python -c "import cutter3d; print('ok')"
```

---

## 2. Publicarla en Render

### 2.1 Crear el servicio

1. Subir la rama con estos archivos.
2. Render → **New → Blueprint** → elegir el repo. Lee `render.yaml` y arma el
   servicio: runtime Docker, health check en `/salud`, una sola instancia.
3. Render pregunta por las variables marcadas `sync: false`
   (`STUDIOCUTTER_HOSTS` y `STUDIOCUTTER_CREDENCIALES_JSON`). Se pueden dejar
   vacías y completarlas después.

### 2.2 Cargar las credenciales — dos caminos, elegir uno

**a) Secret file (recomendado).** Es el archivo tal cual, sin tocarlo:

- Service → **Environment → Secret Files → Add Secret File**
- Filename: `credenciales.json`
- Contents: pegar el contenido del `credenciales.json` local

Render lo monta en `/etc/secrets/credenciales.json`, que es exactamente donde la
imagen lo busca por default. No hay nada más que configurar.

> El `Dockerfile` agrega el usuario de la app al **grupo 1000** por esto: los
> secret files de Render se montan con ese grupo, y sin pertenecer a él el
> proceso no puede leerlos. El síntoma sería, otra vez, *"no hay usuarios dados
> de alta"* con la app funcionando perfecta.

**b) Variable de entorno.** Para plataformas donde montar un archivo es
incómodo: pegar el JSON entero, en una línea, en
`STUDIOCUTTER_CREDENCIALES_JSON`. Si están las dos fuentes, **gana la variable**.

### 2.3 Cerrar el `Host`

Una vez que Render asigne el dominio (`<nombre>.onrender.com`, o el propio):

- `STUDIOCUTTER_HOSTS` = `studiocutter3d.onrender.com`

Desde ahí, un pedido con otro `Host` se rechaza con 400 antes de llegar a
ningún handler.

### 2.4 Agregar o cambiar un usuario

El hash se genera **en la máquina**, nunca en el servidor, y la contraseña en
claro no se escribe en ningún lado:

```bash
.venv/Scripts/python -c "from app.seguridad import hashear; import getpass; print(hashear(getpass.getpass()))"
```

> Sale de `app.seguridad` y no de `PasswordHasher()` a secas **a proposito**.
> Esta app no usa los parametros por default de argon2-cffi (64 MiB, `p=4`) sino
> el perfil de baja memoria de la RFC 9106 (**19 MiB, `t=2`, `p=1`**), porque
> 64 MiB por verify en una instancia de 512 MB es caro y con 0,1 CPU el `p=4`
> solo agrega contencion. Llamando a `hashear` los parametros no se escriben en
> dos lados y no se pueden desincronizar.
>
> Los hashes viejos **siguen andando**: argon2 guarda sus parametros adentro del
> propio hash. Conviene regenerarlos igual — mientras quede uno con 64 MiB, el
> login sigue pagando 64 MiB cada vez que se verifica contra el.

Se pega en el `credenciales.json` local, y ese archivo se vuelve a cargar como
secret file. El deploy se reinicia solo.

---

## 3. Variables de entorno

Ninguna es obligatoria: sin ninguna, la app corre como siempre en localhost.
⛔ **Nunca en un `.env`** — variables del sistema o del panel de la plataforma.

| Variable | Default | Cuándo tocarla |
|---|---|---|
| `STUDIOCUTTER_SECRET` | se genera y se persiste | Siempre en producción: sin ella, un reinicio cierra todas las sesiones |
| `STUDIOCUTTER_COOKIE_SECURE` | `0` | `1` cuando hay HTTPS adelante. Prende también el `Strict-Transport-Security` |
| `STUDIOCUTTER_DETRAS_DE_PROXY` | `0` | `1` detrás de Render/nginx/Traefik. Es lo que hace que la app sepa quién pide y que la conexión original era HTTPS |
| `STUDIOCUTTER_HOSTS` | vacío (cualquiera) | El dominio propio, separado por comas si hay varios |
| `STUDIOCUTTER_MAX_TRABAJOS` | `3` (la imagen lo baja a `1`) | Según la RAM: cada trabajo en curso son ~300 MB |
| `STUDIOCUTTER_CREDENCIALES` | `/etc/secrets/credenciales.json` en la imagen | Otra ruta de montaje |
| `STUDIOCUTTER_CREDENCIALES_JSON` | vacío | El JSON de credenciales en la variable misma |
| `STUDIOCUTTER_DIR_TRABAJO` | `/datos/trabajo` en la imagen | Mover el directorio de trabajos |
| `STUDIOCUTTER_ARCHIVO_SECRETO` | `/datos/sesion.key` en la imagen | Mover el secreto generado (tiene que caer en algo escribible) |
| `PORT` | `8000` | La inyecta la plataforma; en Render es `10000` |

---

## 4. Qué defensas se agregaron, y contra qué

Publicar la app cambia el modelo de amenaza: hasta ahora el único que podía
pedirle algo era quien estaba sentado adelante.

**En la app** (`app/proteccion.py`, y hay tests de cada una en
`tests/test_web_proteccion.py`):

| Defensa | Contra qué |
|---|---|
| Freno de login: 8 fallos por IP → 15 min de bloqueo | Fuerza bruta. argon2id ya hace cara cada prueba; el freno la hace finita |
| Freno general: 240 pedidos por IP por minuto | Inundación de pedidos y barridos automáticos |
| IP real leída de derecha a izquierda en `X-Forwarded-For` | Que el atacante se saltee los dos frenos inventando la cabecera |
| CSP con `nonce` por pedido, `frame-ancestors 'none'`, `base-uri 'none'` | XSS, clickjacking, inyección de `<base>` |
| `nosniff`, `Referrer-Policy`, COOP/CORP, `Permissions-Policy` | Confusión de tipo MIME, fuga del referer, aislamiento entre orígenes |
| `Cache-Control: no-store` fuera de `/static` | Que un proxy intermedio guarde una página con el usuario o un archivo ajeno |
| HSTS, solo cuando hay TLS adelante | Downgrade a HTTP |
| `Host` permitido | Host header injection: las URLs absolutas que arma `url_for` salen del `Host` |
| Tope de claves en los frenos (8192) | Que el propio contador sea el agotamiento de memoria |

Y lo que **ya estaba** y sigue haciendo el trabajo pesado: argon2id con hash
señuelo (no se puede enumerar usuarios), sesión firmada que se regenera al
entrar, destino post-login validado, límite de 25 MB por subida cortado por
`Content-Length`, tope de trabajos simultáneos, timeout real por trabajo,
detección de formato por contenido, ids UUID contra path traversal, y errores
que nunca devuelven rutas del servidor.

**En el contenedor:**

| Medida | Qué acota |
|---|---|
| Proceso no-root (uid 10001) y **código de root, solo lectura** | Una ejecución remota no puede reescribir la app para quedarse |
| `read_only: true` + `/datos` como único volumen escribible | Nada se escribe fuera del único lugar previsto |
| `/tmp` en tmpfs con `noexec,nosuid` | Los temporales de subida no tocan el disco del host ni se pueden ejecutar |
| `cap_drop: ALL`, `no-new-privileges` | El proceso no tiene una sola capability de root ni puede escalar |
| `mem_limit`, `memswap_limit`, `pids_limit` | Un trabajo pesado o una fork bomb no se llevan puesto al host |
| Base clavada por digest, sin compilador, sin pip en la imagen final | Superficie mínima y builds reproducibles |
| `tini` como PID 1 | Los hijos que mueren se recogen; `docker stop` baja limpio |
| Sin cabecera `Server` | Un dato menos para el que hace reconocimiento |

**Lo que esto NO resuelve, y conviene tener presente:** el freno vive en memoria
del proceso, así que con dos instancias sería un contador por instancia; no hay
WAF ni protección de capa 3/4 más allá de la que ponga la plataforma; y un
atacante distribuido con muchas IPs sigue pudiendo hacer ruido —lo que no puede
es adivinar la contraseña, que es lo que importa acá.

---

## 5. Números medidos (en esta imagen, no estimados)

| Medición | Valor |
|---|---|
| Tamaño de la imagen | 725 MB |
| RAM del servidor en reposo | ~105 MB |
| Pico del contenedor con un cortante en curso | ~225 MB |
| Pico del proceso hijo (fixture `murcielago`, el más pesado) | ~290 MB |
| Cortante de punta a punta (`murcielago`, 70 mm) | 7,5 s |
| Arranque en frío del motor en el hijo | 1,2 s |

De ahí sale la recomendación de `plan: starter` (512 MB) con
`STUDIOCUTTER_MAX_TRABAJOS=1`, y de pasar a `standard` (2 GB) para permitir dos
o tres trabajos simultáneos.

### 5.1 Lo que cambió en el ciclo 6 (bajar el consumo)

El disparador fue un `Ran out of memory (used over 512MB)` de Render bajo
demanda. Lo medido, antes y después (Working Set en Windows; los números de
arriba son del contenedor y siguen valiendo como orden de magnitud):

| Medición | Antes | Después |
|---|---|---|
| Proceso web residente | **128,7 MB** · 1577 módulos | **95,8 MB** · 962 módulos |
| `import app.tareas` (lo que paga el hijo al arrancar) | **106,7 MB** · 1284 módulos | **22,5 MB** · 134 módulos |
| F2 sobre una foto de teléfono de 12 MP | **830 MB** | **263 MB** |
| Cortante `murcielago` (F3) | 281 MB | 281 MB (sin cambio) |
| argon2 por hash / verify | 64 MiB, `p=4` | 19 MiB, `p=1` |
| Quedarse sin memoria | **mata el contenedor** (exit 137, sin log) | falla el trabajo con `sin_memoria` |

Las cuatro causas y sus arreglos:

1. **El proceso web cargaba el motor entero y no lo usaba.** `app/errores.py`
   importa `cutter3d.errors` —un módulo que solo importa `__future__`— y eso
   ejecutaba el `__init__` del paquete, que traía trimesh, manifold3d, shapely,
   scipy y skimage. Ahora `cutter3d/__init__.py` importa el motor **adentro de
   `generar()`** y expone lo demás con un `__getattr__` de módulo. Con `spawn`
   (que no tiene copy-on-write) el padre y el hijo suman, así que esto se cobra
   dos veces.
2. **Nada acotaba el tamaño con el que se trabaja una imagen.** El único límite
   era `MAX_PIXELES` (89,4 MP), que es un guard anti-bomba de descompresión, no
   un presupuesto de memoria. `medial_axis` cuesta **~63 MB por megapixel**, así
   que una foto de teléfono normal pedía 830 MB. Ahora hay un segundo límite,
   `MAX_PIXELES_TRABAJO` (3 MP), y lo que se reduce **se declara** en el reporte.
3. **argon2 con los defaults** = 64 MiB por hash, uno al importar y otro por
   cada login. Ver §2.4.
4. **El hijo no tenía techo de RAM.** Ahora sí
   (`app/tareas.py:LIMITE_RAM_HIJO_MB`, 380 MB, vía **`RLIMIT_DATA`**): la
   asignación desbocada levanta `MemoryError` **adentro del hijo**, el trabajo
   queda en error con un mensaje que se entiende, y el servidor no se entera.
   Verificado en un contenedor Linux de 512 MB, contra el control sin techo que
   muere con exit 137 y sin una línea de log.

   ⚠ **`RLIMIT_DATA` y no `RLIMIT_AS`, y la diferencia es enorme.** Medido en el
   contenedor con tres cortantes seguidos: `VmPeak` (espacio de direcciones)
   **611 MB**, `VmData` (heap anónimo) **277 MB**, `VmHWM` (RSS) **291 MB**. Un
   cortante normal *reserva* el doble de direcciones de las que *usa*, así que un
   techo de `RLIMIT_AS` dimensionado contra el RSS hace fallar hasta la estrella.
   `VmData` queda a un 5% del RSS, que es lo único que Render mide.

### 5.2 End-to-end contra el contenedor

Los tests corren in-process con `TestClient` y `dependency_overrides`: **nunca
levantan un hijo de verdad ni escriben en `trabajo/`**. Lo que cierra el ciclo es
correr el flujo entero por HTTP contra la imagen real, con `-m 512m`:

```bash
docker build -t studiocutter3d:local .
docker run -d --name sc3d -m 512m --memory-swap 512m \
  -e STUDIOCUTTER_CREDENCIALES_JSON="$(cat credenciales.json)" \
  -e STUDIOCUTTER_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  -v sc3d-datos:/datos -p 8945:8000 studiocutter3d:local
# login -> F1 -> F2 -> F3 -> polling -> descargas -> ZIP -> imagen enorme
docker stats --no-stream sc3d
```

Resultado del ciclo 6: **31 chequeos verdes, pico de 267 MiB de 512 (52%)**,
reposo en **64 MiB**. Los dos cortantes (murciélago con puenteo y estrella sin
puenteo) cierran watertight con el euler esperado, y la imagen de 63 MP falla el
trabajo sin tocar al servidor.

---

## 6. Mantenimiento

**Cambiar una dependencia.** `pyproject.toml` sigue siendo la fuente de verdad
para desarrollo; `requirements.txt` es su espejo clavado para la imagen. Después
de tocar una dependencia, regenerar el cierre transitivo desde el venv:

```bash
.venv/Scripts/python -m pip install -e ".[dev,web]"
.venv/Scripts/python -m pip freeze          # y actualizar las versiones del requirements
```

**Actualizar la imagen base** (parches de seguridad de Debian y de Python):

```bash
docker pull python:3.13-slim-bookworm
docker image inspect python:3.13-slim-bookworm --format '{{index .RepoDigests 0}}'
# copiar el digest nuevo a las dos líneas FROM del Dockerfile
```

**Verificar que la imagen sigue sana** después de cualquier cambio:

```bash
docker build -t studiocutter3d:local .
docker run --rm studiocutter3d:local python -c "import cutter3d, app.main; print('ok')"
docker compose up -d && curl -i http://127.0.0.1:8000/salud
```

---

## 7. Errores confusos y qué significan

| Síntoma | Causa |
|---|---|
| Login dice *"no hay usuarios dados de alta"* con la app andando | El secret file no se montó, o el proceso no está en el grupo 1000, o Docker creó un **directorio** `credenciales.json` |
| `exec /usr/local/bin/arranque.sh: no such file or directory` | El `.sh` llegó con CRLF. El `Dockerfile` lo normaliza, pero si se cambió el arranque, revisar `.gitattributes` |
| El CSS y el JS no cargan en HTTPS | Falta `STUDIOCUTTER_DETRAS_DE_PROXY=1`: la app arma URLs `http://` adentro de una página `https://` y el navegador las bloquea |
| Hay que loguearse de nuevo después de cada deploy | Falta `STUDIOCUTTER_SECRET` |
| El contenedor muere sin log durante un cortante | OOM del kernel (exit 137). Desde el ciclo 6 el techo de RAM del hijo lo convierte en un error del trabajo: si vuelve a pasar, el que se pasó es el **proceso web**, no el hijo — bajar `STUDIOCUTTER_MAX_TRABAJOS` o subir el plan |
| Un trabajo falla con *"necesitó más memoria de la que el servidor tiene"* | El techo de `LIMITE_RAM_HIJO_MB` funcionando. Es lo esperado con un dibujo muy pesado; el servidor sigue en pie |
| El JPG convertido salió más chico de lo que subí | El presupuesto de píxeles (`MAX_PIXELES_TRABAJO`, 3 MP). El reporte del trabajo trae `tamano_salida` |
| 429 sin haber hecho nada raro | Un script propio pollea sin pausa. El front real pollea cada 0,8 s y nunca lo toca |
| `ERROR — no puedo escribir en /datos/trabajo` | El volumen se montó como bind mount de una carpeta del host, que llega como root. Usar un volumen con nombre |
