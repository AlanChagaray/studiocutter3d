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
.venv/Scripts/python -m mypy                                  # strict, files=["cutter3d","app","scripts"]
.venv/Scripts/python -m ruff check app cutter3d tests scripts # lint + complejidad (C901 <= 10)
.venv/Scripts/python -m ruff format --check app cutter3d tests scripts
.venv/Scripts/python -m bandit -c pyproject.toml -r app cutter3d
.venv/Scripts/python scripts/version.py verificar             # las 3 copias de la versión coinciden
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

### CI, versión y deploy (GitHub Actions)

Los mismos gates corren en `.github/workflows/ci-*.yml`, con la nomenclatura y la estructura de
`api`/`admin`/`tienda` (compuertas reusables por `workflow_call`, acciones de terceros pineadas por
SHA, todo lo que viene del evento pasa por `env:`): **`ci-quality`** (rama `tipo/…`, coherencia de
versión y de Python 3.13, sintaxis py/js/Jinja, ruff, mypy, **frontera motor ↔ web**, actionlint) ·
**`ci-tests`** · **`ci-security`** (gitleaks sobre el historial, pip-audit sobre `requirements.txt`,
bandit, trivy) · **`ci-build`**, el orquestador: compuertas → imagen + `/salud` + trivy →
`ci-release` → `ci-deploy`. Tests, seguridad y el escaneo de la imagen corren además todos los días
a las 08:00 ART. El detalle operativo (qué se configura a mano en GitHub y en Render) está en
`DESPLIEGUE.md` §8.

- **La versión tiene UNA fuente, `app/__init__.py:__version__`**, espejada en `pyproject.toml` y
  `cutter3d/__init__.py` (es la única que viaja en la imagen: el Dockerfile no instala el paquete).
  `scripts/version.py verificar` falla si difieren, y **no se edita a mano**: la sube `ci-release`
  en cada merge a `main` según el tipo de la rama —`feat/` → MENOR, `break/` → MAYOR, el resto →
  PARCHE; MENOR y PARCHE van de 0 a 99 y acarrean—, commitea `chore(release): … [skip ci]`, taggea
  `vX.Y.Z` y **recién entonces** dispara el deploy hook. El número se ve debajo del logo
  (`macros.marca`, global de Jinja `version` en `dependencias.py`) **solo con sesión**: el login no
  lo muestra, por la misma razón que `/salud` no dice la versión.
- **Las ramas se llaman `tipo/descripcion`**, con los tipos de `scripts/version.py tipos` (la
  misma lista que decide el bump: agregar un tipo es tocar un solo dict). `main` recibe solo merges
  por PR con la CI en verde (ruleset de GitHub), y el **Auto-Deploy de Render está apagado**:
  despliega la CI, no el push.
- ⚠ Sin el secret `RELEASE_TOKEN`, `ci-release` pushea con el `GITHUB_TOKEN`, que deja de poder
  apenas `main` quede protegida. El mensaje de error del push dice cuál de los dos falta.
- **`arquitectura` en `ci-quality` es la frontera de abajo, ejecutada en cada PR**: `cutter3d/` no
  importa la web, `app/` no importa las librerías del motor, `import cutter3d` no carga trimesh
  (ciclo 6) e `import app.main` tampoco, y el JS no escribe HTML desde strings.

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
o un `.stl` **que este motor no construyó** y le deriva el `.glb` que F4 fotografía, o lo escribe
en el otro formato de malla (`convertir`). Va aparte de `export.py` porque su entrada no es
confiable: ahí viven las cotas (`MAX_TRIANGULOS`) y la lectura defensiva.

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
- ⚠ **`convertir` (3MF ↔ STL) admite exactamente DOS modificaciones, y las dos las obliga el
  formato.** (1) **La escala a milímetros**: un 3MF declara su unidad en el XML y un STL no tiene
  ninguna, así que pasar un 3MF en pulgadas sin escalar entrega la misma pieza **25,4 veces más
  chica**, sin un solo error a la vista. La unidad sale de una **lista cerrada** —`unit_conversion`
  de trimesh también acepta `"1.21 * meters"`, que el 3MF no permite— y una que no esté **falla**:
  asumir mm sería adivinar. (2) **La unión de cuerpos al ir a STL**, que no sabe contener más de
  uno: las coordenadas no se tocan pero el slicer ya no los separa. Las dos se declaran en el
  reporte, con el número. Lo que **no** hace: reparar, simplificar, reorientar, centrar ni cerrar —
  una malla abierta se convierte igual y se declara abierta. Y como todo lo demás del módulo, la
  equivalencia se prueba **releyendo el archivo escrito**, con una tolerancia que suma un término
  relativo (`TOLERANCIA_RELATIVA`) porque STL guarda en `float32` y su error crece con la
  coordenada — un cortante real de este repo hace el roundtrip bit a bit, pero una pieza de 2 m no.
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
- ⚠ **F1 tiene DOS mitades y solo una es síncrona.** El conversor también pasa mallas entre
  `.3mf` y `.stl`, y eso **no puede correr en el request**: `cutter3d.malla` importa trimesh, y
  trimesh adentro de uvicorn revierte el ciclo 6. Va a un hijo como F2/F3/F4 — la respuesta
  vuelve `procesando` y el JS sondea—, y de paso hereda el techo de RAM y el timeout, que es lo
  que un STL de 20 MB de un desconocido justifica solo. Lo único que el router importa de mallas
  es `paquete3mf`, que cuesta `zipfile`. **Las dos mitades no se cruzan**
  (`archivos.destinos_de`): una malla a JPG sería una foto, y eso es F4; una imagen a 3MF sería
  construir geometría, y eso es F3. El par se valida y el 422 nombra la pantalla que sí lo hace.
  Un par que ya coincide —un `.stl` que los bytes dicen que era 3MF— **se copia tal cual**, igual
  que un SVG pedido como SVG.
- **F2 deja tres archivos y cada uno tiene un consumidor distinto.** `salida.png` (binario puro)
  es lo que se vectoriza y lo que muestra la pantalla; `salida.svg` es lo que acepta el cortante;
  y `editable.jpg` (`ClaveArchivo.JPG_EDITABLE`) es **lo que el usuario se baja**: lo abre en
  Paint, retoca lo que la imagen traía mal, y lo vuelve a subir a Correcto, que acepta solo JPG.
  No se reusa `ClaveArchivo.JPG` porque ese es el del Convertidor, y la regla del enum es que dos
  productores no comparten clave. La copia se re-umbraliza exacta (hay test): no es "la salida en
  JPG", que el contrato prohíbe, es una copia para editar.
- **El trabajo recordado en `sessionStorage` vale solo para la entrada recordada**
  (`trabajoQueSigueVigente` en `app.js`, usado por Cortante y por Correcto). Si la URL trae otro
  `origen` —"seguir" desde un diseño nuevo—, ese trabajo es del diseño anterior y no se retoma. Sin
  esa regla el cortante retomaba el trabajo viejo, `retomarTrabajo` firmaba el estado actual como
  "ya generado" y el botón **Generar quedaba apagado hasta un F5**, que es lo único que borra el
  `sessionStorage`. Y las dos pantallas persisten el estado al restaurar un origen, no solo al
  tocar algo: si no, volver por el menú traía el diseño anterior.
- **F4 post (`/post`) entra por el lado del sólido, no del dibujo.** Recibe mallas ya construidas
  —de este motor o de cualquier otro— y les saca **la misma foto cenital** que F3: la rinde el
  mismo `preview3d.js` sobre un `.glb` derivado. Existe para los cortantes viejos y para cuando la
  foto no se bajó, que es la única forma de recuperarla: el `.glb` de un trabajo se va con él a las
  6 h. No hay parámetros — la geometría ya viene decidida adentro del archivo — y lo único que se
  elige son los colores, que son de pantalla. **El color se elige en la vista previa, sobre la
  pieza**, y la paleta le pega al diseño que se esté mirando (`Ver` lo trae al visor); sin ninguno
  a la vista, que es el estado de antes del lote, les pega a todos. Los rótulos lo dicen, porque un
  control que a veces pega en uno y a veces en todos y no lo aclara es una trampa.
- ⚠ **Mientras la cola maneja el visor, el visor se tapa.** Hay un solo canvas, así que rehacer las
  celdas obliga a pintar cada pieza con los colores del **set** —que no son los de ningún diseño— y
  a devolver después el visor a lo que se estaba mirando. En pantalla ese ida y vuelta se lee como
  si el color del diseño se hubiera cambiado solo y vuelto atrás. El velo (`tapar`) es **sostenido**:
  no lo apaga ningún aviso de carga, porque el lote carga un modelo por diseño y un velo normal
  parpadearía una vez por diseño —y entre parpadeo y parpadeo se vería justo lo que se tapó—. Lo
  suelta el `finally` de quien lo prendió, que es mejor garantía que un reloj. **Lo que NO se tapa**
  es rehacer la foto del diseño que se está mirando: ahí el usuario acaba de elegir ese color y
  taparlo sería esconderle lo que pidió (`laColaTapa`).
- **El lote va en dos fases: primero todas las fotos sueltas, después todas las celdas y la lámina.**
  Son dos entregables distintos —lo que se baja por diseño y la materia prima del set— y separarlos
  deja las descargas listas sin esperar a la lámina; además, una celda que falla ya no marca al
  diseño como fallido, porque su foto suelta está subida y bajable. ⚠ **Cuesta cargar cada modelo
  dos veces**, una por fase, en vez de sacar las dos fotos de una sola carga: el `.glb` se sirve con
  `FileResponse`, así que la segunda vez es una revalidación condicional y el archivo sale de la
  caché — lo que se paga de verdad es parsearlo y volver a subir la geometría a la GPU. Al destapar,
  el visor queda en el **primer** diseño: la lista se revisa desde arriba, y el último es donde
  quedó la máquina, que no es una razón.
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
  desde la suite, y el comportamiento del JS no lo mira ningún gate. Las celdas se achican con
  `Image.draft()` al abrirlas: 25 fotos de 2048 px enteras serían 314 MB de pico contra ~13 MB así,
  que es la misma lección del presupuesto de píxeles de F2.
- ⚠ **Cada diseño se fotografía DOS veces, y son dos entregables distintos.** La foto suelta
  (`ClaveDiseno.JPG_VISTA`) lleva el color de pieza y de fondo que se le eligió a ese diseño; la
  celda (`JPG_SET`) lleva los del **set**, que tiene su propia pareja pieza + fondo. Lo que entra a
  la lámina son las celdas: componerla con las fotos sueltas obligaría a que las 25 compartan color,
  que es justo lo que el set existe para no obligar. La celda es interna
  (`CLAVES_DISENO_INTERNAS`) y no va al ZIP — el set va entero, y media lámina suelta no es nada.
  El color de los huecos llega como **parámetro** de `componer` y es el mismo con el que se
  rindieron las celdas: leerlo de la esquina de la primera foto —como se hizo un ciclo— dejó de
  poder ser cuando cada diseño eligió su propio fondo, porque ahí "el fondo de las fotos" no es un
  color sino hasta veinticinco.
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
- **El hueco entre filas es más chico que el de entre columnas** (`SEPARACION_FILAS_REL`, un 10%
  menos: 37 px contra 41). No es geometría sino cómo se lee: con celdas cuadradas y piezas más
  anchas que altas, entre dos filas hay el hueco **más** el fondo de arriba y de abajo de cada foto,
  así que a igual cantidad de píxeles el aire vertical se ve mayor. La lámina sigue siendo cuadrada
  y el bloque centrado — lo que el hueco más chico libera se reparte arriba y abajo.
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
- ⚠ **El mapa de sombra es VSM y NO se re-rinde solo (`shadow.autoUpdate = false`). Las dos cosas
  van juntas.** VSM es lo que permite que el ancho de la penumbra sea un parámetro
  (`PENUMBRA_REL`) en vez de una consecuencia del tamaño del texel — con filtrado PCF,
  `shadow.radius` lo ignora el sombreador de three—, pero cuesta dos pasadas de desenfoque sobre
  2048×2048. En el visor, que anima, eso corriendo por frame es impagable; y no hace falta, porque
  la luz está fija y la pieza tampoco se mueve: lo que gira es la cámara. El re-render lo pide
  `ajustarPenumbra`, que es el único lugar que lo hace y al que las dos vistas llaman al encuadrar.
  **Volver a poner `autoUpdate = true` "porque la sombra se ve vieja" es tratar el síntoma
  equivocado**: lo que falta en ese caso es un `needsUpdate` donde cambió la pieza.

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
  ⚠ **Se puentea por regla, no por síntoma.** `base` es el cierre morfológico de la silueta con radio
  `o2 + distancia/2` —o sea *todo* lo que el complemento tiene más angosto que `2·o2 + distancia`— y de
  ahí se rellena lo que **entra en la banda del filo** (la zona que no queda contenida en `o1`); lo que
  cae entero adentro de la luz se deja, porque el filo nunca lo pisa y rellenarlo solo inflaría el área
  que el reporte declara como no cortada. Eso garantiza las dos cosas que el cortante necesita para
  existir: **filo de `filo_ancho_mm` en todo su recorrido** y **una sola pieza**. Sembrar en cambio con
  síntomas —bolsillos ciegos del filo + el material que agrega forzar la colisión, que es como estaba—
  no cubre el caso más común, que es el de dos paredes de `o2` que **ya se tocan** sin encerrar nada: un
  brazo que roza el cuerpo. Ahí el filo bajaba a la garganta con un alma de menos de 1 mm y, si `o1`
  llegaba a cerrar la boca, dejaba además la cámara de adentro como un **cuerpo suelto** — un pedazo de
  filo de 10 mm flotando, sin nada que lo sujete. Lo verifican `cuerpos_sueltos()` (guarda dura en
  `construir_cortador_2d`, que levanta `CuerpoSueltoEnCortador`) y, en los tests,
  `medir_ancho_trazo(c.filo)` contra los percentiles del `circulo`, que es el filo sano de referencia.
- **Nada de fallbacks silenciosos.** Falla duro lo que produciría un archivo inválido (parámetro fuera
  de rango, SVG ilegible, escala que no converge, booleana que no cierra, malla no manifold) y no se
  escribe nada. **Advierte** en el reporte lo que produce un archivo válido pero difícil de imprimir.
- **Las dos modificaciones del arte viven SOLO en F2** (`raster.py`) y las dos declaran siempre
  cuánto tocaron. **F3 nunca altera el arte.**
  - El **contorneado de zonas macizas** viene encendido, y declara cuántas zonas y qué área.
  - La **normalización de ancho de trazo** viene apagada: deja todos los trazos al mismo ancho
    reconstruyéndolos desde su eje medial, y declara cuántos píxeles engrosó, cuántos afinó y
    cuántos de zona maciza dejó intactos. Es lo único del proyecto que **afina** un trazo — la
    dilatación de F3 solo engorda—, y por eso resuelve el caso que F3 no puede: un contorno más
    grueso que el detalle interior. ⚠ Es también lo único de F2 que necesita saber la escala
    física, y la deduce de que F3 va a escalar el dibujo a `lado_mayor_mm`: si después se genera el
    cortante con otro tamaño, la calibración quedó para otra pieza. Por eso el resultado declara
    los dos milímetros que supuso.
    ⚠ **Es lo único de F2 que AMPLÍA la imagen, y sin eso la línea sale temblorosa.** Un JPG
    de 339 px trae el trazo objetivo en 3,5 px, y reconstruir eso desde un eje de 1 px deja el
    centro y el ancho clavados a la grilla con medio píxel de error (±17%): se ve como una línea
    que ondula, y la impresora vibra siguiéndola. Con la normalización encendida la etapa amplía
    los **grises** por un factor entero hasta que el objetivo mida
    `ANCHO_MINIMO_NORMALIZACION_PX` (16 px), acotado por el mismo `MAX_PIXELES_TRABAJO` de la
    reducción, y **entrega el PNG a esa escala** (`factor_ampliacion`, `tamano_usado`). Después
    vienen los tres pasos de la reconstrucción —podar el eje, reconstruir, limar— que sacan las
    espigas y el dentado del disco discreto. Medido en el trazo final del marcador, p5→p95 en mm:
    buzz 0,51→2,06 sin normalizar, **0,92→1,14** normalizado; calabaza 0,30→1,24 → **0,89→1,10**;
    murciélago 0,40→1,25 → **0,89→1,35**. El camino sin normalizar no cambia de escala.
- **Nombres y comentarios en español**, incluidas las excepciones (por eso `N818` está apagado en
  ruff). **Nada de `innerHTML`** con datos del servidor o del usuario en el JS.

## Trampas ya pagadas (las más caras)

El listado completo está en `.claude/project-spec.md` → "Hallazgos de librerías". Las que más
probablemente vuelvan a morder:

1. **`skeletonize` de scikit-image 0.26 segfaultea** con la máscara del fixture del círculo. No es el
   tamaño (un disco sintético idéntico pasa). Se usa **`medial_axis`**.
   ⚠ Y **`medial_axis` no es determinista si no se le pasa `rng`**: desempata al azar el orden de los
   píxeles de tinta. Medido sobre `tests/buzz-lightyear.jpg`, cuatro corridas dieron ejes de 3286,
   3289, 3288 y 3287 px, y `area_engrosada_px` salió 227, 224 y 224 en tres corridas del mismo
   archivo. En un proyecto cuyo punto es *afirmar* cuánto se modificó el dibujo, una cifra que se
   mueve sola no afirma nada. Va `rng=SEMILLA_EJE` en las **dos** llamadas de `raster.py`.
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
   ⚠ **Y al AMPLIAR para normalizar, el nivel de corte NO es el de Otsu.** `corte` responde *qué*
   píxel es tinta; al interpolar los grises la pregunta es *por dónde pasa el borde*, y la rampa
   entre un píxel de tinta y uno de papel lo cruza en el **punto medio de sus niveles** — ahí está
   el borde de la máscara nativa. Sobre un line art puro guardado como JPG, Otsu cae en **3** (el
   histograma es 0 y 255 y casi nada en el medio) y cortar ahí corría el borde medio píxel hacia
   adentro: las líneas de 2/4/6 px salían 1/3/5, y el detector de macizos, calibrado con esa
   mediana achicada, clasificaba mal (14 zonas donde había 0). Sobre una foto real Otsu ya está
   en el medio (139 contra 139 en buzz) y no se nota — por eso lo agarró el fixture sintético y
   no el dibujo real. Va **bicúbico**, no Lanczos: sus lóbulos negativos dejan un halo alrededor
   de cada línea (dispersión 0,36 contra 0,06 en la onda).
10. **`distance_transform_edt` sin un solo cero NO falla: devuelve la distancia a un punto fantasma
    pegado a la esquina superior izquierda** (medido: 1, 1,41, 2,24… desde (0,0)). Toda la
    morfología por disco de F2 —la apertura del detector de macizos, la erosión del anillo, la
    reconstrucción del trazo— va por transformada de distancia y no por footprint, porque el radio
    crece con la grilla ampliada y con footprint el contorneado se llevaba **4,1 de los 5,1 s** de
    buzz a ×5 (hoy 1,3 s en total). Pero en un line art puro la erosión no deja nada, y dilatar esa
    máscara vacía con la transformada cruda pintaba un cuarto de disco "macizo" en la esquina → un
    arquito de tinta inexistente → la caja de la tinta corrida → la escala equivocada. Costó seis
    tests. Por eso `_distancia_a` es el **único** que la llama, y devuelve infinito sin ningún True.
11. **Un cortador partido en islas cumple su propio número de Euler.** `euler_esperado_de` se calcula
    sobre la huella que se extruyó de verdad —y tiene que ser así, por las ventanas del pie—, así que
    un pie de 3 piezas con 1 hueco espera 4 y la malla mide 4: watertight ✓, euler ✓, **VERIFICADO**,
    con dos pedazos de filo de 10 mm flotando adentro. La verificación se validaba a sí misma. Es el
    único punto ciego encontrado hasta ahora en esa batería y no se tapa con otro número topológico
    —ninguno lo distingue—: lo tapa `cuerpos_sueltos()`, que pregunta otra cosa (¿esta pieza rodea
    galletita?) y falla duro antes de extruir. La lección general: un invariante derivado de la
    geometría que se quiere probar no prueba nada; el contraste tiene que venir de afuera.

## Red de regresión

`tests/test_fidelidad.py` es el golden master del motor y **sus asserts numéricos SON la referencia**:
no se versionan `.3mf` binarios, que cambiarían con cada versión de manifold3d sin que cambie nada
real. Los `tests/test_web_*.py` cubren la capa web con `TestClient` y `dependency_overrides` — ningún
test toca `trabajo/`, ninguno depende de que `credenciales.json` exista, y la contraseña de prueba se
genera al vuelo (no hay una sola credencial literal en el repo).

La normalización de trazo se cubre en `tests/test_raster.py` con un fixture de **tres líneas de 2,
4 y 6 px** cuyos números no son arbitrarios: la mediana da 4 px, así que el detector de macizos abre
con un disco de radio 3 y ninguna de las tres califica —el caso queda limpio de contorneado—, y con
`lado_mayor_mm=52` el objetivo de 1 mm cae exactamente en 5 px, justo en el medio de las tres. Así
una sola imagen prueba las dos direcciones. El que más vale es
`test_f2_normalizar_engorda_y_tambien_afina`: afinar es lo único que F3 no sabe hacer, y un cambio
que dejara `area_afinada_px` en cero pasaría igual mirando solo la mediana.
`test_f2_normalizar_no_deja_el_dibujo_en_un_pixel` fija el redondeo del medio ancho —truncar dejaba
todo el arte en 1 px— y `test_la_normalizacion_de_trazo_cruza_la_frontera_de_procesos` (marcado
`lento`) es lo único que prueba que el flag y los dos milímetros llegan al hijo: en el medio viajan
posicionales dentro de una tupla, así que agregar un parámetro en el router sin tocar
`ejecutar_lineas` no rompe ningún tipo — corre, y normaliza con el número equivocado.

⚠ **Con la normalización encendida el resultado viene en la grilla ampliada**, así que todo test
que lea coordenadas o cuente píxeles tiene que escalar por `factor_ampliacion` (las franjas y la
columna de `_ancho_de_linea`, la ventana de la espiga ×k y su cuenta ÷k², las etiquetas de piezas
con `np.kron`). Ya pasó: dos tests de la onda leían una ventana nativa sobre una imagen ×3, caían en
otra parte del dibujo y **pasaban mirando cualquier cosa**. El fixture de las tres líneas amplía
×4 (16 / 5 → 4) y entrega a 1280×960; el logrado ahí es 21 px, 5,25 originales.

El **ruido** de la reconstrucción tiene su propio fixture y no podía compartir el anterior: las tres
líneas son rectas horizontales, y el dentado aparece justo donde ellas no tienen nada. Es una **onda
de grosor variable** con una espiga pegada y un puntito suelto, a `lado_mayor_mm=38` (objetivo 6,9
px nativos, ×3 → 20,7, radio 10). Cada pieza del arreglo tiene un test que falla si se la saca, y
está verificado desarmándolas de a una: la **ampliación** la agarra
`test_f2_normalizar_deja_el_ancho_parejo_a_lo_largo_del_trazo` (dispersión `(p95-p5)/p50` del
ancho: 0,37 en la tinta, 0,14 reconstruyendo en la grilla nativa, **0,056** ampliando; tope 0,10) y
`..._un_dibujo_chico_se_amplia_hasta_que_el_trazo_tenga_cuerpo`; `_limar` lo agarran
`..._no_deja_el_trazo_mas_dentado_que_el_dibujo` (4 dientes contra 16) y
`..._no_deja_tramos_mas_finos_que_los_que_recibio` (sin limar el p5 cae a 2,2 px nativos con un
objetivo de 6,9 — más fino que la entrada, que es el defecto original sin corregir);
`_podar_espigas` lo agarra `..._no_convierte_una_espiga_en_un_bulto` (16,8 px contra 31,6 en la
ventana); su **rescate** de piezas lo agarra `..._no_se_come_las_piezas_mas_chicas_que_la_poda`, el
único que falla si se poda sin rescatar; la semilla, `..._da_lo_mismo_en_dos_corridas`; y el nivel
de corte del punto medio lo agarra el fixture de las tres líneas (con Otsu salen 1/3/5 y ya no son
iguales). El presupuesto lo fija `test_f2_la_ampliacion_se_queda_corta_antes_que_pasar_el_presupuesto`.
Y `test_el_contorneado_es_morfologia_por_disco_exacta_en_tiempo_lineal` fija que la morfología por
transformada de distancia dé **pixel a pixel** lo mismo que `opening`/`erosion` con `disk(r)`,
incluidos los dos casos degenerados (sin manchas, y una mancha que lo cubre todo) — que son justo
los que la transformada cruda resuelve mal (trampa 10).

⚠ Los tests de dientes y de tramos finos comparan contra la **propia entrada** y no contra una
constante, a propósito: lo que hay que sostener es que la etapa no ensucia el dibujo, y eso no
depende de cuánto ruido traiga. El proxy es `_dientes` (píxeles de tinta con 5+ vecinos de fondo)
sobre el raster y no los nodos del SVG, para no atar la suite a la versión de vtracer.

`tests/test_malla.py` hace lo propio con F4 **y con la conversión entre formatos**. De esta última
el que más vale es `test_un_3mf_en_pulgadas_sale_en_milimetros_y_lo_declara`: es el único caso donde
"no cambiar las medidas" se rompe en silencio y el usuario lo descubre recién con la galletita en la
mano. Los de vértices (`..._no_mueve_un_solo_vertice`, `..._float32_permite`) comparan **vértice a
vértice y no por la caja**, porque una pieza espejada o rotada 90° tiene la misma caja. Sus dos tests
centrales de F4:
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
