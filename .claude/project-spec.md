# Spec Base — studioCutter3D

> Generado por la skill `inspect` del workflow ViaBariloche. Es la referencia de contexto
> del proyecto entre sesiones: mientras esté vigente, `inspect` NO re-analiza el stack desde
> cero. Se regenera con aprobación cuando cambian las señales de frescura de abajo.

**Última actualización:** 2026-09-17
**Versión de plantilla:** 3

> Actualizado al cerrar el ciclo del **puenteo por regla** (fix del cortador): `_puentear_colisiones`
> dejó de sembrar por síntomas y pasó a **una sola regla** — cierre morfológico de radio
> `o2 + distancia/2`, y se puentea lo que **entra en la banda del filo**. Eso cierra los dos defectos
> que el detector viejo no veía: **cuerpos sueltos** del cortador y **filo más fino o más grueso** que
> `filo_ancho_mm` adentro de la colisión. Suma `cuerpos_sueltos()` como guarda dura
> (`CuerpoSueltoEnCortador`), el fixture `tests/fixtures/sr-cara-papa.svg` y 4 tests. **550 tests**
> (eran 303) y los **8 gates** en verde. El **marcador no se tocó** — restricción dura del ciclo.
>
> ⚠ **Este archivo está atrasado tres ciclos en todo lo que NO es el cortador.** El cuerpo todavía
> describe el proyecto al cierre del ciclo 5: no menciona `cutter3d/lamina.py`, `cutter3d/malla.py`,
> `cutter3d/paquete3mf.py`, la funcionalidad **F4 post** (foto del cortante desde el archivo, en lote
> y con set), la **conversión 3MF ↔ STL** del conversor, ni los **imports perezosos** del ciclo 6
> (`cutter3d/__init__.py` importa el motor adentro de `generar()`; subirlos al tope revierte ese
> ciclo). El `CLAUDE.md` del repo sí tiene todo eso y hoy es la referencia más fresca. Para ponerlo
> al día hace falta una corrida **completa** de `inspect`, no un delta: se declara en lugar de
> disimularlo.
>
> Del **ciclo 5** vienen el ZIP de descarga completa, la foto del cortante como archivo del
> trabajo y el rediseño del render de la foto. La capa web sumó **2 endpoints** y el contrato de archivos
> una clave (`jpg_vista`); la vista imagen comparte con el visor las funciones, la exposición y la
> sombra, pero lleva **su propio reparto de paneles, neutro y sin cenital fuerte**. **303 tests**
> (eran 228) y los **8 gates** en verde.
> ⚠ El motor (`cutter3d/`), `pyproject.toml` y `requirements.txt` **no se tocaron**: fue restricción
> dura del ciclo y está verificado con `git diff --name-only`.
>
> Del ciclo 4 viene el conversor de **13 formatos**, la **barra superior única** y que el nombre del
> archivo del usuario sobreviva todo el pipeline; del ciclo 3, `distancia_colision_mm` y el puenteo de
> bolsillos ciegos; del ciclo 2, la capa web.

## Señales de frescura
- **Manifests:** `pyproject.toml` (existe en la raíz) — `version = "0.2.0"`
- **Versión lenguaje/framework:** Python 3.13.7 (`requires-python = ">=3.13"`) · FastAPI 0.141.1
- **Dependencias clave (con versión instalada):**
  - *Motor:* numpy 2.5.3 · scipy 1.18.1 · shapely 2.1.2 · trimesh 5.1.0 · manifold3d 3.5.3 ·
    mapbox-earcut 2.1.0 · scikit-image 0.26.0 · pillow 12.3.0 · svgelements 1.9.6 ·
    vtracer 0.6.15 · resvg-py 0.5.0 · lxml 6.1.3 · **pillow-heif 1.7.0** · **rawpy 0.27.1**
  - *Web (extra `web`):* fastapi 0.141.1 · starlette 1.6.0 · uvicorn 0.52.4 · jinja2 3.1.6 ·
    python-multipart 0.0.32 · itsdangerous 2.2.0 · argon2-cffi 25.1.0
- **Front vendorizado (no hay gestor de paquetes JS):** three.js **0.186.0** en
  `app/static/vendor/three/` (5 archivos con la disposición de npm, sin minificar, 2,2 MB) · Space Grotesk + IBM Plex Mono en
  `app/static/vendor/fuentes/` (4 archivos, 72 KB, subset latino)
- **Lockfiles presentes:** ninguno (pip + venv, sin lock)
- **Configs de gates presentes:** ninguna suelta — todas dentro de `pyproject.toml`
  (`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`, **dos bloques `[[tool.mypy.overrides]]`**,
  `[tool.pytest.ini_options]`, `[tool.bandit]`). `gitleaks` corre con su configuración por defecto.

## Stack
- **Lenguaje:** Python 3.13.7 (invocado como `py -3.13`; ⚠ `python` no está en PATH en esta máquina)
- **Framework(s):** FastAPI + Jinja2 en el servidor · CSS propio + JS vanilla + three.js en el cliente.
  **Sin Node, sin bundler, sin build step.**
- **Versión runtime/engine:** CPython 3.13.7, venv en `.venv/`, pip 25.2

## Arquitectura
- **Patrón / capas:** librería de dominio pura (`cutter3d/`) + capa web que la orquesta (`app/`).
  La frontera es real y está verificada: `grep -rE "fastapi|uvicorn|jinja2|starlette" cutter3d/` no
  devuelve nada, y `app/` no reimplementa una sola línea de geometría.
- **Entrypoints:**
  - Web: `.venv/Scripts/python -m uvicorn app.main:app` → `app/main.py:crear_app()`
  - CLI: `python -m cutter3d` (`cutter3d/cli.py`), 4 subcomandos
  - API de librería: `cutter3d.generar()`
- **Las tres funcionalidades, y cómo se ejecuta cada una:**
  - **F1 Convertidor** (**13 formatos** → **jpg o svg**, destino elegido por archivo):
    png · jpeg/jfif · webp · svg · gif · bmp · ico · tga · psd · avif · heif (HEIC/HEIF) ·
    **tiff** (deliberadamente ambiguo: cubre TIF/TIFF comunes **y** los RAW ARW/CR2/NEF/DNG, que son
    TIFF por dentro) · cr3. Quedan **fuera con mensaje que los nombra**: EPS (necesita Ghostscript
    instalado en el sistema, y no lo está), DICOM, EXR y XCF.
    **síncrono**, dentro del pedido, un archivo por request. Medido: 14-45 ms a JPG y 14-190 ms a
    SVG (400 a 2000 px) — órdenes de magnitud menos que el arranque en frío del proceso hijo, que
    es de 1,09 s.
  - **F2 corrección de líneas** y **F3 cortante**: **un `multiprocessing.Process` por trabajo**, con
    polling desde el navegador. No es un `ProcessPoolExecutor` y el motivo es concreto: **el pool no
    sabe imponer un timeout**. `future.result(timeout=N)` corta la espera, no al worker. Con un
    `Process` dedicado, `join(timeout)` + `terminate()` da un timeout real con stdlib pura.
- **Los archivos de un trabajo se mueven por `app/routers/trabajos.py`, en las dos direcciones.**
  Desde el ciclo 5 son cinco endpoints, y el tercero es el único camino de escritura del cliente:
  - `GET /{id}` (polling) · `GET /{id}/archivo/{clave}` (descarga suelta) · `DELETE /{id}` (cancelar).
  - **`GET /{id}/zip`** — arma un ZIP al vuelo con `claves_descargables` (todo menos `CLAVES_INTERNAS`,
    o sea sin el `.glb`), con los miembros nombrados por `nombre_de_descarga` — el mismo criterio que
    la descarga suelta. Se sirve con `StreamingResponse` y **no** con `FileResponse`: ver el hallazgo
    10. Los `.3mf` y el JPG entran con `ZIP_STORED` porque ya están comprimidos.
  - **`PUT /{id}/imagen`** — recibe la foto cenital que rindió el navegador. Cuatro defensas en orden:
    propietario → tipo `CORTANTE` (404, no 403) → estado `LISTO` (409) → `guardar_subida` decidiendo
    por los BYTES, con `destino_nombre=NOMBRE_DE[JPG_VISTA]`. El cliente sigue sin nombrar nada.
  - La foto **es un archivo del trabajo como cualquier otro** (`jpg_vista` → `vista.jpg`): se baja por
    el mismo endpoint, con el mismo criterio de nombre, y entra en el ZIP. La sube el front
    **on-demand al hacer clic**, no con debounce: lo que baja es lo que se está viendo.
- **Frontera entre procesos:** `app/tareas.py`. Recibe solo primitivos y `str` de rutas, no devuelve
  nada, y **nunca deja escapar una excepción**: el resultado viaja por `estado.json`, escrito de forma
  atómica en el directorio del trabajo. Su ausencia **significa fallo, no "todavía no"**.
- **Estado de los trabajos:** `AlmacenTrabajos` (`Protocol`) + `AlmacenEnMemoria` (dict + lock).
  `Trabajo` lleva `propietario` desde el día uno y **`obtener()` lo exige**: la autorización vive en el
  almacén, así que un router nuevo no puede olvidarse de chequearla. Es la costura por donde se corta
  el día que esto sea multiusuario — entra un `AlmacenSQLite` con la misma firma y no cambia nada más.
- **Archivos:** un directorio por UUID bajo `trabajo/`, limpieza por TTL (6 h) más barrido de
  huérfanos que quedan de un reinicio. **Sin base de datos.**
- **Auth y autorización:** usuario + contraseña con argon2id (`credenciales.json`, gitignoreado) y
  sesión por cookie firmada (`SessionMiddleware`). El secreto sale de `STUDIOCUTTER_SECRET`, si no de
  `sesion.key`, y si no se genera y se persiste. ⛔ Nunca de un `.env`. En la sesión viaja **solo** el
  usuario; el tema claro/oscuro va en `localStorage` y el encadenado entre pantallas en la URL.
- **Manejo de errores y logging:** jerarquía propia en `cutter3d/errors.py`, y `app/errores.py` la
  traduce a HTTP. **Los mensajes se arman desde los atributos de la excepción, nunca con `str(exc)`**,
  que empieza con la ruta del filesystem del servidor. Lo que no tenga atributo seguro sale como
  `interno` y el detalle va al log.
- **Integraciones externas:** ninguna. **Cero URLs externas** en templates, CSS y JS: la app entera
  funciona sin internet, y hay un test que lo verifica.

### Hallazgos de librerías que costaron encontrar — no volver a descubrirlos
1. **`skeletonize` de scikit-image 0.26 segfaultea** con la máscara rasterizada del fixture del
   círculo (1812×1812, área maciza), incluso sin llamada previa a la transformada de distancia.
   **No es el tamaño**: un disco sintético de idéntico tamaño, dtype, flags y área pasa sin problema.
   Se usa **`medial_axis`**, que además devuelve eje y distancia en una sola pasada.
2. **`Path3D.to_planar()` de trimesh exige `rtree`**, que no está instalado. Las áreas de sección se
   calculan armando los anillos con shapely y combinándolos con XOR — sin sumar la dependencia.
3. **`pypotrace` no tiene wheel de Windows.** La vectorización va con **vtracer**, y
   `hierarchical="cutout"` es **obligatorio**: su default `"stacked"` apila formas en vez de generar
   huecos, y el marcador saldría con todos los huecos internos rellenos.
4. **El `.glb` que exporta trimesh viene con Z arriba.** Se comprobó leyendo el chunk JSON del GLB: el
   nodo no trae ninguna transformación de base. glTF es Y arriba, así que `preview3d.js` rota el
   modelo −90° en X al cargarlo. Sin eso el cortante se ve acostado.
5. **`GLTFLoader` y `OrbitControls` importan de `'three'` por nombre.** Como no hay bundler, hace falta
   un `<script type="importmap">` que traduzca ese nombre al archivo vendorizado. Es preferible a
   editar los archivos de la librería, que así siguen siendo diffeables contra el original.
5-bis. **Vendorizar three no es copiar un archivo: son cinco, y las rutas importan.** Dos
   dependencias transitivas se pasaron por alto en el ciclo 2 y dejaron la vista previa muerta:
   - **`three.module.js` no es autocontenido.** Desde r167 el build viene partido, y hace
     `export ... from './three.core.js'`. Falta ese archivo y no carga nada.
   - **`GLTFLoader.js` importa `../utils/BufferGeometryUtils.js` y `../utils/SkeletonUtils.js`**
     por ruta **relativa**, así que el vendor tiene que respetar la disposición de
     `three/examples/jsm`: `three/addons/{loaders,controls,utils}/`.

   Un import que 404ea **no tira ningún error visible**: el grafo entero deja de evaluarse, la
   pantalla se queda sin visor y no hay nada ni en la consola del servidor ni en la página. Por eso
   se vendoriza el par **sin minificar**, con los nombres exactos de npm (los 5 archivos son
   diffeables contra el paquete), y por eso hay un test que **recorre el grafo**
   (`test_el_grafo_de_modulos_del_visor_cierra`) en vez de una lista de archivos escrita a mano:
   una lista solo puede nombrar lo que uno ya sabe que existe, que es justo lo que este bug no era.
   Su patrón de imports no se ancla al margen izquierdo a propósito — un build minificado viene en
   una sola línea y así igual se le ven las dependencias.
6. **`spawn` necesita un `__main__` que sea un archivo real.** Un script pasado por stdin
   (`python - <<EOF`) hace fallar a todo proceso hijo con `OSError: Invalid argument: '<stdin>'`.
   Para probar algo que lance trabajos, hay que escribir el script a un archivo.
7. **Un radio de cierre grande NO es más caro: es más barato.** Se midió sobre el murciélago (arte
   real, 21 contornos) al evaluar si `distancia_colision_mm` podía ser un vector de agotamiento de
   recursos: con radio 0,5 mm el puenteo tarda **0,021 s** y pica **3,6 KB**; con radio 500 mm tarda
   **0,010 s** y pica **3,3 KB**, y el filo baja de 494 a 150 vértices. El motivo es que el cierre
   funde la silueta en un blob con menos vértices, así que GEOS tiene *menos* trabajo, no más. La
   intuición dice lo contrario —"radio grande = offset auto-intersecante = noding caro"— y por eso
   conviene que quede escrito: **si algún día se sube `LIMITE_MAX_MM`, hay que volver a medirlo**, no
   es una propiedad garantizada para cualquier arte.
   ⚠ **Los números son los del puenteo por regla** (ciclo del fix del cortador). El detector viejo,
   que sembraba con bolsillos ciegos + el material de forzar la colisión, medía 0,046 s / 14,1 KB con
   radio 0,5 mm y 0,016 s / 4,9 KB con radio 500: la regla nueva es **más del doble de rápida y ~4x
   más barata en memoria** porque ya no calcula el filo ni une semillas. Lo que no cambió es la
   propiedad — el radio grande sigue siendo el caso barato.
8-bis. **Con tres decodificadores, el ORDEN en que se prueban decide si el resultado es correcto.**
   Ante un contenedor TIFF o ISO-BMFF va **LibRaw primero**, nunca Pillow. El motivo no es de
   performance: Pillow **abre** un `.NEF` sin fallar, pero devuelve el **preview JPEG embebido** en
   vez de la foto del sensor. O sea, "probar Pillow y si falla usar LibRaw" produce una salida
   silenciosamente incorrecta, sin ningún error a la vista. LibRaw, al revés, rechaza lo que no es
   RAW con `LibRawFileUnsupportedError` limpio, y su `imread()` solo parsea metadata (barato): es el
   único discriminador confiable de los dos. Vive en `cutter3d/raster.py:_abrir_como_pil`.

8-ter. **vtracer no falla ante un formato que no lee: PANIQUEA en Rust, y `except Exception` no lo ve.**
   El crate `image` no soporta HEIC, AVIF ni RAW. Ante uno de esos, `pyo3` levanta un
   `pyo3_runtime.PanicException`, **que hereda de `BaseException` y no de `Exception`** — así que
   el `except Exception` que envolvía la llamada lo dejaba pasar hasta el borde web y salía un 500.
   Por eso `vector.a_svg` ahora normaliza la entrada a PNG con `raster.preparar_para_vectorizar`
   antes de llamar a vtracer, y captura `BaseException` re-lanzando `KeyboardInterrupt`/`SystemExit`.
   Efecto colateral valioso: **vtracer era el único camino de decodificación sin tope de píxeles**, y
   normalizar lo puso detrás del mismo guard que los otros dos.

9. **XML no admite `--` adentro de un comentario.** Escribir un guion doble en el comentario de un
   fixture `.svg` lo vuelve inválido y el parseo se cae con un error que no menciona el comentario.
   Pasó al documentar `dos_lobulos.svg`: dos tests en rojo hasta encontrarlo.
10. **`FileResponse` retorna ANTES de correr su `background` si el `Range` es inválido.** Starlette
   soporta `Range` y ante un header malformado (`Range: bytes=abc`) o insatisfacible devuelve
   400/416 con un `return` temprano que **saltea** la línea del `BackgroundTask`. Un ZIP que se arma
   en un temporal y se borra ahí queda en disco, y como cada pedido escribe una copia completa de
   todas las salidas, **el cliente amplifica disco con un solo header**. Por eso `descargar_todo`
   usa `StreamingResponse`. Y hay un segundo nivel: con `StreamingResponse`, una desconexión
   **cancela** la tarea del stream y el generador queda suspendido en su `yield` sin que nadie le
   llame `close()`, así que su `finally` corre **por refcount del recolector, no por flujo de
   control**. Lo que realmente sostiene el borrado es el `unlink` con el descriptor ya abierto: en
   POSIX el archivo se sigue sirviendo y el espacio se recupera solo.
11. **`almacen.actualizar()` refresca `actualizado_en` SIEMPRE**, y de ese campo depende `vencidos()`
   — o sea el TTL de 6 h, que es la **única cota de disco total** del diseño. Hasta el ciclo 5
   ninguna acción del cliente escribía sobre un trabajo ya terminado, así que no se notaba; el
   `PUT /imagen` fue el primero, y sin restaurar el valor previo un PUT de tres bytes cada cinco
   horas dejaba un directorio con todas las salidas vivo para siempre. El arreglo de fondo es un
   `tocar=False` en el almacén; hoy está mitigado en el router. ⚠ La mitigación funciona **porque
   `AlmacenEnMemoria` devuelve la instancia viva**: el `Protocol` no lo promete, así que con un
   `AlmacenSQLite` se pierde — y el test que la cubre seguiría en verde.
12. **`uvicorn` está fijado SIN `[standard]`, y eso es una defensa, no una omisión.** Sin ese extra el
   writer HTTP es `h11`, que valida los valores de header de respuesta contra el ABNF y **rechaza
   CR/LF**. Instalar `uvicorn[standard]` cambia a `httptools`, que **no valida**. Importa desde el
   ciclo 5: el `content-disposition` del ZIP se arma a mano, y su única defensa propia es que
   `base_es_segura` use `fullmatch` (con `match`, comillas, `;`, CR y LF pasan todos).

## Dependencias y librerías principales
| Dependencia | Versión | Para qué se usa |
|---|---|---|
| shapely | 2.1.2 | Polígonos 2D, offsets `join_style=round`, booleanas planas, simplificación |
| trimesh | 5.1.0 | Mallas, extrusión, `Scene` con objetos nombrados, export `.3mf` / `.glb` / `.stl` |
| manifold3d | 3.5.3 | Engine de booleanas reales (garantiza sólidos manifold cerrados) |
| mapbox-earcut | 2.1.0 | Triangulación de polígonos con huecos |
| scikit-image | 0.26.0 | `medial_axis` para medir ancho de trazo · `opening`/`erosion`/`disk` en F2 |
| scipy | 1.18.1 | `ndimage.convolve` (poda del eje) · `ndimage.label` |
| numpy | 2.5.3 | Base numérica |
| svgelements | 1.9.6 | Parseo de SVG **aplicando las transformaciones** del archivo |
| vtracer | 0.6.15 | Vectorización jpg→svg (reemplaza a potrace, sin wheel de Windows) |
| resvg-py | 0.5.0 | Rasterizado SVG→PNG para el conversor a JPG (Rust, sin Cairo del sistema) |
| pillow | 12.3.0 | F1/F2 — conversión y binarización de imagen; rasterizado de polígonos |
| **pillow-heif** | **1.7.0** | **HEIC/HEIF (y AVIF) en Pillow — embebe libheif 1.23.3. Se registra al importar `raster`** |
| **rawpy** | **0.27.1** | **RAW de cámara (ARW, CR2, CR3, DNG, NEF) — embebe LibRaw 0.22.1** |
| lxml | 6.1.3 | Lectura del XML interno del `.3mf` |
| **fastapi** | **0.141.1** | **Routers, dependencias, validación de formularios** |
| **starlette** | **1.6.0** | **`SessionMiddleware`, `StaticFiles`, `TestClient`** (llega con fastapi) |
| **uvicorn** | **0.52.4** | **Servidor ASGI** — sin `[standard]`, que arrastra dependencias con C |
| **jinja2** | **3.1.6** | **Plantillas del servidor** |
| **python-multipart** | **0.0.32** | **Parseo de `multipart/form-data`** (subidas) |
| **itsdangerous** | **2.2.0** | **Firma de la cookie de sesión** |
| **argon2-cffi** | **25.1.0** | **Hash y verificación de contraseñas (argon2id)** |
| **three.js** | **0.186.0** | **Preview 3D del `.glb`** — vendorizado, no viene por pip |

Dev: pytest 9.1.1 · ruff 0.16.6 · mypy 2.3.1 · bandit 1.9.4 · **httpx 0.28.1** (lo pide `TestClient`)
· **pip-audit 2.10.1** · **gitleaks 8.30.1** (fuera del venv, instalado con winget)

## Archivos de configuración (chore)
| Archivo | Rol |
|---|---|
| `pyproject.toml` | Metadata, 12 deps + extras `web` (7) y `dev` (6), config de ruff / mypy / pytest / bandit |
| `.gitignore` | `.venv/`, `__pycache__/`, `out/`, `*.3mf`, `*.stl`, `*.glb`, `credenciales.json`, `sesion.key`, `trabajo/` |
| `prompt_cortante.md` | **Contrato funcional** del motor — no es documentación decorativa |
| `credenciales.json` | Usuarios y hashes argon2id. **Gitignoreado**, nunca se commitea |
| `sesion.key` | Secreto de firma de la sesión, generado al primer arranque. **Gitignoreado** |

## Docker / contenedores
- **Dockerfile:** no
- **docker-compose:** no aplica (Docker existe en la máquina pero el proyecto no lo usa)

## Comandos
| Acción | Comando |
|---|---|
| Setup | `py -3.13 -m venv .venv` y `.venv/Scripts/python -m pip install -e ".[dev,web]"` |
| Build | no aplica (sin build step, ni en el back ni en el front) |
| **Dev / run (web)** | `.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000` → http://127.0.0.1:8000 |
| Test | `.venv/Scripts/python -m pytest` → **302 passed** (~76 s) — ⚠ **sin `-q`**, ver nota |
| Test rápido | `.venv/Scripts/python -m pytest -m "not lento"` (saltea el que corre el motor real) |
| Lint | `.venv/Scripts/python -m ruff check app cutter3d tests` |
| CLI | `.venv/Scripts/python -m cutter3d --svg <arte.svg> --modo cortante+marcador --out <salida.3mf> --reporte` |

⚠ **No le agregues `-q` al comando de test.** `pyproject.toml` ya trae `addopts = "-q"`, así que un
`-q` explícito lo vuelve **`-qq`** y pytest **suprime la línea `N passed`**: la corrida termina en el
bloque de warnings y parece que no dijo nada. Es exactamente lo que hacía el comando documentado hasta
el ciclo 3.

El CLI tiene 4 subcomandos: **`cortante`** (default — si el primer argumento no es un subcomando
conocido, se asume), **`jpg`** (png/jfif/webp/svg → jpg), **`lineas`** (jpg → B/N puro) y
**`vectorizar`** (imagen → svg). La instalación registra además el comando `cutter3d`. Desde el ciclo 3
el grupo de dimensiones tiene **10** flags: el décimo es `--distancia-colision` (default 1 mm).

**Variables de entorno** (ninguna obligatoria; ⛔ nunca en un `.env`):
`STUDIOCUTTER_SECRET` (secreto de sesión, ≥ 32 caracteres) ·
`STUDIOCUTTER_COOKIE_SECURE=1` (cuando haya HTTPS adelante) ·
`STUDIOCUTTER_DIR_TRABAJO` (directorio de trabajos).

## Gates de calidad
| Gate | Comando | Alcance | Umbral / baseline |
|---|---|---|---|
| Tipos | `.venv/Scripts/python -m mypy` | `cutter3d/` + `app/` (por `files` en config) | `strict = true` · **sin baseline** |
| Estilo | `.venv/Scripts/python -m ruff check app cutter3d tests` | archivos del diff | 0 desvíos |
| Formato | `.venv/Scripts/python -m ruff format --check app cutter3d tests` | archivos del diff | 0 desvíos |
| Complejidad | reglas `C901` / `PLR` de ruff | archivos del diff | ciclomática ≤ 10 · ≤ 5 posicionales |
| SAST | `.venv/Scripts/python -m bandit -c pyproject.toml -r app cutter3d` | `app/` + `cutter3d/` | 0 hallazgos medio/alto |
| **Secretos** | **`gitleaks dir app` / `cutter3d` / `tests`, y `gitleaks git .`** | fuentes + historial | **0 hallazgos** |
| **CVEs** | **`.venv/Scripts/python -m pip_audit`** | todo el venv | **0 vulnerabilidades conocidas** |
| **Sintaxis JS** | **`node --check <archivo>`** | los `.js` del diff | **0 errores de parseo** |

**Notas — excepciones documentadas, no deuda escondida:**

- **`gitleaks` se corre por directorio, no sobre la raíz.** Un `gitleaks detect --no-git` en la raíz
  escanea `.venv/` y devuelve 208 falsos positivos de paquetes de terceros. `gitleaks dir <fuente>`
  y `gitleaks git .` cubren lo que es del proyecto, que es lo único que el gate puede exigir.
  **No está en el PATH del shell**: vive en
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gitleaks.Gitleaks_*\gitleaks.exe`.
- **`pip-audit` saltea `studiocutter3d`** ("dependency not found on PyPI") porque el propio proyecto
  está instalado en editable y no se publica. Es esperado, no un hallazgo.
- **`disallow_untyped_calls = false`** es la única perilla de `strict` que se afloja. Motivo:
  **skimage y trimesh SÍ traen `py.typed`** pero exponen funciones sin anotar (`medial_axis`, `disk`,
  `threshold_otsu`, `PBRMaterial`, `TextureVisuals`, `Scene.export`). Como el módulo se resuelve,
  `ignore_missing_imports` no las alcanza y la alternativa sería sembrar `# type: ignore` por todo el
  código propio — peor, porque también esconde los errores reales. Todo el resto de `strict` sigue
  activo.
- **`N818` desactivado** en ruff: las excepciones tienen nombre en español (`SvgInvalido`,
  `MallaNoManifold`, `BooleanaFallida`) y el sufijo `Error` inglés las volvería híbridos ilegibles.
- **`PLC0415` silenciado en dos líneas de `app/tareas.py`**, con el motivo al lado: el import de
  `cutter3d` va adentro de la función a propósito, para que el servidor no cargue trimesh y
  manifold3d solo por definir la tarea del hijo.
- **Sin baseline, y corresponde**: el proyecto nació en el ciclo 1 con la deuda en cero. Cualquier
  hallazgo de gate es del ciclo que lo produjo.
- **`node --check` es el único gate que mira el JS, y solo valida que parsee.** Se agregó en el ciclo
  5 al descubrir que **`node` está instalado en esta máquina**: no lo usa el proyecto —sigue sin
  bundler, sin `package.json` y sin build step— pero está disponible, no agrega ninguna dependencia y
  cubre de errores de sintaxis los 65 KB de JS que antes no miraba nada. ⚠ **No es un linter**: no
  ve estilo, variables sin usar ni nada semántico. Si algún día se quiere eso, eslint exigiría
  `package.json` y `node_modules`, que es justamente lo que el proyecto evitó.
- **Ningún gate mira el CSS**, y sigue siendo el bloque menos verificable: se contrasta a ojo contra
  los artboards. Ya costó dos veces — la colisión de `.barra` en el ciclo 4 y, en el ciclo 5, un
  `.boton--todo` declarado **antes** de `.boton` cuyas propiedades quedaban pisadas por tener la
  misma especificidad. Las dos las encontró una lectura, no una herramienta.

## Red de regresión
- **Estado:** `caracterización` (motor) + `unit`/integración (web, con `TestClient`)
- **Ubicación:** `tests/test_fidelidad.py` (motor) y `tests/test_web_*.py` (web), con `tests/conftest.py`
- **Cómo se corre:** `.venv/Scripts/python -m pytest` → 550 passed, 2 skipped (~150 s).
  ⚠ **Nunca agregarle `-q`**: `pyproject.toml` ya trae `addopts = "-q"`, así que un `-q` explícito lo
  vuelve `-qq` y pytest **suprime la línea `N passed`** — la corrida termina en los warnings y parece
  que no dijo nada.
- **Áreas cubiertas:**
  - *Motor:* el pipeline de geometría completo (`svg_io → geometry → solids → export`), verificado
    **releyendo el `.3mf` exportado**, no la malla en memoria. Los asserts numéricos SON el golden
    master: no se versionan `.3mf` binarios, que cambiarían con cada versión de manifold3d sin que
    cambie nada real. Desde el ciclo 3 cubre además el **puenteo de colisiones entre extremos**:
    `dos_lobulos.svg` (sintético, 12 segmentos rectos — cámara de 14,4 mm detrás de un cuello de
    2,52 mm, la topología mínima que produce el bolsillo ciego) y `murcielago.svg` (arte vectorizado
    real, `@pytest.mark.lento`, criterio `euler_number == 0` releído del `.3mf`).
  - *Cortador de una pieza y filo de ancho constante:* fixture **`sr-cara-papa.svg`** — line art real
    con las dos manos rozando el cuerpo, que es el caso que destapó los dos defectos del detector por
    semillas. Cuatro tests, y el par que más vale es el que **documenta el defecto crudo** al lado del
    que lo arregla: `test_una_mano_pegada_al_cuerpo_deja_cuerpos_sueltos_sin_puentear` arma los
    offsets a mano sobre la silueta **sin** puentear (`o1` con 2 huecos → 2 cuerpos sueltos de 0,21 y
    2,21 mm²) y `test_el_cortador_sale_en_una_sola_pieza` pasa la misma silueta por
    `construir_cortador_2d`. El ancho real del filo lo fija
    `test_el_filo_no_baja_a_una_garganta_mas_angosta_que_sus_dos_paredes` con `medir_ancho_trazo` —la
    misma herramienta del trazo del marcador—, comparando **contra los percentiles del `circulo`**, que
    no tiene una sola colisión y por eso es el filo sano de referencia (p1 0,949 / p95 1,044): sin ese
    patrón habría que elegir un umbral a dedo, y el piso del rasterizado a 20 px/mm se confundiría con
    un defecto. La contraparte 3D es `test_el_cortador_del_sr_cara_papa_sale_de_una_pieza`
    (`@pytest.mark.lento`), que cuenta **`body_count == 1` sobre la malla releída del disco** — el
    único número que distinguía el cortador roto del sano, y el que no miraba nadie.
  - *Web:* login y no-enumeración de usuarios, autorización de las 4 páginas y de la API, open
    redirect, contenido de la sesión y flags de la cookie, detección de formato por contenido, límite
    de tamaño por las dos vías, nombres de archivo hostiles, path traversal en el id, claves fuera del
    enum, propietario ajeno, ciclo completo de polling, **timeout con el hijo muerto de verdad**, tope
    de trabajos simultáneos, un hijo que muere sin escribir, los 10 parámetros fuera de rango, y que
    ningún error devuelva rutas del servidor. Un solo test (`-m lento`) corre el motor real de punta a
    punta.
  - El andamiaje aísla todo con `dependency_overrides`: ningún test toca `trabajo/` ni depende de que
    `credenciales.json` exista, y **la contraseña de prueba se genera al vuelo** — no hay ninguna
    credencial literal en el repo.
  - *Front (contrato, no comportamiento):* **17 tests** en `test_web_auth.py` verifican lo que el
    servidor SIRVE — los 5 estáticos sin internet, el grafo de módulos del visor 3D recorrido con BFS,
    los 8 colores de la paleta contra `COLORES`, que ningún hex esté escrito dos veces, el botón de
    generar apagado con su pista, los tokens de los dos temas en el CSS, cero URLs externas, el menú
    de módulos único con nombre accesible por ítem, el CSS sin `--sidebar-ancho`, y el estado en
    `sessionStorage`.
- **Sin cobertura:** `cli.py`, `__main__.py`, y el **comportamiento** del CSS y del JS.
  ⚠ Precisión que costó una confusión: **no es que el front no tenga cobertura** — tiene los 11 tests
  de contrato de arriba. Lo que no hay es (a) ningún **gate de calidad** que mire CSS o JS (no hay
  eslint ni stylelint ni gestor de paquetes JS) y (b) ningún test que **ejecute** JS: no hay navegador
  ni Playwright. La distinción importa: en el ciclo 4 apareció una colisión de clase CSS
  (`.barra` del encabezado contra `.barra` de la barra de progreso, que declara `height: 5px`) que
  **ningún gate ni test habría detectado** — se encontró leyendo.

## Estructura del proyecto (alto nivel)

```
studiocutter3d/
├─ pyproject.toml           ← deps + extras web/dev + config de los gates
├─ prompt_cortante.md       ← contrato de geometría
├─ credenciales.json        ← gitignored
├─ sesion.key               ← gitignored, se genera solo
├─ trabajo/                 ← gitignored, un directorio por UUID, TTL 6 h
├─ cutter3d/                ← motor puro (13 archivos, sin dependencias web)
│  ├─ params.py  errors.py  svg_io.py  measure.py
│  ├─ geometry.py  solids.py  export.py  verify.py
│  ├─ raster.py  vector.py
│  └─ cli.py  __main__.py  __init__.py
├─ app/                     ← capa web (17 .py + 6 plantillas + estáticos)
│  ├─ main.py               ← crea la app, middleware de tamaño, handlers de error, limpieza
│  ├─ config.py             ← TODO lo que cambia entre local y expuesto
│  ├─ seguridad.py          ← credenciales argon2id + secreto de sesión
│  ├─ dependencias.py       ← ajustes, almacén, plantillas y `exigir_login`
│  ├─ errores.py            ← excepciones del motor → HTTP sin filtrar rutas
│  ├─ almacen.py            ← Trabajo · AlmacenTrabajos (Protocol) · AlmacenEnMemoria
│  ├─ archivos.py           ← UUID, detección por contenido, enum de claves, TTL
│  ├─ trabajos.py           ← lanzar / vigilar / cancelar, con timeout real
│  ├─ tareas.py             ← lo que corre EN el proceso hijo
│  ├─ routers/              ← auth · paginas · conversor · lineas · cortante · trabajos
│  ├─ templates/            ← base · macros · login · conversor · lineas · cortante
│  └─ static/
│     ├─ css/estilo.css     ← el sistema completo: tokens de los 2 temas, un breakpoint (860 px)
│     ├─ js/app.js          ← tema, subidas, polling, reporte
│     ├─ js/preview3d.js    ← three.js: entorno PMREM, sombras, encuadre automático
│     └─ vendor/            ← three 0.186.0 + fuentes (2,4 MB, para andar sin internet)
└─ tests/                   ← 228 tests
   ├─ conftest.py           ← andamiaje aislado de la web
   ├─ test_params.py  test_svg_io.py  test_geometry.py  test_raster.py
   ├─ test_fidelidad.py     ← red de caracterización del motor
   ├─ test_web_auth.py  test_web_uploads.py  test_web_trabajos.py
   └─ fixtures/             ← circulo · estrella · lineart_ojos_llenos · dos_lobulos · murcielago ·
                               kitty_bruja
```

## Convenciones y restricciones
- **`prompt_cortante.md` manda sobre la geometría.** Sus números y su regla de fidelidad se
  transcriben literalmente; no se reinterpretan ni se "mejoran".
- **Regla de fidelidad:** la única modificación permitida al arte es la dilatación uniforme (más la
  simplificación de 0,02 mm que el propio contrato ordena). Prohibido: cierres morfológicos, rellenos
  entre trazos, macizo→contorno, suavizado, marcos. Los efectos colaterales (muescas selladas) se
  **miden y reportan**, no se compensan.
- **Límites de parámetros:** ningún valor `<= 0`, ninguno `> 1000 mm`. Se valida en `params.py`,
  en un solo lugar — **la web no los revalida**, solo traduce el error a un 422 con el nombre del campo.
  Dos verdades sobre el mismo límite se desincronizan.
- **Nada de fallbacks silenciosos.** Falla duro lo que produciría un archivo inválido; advierte en el
  reporte lo que produce un archivo válido pero difícil de imprimir.
- **La verificación relee el archivo exportado**, nunca la malla en memoria.
- **Los offsets del cortador se derivan de los parámetros**, no se hardcodean: `o1 = luz`,
  `o2 = luz + filo_ancho`, `o3 = luz + filo_ancho + pie_ancho_extra`.
- **El cortador puentea las colisiones entre extremos, y es la ÚNICA excepción a la regla de
  fidelidad.** Cuando dos extremos quedan tan cerca que las paredes externas del filo (`o2`) se tocan
  —o les queda una luz ≤ `distancia_colision_mm`—, la muesca se rellena **en una copia de la silueta,
  la que consume el cortador, aguas arriba de los offsets**, para que `o1`/`o2`/`o3` salgan todos de
  la misma y las secciones sigan cerrando. Con los defaults el umbral es una boca de `2*o2 + 1 =
  4,4 mm`. El motivo es físico: una muesca cerrada por filo en los cuatro lados es un bolsillo ciego
  donde la masa se atasca, y el contrato prefiere perder la muesca antes que eso. **El arte y el
  marcador no se tocan nunca** — el puenteo vive solo en `construir_cortador_2d`, y hay un test que lo
  fija. Se reporta con `colisiones_puenteadas` / `area_puenteada_mm2` / `area_puenteada_pct`: se mide
  y se declara, no se compensa.
- ⚠ **Se puentea por REGLA, no por síntoma — y eso compra dos garantías del producto.** `base` es el
  cierre morfológico de la silueta con radio `o2 + distancia/2`, o sea **exactamente** todo lo que el
  complemento tiene más angosto que `2*o2 + distancia` (el cierre por disco de radio `r` deja intacto
  lo que un disco de radio `r` puede recorrer: rellena las gargantas más angostas que `2r` y ninguna
  otra). De ese material se puentea el que **entra en la banda del filo** — la zona que **no** queda
  contenida en `o1` —, y se deja el que no: una zona adentro de la luz el filo nunca la pisa, así que
  rellenarla no cambiaría el cortador y solo inflaría el área que el reporte declara como no cortada.
  Las garantías son **filo de `filo_ancho_mm` en todo su recorrido** (ni más fino ni más grueso) y
  **el cortador en una sola pieza**. `tapar_huecos(o2_forzado)` es **load-bearing**: mete la cámara
  entera al puente; sin ese paso la cámara sobrevive como hueco de `o1` y vuelve el cuerpo suelto.
  **Lo que NO alcanza**, y era lo que había: sembrar con bolsillos ciegos ya cerrados del filo + el
  material que agrega forzar la colisión. Ninguna de las dos semillas cubre el caso más común —dos
  paredes de `o2` que **ya se tocan** sin encerrar nada, un brazo rozando el cuerpo—, porque forzar la
  colisión no agrega material donde ya estaba fusionado y sin cámara cerrada no hay bolsillo que
  sembrar. Medido sobre `sr-cara-papa.svg`: **2 cuerpos sueltos** (0,21 y 2,21 mm²) y el filo con
  **p1 0,283 mm** y p95 1,105; con la regla, 0 sueltos y p1 0,943 / p95 1,030 — el mismo rango que el
  `circulo`, que no tiene una sola colisión (0,949 / 1,044).
- **Un cuerpo suelto del cortador es archivo inválido, no advertencia.** `cuerpos_sueltos(anillo, o1)`
  devuelve las piezas del pie que **no rodean galletita** (sin ningún hueco que interseque `o1`), y
  `construir_cortador_2d` levanta `CuerpoSueltoEnCortador` **antes de extruir nada**. Son exactamente
  los huecos de `o1`: cuando la boca de una muesca queda bajo `2*luz` y adentro se ensancha, `o1` se
  cierra sobre ella y `o2 - o1` vale la cámara entera — un pedazo de filo de 10 mm flotando, que no se
  puede usar ni pegar. La excepción **no** está en `_TRADUCCIONES` de `app/errores.py`: con el puenteo
  por regla es inalcanzable, así que si aparece es un bug de este motor y sale como `interno` 500 con
  traza, igual que `ConversionInfiel`. Un dibujo legítimamente en varias piezas **no** dispara nada:
  cada anillo rodea su galletita (verificado con dos círculos disjuntos).
- **El conteo de colisiones puenteadas NO es monótono.** Cuenta componentes conexas, así que al subir
  `distancia_colision_mm` dos zonas vecinas pueden fundirse y el número baja mientras el área sube.
  **El área sí es monótona** y es la que hay que mirar para juzgar cuánto se puenteó. Con la regla
  nueva la monotonía del área es **estructural** y no un parche: `base` crece con la distancia, cada
  zona crece con `base`, y una zona que se salía de `o1` se sigue saliendo. El detector viejo
  necesitaba dos fuentes de semilla justamente para no perderla.
- ⚠ **El punto ciego que tenía la verificación, y con qué se tapó.** El Euler esperado del cortador no
  es `0` fijo desde el ciclo del velocirapto: se calcula con `euler_esperado_de(cortador.pie)` sobre
  la huella que se extruyó de verdad — y **tiene que ser así**, porque una ventana del pie es un
  sólido válido de género 2. El costo es que **un cortador partido en islas cumple su propio número**:
  3 piezas y 1 hueco esperan 4, y la malla mide 4. El `.3mf` de `sr-cara-papa` salía watertight, con
  euler ✓, **VERIFICADO**, y con dos pedazos de filo sueltos adentro. La verificación se validaba a sí
  misma. No se tapa con otro número topológico —ninguno los distingue— sino con `cuerpos_sueltos()`,
  que pregunta otra cosa: *¿esta pieza rodea galletita?*. La lección general, y vale para toda la
  batería: **un invariante derivado de la geometría que se quiere probar no prueba nada; el contraste
  tiene que venir de afuera.** La otra mitad —que el filo mida lo pedido— se fija en los tests con
  `medir_ancho_trazo(c.filo, a)` contra los percentiles del `circulo`, y **no** en `verify`: una
  segunda pasada de `medial_axis` a 20 px/mm cuesta ~63 MB/megapixel y el techo de RAM del hijo de la
  web (380 MB) no está para pagarla.
- **Sigue en pie lo demás de la circularidad**: las secciones comparan contra `o3.area − o1.area`
  derivada de los mismos offsets ya puenteados y la luz mínima solo puede alejarse, así que
  **`todo_ok=True` sigue siendo compatible con un cortante muy puenteado**. Lo que protege es el
  default de 1 mm y que el usuario lea el porcentaje de la advertencia. Es deliberado: un cortante muy
  puenteado es un sólido válido y el contrato manda advertir eso, no fallar. Lo que cambió es que ya
  **no** es compatible con un cortante *roto* — eso ahora falla duro.
- **El contorneado de macizos vive SOLO en F2** (`raster.py`), viene encendido por default y
  **siempre declara** cuántas zonas tocó y qué área. F3 nunca altera el arte.
- **La salida de F2 va en PNG, nunca en JPG**: la compresión volvería a meter grises en el borde.
- **F2 vectoriza además a SVG** (`salida.png` + `salida.svg`). No es un extra: **F3 solo acepta
  SVG**, así que este es el único puente desde una imagen, y el mejor momento posible para
  vectorizar — la imagen acaba de quedar en dos valores puros, que es donde vtracer pierde menos.
- **F3 no vectoriza nada.** `FORMATOS_CORTANTE` es `{svg}` y punto: una vectorización escondida
  adentro del cortante sería una pérdida de fidelidad que el usuario no eligió ni puede revisar.
  Producir el SVG es trabajo del Convertidor y de F2, que lo dejan descargable antes de este paso.
- **Los parámetros que el motor ignora no se muestran ni se envían.** En modo `cortante` el motor
  no usa `altura_base_mm`, `altura_trazos_mm` ni `ancho_trazo_mm` (no hay marcador que construir),
  así que la pantalla los esconde y el formulario no los manda. `luz_mm` **sí** se usa en los dos
  modos: es `offset_o1`, donde arranca el filo. La marca vive en `CampoParametro.solo_marcador`,
  del lado que conoce el motor, no en el template.
- **Las descargas del cortante se habilitan al TERMINAR el trabajo, no después de la vista previa.**
  El archivo ya está completo en el servidor, y el visor puede fallar por cosas que no dicen nada de
  él (sin WebGL, un GLB que no carga). Lo único que sigue dependiendo del preview es el TEXTO de la
  pista, que es texto y no una traba.
  ⚠ Esto se invirtió en el ciclo 4: antes esperaban al visor y había un `MS_ESPERA_PREVIEW` de 15 s
  para destrabarlas. Ese timeout **se borró en el commit `63c1574`** y el spec base lo siguió
  describiendo hasta el ciclo 5. El único tope que queda en `app.js` es `MS_ESPERA_FOTO` (30 s) y
  cubre otra cosa: el handshake de la foto.
- **La foto del cortante es la excepción, y por eso se sube on-demand.** Es el único "archivo" que no
  existe en el servidor cuando el trabajo termina: la rinde el navegador. Se sube al hacer clic —no
  con debounce al renderizar— porque depende del color de la pieza y del fondo, que se pueden cambiar
  en cualquier momento: subir antes abriría una ventana en la que el servidor tiene una foto vieja y
  nadie se entera. El ZIP **no depende de ella**: si no se puede generar, baja igual y la pantalla
  dice por qué, con el motivo real y no con un texto fijo.
- **La foto comparte el rig del visor pero no su reparto de luz, y la diferencia está medida.**
  Las dos vistas se arman con las mismas funciones (`crearEntorno`, `agregarLuces`, `agregarPiso`),
  la misma exposición, el mismo tone mapping y la misma dirección de luz principal — lo único propio
  de la foto es cuánta luz **sin dirección** hay (`ESTUDIO_FOTO` + `neutralizarAmbiente`). El motivo
  es geométrico: vista a plomo, el panel cenital del visor cae por igual sobre el plato y sobre el
  fondo del surco —ahí adentro no hay oclusión ambiental que lo tape—, así que el grabado del
  marcador quedaba en **9,5 niveles de contraste sobre 255** y la pieza salía 72 niveles por encima
  de su propio color. Con el reparto de la foto son **29,1**. ⚠ La luz principal **no se toca**: es
  la que proyecta la sombra, y `neutralizarAmbiente` no puede ni nombrarla (hay un test que lo
  exige). Medido con un modelo del estudio corrido en node, fuera del navegador.
- **La vista imagen no sigue el tema claro/oscuro, y el visor sí.** Es deliberado: el visor se dibuja
  sobre el fondo de la página, pero la foto es un ARCHIVO que el usuario se lleva, y su contenido no
  puede depender de una preferencia de UI. Consecuencia aceptada: en tema oscuro la sombra del visor
  es más densa que la de la foto.
- **El `.3mf` se entrega combinado y también por objeto.** El combinado es el archivo bueno: los
  dos cuerpos en su posición anidada real, que es lo que se imprime. Los sueltos
  (`salida_marcador.3mf`, `salida_cortador.3mf`) son el mismo cuerpo **en las mismas coordenadas**
  — no se recentran, así que abrir los dos en el slicer los reencuentra anidados. En modo
  `cortante` no se generan: serían una copia del combinado.
- **El cliente no nombra ningún archivo EN DISCO, pero sí cómo se VE la descarga.** Lo que sube se
  guarda como `entrada.<ext>` con la extensión sacada de **mirar los bytes** (ni el nombre ni el
  `Content-Type`, que los elige quien sube); las salidas salen de `NOMBRE_DE`, un mapa fijo. Eso no
  cambió. Lo que sí cambió en el ciclo 4: el **stem** del nombre original, saneado con
  `sanear_nombre_base` (whitelist `[A-Za-z0-9._-]`, tope 60), se guarda en `Trabajo.nombre_base` y
  alimenta **solo** el `filename=` de `FileResponse`, para que `buddy.heif` baje como `buddy.jpg`.
  **Las dos mitades nunca se cruzan** — el valor del cliente jamás participa de un `Path` —, y por eso
  conservar el nombre no reabre el path traversal. Consecuencia aceptada de la whitelist estricta:
  `mi dibujo.png` baja como `mi_dibujo.jpg`.
- **`MAX_PIXELES = 89_478_485` es el tope único para los TRES decodificadores** (Pillow, LibRaw y
  vtracer), aplicado siempre **antes** de decodificar. Es el mismo número que Pillow usa como guard de
  decompression bomb, reusado a propósito: un solo límite. Hace falta declararlo porque `rawpy` y
  vtracer **no pasan por Pillow** y no tienen red propia. ⚠ Residual conocido: en el camino SVG,
  resvg ya reservó el bitmap antes de que Python pueda mirarlo — el tope acota el límite y el tipo de
  error, no la asignación.
- **La navegación es UNA barra superior (`.barra-superior`), en los dos lados del breakpoint.** No hay
  sidebar lateral ni barra inferior de mobile. La lista de módulos se sirve desde
  `app/routers/paginas.py:MODULOS` —un solo lugar, como `CAMPOS` y `COLORES`— y se dibuja con el macro
  `nav_modulos`. ⚠ **Ojo con el nombre:** `.barra` a secas **ya existía** y es la barra de PROGRESO de
  las filas del conversor (`estilo.css`, `height: 5px`); como se declara después, pisaría al
  encabezado si este se llamara igual.
- **Los mensajes de error se arman desde los atributos de la excepción, nunca con `str(exc)`.**
- **La autorización es una dependencia (`UsuarioRequerido`), no una línea adentro del handler**: una
  ruta que se olvide de declararla queda abierta, y eso se ve leyendo la firma.
- **Cero URLs externas** en templates, CSS y JS. La app tiene que andar sin internet, y hay un test
  que lo verifica.
- **Nada de `innerHTML` con datos del servidor o del usuario** en el JS: las filas salen de un
  `<template>` y los textos se escriben con `textContent`.
- **Nombres y comentarios en español**, incluidas las excepciones.
- Todo comando de Python usa `py -3.13` o `.venv/Scripts/python`: **`python` no existe en el PATH
  de esta máquina**.
- **Heredocs de bash: sirven, con dos trampas.** Un heredoc con delimitador entre comillas funciona
  para la mayoría de los archivos, pero falló con un `.py` de comillas anidadas densas y **mangló
  secuencias de escape** (`\x89PNG\r\n` salió como bytes reales). Para archivos con comillas anidadas
  o escapes, usar las tools de escritura.
