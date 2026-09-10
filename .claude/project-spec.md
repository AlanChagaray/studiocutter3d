# Spec Base — studioCutter3D

> Generado por la skill `inspect` del workflow ViaBariloche. Es la referencia de contexto
> del proyecto entre sesiones: mientras esté vigente, `inspect` NO re-analiza el stack desde
> cero. Se regenera con aprobación cuando cambian las señales de frescura de abajo.

**Última actualización:** 2026-09-10
**Versión de plantilla:** 3

> Actualizado al cerrar el **ciclo 2** (capa web `app/`). El proyecto pasó de librería + CLI a
> aplicación web completa: 39 archivos nuevos, 62 tests web sumados a los 105 del motor
> (**167 en total**), y **6 gates** en verde — los 4 del ciclo 1 más `gitleaks` y `pip-audit`.

## Señales de frescura
- **Manifests:** `pyproject.toml` (existe en la raíz) — `version = "0.2.0"`
- **Versión lenguaje/framework:** Python 3.13.7 (`requires-python = ">=3.13"`) · FastAPI 0.141.1
- **Dependencias clave (con versión instalada):**
  - *Motor:* numpy 2.5.3 · scipy 1.18.1 · shapely 2.1.2 · trimesh 5.1.0 · manifold3d 3.5.3 ·
    mapbox-earcut 2.1.0 · scikit-image 0.26.0 · pillow 12.3.0 · svgelements 1.9.6 ·
    vtracer 0.6.15 · resvg-py 0.5.0 · lxml 6.1.3
  - *Web (extra `web`):* fastapi 0.141.1 · starlette 1.6.0 · uvicorn 0.52.4 · jinja2 3.1.6 ·
    python-multipart 0.0.32 · itsdangerous 2.2.0 · argon2-cffi 25.1.0
- **Front vendorizado (no hay gestor de paquetes JS):** three.js **0.186.0** en
  `app/static/vendor/three/` (5 archivos con la disposición de npm, sin minificar, 2,2 MB) · Space Grotesk + IBM Plex Mono en
  `app/static/vendor/fuentes/` (4 archivos, 72 KB, subset latino)
- **Lockfiles presentes:** ninguno (pip + venv, sin lock)
- **Configs de gates presentes:** ninguna suelta — todas dentro de `pyproject.toml`
  (`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`, `[tool.pytest.ini_options]`, `[tool.bandit]`).
  `gitleaks` corre con su configuración por defecto.

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
  - **F1 Convertidor** (png/jfif/webp/jpg/svg → **jpg o svg**, destino elegido por archivo):
    **síncrono**, dentro del pedido, un archivo por request. Medido: 14-45 ms a JPG y 14-190 ms a
    SVG (400 a 2000 px) — órdenes de magnitud menos que el arranque en frío del proceso hijo, que
    es de 1,09 s.
  - **F2 corrección de líneas** y **F3 cortante**: **un `multiprocessing.Process` por trabajo**, con
    polling desde el navegador. No es un `ProcessPoolExecutor` y el motivo es concreto: **el pool no
    sabe imponer un timeout**. `future.result(timeout=N)` corta la espera, no al worker. Con un
    `Process` dedicado, `join(timeout)` + `terminate()` da un timeout real con stdlib pura.
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
| Test | `.venv/Scripts/python -m pytest -q` → **167 passed** (~46 s) |
| Test rápido | `.venv/Scripts/python -m pytest -q -m "not lento"` (saltea el que corre el motor real) |
| Lint | `.venv/Scripts/python -m ruff check app cutter3d tests` |
| CLI | `.venv/Scripts/python -m cutter3d --svg <arte.svg> --modo cortante+marcador --out <salida.3mf> --reporte` |

El CLI tiene 4 subcomandos: **`cortante`** (default — si el primer argumento no es un subcomando
conocido, se asume), **`jpg`** (png/jfif/webp/svg → jpg), **`lineas`** (jpg → B/N puro) y
**`vectorizar`** (imagen → svg). La instalación registra además el comando `cutter3d`.

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
- **Ningún gate mira el CSS ni el JS.** Es el bloque más grande y menos verificable del ciclo 2
  (22 KB de CSS, 25 KB de JS): se contrasta contra los 8 artboards a ojo, y nada más.

## Red de regresión
- **Estado:** `caracterización` (motor) + `unit`/integración (web, con `TestClient`)
- **Ubicación:** `tests/test_fidelidad.py` (motor) y `tests/test_web_*.py` (web), con `tests/conftest.py`
- **Cómo se corre:** `.venv/Scripts/python -m pytest -q` → 167 passed
- **Áreas cubiertas:**
  - *Motor:* el pipeline de geometría completo (`svg_io → geometry → solids → export`), verificado
    **releyendo el `.3mf` exportado**, no la malla en memoria. Los asserts numéricos SON el golden
    master: no se versionan `.3mf` binarios, que cambiarían con cada versión de manifold3d sin que
    cambie nada real.
  - *Web:* login y no-enumeración de usuarios, autorización de las 4 páginas y de la API, open
    redirect, contenido de la sesión y flags de la cookie, detección de formato por contenido, límite
    de tamaño por las dos vías, nombres de archivo hostiles, path traversal en el id, claves fuera del
    enum, propietario ajeno, ciclo completo de polling, **timeout con el hijo muerto de verdad**, tope
    de trabajos simultáneos, un hijo que muere sin escribir, los 9 parámetros fuera de rango, y que
    ningún error devuelva rutas del servidor. Un solo test (`-m lento`) corre el motor real de punta a
    punta.
  - El andamiaje aísla todo con `dependency_overrides`: ningún test toca `trabajo/` ni depende de que
    `credenciales.json` exista, y **la contraseña de prueba se genera al vuelo** — no hay ninguna
    credencial literal en el repo.
- **Sin cobertura:** `cli.py`, `__main__.py`, el CSS, el JS del navegador y `preview3d.js`. La cadena
  argparse → motor y el render de las pantallas están verificados por lectura y corridas manuales, no
  por un gate.

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
└─ tests/                   ← 167 tests
   ├─ conftest.py           ← andamiaje aislado de la web
   ├─ test_params.py  test_svg_io.py  test_geometry.py  test_raster.py
   ├─ test_fidelidad.py     ← red de caracterización del motor
   ├─ test_web_auth.py  test_web_uploads.py  test_web_trabajos.py
   └─ fixtures/             ← circulo · estrella · lineart_ojos_llenos
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
- **Las descargas del cortante se habilitan después de la vista previa**, no junto con ella: lo que
  se baja es la misma geometría que se está viendo. Si el visor falla (sin WebGL, GLB que no carga,
  o el módulo que ni siquiera arranca) se habilitan igual — hay tres caminos que lo garantizan,
  incluido un timeout de 15 s en `app.js`: un visor roto no puede dejar un archivo bueno sin bajar.
- **El `.3mf` se entrega combinado y también por objeto.** El combinado es el archivo bueno: los
  dos cuerpos en su posición anidada real, que es lo que se imprime. Los sueltos
  (`salida_marcador.3mf`, `salida_cortador.3mf`) son el mismo cuerpo **en las mismas coordenadas**
  — no se recentran, así que abrir los dos en el slicer los reencuentra anidados. En modo
  `cortante` no se generan: serían una copia del combinado.
- **El cliente nunca nombra un archivo.** Lo que sube se guarda como `entrada.<ext>` con la extensión
  sacada de **mirar los bytes** (ni el nombre ni el `Content-Type`, que los elige quien sube); lo que
  baja se pide por una clave de un enum cerrado. No hay nada que sanitizar porque el nombre original
  directamente no se usa.
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
