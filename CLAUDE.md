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

`generar()` en `__init__.py` es la entrada pública del pipeline, y su orden es el mejor orden de
lectura:

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

#### La entrada de atrás: `malla.py` y `paquete3mf.py`

Todo el resto del paquete va de un SVG a un `.3mf`. `cutter3d/malla.py` va al revés: lee un `.3mf`
o un `.stl` **que este motor no construyó** y le deriva el `.glb` que F4 fotografía. Va aparte de
`export.py` porque su entrada no es confiable: ahí viven las cotas (`MAX_TRIANGULOS`) y la lectura
defensiva.

- ⚠ **Un `.3mf` no transporta materiales, y sin re-aplicarlos la foto sale distinta.** Medido: el
  `.3mf` releído conserva geometría, bounds y watertight exactos pero llega **sin material**, y el
  `.glb` derivado sale **sin array `materials`** — o sea con el default de glTF (`metallic` 1.0,
  `roughness` 1.0), metal rugoso en vez de PLA mate. El front pisa el **color** (`pintarPieza`) pero
  nunca el acabado, así que nadie lo corrige aguas abajo. Por eso `solids.aplicar_acabado` es
  pública y tiene **un solo dueño**: la usan `construir_*_3d` y `malla.a_glb`. Separarlas es separar
  la foto de F3 de la de F4, que es exactamente lo que F4 existe para evitar.
- **La escala y la orientación sí se conservan**: los bounds del `.3mf`, del `.stl` y del `.glb` del
  motor coinciden al milímetro, con Z arriba y la pieza en z=0. El `rotation.x = -π/2` del visor vale
  igual para un archivo derivado: no hay nada que reorientar.
- **`a_glb` relee el `.glb` del disco** y compara triángulos, bounds y volumen contra la entrada,
  por la misma razón que `verify` relee el `.3mf`. Si no coinciden levanta `ConversionInfiel`, que
  **no está en `_TRADUCCIONES`**: sale como `interno` 500 porque es un bug nuestro, no del archivo.
  Una malla **abierta advierte y no bloquea** — esta pantalla no imprime nada.
- **`paquete3mf.py` está separado por costo de import.** Confirma que un ZIP sea de verdad un 3MF
  (`.model` adentro) y acota entradas y tamaño descomprimido leyendo el central directory, sin
  descomprimir. Vive aparte porque **lo importa el router**, para rechazar un docx con un 422 sin
  gastar uno de los tres slots de proceso; `malla.py` importa trimesh y meterlo en uvicorn revierte
  el ciclo 6. Verificado: tras `import app.main`, `trimesh` sigue sin cargarse.

### La capa web (`app/`)

- **Cuatro funcionalidades.** F1 conversor (13 formatos → jpg/svg) es **síncrona dentro del
  request**; F2 corrección de líneas, F3 cortante y F4 post corren en **un
  `multiprocessing.Process` por trabajo**, con polling desde el navegador. No es un
  `ProcessPoolExecutor` y el motivo es concreto: el pool **no sabe imponer un timeout** —
  `future.result(timeout=N)` corta la espera, no al worker.
- **F4 post (`/post`) entra por el lado del sólido, no del dibujo.** Recibe mallas ya construidas
  —de este motor o de cualquier otro— y les saca **la misma foto cenital** que F3: la rinde el
  mismo `preview3d.js` sobre un `.glb` derivado. Existe para los cortantes viejos y para cuando la
  foto no se bajó, que es la única forma de recuperarla: el `.glb` de un trabajo se va con él a las
  6 h. No hay parámetros — la geometría ya viene decidida adentro del archivo — y lo único que se
  elige son los colores, que son de pantalla.
- ⚠ **En F4 la unidad es el DISEÑO, no el archivo.** Un diseño puede venir en un `.3mf` combinado o
  en dos archivos sueltos (`<base>_cortador.stl` + `<base>_marcador.stl`), y las dos formas
  describen la misma pieza: fotografiar el cortador sin su marcador es fotografiar otra cosa.
  Verificado: el par suelto y el combinado dan **los mismos triángulos, las mismas medidas y el
  mismo volumen** — las coordenadas se conservan, así que unir las escenas los reencuentra
  anidados sin mover nada. **El emparejado lo hace el navegador**, que es el único que ve los
  nombres originales, y se puede corregir a mano (Separar / Unir). Al servidor llega
  `agrupacion=0:cortador,1:marcador;2:unico` — enteros y roles de una lista cerrada, ni un nombre
  de archivo, que es la regla de `app/archivos.py`.
- **Hasta 25 diseños por lote, convertidos DE A UNO** (`trabajos.lanzar_serie`): un proceso por
  diseño, el siguiente arranca cuando termina el anterior. No hace falta paralelismo y sí hace
  falta no saturar el servidor. Un proceso por paso y no uno largo para los tres, porque el techo
  de RAM y el timeout son **por proceso**, y porque un diseño ilegible no puede llevarse puestos a
  los otros 24: falla el suyo, se declara en el reporte y la serie sigue. Una serie ocupa **un solo
  lugar** del cupo de punta a punta, incluidos los huecos entre proceso y proceso — de eso se
  encarga `_series` en `vivos()`, y sin eso esos huecos son la puerta para pasarse del tope.
- **Los archivos por diseño NO están en `Trabajo.archivos`.** Ese dict mapea una clave de enum
  cerrado a un nombre fijo y no escala a 25 copias de lo mismo; se piden por **clave más índice**
  (`ClaveDiseno` + `nombre_de_diseno`, ruta `/api/trabajos/{id}/diseno/{n}/archivo/{clave}`). El
  cliente manda una palabra de una lista cerrada y un entero acotado: sigue sin nombrar nada.
  `Trabajo.disenos` es solo el contador; el detalle de cada uno viaja en `reporte`.
- **El set lo compone el SERVIDOR con Pillow** (`cutter3d/lamina.py`), no el canvas, y la razón es
  que así es **medible**: el reparto, la separación, el centrado y el color de los huecos se afirman
  desde la suite, y el comportamiento del JS no lo mira ningún gate. El color de los huecos **se
  lee de la esquina de las propias fotos** en vez de recibirse como parámetro — pedirlo aparte
  serían dos verdades sobre el mismo color y un set con los huecos de otro tono. Las celdas se
  achican con `Image.draft()` al abrirlas: 25 fotos de 2048 px enteras serían 314 MB de pico contra
  ~13 MB así, que es la misma lección del presupuesto de píxeles de F2.
- ⚠ **El set NO es una grilla rectangular: las filas pueden tener cantidades distintas.** Es lo que
  llena el cuadro. Una grilla pareja obliga a que la última fila quede corta —7 fotos en 3 columnas
  son `3-3-1`, con dos huecos juntos abajo— y repartirlas en `3-2-2` deja tres filas equilibradas.
  La regla es una: **tantas filas como `round(sqrt(n))`**, repartidas lo más parejo posible con las
  más largas arriba (3 → `2-1`, 5 → `3-2`, 7 → `3-2-2`, 9 → `3-3-3`). Por eso el dato del reporte es
  `distribucion` y no `columnas × filas`: ese par **no describe** el armado — un set de 7 y uno de 9
  dan los dos `3×3`.
- ⚠ **La lámina es CUADRADA siempre. Es un requisito duro, no una preferencia**: un JPG más ancho
  que alto, publicado en un marco cuadrado, sale con bandas arriba y abajo y **recortado de los
  costados**. Con celdas cuadradas un reparto que no es cuadrado no puede llenar un cuadrado —5
  fotos son `3-2`, y para que esas dos filas llegaran arriba y abajo cada celda tendría que medir
  medio lienzo, y entonces tres no entrarían a lo ancho—. Es geometría, no una decisión: el bloque
  se hace **lo más grande que entra** (el lado sale de `max(columnas, filas)`) y se **centra**. Lo
  que sobra no se ve como banda porque se pinta del mismo color que el fondo de las fotos, que es el
  mismo de los huecos.
- **Sin borde exterior**: la separación va solo *entre* celdas (`n-1`, no `n+1`). Un marco no separa
  nada de nada —afuera no hay otra foto— y lo único que hace es achicar las piezas para dejar un
  margen que el visor de cualquier red social vuelve a recortar. En la dimensión que llena, las
  celdas llegan al filo del lienzo.
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
- **La vista previa tiene UNA implementación para las dos pantallas que la usan.**
  `iniciarVistaPrevia3D` en `app/static/js/app.js` es de nivel de módulo, no de `iniciarCortante`:
  se lleva las paletas, las dos vistas y toda la coreografía de la foto (`pedirFoto` →
  `cortante:exportar` → `PUT /imagen` → recién ahí navegar). Es lo único que sostiene que la foto de
  F4 sea la misma que la de F3, y el mismo principio que ya seguía `preview3d.js`, que tampoco tiene
  una versión por pantalla. ⚠ **Los eventos siguen llamándose `cortante:*` en las dos** y los ids del
  bloque de vista previa (`#visor`, `#lienzo`, `#paleta*`, `#pista-*`) son **contrato**:
  `preview3d.js` los resuelve una sola vez a nivel de módulo y no tiene namespace por pantalla.
  Renombrar cualquiera deja la vista previa muerta y sin un solo error a la vista. Ahí "cortante"
  nombra a la pieza que se está mirando, no a la pantalla que la pidió.

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

`tests/test_malla.py` hace lo propio con F4. Sus dos tests centrales:
`test_el_glb_derivado_lleva_el_acabado_pla` —si se cae, la pieza se ve metálica y la foto de un
archivo viejo deja de coincidir con la de su ciclo— y
`test_un_diseno_partido_en_dos_da_lo_mismo_que_el_combinado`, que es lo que sostiene que agrupar
dos archivos no mueve nada. Las mallas se construyen con trimesh en el test por lo mismo que el
golden master no versiona sus salidas.

`tests/test_lamina.py` cubre el set, y existe **porque el set se compone del lado del servidor**:
el reparto, la separación, el centrado de las filas cortas y el color de los huecos son números que
la suite afirma. Si eso viviera en el canvas, no lo miraría nada. El test que más vale es
`test_el_reparto_es_lo_mas_cuadrado_que_se_puede`: en vez de fijar los cuatro casos del pedido,
compara contra **todos** los repartos posibles y exige que ninguno sea más cuadrado — así falla si
alguien cambia la fórmula por una que anda solo en los ejemplos.

⚠ Al medir la lámina, **el fondo de cada foto y el hueco entre celdas son del mismo color** — ese es
el punto del diseño. No se pueden distinguir mirando un píxel, así que el centrado se verifica por
**simetría** y el borde exterior con fotos a sangre (`_foto(..., pieza_completa=True)`). Y no se
sondean píxeles sueltos para medir el bloque: con un reparto en pirámide una columna cualquiera
puede caer en el hueco de una fila corta y en una celda de la de arriba — para eso está
`_caja_del_contenido`, que saca la caja de todo lo que no es fondo en una pasada de Pillow.

`tests/test_web_trabajos.py` cubre la serie, y el que importa es
`test_los_disenos_corren_de_a_uno_y_no_a_la_vez`: **no mide que tarde más** —tres procesos en
paralelo tardarían parecido a uno y un test de duración pasaría igual— sino que los intervalos de
cada paso **no se solapan**.

⚠ El fixture autouse `trabajos_limpios` de `conftest.py` existe por la misma razón que
`frenos_limpios`: `_procesos` y `_series` son estado de módulo, y con el cupo en 3, tres tests que
lanzan y no esperan hacen que el cuarto reciba un 429 **según el orden de ejecución**.

Sin cobertura: `cli.py`, `__main__.py`, y el **comportamiento** del CSS y del JS (no hay navegador ni
Playwright). El front sí tiene tests de **contrato** en `test_web_auth.py`: verifican lo que el
servidor sirve, no cómo se comporta. Los que valen para las dos pantallas con visor están
parametrizados sobre `CON_VISOR`, así que agregar una tercera es agregarla a esa lista.

⚠ **Lo único que prueba el punto de F4 es mirar las fotos.** "Salen los mismos números" lo
verifica la suite; "se ven iguales" no lo puede verificar nadie sin un navegador. El paso manual es:
generar un cortante, bajar su JPG y su `.3mf`, subir ese `.3mf` en `/post` con el mismo color de
pieza y de fondo, y comparar. Para el lote: soltar varios archivos —incluyendo un par
`_cortador`/`_marcador`— y revisar que la lista de diseños los haya agrupado como corresponde
**antes** de mandar, que es justo la parte que los botones Separar y Unir existen para corregir.
