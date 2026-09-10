Adjunto un SVG de line art (trazado con potrace: paths rellenos en negro, sin
stroke). Necesito un archivo **.3mf** listo para Ultimaker Cura con un cortante
de galletitas y su marcador.

## Regla de fidelidad — manda sobre todo lo demás

El marcador tiene que reproducir el dibujo tal cual es. **No cambies el diseño,
no agregues líneas y no quites líneas.** En concreto:

- Nada de cierres morfológicos ni rellenos entre trazos.
- Nada de convertir áreas macizas a contorno. Si el dibujo tiene los ojos
  llenos, los ojos van llenos.
- Nada de suavizar, redibujar, "limpiar" ni reinterpretar formas.
- Nada de agregar marcos, bordes ni detalles que no estén en el arte.

La **única** modificación permitida sobre el arte es la dilatación uniforme para
llegar al ancho de trazo objetivo, y solo si el trazo viene más fino que eso.
Si esa dilatación sella alguna muesca más angosta que el doble de la dilatación,
es un efecto colateral inevitable: avisame cuánto contorno quedó afectado, no lo
compenses inventando geometría.

Si te parece que algo del dibujo no va a imprimir bien, decímelo en la
verificación con números. No lo arregles por tu cuenta.

## Parámetros

| Parámetro | Valor |
|---|---|
| Tamaño del marcador, lado mayor | 90 mm |
| Altura de la base del marcador | 1 mm, maciza |
| Altura de los trazos sobre la base | 3 mm (altura total 4 mm) |
| Ancho de trazo objetivo | 1 mm |
| Luz entre marcador y cortador | 0,7 mm |
| Filo del cortador | 1 mm de ancho × 10 mm de alto |
| Pie del cortador | +1,8 mm de ancho × 2 mm de alto |
| Ancho total del cortador | 2,8 mm desde la luz de 0,7 mm |

## Cómo construir el marcador

1. Parseá el SVG a polígonos, aplicando las transformaciones del archivo.
   Combiná los subpaths con XOR (even-odd) para que los contornos internos
   queden como huecos. Aplaná las curvas fino (~0,05 mm en escala final) para no
   perder detalle.
2. Espejá el eje Y (el SVG es y-abajo) y centrá el dibujo en el origen.
3. Escalá para que el lado mayor mida lo indicado en los parámetros. **Medí
   sobre la silueta final, después de engrosar**, no sobre el arte crudo. Como
   la dilatación se define en mm y depende de la escala, iterá hasta que
   converja.
4. Medí el ancho real del trazo con transformada de distancia sobre el
   esqueleto, podando las puntas espurias del esqueleto (~0,5 mm) para que las
   esquinas no ensucien los percentiles bajos. Si la mediana quedó por debajo
   del ancho objetivo, dilatá el arte la mitad de la diferencia por lado hasta
   llegar. Si quedó muy por encima, avisame antes de adelgazar.
5. La silueta es la unión de los contornos exteriores del arte con todos los
   huecos tapados. Esa silueta maciza es la base de 1 mm y hace de fondo, así
   que el marcador no tiene ningún agujero pasante. Ojo: la silueta se usa solo
   como base, nunca para modificar los trazos.
6. Los trazos se extruyen desde z=0 hasta la altura total y se unen con la base.

## Cómo construir el cortador

Tres offsets sobre la silueta del marcador, con `join_style=round`:

- `o1 = silueta + 0,7`
- `o2 = silueta + 1,7`
- `o3 = silueta + 3,5`

El filo es `o2 − o1` extruido 10 mm. El pie es `o3 − o2` extruido 2 mm. Los dos
arrancan en z=0.

Dos detalles de implementación que evitan problemas conocidos:

- Extruí el pie como `o3 − o1` en vez de `o3 − o2`. Abajo de z=2 se solapa con
  el filo y la unión booleana no depende de dos caras coincidentes. La forma
  final es idéntica.
- Simplificá el arte **una sola vez** y derivá de ahí tanto la silueta como los
  offsets. Si simplificás la silueta y los trazos por separado, cada uno se
  mueve hasta la tolerancia y se te come la luz de 0,7 mm. Simplificá los
  offsets con tolerancia más fina (0,01 mm) por el mismo motivo.

## Salida

- Un solo `.3mf` en milímetros, con **dos objetos nombrados** (`marcador` y
  `cortador`), los dos apoyados en z=0 y en su posición anidada real.
- Cada objeto tiene que ser un sólido manifold cerrado: unilos con booleana
  real (`trimesh` + `manifold3d`), no con mallas superpuestas sueltas.
- Simplificá los contornos con tolerancia de 0,02 mm antes de triangular, para
  que el archivo no se vaya a decenas de miles de caras.

## Verificación antes de entregar

Mostrame:

1. Un render de vista superior con la base en gris, los trazos en negro, el
   filo en rojo y el pie en naranja.
2. **Chequeo de fidelidad**, que es lo que más me importa:
   - cantidad de contornos exteriores y de huecos en el arte original escalado
     contra el arte final del marcador — tienen que dar exactamente igual;
   - confirmación de que el arte original queda contenido íntegro en el final
     (área de la diferencia = 0);
   - percentiles del desvío del contorno final respecto al original (p50, p99,
     máximo), que no deberían pasar de la dilatación aplicada;
   - en sentido inverso, cuánto contorno del original quedó a más de 0,15 mm del
     final, en mm y en porcentaje: esas son las muescas que selló la dilatación.
3. Las medidas finales del marcador y del cortador, incluida la luz mínima real
   entre los dos sólidos y las secciones del cortador a z=1, z=5 y z=9,5
   comparadas contra el área esperada.
4. Los percentiles del ancho de trazo (p1, p5, mediana, p95) y los huecos que
   quedaron entre trazos (mínimo, p1, p5, mediana), más qué fracción del
   esqueleto de huecos está por debajo de 0,4 mm y de 1 mm. Reportalos como
   salgan; no los "arregles".
5. Confirmación de `is_watertight` en los dos objetos, releyendo el 3MF
   exportado y no la malla en memoria. Sumá el número de Euler: el marcador
   tiene que dar 2 (sin agujeros pasantes) y el cortador 0 (anillo cerrado).

Si algún dato del pedido parece un error de tipeo (por ejemplo una altura en cm
donde correspondería mm), construilo con el valor razonable y avisame.

---

## Si no tengo el SVG

Vectorizalo vos desde la imagen que adjunte. Para que salga bien mandame PNG en
blanco y negro puro, mínimo 1500 px del lado mayor, sin antialias, sin grises,
sin sombras ni degradés, líneas cerradas y fondo blanco. Nada de JPG: el ruido
de compresión ensucia el borde y aparecen dientes en el filo.
