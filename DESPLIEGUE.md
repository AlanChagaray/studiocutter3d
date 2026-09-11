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
python -c "from argon2 import PasswordHasher; import getpass; print(PasswordHasher().hash(getpass.getpass()))"
```

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
| El contenedor muere sin log durante un cortante | OOM. Bajar `STUDIOCUTTER_MAX_TRABAJOS` o subir el plan |
| 429 sin haber hecho nada raro | Un script propio pollea sin pausa. El front real pollea cada 0,8 s y nunca lo toca |
| `ERROR — no puedo escribir en /datos/trabajo` | El volumen se montó como bind mount de una carpeta del host, que llega como root. Usar un volumen con nombre |
