# studioCutter3D

Convierte line art en **cortantes de galletitas** imprimibles en 3D — con su marcador
opcional — y **prueba numéricamente que no alteró el dibujo**.

Esa última parte es el punto del proyecto. Cualquiera puede engordar un trazo y extruirlo;
lo difícil es demostrar que el resultado sigue siendo el mismo dibujo. El motor mide lo que
hizo y lo reporta: cuántos contornos entraron y cuántos salieron, cuánto se movió cada
contorno, qué muescas selló la dilatación y cuánta luz real quedó entre el marcador y el
cortador.

El contrato de geometría es [`prompt_cortante.md`](prompt_cortante.md). Sus números y su
regla de fidelidad se transcriben literalmente: no se reinterpretan ni se "mejoran".

## Estado

**Ciclo 1 — motor headless.** La librería `cutter3d/` está completa y verificada.
La capa web (FastAPI + login + preview 3D en el navegador) es el ciclo 2 y todavía no existe;
el diseño de esa UI ya está definido y aprobado.

## Instalación

⚠ En esta máquina `python` **no está en el PATH** (solo el alias del Microsoft Store) y el
default del launcher es 3.14. Por eso todos los comandos van con la versión explícita.

```bash
py -3.13 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
```

Las 12 dependencias instalan como wheels precompiladas en Windows: no hace falta compilador.

La instalación registra además el comando `cutter3d`, equivalente a `python -m cutter3d`:
`.venv/Scripts/cutter3d --help`. Los ejemplos de abajo usan la forma `python -m` para que
funcionen sin activar el venv.

## Uso

### El módulo estrella — cortante y marcador

```bash
# Una forma simple: solo el cortante
.venv/Scripts/python -m cutter3d --svg tests/fixtures/circulo.svg \
    --modo cortante --out out/circulo.3mf

# Line art completo: cortante + marcador, con STL y reporte de fidelidad
.venv/Scripts/python -m cutter3d --svg tests/fixtures/lineart_ojos_llenos.svg \
    --modo cortante+marcador --out out/lineart.3mf --stl --reporte
```

Salidas: el `.3mf` (el formato bueno — milímetros y dos objetos nombrados), un `.glb` con
materiales PBR para el preview 3D, y opcionalmente dos `.stl` separados.

> **Por qué dos STL y no uno.** STL no tiene nombres de objeto ni unidades. Metidos en un
> mismo archivo, el slicer ve un único cuerpo de dos cáscaras y no se pueden mover por
> separado. El `.3mf` sigue siendo el formato recomendado; el STL es compatibilidad.

Los 9 parámetros son editables y ninguno puede ser ≤ 0 ni mayor a 1000 mm:

| Parámetro | Flag | Default |
|---|---|---|
| Lado mayor del marcador | `--lado-mayor` | 90 mm |
| Altura de la base | `--altura-base` | 1 mm |
| Altura de los trazos | `--altura-trazos` | 3 mm |
| Ancho de trazo objetivo | `--ancho-trazo` | 1 mm |
| Luz marcador ↔ cortador | `--luz` | 0,7 mm |
| Filo — ancho | `--filo-ancho` | 1 mm |
| Filo — alto | `--filo-alto` | 10 mm |
| Pie — ancho extra | `--pie-ancho-extra` | 1,8 mm |
| Pie — alto | `--pie-alto` | 2 mm |

Los offsets del cortador se **derivan** de esos valores (`o1 = luz`, `o2 = luz + filo`,
`o3 = luz + filo + pie`), así que cambiar el filo mantiene la geometría coherente en vez de
romperla contra constantes sueltas.

### Las otras tres etapas

```bash
# Conversión a JPG: acepta png, jfif, webp y svg
.venv/Scripts/python -m cutter3d jpg dibujo.webp -o dibujo.jpg

# Corrección de líneas: blanco y negro puro, sin grises
.venv/Scripts/python -m cutter3d lineas dibujo.jpg -o lineas.png

# Vectorización para alimentar el módulo de cortante
.venv/Scripts/python -m cutter3d vectorizar lineas.png -o arte.svg
```

> **El contorneado de zonas macizas viene ENCENDIDO.** Es la única etapa del flujo que
> modifica el arte: las manchas rellenas (ojos, por ejemplo) pasan a ser solo su contorno.
> Por eso **siempre declara cuántas zonas tocó y qué área**. Se apaga con
> `--no-contornear-macizos`. El módulo de cortante nunca hace esto: reproduce fiel lo que
> reciba.

### Desde Python

```python
from pathlib import Path
from cutter3d import CutterParams, Modo, generar, render_texto

resultado = generar(
    svg=Path("arte.svg"),
    modo=Modo.CORTANTE_MARCADOR,
    salida=Path("out/arte.3mf"),
    params=CutterParams(lado_mayor_mm=120.0),
    con_stl=True,
)
print(render_texto(resultado.reporte))
```

## Qué verifica el reporte

Todo se mide **releyendo el `.3mf` del disco**, no la malla en memoria: es la única forma de
probar que lo que se entrega es lo que se validó.

- Contornos exteriores y huecos: original escalado contra final, tienen que dar igual.
- Contención íntegra: el área de `original − final` tiene que ser 0.
- Desvío del contorno (p50 / p99 / máximo) acotado por la dilatación aplicada.
- Muescas selladas por la dilatación, en mm y en porcentaje. Se reportan, no se compensan.
- Percentiles del ancho de trazo y de los huecos entre trazos.
- Luz mínima real entre marcador y cortador.
- Secciones del cortador a z=1, z=5 y z=9,5, medidas **sobre la malla** y comparadas contra
  las áreas calculadas desde los polígonos 2D — dos caminos independientes.
- `is_watertight` y número de Euler: **2 para el marcador** (sin agujeros pasantes) y
  **0 para el cortador** (anillo cerrado).

El reporte cierra con una sección **"qué NO prueba"**. La luz mínima se mide en 2D sobre los
polígonos que efectivamente se extruyeron: demuestra que la simplificación no se comió la
separación, que es el riesgo real, pero no es distancia superficie-a-superficie entre las dos
mallas.

## Política de fallos

- **Falla duro y no escribe nada** lo que produciría un archivo inválido: parámetro fuera de
  rango, SVG que no se puede interpretar, escala que no converge, booleana que no cierra,
  malla que no queda manifold.
- **Advierte en el reporte** lo que produce un archivo válido pero difícil de imprimir:
  huecos finos, trazo más grueso que el objetivo, muescas selladas.

Nada de fallbacks silenciosos.

## Desarrollo

```bash
.venv/Scripts/python -m pytest -q          # suite completa
.venv/Scripts/python -m ruff check .       # lint + complejidad
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy               # tipos, strict
.venv/Scripts/python -m bandit -r cutter3d -q
```

La red de regresión es `tests/test_fidelidad.py`: sus asserts numéricos **son** el golden
master. No se versionan `.3mf` binarios, que cambiarían con cada versión de manifold3d sin
que cambie nada real.

## Notas de implementación

Tres cosas que costaron encontrar y conviene no volver a descubrir:

- **`pypotrace` no tiene wheel de Windows.** La vectorización va con `vtracer`, y con
  `hierarchical="cutout"` obligatorio: su default apila formas en vez de generar huecos.
- **`skeletonize` de scikit-image 0.26 segfaultea** con la máscara rasterizada del fixture
  del círculo. Un disco sintético del mismo tamaño y área pasa sin problema, así que no es el
  tamaño. Se usa `medial_axis`, que además devuelve el eje y la distancia en una sola pasada.
- **`Path3D.to_planar()` de trimesh exige `rtree`.** Las áreas de sección se calculan armando
  los anillos con shapely y combinándolos con XOR, sin sumar la dependencia.
