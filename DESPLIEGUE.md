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

**Actualizar la imagen base** (parches de Python; los de Debian entran solos, ver abajo):

```bash
docker pull python:3.13-slim-bookworm
docker image inspect python:3.13-slim-bookworm --format '{{index .RepoDigests 0}}'
# copiar el digest nuevo a las dos líneas FROM del Dockerfile (Dependabot abre ese PR solo)
```

Los parches de **Debian** no esperan al digest: la etapa final hace `apt-get upgrade` en cada
build, y `ci-build.yml` le pasa `APT_REFRESH=<id de la corrida>` para que esa capa —y solo esa—
no salga del caché. Sin eso la primera corrida de trivy frenó la imagen con 3 HIGH de
`libpcre2-8-0` cuyo fix ya estaba publicado. En un build local el arg vale `manual`: para forzar
el upgrade, `docker build --build-arg APT_REFRESH=$(date +%s) .`.

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
| El release de la CI falla en el push con `GH006`/`GH013`/*protected branch* | `main` quedó protegida y falta el secret `RELEASE_TOKEN`, o su dueño no está en la lista de bypass del ruleset (§8.3) |
| El release falla con *non-fast-forward* | `main` avanzó mientras corría. No se pisó nada: *Re-run jobs* y parte de la `main` nueva |
| Render desplegó antes de que la CI terminara | El Auto-Deploy de Render sigue prendido (§8.2). Con eso apagado, el único que despliega es `ci-deploy` |
| El frontend muestra una versión vieja después del deploy | Render construyó el commit del merge y no el del release: el hook se disparó antes del push del bump, o el bump falló. Mirar la corrida de `CI · Build` → job *Versión y tag* |

---

## 8. CI → deploy: la compuerta antes de Render (GitHub Actions)

Desde este ciclo el deploy **no lo dispara el push a `main`: lo dispara la CI**, y recién cuando
calidad, tests, seguridad y la construcción de la imagen están en verde. Los workflows viven en
`.github/workflows/ci-*.yml` con la nomenclatura de `api`/`admin`/`tienda`, y cada archivo explica
su porqué en la cabecera. Esta sección es lo que **no** vive en un archivo: lo que hay que
configurar a mano en Render y en GitHub, una vez.

### 8.1 Cómo fluye un cambio

```
rama feat/…  ──PR──►  main
   en el PR:   ci-quality · ci-tests · ci-security · ci-build (construye, no despliega)
   al mergear: ci-build → compuertas → imagen + /salud + trivy
                        → ci-release  (1.0.0 → 1.1.0, commit + tag v1.1.0 en main)
                        → ci-deploy   (POST al deploy hook → Render reconstruye main)
```

La versión sube según el **tipo de la rama**: `feat/` → MENOR (y PARCHE a 0), `break/` → MAYOR, el
resto (`fix`, `hotfix`, `docs`, `chore`, `ci`, `refactor`, `perf`, `test`, `style`, `build`) → PARCHE.
MENOR y PARCHE van de 0 a 99 y acarrean (`0.2.99` + fix → `0.3.0`). La regla y su test están en
`scripts/version.py` y `tests/test_version.py`. El número queda **debajo del logo** en toda pantalla
con sesión.

La versión arranca en **`1.0.0`**, con el tag `v1.0.0` sobre el commit que trajo la CI — puesto a
mano, porque `ci-release` solo taggea lo que bumpea. El primer merge la lleva a `1.0.1` (fix,
docs, chore…) o `1.1.0` (feat) y crea el segundo tag. Un push de tag no dispara ningún workflow.

### 8.2 Render — una vez

1. El servicio → **Settings → Build & Deploy → Auto-Deploy: `Off`**. ⚠ Es el paso que importa: con
   Auto-Deploy prendido Render construye en paralelo con la CI, y el deploy sale antes de que los
   tests digan nada — la CI reportaría el error *después*.
2. Mismo panel → **Deploy Hook** → copiar la URL. Es una URL con una clave embebida: va a un secret
   de GitHub (abajo), nunca al repo ni a un log.
3. `STUDIOCUTTER_HOSTS` (§2.3) sigue siendo manual y **hoy está vacío**, o sea que se acepta
   cualquier `Host`. Cerrarlo es una variable en el panel, y es el hallazgo de phishing más barato
   de resolver del informe de seguridad.

### 8.3 GitHub — una vez

**Secrets** (Settings → Secrets and variables → Actions → *New repository secret*):

| Secret | Qué es | Cuándo hace falta |
|---|---|---|
| `RENDER_DEPLOY_HOOK_URL` | La URL del paso 8.2.2 | Desde el primer merge: sin él `ci-deploy` falla diciendo exactamente esto |
| `RELEASE_TOKEN` | Fine-grained PAT del dueño del repo: *Only select repositories* → `studiocutter3d`; *Repository permissions* → **Contents: Read and write**, nada más | Apenas se active el ruleset de `main` de abajo. Antes, `ci-release` pushea con el `GITHUB_TOKEN` y anda. ⚠ Un PAT **vence**: anotarse la fecha, porque el día que expire el release falla en el checkout y `main` se queda sin bump sin que nada más avise |

Van como *repository secrets* y no como secrets del environment `production`: los workflows
reusables los reciben con `secrets: inherit`, que hereda los del repositorio. El environment
`production` se crea solo en el primer deploy y sirve para la traza (pestaña *Environments*) y para
exigir aprobación manual si algún día se quiere.

**Ruleset de `main`** (Settings → Rules → Rulesets → *New branch ruleset*) — es lo que convierte
"la CI avisa" en "la CI frena":

- *Target branches*: `main`.
- ☑ **Require a pull request before merging** (0 aprobaciones alcanza en un repo de una persona: lo
  que se exige es el PR, no la revisión).
- ☑ **Require status checks to pass** → elegir los jobs de los tres workflows de verificación y el
  build: `Nombre de la rama`, `Versión del proyecto y de Python`, `Sintaxis, estilo y tipos`,
  `Frontera motor ↔ web`, `YAML de Actions (actionlint)`, `pytest (Python 3.13, Linux)`, `Secretos en
  el historial (gitleaks)`, `CVEs en requirements.txt (pip-audit)`, `Patrones inseguros (bandit)`,
  `Vulnerabilidades en el repositorio (trivy)`, `Construir, arrancar y escanear la imagen`. Aparecen
  en el buscador recién después de la primera corrida en un PR.
- ☑ **Block force pushes**.
- *Bypass list*: el dueño del `RELEASE_TOKEN` (rol *Repository admin*). Es lo que deja pasar el
  commit `chore(release): …` de la CI. ⚠ El bypass es de la persona, no del workflow: el dueño
  también puede pushear directo a `main` desde su máquina, y ahí la regla lo frena solo por
  disciplina. La versión sin ese agujero es una GitHub App con el token acuñado por corrida
  (`actions/create-github-app-token`) y la App en la lista de bypass — vale hacerlo el día que haya
  más de una persona con permisos.

**Ruleset de nombres de rama** (opcional; es lo único que hace que una rama mal llamada **no se
pueda crear** — `ci-quality` solo impide que se mergee):

- *New branch ruleset* → *Target branches* → **Include: all branches**; **Exclude**: `main`,
  `dependabot/**` y un patrón por tipo: `feat/**`, `fix/**`, `hotfix/**`, `break/**`, `docs/**`,
  `chore/**`, `ci/**`, `refactor/**`, `perf/**`, `test/**`, `style/**`, `build/**`.
- ☑ **Restrict creations**. Efecto: todo lo que no está excluido no se puede crear. Si se agrega un
  tipo en `scripts/version.py`, va también acá.

**Actions → General → Workflow permissions**: dejar *Read repository contents and packages
permissions*. Cada workflow declara arriba lo que necesita, y `ci-release` pide `contents: write`
solo en su job.

### 8.4 Operación

- **Releasear a mano** (un merge que la CI no pudo clasificar, o un bump que se quiere forzar):
  Actions → *CI · Release* → *Run workflow* → elegir el tipo. Después, *CI · Deploy* → *Run
  workflow* para desplegarlo.
- **Redesplegar la misma `main`** (Render reconstruye desde el repo, así que también es el rollback
  después de un `git revert` mergeado): Actions → *CI · Deploy* → *Run workflow*.
- **Dos merges seguidos**: `ci-build` los serializa. Actions encola una sola corrida pendiente por
  grupo, así que con tres merges en un minuto la del medio se cancela y la última bumpea una vez por
  los dos. Sigue valiendo "un deploy, un tag".
- **Dependabot**: sus ramas `dependabot/**` están aceptadas en `ci-quality`; el tipo del release
  sale del prefijo del commit (`chore:` → PARCHE, `ci:` → PARCHE), configurado en
  `.github/dependabot.yml`. Sus PRs pasan por las mismas compuertas, incluida la construcción y el
  escaneo de la imagen. ⚠ Para **pip** solo abre PRs de **seguridad**, no de versión: su primer
  PR subió `pydantic-core` sin tocar `pydantic`, que la clava, y el build murió en
  `ResolutionImpossible`. `requirements.txt` es un freeze completo y se actualiza regenerándolo
  entero (§6) — la señal de cuándo la da `pip-audit` cada mañana. Un PR de seguridad de Dependabot
  sobre pip puede traer el mismo problema: si el build falla en `pip install`, la respuesta es
  regenerar el freeze, no mergear el pin suelto.
- **Corridas diarias (08:00 ART)**: tests, seguridad y el escaneo de la imagen. No despliegan. Lo
  que vale de ellas es enterarse de un CVE nuevo en `requirements.txt` o en el Debian base el mismo
  día que se publica, no la próxima vez que alguien toque el repo.
