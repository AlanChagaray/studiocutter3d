# Spec Base — studioCutter3D

> Generado por la skill `inspect` del workflow ViaBariloche. Es la referencia de contexto
> del proyecto entre sesiones: mientras esté vigente, `inspect` NO re-analiza el stack desde
> cero. Se regenera con aprobación cuando cambian las señales de frescura de abajo.

**Última actualización:** 2026-09-10
**Versión de plantilla:** 3

> Actualizado al cerrar el **ciclo 1** (motor de geometría). Lo que antes era una previsión
> ahora está en disco y verificado: 24 archivos, 105 tests en verde, los 4 gates limpios.

## Señales de frescura
- **Manifests:** `pyproject.toml` (existe en la raíz)
- **Versión lenguaje/framework:** Python 3.13.7 (`requires-python = ">=3.13"`), sin framework en el motor
- **Dependencias clave (con versión instalada):** numpy 2.5.3 · scipy 1.18.1 · shapely 2.1.2 ·
  trimesh 5.1.0 · manifold3d 3.5.3 · mapbox-earcut 2.1.0 · scikit-image 0.26.0 · pillow 12.3.0 ·
  svgelements 1.9.6 · vtracer 0.6.15 · **resvg-py 0.5.0** · lxml 6.1.3
- **Lockfiles presentes:** ninguno (pip + venv, sin lock)
- **Configs de gates presentes:** ninguna suelta — todas dentro de `pyproject.toml`
  (`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`, `[tool.pytest.ini_options]`, `[tool.bandit]`)

## Stack
- **Lenguaje:** Python 3.13.7 (invocado como `py -3.13`; ⚠ `python` no está en PATH en esta máquina)
- **Framework(s):** ninguno en el motor. Ciclo 2: FastAPI + Jinja2 + Three.js
- **Versión runtime/engine:** CPython 3.13.7, venv en `.venv/`, pip 25.2

## Arquitectura
- **Patrón / capas:** librería de dominio pura (`cutter3d/`) + capa web separada (`app/`, ciclo 2).
  El motor no importa nada de la web (verificado: `grep -rE "fastapi|uvicorn|jinja2" cutter3d/` sin
  resultados); la web orquestará al motor llamando a `generar()`.
- **Entrypoints:** CLI `python -m cutter3d` (`cutter3d/cli.py`) con 4 subcomandos, y la API pública
  `cutter3d.generar()` que el ciclo 2 va a llamar desde un `ProcessPoolExecutor`.
- **Flujo de un caso de uso típico (implementado, no previsto):**
  `cli.py` parsea args → `params.py` valida los 9 rangos → `svg_io.cargar_svg` SVG→polígonos
  (transforms, XOR even-odd, flatten 0,05 mm, espejado Y, centrado) →
  `geometry.construir_marcador_2d` bucle escala↔dilatación midiendo con `measure.medir_ancho_trazo`
  (converge en 2 iteraciones con los fixtures) → silueta con huecos tapados →
  `geometry.construir_cortador_2d` offsets o1/o2/o3 `join_style=round` →
  `solids.construir_escena` extrusión + booleanas `engine="manifold"` →
  `export.exportar` `.3mf` + `.glb` + 2 `.stl` con escritura atómica →
  `verify.verificar` **relee el `.3mf` del disco** y emite `ReporteFidelidad`
- **Dónde vive qué:** entrada/validación `params.py` · excepciones `errors.py` · parseo `svg_io.py` ·
  medición `measure.py` · geometría 2D `geometry.py` · sólidos 3D `solids.py` ·
  serialización `export.py` · verificación `verify.py` · imagen `raster.py` ·
  vectorización `vector.py` · CLI `cli.py` · API pública `__init__.py`
- **Acceso a datos:** ninguno en el motor (todo en memoria, entrada y salida por archivo).
  Ciclo 2: SQLite vía SQLAlchemy solo para usuarios
- **Auth y autorización:** no aplica al motor. Ciclo 2: usuario+contraseña con argon2 y sesión por cookie
- **Manejo de errores y logging:** jerarquía propia en `errors.py` (`Cutter3DError` como base).
  **Nada de fallbacks silenciosos**: falla duro lo que produciría un archivo inválido
  (`ParametroFueraDeRango`, `SvgInvalido`, `NoConvergeError`, `BooleanaFallida`, `MallaNoManifold`),
  y advierte en el reporte lo que produce un archivo válido pero difícil de imprimir
- **Integraciones externas:** ninguna. Todo corre local y offline

### Tres hallazgos de librerías que costaron encontrar — no volver a descubrirlos
1. **`skeletonize` de scikit-image 0.26 segfaultea** con la máscara rasterizada del fixture del
   círculo (1812×1812, área maciza), incluso sin llamada previa a la transformada de distancia.
   **No es el tamaño**: un disco sintético de idéntico tamaño, dtype, flags y área pasa sin problema.
   Se usa **`medial_axis`**, que además devuelve eje y distancia en una sola pasada.
2. **`Path3D.to_planar()` de trimesh exige `rtree`**, que no está instalado. Las áreas de sección se
   calculan armando los anillos con shapely y combinándolos con XOR — sin sumar la dependencia.
3. **`pypotrace` no tiene wheel de Windows.** La vectorización va con **vtracer**, y
   `hierarchical="cutout"` es **obligatorio**: su default `"stacked"` apila formas en vez de generar
   huecos, y el marcador saldría con todos los huecos internos rellenos.

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
| **resvg-py** | **0.5.0** | **Rasterizado SVG→PNG para el conversor a JPG** (Rust, sin Cairo del sistema) |
| pillow | 12.3.0 | F1/F2 — conversión y binarización de imagen; rasterizado de polígonos |
| lxml | 6.1.3 | Lectura del XML interno del `.3mf` |

Dev: pytest 9.1.1 · ruff 0.16.6 · mypy 2.3.1 · bandit 1.9.4

## Archivos de configuración (chore)
| Archivo | Rol |
|---|---|
| `pyproject.toml` | Metadata, 12 deps + 4 dev, y config de ruff / mypy / pytest / bandit |
| `.gitignore` | `.venv/`, `__pycache__/`, `out/`, `*.3mf`, `*.stl`, `*.glb`, `bash.exe.stackdump` |
| `prompt_cortante.md` | **Contrato funcional** del motor — no es documentación decorativa |

## Docker / contenedores
- **Dockerfile:** no
- **docker-compose:** no aplica (Docker existe en la máquina pero el proyecto no lo usa)

## Comandos
| Acción | Comando |
|---|---|
| Setup | `py -3.13 -m venv .venv` y `.venv/Scripts/python -m pip install -e ".[dev]"` |
| Build | no aplica (librería pura) |
| Test | `.venv/Scripts/python -m pytest -q` → **105 passed** |
| Lint | `.venv/Scripts/python -m ruff check cutter3d tests` |
| Dev / run | `.venv/Scripts/python -m cutter3d --svg <arte.svg> --modo cortante+marcador --out <salida.3mf> --reporte` |

El CLI tiene 4 subcomandos: **`cortante`** (default — si el primer argumento no es un subcomando
conocido, se asume), **`jpg`** (png/jfif/webp/svg → jpg), **`lineas`** (jpg → B/N puro) y
**`vectorizar`** (imagen → svg). La instalación registra además el comando `cutter3d`.

## Gates de calidad
| Gate | Comando | Alcance | Umbral / baseline |
|---|---|---|---|
| Tipos | `.venv/Scripts/python -m mypy` | `cutter3d/` (por `files` en config) | `strict = true` · **sin baseline** |
| Estilo | `.venv/Scripts/python -m ruff check` | archivos del diff | 0 desvíos |
| Formato | `.venv/Scripts/python -m ruff format --check` | archivos del diff | 0 desvíos |
| Complejidad | reglas `C901` / `PLR` de ruff | archivos del diff | ciclomática ≤ 10 |
| SAST | `.venv/Scripts/python -m bandit -r cutter3d -q` | `cutter3d/` | 0 hallazgos medio/alto |
| Secretos | **ninguno disponible** | — | ⚠ sin cobertura |

**Notas — dos excepciones documentadas, no deuda escondida:**

- **`disallow_untyped_calls = false`** es la única perilla de `strict` que se afloja. Motivo:
  **skimage y trimesh SÍ traen `py.typed`** pero exponen funciones sin anotar (`medial_axis`, `disk`,
  `threshold_otsu`, `PBRMaterial`, `TextureVisuals`, `Scene.export`). Como el módulo se resuelve,
  `ignore_missing_imports` no las alcanza y la alternativa sería sembrar `# type: ignore` por todo el
  código propio — peor, porque también esconde los errores reales. Todo el resto de `strict` sigue
  activo.
- **`N818` desactivado** en ruff: las excepciones tienen nombre en español (`SvgInvalido`,
  `MallaNoManifold`, `BooleanaFallida`) y el sufijo `Error` inglés las volvería híbridos ilegibles.
- **Sin baseline, y corresponde**: el proyecto nació en el ciclo 1 con la deuda en cero. Cualquier
  hallazgo de gate es del ciclo que lo produjo.
- **Falta el gate de secretos** — `gitleaks` no está instalado. Impacto bajo mientras el motor no maneje
  credenciales, pero **hay que resolverlo antes del ciclo 2**, que suma login, hash de contraseñas y
  sesiones: `winget install gitleaks`. `gate` nunca instala nada — es una decisión de proyecto.

## Red de regresión
- **Estado:** `caracterización` (+ `unit` para los módulos de imagen y de parámetros)
- **Ubicación:** `tests/test_fidelidad.py` con los fixtures fijos de `tests/fixtures/`
- **Cómo se corre:** `.venv/Scripts/python -m pytest tests/test_fidelidad.py -q`
- **Áreas cubiertas:** el pipeline de geometría completo (`svg_io → geometry → solids → export`),
  verificado **releyendo el `.3mf` exportado**, no la malla en memoria. Los asserts numéricos SON el
  golden master: no se versionan `.3mf` binarios, que cambiarían con cada versión de manifold3d sin que
  cambie nada real.
- **Sin cobertura:** `cli.py`, `__main__.py` y el manejo de errores de E/S. La cadena
  argparse → motor está verificada por lectura y corridas manuales, no por un gate: un typo futuro en un
  `dest` de argparse no lo detectaría nada. `review` ratificó esta declaración al cerrar el ciclo 1.

## Estructura del proyecto (alto nivel)

```
studiocutter3d/
├─ pyproject.toml
├─ prompt_cortante.md      ← contrato de geometría
├─ cutter3d/               ← motor puro (13 archivos, sin dependencias web)
│  ├─ params.py  errors.py  svg_io.py  measure.py
│  ├─ geometry.py  solids.py  export.py  verify.py
│  ├─ raster.py  vector.py
│  └─ cli.py  __main__.py  __init__.py
├─ tests/                  ← 105 tests
│  ├─ test_params.py  test_svg_io.py  test_geometry.py
│  ├─ test_fidelidad.py    ← red de caracterización
│  ├─ test_raster.py
│  └─ fixtures/            ← circulo · estrella · lineart_ojos_llenos
└─ app/                    ← ciclo 2 (FastAPI + UI), todavía no existe
```

## Convenciones y restricciones
- **`prompt_cortante.md` manda sobre la geometría.** Sus números y su regla de fidelidad se
  transcriben literalmente; no se reinterpretan ni se "mejoran".
- **Regla de fidelidad:** la única modificación permitida al arte es la dilatación uniforme (más la
  simplificación de 0,02 mm que el propio contrato ordena). Prohibido: cierres morfológicos, rellenos
  entre trazos, macizo→contorno, suavizado, marcos. Los efectos colaterales (muescas selladas) se
  **miden y reportan**, no se compensan.
- **Límites de parámetros:** ningún valor `<= 0`, ninguno `> 1000 mm`. Se valida en `params.py`,
  en un solo lugar.
- **Nada de fallbacks silenciosos.** Falla duro lo que produciría un archivo inválido; advierte en el
  reporte lo que produce un archivo válido pero difícil de imprimir.
- **La verificación relee el archivo exportado**, nunca la malla en memoria — es la única forma de
  probar que lo que se entrega es lo que se validó.
- **Los offsets del cortador se derivan de los parámetros**, no se hardcodean: `o1 = luz`,
  `o2 = luz + filo_ancho`, `o3 = luz + filo_ancho + pie_ancho_extra`. Lo mismo vale para las alturas de
  muestreo de las secciones, que se derivan de `pie_alto` y `filo_alto`.
- **El contorneado de macizos vive SOLO en F2** (`raster.py`), viene encendido por default por decisión
  del usuario, y **siempre declara** cuántas zonas tocó y qué área. El motor de cortante (F3) nunca
  altera el arte.
- **La salida de F2 va en PNG, nunca en JPG**: la compresión volvería a meter grises en el borde.
- Todo comando de Python usa `py -3.13` o `.venv/Scripts/python`: **`python` no existe en el PATH
  de esta máquina**.
- Los heredocs de bash dieron problemas en este entorno: para crear o editar archivos, usar las tools
  de escritura, no `cat > archivo <<EOF`.
