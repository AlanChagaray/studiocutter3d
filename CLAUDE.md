# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es

studioCutter3D convierte line art en **cortantes de galletitas imprimibles en 3D** (`.3mf`), con
marcador opcional, y **prueba numéricamente que no alteró el dibujo**. Esa prueba es el punto del
proyecto, no un extra: cualquiera engorda un trazo y lo extruye; lo difícil es demostrar que el
resultado sigue siendo el mismo dibujo.

Dos documentos mandan sobre este archivo cuando hay duda:

- **`prompt_cortante.md`** — contrato funcional de la geometría. Sus números y su regla de fidelidad
  se transcriben **literalmente**: no se reinterpretan ni se "mejoran".
- **`.claude/project-spec.md`** — spec base (lo mantiene la skill `inspect`). Tiene el detalle fino:
  hallazgos de librerías que costaron días de encontrar, decisiones por ciclo, notas de cada gate.
  Ante algo que parezca raro, buscalo ahí antes de "arreglarlo" — probablemente ya está explicado.

⚠ La sección **"Estado" del README quedó en el ciclo 1** y dice que la capa web no existe. Existe
desde el ciclo 2, y hoy es la mitad del proyecto.

## Comandos

⚠ **`python` no está en el PATH de esta máquina** (solo el alias del Microsoft Store, y el default
del launcher es 3.14). Todo va con `.venv/Scripts/python` o `py -3.13`.

```bash
# Setup
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,web]"

# Web (http://127.0.0.1:8000)
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# CLI del motor — 4 subcomandos: cortante (default), jpg, lineas, vectorizar
.venv/Scripts/python -m cutter3d --svg arte.svg --modo cortante+marcador --out out/arte.3mf --reporte
```

### Tests

```bash
.venv/Scripts/python -m pytest                       # suite completa (~76 s)
.venv/Scripts/python -m pytest -m "not lento"        # saltea los que corren el motor real
.venv/Scripts/python -m pytest tests/test_geometry.py::test_nombre   # un test solo
.venv/Scripts/python -m pytest -k "puenteo"          # por patrón
```

⚠ **No le agregues `-q`.** `pyproject.toml` ya trae `addopts = "-q"`, así que un `-q` explícito lo
vuelve `-qq` y pytest **suprime la línea `N passed`**: la corrida termina en los warnings y parece
que no dijo nada.

El marcador `lento` es el que lanza el motor de verdad en un proceso hijo (segundos, no ms).

### Gates de calidad

Sin baseline: el proyecto nació con la deuda en cero y cualquier hallazgo es del ciclo que lo produjo.

```bash
.venv/Scripts/python -m mypy                                  # strict, files=["cutter3d","app"]
.venv/Scripts/python -m ruff check app cutter3d tests         # lint + complejidad (C901 <= 10)
.venv/Scripts/python -m ruff format --check app cutter3d tests
.venv/Scripts/python -m bandit -c pyproject.toml -r app cutter3d
.venv/Scripts/python -m pip_audit                             # CVEs del venv
node --check app/static/js/app.js                             # único gate que mira el JS
gitleaks dir app && gitleaks dir cutter3d && gitleaks dir tests && gitleaks git .
```

- **`gitleaks` se corre por directorio, nunca sobre la raíz**: un `detect --no-git` en la raíz escanea
  `.venv/` y devuelve 208 falsos positivos. No está en el PATH — vive en
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gitleaks.Gitleaks_*\gitleaks.exe`.
- **`pip-audit` saltea `studiocutter3d`** ("dependency not found on PyPI"): está instalado en editable
  y no se publica. Es esperado.
- **`node --check` no es un linter**: solo valida que el JS parsee. No hay eslint ni stylelint, y
  **ningún gate mira el CSS** — es el bloque menos verificable del proyecto, y ya costó dos bugs que
  encontró una lectura, no una herramienta.

## Arquitectura

Dos capas con una frontera real: **`cutter3d/` es una librería de dominio pura** y **`app/` la
orquesta**. Está verificado en las dos direcciones: `grep -rE "fastapi|uvicorn|jinja2|starlette"
cutter3d/` no devuelve nada, y `app/` no reimplementa una línea de geometría.

### El motor (`cutter3d/`)

**⚠ `cutter3d/__init__.py` importa el motor ADENTRO de `generar()`, a propósito.** No es desprolijidad
ni un import que alguien se olvidó de subir: importar el paquete por cualquier motivo ejecuta su
`__init__`, y `app/errores.py` hace `from cutter3d.errors import ...` sobre un módulo que solo importa
`__future__`. Con los imports arriba, eso arrastraba **1200 módulos y ~89 MB** a cada proceso de la
capa web, que no construye un solo polígono. Con `spawn` no hay copy-on-write, así que padre e hijo
suman. Medido: proceso web 128,7 → **95,8 MB**, `import app.tareas` 106,7 → **22,5 MB**. Los
re-exportados que viven en los módulos caros (`Salidas`, `ReporteFidelidad`, `render_texto`) salen por
el `__getattr__` de módulo. **Subir esos imports al tope revierte el ciclo 6 entero.**

`generar()` en `__init__.py` es la única entrada pública, y su orden es el mejor orden de lectura:

```
svg_io.cargar_svg
  └─ geometry.construir_marcador_2d   (modo cortante+marcador — bucle escala <-> dilatación)
     ó geometry.construir_silueta_sola (modo cortante — un paso, exacto)
  └─ geometry.construir_cortador_2d   (puenteo de colisiones → o1/o2/o3 → filo y pie)
  └─ solids.construir_escena          (extrusión + booleanas reales con el engine `manifold`)
  └─ export.exportar                  (.3mf + .glb, opcionalmente .stl)
  └─ verify.verificar → verify.exigir_manifold
```

Lo que no se deduce leyendo un archivo solo:

- **`verify` relee el `.3mf` del disco**, nunca la malla en memoria: es la única forma de probar que
  lo que se entrega es lo que se validó.
- **`exigir_manifold` corre DESPUÉS de escribir** el archivo, a propósito: falla, pero el `.3mf` queda
  para inspección. Es el que levanta `MallaNoManifold`.
- **El número de Euler esperado se calcula, no se fija**: `euler_esperado_de(huella)` devuelve
  `2 × (componentes − huecos)` de la huella 2D que se extruyó de verdad — la silueta para el
  marcador, **el pie** para el cortador (`o2 ⊆ o3` ⟹ `filo ⊆ pie`, así que el sólido se retrae sobre
  el pie). El contrato enuncia `2` y `0` para el caso canónico; la geometría real los generaliza en
  dos ejes — varias piezas (`c > 1`) y ventanas del pie (`h > 1`). Fijar el `0` daba por inválido un
  sólido con `watertight=True`.
- **Los offsets del cortador se derivan de los parámetros, nunca se hardcodean**: `o1 = luz`,
  `o2 = luz + filo_ancho`, `o3 = luz + filo_ancho + pie_ancho_extra`.
- **El pie se extruye como `o3 − o1`, no `o3 − o2`**: abajo de la altura del pie se solapa con el
  filo, así que la booleana no depende de dos caras coincidentes. La forma final es idéntica.
- **`_extruir` pasa `engine="manifold"` explícitamente.** El default de trimesh es earcut, que ante
  vértices colineales (VTracer los deja por tiradas) emite triángulos degenerados y la extrusión sale
  no watertight. El síntoma engaña: el polígono 2D es válido, el área y el volumen dan bien, y explota
  lejos de la causa con "Not all meshes are volumes!".
- **Los límites de los parámetros se validan en `params.py` y SOLO ahí.** La web no los revalida:
  traduce el error a un 422 con el nombre del campo. Dos verdades sobre el mismo límite se
  desincronizan.
- **Hay DOS límites de píxeles y no son lo mismo.** `MAX_PIXELES` (89,4 MP) dice **qué se acepta
  decodificar** — guard anti-bomba de descompresión, cubre la cámara de 61 MP. `MAX_PIXELES_TRABAJO`
  (3 MP) dice **a qué tamaño se procesa**, y es un presupuesto de memoria. Igualarlos "para
  simplificar" trae de vuelta el OOM. Lo que se reduce **se declara** (`ResultadoF2.tamano_original`).

### La capa web (`app/`)

- **Tres funcionalidades.** F1 conversor (13 formatos → jpg/svg) es **síncrona dentro del request**;
  F2 corrección de líneas y F3 cortante corren en **un `multiprocessing.Process` por trabajo**, con
  polling desde el navegador. No es un `ProcessPoolExecutor` y el motivo es concreto: el pool **no
  sabe imponer un timeout** — `future.result(timeout=N)` corta la espera, no al worker.
- **`app/tareas.py` es la otra orilla de la frontera entre procesos.** Funciones a nivel de módulo
  (en Windows el arranque es `spawn`), solo primitivos como argumentos, no devuelve nada, y **ninguna
  excepción escapa**. El resultado viaja por `estado.json` escrito de forma atómica: **su ausencia
  significa fallo, no "todavía no"**.
- **`app/trabajos.py`** lanza / vigila / cancela con un hilo vigilante por trabajo;
  **`app/routers/trabajos.py`** es la API de archivos (polling · descarga suelta · ZIP · cancelar ·
  `PUT /imagen`, el único camino de escritura del cliente).
- **La autorización vive en el almacén, no en los routers**: `AlmacenTrabajos.obtener()` exige el
  propietario, así que un router nuevo no puede olvidarse de chequearla. Para las páginas, la
  autorización es la dependencia `UsuarioRequerido` — una ruta que se olvide de declararla queda
  abierta, y eso se ve leyendo la firma.
- **El proceso hijo tiene techo de RAM** (`app/tareas.py:LIMITE_RAM_HIJO_MB`, 380 MB, **`RLIMIT_DATA`**,
  POSIX only). Es lo que separa "falló el trabajo" de "murió el servidor": sin techo, la asignación
  desbocada la corta el kernel y se lleva el contenedor (exit 137, **sin una línea de log**); con
  techo levanta `MemoryError` adentro del hijo y sale como `sin_memoria`. Verificado en un contenedor
  Linux de 512 MB.
  ⚠ **Es `RLIMIT_DATA`, no `RLIMIT_AS`, y confundirlos rompe todo.** Medido en el contenedor con tres
  cortantes: `VmPeak` (direcciones) **611 MB** · `VmData` (heap) **277 MB** · `VmHWM` (RSS) **291 MB**.
  Un cortante normal reserva el doble de direcciones de las que usa, así que un techo de `RLIMIT_AS`
  puesto contra el número de RSS **hace fallar hasta la estrella** — ya pasó, y no lo vio ningún test:
  lo agarró el end-to-end contra el contenedor, porque en Windows la métrica ni existe.
- **argon2 NO usa los defaults de la librería** (`app/seguridad.py`): va con el perfil de baja memoria
  de la RFC 9106 (19 MiB, `t=2`, `p=1`) en vez de 64 MiB con `p=4`. Con 0,1 CPU el `p=4` solo agrega
  contención. ⚠ **El hash señuelo se arma con los parámetros de los hashes guardados**
  (`_senuelo_como`), no con los del módulo: si el señuelo es más barato que el hash real, la
  diferencia de tiempo vuelve a enumerar usuarios, que es justo lo que el señuelo existe para tapar.
  Para dar de alta un usuario se usa `seguridad.hashear`, no `PasswordHasher()` a secas.
- **El threadpool está acotado a 8** (`app/main.py:HILOS_MAXIMOS`; anyio trae 40). Todos los handlers
  son `def`, así que ese número multiplica cada pico de memoria del proceso web.
- **`app/errores.py` arma los mensajes desde los ATRIBUTOS de la excepción, nunca con `str(exc)`**,
  que empieza con la ruta del filesystem del servidor. Lo que no tenga atributo seguro sale como
  `interno` y el detalle va al log.
- **El cliente no nombra ningún archivo en disco.** Lo que sube se guarda como `entrada.<ext>` con la
  extensión sacada de **mirar los bytes** (ni el nombre ni el `Content-Type`); el stem original,
  saneado con whitelist, alimenta **solo** el `filename=` de la descarga. Las dos mitades nunca se
  cruzan.
- **Front sin bundler, sin Node, sin `package.json`, sin build step.** three.js está vendorizado en
  `app/static/vendor/three/` (5 archivos con la disposición exacta de npm) y se resuelve con un
  `<script type="importmap">`. **Cero URLs externas** en templates, CSS y JS — la app anda sin
  internet y hay un test que lo verifica.

### Datos

Sin base de datos: un directorio por UUID bajo `trabajo/`, con TTL de 6 h más barrido de huérfanos.
Auth con argon2id (`credenciales.json`, gitignoreado) y sesión por cookie firmada. Variables de
entorno, ninguna obligatoria: `STUDIOCUTTER_SECRET`, `STUDIOCUTTER_COOKIE_SECURE`,
`STUDIOCUTTER_DIR_TRABAJO`, `STUDIOCUTTER_HOSTS`, `STUDIOCUTTER_MAX_TRABAJOS`, `..._DETRAS_DE_PROXY`,
`..._CREDENCIALES[_JSON]`, `..._ARCHIVO_SECRETO`. **⛔ Nunca en un `.env`.**

## Reglas que no se negocian

- **Regla de fidelidad:** la única modificación permitida al arte es la **dilatación uniforme** (más
  la simplificación de 0,02 mm que el propio contrato ordena). Prohibido: cierres morfológicos,
  rellenos entre trazos, macizo→contorno, suavizado, marcos. Los efectos colaterales (muescas
  selladas) se **miden y reportan**, no se compensan.
- **Única excepción, y solo para el cortador:** el **puenteo de colisiones entre extremos**
  (`geometry._puentear_colisiones`). Rellena una **copia** de la silueta, aguas arriba de los offsets,
  para que `o1`/`o2`/`o3` salgan todos de la misma. **El arte y el marcador no se tocan nunca**, y hay
  un test que lo fija.
- **Nada de fallbacks silenciosos.** Falla duro lo que produciría un archivo inválido (parámetro fuera
  de rango, SVG ilegible, escala que no converge, booleana que no cierra, malla no manifold) y no se
  escribe nada. **Advierte** en el reporte lo que produce un archivo válido pero difícil de imprimir.
- **El contorneado de zonas macizas vive SOLO en F2** (`raster.py`), viene encendido por default y
  siempre declara cuántas zonas tocó y qué área. **F3 nunca altera el arte.**
- **Nombres y comentarios en español**, incluidas las excepciones (por eso `N818` está apagado en
  ruff). **Nada de `innerHTML`** con datos del servidor o del usuario en el JS.

## Trampas ya pagadas (las más caras)

El listado completo está en `.claude/project-spec.md` → "Hallazgos de librerías". Las que más
probablemente vuelvan a morder:

1. **`skeletonize` de scikit-image 0.26 segfaultea** con la máscara del fixture del círculo. No es el
   tamaño (un disco sintético idéntico pasa). Se usa **`medial_axis`**.
2. **`pypotrace` no tiene wheel de Windows** → vectorización con **vtracer**, y
   `hierarchical="cutout"` es **obligatorio**: su default apila formas en vez de generar huecos.
3. **vtracer no falla ante un formato que no lee: PANIQUEA en Rust**, y `PanicException` hereda de
   `BaseException` — un `except Exception` no la ve. Por eso `vector.a_svg` normaliza a PNG antes.
4. **`Path3D.to_planar()` de trimesh exige `rtree`**, que no está instalado. Las áreas de sección se
   arman con shapely + XOR.
5. **Un import que 404ea en el front no tira ningún error visible**: el grafo entero deja de
   evaluarse, sin nada en la consola ni en el servidor. Por eso hay un test que **recorre el grafo**
   de módulos del visor con BFS en vez de una lista escrita a mano.
6. **`spawn` necesita un `__main__` que sea un archivo real.** Un script por stdin (`python - <<EOF`)
   hace fallar a todo proceso hijo con `OSError: Invalid argument: '<stdin>'`. Para probar algo que
   lance trabajos, escribí el script a un archivo.
7. **Heredocs de bash: sirven, con dos trampas.** Fallan con `.py` de comillas anidadas densas y
   **manglan secuencias de escape** (`\x89PNG\r\n` salió como bytes reales). Para esos archivos, usá
   las tools de escritura.
8. **`medial_axis` cuesta ~63 MB por megapixel, no 8.** La intuición dice que el costo es la
   transformada de distancia en `float64` (8 bytes/px); medido es **ocho veces eso**, porque skimage
   materializa además el orden de los píxeles de tinta y varios intermedios del mismo tamaño. Es el
   único paso de F2 que pica: `opening`, `erosion` y `label` no movieron el pico. Consecuencia
   práctica: una **foto de teléfono de 12 MP** —la entrada más común que existe— picaba **830 MB** en
   una instancia de 512 MB. No hacía falta ningún caso patológico, y por eso el presupuesto de
   píxeles no es una optimización sino la corrección de un bug.
9. **El presupuesto se aplica con `Image.draft()` antes del `resize`.** Reducir con `resize` a secas
   llega tarde: `resize` necesita el bitmap completo decodificado, así que abrir y reducir un JPEG de
   48 MP picaba 441 MB **antes** de que F2 empezara. `draft()` le pide al decodificador JPEG la
   imagen a 1/2, 1/4 u 1/8, y es un no-op en los demás formatos.

## Red de regresión

`tests/test_fidelidad.py` es el golden master del motor y **sus asserts numéricos SON la referencia**:
no se versionan `.3mf` binarios, que cambiarían con cada versión de manifold3d sin que cambie nada
real. Los `tests/test_web_*.py` cubren la capa web con `TestClient` y `dependency_overrides` — ningún
test toca `trabajo/`, ninguno depende de que `credenciales.json` exista, y la contraseña de prueba se
genera al vuelo (no hay una sola credencial literal en el repo).

Sin cobertura: `cli.py`, `__main__.py`, y el **comportamiento** del CSS y del JS (no hay navegador ni
Playwright). El front sí tiene tests de **contrato** en `test_web_auth.py`: verifican lo que el
servidor sirve, no cómo se comporta.
